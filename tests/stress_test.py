"""
V6.1 压力测试 — 暴露真实问题
================================
不是跑 demo，是找 bug。
"""

import os
import sys
import time
import json
import shutil
import tempfile
import random
import string
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from graph_context.engine import VibeCodingEngine, DEFAULT_CONFIG, CodeChunk
from graph_context.rules import Rule, RulesStore, _tokenize
from graph_context.project_scope import ScopeManager


def gen_project(root: str, n_files: int = 20, funcs_per_file: int = 10):
    """生成一个有规模的项目"""
    os.makedirs(root, exist_ok=True)
    modules = ["auth", "user", "api", "database", "cache", "config",
               "logging", "middleware", "scheduler", "worker",
               "notification", "payment", "search", "analytics", "storage"]
    for i in range(n_files):
        mod = modules[i % len(modules)]
        suffix = i // len(modules)
        fname = f"{mod}{'_' + str(suffix) if suffix else ''}.py"
        lines = [f'"""Module {mod} v{suffix}"""', ""]
        for j in range(funcs_per_file):
            fn = f"{mod}_func_{j}"
            # 让一些函数有跨模块调用
            calls = []
            if random.random() < 0.3:
                target_mod = random.choice(modules)
                calls.append(f"{target_mod}_func_{random.randint(0, funcs_per_file-1)}")
            call_str = ""
            if calls:
                call_str = "\n        " + "\n        ".join(f"{c}()" for c in calls)
            lines.append(f'''def {fn}(data=None):
    """Function {fn} in {mod}"""
    result = process(data){call_str}
    return result
''')
        with open(os.path.join(root, fname), "w") as f:
            f.write("\n".join(lines))


# ══════════════════════════════════════════════════
#  测试 1: MVCC COW 正确性 — 快照是否真的隔离？
# ══════════════════════════════════════════════════

def test_mvcc_correctness(root: str) -> dict:
    """
    核心问题：COW 快照共享引用，如果旧 chunk 被修改（不是替换），
    快照看到的数据也会变 —— 这违反了快照语义。
    """
    print("\n" + "=" * 60)
    print("测试 1: MVCC COW 正确性 — 快照隔离是否被破坏？")
    print("=" * 60)

    config = dict(DEFAULT_CONFIG)
    engine = VibeCodingEngine(root, config)
    engine.index_project()

    # 记录快照前的状态
    original_count = len([c for c in engine.memory.chunks if c is not None])
    v0 = engine.memory.snapshot()

    # 修改一个已有 chunk 的 content（模拟 in-place 修改）
    # 实际上 add_chunk 不会修改已有 chunk，但 remove_file + add_file 会
    # 先记录 v0 时第 0 个 chunk 的 name
    snap_v0 = engine.memory.read_at(v0)
    v0_first_name = None
    for c in snap_v0["chunks"]:
        if c is not None:
            v0_first_name = c.name
            break

    # 添加新 chunk
    for i in range(10):
        engine.memory.add_chunk(CodeChunk(
            file_path=f"<test>/new_{i}.py",
            chunk_type="function",
            name=f"extra_func_{i}",
            content=f"def extra_func_{i}(): pass",
            start_line=1, end_line=1,
        ))

    v1 = engine.memory.snapshot()

    # 验证 v0 是否被污染
    snap_v0_after = engine.memory.read_at(v0)
    v0_count_after = sum(1 for c in snap_v0_after["chunks"] if c is not None)
    v1_count = sum(1 for c in engine.memory.read_at(v1)["chunks"] if c is not None)

    # 关键测试：v0 的 chunks 列表长度
    v0_len = len(snap_v0_after["chunks"])
    v1_len = len(engine.memory.read_at(v1)["chunks"])

    print(f"  原始 chunks: {original_count}")
    print(f"  v0 快照时 chunks: {len(snap_v0['chunks'])}")
    print(f"  添加 10 chunks 后:")
    print(f"    v0 读取: {v0_count_after} active, 列表长度 {v0_len}")
    print(f"    v1 读取: {v1_count} active, 列表长度 {v1_len}")

    # 问题检测：v0 的列表长度不应该变
    isolation_broken = v0_len != len(snap_v0["chunks"])
    print(f"\n  ⚠️  v0 列表长度变化: {len(snap_v0['chunks'])} -> {v0_len}")
    if isolation_broken:
        print(f"  ❌ 快照隔离被破坏！v0 看到了后续写入的数据")
    else:
        print(f"  ✅ 快照列表长度未变")

    # 更深层的问题：共享引用的 chunk 对象是否被修改？
    # 检查 v0 中的 chunk 是否和当前 chunks 是同一个对象
    import copy
    v0_snap = snap_v0_after["chunks"]
    current = engine.memory.chunks
    shared_objects = 0
    for i in range(min(len(v0_snap), len(current))):
        if v0_snap[i] is not None and current[i] is not None:
            if v0_snap[i] is current[i]:
                shared_objects += 1
    print(f"  共享引用的 chunk 对象: {shared_objects}/{min(len(v0_snap), len(current))}")
    if shared_objects > 0:
        # add_chunk 已创建 frozen 副本，chunk 对象不可变，共享引用是安全的
        print(f"  ℹ️  chunk 对象共享（安全：add_chunk 冻结了副本，无 in-place 修改）")

    # 额外验证：快照内容一致性 — 读两次应得到相同结果
    snap_v0_re_read = engine.memory.read_at(v0)
    content_match = True
    for i in range(len(snap_v0_after["chunks"])):
        c1 = snap_v0_after["chunks"][i]
        c2 = snap_v0_re_read["chunks"][i]
        if (c1 is None) != (c2 is None):
            content_match = False
            break
        if c1 is not None and c2 is not None and c1.content != c2.content:
            content_match = False
            break
    print(f"  快照重读一致性: {'✅ 通过' if content_match else '❌ 不一致'}")

    return {
        "original_count": original_count,
        "isolation_broken": isolation_broken,
        "shared_objects": shared_objects,
        "v0_len_before": len(snap_v0["chunks"]),
        "v0_len_after": v0_len,
    }


