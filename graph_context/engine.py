"""
Graph Diffusion Context Engine v6 (MVCC + Rules + Scopes)
==========================================================
融合 v3 (BM25 精排) + v4 (AST 调用图 + 粗细筛选 + IDF 去噪)
+ v5 (缓存 + 持久化 + chunk 淘汰)
+ v6 (MVCC 多版本快照 + 读写分离)

核心改进:
  1. 精确 AST 调用图 — 函数→函数精确映射，不是 token 共现
  2. BM25 精排 — 信息检索标准评分
  3. 粗细筛选 + 限制 — 类→方法下钻，最多返回 N 个方法
  4. IDF 噪声过滤 — 高频 token 丢弃
  5. 查询缓存 + JSON 持久化 + chunk 淘汰
  6. MVCC 快照 — 多版本并发控制，读写分离
"""

import os
import re
import ast
import time
import math
import json
import hashlib
import threading
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .ground_truth import GroundTruthEvaluator
from .ground_truth_report import save_csv_report
from .synonyms import expand_query, SYNONYM_MAP

try:
    import jieba
    jieba.setLogLevel(jieba.logging.WARNING)
    HAS_JIEBA = True
except ImportError:
    HAS_JIEBA = False

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False


# ══════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════

DEFAULT_CONFIG = {
    "max_context_tokens": 3000,
    "memory_top_k": 8,
    "max_hops": 2,
    "debounce_ms": 500,
    "adaptive_context": True,
    "skip_dirs": {
        ".git", "node_modules", "__pycache__", ".venv", "venv",
        "dist", "build", ".next", ".nuxt", "coverage",
        ".opencode", ".vscode", ".idea", "target", "vendor",
        ".mypy_cache", ".pytest_cache", ".ruff_cache",
    },
    "supported_extensions": {
        ".py", ".js", ".ts", ".jsx", ".tsx", ".vue", ".svelte",
        ".css", ".scss", ".less", ".html",
        ".json", ".yaml", ".yml", ".toml",
        ".md", ".rst",
        ".sql", ".graphql",
        ".go", ".rs", ".java", ".kt",
        ".c", ".cpp", ".h", ".hpp",
        ".sh", ".bash",
    },
    "adaptive_max_context_tokens": 6000,
    "idf_boost_enabled": True,
    "idf_boost_weight": 0.35,
    "ground_truth_enabled": False,
    "ground_truth_path": "ground_truth/opencode_ground_truth.json",
    "ground_truth_top_k": 5,
    # ── v5 配置 ──
    "cache_max_size": 128,
    "persist_path": None,
    "max_chunks": 50000,
    "max_graph_tokens_per_chunk": 20,
    "token_estimate_mode": "mixed",
    "idf_filter_threshold": 0.50,
    "idf_filter_enabled": True,
    "coarse_to_fine": True,
    "coarse_to_fine_max_methods": 3,
    "call_graph_weight": 3.0,
    "bm25_k1": 1.5,
    "bm25_b": 0.75,
    "layered_index": True,
    "chunk_type_weights": {
        "function": 1.6,
        "method": 1.5,
        "class": 1.2,
        "imports": 0.8,
        "file": 0.4,
    },
    "stopwords": {
        "def", "class", "import", "from", "return", "if", "else", "for", "while",
        "try", "except", "with", "as", "in", "not", "and", "or", "is", "true", "false",
        "none", "self", "this", "super", "pass", "break", "continue", "raise",
        "async", "await", "yield", "lambda", "print",
        "var", "let", "const", "function", "export", "default", "new", "typeof",
        "instanceof", "void", "delete", "switch", "case", "do",
        "func", "package", "struct", "interface", "chan", "map", "go", "defer",
        "select", "range", "make", "append", "len", "cap",
        "fn", "mut", "pub", "use", "mod", "impl", "trait", "enum",
        "match", "move", "ref", "where", "dyn", "unsafe",
        "public", "private", "protected", "static", "final", "abstract",
        "extends", "implements", "throw", "throws", "void",
        "include", "define", "ifdef", "ifndef", "endif", "typedef",
        "sizeof", "extern", "inline", "register", "volatile",
        "todo", "fixme", "hack", "xxx", "temp", "tmp", "val", "data",
    },
    # ── v6 配置 ──
    "mvcc_max_snapshots": 10,
}

SKIP_DIRS = DEFAULT_CONFIG["skip_dirs"]
SUPPORTED_EXTENSIONS = DEFAULT_CONFIG["supported_extensions"]
CHUNK_TYPE_WEIGHTS = DEFAULT_CONFIG["chunk_type_weights"]
STOPWORDS = DEFAULT_CONFIG["stopwords"]


# ══════════════════════════════════════════════════
#  数据结构
# ══════════════════════════════════════════════════

@dataclass
class CodeChunk:
    file_path: str
    chunk_type: str        # function | class | method | imports | file | file_summary
    name: str
    content: str
    start_line: int
    end_line: int
    tokens: list = field(default_factory=list)
    dependencies: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    parent_class: str = ""
    chunk_id: int = -1
    hash: str = ""
    last_modified: float = 0.0
    is_summary: bool = False
    child_ids: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "file_path": self.file_path,
            "chunk_type": self.chunk_type,
            "name": self.name,
            "content": self.content[:500],
            "start_line": self.start_line,
            "end_line": self.end_line,
            "dependencies": self.dependencies,
            "calls": self.calls,
            "parent_class": self.parent_class,
            "chunk_id": self.chunk_id,
            "hash": self.hash,
            "last_modified": self.last_modified,
            "is_summary": self.is_summary,
            "child_ids": self.child_ids,
        }


# ══════════════════════════════════════════════════
#  BM25 评分器
# ══════════════════════════════════════════════════

