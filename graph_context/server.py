"""
MCP Server — v6 优化版
  - 新增 MVCC 快照工具
  - 新增规则管理工具 (add_rule, list_rules, evaluate_rules, apply_rule, prune_rules)
  - 新增项目作用域工具 (create_scope, link_projects, search_across_projects)
  - 保持 V5 所有工具兼容
"""

import os
import json
import time
import threading
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    from mcp.server.fast_server import FastMCP

from .engine import VibeCodingEngine, DEFAULT_CONFIG
from .rules import Rule, RulesStore
from .project_scope import ScopeManager

# ══════════════════════════════════════════════════
#  全局状态
# ══════════════════════════════════════════════════

_engine: VibeCodingEngine | None = None
_engine_lock = threading.Lock()
_initialized = False

_rules_store: RulesStore | None = None
_scope_manager: ScopeManager | None = None

mcp = FastMCP("mcporter")


def _get_engine() -> VibeCodingEngine:
    """懒初始化引擎：首次调用时从环境变量读取配置"""
    global _engine, _initialized
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine

        root = os.environ.get("PROJECT_ROOT", os.getcwd())
        config = dict(DEFAULT_CONFIG)

        if os.environ.get("MCP_MAX_TOKENS"):
            config["max_context_tokens"] = int(os.environ["MCP_MAX_TOKENS"])
        if os.environ.get("MCP_TOP_K"):
            config["memory_top_k"] = int(os.environ["MCP_TOP_K"])
        if os.environ.get("MCP_MAX_HOPS"):
            config["max_hops"] = int(os.environ["MCP_MAX_HOPS"])
        if os.environ.get("MCP_PERSIST_PATH"):
            config["persist_path"] = os.environ["MCP_PERSIST_PATH"]
        if os.environ.get("MCP_CACHE_SIZE"):
            config["cache_max_size"] = int(os.environ["MCP_CACHE_SIZE"])

        engine = VibeCodingEngine(root, config)
        engine.index_project()
        engine.start_watching()
        _engine = engine
        _initialized = True
        return engine


def _get_rules_store() -> RulesStore:
    """获取规则存储实例"""
    global _rules_store
    if _rules_store is None:
        persist = os.environ.get("MCP_RULES_PATH", "rules/rules.json")
        _rules_store = RulesStore(persist_path=persist)
    return _rules_store


def _get_scope_manager() -> ScopeManager:
    """获取作用域管理器实例"""
    global _scope_manager
    if _scope_manager is None:
        persist = os.environ.get("MCP_SCOPES_PATH", "scopes/scopes.json")
        _scope_manager = ScopeManager(persist_path=persist)
    return _scope_manager


# ══════════════════════════════════════════════════
#  V5 工具（完全兼容）
# ══════════════════════════════════════════════════

@mcp.tool()
def retrieve_context(query: str, top_k: int = 5) -> str:
    """
    Retrieve the most relevant code chunks for a given query.
    Uses graph diffusion to find related code based on token co-occurrence.

    Args:
        query: The query to search for
        top_k: The number of results to return
    """
    engine = _get_engine()
    results = engine.retrieve(query, top_k=top_k)
    return engine.format_results(results)


@mcp.tool()
def retrieve_context_adaptive(query: str, top_k: int = 5) -> str:
    """
    Retrieve context with adaptive strategy.
    Automatically switches between full-context scoring (small projects)
    and graph diffusion (large projects) based on project size.

    Args:
        query: The query to search for
        top_k: The number of results to return
    """
    engine = _get_engine()
    results = engine.retrieve_adaptive(query, top_k=top_k)
    return engine.format_results(results)


@mcp.tool()
def retrieve_with_dependencies(query: str, top_k: int = 5) -> str:
    """
    Retrieve relevant code chunks AND their cross-file dependencies.
    Returns relevant code plus related functions/classes from other files.

    Args:
        query: The query to search for
        top_k: The number of results to return
    """
    engine = _get_engine()
    results = engine.retrieve_with_deps(query, top_k=top_k)
    return engine.format_results(results)


@mcp.tool()
def batch_retrieve(queries: list[str], top_k: int = 3) -> str:
    """
    Batch retrieve context for multiple queries at once.
    More efficient than calling retrieve_context multiple times.

    Args:
        queries: List of queries to search for
        top_k: Number of results per query
    """
    engine = _get_engine()
    all_results = []
    for i, q in enumerate(queries[:10]):
        results = engine.retrieve(q, top_k=top_k)
        formatted = engine.format_results(results)
        all_results.append(f"## Query {i+1}: {q}\n\n{formatted}")
    return "\n\n---\n\n".join(all_results)