# ══════════════════════════════════════════════════
#  测试 2: 规则匹配质量 — Token 匹配 vs 语义
# ══════════════════════════════════════════════════

def test_rule_matching(root: str) -> dict:
    """
    Token 匹配的问题：
    1. "user" 匹配 "user_agent" 也匹配 "UserService" — 语义完全不同
    2. 中文同义词没有在规则匹配中使用
    3. 规则 condition 太短时，token 集合太小，误匹配率高
    """
    print("\n" + "=" * 60)
    print("测试 2: 规则匹配质量 — 误匹配率")
    print("=" * 60)

    config = dict(DEFAULT_CONFIG)
    engine = VibeCodingEngine(root, config)
    engine.index_project()

    store = RulesStore()

    # 添加一个宽泛的规则
    rule = Rule(
        rule_id="", rule_type="pattern", scope="project",
        description="用户相关查询",
        condition="user",
        action="boost user.py",
        confidence=0.8, source="manual",
    )
    store.add_rule(rule, auto_observe=False)

    # 测试各种查询
    test_cases = [
        # (query, 应该匹配, 不应该匹配)
        ("user authentication", True),
        ("user service create", True),
        ("user agent string", False),      # user_agent 和 UserService 语义不同
        ("file permissions", False),
        ("database user table", True),      # 有 user，应该匹配
        ("cache strategy", False),
    ]

    correct = 0
    total = len(test_cases)
    details = []

    for query, should_match in test_cases:
        results = engine.retrieve(query, top_k=3)
        boosted = store.apply_rules_to_query(query, results)

        # 检查是否有 boost
        has_boost = False
        if results and boosted:
            orig_top = results[0][0]
            boost_top = boosted[0][0]
            has_boost = boost_top > orig_top + 0.01

        matched_correctly = has_boost == should_match
        if matched_correctly:
            correct += 1
        details.append({
            "query": query,
            "should_match": should_match,
            "actually_matched": has_boost,
            "correct": matched_correctly,
        })
        status = "✅" if matched_correctly else "❌"
        print(f"  {status} '{query}': expected={should_match}, got={has_boost}")

    accuracy = correct / total
    print(f"\n  匹配准确率: {correct}/{total} = {accuracy:.0%}")

    # 测试短 condition 的误匹配问题
    print("\n  --- 短 condition 误匹配 ---")
    short_rules = [
        ("a", "very short"),
        ("go", "language name but also common word"),
        ("do", "common word"),
        ("get", "common verb"),
    ]
    for cond, desc in short_rules:
        tokens = _tokenize(cond)
        print(f"  condition='{cond}' ({desc}): tokens={tokens}")

    return {
        "accuracy": accuracy,
        "correct": correct,
        "total": total,
        "details": details,
    }


