"""
V6.1 实验测试脚本
==================
测试改进:
  1. MVCC COW 快照 (无 deepcopy)
  2. 规则衰减: 基于准确率，非时间
  3. 观察模式: 新规则先观察再启用
  4. 冲突检测
  5. Token 匹配 (非子串)
  6. 跨项目模式聚合
  7. 自动发现规则

运行: python -m graph_context.experiment
"""

import os
import sys
import time
import json
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from graph_context.engine import VibeCodingEngine, DEFAULT_CONFIG
from graph_context.rules import Rule, RulesStore
from graph_context.project_scope import ScopeManager


# ══════════════════════════════════════════════════
#  测试数据生成
# ══════════════════════════════════════════════════

def generate_test_project(root: str, prefix: str = "test"):
    os.makedirs(root, exist_ok=True)

    auth_code = '''"""用户认证模块"""
import hashlib
import time
from typing import Optional

class AuthService:
    """处理用户登录、注册、令牌管理"""
    def __init__(self, db_connection):
        self.db = db_connection
        self.token_cache = {}
        self.max_login_attempts = 5

    def authenticate(self, username: str, password: str) -> Optional[str]:
        """验证用户凭据，返回 JWT token"""
        user = self.db.find_user(username)
        if not user:
            return None
        if not self._verify_password(password, user.password_hash):
            return None
        token = self._generate_token(user)
        self.token_cache[user.id] = token
        return token

    def _verify_password(self, password: str, password_hash: str) -> bool:
        computed = hashlib.sha256(password.encode()).hexdigest()
        return computed == password_hash

    def _generate_token(self, user) -> str:
        payload = {"user_id": user.id, "exp": time.time() + 3600}
        return f"jwt_{hashlib.md5(str(payload).encode()).hexdigest()}"

    def revoke_token(self, token: str) -> bool:
        for uid, t in self.token_cache.items():
            if t == token:
                del self.token_cache[uid]
                return True
        return False
'''
    with open(os.path.join(root, "auth.py"), "w") as f:
        f.write(auth_code)

    user_code = '''"""用户管理模块"""
from typing import List, Optional
from auth import AuthService

class UserModel:
    def __init__(self, id: int, username: str, email: str, role: str = "user"):
        self.id = id
        self.username = username
        self.email = email
        self.role = role
        self.password_hash = ""
        self.created_at = 0

class UserService:
    def __init__(self, db, auth_service: AuthService):
        self.db = db
        self.auth = auth_service

    def create_user(self, username: str, email: str, password: str) -> UserModel:
        user = UserModel(id=self.db.next_id(), username=username, email=email)
        user.password_hash = self._hash_password(password)
        self.db.save(user)
        return user

    def get_user(self, user_id: int) -> Optional[UserModel]:
        return self.db.find_by_id(user_id)

    def list_users(self, page: int = 1, size: int = 20) -> List[UserModel]:
        return self.db.paginate(page=page, size=size)

    def delete_user(self, user_id: int) -> bool:
        return self.db.delete(user_id)

    def _hash_password(self, password: str) -> str:
        import hashlib
        return hashlib.sha256(password.encode()).hexdigest()
'''
    with open(os.path.join(root, "user.py"), "w") as f:
        f.write(user_code)

    api_code = '''"""API 路由模块"""
from flask import Flask, request, jsonify
from user import UserService
from auth import AuthService

app = Flask(__name__)
auth_service = AuthService(db_connection=None)
user_service = UserService(db=None, auth_service=auth_service)

@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json()
    token = auth_service.authenticate(data.get("username"), data.get("password"))
    if token:
        return jsonify({"token": token})
    return jsonify({"error": "认证失败"}), 401

@app.route("/api/users", methods=["GET"])
def list_users():
    page = request.args.get("page", 1, type=int)
    users = user_service.list_users(page=page)
    return jsonify([u.__dict__ for u in users])

@app.route("/api/users", methods=["POST"])
def create_user():
    data = request.get_json()
    user = user_service.create_user(data["username"], data["email"], data["password"])
    return jsonify(user.__dict__), 201
'''
    with open(os.path.join(root, "api.py"), "w") as f:
        f.write(api_code)

    db_code = '''"""数据库连接模块"""
import json
import os
from typing import Optional, List

class DatabaseConnection:
    def __init__(self, db_path: str = "data/db.json"):
        self.db_path = db_path
        self._data = {}
        self._next_id = 1

    def save(self, entity) -> None:
        table = type(entity).__name__
        if table not in self._data:
            self._data[table] = {}
        self._data[table][str(entity.id)] = entity.__dict__

    def find_by_id(self, entity_id: int, table: str = "UserModel") -> Optional[dict]:
        return self._data.get(table, {}).get(str(entity_id))

    def find_user(self, username: str) -> Optional[dict]:
        for uid, data in self._data.get("UserModel", {}).items():
            if data.get("username") == username:
                return data
        return None

    def paginate(self, page: int = 1, size: int = 20, table: str = "UserModel") -> List[dict]:
        items = list(self._data.get(table, {}).values())
        start = (page - 1) * size
        return items[start:start + size]

    def delete(self, entity_id: int, table: str = "UserModel") -> bool:
        if str(entity_id) in self._data.get(table, {}):
            del self._data[table][str(entity_id)]
            return True
        return False

    def next_id(self) -> int:
        nid = self._next_id
        self._next_id += 1
        return nid
'''
    with open(os.path.join(root, "database.py"), "w") as f:
        f.write(db_code)

    cache_code = '''"""缓存管理模块"""
import time
from typing import Any, Optional
from collections import OrderedDict

class LRUCache:
    def __init__(self, max_size: int = 1000, ttl: int = 300):
        self.max_size = max_size
        self.ttl = ttl
        self._cache = OrderedDict()
        self._timestamps = {}

    def get(self, key: str) -> Optional[Any]:
        if key not in self._cache:
            return None
        if time.time() - self._timestamps[key] > self.ttl:
            del self._cache[key]
            del self._timestamps[key]
            return None
        self._cache.move_to_end(key)
        return self._cache[key]

    def put(self, key: str, value: Any) -> None:
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = value
        self._timestamps[key] = time.time()
        if len(self._cache) > self.max_size:
            oldest = next(iter(self._cache))
            del self._cache[oldest]
            del self._timestamps[oldest]
'''
    with open(os.path.join(root, "cache.py"), "w") as f:
        f.write(cache_code)