@mcp.tool()
def compare_strategies(query: str) -> str:
    """
    Compare different retrieval strategies (full context, recent files, graph diffusion).
    Shows token usage for each approach to evaluate savings.

    Args:
        query: The query to compare strategies with
    """
    engine = _get_engine()
    result = engine.compare_strategies(query)
    return (
        f"Query: {result['query']}\n"
        f"Full Context: {result['full_context_tokens']} tokens\n"
        f"Recent Files: {result['recent_files_tokens']} tokens\n"
        f"Graph Diffusion: {result['graph_diffusion_tokens']} tokens\n"
        f"Savings: {result['savings']}"
    )


@mcp.tool()
def health_check() -> str:
    """
    Check the health status of the context engine.
    Returns indexing stats, file watcher status, cache info, and MVCC version.
    """
    engine = _get_engine()
    stats = engine.stats()
    watcher_status = "running" if (engine.watcher and engine.watcher.is_running) else "stopped"
    return json.dumps({
        "status": "ok",
        "version": "6.0.0",
        "project_root": str(engine.project_root),
        "indexed_files": len(engine._indexed_files),
        "chunks": stats["chunks"],
        "token_types": stats["token_types"],
        "graph_edges": stats["graph_edges"],
        "cache_size": stats["cache_size"],
        "watcher": watcher_status,
        "persist_path": engine.config.get("persist_path"),
        "mvcc_version": stats.get("version", 0),
        "snapshots_stored": stats.get("snapshots_stored", 0),
    }, indent=2)


@mcp.tool()
def get_config() -> str:
    """
    Get the current engine configuration.
    """
    engine = _get_engine()
    safe_config = {}
    for k, v in engine.config.items():
        if isinstance(v, (str, int, float, bool, type(None))):
            safe_config[k] = v
        elif isinstance(v, (set, list)):
            safe_config[k] = sorted(v) if isinstance(v, set) else v
        elif isinstance(v, dict):
            safe_config[k] = {str(kk): vv for kk, vv in v.items()}
    return json.dumps(safe_config, indent=2, ensure_ascii=False)


@mcp.tool()
def update_config(key: str, value: str) -> str:
    """
    Update a configuration value at runtime.
    Supports: max_context_tokens, memory_top_k, max_hops, idf_boost_enabled,
    idf_boost_weight, cache_max_size

    Args:
        key: Configuration key to update
        value: New value (will be auto-parsed as int/float/bool if applicable)
    """
    engine = _get_engine()
    allowed_keys = {
        "max_context_tokens", "memory_top_k", "max_hops",
        "idf_boost_enabled", "idf_boost_weight", "cache_max_size",
        "adaptive_context", "adaptive_max_context_tokens",
    }
    if key not in allowed_keys:
        return f"Error: key '{key}' not in allowed keys: {sorted(allowed_keys)}"

    if value.lower() in ("true", "false"):
        parsed = value.lower() == "true"
    elif "." in value:
        try:
            parsed = float(value)
        except ValueError:
            parsed = value
    else:
        try:
            parsed = int(value)
        except ValueError:
            parsed = value

    engine.config[key] = parsed
    engine.memory.invalidate_cache()
    return f"Updated {key} = {parsed}"


# ══════════════════════════════════════════════════
#  V6 工具 — MVCC 快照
# ══════════════════════════════════════════════════

@mcp.tool()
def create_snapshot() -> str:
    """
    Create a MVCC snapshot of the current engine state.
    Returns the version number of the snapshot.
    Useful for multi-agent read-write isolation.
    """
    engine = _get_engine()
    version = engine.memory.snapshot()
    return json.dumps({
        "version": version,
        "message": f"Snapshot created at version {version}",
        "chunks": len([c for c in engine.memory.chunks if c is not None]),
    }, indent=2)


