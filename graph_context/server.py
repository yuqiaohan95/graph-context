"""
MCP Server -- v6 动态加载版 (优化后)
  - 启动时只注入 2 个工具: search + tools (~600 tokens)
  - 其他工具按需加载: tools(action="load", module="rules")
  - 加载后客户端收到 tools/list_changed 通知
  - 支持 unload 卸载不需要的工具

优化:
  - MODULE_REGISTRY 只构建一次
  - _loaded_modules 改为 per-session 隔离 (通过 _session_loaded 映射)
  - load/unload 返回 fallback 提示, 兼容不支持 list_changed 的客户端
"""

import os
import json
import time
import threading
from pathlib import Path
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP, Context
except ImportError:
    from mcp.server.fast_server import FastMCP, Context

from .engine import VibeCodingEngine, DEFAULT_CONFIG
from .rules import Rule, RulesStore
from .project_scope import ScopeManager
from . import synonyms as _synonyms_mod

# ══════════════════════════════════════════════════
#  全局状态
# ══════════════════════════════════════════════════

_engine: VibeCodingEngine | None = None
_engine_lock = threading.Lock()
_initialized = False
_rules_store: RulesStore | None = None
_scope_manager: ScopeManager | None = None
_retrieval_counter = 0
_gt_log_path: str | None = None
_gt_log_lock = threading.Lock()
_custom_synonyms_path: str | None = None
_custom_synonyms: dict[str, list[str]] = {}
_unmatched_tokens_log: list[dict] = []
_synonyms_lock = threading.Lock()

# 动态加载状态 — per-session 隔离
# key: session_id (str), value: set of loaded module names
_session_loaded: dict[str, set[str]] = {}
_session_loaded_lock = threading.Lock()

mcp = FastMCP("mcporter")


def _get_session_id(ctx: Context | None) -> str:
    """从 Context 提取 session id, 无 context 时 fallback 到 'default'"""
    if ctx and hasattr(ctx, "session") and ctx.session:
        return str(id(ctx.session))
    return "default"


def _get_loaded_modules(ctx: Context | None) -> set[str]:
    """获取当前 session 的已加载模块集合"""
    sid = _get_session_id(ctx)
    with _session_loaded_lock:
        if sid not in _session_loaded:
            _session_loaded[sid] = set()
        return _session_loaded[sid]


# ══════════════════════════════════════════════════
#  辅助函数
# ══════════════════════════════════════════════════

def _get_engine() -> VibeCodingEngine:
    global _engine, _initialized
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        root = os.environ.get("PROJECT_ROOT", os.getcwd())
        config = dict(DEFAULT_CONFIG)
        for env_key, cfg_key, typ in [
            ("MCP_MAX_TOKENS", "max_context_tokens", int),
            ("MCP_TOP_K", "memory_top_k", int),
            ("MCP_MAX_HOPS", "max_hops", int),
            ("MCP_CACHE_SIZE", "cache_max_size", int),
        ]:
            if os.environ.get(env_key):
                config[cfg_key] = typ(os.environ[env_key])
        if os.environ.get("MCP_PERSIST_PATH"):
            config["persist_path"] = os.environ["MCP_PERSIST_PATH"]
        engine = VibeCodingEngine(root, config)
        engine.index_project()
        engine.start_watching()
        _ensure_custom_synonyms()
        _engine = engine
        _initialized = True
        return engine


def _get_rules_store() -> RulesStore:
    global _rules_store
    if _rules_store is None:
        persist = os.environ.get("MCP_RULES_PATH", "rules/rules.json")
        _rules_store = RulesStore(persist_path=persist)
    return _rules_store


def _get_scope_manager() -> ScopeManager:
    global _scope_manager
    if _scope_manager is None:
        persist = os.environ.get("MCP_SCOPES_PATH", "scopes/scopes.json")
        _scope_manager = ScopeManager(persist_path=persist)
        _ensure_all_scopes_have_engines(_scope_manager)
    return _scope_manager


