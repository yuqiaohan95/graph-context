"""
Rules Engine v2 — 规则自动进化系统 (改进版)
=============================================
改进:
  1. 衰减模型: 基于准确率 + 代码变更，非时间衰减
  2. 规则匹配: BM25 tokenize 匹配，非子串匹配
  3. 观察模式: 新规则先观察再启用
  4. 冲突检测: 添加规则时检测矛盾
  5. 自动发现: 从 Ground Truth 错误中提取候选规则
"""

import os
import re
import time
import json
import math
import uuid
import threading
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional


# ══════════════════════════════════════════════════
#  分词工具 (复用 engine 的逻辑，保持独立)
# ══════════════════════════════════════════════════

_STOPWORDS = {
    "def", "class", "import", "from", "return", "if", "else", "for", "while",
    "try", "except", "with", "as", "in", "not", "and", "or", "is", "the",
    "a", "an", "to", "of", "it", "its", "be", "been", "being", "have", "has",
    "had", "do", "does", "did", "will", "would", "could", "should", "may",
    "might", "shall", "can", "this", "that", "these", "those", "at", "by",
    "for", "with", "about", "between", "through", "during", "before", "after",
}


def _tokenize(text: str) -> set[str]:
    """轻量 tokenize，用于规则匹配"""
    tokens = set()
    for ident in re.findall(r"[a-zA-Z_]\w*", text):
        low = ident.lower()
        if len(low) > 1 and low not in _STOPWORDS:
            tokens.add(low)
        # snake_case 拆分
        for seg in low.split("_"):
            if len(seg) > 1 and seg not in _STOPWORDS:
                tokens.add(seg)
    # 中文
    for cn in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.add(cn)
    return tokens


# ══════════════════════════════════════════════════
#  数据结构
# ══════════════════════════════════════════════════

