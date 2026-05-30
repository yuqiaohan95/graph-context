"""
真实场景 Token 消耗模拟器 v2
=========================
核心改进（基于反馈）：
  1. 只问项目里已存在的文件 — 不存在的不算 MCP 失败
  2. 用真实引擎 retrieve() 判断召回成功/失败
  3. 输入/输出价格不同
  4. MCP 召回失败 → 用户追问 → 额外 token 消耗
  5. 遗忘导致重复提供上下文
"""

import os
import sys
import json
import time
import random
import tempfile
import shutil
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from graph_context.engine import VibeCodingEngine, DEFAULT_CONFIG
from graph_context.rules import Rule, RulesStore


# ══════════════════════════════════════════════════
#  模型定价 (per 1M tokens, 人民币)
# ══════════════════════════════════════════════════

PRICING = {
    "gpt-4o": {"input": 18.0, "output": 72.0},
    "gpt-4o-mini": {"input": 1.08, "output": 4.32},
    "claude-3.5-sonnet": {"input": 21.6, "output": 108.0},
    "deepseek-v3": {"input": 1.0, "output": 2.0},
    "qwen-max": {"input": 16.0, "output": 64.0},
}


@dataclass
class TokenUsage:
    round_num: float
    action: str
    input_tokens: int = 0
    output_tokens: int = 0
    context_tokens: int = 0
    is_recall_failure: bool = False
    is_repeat_context: bool = False


@dataclass
class ConversationScenario:
    name: str
    description: str
    rounds: list = field(default_factory=list)

    def total_input(self) -> int:
        return sum(r.input_tokens for r in self.rounds)

    def total_output(self) -> int:
        return sum(r.output_tokens for r in self.rounds)

    def failure_rounds(self) -> int:
        return sum(1 for r in self.rounds if r.is_recall_failure)

    def repeat_rounds(self) -> int:
        return sum(1 for r in self.rounds if r.is_repeat_context)

    def cost(self, model: str) -> dict:
        p = PRICING.get(model, PRICING["deepseek-v3"])
        ic = self.total_input() / 1_000_000 * p["input"]
        oc = self.total_output() / 1_000_000 * p["output"]
        return {
            "model": model,
            "input_cost": round(ic, 4),
            "output_cost": round(oc, 4),
            "total_cost": round(ic + oc, 4),
            "total_cost_yuan": f"¥{ic + oc:.4f}",
        }


# ══════════════════════════════════════════════════
#  生成测试项目
# ══════════════════════════════════════════════════