# ══════════════════════════════════════════════════
#  实验 1: MVCC COW 快照
# ══════════════════════════════════════════════════

def test_mvcc(project_root: str) -> dict:
    print("\n" + "=" * 60)
    print("实验 1: MVCC COW 快照 (无 deepcopy)")
    print("=" * 60)

    config = dict(DEFAULT_CONFIG)
    engine = VibeCodingEngine(project_root, config)
    files, chunks = engine.index_project()
    print(f"  索引: {files} 文件, {chunks} chunks")

    # 创建快照
    v0 = engine.memory.snapshot()
    print(f"  快照 v0: version={v0}")

    # 写入新 chunk
    from graph_context.engine import CodeChunk
    for i in range(5):
        engine.memory.add_chunk(CodeChunk(
            file_path=f"<virtual>/new_{i}.py",
            chunk_type="function",
            name=f"new_func_{i}",
            content=f"def new_func_{i}(): return {i}",
            start_line=1, end_line=1,
        ))

    v1 = engine.memory.snapshot()
    print(f"  写入 5 chunks 后快照 v1: version={v1}")

    # 读取旧版本
    snap_v0 = engine.memory.read_at(v0)
    active_v0 = sum(1 for c in snap_v0["chunks"] if c is not None)
    print(f"  读取 v0: {active_v0} active chunks (原始)")

    # 性能对比：COW vs deepcopy 模拟
    start = time.perf_counter()
    for _ in range(100):
        engine.memory.snapshot()
    cow_time = (time.perf_counter() - start) * 1000 / 100

    # 模拟 deepcopy 的开销（只拷贝 chunks 列表）
    import copy
    start = time.perf_counter()
    for _ in range(10):
        copy.deepcopy(engine.memory.chunks)
    deepcopy_time = (time.perf_counter() - start) * 1000 / 10

    print(f"  COW 快照: {cow_time:.3f}ms/次")
    print(f"  deepcopy 模拟: {deepcopy_time:.1f}ms/次")
    print(f"  加速比: {deepcopy_time / max(cow_time, 0.001):.0f}x")

    # 查询延迟
    start = time.perf_counter()
    for _ in range(100):
        engine.retrieve("user authentication", top_k=3)
    query_time = (time.perf_counter() - start) * 1000 / 100
    print(f"  查询延迟: {query_time:.2f}ms")

    return {
        "files": files, "chunks": chunks,
        "cow_ms": round(cow_time, 3),
        "deepcopy_ms": round(deepcopy_time, 1),
        "speedup": round(deepcopy_time / max(cow_time, 0.001), 0),
        "query_ms": round(query_time, 2),
    }