@mcp.tool()
def read_at_version(version: int) -> str:
    """
    Read engine state at a specific MVCC version.
    Returns metadata about the snapshot (not full content, for efficiency).

    Args:
        version: The version number to read at
    """
    engine = _get_engine()
    try:
        snap = engine.memory.read_at(version)
        chunks = snap.get("chunks", [])
        active = sum(1 for c in chunks if c is not None)
        return json.dumps({
            "version": version,
            "timestamp": snap.get("timestamp", 0),
            "active_chunks": active,
            "total_chunks": len(chunks),
            "files": len(snap.get("file_chunks", {})),
            "token_types": len(snap.get("token_to_chunks", {})),
        }, indent=2)
    except KeyError as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_mvcc_status() -> str:
    """
    Get MVCC status: current version, stored snapshots, etc.
    """
    engine = _get_engine()
    stats = engine.memory.stats()
    snapshot_versions = sorted(engine.memory._snapshots.keys())
    return json.dumps({
        "current_version": engine.memory.current_version,
        "snapshots_stored": len(snapshot_versions),
        "snapshot_versions": snapshot_versions,
        "max_snapshots": engine.config.get("mvcc_max_snapshots", 10),
        "chunks": stats["chunks"],
        "files": stats["files"],
    }, indent=2)


# ══════════════════════════════════════════════════
#  V6 工具 — 规则管理
# ══════════════════════════════════════════════════

@mcp.tool()
def add_rule(
    rule_type: str,
    description: str,
    condition: str,
    action: str,
    scope: str = "project",
    confidence: float = 0.8,
    source: str = "manual",
    tags: list[str] = None,
    priority: int = 0,
    related_files: list[str] = None,
) -> str:
    """
    Add a new rule to the rules store.
    Rules use accuracy-based decay (not time-based): they stay valid as long as they prove correct.

    Args:
        rule_type: Type of rule: "pattern", "preference", or "constraint"
        description: Human-readable description of the rule
        condition: Trigger condition (tokenized for matching, not substring)
        action: Suggested action when condition is met
        scope: "project" or "global"
        confidence: Initial confidence (0.0-1.0)
        source: "auto", "manual", or "community"
        tags: Optional tags for categorization
        priority: Priority level (higher = checked first, used for conflict resolution)
        related_files: Files this rule relates to (for code-change-aware decay)
    """
    store = _get_rules_store()

    # 冲突检测
    rule = Rule(
        rule_id="",
        rule_type=rule_type,
        scope=scope,
        description=description,
        condition=condition,
        action=action,
        confidence=confidence,
        source=source,
        tags=tags or [],
        priority=priority,
        related_files=related_files or [],
    )
    conflicts = store.detect_conflicts(rule)

    rule_id = store.add_rule(rule)
    result = {
        "rule_id": rule_id,
        "status": rule.status,
        "message": f"Rule added: {rule_id} (status: {rule.status})",
        "effective_confidence": round(rule.effective_confidence, 4),
    }
    if conflicts:
        result["conflicts"] = conflicts
        result["conflict_warning"] = f"Found {len(conflicts)} potential conflict(s) with existing rules"
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
def list_rules(
    rule_type: str = "",
    scope: str = "",
    min_confidence: float = 0.0,
    status: str = "",
) -> str:
    """
    List rules in the store, with optional filters.

    Args:
        rule_type: Filter by type ("pattern", "preference", "constraint") or empty for all
        scope: Filter by scope ("project", "global") or empty for all
        min_confidence: Minimum effective confidence threshold
        status: Filter by status ("observing", "active", "disabled") or empty for all
    """
    store = _get_rules_store()
    rules = store.list_rules(
        rule_type=rule_type or None,
        scope=scope or None,
        min_confidence=min_confidence,
        status=status or None,
    )
    result = []
    for r in rules:
        result.append({
            "rule_id": r.rule_id,
            "rule_type": r.rule_type,
            "scope": r.scope,
            "description": r.description,
            "condition": r.condition,
            "action": r.action,
            "confidence": r.confidence,
            "effective_confidence": round(r.effective_confidence, 4),
            "hit_count": r.hit_count,
            "miss_count": r.miss_count,
            "hit_rate": round(r.hit_rate, 4),
            "accuracy_rate": round(r.accuracy_rate, 4),
            "verified_count": r.verified_count,
            "rejected_count": r.rejected_count,
            "source": r.source,
            "status": r.status,
            "enabled": r.enabled,
            "priority": r.priority,
        })
    return json.dumps({
        "count": len(result),
        "rules": result,
    }, indent=2, ensure_ascii=False)


@mcp.tool()
def evaluate_rules() -> str:
    """
    Evaluate rule effectiveness using ground truth data.
    Compares retrieval accuracy with and without rules applied.
    """
    engine = _get_engine()
    store = _get_rules_store()
    result = store.evaluate_rules(engine)
    return json.dumps(result, indent=2)


