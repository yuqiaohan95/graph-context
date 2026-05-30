"""
Project Scope Manager — 项目隔离与打通
========================================
管理多个项目的作用域，支持 strict 隔离和 shared 打通模式。

功能:
  1. ProjectScope 数据结构
  2. ScopeManager — 多项目作用域管理
  3. 隔离模式(strict) — 完全独立索引
  4. 打通模式(shared) — 可引用其他项目的模式
  5. 跨项目搜索 — 只返回模式，不返回代码
"""

import os
import re
import json
import time
import threading
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

# 轻量 tokenize (复用 rules 的逻辑)
_STOPWORDS = {
    "def", "class", "import", "from", "return", "if", "else", "for", "while",
    "try", "except", "with", "as", "in", "not", "and", "or", "is", "the",
    "a", "an", "to", "of", "it", "its", "be", "been", "being", "have", "has",
}


def _tokenize(text: str) -> set[str]:
    tokens = set()
    for ident in re.findall(r"[a-zA-Z_]\w*", text):
        low = ident.lower()
        if len(low) > 1 and low not in _STOPWORDS:
            tokens.add(low)
        for seg in low.split("_"):
            if len(seg) > 1 and seg not in _STOPWORDS:
                tokens.add(seg)
    for cn in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.add(cn)
    return tokens


# ══════════════════════════════════════════════════
#  数据结构
# ══════════════════════════════════════════════════

@dataclass
class ProjectScope:
    project_id: str
    root: str
    isolation: str = "strict"        # "strict" | "shared"
    shared_rules: list = field(default_factory=list)
    shared_patterns: list = field(default_factory=list)
    cross_references: dict = field(default_factory=dict)  # 项目间引用
    created_at: float = 0.0
    description: str = ""
    tags: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectScope":
        defaults = {
            "created_at": 0.0,
            "description": "",
            "tags": [],
        }
        for k, v in defaults.items():
            if k not in data:
                data[k] = v
        return cls(**data)


@dataclass
class PatternSummary:
    """跨项目搜索返回的模式摘要（不含代码）"""
    pattern_name: str
    pattern_type: str     # "function_pattern" | "class_pattern" | "import_pattern" | "architecture"
    source_project: str
    description: str
    frequency: int        # 在源项目中出现的次数
    confidence: float
    example_files: list = field(default_factory=list)  # 文件路径，不含代码

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AggregatedPattern:
    """聚合后的模式：跨项目搜索结果按模式名聚类"""
    pattern_name: str
    pattern_type: str
    description: str
    total_frequency: int           # 总出现次数
    avg_confidence: float          # 平均置信度
    projects: list = field(default_factory=list)     # 出现的项目列表
    project_details: dict = field(default_factory=dict)  # project_id -> {frequency, confidence, files}
    best_project: str = ""         # 最强匹配的项目

    def to_dict(self) -> dict:
        return {
            "pattern_name": self.pattern_name,
            "pattern_type": self.pattern_type,
            "description": self.description,
            "total_frequency": self.total_frequency,
            "avg_confidence": round(self.avg_confidence, 4),
            "projects": self.projects,
            "project_details": self.project_details,
            "best_project": self.best_project,
        }


# ══════════════════════════════════════════════════
#  Scope Manager
# ══════════════════════════════════════════════════