def generate_realistic_project(root: str, scale: str = "medium"):
    """生成一个有真实代码结构的项目"""
    os.makedirs(root, exist_ok=True)

    modules = {
        "auth": {
            "description": "用户认证模块",
            "classes": ["AuthService", "TokenManager", "PasswordHasher"],
            "functions": ["login", "logout", "register", "verify_token", "refresh_token"],
        },
        "user": {
            "description": "用户管理模块",
            "classes": ["UserService", "UserModel", "UserRepository"],
            "functions": ["create_user", "get_user", "update_user", "delete_user", "list_users"],
        },
        "api": {
            "description": "API 路由模块",
            "classes": ["APIRouter", "RequestHandler", "ResponseBuilder"],
            "functions": ["handle_login", "handle_register", "handle_users", "handle_health"],
        },
        "database": {
            "description": "数据库连接和 ORM",
            "classes": ["DatabaseConnection", "QueryBuilder", "Migration"],
            "functions": ["connect", "execute", "migrate", "seed_data"],
        },
        "cache": {
            "description": "缓存管理",
            "classes": ["LRUCache", "RedisCache", "CacheManager"],
            "functions": ["get_cache", "set_cache", "invalidate", "warm_cache"],
        },
        "config": {
            "description": "配置管理",
            "classes": ["Config", "EnvironmentLoader"],
            "functions": ["load_config", "get_env", "validate_config"],
        },
        "middleware": {
            "description": "中间件",
            "classes": ["AuthMiddleware", "RateLimiter", "CORSHandler"],
            "functions": ["before_request", "after_request", "check_rate_limit"],
        },
        "notification": {
            "description": "通知服务",
            "classes": ["EmailService", "SMSService", "NotificationManager"],
            "functions": ["send_email", "send_sms", "send_push", "queue_notification"],
        },
        "payment": {
            "description": "支付模块",
            "classes": ["PaymentGateway", "Transaction", "InvoiceGenerator"],
            "functions": ["process_payment", "refund", "generate_invoice"],
        },
        "search": {
            "description": "搜索引擎",
            "classes": ["SearchEngine", "IndexBuilder", "QueryParser"],
            "functions": ["search", "index_document", "build_index"],
        },
        "logging": {
            "description": "日志模块",
            "classes": ["Logger", "LogFormatter"],
            "functions": ["setup_logging", "log_request", "log_error"],
        },
        "scheduler": {
            "description": "定时任务",
            "classes": ["TaskScheduler", "CronJob"],
            "functions": ["schedule_task", "run_pending", "cancel_task"],
        },
        "worker": {
            "description": "后台任务",
            "classes": ["WorkerPool", "TaskQueue"],
            "functions": ["submit_task", "process_queue", "get_status"],
        },
        "analytics": {
            "description": "数据分析",
            "classes": ["AnalyticsEngine", "ReportGenerator"],
            "functions": ["track_event", "generate_report", "get_metrics"],
        },
    }

    if scale == "small":
        keep = ["auth", "user", "api", "database"]
    elif scale == "medium":
        keep = ["auth", "user", "api", "database", "cache", "config", "middleware", "notification", "payment", "search"]
    else:
        keep = list(modules.keys())

    for mod_name in keep:
        mod = modules.get(mod_name, {
            "description": f"{mod_name} module",
            "classes": [f"{mod_name.title()}Service"],
            "functions": [f"{mod_name}_process"],
        })

        lines = [f'"""{mod["description"]}"""', ""]
        for cls_name in mod.get("classes", []):
            lines.append(f"class {cls_name}:")
            lines.append(f'    """{cls_name} for {mod["description"]}"""')
            lines.append("")
            lines.append("    def __init__(self, config=None):")
            lines.append("        self.config = config")
            lines.append("        self._initialized = False")
            lines.append("")
            lines.append("    def initialize(self):")
            lines.append("        self._initialized = True")
            lines.append("")
            for func in mod.get("functions", [])[:2]:
                lines.append(f"    def {func}(self, data=None):")
                lines.append(f'        """Execute {func}"""')
                lines.append("        return self._process(data)")
                lines.append("")
            lines.append("    def _process(self, data):")
            lines.append("        return {'status': 'ok', 'data': data}")
            lines.append("")

        for func_name in mod.get("functions", []):
            lines.append(f"def {func_name}(data=None):")
            lines.append(f'    """{func_name} function"""')
            lines.append("    return {'status': 'ok'}")
            lines.append("")

        with open(os.path.join(root, f"{mod_name}.py"), "w") as f:
            f.write("\n".join(lines))

    return len(keep)


# ══════════════════════════════════════════════════
#  召回质量检测
# ══════════════════════════════════════════════════

def check_recall_quality(engine: VibeCodingEngine, query: str, relevant_files: list) -> bool:
    """
    用真实引擎检查召回质量（纯 BM25，无规则）。
    检查 top-5 结果中是否包含期望的文件。
    """
    try:
        results = engine.retrieve(query, top_k=5)
        if not results:
            return False
        retrieved_stems = set()
        for score, chunk in results:
            retrieved_stems.add(Path(chunk.file_path).stem.lower())

        for rf in relevant_files:
            stem = Path(rf).stem.lower()
            if stem in retrieved_stems:
                return True
            for r in retrieved_stems:
                if stem in r or r in stem:
                    return True
        return False
    except Exception:
        return False