@mcp.tool()
def apply_rule(rule_id: str) -> str:
    """
    Apply a specific rule to the retrieval pipeline.
    Records a hit for the rule and returns its details.

    Args:
        rule_id: The ID of the rule to apply
    """
    store = _get_rules_store()
    rule = store.get_rule(rule_id)
    if rule is None:
        return json.dumps({"error": f"Rule '{rule_id}' not found"})
    store.record_hit(rule_id)
    return json.dumps({
        "rule_id": rule.rule_id,
        "description": rule.description,
        "condition": rule.condition,
        "action": rule.action,
        "effective_confidence": round(rule.effective_confidence, 4),
        "hit_count": rule.hit_count + 1,
        "message": "Rule applied, hit recorded",
    }, indent=2)


@mcp.tool()
def prune_rules(min_hit_rate: float = 0.1, min_uses: int = 5) -> str:
    """
    Prune low-performing rules.
    Rules with hit rate below threshold and enough usage data will be disabled.
    Uses accuracy-based decay (not time-based).

    Args:
        min_hit_rate: Minimum hit rate threshold (default 0.1)
        min_uses: Minimum number of uses before pruning (default 5)
    """
    store = _get_rules_store()
    # 基于准确率的衰减
    decay_disabled = store.apply_decay()
    # 清理低效规则
    pruned = store.prune_rules(min_hit_rate=min_hit_rate, min_uses=min_uses)
    # 提升观察期规则
    promoted = store.promote_observing_rules()
    # 禁用观察期中表现极差的规则
    demoted = store.demote_failing_rules()
    return json.dumps({
        "decay_disabled": decay_disabled,
        "pruned": pruned,
        "promoted_from_observing": promoted,
        "demoted_from_observing": demoted,
        "total_disabled": len(decay_disabled) + len(pruned) + len(demoted),
        "total_promoted": len(promoted),
        "remaining_active": len(store.list_rules(enabled_only=True, status="active")),
    }, indent=2)


@mcp.tool()
def verify_rule(rule_id: str, correct: bool = True) -> str:
    """
    Verify a rule's effectiveness based on user feedback.
    This is the core of accuracy-based decay: rules stay valid as long as they're verified correct.

    Args:
        rule_id: The ID of the rule to verify
        correct: True if the rule was helpful, False if it was wrong
    """
    store = _get_rules_store()
    rule = store.get_rule(rule_id)
    if rule is None:
        return json.dumps({"error": f"Rule '{rule_id}' not found"})

    if correct:
        store.record_verified(rule_id)
    else:
        store.record_rejected(rule_id)

    rule = store.get_rule(rule_id)  # re-fetch
    return json.dumps({
        "rule_id": rule_id,
        "verified_count": rule.verified_count,
        "rejected_count": rule.rejected_count,
        "accuracy_rate": round(rule.accuracy_rate, 4),
        "effective_confidence": round(rule.effective_confidence, 4),
        "message": f"Rule {'verified' if correct else 'rejected'}",
    }, indent=2)


@mcp.tool()
def check_rule_conflicts(
    rule_type: str,
    condition: str,
    action: str,
) -> str:
    """
    Check if a new rule would conflict with existing rules before adding it.
    Returns conflict details and recommendations.

    Args:
        rule_type: Type of the proposed rule
        condition: Condition of the proposed rule
        action: Action of the proposed rule
    """
    store = _get_rules_store()
    proposed = Rule(
        rule_id="",
        rule_type=rule_type,
        scope="project",
        description="conflict check",
        condition=condition,
        action=action,
        confidence=0.5,
    )
    conflicts = store.detect_conflicts(proposed)
    return json.dumps({
        "has_conflicts": len(conflicts) > 0,
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
    }, indent=2, ensure_ascii=False)


@mcp.tool()
def discover_rules(top_k: int = 5) -> str:
    """
    Auto-discover candidate rules from Ground Truth evaluation errors.
    Analyzes missed queries and suggests rules to improve retrieval.
    New rules enter 'observing' mode for validation.
    """
    engine = _get_engine()
    store = _get_rules_store()
    gt_path = engine.config.get("ground_truth_path", "ground_truth/opencode_ground_truth.json")
    discovered = store.discover_rules_from_errors(engine, gt_path, top_k=top_k)
    return json.dumps({
        "discovered_count": len(discovered),
        "rule_ids": discovered,
        "message": f"Discovered {len(discovered)} candidate rules (status: observing)",
    }, indent=2)