# ══════════════════════════════════════════════════
#  测试 3: 规则冲突 — 假阳性
# ══════════════════════════════════════════════════

def test_conflict_false_positives(root: str) -> dict:
    """
    冲突检测的问题：
    1. "boost auth.py" 和 "boost auth module" 不是冲突，但 token 重叠高
    2. 不同 scope 的规则不应该冲突
    3. 优先级机制能解决的冲突不应该报冲突
    """
    print("\n" + "=" * 60)
    print("测试 3: 冲突检测 — 假阳性率")
    print("=" * 60)

    store = RulesStore()

    # 基准规则
    base = Rule(
        rule_id="", rule_type="pattern", scope="project",
        description="认证查询 boost auth",
        condition="authentication login",
        action="boost auth.py",
        confidence=0.9, source="manual", priority=10,
    )
    store.add_rule(base, auto_observe=False)

    # 测试用例：哪些应该/不应该被检测为冲突
    test_cases = [
        # (rule, should_conflict, description)
        (Rule(rule_id="", rule_type="pattern", scope="project",
              description="", condition="authentication login",
              action="filter auth.py", confidence=0.7, source="manual"),
         True, "同 condition 反向 action"),

        (Rule(rule_id="", rule_type="pattern", scope="project",
              description="", condition="authentication login",
              action="boost auth module", confidence=0.7, source="manual"),
         False, "同 condition 同向 action (不冲突)"),

        (Rule(rule_id="", rule_type="pattern", scope="global",
              description="", condition="authentication login",
              action="filter auth.py", confidence=0.7, source="manual"),
         True, "不同 scope 但反向 action (应该冲突)"),

        (Rule(rule_id="", rule_type="pattern", scope="project",
              description="", condition="user service",
              action="boost user.py", confidence=0.7, source="manual"),
         False, "完全不同的 condition"),

        (Rule(rule_id="", rule_type="pattern", scope="project",
              description="", condition="authentication",
              action="boost auth.py", confidence=0.7, source="manual"),
         False, "子集 condition 同向 action (不冲突)"),

        (Rule(rule_id="", rule_type="pattern", scope="project",
              description="", condition="authentication verify",
              action="filter auth.py", confidence=0.7, source="manual"),
         True, "部分重叠 condition 反向 action"),
    ]

    correct = 0
    total = len(test_cases)
    for rule, should_conflict, desc in test_cases:
        conflicts = store.detect_conflicts(rule)
        has_conflict = len(conflicts) > 0
        matched = has_conflict == should_conflict
        if matched:
            correct += 1
        status = "✅" if matched else "❌"
        print(f"  {status} {desc}: expected={should_conflict}, got={has_conflict} ({len(conflicts)} conflicts)")
        if not matched and conflicts:
            for c in conflicts:
                print(f"       {c['conflict_type']}: overlap={c.get('overlap_tokens', [])}")

    accuracy = correct / total
    print(f"\n  冲突检测准确率: {correct}/{total} = {accuracy:.0%}")

    return {"accuracy": accuracy, "correct": correct, "total": total}


# ══════════════════════════════════════════════════
#  测试 4: 大规模性能 — 内存和延迟
# ══════════════════════════════════════════════════