@dataclass
class Rule:
    rule_id: str
    rule_type: str          # "pattern" | "preference" | "constraint"
    scope: str              # "project" | "global"
    description: str
    condition: str          # 触发条件 (自然语言)
    action: str             # 建议动作
    confidence: float       # 基础置信度 0.0-1.0
    hit_count: int = 0
    miss_count: int = 0
    created_at: float = 0.0
    last_used: float = 0.0
    source: str = "manual"  # "auto" | "manual" | "community"
    enabled: bool = True
    tags: list = field(default_factory=list)
    # ── v2 新增字段 ──
    status: str = "active"  # "observing" | "active" | "disabled"
    priority: int = 0       # 优先级，用于冲突时排序
    related_files: list = field(default_factory=list)  # 规则涉及的文件
    related_files_hashes: dict = field(default_factory=dict)  # 文件内容 hash
    verified_count: int = 0   # 被验证正确的次数
    rejected_count: int = 0   # 被验证错误的次数
    observing_hits: int = 0   # 观察期命中数
    observing_total: int = 0  # 观察期总触发数
    condition_tokens: list = field(default_factory=list)  # 预计算的 token
    _files_stale: bool = field(default=False, repr=False)  # 内部标记：关联文件是否被重写

    def __post_init__(self):
        if not self.condition_tokens:
            self.condition_tokens = sorted(_tokenize(self.condition))

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Rule":
        defaults = {
            "enabled": True, "tags": [],
            "status": "active", "priority": 0,
            "related_files": [], "related_files_hashes": {},
            "verified_count": 0, "rejected_count": 0,
            "observing_hits": 0, "observing_total": 0,
            "condition_tokens": [],
        }
        for k, v in defaults.items():
            if k not in data:
                data[k] = v
        rule = cls(**data)
        if not rule.condition_tokens:
            rule.condition_tokens = sorted(_tokenize(rule.condition))
        return rule

    # ── 准确率 ────────────────────────────────────

    @property
    def accuracy_rate(self) -> float:
        """基于验证结果的准确率"""
        total = self.verified_count + self.rejected_count
        if total == 0:
            # 没有验证数据，用 hit_rate 作为近似
            return self.hit_rate if self.total_uses > 0 else 0.5
        return self.verified_count / total

    @property
    def total_uses(self) -> int:
        return self.hit_count + self.miss_count

    @property
    def hit_rate(self) -> float:
        if self.total_uses == 0:
            return 0.0
        return self.hit_count / self.total_uses

    @property
    def effective_confidence(self) -> float:
        """
        有效置信度 = 基础置信度 × 准确率因子 × 代码新鲜度因子
        
        不再基于时间衰减！
        - 准确率因子：验证越多且正确率越高，因子越大
        - 代码新鲜度：规则涉及的文件被重写后，因子降低
        """
        return self.confidence * self._accuracy_factor() * self._code_freshness()

    def _accuracy_factor(self) -> float:
        """
        准确率因子：
        - 验证次数 < 5：因子=1.0 (数据不足，信任基础置信度)
        - 验证次数 >= 5：因子 = accuracy_rate，但有贝叶斯平滑
        """
        total = self.verified_count + self.rejected_count
        if total < 5:
            return 1.0
        # 贝叶斯平滑：假设先验为 2 次成功 1 次失败
        smoothed = (self.verified_count + 2) / (total + 3)
        return max(0.1, smoothed)

    def _code_freshness(self) -> float:
        """
        代码新鲜度因子：
        - 没有关联文件：1.0 (不限于特定代码)
        - 关联文件未变更：1.0
        - 关联文件被重写：降到 0.3-0.7 (取决于变更幅度)
        """
        if not self.related_files or not self.related_files_hashes:
            return 1.0
        # _files_stale 由 RulesStore._check_file_hashes 设置
        if self._files_stale:
            return 0.5  # 文件被重写，置信度减半
        return 1.0

    # ── 观察期 ────────────────────────────────────

    def record_observation(self, triggered: bool):
        """观察期记录：triggered=True 表示规则被触发"""
        self.observing_total += 1
        if triggered:
            self.observing_hits += 1

    @property
    def observation_score(self) -> float:
        """观察期得分"""
        if self.observing_total == 0:
            return 0.0
        return self.observing_hits / self.observing_total

    def should_activate(self, min_observations: int = 20, min_score: float = 0.5) -> bool:
        """判断观察期规则是否应该激活（更严格阈值）"""
        if self.status != "observing":
            return False
        return (self.observing_total >= min_observations and
                self.observation_score >= min_score)

    # ── 命中/验证 ─────────────────────────────────

    def record_hit(self):
        self.hit_count += 1
        self.last_used = time.time()

    def record_miss(self):
        self.miss_count += 1

    def record_verified(self):
        """被验证正确（用户确认规则有用）"""
        self.verified_count += 1
        self.last_used = time.time()

    def record_rejected(self):
        """被验证错误（用户忽略/回退了规则效果）"""
        self.rejected_count += 1

    # ── Token 匹配 ────────────────────────────────

    def match_score(self, query_tokens: set[str], chunk_tokens: set[str] = None) -> float:
        """
        基于 tokenize 的匹配分数。
        要求 condition 至少 2 个有效 token，避免 "user" 这种宽泛匹配。
        """
        cond_tokens = set(self.condition_tokens)
        if len(cond_tokens) < 2:
            return 0.0

        query_overlap = cond_tokens & query_tokens
        if not query_overlap:
            return 0.0

        coverage = len(query_overlap) / len(cond_tokens)
        if coverage < 0.4:
            return 0.0

        query_score = coverage
        chunk_score = 0.0
        if chunk_tokens:
            chunk_overlap = len(cond_tokens & chunk_tokens)
            chunk_score = chunk_overlap / len(cond_tokens) * 0.5

        return query_score + chunk_score


# ══════════════════════════════════════════════════
#  规则存储
# ══════════════════════════════════════════════════