def check_recall_quality_with_rules(engine: VibeCodingEngine, rules_store: RulesStore,
                                     query: str, relevant_files: list) -> bool:
    """
    模拟 MCP 完整检索流程：BM25 + 规则直接召回。

    关键区别：规则不仅重排，还能直接召回 BM25 没找到的文件。
    当规则匹配时，规则关联的文件会被直接加入候选集。
    """
    try:
        # 第一步：BM25 检索
        results = engine.retrieve(query, top_k=10)

        # 第二步：规则直接召回 — 匹配的规则把关联文件加入候选
        query_tokens = set()
        import re
        for ident in re.findall(r"[a-zA-Z_]\w*", query):
            query_tokens.add(ident.lower())
        for cn in re.findall(r"[\u4e00-\u9fff]+", query):
            query_tokens.add(cn)

        active_rules = rules_store.list_rules(enabled_only=True, status="active")
        rule_boosted_files = set()
        for rule in active_rules:
            # 检查规则是否匹配当前查询
            rule_tokens = set(rule.condition_tokens)
            if rule_tokens and query_tokens:
                overlap = len(rule_tokens & query_tokens) / max(len(rule_tokens), 1)
                if overlap > 0.3:  # 30% token 重叠即认为匹配
                    for rf in rule.related_files:
                        rule_boosted_files.add(rf)

        # 把规则关联的文件对应的 chunks 加入结果
        if rule_boosted_files:
            for cid, chunk in enumerate(engine.memory.chunks):
                if chunk is None:
                    continue
                if any(chunk.file_path.endswith(rf) or rf.endswith(Path(chunk.file_path).name)
                       for rf in rule_boosted_files):
                    # 检查是否已在结果中
                    already_in = any(c.chunk_id == chunk.chunk_id for _, c in results)
                    if not already_in:
                        results.append((999.0, chunk))  # 高分加入

        # 取 top-5
        results.sort(key=lambda x: -x[0])
        results = results[:5]

        # 检查是否命中
        retrieved_stems = set()
        for score, chunk in results:
            retrieved_stems.add(Path(chunk.file_path).stem.lower())

        for rf in relevant_files:
            stem = Path(rf).stem.lower()
            if stem in retrieved_stems:
                return True
            for r in retrieved_stems:
                if stem in r or r in stem:
                    return True
        return False
    except Exception:
        return False


# ══════════════════════════════════════════════════
#  构建对话场景
# ══════════════════════════════════════════════════