class ScopeManager:
    """管理多个项目的作用域"""

    def __init__(self, persist_path: Optional[str] = None):
        self._scopes: dict[str, ProjectScope] = {}
        self._engines: dict[str, object] = {}  # project_id -> VibeCodingEngine
        self._persist_path = persist_path
        self._lock = threading.Lock()
        if persist_path:
            self._load()

    # ── CRUD ──────────────────────────────────────

    def create_scope(
        self,
        project_id: str,
        root: str,
        isolation: str = "strict",
        description: str = "",
        tags: list = None,
    ) -> ProjectScope:
        """创建项目作用域"""
        with self._lock:
            if project_id in self._scopes:
                raise ValueError(f"Project '{project_id}' already exists")
            scope = ProjectScope(
                project_id=project_id,
                root=str(Path(root).resolve()),
                isolation=isolation,
                created_at=time.time(),
                description=description,
                tags=tags or [],
            )
            self._scopes[project_id] = scope
            self._save()
            return scope

    def get_scope(self, project_id: str) -> Optional[ProjectScope]:
        return self._scopes.get(project_id)

    def update_scope(self, project_id: str, **kwargs) -> bool:
        with self._lock:
            scope = self._scopes.get(project_id)
            if scope is None:
                return False
            for k, v in kwargs.items():
                if hasattr(scope, k):
                    setattr(scope, k, v)
            self._save()
            return True

    def delete_scope(self, project_id: str) -> bool:
        with self._lock:
            if project_id in self._scopes:
                del self._scopes[project_id]
                self._engines.pop(project_id, None)
                # 清理其他项目的交叉引用
                for other in self._scopes.values():
                    other.cross_references.pop(project_id, None)
                self._save()
                return True
            return False

    def list_scopes(self) -> list[ProjectScope]:
        return list(self._scopes.values())

    # ── 引擎绑定 ──────────────────────────────────

    def bind_engine(self, project_id: str, engine):
        """将引擎绑定到项目作用域"""
        self._engines[project_id] = engine

    def get_engine(self, project_id: str):
        return self._engines.get(project_id)

    # ── 项目关联 ──────────────────────────────────

    def link_projects(
        self,
        project_a: str,
        project_b: str,
        link_type: str = "reference",
        shared_rules: list = None,
        shared_patterns: list = None,
    ) -> bool:
        """
        关联两个项目。
        link_type: "reference" (单向引用) | "bidirectional" (双向引用) | "shared" (共享模式)
        """
        with self._lock:
            scope_a = self._scopes.get(project_a)
            scope_b = self._scopes.get(project_b)
            if not scope_a or not scope_b:
                return False

            # 更新交叉引用
            scope_a.cross_references[project_b] = {
                "link_type": link_type,
                "linked_at": time.time(),
            }
            if link_type == "bidirectional":
                scope_b.cross_references[project_a] = {
                    "link_type": link_type,
                    "linked_at": time.time(),
                }

            # 共享规则
            if shared_rules:
                for rule_id in shared_rules:
                    if rule_id not in scope_a.shared_rules:
                        scope_a.shared_rules.append(rule_id)
                    if link_type in ("bidirectional", "shared"):
                        if rule_id not in scope_b.shared_rules:
                            scope_b.shared_rules.append(rule_id)

            # 共享模式
            if shared_patterns:
                for pattern in shared_patterns:
                    if pattern not in scope_a.shared_patterns:
                        scope_a.shared_patterns.append(pattern)
                    if link_type in ("bidirectional", "shared"):
                        if pattern not in scope_b.shared_patterns:
                            scope_b.shared_patterns.append(pattern)

            # 如果是 shared 模式，自动切换隔离级别
            if link_type == "shared":
                if scope_a.isolation == "strict":
                    scope_a.isolation = "shared"
                if scope_b.isolation == "strict":
                    scope_b.isolation = "shared"

            self._save()
            return True

    def unlink_projects(self, project_a: str, project_b: str) -> bool:
        with self._lock:
            scope_a = self._scopes.get(project_a)
            scope_b = self._scopes.get(project_b)
            if not scope_a or not scope_b:
                return False
            scope_a.cross_references.pop(project_b, None)
            scope_b.cross_references.pop(project_a, None)
            self._save()
            return True

    # ── 跨项目搜索 ────────────────────────────────

    def search_across_projects(
        self,
        query: str,
        source_project: Optional[str] = None,
        top_k: int = 5,
        aggregate: bool = True,
    ) -> list:
        """
        跨项目搜索。
        aggregate=True 时返回聚合后的模式（按模式名聚类），否则返回扁平列表。
        只返回模式摘要，不返回代码内容。
        
        改进：聚合时检查实现相似度，差异大时不合并。
        """
        raw_results = self._search_raw(query, source_project, top_k)

        if not aggregate:
            return raw_results

        # 聚合：按 pattern_name + pattern_type 聚类
        groups: dict[str, list[PatternSummary]] = {}
        for p in raw_results:
            key = f"{p.pattern_type}:{p.pattern_name}"
            if key not in groups:
                groups[key] = []
            groups[key].append(p)

        aggregated = []
        for key, patterns in groups.items():
            if len(patterns) == 1:
                # 单项目，直接用
                p = patterns[0]
                agg = AggregatedPattern(
                    pattern_name=p.pattern_name,
                    pattern_type=p.pattern_type,
                    description=p.description,
                    total_frequency=p.frequency,
                    avg_confidence=p.confidence,
                    projects=[p.source_project],
                    project_details={
                        p.source_project: {
                            "frequency": p.frequency,
                            "confidence": p.confidence,
                            "files": p.example_files,
                        }
                    },
                    best_project=p.source_project,
                )
                aggregated.append(agg)
            else:
                # 多项目：检查实现相似度
                descs = [p.description for p in patterns]
                desc_sim = self._description_similarity(descs)

                # 还要检查实际内容
                content_similar = True
                if len(patterns) >= 2 and desc_sim >= 0.3:
                    # 描述相似，再检查内容
                    pids = list(set(p.source_project for p in patterns))
                    if len(pids) >= 2:
                        eng_a = self._engines.get(pids[0])
                        eng_b = self._engines.get(pids[1])
                        content_similar = self._check_content_similarity(
                            eng_a, eng_b, patterns[0].pattern_name
                        )

                if desc_sim < 0.3 or not content_similar:
                    # 描述差异大 → 不合并，各自保留
                    for p in patterns:
                        agg = AggregatedPattern(
                            pattern_name=f"{p.pattern_name} [{p.source_project}]",
                            pattern_type=p.pattern_type,
                            description=p.description,
                            total_frequency=p.frequency,
                            avg_confidence=p.confidence,
                            projects=[p.source_project],
                            project_details={
                                p.source_project: {
                                    "frequency": p.frequency,
                                    "confidence": p.confidence,
                                    "files": p.example_files,
                                }
                            },
                            best_project=p.source_project,
                        )
                        aggregated.append(agg)
                else:
                    # 描述相似 → 正常聚合
                    total_freq = sum(p.frequency for p in patterns)
                    avg_conf = sum(p.confidence for p in patterns) / len(patterns)
                    projects = list(set(p.source_project for p in patterns))
                    best = max(patterns, key=lambda p: p.confidence)

                    project_details = {}
                    for p in patterns:
                        if p.source_project not in project_details:
                            project_details[p.source_project] = {
                                "frequency": 0, "confidence": 0.0, "files": [],
                            }
                        detail = project_details[p.source_project]
                        detail["frequency"] += p.frequency
                        detail["confidence"] = max(detail["confidence"], p.confidence)
                        detail["files"].extend(p.example_files)

                    agg = AggregatedPattern(
                        pattern_name=best.pattern_name,
                        pattern_type=best.pattern_type,
                        description=best.description,
                        total_frequency=total_freq,
                        avg_confidence=avg_conf,
                        projects=projects,
                        project_details=project_details,
                        best_project=best.source_project,
                    )
                    aggregated.append(agg)

        aggregated.sort(key=lambda a: -a.avg_confidence)
        return aggregated[:top_k * 2]

    def _description_similarity(self, descs: list[str]) -> float:
        """
        计算多个描述之间的相似度（基于 token Jaccard）。
        返回 0.0-1.0。
        """
        if len(descs) < 2:
            return 1.0
        token_sets = [_tokenize(d) for d in descs]
        total_sim = 0
        pairs = 0
        for i in range(len(token_sets)):
            for j in range(i + 1, len(token_sets)):
                a, b = token_sets[i], token_sets[j]
                if not a and not b:
                    total_sim += 1.0
                elif a and b:
                    total_sim += len(a & b) / len(a | b)
                pairs += 1
        return total_sim / max(pairs, 1)

    def _check_content_similarity(self, engine_a, engine_b, pattern_name: str) -> bool:
        """
        检查两个项目中同名模式的实际内容是否相似。
        返回 True 表示实现相似（可以合并），False 表示不同（不应合并）。
        """
        if not engine_a or not engine_b:
            return True  # 无法比较，默认合并

        # 搜索同名 chunk
        results_a = engine_a.retrieve(pattern_name, top_k=1)
        results_b = engine_b.retrieve(pattern_name, top_k=1)

        if not results_a or not results_b:
            return True

        content_a = results_a[0][1].content
        content_b = results_b[0][1].content

        # 内容完全相同
        if content_a == content_b:
            return True

        # 基于 token 的内容相似度
        tokens_a = _tokenize(content_a)
        tokens_b = _tokenize(content_b)
        if not tokens_a or not tokens_b:
            return True

        jaccard = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
        return jaccard > 0.5  # 50% 以上 token 重叠才合并

    def _search_raw(
        self,
        query: str,
        source_project: Optional[str] = None,
        top_k: int = 5,
    ) -> list[PatternSummary]:
        """原始搜索，返回扁平的 PatternSummary 列表"""
        results = []
        search_scope = self._get_search_scope(source_project)

        for pid in search_scope:
            engine = self._engines.get(pid)
            if engine is None:
                continue
            scope = self._scopes.get(pid)
            if scope is None:
                continue

            try:
                search_results = engine.retrieve(query, top_k=top_k)
            except Exception:
                continue

            for score, chunk in search_results:
                pattern = PatternSummary(
                    pattern_name=chunk.name,
                    pattern_type=self._classify_pattern(chunk),
                    source_project=pid,
                    description=self._describe_pattern(chunk),
                    frequency=1,
                    confidence=min(1.0, score / 10.0),
                    example_files=[chunk.file_path],
                )
                results.append(pattern)

        # 去重并按置信度排序
        seen = set()
        unique = []
        for p in results:
            key = f"{p.source_project}:{p.pattern_name}"
            if key not in seen:
                seen.add(key)
                unique.append(p)
        unique.sort(key=lambda p: -p.confidence)
        return unique[:top_k * 2]

    def _get_search_scope(self, source_project: Optional[str]) -> list[str]:
        """确定搜索范围：包括源项目及其关联项目"""
        if source_project is None:
            return list(self._scopes.keys())

        scope = self._scopes.get(source_project)
        if scope is None:
            return []

        pids = [source_project]
        # 添加关联项目（只在 shared 模式下）
        if scope.isolation == "shared":
            for ref_pid in scope.cross_references:
                ref_scope = self._scopes.get(ref_pid)
                if ref_scope and ref_scope.isolation == "shared":
                    pids.append(ref_pid)
        return pids

    def _classify_pattern(self, chunk) -> str:
        """根据 chunk 类型分类模式"""
        type_map = {
            "function": "function_pattern",
            "method": "function_pattern",
            "class": "class_pattern",
            "imports": "import_pattern",
            "file_summary": "architecture",
            "file": "architecture",
        }
        return type_map.get(getattr(chunk, "chunk_type", ""), "function_pattern")

    def _describe_pattern(self, chunk) -> str:
        """生成模式描述（不含代码）"""
        ct = getattr(chunk, "chunk_type", "unknown")
        name = getattr(chunk, "name", "unknown")
        fp = getattr(chunk, "file_path", "")
        deps = getattr(chunk, "dependencies", [])
        calls = getattr(chunk, "calls", [])

        desc = f"{ct} '{name}'"
        if fp:
            desc += f" in {Path(fp).name}"
        if deps:
            desc += f", depends on: {', '.join(deps[:5])}"
        if calls:
            desc += f", calls: {', '.join(calls[:5])}"
        return desc

    # ── 持久化 ────────────────────────────────────

    def _save(self):
        if not self._persist_path:
            return
        data = {
            "version": 1,
            "scopes": {pid: s.to_dict() for pid, s in self._scopes.items()},
            "saved_at": time.time(),
        }
        os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
        tmp = self._persist_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._persist_path)

    def _load(self):
        if not self._persist_path or not os.path.exists(self._persist_path):
            return
        try:
            with open(self._persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for pid, sdata in data.get("scopes", {}).items():
                self._scopes[pid] = ProjectScope.from_dict(sdata)
        except Exception:
            pass

    def stats(self) -> dict:
        strict = sum(1 for s in self._scopes.values() if s.isolation == "strict")
        shared = sum(1 for s in self._scopes.values() if s.isolation == "shared")
        total_links = sum(len(s.cross_references) for s in self._scopes.values())
        return {
            "total_projects": len(self._scopes),
            "strict_projects": strict,
            "shared_projects": shared,
            "total_links": total_links // 2,  # 双向链接算一个
            "engines_bound": len(self._engines),
        }