def _ensure_all_scopes_have_engines(manager: ScopeManager):
    main_engine = _get_engine()
    main_root = str(main_engine.project_root)
    default_id = Path(main_root).name
    if manager.get_scope(default_id) is None:
        manager.create_scope(project_id=default_id, root=main_root, isolation="shared", description="Default")
    for scope in manager.list_scopes():
        if manager.get_engine(scope.project_id) is not None:
            continue
        if os.path.abspath(scope.root) == os.path.abspath(main_root):
            manager.bind_engine(scope.project_id, main_engine)
    config = dict(DEFAULT_CONFIG)
    for ek, ck, t in [("MCP_MAX_TOKENS","max_context_tokens",int),("MCP_TOP_K","memory_top_k",int),("MCP_MAX_HOPS","max_hops",int),("MCP_CACHE_SIZE","cache_max_size",int)]:
        if os.environ.get(ek): config[ck] = t(os.environ[ek])
    for scope in manager.list_scopes():
        if manager.get_engine(scope.project_id) is not None:
            continue
        if not os.path.isdir(scope.root):
            continue
        try:
            eng = VibeCodingEngine(scope.root, config)
            eng.index_project()
            eng.start_watching()
            manager.bind_engine(scope.project_id, eng)
        except Exception:
            pass


_IMPLICIT_GT_MAX = 1000


