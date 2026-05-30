"""
中英文代码同义词映射表
零成本语义桥接 — 不需要模型，人工整理高频词
"""
import re

# 中文 → 英文代码标识符映射
SYNONYM_MAP: dict[str, list[str]] = {
    # ── 认证/授权 ──
    "认证": ["auth", "authenticate", "verify", "login", "signin", "credential"],
    "授权": ["authorize", "permission", "access", "grant", "role"],
    "登录": ["login", "signin", "sign_in", "log_in", "authenticate"],
    "登出": ["logout", "signout", "sign_out", "log_out"],
    "注册": ["register", "signup", "sign_up", "create_user", "enroll"],
    "密码": ["password", "passwd", "pwd", "hash", "salt", "credential"],
    "令牌": ["token", "jwt", "bearer", "session", "access_token"],
    "过期": ["expire", "expired", "overdue", "timeout", "expiry", "due_date"],
    "撤销": ["revoke", "invalidate", "cancel", "withdraw"],
    "刷新": ["refresh", "renew", "rotate"],

    # ── CRUD ──
    "创建": ["create", "add", "new", "insert", "make", "build"],
    "读取": ["read", "get", "fetch", "load", "retrieve", "query"],
    "更新": ["update", "modify", "edit", "patch", "change", "mutate"],
    "删除": ["delete", "remove", "drop", "destroy", "erase", "clear"],
    "搜索": ["search", "find", "query", "filter", "lookup", "seek"],
    "列表": ["list", "index", "browse", "paginate", "enumerate"],
    "详情": ["detail", "get", "show", "view", "inspect"],
    "保存": ["save", "store", "persist", "commit", "write"],
    "导入": ["import", "load", "read", "ingest", "parse"],
    "导出": ["export", "dump", "write", "output", "serialize"],

    # ── 任务/工作流 ──
    "任务": ["task", "job", "work", "assignment", "todo"],
    "项目": ["project", "workspace", "repo", "repository"],
    "状态": ["status", "state", "stage", "phase", "lifecycle"],
    "分配": ["assign", "allocate", "delegate", "dispatch"],
    "完成": ["complete", "finish", "done", "close", "resolve"],
    "取消": ["cancel", "abort", "revoke", "void"],
    "优先级": ["priority", "urgency", "importance", "severity"],
    "截止": ["deadline", "due", "due_date", "expire", "cutoff"],
    "通知": ["notify", "notification", "alert", "message", "signal"],
    "提醒": ["remind", "reminder", "alert", "notification"],
    "评论": ["comment", "note", "remark", "feedback", "review"],
    "标签": ["tag", "label", "category", "badge", "marker"],

    # ── 数据/模型 ──
    "用户": ["user", "account", "member", "person", "profile"],
    "文章": ["article", "post", "content", "document", "entry"],
    # ── 通用 ──
    "代码": ["code", "source", "implementation", "module"],
    "相关": ["related", "relevant", "associated", "linked"],
    "功能": ["feature", "function", "capability"],
    "模块": ["module", "component", "package", "service"],
    "实现": ["implement", "implementation", "realization"],
    "逻辑": ["logic", "business", "handler"],
    "机制": ["mechanism", "strategy", "approach", "pattern"],
    "策略": ["strategy", "policy", "approach", "pattern"],
    "淘汰": ["evict", "expire", "expire", "remove", "cleanup", "purge"],
    "过期": ["expire", "expired", "overdue", "timeout", "expiry", "due_date"],
    "配置": ["config", "configuration", "setting", "option", "preference"],
    "数据": ["data", "record", "entity", "model", "schema"],
    "字段": ["field", "column", "property", "attribute", "key"],
    "验证": ["validate", "verify", "check", "assert", "ensure"],
    "约束": ["constraint", "rule", "validation", "limit", "restriction"],

    # ── 网络/API ──
    "请求": ["request", "req", "call", "invoke", "fetch"],
    "响应": ["response", "resp", "reply", "result", "output"],
    "路由": ["route", "router", "endpoint", "path", "handler"],
    "中间件": ["middleware", "interceptor", "filter", "plugin", "hook"],
    "接口": ["interface", "api", "endpoint", "contract", "schema"],
    "限流": ["rate_limit", "throttle", "rate_limiter", "quota", "limiter"],
    "缓存": ["cache", "cached", "caching", "memoize", "store", "lru", "ttl", "evict"],
    "重试": ["retry", "redo", "rerun", "reattempt", "backoff"],
    "超时": ["timeout", "expire", "deadline", "time_limit"],

    # ── 工具/通用 ──
    "分页": ["paginate", "pagination", "page", "paging", "cursor"],
    "排序": ["sort", "order", "rank", "arrange", "sequence"],
    "过滤": ["filter", "where", "match", "select", "criteria"],
    "统计": ["stats", "statistics", "count", "aggregate", "metric"],
    "日志": ["log", "logger", "logging", "trace", "audit"],
    "错误": ["error", "err", "exception", "fault", "failure"],
    "异常": ["exception", "error", "fault", "panic", "crash"],
    "重试": ["retry", "redo", "rerun", "reattempt"],
    "降级": ["fallback", "degrade", "graceful", "circuit_breaker"],
    "熔断": ["circuit_breaker", "breaker", "fuse", "fallback"],
    "健康检查": ["health", "healthcheck", "ping", "heartbeat", "probe"],
    "迁移": ["migration", "migrate", "upgrade", "schema_change"],
    "部署": ["deploy", "deployment", "release", "publish", "rollout"],

    # ── 支付/交易 ──
    "支付": ["payment", "pay", "checkout", "charge", "billing"],
    "付款": ["payment", "pay", "checkout", "charge"],
    "退款": ["refund", "reimburse", "return", "chargeback", "reversal"],
    "交易": ["transaction", "trade", "deal", "exchange"],
    "订单": ["order", "purchase", "booking"],
    "发票": ["invoice", "receipt", "billing"],
    "金额": ["amount", "total", "price", "cost", "sum"],
    "账户": ["account", "wallet", "balance"],
    "余额": ["balance", "remaining", "credit"],
    "充值": ["topup", "recharge", "deposit", "add_funds"],
    "扣款": ["deduct", "charge", "debit"],
    "转账": ["transfer", "wire", "send_money"],

    # ── 流程/状态 ──
    "流程": ["flow", "process", "procedure", "workflow", "pipeline"],
    "状态": ["status", "state", "stage", "phase", "lifecycle"],
    "处理": ["process", "handle", "deal", "execute"],
    "触发": ["trigger", "fire", "emit", "invoke"],
    "回调": ["callback", "hook", "handler", "listener"],
    "轮询": ["poll", "polling", "check_interval"],
    "取消": ["cancel", "abort", "revoke", "void"],
    "完成": ["complete", "finish", "done", "close", "resolve"],
    "失败": ["fail", "failure", "error", "break"],
    "成功": ["success", "ok", "passed", "succeed"],

    # ── 安全 ──
    "加密": ["encrypt", "encryption", "cipher", "crypto"],
    "解密": ["decrypt", "decryption", "decipher"],
    "哈希": ["hash", "digest", "checksum", "sha", "md5"],
    "权限": ["permission", "access", "privilege", "authorization", "acl"],
    "角色": ["role", "group", "team", "profile"],
    "安全": ["security", "secure", "safe", "protection", "auth", "authenticate",
             "permission", "access", "privilege", "acl", "credential", "encrypt",
             "hash", "verify", "token", "session", "cors", "csrf", "xss", "sanitize"],
    "注入": ["inject", "injection", "sqli", "xss"],
    "跨站": ["csrf", "xss", "cors", "cross_site"],

    # ── 性能 ──
    "并发": ["concurrent", "parallel", "async", "threading", "thread", "mutex", "lock", "synchronized"],
    "线程": ["thread", "threading", "mutex", "lock", "synchronized", "concurrent", "parallel"],
    "异步": ["async", "await", "deferred", "non_blocking"],
    "队列": ["queue", "fifo", "buffer", "channel"],
    "批量": ["batch", "bulk", "chunk", "group"],
    "压缩": ["compress", "gzip", "zip", "pack", "minify"],
    "优化": ["optimize", "improve", "enhance", "perf", "performance"],
}