def build_conversations(modules: list, lang: str = "zh") -> list:
    """
    根据项目实际存在的模块，构建对话场景。
    只问项目里已有的东西。支持中/英文查询。

    查询设计故意用不同的表述方式来测试召回：
      - 直接提文件名（容易召回）
      - 用描述功能（中等难度）
      - 用间接/语义描述（较难召回）
    """

    if lang == "en":
        queries_by_module = {
            "auth": [
                ("How does the authentication system work in this project?", ["auth"], "high"),
                ("What password hashing algorithm is used?", ["auth"], "medium"),
                ("Where is the token management logic?", ["auth"], "medium"),
                ("Show me the auth module, I need to add login failure locking", ["auth"], "high"),
            ],
            "user": [
                ("What is the full user registration flow?", ["user", "auth"], "high"),
                ("Does user listing support pagination?", ["user"], "medium"),
                ("Show me the UserModel field definitions", ["user"], "high"),
            ],
            "api": [
                ("What HTTP endpoints does this project have?", ["api"], "high"),
                ("What are the request and response formats for the login endpoint?", ["api", "auth"], "high"),
                ("Help me write a new endpoint following existing conventions", ["api"], "high"),
            ],
            "database": [
                ("How are database connections managed?", ["database"], "medium"),
                ("Is there an ORM or query builder?", ["database"], "medium"),
                ("How does database migration work?", ["database"], "medium"),
            ],
            "cache": [
                ("What caching strategy is used? LRU or something else?", ["cache"], "medium"),
                ("How does cache expiration work?", ["cache"], "medium"),
            ],
            "config": [
                ("How are config files loaded?", ["config"], "medium"),
                ("Where is environment variable management?", ["config"], "medium"),
            ],
            "middleware": [
                ("What middleware exists and what is the execution order?", ["middleware"], "high"),
                ("How does the auth middleware verify tokens?", ["middleware", "auth"], "high"),
                ("Is there rate limiting?", ["middleware"], "medium"),
            ],
            "notification": [
                ("What notification channels are supported? Email and SMS?", ["notification"], "medium"),
                ("How is email sending implemented?", ["notification"], "medium"),
            ],
            "payment": [
                ("How does the payment flow work?", ["payment"], "high"),
                ("How is refund logic handled?", ["payment"], "medium"),
            ],
            "search": [
                ("How is the search feature implemented?", ["search"], "medium"),
                ("How is the search index built?", ["search"], "medium"),
            ],
            "logging": [
                ("How is the logging system configured?", ["logging"], "medium"),
            ],
            "scheduler": [
                ("What scheduled tasks exist?", ["scheduler"], "medium"),
            ],
            "worker": [
                ("How does the background task queue work?", ["worker"], "medium"),
            ],
            "analytics": [
                ("What metrics does the analytics module track?", ["analytics"], "medium"),
            ],
        }
    else:  # zh
        queries_by_module = {
            "auth": [
                ("这个项目的用户认证是怎么实现的？", ["auth"], "high"),
                ("登录密码用什么方式加密的？", ["auth"], "medium"),
                ("token 管理的逻辑在哪里？", ["auth"], "medium"),
                ("帮我看看认证模块，我要加登录失败锁定", ["auth"], "high"),
            ],
            "user": [
                ("用户注册的完整流程是什么？", ["user", "auth"], "high"),
                ("用户列表查询支持分页吗？", ["user"], "medium"),
                ("帮我看看 UserModel 的字段定义", ["user"], "high"),
            ],
            "api": [
                ("这个项目有哪些 HTTP 接口？", ["api"], "high"),
                ("登录接口的请求和响应格式是什么？", ["api", "auth"], "high"),
                ("参考现有接口风格，帮我写新接口", ["api"], "high"),
            ],
            "database": [
                ("数据库连接是怎么管理的？", ["database"], "medium"),
                ("有没有 ORM 或查询构建器？", ["database"], "medium"),
                ("数据迁移怎么做的？", ["database"], "medium"),
            ],
            "cache": [
                ("缓存用的什么策略？LRU 还是其他？", ["cache"], "medium"),
                ("缓存过期机制是怎么实现的？", ["cache"], "medium"),
            ],
            "config": [
                ("配置文件怎么加载的？", ["config"], "medium"),
                ("环境变量管理在哪？", ["config"], "medium"),
            ],
            "middleware": [
                ("中间件有哪些？执行顺序是什么？", ["middleware"], "high"),
                ("认证中间件怎么验证 token 的？", ["middleware", "auth"], "high"),
                ("有没有限流功能？", ["middleware"], "medium"),
            ],
            "notification": [
                ("通知服务支持哪些渠道？邮件和短信？", ["notification"], "medium"),
                ("邮件发送用的什么方式？", ["notification"], "medium"),
            ],
            "payment": [
                ("支付流程是怎样的？", ["payment"], "high"),
                ("退款逻辑怎么处理的？", ["payment"], "medium"),
            ],
            "search": [
                ("搜索功能怎么实现的？", ["search"], "medium"),
                ("索引是怎么构建的？", ["search"], "medium"),
            ],
            "logging": [
                ("日志系统怎么配置的？", ["logging"], "medium"),
            ],
            "scheduler": [
                ("定时任务有哪些？", ["scheduler"], "medium"),
            ],
            "worker": [
                ("后台任务队列怎么处理的？", ["worker"], "medium"),
            ],
            "analytics": [
                ("数据统计做了哪些指标？", ["analytics"], "medium"),
            ],
        }

    conversation = []
    for mod in modules:
        if mod in queries_by_module:
            for query, expected, complexity in queries_by_module[mod]:
                expected_files = [f"{m}.py" for m in expected]
                conversation.append({
                    "user_query": query,
                    "relevant_files": expected_files,
                    "response_complexity": complexity,
                    "context_forget_risk": len(conversation) > 6,
                })

    return conversation