def _ensure_custom_synonyms():
    global _custom_synonyms_path, _custom_synonyms
    if _custom_synonyms_path is not None:
        return
    root = os.environ.get("PROJECT_ROOT", os.getcwd())
    _custom_synonyms_path = os.environ.get(
        "MCP_CUSTOM_SYNONYMS_PATH",
        os.path.join(root, "ground_truth", "custom_synonyms.json"),
    )
    if os.path.exists(_custom_synonyms_path):
        try:
            with open(_custom_synonyms_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _custom_synonyms = data
                for cn, ens in data.items():
                    if cn not in _synonyms_mod.SYNONYM_MAP:
                        _synonyms_mod.SYNONYM_MAP[cn] = []
                    for en in ens:
                        if en not in _synonyms_mod.SYNONYM_MAP[cn]:
                            _synonyms_mod.SYNONYM_MAP[cn].append(en)
                            _synonyms_mod._REVERSE_MAP.setdefault(en, []).append(cn)
        except Exception:
            pass


def _save_custom_synonyms():
    path = _custom_synonyms_path
    if path is None:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_custom_synonyms, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _track_unmatched_tokens(query: str):
    import re
    for cn_block in re.findall(r"[\u4e00-\u9fff]+", query):
        remaining = cn_block
        for key in sorted(_synonyms_mod.SYNONYM_MAP.keys(), key=len, reverse=True):
            remaining = remaining.replace(key, "")
        remaining = remaining.strip()
        if remaining:
            with _synonyms_lock:
                _unmatched_tokens_log.append({"token": remaining, "query": query, "timestamp": time.time()})
                if len(_unmatched_tokens_log) > 200:
                    _unmatched_tokens_log.pop(0)


def _log_implicit_gt(query: str, results: list):
    global _gt_log_path
    if _gt_log_path is None:
        root = os.environ.get("PROJECT_ROOT", os.getcwd())
        gt_dir = os.environ.get("MCP_GT_DIR", os.path.join(root, "ground_truth"))
        os.makedirs(gt_dir, exist_ok=True)
        _gt_log_path = os.path.join(gt_dir, "implicit_gt.jsonl")
    try:
        entry = {
            "query": query,
            "results": [
                {"score": round(float(s), 4), "file_path": getattr(c, "file_path", ""), "name": getattr(c, "name", ""), "chunk_type": getattr(c, "chunk_type", "")}
                for s, c in results[:5]
            ],
            "timestamp": time.time(),
        }
        with _gt_log_lock:
            with open(_gt_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if os.path.getsize(_gt_log_path) > _IMPLICIT_GT_MAX * 500:
                os.rename(_gt_log_path, _gt_log_path + ".old")
    except Exception:
        pass


def _auto_evolve():
    global _retrieval_counter
    _retrieval_counter += 1
    if _retrieval_counter % 50 != 0:
        return
    try:
        store = _get_rules_store()
        engine = _get_engine()
        store.promote_observing_rules()
        store.demote_failing_rules()
        store.apply_decay()
        store.prune_rules()
        store.check_code_freshness(engine)
    except Exception:
        pass


def _post_retrieve(query: str, results: list) -> list:
    results = _get_rules_store().apply_rules_to_query(query, results)
    _track_unmatched_tokens(query)
    _log_implicit_gt(query, results)
    _auto_evolve()
    return results


# ══════════════════════════════════════════════════
#  动态工具模块定义 (只构建一次)
# ══════════════════════════════════════════════════

MODULE_REGISTRY: dict[str, dict[str, Any]] = {}
_registry_built = False
_registry_lock = threading.Lock()


def _ensure_registry():
    """确保 MODULE_REGISTRY 只构建一次"""
    global _registry_built
    if _registry_built:
        return
    with _registry_lock:
        if _registry_built:
            return
        _build_module_registry()
        _registry_built = True


def _build_module_registry():
    """构建可按需加载的工具模块注册表"""

    # ── rules 模块 ──
    def _rules_list(rule_type: str = "", scope: str = "", min_confidence: float = 0.0, status: str = "") -> str:
        store = _get_rules_store()
        rules_list = store.list_rules(rule_type=rule_type or None, scope=scope or None, min_confidence=min_confidence, status=status or None)
        out = [{"rule_id": r.rule_id, "type": r.rule_type, "desc": r.description, "condition": r.condition, "action": r.action, "confidence": round(r.effective_confidence, 4), "hits": r.hit_count, "accuracy": round(r.accuracy_rate, 4), "status": r.status} for r in rules_list]
        return json.dumps({"count": len(out), "rules": out}, indent=2, ensure_ascii=False)

    def _rules_add(rule_type: str, condition: str, description: str = "", rule_action: str = "", scope: str = "project", confidence: float = 0.8, source: str = "manual", tags: str = "", priority: int = 0, related_files: str = "") -> str:
        store = _get_rules_store()
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        file_list = [f.strip() for f in related_files.split(",") if f.strip()] if related_files else []
        rule = Rule(rule_id="", rule_type=rule_type, scope=scope, description=description, condition=condition, action=rule_action, confidence=confidence, source=source, tags=tag_list, priority=priority, related_files=file_list)
        conflicts = store.detect_conflicts(rule)
        new_id = store.add_rule(rule)
        result = {"rule_id": new_id, "status": rule.status, "confidence": round(rule.effective_confidence, 4)}
        if conflicts:
            result["conflicts"] = conflicts
        return json.dumps(result, indent=2, ensure_ascii=False)

    def _rules_apply(rule_id: str) -> str:
        store = _get_rules_store()
        rule = store.get_rule(rule_id)
        if not rule:
            return json.dumps({"error": f"Rule '{rule_id}' not found"})
        store.record_hit(rule_id)
        return json.dumps({"rule_id": rule_id, "hits": rule.hit_count + 1}, indent=2)

    def _rules_verify(rule_id: str, correct: bool = True) -> str:
        store = _get_rules_store()
        rule = store.get_rule(rule_id)
        if not rule:
            return json.dumps({"error": f"Rule '{rule_id}' not found"})
        (store.record_verified if correct else store.record_rejected)(rule_id)
        rule = store.get_rule(rule_id)
        return json.dumps({"rule_id": rule_id, "correct": correct, "accuracy": round(rule.accuracy_rate, 4), "confidence": round(rule.effective_confidence, 4)}, indent=2)

    def _rules_prune(min_hit_rate: float = 0.1, min_uses: int = 5) -> str:
        store = _get_rules_store()
        store.apply_decay()
        pruned = store.prune_rules(min_hit_rate=min_hit_rate, min_uses=min_uses)
        promoted = store.promote_observing_rules()
        demoted = store.demote_failing_rules()
        return json.dumps({"pruned": pruned, "promoted": promoted, "demoted": demoted, "remaining": len(store.list_rules(enabled_only=True, status="active"))}, indent=2)

    def _rules_discover(top_k: int = 5) -> str:
        engine = _get_engine()
        store = _get_rules_store()
        gt_path = engine.config.get("ground_truth_path", "ground_truth/opencode_ground_truth.json")
        discovered = store.discover_rules_from_errors(engine, gt_path, top_k=top_k)
        return json.dumps({"discovered": len(discovered), "rule_ids": discovered}, indent=2)

    MODULE_REGISTRY["rules"] = {
        "description": "Rule management for LLM self-evolution. Accuracy-based decay.",
        "tools": [
            (_rules_list, "rules_list", "List retrieval rules. Optional filters: rule_type, scope, min_confidence, status."),
            (_rules_add, "rules_add", "Add a new rule. Required: rule_type, condition. The rule enters observing period."),
            (_rules_apply, "rules_apply", "Apply a rule (records a hit). Required: rule_id."),
            (_rules_verify, "rules_verify", "Verify a rule as correct or incorrect. Required: rule_id."),
            (_rules_prune, "rules_prune", "Prune low-performing rules. Params: min_hit_rate, min_uses."),
            (_rules_discover, "rules_discover", "Auto-discover candidate rules from retrieval errors."),
        ],
    }

    # ── admin 模块 ──
    def _admin_health() -> str:
        engine = _get_engine()
        stats = engine.stats()
        watcher = "running" if (engine.watcher and engine.watcher.is_running) else "stopped"
        return json.dumps({"status": "ok", "project_root": str(engine.project_root), "indexed_files": len(engine._indexed_files), "chunks": stats["chunks"], "graph_edges": stats["graph_edges"], "cache_size": stats["cache_size"], "watcher": watcher, "mvcc_version": stats.get("version", 0)}, indent=2)

    def _admin_config() -> str:
        engine = _get_engine()
        cfg = {}
        for k, v in engine.config.items():
            if isinstance(v, (str, int, float, bool, type(None))):
                cfg[k] = v
            elif isinstance(v, (set, list)):
                cfg[k] = sorted(v) if isinstance(v, set) else v
        return json.dumps(cfg, indent=2, ensure_ascii=False)

    def _admin_update_config(key: str, value: str) -> str:
        engine = _get_engine()
        allowed = {"max_context_tokens", "memory_top_k", "max_hops", "idf_boost_enabled", "idf_boost_weight", "cache_max_size", "adaptive_context", "adaptive_max_context_tokens"}
        if key not in allowed:
            return json.dumps({"error": f"key not allowed", "valid": sorted(allowed)})
        if value.lower() in ("true", "false"):
            parsed = value.lower() == "true"
        elif "." in value:
            try: parsed = float(value)
            except ValueError: parsed = value
        else:
            try: parsed = int(value)
            except ValueError: parsed = value
        engine.config[key] = parsed
        engine.memory.invalidate_cache()
        return json.dumps({"key": key, "value": parsed}, indent=2)

    def _snapshot_create() -> str:
        engine = _get_engine()
        ver = engine.memory.snapshot()
        return json.dumps({"version": ver, "chunks": len([c for c in engine.memory.chunks if c is not None])}, indent=2)

    def _snapshot_read(version: int) -> str:
        engine = _get_engine()
        try:
            snap = engine.memory.read_at(version)
            return json.dumps({"version": version, "active": sum(1 for c in snap.get("chunks", []) if c is not None), "files": len(snap.get("file_chunks", {}))}, indent=2)
        except KeyError as e:
            return json.dumps({"error": str(e)})

    def _snapshot_status() -> str:
        engine = _get_engine()
        stats = engine.memory.stats()
        return json.dumps({"current_version": engine.memory.current_version, "snapshots": len(engine.memory._snapshots), "chunks": stats["chunks"]}, indent=2)

    def _scope_create(project_id: str, root: str, isolation: str = "strict", description: str = "") -> str:
        manager = _get_scope_manager()
        if not project_id or not root:
            return json.dumps({"error": "project_id and root required"})
        if manager.get_scope(project_id):
            return json.dumps({"error": f"Scope '{project_id}' already exists"})
        root_resolved = str(Path(root).resolve())
        config = dict(DEFAULT_CONFIG)
        for ek, ck, t in [("MCP_MAX_TOKENS","max_context_tokens",int),("MCP_TOP_K","memory_top_k",int),("MCP_MAX_HOPS","max_hops",int),("MCP_CACHE_SIZE","cache_max_size",int)]:
            if os.environ.get(ek): config[ck] = t(os.environ[ek])
        eng = VibeCodingEngine(root_resolved, config)
        eng.index_project(); eng.start_watching()
        s = manager.create_scope(project_id=project_id, root=root_resolved, isolation=isolation, description=description)
        manager.bind_engine(project_id, eng)
        return json.dumps({"project_id": s.project_id, "root": s.root, "message": "created"}, indent=2)

    def _scope_list() -> str:
        manager = _get_scope_manager()
        scopes = manager.list_scopes()
        out = [{"id": s.project_id, "root": s.root, "isolation": s.isolation} for s in scopes]
        return json.dumps({"total": len(out), "scopes": out}, indent=2)

    def _scope_link(project_a: str, project_b: str, link_type: str = "reference") -> str:
        manager = _get_scope_manager()
        if not project_a or not project_b:
            return json.dumps({"error": "project_a and project_b required"})
        ok = manager.link_projects(project_a=project_a, project_b=project_b, link_type=link_type)
        return json.dumps({"linked": ok, "link_type": link_type}, indent=2)

    def _synonym_add(cn_term: str, en_terms: str) -> str:
        _ensure_custom_synonyms()
        if not cn_term or not en_terms:
            return json.dumps({"error": "cn_term and en_terms required"})
        en_list = [e.strip() for e in en_terms.split(",") if e.strip()]
        with _synonyms_lock:
            if cn_term not in _custom_synonyms: _custom_synonyms[cn_term] = []
            added = [e for e in en_list if e not in _custom_synonyms[cn_term]]
            _custom_synonyms[cn_term].extend(added)
            _save_custom_synonyms()
            if cn_term not in _synonyms_mod.SYNONYM_MAP: _synonyms_mod.SYNONYM_MAP[cn_term] = []
            for e in added:
                _synonyms_mod.SYNONYM_MAP[cn_term].append(e)
                _synonyms_mod._REVERSE_MAP.setdefault(e, []).append(cn_term)
        return json.dumps({"cn_term": cn_term, "added": added}, indent=2, ensure_ascii=False)

    def _synonym_discover(top_k: int = 10) -> str:
        _ensure_custom_synonyms()
        with _synonyms_lock:
            if not _unmatched_tokens_log:
                return json.dumps({"count": 0, "message": "No unmatched tokens yet"}, indent=2)
            from collections import Counter
            counts = Counter(i["token"] for i in _unmatched_tokens_log)
            candidates = [{"cn": t, "freq": c} for t, c in counts.most_common(top_k) if t not in _synonyms_mod.SYNONYM_MAP and not any(k in t for k in _synonyms_mod.SYNONYM_MAP)]
        return json.dumps({"count": len(candidates), "candidates": candidates}, indent=2, ensure_ascii=False)

    MODULE_REGISTRY["admin"] = {
        "description": "Engine config, snapshots, scopes, synonyms. Rarely needed by LLM.",
        "tools": [
            (_admin_health, "admin_health", "Check engine health: index stats, watcher, cache, MVCC version."),
            (_admin_config, "admin_config", "Get current engine configuration."),
            (_admin_update_config, "admin_update_config", "Update config at runtime. Required: key, value."),
            (_snapshot_create, "snapshot_create", "Create MVCC snapshot for multi-agent isolation."),
            (_snapshot_read, "snapshot_read", "Read snapshot at a specific version. Required: version."),
            (_snapshot_status, "snapshot_status", "Get MVCC status: current version, snapshot count."),
            (_scope_create, "scope_create", "Create project scope. Required: project_id, root."),
            (_scope_list, "scope_list", "List all project scopes."),
            (_scope_link, "scope_link", "Link two projects. Required: project_a, project_b."),
            (_synonym_add, "synonym_add", "Add Chinese-English synonym. Required: cn_term, en_terms (comma-separated)."),
            (_synonym_discover, "synonym_discover", "Discover candidate synonyms from unmatched query tokens."),
        ],
    }


# ══════════════════════════════════════════════════
#  Tool 1: search — 代码检索（始终加载）
# ══════════════════════════════════════════════════

@mcp.tool()
def search(query: str, top_k: int = 5, mode: str = "graph") -> str:
    """
    Search project code using AST call graph + BM25 ranking.

    Modes:
      - graph: BM25 + graph diffusion (default, best for most queries)
      - deps:  graph + cross-file dependency expansion

    Args:
        query: Natural language or code term to search for
        top_k: Number of results (default 5, max 20)
        mode: "graph" or "deps"
    """
    engine = _get_engine()
    top_k = min(max(top_k, 1), 20)
    fn = engine.retrieve_with_deps if mode == "deps" else engine.retrieve
    results = fn(query, top_k=top_k)
    results = _post_retrieve(query, results)
    return engine.format_results(results)


# ══════════════════════════════════════════════════
#  Tool 2: tools — 动态工具管理器（始终加载）
# ══════════════════════════════════════════════════

@mcp.tool()
def tools(action: str = "list", module: str = "", ctx: Context = None) -> str:
    """
    Dynamic tool loader. Load/unload tool modules on demand to save context tokens.

    Actions:
      - list:   Show available modules and their status (loaded/unloaded)
      - load:   Load a module's tools into the session. Sends tools/list_changed notification.
      - unload: Unload a module's tools from the session.
      - loaded: Show currently loaded tools.

    Args:
        action: Operation (list|load|unload|loaded)
        module: Module name to load/unload (for load/unload)
    """
    _ensure_registry()
    loaded = _get_loaded_modules(ctx)

    if action == "list":
        result = {}
        for name, mod in MODULE_REGISTRY.items():
            tool_names = [t[1] for t in mod["tools"]]
            result[name] = {
                "description": mod["description"],
                "tools": tool_names,
                "loaded": name in loaded,
            }
        return json.dumps(result, indent=2, ensure_ascii=False)

    elif action == "load":
        if not module:
            return json.dumps({"error": "module name required", "available": list(MODULE_REGISTRY.keys())})
        if module not in MODULE_REGISTRY:
            return json.dumps({"error": f"Unknown module '{module}'", "available": list(MODULE_REGISTRY.keys())})
        if module in loaded:
            return json.dumps({"message": f"Module '{module}' already loaded", "tools": [t[1] for t in MODULE_REGISTRY[module]["tools"]]})

        mod = MODULE_REGISTRY[module]
        registered = []
        for fn, name, desc in mod["tools"]:
            mcp.add_tool(fn, name=name, description=desc)
            registered.append(name)
        loaded.add(module)

        # 通知客户端工具列表已变更
        notified = False
        if ctx and hasattr(ctx, "session"):
            try:
                ctx.session.send_tool_list_changed()
                notified = True
            except Exception:
                pass

        result = {
            "module": module,
            "loaded": registered,
            "message": f"Loaded {len(registered)} tools.",
        }
        if not notified:
            result["hint"] = (
                "Client did not acknowledge tools/list_changed notification. "
                "If these tools appear as 'unknown', please start a new conversation "
                "or re-request the tool list to pick them up."
            )
        return json.dumps(result, indent=2)

    elif action == "unload":
        if not module:
            return json.dumps({"error": "module name required"})
        if module not in loaded:
            return json.dumps({"error": f"Module '{module}' not loaded"})
        mod = MODULE_REGISTRY[module]
        removed = []
        for _, name, _ in mod["tools"]:
            try:
                mcp.remove_tool(name)
                removed.append(name)
            except Exception:
                pass
        loaded.discard(module)

        notified = False
        if ctx and hasattr(ctx, "session"):
            try:
                ctx.session.send_tool_list_changed()
                notified = True
            except Exception:
                pass

        result = {
            "module": module,
            "unloaded": removed,
            "message": f"Unloaded {len(removed)} tools.",
        }
        if not notified:
            result["hint"] = "Client did not acknowledge tools/list_changed notification."
        return json.dumps(result, indent=2)

    elif action == "loaded":
        all_tools = []
        for mod_name in loaded:
            mod = MODULE_REGISTRY.get(mod_name, {})
            for _, name, desc in mod.get("tools", []):
                all_tools.append({"name": name, "module": mod_name, "desc": desc[:60]})
        return json.dumps({"loaded_modules": sorted(loaded), "tools": all_tools}, indent=2, ensure_ascii=False)

    else:
        return json.dumps({"error": f"Unknown action '{action}'", "valid": ["list", "load", "unload", "loaded"]})


# ══════════════════════════════════════════════════
#  资源
# ══════════════════════════════════════════════════

@mcp.resource("context://stats")
def get_stats() -> str:
    engine = _get_engine()
    return json.dumps(engine.stats(), indent=2)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