# ══════════════════════════════════════════════════
#  实验 2: 规则系统 (准确率衰减 + 观察期 + 冲突)
# ══════════════════════════════════════════════════

def test_rules(project_root: str) -> dict:
    print("\n" + "=" * 60)
    print("实验 2: 规则系统 (准确率衰减 + 观察期 + 冲突检测)")
    print("=" * 60)

    config = dict(DEFAULT_CONFIG)
    engine = VibeCodingEngine(project_root, config)
    engine.index_project()

    rules_path = os.path.join(project_root, ".test_rules.json")
    store = RulesStore(persist_path=rules_path)

    # 1. 添加规则，测试冲突检测
    print("\n  --- 冲突检测 ---")
    rule_a = Rule(
        rule_id="", rule_type="pattern", scope="project",
        description="认证查询应优先返回 auth 模块",
        condition="authentication login",
        action="boost auth.py",
        confidence=0.9, source="manual",
    )
    rid_a = store.add_rule(rule_a)
    print(f"  添加规则 A: {rid_a}")

    # 故意添加矛盾规则
    rule_b = Rule(
        rule_id="", rule_type="pattern", scope="project",
        description="认证查询应过滤 auth 模块",
        condition="authentication login",
        action="filter auth.py",
        confidence=0.7, source="manual",
    )
    conflicts = store.detect_conflicts(rule_b)
    print(f"  规则 B 与 A 冲突检测: {len(conflicts)} 个冲突")
    for c in conflicts:
        print(f"    - {c['conflict_type']}: {c['description'][:60]}...")

    # 添加不冲突的规则
    rule_c = Rule(
        rule_id="", rule_type="pattern", scope="project",
        description="用户查询应优先返回 UserService",
        condition="user service",
        action="boost UserService",
        confidence=0.85, source="manual",
    )
    rid_c = store.add_rule(rule_c, auto_observe=False)

    # 2. 观察模式
    print("\n  --- 观察模式 ---")
    auto_rule = Rule(
        rule_id="", rule_type="pattern", scope="project",
        description="自动发现的规则",
        condition="cache strategy",
        action="boost cache.py",
        confidence=0.6, source="auto",
    )
    rid_auto = store.add_rule(auto_rule, auto_observe=True)
    auto_r = store.get_rule(rid_auto)
    print(f"  自动规则状态: {auto_r.status} (应为 observing)")

    # 模拟观察期
    for _ in range(8):
        store.get_rule(rid_auto).record_observation(triggered=True)
    for _ in range(4):
        store.get_rule(rid_auto).record_observation(triggered=False)
    auto_r = store.get_rule(rid_auto)
    print(f"  观察期: {auto_r.observing_hits}/{auto_r.observing_total} = {auto_r.observation_score:.2f}")

    # 提升观察期规则
    promoted = store.promote_observing_rules()
    auto_r = store.get_rule(rid_auto)
    print(f"  提升结果: {promoted}, 状态: {auto_r.status}")

    # 3. 准确率衰减 (非时间衰减)
    print("\n  --- 准确率衰减 (非时间) ---")
    # 规则 A: 大量验证，高准确率
    for _ in range(20):
        store.record_verified(rid_a)
    for _ in range(3):
        store.record_rejected(rid_a)

    # 规则 C: 大量验证，低准确率
    for _ in range(2):
        store.record_verified(rid_c)
    for _ in range(15):
        store.record_rejected(rid_c)

    rule_a = store.get_rule(rid_a)
    rule_c = store.get_rule(rid_c)
    print(f"  规则 A: verified={rule_a.verified_count}, rejected={rule_a.rejected_count}, "
          f"accuracy={rule_a.accuracy_rate:.2f}, eff_conf={rule_a.effective_confidence:.4f}")
    print(f"  规则 C: verified={rule_c.verified_count}, rejected={rule_c.rejected_count}, "
          f"accuracy={rule_c.accuracy_rate:.2f}, eff_conf={rule_c.effective_confidence:.4f}")

    # 模拟时间流逝 - 规则不应该因为时间衰减
    rule_a.created_at = time.time() - 86400 * 365  # 1年前
    rule_a.last_used = time.time() - 864400 * 180   # 180天前
    rule_a_fresh = rule_a.effective_confidence
    print(f"  规则 A (180天未用): eff_conf={rule_a_fresh:.4f} (应≈0.87，不因时间衰减)")

    # 4. Token 匹配 vs 子串匹配
    print("\n  --- Token 匹配 ---")
    test_q = "user authentication service"
    test_chunk_name = "AuthService"
    test_chunk_content = "def authenticate(username, password): ..."
    from graph_context.rules import _tokenize
    q_tokens = _tokenize(test_q)
    c_tokens = _tokenize(f"{test_chunk_name} {test_chunk_content}")
    match = rule_a.match_score(q_tokens, c_tokens)
    print(f"  查询: '{test_q}'")
    print(f"  Chunk: '{test_chunk_name}'")
    print(f"  匹配分数: {match:.3f}")

    # 5. 衰减和清理
    print("\n  --- 衰减和清理 ---")
    decay_disabled = store.apply_decay()
    pruned = store.prune_rules(min_hit_rate=0.2, min_uses=3)
    stats = store.stats()
    print(f"  衰减禁用: {decay_disabled}")
    print(f"  清理禁用: {pruned}")
    print(f"  统计: {json.dumps(stats, indent=2)}")

    os.remove(rules_path)
    return {
        "rules_added": 4,
        "conflicts_detected": len(conflicts),
        "auto_promoted": len(promoted),
        "decay_disabled": len(decay_disabled),
        "pruned": len(pruned),
        "stats": stats,
    }