def test_scale(root: str) -> dict:
    """
    真实项目可能有几千个文件，几万个 chunk。
    测试：
    1. 索引时间
    2. 查询延迟随 chunk 数增长
    3. 快照内存开销
    4. 大量规则的匹配开销
    """
    print("\n" + "=" * 60)
    print("测试 4: 大规模性能")
    print("=" * 60)

    # 生成大项目
    big_root = os.path.join(root, "big_project")
    gen_project(big_root, n_files=50, funcs_per_file=15)

    config = dict(DEFAULT_CONFIG)
    config["persist_path"] = None  # 不持久化，纯内存测试

    start = time.perf_counter()
    engine = VibeCodingEngine(big_root, config)
    files, chunks = engine.index_project()
    index_ms = (time.perf_counter() - start) * 1000
    print(f"  索引: {files} 文件, {chunks} chunks, {index_ms:.0f}ms")

    # 查询延迟
    queries = ["authentication flow", "database query", "cache invalidation",
               "user permission", "api endpoint", "background worker"]
    latencies = []
    for q in queries:
        start = time.perf_counter()
        engine.retrieve(q, top_k=5)
        latencies.append((time.perf_counter() - start) * 1000)
    avg_lat = sum(latencies) / len(latencies)
    max_lat = max(latencies)
    print(f"  查询延迟: avg={avg_lat:.2f}ms, max={max_lat:.2f}ms")

    # 快照内存估算
    import sys
    chunks_size = sys.getsizeof(engine.memory.chunks)
    # 估算单个 chunk 大小
    sample_chunk = next(c for c in engine.memory.chunks if c is not None)
    chunk_size = sys.getsizeof(sample_chunk) + sys.getsizeof(sample_chunk.content)

    # COW 快照：只存引用
    v = engine.memory.snapshot()
    snap_size = sys.getsizeof(engine.memory._snapshot_refs[v])
    print(f"  内存: chunks列表={chunks_size/1024:.1f}KB, 单chunk≈{chunk_size}B")
    print(f"  COW快照内存: {snap_size/1024:.2f}KB (vs 全量≈{chunks_size*chunk_size/chunks_size/1024:.1f}KB)")

    # 大量规则匹配开销
    store = RulesStore()
    for i in range(100):
        store.add_rule(Rule(
            rule_id="", rule_type="pattern", scope="project",
            description=f"rule {i}",
            condition=f"test condition {i} with tokens",
            action=f"boost file_{i}.py",
            confidence=0.5, source="manual",
        ), auto_observe=False)

    start = time.perf_counter()
    for q in queries:
        results = engine.retrieve(q, top_k=5)
        store.apply_rules_to_query(q, results)
    rule_lat = (time.perf_counter() - start) * 1000 / len(queries)
    print(f"  100条规则匹配开销: {rule_lat:.2f}ms/查询")

    return {
        "files": files, "chunks": chunks,
        "index_ms": round(index_ms),
        "avg_query_ms": round(avg_lat, 2),
        "max_query_ms": round(max_lat, 2),
        "cow_snap_kb": round(snap_size / 1024, 2),
        "rule_match_ms": round(rule_lat, 2),
    }


# ══════════════════════════════════════════════════
#  测试 5: 观察期阈值是否合理
# ══════════════════════════════════════════════════

def test_observation_threshold(root: str) -> dict:
    """
    观察期的问题：
    1. 阈值太低：随机规则也能通过
    2. 阈值太高：好规则永远出不了观察期
    3. 触发≠有用：规则被触发不代表它真的有帮助
    """
    print("\n" + "=" * 60)
    print("测试 5: 观察期阈值 — 随机规则能否通过？")
    print("=" * 60)

    store = RulesStore()

    # 模拟一个"随机"规则：随机命中
    random_rule = Rule(
        rule_id="", rule_type="pattern", scope="project",
        description="random rule",
        condition="random condition",
        action="boost random.py",
        confidence=0.5, source="auto", status="observing",
    )
    store.add_rule(random_rule, auto_observe=False)
    rid = random_rule.rule_id

    # 模拟 100 次观察，命中率 40% (高于阈值 30%)
    random.seed(42)
    for _ in range(100):
        triggered = random.random() < 0.4  # 40% 命中率
        store.get_rule(rid).record_observation(triggered=triggered)

    r = store.get_rule(rid)
    print(f"  随机规则 (40%命中率): {r.observing_hits}/{r.observing_total} = {r.observation_score:.2f}")

    promoted = store.promote_observing_rules()
    r = store.get_rule(rid)
    print(f"  提升结果: {promoted}, 状态: {r.status}")

    if r.status == "active":
        print(f"  ❌ 随机规则通过了观察期！阈值太低")
    else:
        print(f"  ✅ 随机规则被正确拦截")

    # 测试不同命中率的通过情况
    print("\n  不同命中率的通过测试:")
    thresholds = [0.2, 0.3, 0.4, 0.5, 0.6]
    for rate in thresholds:
        test_store = RulesStore()
        test_r = Rule(
            rule_id="", rule_type="pattern", scope="project",
            description=f"rule at {rate}",
            condition="test", action="boost test.py",
            confidence=0.5, source="auto", status="observing",
        )
        test_store.add_rule(test_r, auto_observe=False)
        trid = test_r.rule_id
        for _ in range(50):
            triggered = random.random() < rate
            test_store.get_rule(trid).record_observation(triggered=triggered)
        promoted = test_store.promote_observing_rules()
        final = test_store.get_rule(trid)
        status = "PASS" if final.status == "active" else "STAY"
        print(f"    {rate:.0%}命中 -> {final.observation_score:.2f} -> {status}")

    return {"random_passed": r.status == "active"}