@mcp.tool()
def check_code_freshness() -> str:
    """
    Check if rules' related files have been modified.
    Rules tied to rewritten code will have their confidence reduced.
    """
    engine = _get_engine()
    store = _get_rules_store()
    affected = store.check_code_freshness(engine)
    return json.dumps({
        "affected_rules": affected,
        "count": len(affected),
        "message": f"{len(affected)} rules affected by code changes",
    }, indent=2)


# ══════════════════════════════════════════════════
#  V6 工具 — 项目作用域
# ══════════════════════════════════════════════════

@mcp.tool()
def create_scope(
    project_id: str,
    root: str,
    isolation: str = "strict",
    description: str = "",
    tags: list[str] = None,
) -> str:
    """
    Create a new project scope.
    Each scope manages its own index and can be isolated or shared.

    Args:
        project_id: Unique project identifier
        root: Project root directory path
        isolation: "strict" (independent index) or "shared" (can reference other projects)
        description: Project description
        tags: Optional tags
    """
    manager = _get_scope_manager()
    try:
        scope = manager.create_scope(
            project_id=project_id,
            root=root,
            isolation=isolation,
            description=description,
            tags=tags or [],
        )
        return json.dumps({
            "project_id": scope.project_id,
            "root": scope.root,
            "isolation": scope.isolation,
            "message": f"Scope '{project_id}' created",
        }, indent=2)
    except ValueError as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def link_projects(
    project_a: str,
    project_b: str,
    link_type: str = "reference",
    shared_rules: list[str] = None,
    shared_patterns: list[str] = None,
) -> str:
    """
    Link two projects together.
    Enables cross-project pattern sharing and searching.

    Args:
        project_a: First project ID
        project_b: Second project ID
        link_type: "reference" (one-way), "bidirectional" (two-way), "shared" (full sharing)
        shared_rules: Rule IDs to share between projects
        shared_patterns: Pattern names to share
    """
    manager = _get_scope_manager()
    success = manager.link_projects(
        project_a=project_a,
        project_b=project_b,
        link_type=link_type,
        shared_rules=shared_rules,
        shared_patterns=shared_patterns,
    )
    if success:
        return json.dumps({
            "message": f"Projects '{project_a}' and '{project_b}' linked",
            "link_type": link_type,
        })
    else:
        return json.dumps({"error": "Failed to link projects. Check that both projects exist."})


@mcp.tool()
def search_across_projects(
    query: str,
    source_project: str = "",
    top_k: int = 5,
    aggregate: bool = True,
) -> str:
    """
    Search for patterns across linked projects.
    Returns aggregated pattern summaries (no code content) for security.
    Patterns are clustered by name across projects.

    Args:
        query: Search query
        source_project: Source project ID (empty to search all)
        top_k: Maximum results per project
        aggregate: If true, aggregate patterns across projects; if false, return flat list
    """
    manager = _get_scope_manager()
    results = manager.search_across_projects(
        query=query,
        source_project=source_project or None,
        top_k=top_k,
        aggregate=aggregate,
    )
    if aggregate:
        result = [r.to_dict() for r in results]
    else:
        result = [r.to_dict() for r in results]
    return json.dumps({
        "count": len(result),
        "aggregated": aggregate,
        "patterns": result,
    }, indent=2, ensure_ascii=False)


@mcp.tool()
def list_scopes() -> str:
    """
    List all project scopes with their configuration and stats.
    """
    manager = _get_scope_manager()
    scopes = manager.list_scopes()
    result = []
    for s in scopes:
        result.append({
            "project_id": s.project_id,
            "root": s.root,
            "isolation": s.isolation,
            "description": s.description,
            "shared_rules": s.shared_rules,
            "shared_patterns": s.shared_patterns,
            "cross_references": list(s.cross_references.keys()),
            "tags": s.tags,
        })
    stats = manager.stats()
    return json.dumps({
        "stats": stats,
        "scopes": result,
    }, indent=2, ensure_ascii=False)


# ══════════════════════════════════════════════════
#  资源定义
# ══════════════════════════════════════════════════

@mcp.resource("context://stats")
def get_stats() -> str:
    """Get current index statistics as a resource."""
    engine = _get_engine()
    stats = engine.stats()
    return json.dumps(stats, indent=2)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