# ══════════════════════════════════════════════════
#  实验 3: 项目隔离 + 模式聚合
# ══════════════════════════════════════════════════

def test_project_scopes(base_dir: str) -> dict:
    print("\n" + "=" * 60)
    print("实验 3: 项目隔离 + 跨项目模式聚合")
    print("=" * 60)

    project_a_dir = os.path.join(base_dir, "project_a")
    project_b_dir = os.path.join(base_dir, "project_b")
    generate_test_project(project_a_dir, "A")
    generate_test_project(project_b_dir, "B")

    scopes_path = os.path.join(base_dir, ".test_scopes.json")
    manager = ScopeManager(persist_path=scopes_path)

    manager.create_scope("project_a", project_a_dir, "strict", "电商后端")
    manager.create_scope("project_b", project_b_dir, "strict", "管理后台")

    config = dict(DEFAULT_CONFIG)
    engine_a = VibeCodingEngine(project_a_dir, config)
    engine_a.index_project()
    manager.bind_engine("project_a", engine_a)

    engine_b = VibeCodingEngine(project_b_dir, config)
    engine_b.index_project()
    manager.bind_engine("project_b", engine_b)

    # Strict 搜索
    print("\n  Strict 模式:")
    strict_results = manager.search_across_projects("user authentication", source_project="project_a", top_k=3)
    print(f"    项目 A 搜索: {len(strict_results)} 个模式")

    # 关联并切换到 shared
    manager.link_projects("project_a", "project_b", "bidirectional",
                         shared_patterns=["auth_pattern", "user_pattern"])
    manager.update_scope("project_a", isolation="shared")
    manager.update_scope("project_b", isolation="shared")

    # 聚合搜索
    print("\n  Shared 模式 + 聚合:")
    agg_results = manager.search_across_projects("user service", top_k=5, aggregate=True)
    print(f"    聚合结果: {len(agg_results)} 个模式")
    for r in agg_results[:3]:
        print(f"      {r.pattern_name} ({r.pattern_type})")
        print(f"        出现项目: {r.projects}")
        print(f"        总频次: {r.total_frequency}, 平均置信度: {r.avg_confidence:.4f}")
        print(f"        最佳项目: {r.best_project}")

    # 扁平搜索对比
    flat_results = manager.search_across_projects("user service", top_k=5, aggregate=False)
    print(f"\n    扁平结果: {len(flat_results)} 个 (vs 聚合: {len(agg_results)})")

    stats = manager.stats()
    print(f"\n  统计: {json.dumps(stats, indent=2)}")

    os.remove(scopes_path)
    return {
        "projects": 2,
        "strict_results": len(strict_results),
        "aggregated_results": len(agg_results),
        "flat_results": len(flat_results),
        "stats": stats,
    }


# ══════════════════════════════════════════════════
#  实验 4: V5 vs V6.1 对比
# ══════════════════════════════════════════════════