class RulesStore:
    """JSON 持久化规则存储，支持 CRUD + 准确率衰减 + 观察期 + 冲突检测"""

    def __init__(self, persist_path: Optional[str] = None):
        self._rules: dict[str, Rule] = {}
        self._persist_path = persist_path
        self._lock = threading.Lock()
        if persist_path:
            self._load()

    # ── CRUD ──────────────────────────────────────

    def add_rule(self, rule: Rule, auto_observe: bool = True) -> str:
        """
        添加规则。
        auto_observe=True 时，auto 源规则自动进入观察期。
        """
        with self._lock:
            if not rule.rule_id:
                rule.rule_id = f"rule_{uuid.uuid4().hex[:8]}"
            if rule.created_at <= 0:
                rule.created_at = time.time()

            # auto 源规则默认进入观察期
            if auto_observe and rule.source == "auto" and rule.status == "active":
                rule.status = "observing"

            self._rules[rule.rule_id] = rule
            self._save()
            return rule.rule_id

    def get_rule(self, rule_id: str) -> Optional[Rule]:
        return self._rules.get(rule_id)

    def update_rule(self, rule_id: str, **kwargs) -> bool:
        with self._lock:
            rule = self._rules.get(rule_id)
            if rule is None:
                return False
            for k, v in kwargs.items():
                if hasattr(rule, k):
                    setattr(rule, k, v)
            self._save()
            return True

    def delete_rule(self, rule_id: str) -> bool:
        with self._lock:
            if rule_id in self._rules:
                del self._rules[rule_id]
                self._save()
                return True
            return False

    def list_rules(
        self,
        rule_type: Optional[str] = None,
        scope: Optional[str] = None,
        source: Optional[str] = None,
        min_confidence: float = 0.0,
        enabled_only: bool = True,
        status: Optional[str] = None,
    ) -> list[Rule]:
        """列出规则，支持过滤"""
        results = []
        for rule in self._rules.values():
            if enabled_only and not rule.enabled:
                continue
            if rule_type and rule.rule_type != rule_type:
                continue
            if scope and rule.scope != scope:
                continue
            if source and rule.source != source:
                continue
            if status and rule.status != status:
                continue
            if rule.effective_confidence < min_confidence:
                continue
            results.append(rule)
        results.sort(key=lambda r: (-r.priority, -r.effective_confidence))
        return results

    # ── 命中/验证记录 ─────────────────────────────

    def record_hit(self, rule_id: str):
        with self._lock:
            rule = self._rules.get(rule_id)
            if rule:
                rule.record_hit()
                self._save()

    def record_miss(self, rule_id: str):
        with self._lock:
            rule = self._rules.get(rule_id)
            if rule:
                rule.record_miss()
                self._save()

    def record_verified(self, rule_id: str):
        """用户确认规则有用"""
        with self._lock:
            rule = self._rules.get(rule_id)
            if rule:
                rule.record_verified()
                self._save()

    def record_rejected(self, rule_id: str):
        """用户否定规则效果"""
        with self._lock:
            rule = self._rules.get(rule_id)
            if rule:
                rule.record_rejected()
                self._save()

    # ── 观察期管理 ────────────────────────────────

    def promote_observing_rules(self, min_observations: int = 20, min_score: float = 0.5) -> list[str]:
        """
        检查观察期规则，将满足条件的规则提升为 active。
        返回被提升的规则 ID 列表。
        """
        promoted = []
        with self._lock:
            for rule_id, rule in self._rules.items():
                if rule.should_activate(min_observations, min_score):
                    rule.status = "active"
                    promoted.append(rule_id)
            if promoted:
                self._save()
        return promoted

    def demote_failing_rules(self, min_observations: int = 20, max_score: float = 0.1) -> list[str]:
        """
        将观察期中表现极差的规则直接禁用。
        """
        demoted = []
        with self._lock:
            for rule_id, rule in self._rules.items():
                if (rule.status == "observing" and
                    rule.observing_total >= min_observations and
                    rule.observation_score < max_score):
                    rule.status = "disabled"
                    rule.enabled = False
                    demoted.append(rule_id)
            if demoted:
                self._save()
        return demoted

    # ── 衰减 (基于准确率，非时间) ─────────────────

    def apply_decay(self) -> list[str]:
        """
        基于准确率的衰减：
        - 验证次数足够且准确率极低 (< 0.15) → 禁用
        - 不再基于时间！规则可以永远有效，只要它确实正确。
        """
        disabled = []
        with self._lock:
            for rule_id, rule in self._rules.items():
                if not rule.enabled or rule.status != "active":
                    continue
                total = rule.verified_count + rule.rejected_count
                if total >= 10 and rule.accuracy_rate < 0.15:
                    rule.status = "disabled"
                    rule.enabled = False
                    disabled.append(rule_id)
            if disabled:
                self._save()
        return disabled

    def prune_rules(self, min_hit_rate: float = 0.1, min_uses: int = 5) -> list[str]:
        """清理低效规则：命中率过低且使用次数足够多"""
        disabled = []
        with self._lock:
            for rule_id, rule in self._rules.items():
                if not rule.enabled or rule.status != "active":
                    continue
                if rule.total_uses >= min_uses and rule.hit_rate < min_hit_rate:
                    rule.status = "disabled"
                    rule.enabled = False
                    disabled.append(rule_id)
            if disabled:
                self._save()
        return disabled

    # ── 冲突检测 ──────────────────────────────────

    def detect_conflicts(self, new_rule: Rule) -> list[dict]:
        """
        检测新规则与已有规则的冲突。
        改进：
        - 同向 action 不报冲突（即使高重叠）
        - 部分重叠需要更高阈值才报 duplicate
        - 只有真正矛盾的 action 才报 contradicting_action
        """
        conflicts = []
        new_tokens = set(new_rule.condition_tokens)
        if len(new_tokens) < 2:
            return conflicts

        new_action_lower = new_rule.action.lower()
        boost_words = {"boost", "优先", "提升", "增强", "prefer"}
        filter_words = {"filter", "过滤", "排除", "忽略", "skip", "exclude"}
        new_is_boost = any(w in new_action_lower for w in boost_words)
        new_is_filter = any(w in new_action_lower for w in filter_words)

        for rule_id, existing in self._rules.items():
            if not existing.enabled:
                continue

            existing_tokens = set(existing.condition_tokens)
            if len(existing_tokens) < 2:
                continue

            overlap = new_tokens & existing_tokens
            if not overlap:
                continue

            # 判断 action 方向
            existing_action_lower = existing.action.lower()
            existing_is_boost = any(w in existing_action_lower for w in boost_words)
            existing_is_filter = any(w in existing_action_lower for w in filter_words)

            # 只有反向 action 才报 contradicting
            action_opposes = (new_is_boost and existing_is_filter) or (new_is_filter and existing_is_boost)

            # 完全重叠（两个方向的 token 都覆盖）
            full_overlap = (overlap == new_tokens and overlap == existing_tokens)

            # 大部分重叠（>70% 的较小方被覆盖）
            min_size = min(len(new_tokens), len(existing_tokens))
            overlap_ratio = len(overlap) / min_size if min_size > 0 else 0

            if action_opposes and overlap_ratio >= 0.5:
                # 真正矛盾：有实质重叠且 action 相反
                conflicts.append({
                    "rule_id": rule_id,
                    "conflict_type": "contradicting_action",
                    "description": (
                        f"Rule '{rule_id}' ({existing.description[:40]}) has opposite action: "
                        f"'{existing.action[:40]}' vs new '{new_rule.action[:40]}'"
                    ),
                    "overlap_tokens": sorted(overlap),
                })
            elif full_overlap and not action_opposes:
                # 完全重叠且同向 → 只有 action 也完全相同时才是重复
                new_action_tokens = _tokenize(new_rule.action)
                existing_action_tokens = _tokenize(existing.action)
                if (new_action_tokens == existing_action_tokens and
                    existing.rule_type == new_rule.rule_type):
                    conflicts.append({
                        "rule_id": rule_id,
                        "conflict_type": "duplicate",
                        "description": (
                            f"Rule '{rule_id}' is identical to new rule"
                        ),
                        "overlap_tokens": sorted(overlap),
                    })
            # 部分重叠但同向 → 不报冲突（可能是细化规则）

        return conflicts

    # ── 应用规则到检索 (改进匹配) ─────────────────

    def apply_rules_to_query(self, query: str, results: list) -> list:
        """
        将活跃规则应用到检索结果上。
        使用 tokenize 匹配替代子串匹配。
        """
        active_rules = self.list_rules(enabled_only=True, status="active")
        if not active_rules:
            return results

        query_tokens = _tokenize(query)

        boosted = []
        for score, chunk in results:
            boost = 0.0
            chunk_text = f"{getattr(chunk, 'name', '')} {getattr(chunk, 'content', '')[:300]}"
            chunk_tokens = _tokenize(chunk_text)

            for rule in active_rules:
                match = rule.match_score(query_tokens, chunk_tokens)
                if match > 0:
                    boost += rule.effective_confidence * match * 2.0
                    rule.record_hit()
                elif rule.total_uses > 0 and rule.hit_rate > 0.3:
                    # 高命中率规则给小 boost
                    boost += rule.effective_confidence * 0.3

            # 观察期规则也记录，但不加权
            observing_rules = self.list_rules(enabled_only=True, status="observing")
            for rule in observing_rules:
                match = rule.match_score(query_tokens, chunk_tokens)
                rule.record_observation(triggered=(match > 0))

            boosted.append((score + boost, chunk))

        boosted.sort(key=lambda x: -x[0])
        return boosted

    # ── 自动发现规则 ──────────────────────────────

    def discover_rules_from_errors(
        self,
        engine,
        gt_path: str,
        top_k: int = 5,
        min_confidence: float = 0.5,
    ) -> list[str]:
        """
        从 Ground Truth 评估的错误案例中自动发现候选规则。
        分析 missed queries，提取可能的规则。
        返回新发现的规则 ID 列表。
        """
        if not os.path.exists(gt_path):
            return []

        from .ground_truth import GroundTruthEvaluator

        gt_data = GroundTruthEvaluator.load_gt(gt_path)
        if not gt_data:
            return []

        discovered = []
        for q in gt_data:
            query_text = q.get("query") or q.get("text") or ""
            relevant = q.get("relevant", [])
            relevant_contains = q.get("relevant_contains", [])
            if not relevant and not relevant_contains:
                continue

            # 执行检索
            results = engine.retrieve(query_text, top_k=top_k)

            # 找出 missed 的结果
            hit_files = set()
            for _, chunk in results:
                for r in relevant:
                    if (r.get("file_path") and
                        chunk.file_path.endswith(r["file_path"]) and
                        chunk.chunk_type == r.get("chunk_type")):
                        hit_files.add(chunk.file_path)

            # 分析哪些文件应该被返回但没被返回
            for r in relevant:
                fp = r.get("file_path", "")
                if not fp:
                    continue
                # 检查是否在结果中
                found = any(chunk.file_path.endswith(fp) for _, chunk in results)
                if not found:
                    # 这个文件应该被返回但没被返回 → 可能需要规则
                    # 提取查询和文件的关联
                    q_tokens = _tokenize(query_text)
                    file_stem = Path(fp).stem.lower()
                    if file_stem in q_tokens or any(t in file_stem for t in q_tokens):
                        rule = Rule(
                            rule_id="",
                            rule_type="pattern",
                            scope="project",
                            description=f"Auto-discovered: '{query_text[:40]}' should match {fp}",
                            condition=query_text,
                            action=f"boost {fp}",
                            confidence=min_confidence,
                            source="auto",
                            status="observing",
                            related_files=[fp],
                        )
                        rid = self.add_rule(rule, auto_observe=True)
                        discovered.append(rid)

        return discovered

    # ── 代码变更感知 ──────────────────────────────

    def check_code_freshness(self, engine) -> list[str]:
        """
        检查规则关联文件是否被重写。
        如果文件 hash 变更，降低规则置信度。
        返回受影响的规则 ID 列表。
        """
        affected = []
        with self._lock:
            for rule_id, rule in self._rules.items():
                if not rule.related_files or not rule.related_files_hashes:
                    continue
                stale = False
                for fp in rule.related_files:
                    stored_hash = rule.related_files_hashes.get(fp)
                    if not stored_hash:
                        continue
                    # 检查文件当前 hash
                    if hasattr(engine, "memory"):
                        current_chunks = engine.memory.file_chunks.get(fp, [])
                        for cid in current_chunks:
                            if cid < len(engine.memory.chunks):
                                chunk = engine.memory.chunks[cid]
                                if chunk and chunk.hash != stored_hash:
                                    stale = True
                                    break
                    if stale:
                        break
                if stale:
                    # 文件被重写，标记 stale 并降低置信度 30%
                    rule._files_stale = True
                    rule.confidence *= 0.7
                    rule.related_files_hashes.clear()  # 清除旧 hash
                    affected.append(rule_id)
            if affected:
                self._save()
        return affected

    # ── 评估 ──────────────────────────────────────

    def evaluate_rules(self, engine) -> dict:
        """用 GroundTruthEvaluator 验证规则效果"""
        gt_enabled = engine.config.get("ground_truth_enabled", False)
        gt_path = engine.config.get("ground_truth_path", "ground_truth/opencode_ground_truth.json")

        if not gt_enabled or not os.path.exists(gt_path):
            return {
                "enabled": False,
                "message": "Ground truth not enabled or path not found",
                "rules_count": len(self._rules),
            }

        from .ground_truth import GroundTruthEvaluator

        baseline = GroundTruthEvaluator.evaluate(engine, gt_path)

        active_rules = self.list_rules(enabled_only=True, status="active")
        rule_boosts = {}
        for rule in active_rules:
            rule_boosts[rule.rule_id] = {
                "condition": rule.condition,
                "action": rule.action,
                "effective_confidence": rule.effective_confidence,
            }

        original_rules = engine.config.get("_active_rules", None)
        engine.config["_active_rules"] = rule_boosts
        with_rules = GroundTruthEvaluator.evaluate(engine, gt_path)
        engine.config["_active_rules"] = original_rules

        return {
            "enabled": True,
            "active_rules": len(active_rules),
            "observing_rules": len(self.list_rules(enabled_only=True, status="observing")),
            "total_rules": len(self._rules),
            "baseline": {
                "accuracy": baseline.get("accuracy", 0),
                "avg_recall": baseline.get("avg_recall", 0),
                "avg_precision": baseline.get("avg_precision", 0),
            },
            "with_rules": {
                "accuracy": with_rules.get("accuracy", 0),
                "avg_recall": with_rules.get("avg_recall", 0),
                "avg_precision": with_rules.get("avg_precision", 0),
            },
            "improvement": {
                "accuracy_delta": with_rules.get("accuracy", 0) - baseline.get("accuracy", 0),
                "recall_delta": with_rules.get("avg_recall", 0) - baseline.get("avg_recall", 0),
                "precision_delta": with_rules.get("avg_precision", 0) - baseline.get("avg_precision", 0),
            },
        }

    # ── 持久化 ────────────────────────────────────

    def _save(self):
        if not self._persist_path:
            return
        data = {
            "version": 2,
            "rules": {rid: r.to_dict() for rid, r in self._rules.items()},
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
            for rid, rdata in data.get("rules", {}).items():
                self._rules[rid] = Rule.from_dict(rdata)
        except Exception:
            pass

    def stats(self) -> dict:
        active = [r for r in self._rules.values() if r.enabled and r.status == "active"]
        observing = [r for r in self._rules.values() if r.status == "observing"]
        disabled = [r for r in self._rules.values() if r.status == "disabled" or not r.enabled]
        return {
            "total_rules": len(self._rules),
            "active_rules": len(active),
            "observing_rules": len(observing),
            "disabled_rules": len(disabled),
            "by_type": {
                t: sum(1 for r in active if r.rule_type == t)
                for t in ("pattern", "preference", "constraint")
            },
            "by_source": {
                s: sum(1 for r in self._rules.values() if r.source == s)
                for s in ("auto", "manual", "community")
            },
            "avg_effective_confidence": (
                sum(r.effective_confidence for r in active) / len(active)
                if active else 0.0
            ),
            "avg_accuracy_rate": (
                sum(r.accuracy_rate for r in active) / len(active)
                if active else 0.0
            ),
        }