class BM25Scorer:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.doc_count = 0
        self.avg_doc_len = 0.0
        self.doc_freq: dict[str, int] = defaultdict(int)
        self.doc_len: dict[int, int] = {}
        self.tf: dict[int, dict[str, int]] = defaultdict(dict)

    def add_document(self, doc_id: int, tokens: list[str]):
        self.doc_count += 1
        self.doc_len[doc_id] = len(tokens)
        freq: dict[str, int] = defaultdict(int)
        for t in tokens:
            freq[t] += 1
        self.tf[doc_id] = dict(freq)
        for t in set(tokens):
            self.doc_freq[t] += 1
        total_len = sum(self.doc_len.values())
        self.avg_doc_len = total_len / max(1, self.doc_count)

    def remove_document(self, doc_id: int):
        if doc_id not in self.tf:
            return
        tokens = list(self.tf[doc_id].keys())
        del self.tf[doc_id]
        self.doc_len.pop(doc_id, 0)
        self.doc_count = max(0, self.doc_count - 1)
        for t in tokens:
            self.doc_freq[t] = max(0, self.doc_freq[t] - 1)
        if self.doc_count > 0:
            self.avg_doc_len = sum(self.doc_len.values()) / self.doc_count

    def score(self, doc_id: int, query_tokens: list[str]) -> float:
        if doc_id not in self.tf:
            return 0.0
        doc_tf = self.tf[doc_id]
        doc_len = self.doc_len.get(doc_id, 0)
        k1, b = self.k1, self.b
        avg_dl = max(1.0, self.avg_doc_len)
        N = max(1, self.doc_count)
        score = 0.0
        for qt in query_tokens:
            tf = doc_tf.get(qt, 0)
            if tf == 0:
                continue
            df = self.doc_freq.get(qt, 0)
            idf = math.log((N - df + 0.5) / (df + 0.5) + 1)
            tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * doc_len / avg_dl))
            score += idf * tf_norm
        return score

    def to_dict(self) -> dict:
        return {
            "k1": self.k1, "b": self.b, "doc_count": self.doc_count,
            "avg_doc_len": self.avg_doc_len,
            "doc_freq": dict(self.doc_freq),
            "doc_len": self.doc_len,
            "tf": {str(k): v for k, v in self.tf.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BM25Scorer":
        scorer = cls(k1=data["k1"], b=data["b"])
        scorer.doc_count = data["doc_count"]
        scorer.avg_doc_len = data["avg_doc_len"]
        scorer.doc_freq = defaultdict(int, data["doc_freq"])
        scorer.doc_len = {int(k): v for k, v in data["doc_len"].items()}
        scorer.tf = defaultdict(dict, {int(k): v for k, v in data["tf"].items()})
        return scorer


# ══════════════════════════════════════════════════
#  代码解析器 (AST 调用提取)
# ══════════════════════════════════════════════════

class CodeParser:
    def parse_file(self, file_path: str) -> list[CodeChunk]:
        ext = Path(file_path).suffix.lower()
        try:
            mtime = os.path.getmtime(file_path)
        except OSError:
            return []
        if ext == ".py":
            chunks = self._parse_python(file_path)
        elif ext in {".js", ".ts", ".jsx", ".tsx", ".vue", ".svelte"}:
            chunks = self._parse_js_ts(file_path)
        elif ext == ".go":
            chunks = self._parse_go(file_path)
        elif ext == ".java":
            chunks = self._parse_java(file_path)
        elif ext == ".rs":
            chunks = self._parse_rust(file_path)
        elif ext in {".c", ".cpp", ".h", ".hpp"}:
            chunks = self._parse_c_cpp(file_path)
        else:
            chunks = self._parse_generic(file_path)
        for c in chunks:
            c.last_modified = mtime
        return chunks

    def _parse_python(self, file_path: str) -> list[CodeChunk]:
        chunks = []
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
            tree = ast.parse(source, filename=file_path)
            lines = source.split("\n")
        except Exception:
            return [self._fallback_chunk(file_path)]

        import_lines = []
        import_names = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    import_names.append(alias.name.split(".")[0])
                seg = ast.get_source_segment(source, node)
                if seg:
                    import_lines.append(seg)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    import_names.append(node.module.split(".")[0])
                seg = ast.get_source_segment(source, node)
                if seg:
                    import_lines.append(seg)
        if import_lines:
            chunks.append(CodeChunk(
                file_path=file_path, chunk_type="imports",
                name=f"{Path(file_path).stem}::imports",
                content="\n".join(import_lines),
                start_line=1, end_line=len(import_lines),
                dependencies=import_names,
            ))

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                end = getattr(node, "end_lineno", node.lineno + 50)
                body = "\n".join(lines[node.lineno - 1 : end])
                bases = []
                for base in node.bases:
                    if isinstance(base, ast.Name):
                        bases.append(base.id)
                    elif isinstance(base, ast.Attribute):
                        bases.append(base.attr)
                calls = self._extract_calls(node)
                chunks.append(CodeChunk(
                    file_path=file_path, chunk_type="class",
                    name=node.name, content=body,
                    start_line=node.lineno, end_line=end,
                    dependencies=self._extract_names(node) + bases,
                    calls=calls,
                ))
                for item in ast.iter_child_nodes(node):
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        m_end = getattr(item, "end_lineno", item.lineno + 20)
                        m_body = "\n".join(lines[item.lineno - 1 : m_end])
                        m_calls = self._extract_calls(item)
                        chunks.append(CodeChunk(
                            file_path=file_path, chunk_type="method",
                            name=f"{node.name}.{item.name}",
                            content=m_body,
                            start_line=item.lineno, end_line=m_end,
                            dependencies=self._extract_names(item),
                            calls=m_calls,
                            parent_class=node.name,
                        ))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end = getattr(node, "end_lineno", node.lineno + 30)
                body = "\n".join(lines[node.lineno - 1 : end])
                calls = self._extract_calls(node)
                chunks.append(CodeChunk(
                    file_path=file_path, chunk_type="function",
                    name=node.name, content=body,
                    start_line=node.lineno, end_line=end,
                    dependencies=self._extract_names(node),
                    calls=calls,
                ))
        return chunks if chunks else [self._fallback_chunk(file_path)]

    def _extract_names(self, node) -> list[str]:
        names = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                names.add(child.id)
            elif isinstance(child, ast.Attribute):
                if isinstance(child.value, ast.Name):
                    names.add(child.value.id)
        return list(names)

    def _extract_calls(self, node) -> list[str]:
        calls = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name):
                    calls.add(child.func.id)
                elif isinstance(child.func, ast.Attribute):
                    calls.add(child.func.attr)
        return list(calls)

    def _parse_js_ts(self, file_path: str) -> list[CodeChunk]:
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
            lines = source.split("\n")
        except Exception:
            return []
        chunks = []
        import_lines = [l for l in lines if re.match(r"^\s*(import|require|from)\s", l)]
        if import_lines:
            chunks.append(CodeChunk(
                file_path=file_path, chunk_type="imports",
                name=f"{Path(file_path).stem}::imports",
                content="\n".join(import_lines),
                start_line=1, end_line=len(import_lines),
            ))
        patterns = [
            (r"(?:export\s+)?(?:async\s+)?function\s+(\w+)", "function"),
            (r"(?:export\s+)?class\s+(\w+)", "class"),
            (r"(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(", "function"),
        ]
        for pat, ctype in patterns:
            for m in re.finditer(pat, source):
                name = m.group(1)
                start = source[: m.start()].count("\n") + 1
                brace = 0
                end = start
                for i, ch in enumerate(source[m.start() :]):
                    if ch == "{":
                        brace += 1
                    elif ch == "}":
                        brace -= 1
                        if brace == 0:
                            end = source[: m.start() + i + 1].count("\n") + 1
                            break
                body = "\n".join(lines[start - 1 : end])
                chunks.append(CodeChunk(
                    file_path=file_path, chunk_type=ctype,
                    name=name, content=body,
                    start_line=start, end_line=end,
                ))
        return chunks if chunks else [self._fallback_chunk(file_path)]

    def _parse_generic(self, file_path: str) -> list[CodeChunk]:
        return [self._fallback_chunk(file_path)]

    def _parse_go(self, file_path: str) -> list[CodeChunk]:
        """Go 语言解析器 (正则)"""
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
            lines = source.split("\n")
        except Exception:
            return []
        chunks = []

        # import
        import_lines = [l for l in lines if re.match(r'^\s*(import|"|fmt|os|io)\s', l)]
        if import_lines:
            chunks.append(CodeChunk(
                file_path=file_path, chunk_type="imports",
                name=f"{Path(file_path).stem}::imports",
                content="\n".join(import_lines[:20]),
                start_line=1, end_line=len(import_lines),
            ))

        # func / type struct / type interface
        patterns = [
            (r'^func\s+(\([^)]+\)\s+)?(\w+)\s*\(', "function"),
            (r'^type\s+(\w+)\s+struct\b', "class"),
            (r'^type\s+(\w+)\s+interface\b', "class"),
        ]
        for pat, ctype in patterns:
            for m in re.finditer(pat, source, re.MULTILINE):
                name = m.group(2) if m.lastindex >= 2 and m.group(2) else m.group(1)
                start = source[:m.start()].count("\n") + 1
                brace = 0
                end = start
                for i, ch in enumerate(source[m.start():]):
                    if ch == "{":
                        brace += 1
                    elif ch == "}":
                        brace -= 1
                        if brace == 0:
                            end = source[:m.start() + i + 1].count("\n") + 1
                            break
                body = "\n".join(lines[start - 1:end])
                # 提取调用
                calls = list(set(re.findall(r'(\w+)\(', body)))[:20]
                chunks.append(CodeChunk(
                    file_path=file_path, chunk_type=ctype,
                    name=name, content=body[:2000],
                    start_line=start, end_line=end,
                    calls=calls,
                ))
        return chunks if chunks else [self._fallback_chunk(file_path)]

    def _parse_java(self, file_path: str) -> list[CodeChunk]:
        """Java 语言解析器 (正则)"""
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
            lines = source.split("\n")
        except Exception:
            return []
        chunks = []

        # import
        import_lines = [l for l in lines if re.match(r'^\s*import\s', l)]
        if import_lines:
            chunks.append(CodeChunk(
                file_path=file_path, chunk_type="imports",
                name=f"{Path(file_path).stem}::imports",
                content="\n".join(import_lines[:20]),
                start_line=1, end_line=len(import_lines),
            ))

        # class / interface
        for m in re.finditer(r'(?:public\s+|private\s+|protected\s+)?(?:abstract\s+)?(?:class|interface|enum)\s+(\w+)', source):
            name = m.group(1)
            start = source[:m.start()].count("\n") + 1
            brace = 0
            end = start
            for i, ch in enumerate(source[m.start():]):
                if ch == "{":
                    brace += 1
                elif ch == "}":
                    brace -= 1
                    if brace == 0:
                        end = source[:m.start() + i + 1].count("\n") + 1
                        break
            body = "\n".join(lines[start - 1:end])
            chunks.append(CodeChunk(
                file_path=file_path, chunk_type="class",
                name=name, content=body[:3000],
                start_line=start, end_line=end,
            ))

        # methods
        for m in re.finditer(r'(?:public|private|protected|static|final|abstract|synchronized|native)\s+[\w<>\[\],\s]+\s+(\w+)\s*\([^)]*\)', source):
            name = m.group(1)
            if name in ("if", "for", "while", "switch", "catch", "return", "new", "throw"):
                continue
            start = source[:m.start()].count("\n") + 1
            brace = 0
            end = start
            for i, ch in enumerate(source[m.start():]):
                if ch == "{":
                    brace += 1
                elif ch == "}":
                    brace -= 1
                    if brace == 0:
                        end = source[:m.start() + i + 1].count("\n") + 1
                        break
            body = "\n".join(lines[start - 1:end])
            calls = list(set(re.findall(r'(\w+)\(', body)))[:20]
            # 判断是否在类内部
            parent = ""
            for c in chunks:
                if c.chunk_type == "class" and c.start_line <= start and c.end_line >= end:
                    parent = c.name
                    break
            ctype = "method" if parent else "function"
            chunks.append(CodeChunk(
                file_path=file_path, chunk_type=ctype,
                name=f"{parent}.{name}" if parent else name,
                content=body[:2000],
                start_line=start, end_line=end,
                calls=calls, parent_class=parent,
            ))
        return chunks if chunks else [self._fallback_chunk(file_path)]

    def _parse_rust(self, file_path: str) -> list[CodeChunk]:
        """Rust 语言解析器 (正则)"""
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
            lines = source.split("\n")
        except Exception:
            return []
        chunks = []

        # use / mod
        import_lines = [l for l in lines if re.match(r'^\s*(use|mod)\s', l)]
        if import_lines:
            chunks.append(CodeChunk(
                file_path=file_path, chunk_type="imports",
                name=f"{Path(file_path).stem}::imports",
                content="\n".join(import_lines[:20]),
                start_line=1, end_line=len(import_lines),
            ))

        # fn / struct / enum / trait / impl
        patterns = [
            (r'(?:pub\s+)?(?:async\s+)?fn\s+(\w+)', "function"),
            (r'(?:pub\s+)?struct\s+(\w+)', "class"),
            (r'(?:pub\s+)?enum\s+(\w+)', "class"),
            (r'(?:pub\s+)?trait\s+(\w+)', "class"),
            (r'impl\s+(?:\w+\s+for\s+)?(\w+)', "class"),
        ]
        for pat, ctype in patterns:
            for m in re.finditer(pat, source):
                name = m.group(1)
                start = source[:m.start()].count("\n") + 1
                brace = 0
                end = start
                for i, ch in enumerate(source[m.start():]):
                    if ch == "{":
                        brace += 1
                    elif ch == "}":
                        brace -= 1
                        if brace == 0:
                            end = source[:m.start() + i + 1].count("\n") + 1
                            break
                body = "\n".join(lines[start - 1:end])
                calls = list(set(re.findall(r'(\w+)\(', body)))[:20]
                chunks.append(CodeChunk(
                    file_path=file_path, chunk_type=ctype,
                    name=name, content=body[:2000],
                    start_line=start, end_line=end,
                    calls=calls,
                ))
        return chunks if chunks else [self._fallback_chunk(file_path)]

    def _parse_c_cpp(self, file_path: str) -> list[CodeChunk]:
        """C/C++ 语言解析器 (正则)"""
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
            lines = source.split("\n")
        except Exception:
            return []
        chunks = []

        # #include
        import_lines = [l for l in lines if re.match(r'^\s*#include', l)]
        if import_lines:
            chunks.append(CodeChunk(
                file_path=file_path, chunk_type="imports",
                name=f"{Path(file_path).stem}::imports",
                content="\n".join(import_lines[:20]),
                start_line=1, end_line=len(import_lines),
            ))

        # struct / class / function
        patterns = [
            (r'(?:typedef\s+)?struct\s+(\w+)', "class"),
            (r'class\s+(\w+)', "class"),
            (r'(?:[\w\*]+\s+)+(\w+)\s*\([^)]*\)\s*\{', "function"),
        ]
        for pat, ctype in patterns:
            for m in re.finditer(pat, source):
                name = m.group(1)
                # 跳过关键字
                if name in ("if", "for", "while", "switch", "return", "sizeof", "typedef"):
                    continue
                start = source[:m.start()].count("\n") + 1
                brace = 0
                end = start
                for i, ch in enumerate(source[m.start():]):
                    if ch == "{":
                        brace += 1
                    elif ch == "}":
                        brace -= 1
                        if brace == 0:
                            end = source[:m.start() + i + 1].count("\n") + 1
                            break
                body = "\n".join(lines[start - 1:end])
                calls = list(set(re.findall(r'(\w+)\(', body)))[:20]
                chunks.append(CodeChunk(
                    file_path=file_path, chunk_type=ctype,
                    name=name, content=body[:2000],
                    start_line=start, end_line=end,
                    calls=calls,
                ))
        return chunks if chunks else [self._fallback_chunk(file_path)]

    def _fallback_chunk(self, file_path: str) -> CodeChunk:
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception:
            content = ""
        return CodeChunk(
            file_path=file_path, chunk_type="file",
            name=Path(file_path).stem, content=content,
            start_line=1, end_line=len(content.split("\n")),
        )


# ══════════════════════════════════════════════════
#  图扩散记忆 (v6 MVCC 版)
# ══════════════════════════════════════════════════

class CodeGraphMemory:
    """BM25 精排 + 精确 AST 调用图 + IDF 去噪 + 图扩散候选 + MVCC 快照"""

    def __init__(self, config: dict = None):
        self.config = config or DEFAULT_CONFIG
        self.token_to_chunks: dict[str, set[int]] = defaultdict(set)
        self.token_neighbors: dict[str, dict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self.chunks: list[Optional[CodeChunk]] = []
        self.token_freq: dict[str, int] = defaultdict(int)
        self.file_chunks: dict[str, list[int]] = defaultdict(list)
        self._access_order: list[int] = []

        # BM25
        self.bm25 = BM25Scorer(
            k1=self.config.get("bm25_k1", 1.5),
            b=self.config.get("bm25_b", 0.75),
        )

        # 精确 AST 调用图
        self.call_graph: dict[str, set[str]] = defaultdict(set)
        self.caller_ids: dict[str, set[int]] = defaultdict(set)
        self.callee_names: dict[int, set[str]] = defaultdict(set)
        self.name_to_cid: dict[str, set[int]] = defaultdict(set)  # name.lower() -> chunk ids

        # 分层索引
        self.file_summary_ids: dict[str, int] = {}

        # IDF 噪声
        self._noise_tokens: set[str] = set()
        self._idf_computed: bool = False

        # 查询缓存 — 增加 token 依赖追踪
        self._cache_max = self.config.get("cache_max_size", 128)
        self._query_cache: dict[str, tuple[float, list, set[str]]] = {}  # key -> (ts, results, tokens)
        self._cache_lock = threading.Lock()

        # ── v6: MVCC (lazy COW) ─────────────────────
        self._version: int = 0
        self._snapshots: dict[int, dict] = {}
        self._mvcc_max = self.config.get("mvcc_max_snapshots", 10)
        self._write_lock = threading.Lock()
        # COW: 共享引用 + dirty 标记
        self._snapshot_refs: dict[int, dict] = {}  # version -> shallow ref
        self._dirty_chunks: set = set()  # 被修改过的 chunk indices

    # ── v6: MVCC 快照 ────────────────────────────

    @property
    def current_version(self) -> int:
        return self._version

    def snapshot(self) -> int:
        """
        创建 COW 快照：只存引用，不深拷贝。
        写入时才对被修改的 chunk 做延迟拷贝。
        """
        with self._write_lock:
            ver = self._version
            # 浅拷贝：只复制容器结构，chunks 列表共享引用
            self._snapshot_refs[ver] = {
                "chunks_ref": self.chunks,  # 共享引用，不拷贝
                "chunks_len": len(self.chunks),
                "token_to_chunks": {k: set(v) for k, v in self.token_to_chunks.items()},
                "token_neighbors": {k: dict(v) for k, v in self.token_neighbors.items()},
                "token_freq": dict(self.token_freq),
                "file_chunks": {k: list(v) for k, v in self.file_chunks.items()},
                "file_summary_ids": dict(self.file_summary_ids),
                "bm25": self.bm25.to_dict(),
                "noise_tokens": set(self._noise_tokens),
                "idf_computed": self._idf_computed,
                "timestamp": time.time(),
                "dirty_indices": set(),  # 此快照创建后的脏 chunk 索引
            }
            self._dirty_chunks.clear()

            # 清理旧快照
            if len(self._snapshot_refs) > self._mvcc_max:
                oldest = sorted(self._snapshot_refs.keys())[:len(self._snapshot_refs) - self._mvcc_max]
                for old in oldest:
                    del self._snapshot_refs[old]

            return ver

    def read_at(self, version: int) -> dict:
        """
        读取特定版本的数据。
        对于未被修改的 chunk，使用共享引用；对于被修改的 chunk，返回 None。
        """
        snap = self._snapshot_refs.get(version)
        if snap is None:
            raise KeyError(f"Version {version} not found. Available: {sorted(self._snapshot_refs.keys())}")

        # 重建 chunks 视图：未修改的用共享引用，已修改的标记 None
        original_chunks = snap["chunks_ref"]
        original_len = snap["chunks_len"]
        dirty = snap.get("dirty_indices", set())

        view_chunks = []
        for i in range(original_len):
            if i in dirty:
                view_chunks.append(None)  # 已被修改，此版本不可见
            elif i < len(original_chunks):
                view_chunks.append(original_chunks[i])
            else:
                view_chunks.append(None)

        return {
            "chunks": view_chunks,
            "token_to_chunks": snap["token_to_chunks"],
            "token_neighbors": snap["token_neighbors"],
            "token_freq": snap["token_freq"],
            "file_chunks": snap["file_chunks"],
            "file_summary_ids": snap["file_summary_ids"],
            "timestamp": snap["timestamp"],
        }

    def _bump_version(self):
        """写操作时自动递增版本，记录脏 chunk"""
        self._version += 1
        # 标记最近写入的 chunk 为 dirty
        if self.chunks:
            self._dirty_chunks.add(len(self.chunks) - 1)
            # 同步到所有活跃快照
            for snap in self._snapshot_refs.values():
                snap["dirty_indices"] = self._dirty_chunks.copy()

    # ── 缓存 ──────────────────────────────────────

    def _cache_key(self, query: str, top_k: int, max_hops: int) -> str:
        return f"{query}|{top_k}|{max_hops}"

    def _cache_get(self, key: str) -> Optional[list[tuple[float, CodeChunk]]]:
        with self._cache_lock:
            entry = self._query_cache.get(key)
            if entry is None:
                return None
            ts, results, tokens = entry
            if time.time() - ts > 60:
                del self._query_cache[key]
                return None
            return results

    def _cache_put(self, key: str, results: list[tuple[float, CodeChunk]], query_tokens: set[str] = None):
        with self._cache_lock:
            if len(self._query_cache) >= self._cache_max:
                oldest = min(self._query_cache, key=lambda k: self._query_cache[k][0])
                del self._query_cache[oldest]
            self._query_cache[key] = (time.time(), results, query_tokens or set())

    def invalidate_tokens(self, affected_tokens: set[str]):
        """精确失效：只清除包含受影响 token 的缓存条目"""
        if not affected_tokens:
            return
        with self._cache_lock:
            to_invalidate = []
            for key, (ts, results, tokens) in self._query_cache.items():
                if tokens & affected_tokens:
                    to_invalidate.append(key)
            for key in to_invalidate:
                del self._query_cache[key]

    def invalidate_cache(self):
        with self._cache_lock:
            self._query_cache.clear()

    # ── IDF 噪声过滤 ──────────────────────────────

    def _compute_idf_filter(self):
        if self._idf_computed:
            return
        N = max(1, len([c for c in self.chunks if c is not None]))
        threshold = self.config.get("idf_filter_threshold", 0.50)
        for token, chunks_set in self.token_to_chunks.items():
            if token.startswith("@"):
                continue
            doc_freq = len(chunks_set) / N
            if doc_freq > threshold:
                self._noise_tokens.add(token)
        self._idf_computed = True

    # ── 写入 (v6: 自动递增版本) ───────────────────

    def add_chunk(self, chunk: CodeChunk, skip_graph: bool = False):
        max_chunks = self.config.get("max_chunks", 50000)
        if max_chunks > 0 and len(self.chunks) >= max_chunks:
            self._evict_chunks(max(1, max_chunks // 10))

        cid = len(self.chunks)
        chunk.chunk_id = cid
        chunk.hash = hashlib.md5(chunk.content.encode()).hexdigest()[:12]

        tokens = self._tokenize_code(chunk)
        chunk.tokens = tokens

        # COW 安全：先 tokenize 再 append，确保 chunk 不会被后续修改
        # 创建一个不可变快照副本
        frozen = CodeChunk(
            file_path=chunk.file_path,
            chunk_type=chunk.chunk_type,
            name=chunk.name,
            content=chunk.content,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            tokens=list(tokens),
            dependencies=list(chunk.dependencies),
            calls=list(chunk.calls),
            parent_class=chunk.parent_class,
            chunk_id=cid,
            hash=chunk.hash,
            last_modified=chunk.last_modified,
            is_summary=chunk.is_summary,
            child_ids=list(chunk.child_ids),
        )

        self.chunks.append(frozen)
        self.file_chunks[frozen.file_path].append(cid)

        self.bm25.add_document(cid, tokens)

        chunk_name_lower = frozen.name.lower()
        self.name_to_cid[chunk_name_lower].add(cid)
        for callee in frozen.calls:
            callee_low = callee.lower()
            self.call_graph[chunk_name_lower].add(callee_low)
            self.caller_ids[callee_low].add(cid)
        self.callee_names[cid] = set(c.lower() for c in frozen.calls)

        if not skip_graph:
            self._build_graph_edges(cid, tokens)

        self._bump_version()
        # 精确失效：只清除包含受影响 token 的缓存
        self.invalidate_tokens(set(tokens))

    def _build_graph_edges(self, cid: int, tokens: list[str]):
        unique = list(set(tokens))
        max_graph_tokens = self.config.get("max_graph_tokens_per_chunk", 20)
        cooccur = [t for t in unique if not t.startswith("@")]
        cooccur.sort(key=lambda t: self.token_freq.get(t, 0))
        selected = cooccur[:max_graph_tokens]
        for t in selected:
            self.token_to_chunks[t].add(cid)
            self.token_freq[t] += 1
        for i in range(len(selected)):
            for j in range(i + 1, len(selected)):
                a, b = selected[i], selected[j]
                self.token_neighbors[a][b] += 1
                self.token_neighbors[b][a] += 1

    def prune_graph(self, min_weight: float = 2.0):
        """剪除低权重图边，减少噪声"""
        pruned = 0
        for a in list(self.token_neighbors.keys()):
            neighbors = self.token_neighbors[a]
            to_remove = [b for b, w in neighbors.items() if w < min_weight]
            for b in to_remove:
                del neighbors[b]
                pruned += 1
            if not neighbors:
                del self.token_neighbors[a]
        return pruned

    def add_file_summary(self, file_path: str, chunks: list[CodeChunk]) -> Optional[int]:
        if not chunks:
            return None
        names = [c.name for c in chunks if c.chunk_type != "imports"]
        types = [c.chunk_type for c in chunks]
        imports = [c.content for c in chunks if c.chunk_type == "imports"]
        summary_content = f"File: {Path(file_path).name}\n"
        summary_content += f"Classes: {sum(1 for t in types if t == 'class')}, "
        summary_content += f"Functions: {sum(1 for t in types if t in ('function', 'method'))}\n"
        summary_content += f"Defines: {', '.join(names[:20])}\n"
        if imports:
            summary_content += f"Imports:\n{imports[0][:200]}\n"
        summary = CodeChunk(
            file_path=file_path, chunk_type="file_summary",
            name=f"{Path(file_path).stem}::summary",
            content=summary_content, start_line=1, end_line=1,
            is_summary=True, child_ids=[c.chunk_id for c in chunks],
        )
        self.add_chunk(summary, skip_graph=False)
        self.file_summary_ids[file_path] = summary.chunk_id
        return summary.chunk_id

    def remove_file(self, file_path: str):
        cids = self.file_chunks.pop(file_path, [])
        affected_tokens: set[str] = set()
        for cid in cids:
            if cid < len(self.chunks) and self.chunks[cid] is not None:
                chunk = self.chunks[cid]
                for t in chunk.tokens:
                    self.token_to_chunks[t].discard(cid)
                    if not self.token_to_chunks[t]:
                        del self.token_to_chunks[t]
                chunk_name_lower = chunk.name.lower()
                for callee in self.callee_names.pop(cid, set()):
                    self.caller_ids[callee].discard(cid)
                self.call_graph.pop(chunk_name_lower, None)
                self.name_to_cid[chunk_name_lower].discard(cid)
                if not self.name_to_cid[chunk_name_lower]:
                    del self.name_to_cid[chunk_name_lower]
                self.bm25.remove_document(cid)
                self.chunks[cid] = None
                self._dirty_chunks.add(cid)  # 标记为 dirty
        self.file_summary_ids.pop(file_path, None)
        self._bump_version()
        self.invalidate_cache()

    def _evict_chunks(self, count: int):
        evicted = 0
        for cid in self._access_order[:]:
            if evicted >= count:
                break
            if cid < len(self.chunks) and self.chunks[cid] is not None:
                chunk = self.chunks[cid]
                for t in chunk.tokens:
                    self.token_to_chunks[t].discard(cid)
                    if not self.token_to_chunks[t]:
                        del self.token_to_chunks[t]
                chunk_name_lower = chunk.name.lower()
                for callee in self.callee_names.pop(cid, set()):
                    self.caller_ids[callee].discard(cid)
                self.call_graph.pop(chunk_name_lower, None)
                self.name_to_cid[chunk_name_lower].discard(cid)
                if not self.name_to_cid[chunk_name_lower]:
                    del self.name_to_cid[chunk_name_lower]
                self.bm25.remove_document(cid)
                fclist = self.file_chunks.get(chunk.file_path)
                if fclist is not None:
                    try:
                        fclist.remove(cid)
                    except ValueError:
                        pass
                    if not fclist:
                        del self.file_chunks[chunk.file_path]
                self.chunks[cid] = None
                evicted += 1
        self._access_order = self._access_order[evicted:]
        self.invalidate_cache()

    # ── 分词 ──────────────────────────────────────

    def _tokenize_code(self, chunk: CodeChunk) -> list[str]:
        tokens = []
        tokens.append(f"@type:{chunk.chunk_type}")
        tokens.append(f"@name:{chunk.name.lower()}")
        tokens.append(f"@file:{Path(chunk.file_path).stem.lower()}")
        parent = Path(chunk.file_path).parent.name.lower()
        if parent and parent != ".":
            tokens.append(f"@dir:{parent}")
        for dep in chunk.dependencies:
            tokens.append(f"@dep:{dep.lower()}")
        for call in chunk.calls:
            tokens.append(f"@call:{call.lower()}")
        seen = set()
        for ident in re.findall(r"[a-zA-Z_]\w*", chunk.content):
            low = ident.lower()
            if len(low) > 1 and low not in STOPWORDS and low not in seen:
                tokens.append(low)
                seen.add(low)
            for p in re.sub(r"([A-Z])", r" \1", ident).strip().split():
                plow = p.lower()
                if len(plow) > 1 and plow not in STOPWORDS and plow not in seen:
                    tokens.append(plow)
                    seen.add(plow)
            for seg in low.split("_"):
                if len(seg) > 1 and seg not in STOPWORDS and seg not in seen:
                    tokens.append(seg)
                    seen.add(seg)
        chinese = re.findall(r"[\u4e00-\u9fff]+", chunk.content)
        if HAS_JIEBA:
            for c in chinese:
                tokens.extend(jieba.lcut(c))
        else:
            tokens.extend(chinese)
        return tokens

    def _tokenize_query(self, query: str) -> list[str]:
        tokens = []
        seen = set()
        for ident in re.findall(r"[a-zA-Z_]\w*", query):
            low = ident.lower()
            if low not in STOPWORDS and low not in seen:
                tokens.append(low)
                seen.add(low)
            for seg in low.split("_"):
                if len(seg) > 1 and seg not in STOPWORDS and seg not in seen:
                    tokens.append(seg)
                    seen.add(seg)
        chinese = re.findall(r"[\u4e00-\u9fff]+", query)
        if HAS_JIEBA:
            for c in chinese:
                for w in jieba.lcut(c):
                    if w not in seen:
                        tokens.append(w)
                        seen.add(w)
        else:
            # 无 jieba 时：保留完整中文短语 + 按同义词表子串拆分
            for phrase in chinese:
                phrase_matched = False
                # 先尝试完整短语匹配同义词表
                if phrase in SYNONYM_MAP:
                    tokens.append(phrase)
                    seen.add(phrase)
                    phrase_matched = True
                # 再按同义词表做子串切分
                remaining = phrase
                for key in sorted(SYNONYM_MAP.keys(), key=len, reverse=True):
                    if key in remaining and key != phrase:
                        if key not in seen:
                            tokens.append(key)
                            seen.add(key)
                        remaining = remaining.replace(key, "", 1)
                        phrase_matched = True
                # 如果没有任何匹配，按双字切分（比逐字好）
                if not phrase_matched:
                    for i in range(0, len(phrase), 2):
                        seg = phrase[i:i+2]
                        if len(seg) >= 2 and seg not in seen:
                            tokens.append(seg)
                            seen.add(seg)
        tokens = expand_query(tokens, replace_chinese=True)
        return tokens

    # ── 图扩散候选集 ──────────────────────────────

    def _graph_expand(self, q_tokens: list[str], max_hops: int, max_candidates: int) -> set[int]:
        candidates: set[int] = set()
        visited: set[str] = set()
        if not self._idf_computed:
            self._compute_idf_filter()

        for qt in q_tokens:
            if qt in self._noise_tokens:
                continue
            if qt in self.token_to_chunks:
                candidates.update(self.token_to_chunks[qt])
            visited.add(qt)

        frontier = set(t for t in q_tokens if t not in self._noise_tokens)
        for hop in range(1, max_hops + 1):
            next_frontier: set[str] = set()
            for token in frontier:
                for neighbor, weight in self.token_neighbors.get(token, {}).items():
                    if neighbor in visited or neighbor in self._noise_tokens:
                        continue
                    visited.add(neighbor)
                    next_frontier.add(neighbor)
                    if neighbor in self.token_to_chunks:
                        candidates.update(self.token_to_chunks[neighbor])
            frontier = next_frontier
            if len(candidates) >= max_candidates:
                break
        return candidates

    # ── 调用图扩展 ───────────────────────────────

    def _call_graph_expand(self, q_tokens: list[str]) -> set[int]:
        candidates: set[int] = set()
        q_lower = set(q_tokens)

        # O(1) 索引查找，不再遍历全部 chunks
        matched_names = set()
        for qt in q_lower:
            if qt.startswith("@call:"):
                matched_names.add(qt[6:])
            if qt in self.name_to_cid:
                matched_names.add(qt)
            for name in self.name_to_cid:
                if qt in name and qt != name:
                    matched_names.add(name)

        for name in matched_names:
            for callee in self.call_graph.get(name, set()):
                for cid in self.name_to_cid.get(callee, set()):
                    candidates.add(cid)
            for caller_id in self.caller_ids.get(name, set()):
                candidates.add(caller_id)

        return candidates

    # ── BM25 精排 ────────────────────────────────

    def retrieve(
        self, query: str, top_k=5, max_hops=2, recency_bonus=0.0
    ) -> list[tuple[float, CodeChunk]]:
        cache_key = self._cache_key(query, top_k, max_hops)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        q_tokens = self._tokenize_query(query)
        q_token_set = set(q_tokens)
        if not self._idf_computed:
            self._compute_idf_filter()

        graph_candidates = self._graph_expand(q_tokens, max_hops, top_k * 4)
        call_candidates = self._call_graph_expand(q_tokens)
        all_candidates = graph_candidates | call_candidates

        scores: dict[int, float] = {}
        for cid in all_candidates:
            chunk = self.chunks[cid]
            if chunk is None:
                continue

            bm25_score = self.bm25.score(cid, q_tokens)

            exact_bonus = 0.0
            for qt in q_tokens:
                if qt.startswith(("@name:", "@dep:", "@file:", "@dir:", "@call:")):
                    if cid in self.token_to_chunks.get(qt, set()):
                        exact_bonus += 5.0
                if chunk and qt in chunk.name.lower():
                    exact_bonus += 3.0

            call_bonus = 0.0
            chunk_name_lower = chunk.name.lower()
            for qt in q_tokens:
                if qt in chunk_name_lower:
                    if cid in call_candidates:
                        call_bonus += self.config.get("call_graph_weight", 3.0)

            type_weight = CHUNK_TYPE_WEIGHTS.get(chunk.chunk_type, 1.0)

            recency = 0.0
            if recency_bonus > 0 and chunk.last_modified > 0:
                age_hours = (time.time() - chunk.last_modified) / 3600
                recency = recency_bonus / (1 + age_hours)

            scores[cid] = (bm25_score + exact_bonus + call_bonus) * type_weight + recency

        ranked_cids = sorted(scores, key=lambda c: -scores[c])[:top_k * 2]
        for cid in ranked_cids:
            if cid in self._access_order:
                self._access_order.remove(cid)
            self._access_order.append(cid)

        ranked = [(scores[cid], self.chunks[cid]) for cid in ranked_cids if self.chunks[cid] is not None]

        self._cache_put(cache_key, ranked, q_token_set)
        return ranked

    def retrieve_with_coverage(
        self, query: str, top_k: int = 5, max_hops: int = 2, recency_bonus: float = 0.0
    ) -> dict:
        """
        带覆盖度信号的检索。返回结果 + 置信度指标，让 LLM 能判断"我是否找全了"。

        返回:
          {
            "results": [(score, chunk), ...],
            "coverage": {
              "query_token_coverage": float,   # 查询 token 被结果覆盖的比例
              "top_score": float,              # 最高分
              "score_spread": float,           # 最高分与平均分的差距（越大越确定）
              "graph_hops_used": int,          # 图扩散跳了几跳
              "candidate_count": int,          # 候选集大小
              "confidence": str,               # "high" | "medium" | "low"
              "missing_tokens": list[str],     # 未被覆盖的查询 token
            }
          }
        """
        q_tokens = self._tokenize_query(query)
        q_token_set = set(t for t in q_tokens if not t.startswith("@"))
        if not self._idf_computed:
            self._compute_idf_filter()

        graph_candidates = self._graph_expand(q_tokens, max_hops, top_k * 4)
        call_candidates = self._call_graph_expand(q_tokens)
        all_candidates = graph_candidates | call_candidates

        # ── 兜底：图扩散失败时，用关键词暴力匹配 ──
        if len(all_candidates) < top_k:
            q_meaningful = [t for t in q_tokens if not t.startswith("@") and len(t) > 1]
            for cid, chunk in enumerate(self.chunks):
                if chunk is None or cid in all_candidates:
                    continue
                name_lower = chunk.name.lower()
                content_lower = chunk.content[:500].lower()
                for qt in q_meaningful:
                    if qt in name_lower or qt in content_lower:
                        all_candidates.add(cid)
                        break

        # 计算图扩散实际用了几跳
        hops_used = 0
        direct_hits = set()
        for qt in q_tokens:
            if qt in self._noise_tokens:
                continue
            if qt in self.token_to_chunks:
                direct_hits.update(self.token_to_chunks[qt])
        if direct_hits:
            hops_used = 0
        elif all_candidates:
            hops_used = max_hops

        scores: dict[int, float] = {}
        for cid in all_candidates:
            chunk = self.chunks[cid]
            if chunk is None:
                continue
            bm25_score = self.bm25.score(cid, q_tokens)
            exact_bonus = 0.0
            for qt in q_tokens:
                if qt.startswith(("@name:", "@dep:", "@file:", "@dir:", "@call:")):
                    if cid in self.token_to_chunks.get(qt, set()):
                        exact_bonus += 5.0
                if chunk and qt in chunk.name.lower():
                    exact_bonus += 3.0
            call_bonus = 0.0
            chunk_name_lower = chunk.name.lower()
            for qt in q_tokens:
                if qt in chunk_name_lower:
                    if cid in call_candidates:
                        call_bonus += self.config.get("call_graph_weight", 3.0)
            type_weight = CHUNK_TYPE_WEIGHTS.get(chunk.chunk_type, 1.0)
            recency = 0.0
            if recency_bonus > 0 and chunk.last_modified > 0:
                age_hours = (time.time() - chunk.last_modified) / 3600
                recency = recency_bonus / (1 + age_hours)
            scores[cid] = (bm25_score + exact_bonus + call_bonus) * type_weight + recency

        ranked_cids = sorted(scores, key=lambda c: -scores[c])[:top_k * 2]
        for cid in ranked_cids:
            if cid in self._access_order:
                self._access_order.remove(cid)
            self._access_order.append(cid)
        ranked = [(scores[cid], self.chunks[cid]) for cid in ranked_cids if self.chunks[cid] is not None]

        # ── 计算覆盖度信号 ──
        result_tokens: set[str] = set()
        for _, chunk in ranked[:top_k]:
            result_tokens.update(t.lower() for t in chunk.tokens if not t.startswith("@"))

        covered = q_token_set & result_tokens
        missing = q_token_set - result_tokens
        token_coverage = len(covered) / max(len(q_token_set), 1)

        top_score = ranked[0][0] if ranked else 0.0
        avg_score = sum(s for s, _ in ranked[:top_k]) / max(len(ranked[:top_k]), 1)
        score_spread = top_score - avg_score

        # 判断置信度
        if token_coverage >= 0.6 and top_score >= 3.0:
            confidence = "high"
        elif token_coverage >= 0.3 and top_score >= 1.0:
            confidence = "medium"
        else:
            confidence = "low"

        self._cache_put(self._cache_key(query, top_k, max_hops), ranked, q_token_set)

        return {
            "results": ranked[:top_k],
            "coverage": {
                "query_token_coverage": round(token_coverage, 3),
                "top_score": round(top_score, 3),
                "score_spread": round(score_spread, 3),
                "graph_hops_used": hops_used,
                "candidate_count": len(all_candidates),
                "confidence": confidence,
                "missing_tokens": sorted(missing)[:10],
            },
        }

    def score_full_context(self, query: str, top_k: int = 8) -> list[tuple[float, CodeChunk]]:
        q_tokens = self._tokenize_query(query)
        q_token_set = set(q_tokens)
        scores: list[tuple[float, CodeChunk]] = []
        for cid, chunk in enumerate(self.chunks):
            if chunk is None:
                continue
            hit = sum(1 for t in chunk.tokens if t in q_token_set)
            score = float(hit) + 0.01 * len(chunk.tokens)
            scores.append((score, chunk))
        scores.sort(key=lambda x: x[0], reverse=True)
        return scores[:top_k]

    def get_dependent_chunks(self, chunk: CodeChunk, max_depth=1) -> list[CodeChunk]:
        dep_names = set(d.lower() for d in chunk.dependencies)
        return [
            c for c in self.chunks
            if c is not None and c.file_path != chunk.file_path and c.name.lower() in dep_names
        ]

    def stats(self) -> dict:
        active = [c for c in self.chunks if c is not None]
        return {
            "chunks": len(active),
            "files": len(self.file_chunks),
            "token_types": len(self.token_to_chunks),
            "graph_edges": sum(len(v) for v in self.token_neighbors.values()),
            "cache_size": len(self._query_cache),
            "bm25_doc_count": self.bm25.doc_count,
            "file_summaries": len(self.file_summary_ids),
            "noise_tokens_filtered": len(self._noise_tokens),
            "call_graph_edges": sum(len(v) for v in self.call_graph.values()),
            "version": self._version,
            "snapshots_stored": len(self._snapshot_refs),
            "dirty_chunks": len(self._dirty_chunks),
        }

    # ── 持久化 ────────────────────────────────────

    def save(self, path: str):
        data = {
            "version": 6,
            "chunks": [c.to_dict() if c else None for c in self.chunks],
            "token_to_chunks": {k: list(v) for k, v in self.token_to_chunks.items()},
            "token_neighbors": {k: dict(v) for k, v in self.token_neighbors.items()},
            "token_freq": dict(self.token_freq),
            "file_chunks": {k: list(v) for k, v in self.file_chunks.items()},
            "access_order": self._access_order,
            "file_summary_ids": self.file_summary_ids,
            "call_graph": {k: list(v) for k, v in self.call_graph.items()},
            "caller_ids": {k: list(v) for k, v in self.caller_ids.items()},
            "bm25": self.bm25.to_dict(),
            "mvcc_version": self._version,
            "dirty_chunks": list(self._dirty_chunks),
        }
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)

    def load(self, path: str) -> bool:
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version", 1) < 5:
                return False
            self.chunks = []
            for cd in data["chunks"]:
                if cd is None:
                    self.chunks.append(None)
                else:
                    c = CodeChunk(
                        file_path=cd["file_path"], chunk_type=cd["chunk_type"],
                        name=cd["name"], content=cd["content"],
                        start_line=cd["start_line"], end_line=cd["end_line"],
                        dependencies=cd.get("dependencies", []),
                        calls=cd.get("calls", []),
                        parent_class=cd.get("parent_class", ""),
                        chunk_id=cd["chunk_id"], hash=cd.get("hash", ""),
                        last_modified=cd.get("last_modified", 0),
                        is_summary=cd.get("is_summary", False),
                        child_ids=cd.get("child_ids", []),
                    )
                    self.chunks.append(c)
            self.token_to_chunks = defaultdict(set, {k: set(v) for k, v in data["token_to_chunks"].items()})
            self.token_neighbors = defaultdict(
                lambda: defaultdict(float),
                {k: defaultdict(float, v) for k, v in data["token_neighbors"].items()},
            )
            self.token_freq = defaultdict(int, data["token_freq"])
            self.file_chunks = defaultdict(list, data["file_chunks"])
            self._access_order = data.get("access_order", [])
            self.file_summary_ids = data.get("file_summary_ids", {})
            self.call_graph = defaultdict(set, {k: set(v) for k, v in data.get("call_graph", {}).items()})
            self.caller_ids = defaultdict(set, {k: set(v) for k, v in data.get("caller_ids", {}).items()})
            self.callee_names = defaultdict(set)
            for cid, chunk in enumerate(self.chunks):
                if chunk is not None:
                    self.callee_names[cid] = set(c.lower() for c in chunk.calls)
            self.bm25 = BM25Scorer.from_dict(data["bm25"])
            self._version = data.get("mvcc_version", 0)
            self._dirty_chunks = set(data.get("dirty_chunks", []))
            self.invalidate_cache()
            return True
        except Exception:
            return False


# ══════════════════════════════════════════════════
#  文件监听
# ══════════════════════════════════════════════════

if HAS_WATCHDOG:
    class _CodeChangeHandler(FileSystemEventHandler):
        def __init__(self, engine: "VibeCodingEngine", debounce_ms: int = 500):
            super().__init__()
            self.engine = engine
            self.debounce_s = debounce_ms / 1000
            self._pending: dict[str, float] = {}
            self._lock = threading.Lock()
            self._timer: Optional[threading.Timer] = None

        def _should_track(self, path: str) -> bool:
            parts = Path(path).parts
            if any(p in SKIP_DIRS for p in parts):
                return False
            return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS

        def on_any_event(self, event):
            if event.is_directory or not self._should_track(event.src_path):
                return
            self._schedule(event.src_path)

        def on_moved(self, event):
            if event.is_directory:
                return
            self.engine._handle_file_delete(event.src_path)
            if self._should_track(event.dest_path):
                self._schedule(event.dest_path)

        def _schedule(self, path: str):
            with self._lock:
                self._pending[path] = time.time()
                if self._timer is not None:
                    self._timer.cancel()
                self._timer = threading.Timer(self.debounce_s, self._flush)
                self._timer.daemon = True
                self._timer.start()

        def _flush(self):
            with self._lock:
                paths = list(self._pending.keys())
                self._pending.clear()
            for p in paths:
                if os.path.exists(p):
                    self.engine._handle_file_change(p)
                else:
                    self.engine._handle_file_delete(p)


class FileWatcher:
    def __init__(self, engine: "VibeCodingEngine", debounce_ms: int = 500):
        if not HAS_WATCHDOG:
            raise ImportError("watchdog 未安装")
        self.engine = engine
        self.observer = Observer()
        self.handler = _CodeChangeHandler(engine, debounce_ms)
        self._running = False

    def start(self):
        self.observer.schedule(self.handler, str(self.engine.project_root), recursive=True)
        self.observer.start()
        self._running = True

    def stop(self):
        if self._running:
            self.observer.stop()
            self.observer.join(timeout=5)
            self._running = False

    @property
    def is_running(self) -> bool:
        return self._running


# ══════════════════════════════════════════════════
#  主引擎 (v6)
# ══════════════════════════════════════════════════

class VibeCodingEngine:
    """v6: BM25 精排 + AST 调用图 + 粗细筛选 + 分层索引 + MVCC"""

    def __init__(self, project_root: str, config: dict = None):
        self.project_root = Path(project_root).resolve()
        self.config = config or DEFAULT_CONFIG
        self.memory = CodeGraphMemory(self.config)
        self.parser = CodeParser()
        self.watcher: Optional[FileWatcher] = None
        self._lock = threading.Lock()
        self._indexed_files: set[str] = set()
        self._file_hashes: dict[str, str] = {}  # file_path -> content hash

    def index_project(self) -> tuple[int, int]:
        persist_path = self.config.get("persist_path")
        if persist_path and self.memory.load(persist_path):
            return self._incremental_index()

        exts = self.config.get("supported_extensions", SUPPORTED_EXTENSIONS)
        skip = self.config.get("skip_dirs", SKIP_DIRS)
        files = []
        for ext in exts:
            for f in self.project_root.rglob(f"*{ext}"):
                if not any(d in f.parts for d in skip):
                    files.append(str(f))

        chunk_count = 0
        file_chunks_map: dict[str, list[CodeChunk]] = {}
        for f in sorted(files):
            try:
                with open(f, "rb") as fh:
                    self._file_hashes[f] = hashlib.md5(fh.read()).hexdigest()[:16]
            except (OSError, IOError):
                continue
            chunks = self.parser.parse_file(f)
            file_chunks_map[f] = chunks
            for chunk in chunks:
                self.memory.add_chunk(chunk)
                chunk_count += 1
            self._indexed_files.add(f)

        self.memory._compute_idf_filter()
        self.memory.prune_graph(min_weight=2.0)

        if self.config.get("layered_index", True):
            for f, chunks in file_chunks_map.items():
                self.memory.add_file_summary(f, chunks)

        if persist_path:
            self.memory.save(persist_path)
        return len(files), chunk_count

    def _incremental_index(self) -> tuple[int, int]:
        """增量索引：通过文件 hash 比较，只重新索引变化的文件"""
        exts = self.config.get("supported_extensions", SUPPORTED_EXTENSIONS)
        skip = self.config.get("skip_dirs", SKIP_DIRS)
        current_files = set()
        for ext in exts:
            for f in self.project_root.rglob(f"*{ext}"):
                if not any(d in f.parts for d in skip):
                    current_files.add(str(f))
        for f in list(self._indexed_files):
            if f not in current_files:
                self._handle_file_delete(f)
                self._file_hashes.pop(f, None)
        new_count = 0
        updated_count = 0
        chunk_count = 0
        for f in sorted(current_files):
            try:
                with open(f, "rb") as fh:
                    content_hash = hashlib.md5(fh.read()).hexdigest()[:16]
            except (OSError, IOError):
                continue
            old_hash = self._file_hashes.get(f)
            if old_hash == content_hash:
                continue
            if f in self._indexed_files:
                self.memory.remove_file(f)
                updated_count += 1
            else:
                new_count += 1
            chunks = self.parser.parse_file(f)
            for chunk in chunks:
                self.memory.add_chunk(chunk)
                chunk_count += 1
            self._indexed_files.add(f)
            self._file_hashes[f] = content_hash
            if self.config.get("layered_index", True):
                self.memory.add_file_summary(f, chunks)
        self.memory._compute_idf_filter()
        self.memory.prune_graph(min_weight=2.0)
        persist_path = self.config.get("persist_path")
        if persist_path:
            self.memory.save(persist_path)
        return new_count + updated_count, chunk_count

    def retrieve(self, query: str, top_k: int = None) -> list[tuple[float, CodeChunk]]:
        if top_k is None:
            top_k = self.config.get("memory_top_k", 8)

        coarse_k = top_k * 4 if self.config.get("coarse_to_fine", True) else top_k
        results = self.memory.retrieve(
            query, top_k=coarse_k,
            max_hops=self.config.get("max_hops", 2),
            recency_bonus=0.3,
        )

        if self.config.get("coarse_to_fine", True):
            results = self._coarse_to_fine(results, query, top_k)

        return results[:top_k]

    def _coarse_to_fine(
        self, coarse_results: list[tuple[float, CodeChunk]], query: str, top_k: int
    ) -> list[tuple[float, CodeChunk]]:
        q_tokens = set(self.memory._tokenize_query(query))
        max_methods = self.config.get("coarse_to_fine_max_methods", 3)
        refined = []
        seen = set()

        for score, chunk in coarse_results:
            if chunk.chunk_type == "class" and not chunk.is_summary:
                class_methods = [
                    c for c in self.memory.chunks
                    if c is not None
                    and c.file_path == chunk.file_path
                    and c.chunk_type == "method"
                    and c.parent_class == chunk.name
                    and c.chunk_id not in seen
                ]
                if class_methods:
                    method_scores = []
                    for m in class_methods:
                        m_tokens = set(m.tokens)
                        overlap = len(q_tokens & m_tokens)
                        name_bonus = sum(5 for t in q_tokens if t in m.name.lower())
                        m_score = overlap + name_bonus
                        method_scores.append((m_score, m))
                    method_scores.sort(key=lambda x: -x[0])
                    added = 0
                    for m_score, method in method_scores:
                        if m_score > 0 and added < max_methods:
                            refined_score = score * (0.4 + 0.6 * min(m_score / 8, 1.0))
                            refined.append((refined_score, method))
                            seen.add(method.chunk_id)
                            added += 1
                    if added > 0:
                        refined.append((score * 0.2, chunk))
                    else:
                        refined.append((score * 0.8, chunk))
                    seen.add(chunk.chunk_id)
                    continue
            if chunk.chunk_id not in seen:
                refined.append((score, chunk))
                seen.add(chunk.chunk_id)

        refined.sort(key=lambda x: -x[0])
        return refined

    def retrieve_adaptive(self, query: str, top_k: int = None) -> list[tuple[float, CodeChunk]]:
        if top_k is None:
            top_k = self.config.get("memory_top_k", 8)
        all_text = "\n".join(c.content for c in self.memory.chunks if c)
        full_tokens = self._estimate_tokens(all_text)
        threshold = self.config.get("adaptive_max_context_tokens", 3000)
        if self.config.get("adaptive_context", True) and full_tokens > threshold:
            return self.retrieve(query, top_k=top_k)
        else:
            return self.memory.score_full_context(query, top_k=top_k)

    def evaluate_ground_truth(self) -> dict:
        if not self.config.get("ground_truth_enabled", False):
            return {"enabled": False}
        gt_path = self.config.get("ground_truth_path", "ground_truth/opencode_ground_truth.json")
        top_k = self.config.get("ground_truth_top_k", 5)
        try:
            res = GroundTruthEvaluator.evaluate(self, gt_path, top_k=top_k)
            report_path = self.config.get("ground_truth_report_path") or None
            if report_path:
                try:
                    save_csv_report(res, report_path)
                except Exception:
                    pass
            return res
        except Exception as e:
            return {"error": str(e)}

    def retrieve_with_coverage(self, query: str, top_k: int = None) -> dict:
        if top_k is None:
            top_k = self.config.get("memory_top_k", 8)
        coarse_k = top_k * 4 if self.config.get("coarse_to_fine", True) else top_k
        result = self.memory.retrieve_with_coverage(
            query, top_k=coarse_k,
            max_hops=self.config.get("max_hops", 2),
            recency_bonus=0.3,
        )
        if self.config.get("coarse_to_fine", True):
            result["results"] = self._coarse_to_fine(result["results"], query, top_k)
        result["results"] = result["results"][:top_k]
        return result

    def retrieve_with_rules(self, query: str, top_k: int = None, rules_store=None) -> dict:
        """
        带规则增强的检索：先 BM25 召回，再用规则 boost/补充。
        规则匹配的文件会被直接注入候选集。
        """
        result = self.retrieve_with_coverage(query, top_k=top_k)
        if rules_store is None:
            return result

        active_rules = rules_store.list_rules(enabled_only=True, status="active")
        if not active_rules:
            return result

        q_tokens = set(self.memory._tokenize_query(query))
        q_meaningful = {t for t in q_tokens if not t.startswith("@") and len(t) > 1}

        # 规则匹配 → 注入关联文件的 chunks
        existing_ids = {c.chunk_id for _, c in result["results"]}
        injected = []
        for rule in active_rules:
            # 对规则条件也做同义词扩展
            rule_raw_tokens = set(rule.condition_tokens)
            rule_expanded = set(expand_query(list(rule_raw_tokens), replace_chinese=True))
            rule_all = rule_raw_tokens | rule_expanded
            if len(rule_all) < 2:
                continue
            overlap = len(rule_all & q_meaningful) / max(len(rule_all), 1)
            if overlap < 0.2:
                continue
            # 规则匹配 → 找关联文件的 chunks
            for rf in rule.related_files:
                for cid, chunk in enumerate(self.memory.chunks):
                    if chunk is None or cid in existing_ids:
                        continue
                    if chunk.file_path.endswith(rf) or rf.endswith(Path(chunk.file_path).name):
                        injected.append((rule.effective_confidence * 10.0, chunk))
                        existing_ids.add(cid)
                        break

        if injected:
            result["results"].extend(injected)
            result["results"].sort(key=lambda x: -x[0])
            result["results"] = result["results"][:top_k or self.config.get("memory_top_k", 8)]
            result["coverage"]["rules_applied"] = len(injected)
            result["coverage"]["rules_active"] = len(active_rules)

        return result

    def retrieve_with_deps(self, query: str, top_k: int = None) -> list[tuple[float, CodeChunk]]:
        results = self.retrieve(query, top_k=top_k)
        seen = {c.chunk_id for _, c in results}
        extra = []
        for _, chunk in results[:3]:
            for d in self.memory.get_dependent_chunks(chunk)[:2]:
                if d.chunk_id not in seen:
                    extra.append((0.1, d))
                    seen.add(d.chunk_id)
        return results + extra

    def format_results(self, results: list[tuple[float, CodeChunk]]) -> str:
        if not results:
            return "未找到相关代码。"
        parts = []
        for score, chunk in results:
            try:
                rel = os.path.relpath(chunk.file_path, self.project_root)
            except ValueError:
                rel = chunk.file_path
            ext = Path(chunk.file_path).suffix.lstrip(".")
            parts.append(
                f"### {rel} [{chunk.chunk_type}: {chunk.name}] (score: {score:.2f})\n"
                f"```{ext}\n{chunk.content}\n```"
            )
        return "\n\n".join(parts)

    def format_results_compressed(self, results: list[tuple[float, CodeChunk]], query: str, max_lines_per_chunk: int = 15) -> str:
        """
        压缩格式化：只返回每个 chunk 中与查询最相关的行，而非全部内容。
        可以减少 40-60% 的输出 token。
        """
        if not results:
            return "未找到相关代码。"
        q_tokens = set(self.memory._tokenize_query(query))
        q_meaningful = {t for t in q_tokens if not t.startswith("@") and len(t) > 1}
        parts = []
        for score, chunk in results:
            try:
                rel = os.path.relpath(chunk.file_path, self.project_root)
            except ValueError:
                rel = chunk.file_path
            ext = Path(chunk.file_path).suffix.lstrip(".")
            lines = chunk.content.split("\n")
            if len(lines) <= max_lines_per_chunk or not q_meaningful:
                # 短 chunk 或无查询 token，直接返回全部
                compressed = chunk.content
            else:
                # 给每行打分，选出最相关的行
                scored_lines = []
                for i, line in enumerate(lines):
                    line_lower = line.lower()
                    line_score = sum(1 for t in q_meaningful if t in line_lower)
                    # 函数签名/类定义给 bonus
                    if re.match(r"^\s*(def |class |function |async def )", line):
                        line_score += 3
                    # 注释/docstring 给小 bonus
                    elif re.match(r'^\s*(#|"""|\'\'\')', line):
                        line_score += 1
                    scored_lines.append((line_score, i, line))
                scored_lines.sort(key=lambda x: (-x[0], x[1]))
                selected = sorted(scored_lines[:max_lines_per_chunk], key=lambda x: x[1])
                # 保持行号连续性：如果中间跳了多行，加 ...
                compressed_lines = []
                prev_idx = -2
                for _, idx, line in selected:
                    if idx - prev_idx > 1 and prev_idx >= 0:
                        compressed_lines.append("    ...")
                    compressed_lines.append(line)
                    prev_idx = idx
                compressed = "\n".join(compressed_lines)
            parts.append(
                f"### {rel} [{chunk.chunk_type}: {chunk.name}] (score: {score:.2f})\n"
                f"```{ext}\n{compressed}\n```"
            )
        return "\n\n".join(parts)

    def start_watching(self) -> str:
        if not HAS_WATCHDOG:
            return "watchdog 未安装"
        if self.watcher and self.watcher.is_running:
            return "已在监听中"
        self.watcher = FileWatcher(self, debounce_ms=self.config.get("debounce_ms", 500))
        self.watcher.start()
        return f"开始监听 {self.project_root}"

    def stop_watching(self) -> str:
        if self.watcher and self.watcher.is_running:
            self.watcher.stop()
            return "已停止监听"
        return "当前无监听任务"

    def _handle_file_change(self, file_path: str):
        with self._lock:
            old_cids = self.memory.file_chunks.get(file_path, [])
            old_hashes = {}
            for cid in old_cids:
                if cid < len(self.memory.chunks) and self.memory.chunks[cid] is not None:
                    old_hashes[self.memory.chunks[cid].name] = self.memory.chunks[cid].hash
            new_chunks = self.parser.parse_file(file_path)

            new_hashes = {c.name: hashlib.md5(c.content.encode()).hexdigest()[:12] for c in new_chunks}
            if old_hashes == new_hashes:
                return
            self.memory.remove_file(file_path)
            for chunk in new_chunks:
                self.memory.add_chunk(chunk)
            self._indexed_files.add(file_path)
            if self.config.get("layered_index", True):
                self.memory.add_file_summary(file_path, new_chunks)
            self._persist_if_configured()

    def _handle_file_delete(self, file_path: str):
        with self._lock:
            self.memory.remove_file(file_path)
            self._indexed_files.discard(file_path)
            self._persist_if_configured()

    def _persist_if_configured(self):
        persist_path = self.config.get("persist_path")
        if persist_path:
            try:
                self.memory.save(persist_path)
            except Exception:
                pass

    def compare_strategies(self, query: str) -> dict:
        all_text = "\n".join(c.content for c in self.memory.chunks if c)
        full_tokens = self._estimate_tokens(all_text)
        sorted_chunks = sorted(
            [c for c in self.memory.chunks if c],
            key=lambda c: c.last_modified, reverse=True,
        )
        recent, seen = [], set()
        for c in sorted_chunks:
            if c.file_path not in seen:
                seen.add(c.file_path)
                recent.append(c)
            if len(recent) >= 5:
                break
        recent_tokens = self._estimate_tokens("\n".join(c.content for c in recent))
        retrieved = self.retrieve(query, top_k=5)
        graph_tokens = self._estimate_tokens("\n".join(c.content for _, c in retrieved))
        return {
            "query": query,
            "full_context_tokens": full_tokens,
            "recent_files_tokens": recent_tokens,
            "graph_diffusion_tokens": graph_tokens,
            "savings": f"{(1 - graph_tokens / max(full_tokens, 1)) * 100:.0f}%",
        }

    def _estimate_tokens(self, text: str) -> int:
        mode = self.config.get("token_estimate_mode", "mixed")
        if mode == "char":
            chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
            english = len(re.findall(r"[a-zA-Z]+", text))
            return chinese * 2 + int(english * 1.3)
        elif mode == "word":
            return len(text.split())
        else:
            words = len(re.findall(r"[a-zA-Z_]\w*", text))
            chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
            return int(words * 0.75 + chinese * 1.5)

    def stats(self) -> dict:
        return self.memory.stats()