def compare_versions(project_root: str) -> dict:
    print("\n" + "=" * 60)
    print("实验 4: V5 vs V6.1 对比")
    print("=" * 60)

    config = dict(DEFAULT_CONFIG)
    engine = VibeCodingEngine(project_root, config)
    start = time.perf_counter()
    files, chunks = engine.index_project()
    index_ms = (time.perf_counter() - start) * 1000

    queries = [
        "user authentication login",
        "database connection ORM",
        "cache LRU strategy",
        "API route endpoint",
        "password hash verify",
    ]

    total_tokens = 0
    total_latency = 0
    details = []
    for q in queries:
        start = time.perf_counter()
        results = engine.retrieve(q, top_k=5)
        latency = (time.perf_counter() - start) * 1000
        tokens = engine._estimate_tokens("\n".join(c.content for _, c in results))
        total_tokens += tokens
        total_latency += latency
        details.append({"query": q, "tokens": tokens, "ms": round(latency, 2)})

    savings = engine.compare_strategies("user authentication")

    # MVCC 性能
    import copy
    start = time.perf_counter()
    for _ in range(100):
        engine.memory.snapshot()
    cow_ms = (time.perf_counter() - start) * 1000 / 100

    start = time.perf_counter()
    for _ in range(10):
        copy.deepcopy(engine.memory.chunks)
    dc_ms = (time.perf_counter() - start) * 1000 / 10

    print(f"\n  索引: {files}文件 {chunks}chunks, {index_ms:.1f}ms")
    print(f"  平均查询: {total_tokens/5:.0f}tokens, {total_latency/5:.2f}ms")
    print(f"  Token节省: {savings['savings']}")
    print(f"  COW快照: {cow_ms:.3f}ms vs deepcopy: {dc_ms:.1f}ms ({dc_ms/max(cow_ms,0.001):.0f}x加速)")
    print(f"\n  各查询:")
    for d in details:
        print(f"    '{d['query']}': {d['tokens']}tokens, {d['ms']}ms")

    return {
        "files": files, "chunks": chunks,
        "index_ms": round(index_ms, 1),
        "avg_tokens": round(total_tokens / 5),
        "avg_latency_ms": round(total_latency / 5, 2),
        "savings": savings["savings"],
        "cow_ms": round(cow_ms, 3),
        "deepcopy_ms": round(dc_ms, 1),
        "speedup": round(dc_ms / max(cow_ms, 0.001)),
    }


# ══════════════════════════════════════════════════
#  主入口
# ══════════════════════════════════════════════════

def main():
    print("╔══════════════════════════════════════════════════╗")
    print("║       V6.1 代码上下文引擎 — 实验测试            ║")
    print("╚══════════════════════════════════════════════════╝")

    base_dir = tempfile.mkdtemp(prefix="v61_experiment_")
    project_root = os.path.join(base_dir, "main_project")
    generate_test_project(project_root)

    results = {}
    try:
        results["mvcc"] = test_mvcc(project_root)
        results["rules"] = test_rules(project_root)
        results["scopes"] = test_project_scopes(base_dir)
        results["comparison"] = compare_versions(project_root)
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)

    print("\n" + "=" * 60)
    print("总结")
    print("=" * 60)
    r = results
    print(f"\n  MVCC COW:")
    print(f"    快照: {r['mvcc']['cow_ms']}ms (COW) vs {r['mvcc']['deepcopy_ms']}ms (deepcopy)")
    print(f"    加速: {r['mvcc']['speedup']}x")
    print(f"    查询: {r['mvcc']['query_ms']}ms")
    print(f"\n  规则系统:")
    print(f"    冲突检测: {r['rules']['conflicts_detected']} 个冲突")
    print(f"    观察期提升: {r['rules']['auto_promoted']} 条")
    print(f"    活跃规则: {r['rules']['stats']['active_rules']}")
    print(f"    观察中: {r['rules']['stats']['observing_rules']}")
    print(f"    平均准确率: {r['rules']['stats']['avg_accuracy_rate']:.2f}")
    print(f"\n  项目隔离:")
    print(f"    聚合结果: {r['scopes']['aggregated_results']} (vs 扁平: {r['scopes']['flat_results']})")
    print(f"\n  整体:")
    print(f"    文件: {r['comparison']['files']}, Chunks: {r['comparison']['chunks']}")
    print(f"    Token节省: {r['comparison']['savings']}")
    print(f"    平均延迟: {r['comparison']['avg_latency_ms']}ms")
    print("\n✅ 所有实验完成！")


if __name__ == "__main__":
    main()