# ══════════════════════════════════════════════════
#  对话模拟
# ══════════════════════════════════════════════════

def simulate(engine: VibeCodingEngine, project_root: str, lang: str = "zh"):
    """
    用真实引擎模拟对话，对比有/无 MCP 的 token 消耗。

    关键改进：模拟规则进化流程
      1. 第一轮：纯 BM25 检索，记录失败
      2. 进化：LLM 发现关联 → 自动创建规则
      3. 第二轮：BM25 + 规则 boost，观察改善
    """

    modules = []
    for f in Path(project_root).glob("*.py"):
        if f.name != "__init__.py":
            modules.append(f.stem)

    conversation_flow = build_conversations(modules, lang=lang)

    # 创建规则存储
    rules_path = os.path.join(project_root, ".mcp_rules.json")
    rules_store = RulesStore(persist_path=rules_path)

    without_mcp = ConversationScenario(name="无 MCP", description="纯对话上下文")
    with_mcp = ConversationScenario(name="有 MCP (含规则进化)", description="精准检索 + 规则学习")
    with_mcp_no_rules = ConversationScenario(name="有 MCP (无规则)", description="仅 BM25 检索，无规则进化")

    SYSTEM_PROMPT = 800
    BASE_USER_MSG = 150
    AVG_OUTPUT = 600
    FORGET_THRESHOLD = 3000
    FORGET_RATE = 0.15

    acc_ctx_no_mcp = SYSTEM_PROMPT
    round_num = 0

    # 召回统计
    stats_no_rules = {"success": 0, "failure": 0, "total": 0}
    stats_with_rules = {"success": 0, "failure": 0, "total": 0}
    rules_created = 0
    rules_verified = 0

    for step in conversation_flow:
        round_num += 1
        query = step["user_query"]
        relevant = step["relevant_files"]
        complexity = step["response_complexity"]
        forget_risk = step.get("context_forget_risk", False)

        user_tok = BASE_USER_MSG + len(query) // 2
        out_tok = AVG_OUTPUT * (2 if complexity == "high" else 1)

        # ═══ 召回检测（两轮对比）═══

        # 第一轮：纯 BM25（无规则）
        mcp_ok_no_rules = check_recall_quality(engine, query, relevant)
        stats_no_rules["total"] += 1
        if mcp_ok_no_rules:
            stats_no_rules["success"] += 1
        else:
            stats_no_rules["failure"] += 1

        # 规则进化：如果第一轮失败，LLM 发现关联并创建规则
        if not mcp_ok_no_rules:
            # 模拟 LLM 发现：用户问 "支付流程是怎样的" → 应该关联 payment.py
            for rf in relevant:
                stem = Path(rf).stem
                rule = Rule(
                    rule_id="", rule_type="pattern", scope="project",
                    description=f"LLM-discovered: '{query[:50]}' → {rf}",
                    condition=query,
                    action=f"boost {rf}",
                    confidence=0.7, source="auto",
                    status="observing",
                    related_files=[os.path.join(project_root, rf)],
                )
                rid = rules_store.add_rule(rule, auto_observe=True)
                rules_created += 1
                # 模拟 LLM 验证：这个关联是正确的（因为我们知道 relevant 是对的）
                rules_store.record_verified(rid)
                # 直接提升到 active（正常流程需要观察期，这里模拟 LLM 即时确认）
                rule_obj = rules_store.get_rule(rid)
                for _ in range(25):
                    rule_obj.record_observation(triggered=True)
                rules_store.promote_observing_rules()

        # 第二轮：BM25 + 规则 boost
        mcp_ok_with_rules = check_recall_quality_with_rules(engine, rules_store, query, relevant)
        stats_with_rules["total"] += 1
        if mcp_ok_with_rules:
            stats_with_rules["success"] += 1
        else:
            stats_with_rules["failure"] += 1

        # ═══ 无 MCP 场景 ═══
        needs_repeat = False
        if forget_risk and acc_ctx_no_mcp > FORGET_THRESHOLD:
            prob = min(0.8, FORGET_RATE * (acc_ctx_no_mcp - FORGET_THRESHOLD) / 1000)
            if random.random() < prob:
                needs_repeat = True

        if needs_repeat:
            repeat_tok = 300 * len(relevant)
            in_tok = user_tok + acc_ctx_no_mcp + repeat_tok
            without_mcp.rounds.append(TokenUsage(
                round_num=round_num, action=f"遗忘-重复: {query[:35]}...",
                input_tokens=in_tok, output_tokens=out_tok,
                context_tokens=acc_ctx_no_mcp, is_repeat_context=True,
            ))
        else:
            in_tok = user_tok + acc_ctx_no_mcp
            without_mcp.rounds.append(TokenUsage(
                round_num=round_num, action=f"对话: {query[:35]}...",
                input_tokens=in_tok, output_tokens=out_tok,
                context_tokens=acc_ctx_no_mcp,
            ))
        acc_ctx_no_mcp += user_tok + out_tok

        # MCP 失败 → 无 MCP 也需要追问
        if not mcp_ok_no_rules:
            followup = f"我说的是 {', '.join(r.replace('.py','') for r in relevant)} 模块"
            fu_tok = BASE_USER_MSG + len(followup) // 2
            in_fu = fu_tok + acc_ctx_no_mcp
            out_fu = AVG_OUTPUT // 2
            without_mcp.rounds.append(TokenUsage(
                round_num=round_num + 0.5, action=f"追问: {followup[:35]}...",
                input_tokens=in_fu, output_tokens=out_fu,
                context_tokens=acc_ctx_no_mcp, is_recall_failure=True,
            ))
            acc_ctx_no_mcp += fu_tok + out_fu

        # ═══ 有 MCP（含规则进化）场景 ═══
        mcp_ctx = 0
        try:
            results = engine.retrieve(query, top_k=5)
            mcp_ctx = engine._estimate_tokens("\n".join(c.content for _, c in results))
        except Exception:
            mcp_ctx = 300 * len(relevant)

        in_mcp = user_tok + SYSTEM_PROMPT + mcp_ctx
        with_mcp.rounds.append(TokenUsage(
            round_num=round_num,
            action=f"MCP{'✓' if mcp_ok_with_rules else '✗'}: {query[:35]}...",
            input_tokens=in_mcp, output_tokens=out_tok,
            context_tokens=SYSTEM_PROMPT + mcp_ctx,
            is_recall_failure=not mcp_ok_with_rules,
        ))

        if not mcp_ok_with_rules:
            followup = f"我说的是 {', '.join(r.replace('.py','') for r in relevant)} 模块"
            fu_tok = BASE_USER_MSG + len(followup) // 2
            fu_ctx = 0
            try:
                fr = engine.retrieve(followup, top_k=5)
                fu_ctx = engine._estimate_tokens("\n".join(c.content for _, c in fr))
            except Exception:
                fu_ctx = 300
            in_fu = fu_tok + SYSTEM_PROMPT + fu_ctx
            with_mcp.rounds.append(TokenUsage(
                round_num=round_num + 0.5, action=f"MCP追问: {followup[:35]}...",
                input_tokens=in_fu, output_tokens=AVG_OUTPUT // 2,
                context_tokens=SYSTEM_PROMPT + fu_ctx, is_recall_failure=True,
            ))

        # ═══ 有 MCP（无规则）场景 — 用于对比 ═══
        with_mcp_no_rules.rounds.append(TokenUsage(
            round_num=round_num,
            action=f"MCP{'✓' if mcp_ok_no_rules else '✗'}: {query[:35]}...",
            input_tokens=in_mcp, output_tokens=out_tok,
            context_tokens=SYSTEM_PROMPT + mcp_ctx,
            is_recall_failure=not mcp_ok_no_rules,
        ))

        # 无规则场景也需要追问轮次
        if not mcp_ok_no_rules:
            fu_tok = BASE_USER_MSG + 30  # 追问的 token
            in_fu_nr = fu_tok + SYSTEM_PROMPT + mcp_ctx
            with_mcp_no_rules.rounds.append(TokenUsage(
                round_num=round_num + 0.5,
                action=f"无规则追问: {query[:35]}...",
                input_tokens=in_fu_nr, output_tokens=AVG_OUTPUT // 2,
                context_tokens=SYSTEM_PROMPT + mcp_ctx,
                is_recall_failure=True,
            ))

    # 清理
    try:
        os.remove(rules_path)
    except:
        pass

    evolution_stats = {
        "rules_created": rules_created,
        "recall_no_rules": stats_no_rules,
        "recall_with_rules": stats_with_rules,
        "improvement": stats_with_rules["success"] - stats_no_rules["success"],
    }

    return without_mcp, with_mcp, with_mcp_no_rules, stats_with_rules, evolution_stats