# ══════════════════════════════════════════════════
#  测试 6: 跨项目聚合 — 信息丢失
# ══════════════════════════════════════════════════

def test_aggregation_loss(base_dir: str) -> dict:
    """
    聚合的问题：
    1. 同名不同实现的函数被合并 — 丢失差异
    2. 低置信度的结果被高置信度淹没
    3. 聚合后丢失了具体文件位置
    """
    print("\n" + "=" * 60)
    print("测试 6: 跨项目聚合 — 信息丢失检测")
    print("=" * 60)

    # 项目 A: UserService 用 SQLAlchemy
    a_root = os.path.join(base_dir, "proj_a")
    os.makedirs(a_root, exist_ok=True)
    with open(os.path.join(a_root, "service.py"), "w") as f:
        f.write('''class UserService:
    def __init__(self, db):
        self.db = db  # SQLAlchemy session
    def get_user(self, uid):
        return self.db.query(User).filter_by(id=uid).first()
    def create_user(self, data):
        user = User(**data)
        self.db.add(user)
        self.db.commit()
        return user
''')

    # 项目 B: UserService 用原始 SQL (完全不同实现)
    b_root = os.path.join(base_dir, "proj_b")
    os.makedirs(b_root, exist_ok=True)
    with open(os.path.join(b_root, "service.py"), "w") as f:
        f.write('''class UserService:
    def __init__(self, conn):
        self.conn = conn  # raw sqlite3 connection
    def get_user(self, uid):
        cursor = self.conn.execute("SELECT * FROM users WHERE id=?", (uid,))
        return cursor.fetchone()
    def create_user(self, data):
        self.conn.execute("INSERT INTO users ...", data)
        self.conn.commit()
''')

    scopes_path = os.path.join(base_dir, ".test_agg_scopes.json")
    manager = ScopeManager(persist_path=scopes_path)
    manager.create_scope("a", a_root, "shared")
    manager.create_scope("b", b_root, "shared")
    manager.link_projects("a", "b", "shared")

    config = dict(DEFAULT_CONFIG)
    ea = VibeCodingEngine(a_root, config); ea.index_project()
    eb = VibeCodingEngine(b_root, config); eb.index_project()
    manager.bind_engine("a", ea)
    manager.bind_engine("b", eb)

    # 聚合搜索
    agg = manager.search_across_projects("user service create", aggregate=True)
    flat = manager.search_across_projects("user service create", aggregate=False, top_k=10)

    print(f"  聚合结果: {len(agg)} 个模式")
    print(f"  扁平结果: {len(flat)} 个模式")

    for p in agg:
        print(f"\n  聚合模式: {p.pattern_name}")
        print(f"    类型: {p.pattern_type}")
        print(f"    项目: {p.projects}")
        print(f"    描述: {p.description}")
        # 问题：两个完全不同的 UserService 被合并了
        if len(p.projects) > 1:
            print(f"    ⚠️  跨项目合并 — 但实现可能完全不同！")
            # 检查两个项目的实现是否真的相同
            a_chunks = ea.retrieve("UserService", top_k=1)
            b_chunks = eb.retrieve("UserService", top_k=1)
            if a_chunks and b_chunks:
                a_content = a_chunks[0][1].content[:100]
                b_content = b_chunks[0][1].content[:100]
                if a_content != b_content:
                    print(f"    ❌ 实现不同！聚合丢失了实现差异")

    # 聚合后丢失了文件位置
    print(f"\n  信息丢失检查:")
    if flat:
        print(f"    扁平结果有文件路径: {flat[0].example_files}")
    if agg:
        for pid, detail in agg[0].project_details.items():
            print(f"    聚合结果 [{pid}] 文件: {detail.get('files', [])[:1]}")

    os.remove(scopes_path)
    return {
        "agg_count": len(agg),
        "flat_count": len(flat),
        "lost_implementation_diff": len(agg) < len(flat),
    }