# 英文 → 中文反向映射（自动构建）
_REVERSE_MAP: dict[str, list[str]] = {}
for cn, ens in SYNONYM_MAP.items():
    for en in ens:
        if en not in _REVERSE_MAP:
            _REVERSE_MAP[en] = []
        _REVERSE_MAP[en].append(cn)


def expand_query(tokens: list[str], replace_chinese: bool = False) -> list[str]:
    """
    将查询 token 通过同义词表扩展。
    支持传递扩展: 中文→英文→关联英文（如 "安全"→"security"→"auth"）

    Args:
        tokens: 原始查询 token
        replace_chinese: 如果为 True，将中文 token 替换为其英文同义词
                        （中文在代码 chunk 中没有匹配项，替换后 BM25 才能工作）
    """
    expanded = []

    for t in tokens:
        low = t.lower()
        is_chinese = bool(re.match(r"[\u4e00-\u9fff]", t))

        if is_chinese:
            # 中文 token: 尝试从同义词表匹配
            matched = False
            if low in SYNONYM_MAP:
                expanded.extend(SYNONYM_MAP[low])
                matched = True
            else:
                # 子串匹配: "过期任务检查" → 匹配 "过期", "任务", "检查"
                for key in SYNONYM_MAP:
                    if key in low:
                        expanded.extend(SYNONYM_MAP[key])
                        matched = True
            if not matched and not replace_chinese:
                expanded.append(t)
        else:
            # 英文 → 保留 + 间接扩展
            expanded.append(t)
            if low in _REVERSE_MAP:
                for cn in _REVERSE_MAP[low]:
                    expanded.extend(SYNONYM_MAP.get(cn, []))

    # ── 传递扩展: 英文→关联英文（第二跳）──
    # 例: "security" → 反向映射到 "安全" → 正向映射到 "auth", "permission" 等
    second_hop = []
    for t in list(expanded):
        low = t.lower()
        if low in _REVERSE_MAP:
            for cn in _REVERSE_MAP[low]:
                for related in SYNONYM_MAP.get(cn, []):
                    if related.lower() not in {x.lower() for x in expanded}:
                        second_hop.append(related)
    expanded.extend(second_hop)

    # 去重
    seen = set()
    result = []
    for t in expanded:
        if t.lower() not in seen:
            seen.add(t.lower())
            result.append(t)
    return result