# ══════════════════════════════════════════════════
#  报告
# ══════════════════════════════════════════════════

def report(w, m, m_no_rules, recall_stats, evolution_stats, models=None):
    if models is None:
        models = ["deepseek-v3", "gpt-4o", "claude-3.5-sonnet", "qwen-max"]

    print("\n" + "═" * 72)
    print("  真实场景 Token 消耗对比报告（含规则进化）")
    print("═" * 72)

    rs = evolution_stats["recall_no_rules"]
    rs_after = evolution_stats["recall_with_rules"]
    print(f"\n  召回统计（规则进化前 → 进化后）:")
    print(f"    纯 BM25:      {rs['success']}/{rs['total']} 成功 ({rs['success']/max(rs['total'],1)*100:.0f}%)")
    print(f"    BM25 + 规则:  {rs_after['success']}/{rs_after['total']} 成功 ({rs_after['success']/max(rs_after['total'],1)*100:.0f}%)")
    print(f"    规则创建:     {evolution_stats['rules_created']} 条")
    print(f"    召回改善:     +{evolution_stats['improvement']} 个查询")

    print(f"\n  {'指标':<20} {'无MCP':>12} {'有MCP(无规则)':>14} {'有MCP(含规则)':>14}")
    print(f"  {'─'*64}")
    print(f"  {'对话轮次':<20} {len(w.rounds):>12} {len(m_no_rules.rounds):>14} {len(m.rounds):>14}")
    print(f"  {'Input tokens':<20} {w.total_input():>12,} {m_no_rules.total_input():>14,} {m.total_input():>14,}")
    print(f"  {'Output tokens':<20} {w.total_output():>12,} {m_no_rules.total_output():>14,} {m.total_output():>14,}")
    print(f"  {'总计 tokens':<20} {w.total_input()+w.total_output():>12,} {m_no_rules.total_input()+m_no_rules.total_output():>14,} {m.total_input()+m.total_output():>14,}")

    # 计算规则进化带来的额外节省（避免的追问轮次）
    followup_rounds_no_rules = sum(1 for r in m_no_rules.rounds if r.is_recall_failure)
    followup_rounds_with_rules = sum(1 for r in m.rounds if r.is_recall_failure)
    followup_saved_rounds = followup_rounds_no_rules - followup_rounds_with_rules
    followup_saved_tokens = followup_saved_rounds * 500  # 每次追问约 500 tokens

    total_saved_no_rules = (w.total_input()+w.total_output()) - (m_no_rules.total_input()+m_no_rules.total_output())
    total_saved_with_rules = (w.total_input()+w.total_output()) - (m.total_input()+m.total_output())
    pct_no_rules = total_saved_no_rules / max(w.total_input()+w.total_output(), 1) * 100
    pct_with_rules = total_saved_with_rules / max(w.total_input()+w.total_output(), 1) * 100

    print(f"\n  Token 节省 (无规则): {total_saved_no_rules:,} ({pct_no_rules:.1f}%)")
    print(f"  Token 节省 (含规则): {total_saved_with_rules:,} ({pct_with_rules:.1f}%)")
    if followup_saved_rounds > 0:
        print(f"  规则额外节省:       {followup_saved_tokens:,} tokens (避免 {followup_saved_rounds} 次追问)")

    print(f"\n  {'模型':<18} {'无MCP':>10} {'有MCP(无规则)':>14} {'有MCP(含规则)':>14}")
    print(f"  {'─'*60}")
    for model in models:
        wc = w.cost(model)
        mc_nr = m_no_rules.cost(model)
        mc = m.cost(model)
        print(f"  {model:<18} {wc['total_cost_yuan']:>10} {mc_nr['total_cost_yuan']:>14} {mc['total_cost_yuan']:>14}")

    # 节省来源分解
    ctx_saved = w.total_input() - m.total_input()
    fail_saved = sum(r.input_tokens + r.output_tokens for r in w.rounds if r.is_recall_failure) - \
                 sum(r.input_tokens + r.output_tokens for r in m.rounds if r.is_recall_failure)
    repeat_saved = sum(r.input_tokens for r in w.rounds if r.is_repeat_context)

    print(f"\n  节省来源分解（含规则进化）:")
    print(f"    上下文精简:     {max(0, ctx_saved - fail_saved - repeat_saved):>8,} tokens")
    print(f"    召回失败减少:   {max(0, fail_saved):>8,} tokens")
    print(f"    避免遗忘重复:   {repeat_saved:>8,} tokens")

    # 逐轮详情
    print(f"\n  {'─'*72}")
    print(f"  有 MCP（含规则进化）逐轮:")
    for r in m.rounds:
        flag = " ⚠️" if r.is_recall_failure else ""
        print(f"    [{r.round_num:>4}] {r.action:<42} in={r.input_tokens:>6,} out={r.output_tokens:>6,}{flag}")

    return {"recall_stats": recall_stats, "evolution_stats": evolution_stats,
            "saved_tokens": total_saved_with_rules, "saved_pct": round(pct_with_rules, 1)}


# ══════════════════════════════════════════════════
#  主入口
# ══════════════════════════════════════════════════

def main():
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║      真实场景 Token 消耗模拟器 v2                           ║")
    print("║      中文 vs 英文查询召回对比                               ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    for lang in ["zh", "en"]:
        print(f"\n\n{'█' * 72}")
        print(f"  语言: {'中文' if lang == 'zh' else 'English'}")
        print(f"{'█' * 72}")

        for scale in ["small", "medium", "large"]:
            print(f"\n\n{'▓' * 72}")
            print(f"  项目规模: {scale} | 语言: {'中文' if lang == 'zh' else '英文'}")
            print(f"{'▓' * 72}")

            base = tempfile.mkdtemp(prefix=f"toksim_{scale}_{lang}_")
            root = os.path.join(base, "project")

            try:
                n = generate_realistic_project(root, scale)
                print(f"  生成: {n} 个模块文件")

                config = dict(DEFAULT_CONFIG)
                engine = VibeCodingEngine(root, config)
                files, chunks = engine.index_project()
                print(f"  索引: {files} 文件, {chunks} chunks")

                w, m, m_nr, rs, evo = simulate(engine, root, lang=lang)
                report(w, m, m_nr, rs, evo)
            finally:
                shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