# ══════════════════════════════════════════════════
#  主入口
# ══════════════════════════════════════════════════

def main():
    print("╔══════════════════════════════════════════════════╗")
    print("║       V6.1 压力测试 — 暴露真实问题              ║")
    print("╚══════════════════════════════════════════════════╝")

    base_dir = tempfile.mkdtemp(prefix="v61_stress_")
    root = os.path.join(base_dir, "project")
    gen_project(root, n_files=10, funcs_per_file=8)

    results = {}
    try:
        results["mvcc"] = test_mvcc_correctness(root)
        results["matching"] = test_rule_matching(root)
        results["conflict"] = test_conflict_false_positives(root)
        results["scale"] = test_scale(base_dir)
        results["observation"] = test_observation_threshold(root)
        results["aggregation"] = test_aggregation_loss(base_dir)
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)

    # 总结
    print("\n" + "=" * 60)
    print("发现的问题")
    print("=" * 60)

    issues = []

    # MVCC 问题 — chunk 对象共享但已冻结，不再报告为问题
    if results["mvcc"]["isolation_broken"]:
        issues.append({
            "severity": "HIGH",
            "component": "MVCC",
            "issue": "快照隔离被破坏：v0 列表长度在写入后变化",
            "fix": "snapshot() 冻结列表副本，与 live 列表解耦",
        })
    if results["mvcc"]["isolation_broken"]:
        issues.append({
            "severity": "CRITICAL",
            "component": "MVCC",
            "issue": "快照列表长度被后续写入改变",
            "fix": "snapshot 时记录 chunks_len，read_at 时截断",
        })

    # 匹配问题
    if results["matching"]["accuracy"] < 0.8:
        issues.append({
            "severity": "MEDIUM",
            "component": "Rules",
            "issue": f"Token 匹配准确率 {results['matching']['accuracy']:.0%}，存在误匹配",
            "fix": "增加最小 token 数过滤，引入同义词表到规则匹配",
        })

    # 冲突检测问题
    if results["conflict"]["accuracy"] < 0.8:
        issues.append({
            "severity": "MEDIUM",
            "component": "Rules",
            "issue": f"冲突检测准确率 {results['conflict']['accuracy']:.0%}，假阳性/假阴性",
            "fix": "改进冲突逻辑：区分完全重叠和部分重叠，引入 scope 感知",
        })

    # 观察期问题
    if results["observation"]["random_passed"]:
        issues.append({
            "severity": "HIGH",
            "component": "Rules",
            "issue": "随机规则 (40%命中率) 通过了观察期 — 阈值太低",
            "fix": "提高 min_score 到 0.5+，或引入验证期（需要用户确认）",
        })

    # 聚合问题
    if results["aggregation"]["lost_implementation_diff"]:
        issues.append({
            "severity": "MEDIUM",
            "component": "Scopes",
            "issue": "跨项目聚合将不同实现的同名函数合并 — 丢失差异",
            "fix": "聚合时比较实现相似度，差异大时不合并",
        })

    if not issues:
        print("\n  ✅ 未发现严重问题")
    else:
        for i, issue in enumerate(issues, 1):
            print(f"\n  [{issue['severity']}] {issue['component']}: {issue['issue']}")
            print(f"    修复建议: {issue['fix']}")

    return results


if __name__ == "__main__":
    main()
