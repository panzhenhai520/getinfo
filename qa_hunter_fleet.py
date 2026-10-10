#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 04（P04-06）· 并行风扇 / 超时 / 重试 / 回退 / 总预算 / 部分结果回退。

通用包 01_V2_ARCHITECTURE §2.4「独立任务必须 Fan-out」、§2.5「Barrier 只在真正需要全局
结果时出现」、§2.7「Failure Isolation」、§27「Pipeline 与 Barrier」、§28「Node Failure Policy」。
本模块只做**编排**：Hunter 本体在 `qa_hunters.py`，检索能力复用 `qa_retrieval`。

语义（必须能一条条对上 §28）
--------------------------------------------------------------------------
· 有界线程池：`QA_HUNTER_FLEET_MAX_WORKERS`（默认 4）——并发度有上限，不随 Hunter 数量膨胀。
· 单 Hunter 超时：`QA_HUNTER_FLEET_TIMEOUT_SECONDS`（默认 8s）。超时**不杀线程**（Python 不能
  安全杀线程），而是"放弃等待并标记降级"：Hunter 都是只读的，被放弃的那次调用自然结束、
  结果被丢弃，不影响已采纳结果，也不影响后续问题（没有共享可变状态）。
· 重试：`QA_HUNTER_FLEET_RETRIES`（默认 1）——对应 §28 的 `RETRY 1`，只重试超时/抛错的
  Hunter；只读操作天然幂等（这就是"idempotency"在本阶段的落点）。
· 回退：Hunter 声明的 `fallback_hunter`（§28 例：Vector Hunter 超时 → fallback BM25）。
  回退目标若已在本轮成功，直接标记"已被兜底"；若不在舰队里，如实记 `satisfied=False`。
· 总预算：`QA_HUNTER_FLEET_BUDGET_SECONDS`（默认 20s）。预算耗尽 → 停止等待，
  **返回已完成 Hunter 的部分结果**（`partial=True`、`stop_reason=BUDGET_EXHAUSTED`），
  未完成的 Hunter 记 `status=timeout / reason_code=budget_exhausted`。绝不因为一个通道慢
  就把整条检索拖成失败。
· 失败隔离：任何 Hunter 抛错都被 `BaseHunter.safe_run` 收敛成 `status=error` 的契约对象，
  扇入时按"没有这条证据"处理；只有全部 Hunter 都失败且没有任何证据时，
  `stats.partial/stop_reason` 才会体现出来（上层据此决定降级话术）。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence

from qa_graph_contracts import (
    HUNTER_BM25,
    HUNTER_CONTRACT_VERSION,
    HUNTER_GRAPH,
    HUNTER_QUERY_EXPANSION,
    HUNTER_SEMANTIC,
    HUNTER_STRUCTURED,
    QA_FAILURE_DEGRADE,
    QA_FAILURE_FALLBACK,
    QA_FAILURE_RETRY,
    QA_FAILURE_SKIP,
    QA_STOP_BUDGET_EXHAUSTED,
)
from qa_evidence import dedupe_evidence_items
from qa_hunters import (
    BM25Hunter,
    BaseHunter,
    GraphHunter,
    HunterRequest,
    QueryExpansionHunter,
    RetrievalPool,
    SemanticHunter,
    StructuredHunter,
    _env_float,
    hunter_outcome,
    time_window_receipt,
)
from qa_retrieval import _article_evidence, _env_flag, _env_int

# Hunter 的默认顺序 = 扇入优先级（结构化/政策 → 图 → 词面/语义），与既有
# `ArticleRetriever.retrieve()` 的拼装顺序一致（那里也是 page_context → policy_exact → graph → 关键词）。
DEFAULT_HUNTER_ORDER = (HUNTER_STRUCTURED, HUNTER_GRAPH, HUNTER_BM25, HUNTER_SEMANTIC,
                        HUNTER_QUERY_EXPANSION)


def fleet_enabled() -> bool:
    """管线接线开关：默认**关**（关掉即逐字回到 `ArticleRetriever.retrieve()` 旧路径）。"""
    return _env_flag("QA_HUNTER_FLEET", False)


def max_workers() -> int:
    return _env_int("QA_HUNTER_FLEET_MAX_WORKERS", 4, 1, 32)


def hunter_timeout_seconds() -> float:
    return _env_float("QA_HUNTER_FLEET_TIMEOUT_SECONDS", 8.0, 0.05, 300.0)


def total_budget_seconds() -> float:
    return _env_float("QA_HUNTER_FLEET_BUDGET_SECONDS", 20.0, 0.05, 600.0)


def retries() -> int:
    return _env_int("QA_HUNTER_FLEET_RETRIES", 1, 0, 5)


def pool_ttl_seconds() -> float:
    """候选池 TTL（秒）：0 = 不过期（测试用），默认 180 与 level1 检索缓存同量级。"""
    return _env_float("QA_HUNTER_POOL_TTL_SECONDS", 180.0, 0.0, 3600.0)


@dataclass
class HunterTask:
    """一个待执行的 Hunter + 它的失败策略参数（可逐 Hunter 覆盖）。"""

    hunter: BaseHunter
    retries: int = 1
    fallback: str = ""
    timeout_s: float = 0.0
    enabled: bool = True

    @property
    def hunter_id(self) -> str:
        return str(self.hunter.hunter_id)


@dataclass
class FleetOutcome:
    """舰队一次扇出的结果（符合 `qa_graph_contracts.HUNTER_FLEET_RESULT_SCHEMA`）。"""

    hunters: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    partial: bool = False
    stop_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "contract_version": HUNTER_CONTRACT_VERSION,
            "hunters": list(self.hunters),
            "evidence": list(self.evidence),
            "partial": bool(self.partial),
            "stop_reason": str(self.stop_reason),
            "stats": dict(self.stats),
        }


class HunterFleet:
    """有界并发的检索舰队（P04-06）。

    用法（集成/单测都走这条）：
        fleet = HunterFleet([BM25Hunter(retriever), GraphHunter(), ...])
        result = fleet.run(request)          # 契约对象
        local = fleet.retrieve(plan, industry_pack_id="...", limit=12)   # retrieve() 形状
    """

    def __init__(self, hunters: Iterable[HunterTask | BaseHunter], *,
                 max_workers: int | None = None, per_hunter_timeout: float | None = None,
                 total_budget: float | None = None, retries: int | None = None,
                 fallbacks: Mapping | None = None, retriever=None,
                 on_outcome: Callable | None = None, pool_ttl: float | None = None):
        self.tasks = [item if isinstance(item, HunterTask) else HunterTask(hunter=item)
                      for item in (hunters or [])]
        self.max_workers = int(max_workers if max_workers is not None else globals()["max_workers"]())
        self.per_hunter_timeout = float(
            per_hunter_timeout if per_hunter_timeout is not None else hunter_timeout_seconds())
        self.total_budget = float(
            total_budget if total_budget is not None else total_budget_seconds())
        self.retries = int(retries if retries is not None else globals()["retries"]())
        self.fallbacks = dict(fallbacks or {})
        self.retriever = retriever
        self.on_outcome = on_outcome
        self.pool_ttl = float(pool_ttl if pool_ttl is not None else pool_ttl_seconds())
        self._pool_lock = threading.Lock()
        self._pool = None
        self._pool_created = 0.0

    # -- 池子 ---------------------------------------------------------------
    def pool_for(self, pack_id: str) -> RetrievalPool | None:
        """整个舰队共用**一个**候选池（一次加载、多 Hunter 共用；见 RetrievalPool 注释）。

        池子按 (行业包, TTL) 失效：默认 `QA_HUNTER_POOL_TTL_SECONDS=180`（与 level1 检索缓存的
        TTL 同量级），避免长驻进程拿着几小时前的候选集打分。
        """
        retriever = self.retriever
        if retriever is None:
            for task in self.tasks:
                retriever = getattr(task.hunter, "retriever", None)
                if retriever is not None:
                    break
        if retriever is None:
            return None
        now = time.monotonic()
        with self._pool_lock:
            stale = bool(self._pool is not None and self.pool_ttl > 0
                         and (now - self._pool_created) > self.pool_ttl)
            if self._pool is None or self._pool.pack_id != str(pack_id or "") or stale:
                # 换行业包 / 过期都必须换池子，否则会把旧候选或别的包的候选拿来打分
                self._pool = RetrievalPool(retriever, pack_id)
                self._pool_created = now
            return self._pool

    # -- 扇出 ---------------------------------------------------------------
    def run(self, request: HunterRequest) -> dict:
        started = time.monotonic()
        deadline = started + max(0.05, self.total_budget)
        if request.deadline:
            deadline = min(deadline, float(request.deadline))
        request.deadline = deadline
        tasks = [task for task in self.tasks if task.enabled]
        skipped = [task for task in self.tasks if not task.enabled]
        outcomes: dict = {}
        attempts: dict = {}
        for task in skipped:
            outcomes[task.hunter_id] = hunter_outcome(
                task.hunter_id, status="skipped", reason_code="task_disabled",
                failure_policy=QA_FAILURE_SKIP, stats={"enabled": False})
            attempts[task.hunter_id] = 0
        if not tasks:
            return FleetOutcome(hunters=list(outcomes.values()), evidence=[],
                                stats=self._stats(list(outcomes.values()), started, 0, 0, [], 0),
                                partial=False, stop_reason="").to_dict()

        workers = max(1, min(self.max_workers, len(tasks)))
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="qa-hunter")
        pending: set = set()
        meta: dict = {}
        # 超时从"Hunter 真正开始执行"起算（排队不计入）：线程池里排队的任务不该被误判成超时。
        # `starts` 由工作线程写入，主线程只读，用一把小锁保护。
        starts: dict = {}
        starts_lock = threading.Lock()
        sequence = [0]

        def _run_task(hunter, hunter_request, key):
            with starts_lock:
                starts[key] = time.monotonic()
            return hunter.safe_run(hunter_request)

        def _submit(task):
            sequence[0] += 1
            key = "t%d-%s" % (sequence[0], task.hunter_id)
            future = executor.submit(_run_task, task.hunter, request, key)
            pending.add(future)
            meta[future] = {"task": task, "submitted": time.monotonic(), "key": key}
            attempts[task.hunter_id] = attempts.get(task.hunter_id, 0) + 1
            return future

        def _elapsed(info):
            """该次提交实际执行了多久；尚未开工（还在排队）返回 0.0，不参与超时判定。"""
            with starts_lock:
                began = starts.get(info.get("key"))
            return 0.0 if began is None else (time.monotonic() - began)

        try:
            for task in tasks:
                _submit(task)
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                timeout = max(0.01, min(remaining, max(0.01, self.per_hunter_timeout)))
                done, pending = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
                for future in done:
                    info = meta.pop(future)
                    outcome = self._collect(future, info["task"], attempts)
                    hunter_id = str(outcome["hunter_id"])
                    if (str(outcome.get("status")) in ("error", "timeout")
                            and attempts.get(hunter_id, 1) <= self.retries
                            and (deadline - time.monotonic()) > 0.05):
                        # §28 的 RETRY 1：只读检索天然幂等，失败（超时/抛错）重试一次
                        _submit(info["task"])
                        continue
                    outcomes[hunter_id] = outcome
                    if self.on_outcome is not None:
                        try:
                            self.on_outcome(outcome)
                        except Exception:
                            pass
                if done:
                    continue
                # 没有任何任务在时限内完成 → 按"已开始执行且超过单 Hunter 时限"判超时
                expired = [future for future in pending
                           if _elapsed(meta[future]) >= self.per_hunter_timeout]
                for future in expired:
                    pending.discard(future)
                    info = meta.pop(future)
                    task = info["task"]
                    if (attempts.get(task.hunter_id, 0) <= self.retries
                            and (deadline - time.monotonic()) > 0.05):
                        _submit(task)
                        continue
                    outcomes[task.hunter_id] = hunter_outcome(
                        task.hunter_id, status="timeout", reason_code="hunter_timeout",
                        failure_policy=QA_FAILURE_RETRY, timed_out=True,
                        attempts=attempts.get(task.hunter_id, 1),
                        latency_ms=int(_elapsed(info) * 1000),
                        stats={"timeout_s": self.per_hunter_timeout,
                               "queued_ms": int((time.monotonic() - info["submitted"]) * 1000)})
        finally:
            budget_out = bool(pending)
            for future in list(pending):
                info = meta.pop(future, None)
                if info is None:
                    continue
                task = info["task"]
                outcomes.setdefault(task.hunter_id, hunter_outcome(
                    task.hunter_id, status="timeout", reason_code="budget_exhausted",
                    failure_policy=QA_FAILURE_RETRY, timed_out=True,
                    attempts=attempts.get(task.hunter_id, 1),
                    latency_ms=int(_elapsed(info) * 1000),
                    stats={"budget_s": self.total_budget,
                           "queued_ms": int((time.monotonic() - info["submitted"]) * 1000)}))
            executor.shutdown(wait=False, cancel_futures=True)

        self._apply_fallbacks(outcomes)
        for hunter_id, outcome in outcomes.items():
            outcome["attempts"] = max(1, int(attempts.get(hunter_id, outcome.get("attempts") or 1)))
        ordered = self._order(outcomes)
        evidence = merge_hunter_evidence(ordered, limit=request.limit,
                                        page_evidence=request.plan.get("_page_evidence") or [])
        wall_ms = int((time.monotonic() - started) * 1000)
        stop_reason = QA_STOP_BUDGET_EXHAUSTED if budget_out else ""
        stats = self._stats(ordered, started, len(tasks), workers, evidence, wall_ms)
        stats["budget_exhausted"] = bool(budget_out)
        return FleetOutcome(hunters=ordered, evidence=evidence, stats=stats,
                            partial=bool(budget_out), stop_reason=stop_reason).to_dict()

    # -- 内部 ---------------------------------------------------------------
    def _collect(self, future, task: HunterTask, attempts: Mapping) -> dict:
        try:
            outcome = future.result()
        except Exception as exc:  # safe_run 自己已经兜了一层，这里是最后一道
            outcome = hunter_outcome(
                task.hunter_id, status="error", reason_code="hunter_exception",
                failure_policy=QA_FAILURE_DEGRADE,
                error="%s: %s" % (type(exc).__name__, str(exc)[:200]))
        if not isinstance(outcome, Mapping):
            outcome = hunter_outcome(task.hunter_id, status="error", reason_code="bad_outcome")
        outcome = dict(outcome)
        outcome.setdefault("hunter_id", task.hunter_id)
        outcome["attempts"] = int(attempts.get(task.hunter_id, 1) or 1)
        return outcome

    def _apply_fallbacks(self, outcomes: dict) -> None:
        """§28 的 FALLBACK：失败的 Hunter 找兜底；兜底已成功 → 标记"已被兜底"。"""
        for hunter_id in list(outcomes):
            outcome = outcomes[hunter_id]
            if outcome.get("status") in ("ok", "empty", "skipped"):
                continue
            target_id = str(outcome.get("fallback_hunter") or self.fallbacks.get(hunter_id) or "")
            if not target_id or target_id == hunter_id:
                continue
            target = outcomes.get(target_id)
            satisfied = bool(target) and str(target.get("status")) in ("ok", "empty")
            stats = dict(outcome.get("stats") or {})
            stats["fallback"] = {
                "hunter": target_id, "satisfied": satisfied,
                "evidence": len(target.get("evidence") or []) if target else 0,
                "note": "兜底 Hunter 本轮已成功，直接使用它的结果"
                if satisfied else "兜底 Hunter 不在舰队或同样失败（本通道无结果）",
            }
            outcome["stats"] = stats
            if satisfied:
                outcome["failure_policy"] = QA_FAILURE_FALLBACK

    def _order(self, outcomes: Mapping) -> list:
        ordered = []
        for hunter_id in DEFAULT_HUNTER_ORDER:
            if hunter_id in outcomes:
                ordered.append(outcomes[hunter_id])
        for hunter_id, outcome in outcomes.items():
            if hunter_id not in DEFAULT_HUNTER_ORDER:
                ordered.append(outcome)
        return ordered

    def _stats(self, outcomes, started, submitted, workers, evidence, wall_ms) -> dict:
        counts: dict = {}
        by_hunter: dict = {}
        hunter_ms = 0
        budget_exhausted = 0
        for outcome in outcomes:
            status = str(outcome.get("status") or "")
            counts[status] = counts.get(status, 0) + 1
            hunter_ms += int(outcome.get("latency_ms") or 0)
            if str(outcome.get("reason_code") or "") == "budget_exhausted":
                budget_exhausted += 1
            by_hunter[str(outcome.get("hunter_id"))] = {
                "status": status, "evidence": len(outcome.get("evidence") or []),
                "latency_ms": int(outcome.get("latency_ms") or 0),
                "attempts": int(outcome.get("attempts") or 1),
                "reason_code": str(outcome.get("reason_code") or ""),
            }
        degraded = sum(count for status, count in counts.items()
                       if status not in ("ok", "empty"))
        return {
            "hunters_total": len(outcomes),
            "hunters_submitted": int(submitted),
            "max_workers": int(workers),
            "counts": counts,
            "by_hunter": by_hunter,
            "degraded": degraded,
            "wall_ms": int(wall_ms),
            "hunter_ms_sum": int(hunter_ms),
            "parallelism_gain_ms": int(hunter_ms - wall_ms),
            "evidence": len(evidence),
            "retries": int(self.retries),
            "per_hunter_timeout_s": float(self.per_hunter_timeout),
            "budget_s": float(self.total_budget),
            "pool": (self._pool.stats() if self._pool is not None else {}),
            # 在总预算内跑完的 Hunter 数：提交数 - 因预算被放弃的数
            "finished_before_budget": max(0, int(submitted) - budget_exhausted),
        }

    # -- 与既有 retrieve() 同形状的输出 --------------------------------------
    def retrieve(self, plan: Mapping, *, industry_pack_id: str, page_context: Mapping | None = None,
                 limit: int = 12, pool: RetrievalPool | None = None) -> dict:
        """跑舰队并把结果整理成 `ArticleRetriever.retrieve()` 的同形状字典。

        键集与旧返回值一致（queries/evidence/excluded/stats/time_window/graph），另外多一个
        `hunters`（本轮的 Hunter 回执）。`stats` 里多一个**兄弟键** `hunter_fleet`——
        与 Phase 03 把核验回执放 `stats["verification"]` 是同一手法，不动既有键。
        """
        plan = dict(plan or {})
        pool = pool if pool is not None else self.pool_for(str(industry_pack_id))
        request = HunterRequest.from_plan(
            plan, industry_pack_id=str(industry_pack_id), limit=limit,
            page_context=page_context, pool=pool)
        page_items, page_denied = self._page_context_evidence(pool, page_context)
        request.plan["_page_evidence"] = page_items
        outcome = self.run(request)
        window = time_window_receipt(_time_window(request))
        graph_stats = _hunter_stats(outcome, HUNTER_GRAPH)
        structured_stats = _hunter_stats(outcome, HUNTER_STRUCTURED)
        bm25_stats = _hunter_stats(outcome, HUNTER_BM25)
        excluded = {
            **(pool.excluded if pool is not None else {}),
            "page_context": page_denied,
            "policy_exact": structured_stats.get("policy_registry") or {},
        }
        adopted_in_window = _in_window_adopted(outcome.get("evidence") or [], window)
        window["in_window_adopted"] = adopted_in_window
        if window.get("has_time") and not adopted_in_window:
            window["expanded"] = True
            window.setdefault("note", "在 %s 内没有找到直接证据，已自动扩大到全部历史资料"
                              % (window.get("label") or "指定时间范围"))
        stats = {
            "eligible": (pool.stats().get("pool_rows") if pool is not None else 0),
            "adopted": len(outcome.get("evidence") or []),
            "keyword_candidates": int(bm25_stats.get("scored") or 0),
            "graph_adopted": int(graph_stats.get("used") or 0),
            "hunter_fleet": outcome.get("stats") or {},
        }
        return {
            "queries": [str(item) for item in (plan.get("queries") or []) if str(item).strip()],
            "evidence": list(outcome.get("evidence") or []),
            "excluded": excluded,
            "stats": stats,
            "time_window": window,
            "graph": graph_stats,
            "hunters": list(outcome.get("hunters") or []),
        }

    def _page_context_evidence(self, pool, page_context: Mapping | None):
        """用户当前页面/显式引用 → 置顶证据（复用 `_article_evidence`，与旧路径同形状）。"""
        if pool is None:
            return [], []
        by_id = pool.by_id
        requested = []
        page_context = dict(page_context or {})
        for raw in [page_context.get("article_id"), *(page_context.get("article_ids") or [])]:
            try:
                article_id = int(raw)
            except (TypeError, ValueError):
                continue
            if article_id > 0 and article_id not in requested:
                requested.append(article_id)
        items, denied = [], []
        for article_id in requested:
            row = by_id.get(article_id)
            if not row:
                denied.append({"article_id": article_id, "reason": "not_found_or_not_authorized"})
                continue
            items.append(_article_evidence(row, score=1000, method="page_context",
                                           reason="用户当前页面或显式引用",
                                           source_type="page_context"))
        return items, denied


def _time_window(request: HunterRequest) -> dict:
    from qa_hunters import _time_window_for

    return _time_window_for(request)


def _hunter_stats(outcome: Mapping, hunter_id: str) -> dict:
    for item in outcome.get("hunters") or []:
        if str(item.get("hunter_id")) == hunter_id:
            return dict(item.get("stats") or {})
    return {}


def _in_window_adopted(evidence: Sequence[Mapping], window: Mapping) -> int:
    """窗口内采纳条数（与既有通道同口径：证据发布时间落在窗口 start..end 内）。"""
    if not window.get("has_time") or not window.get("start"):
        return 0
    start = str(window.get("start"))[:10]
    end = str(window.get("end") or "")[:10]
    count = 0
    for item in evidence:
        day = str(item.get("published_at") or "")[:10]
        if len(day) == 10 and start <= day <= (end or "9999-12-31"):
            count += 1
    return count


def graph_slot_cap(limit: int) -> int:
    """图证据名额：与 `ArticleRetriever.retrieve()` 里**同一条公式**（图事实不许挤光全文文章）。"""
    return max(1, min(_env_int("QA_GRAPH_MAX_ITEMS", 4, 0, 20),
                      max(1, int(limit or 12) // 3)))


def merge_hunter_evidence(outcomes: Iterable[Mapping], *, limit: int,
                          page_evidence: Iterable[Mapping] = ()) -> list:
    """扇入：按"置顶页内证据 → 结构化/政策 → 图（名额受限）→ 词面 → 语义"拼装。

    **为什么词面排在语义前面**（而不是把两边分数混在一起排）：BM25 分与余弦相似度**量纲不同**
    （实测把两者混排会让语义通道的 0.7×100 压过 BM25 的 6×10，把"确实含查询词的文章"挤出前 12），
    而 §8.1/§8.2 的分工本来就是"精确短语优先、语义近似补位"（与既有 retrieve() 里
    "标题/分类词加权高于语义相似度"的排序纪律一致）。所以这里做**两级排序**：
    先词面（按 BM25 分），再语义（按余弦），语义只填词面没占满的名额——它的价值
    （召回词面不命中的近似文章）照旧保留，只是不再挤掉词面命中。

    去重复用 `qa_evidence.dedupe_evidence_items`（既有四键口径：evidence_ref / article_id /
    source_url / 内容指纹），与 `qa_pipeline._dedupe_evidence` 是同一条实现；
    同一篇同时在两个通道出现时词面那条胜出（它排在前面）。
    Query Expansion 按 §8.5 不产证据，这里天然不进合并。
    """
    buckets = {"structured": [], "graph": [], "lexical": [], "semantic": []}
    for outcome in outcomes or []:
        hunter_id = str(outcome.get("hunter_id") or "")
        evidence = [dict(item) for item in (outcome.get("evidence") or []) if isinstance(item, Mapping)]
        if hunter_id == HUNTER_STRUCTURED:
            buckets["structured"].extend(evidence)
        elif hunter_id == HUNTER_GRAPH:
            buckets["graph"].extend(evidence)
        elif hunter_id == HUNTER_BM25:
            buckets["lexical"].extend(evidence)
        elif hunter_id == HUNTER_SEMANTIC:
            buckets["semantic"].extend(evidence)

    def _by_score(items):
        return sorted(items, key=lambda item: (float(item.get("score") or 0),
                                               str(item.get("published_at") or ""),
                                               int(item.get("article_id") or 0)), reverse=True)

    cap = graph_slot_cap(limit)
    ordered = [dict(item) for item in (page_evidence or [])]
    ordered.extend(buckets["structured"])
    ordered.extend(buckets["graph"][:cap])
    ordered.extend(_by_score(buckets["lexical"]))
    ordered.extend(_by_score(buckets["semantic"]))
    return dedupe_evidence_items(ordered, max(1, int(limit or 12)))


def build_default_fleet(database=None, *, retriever=None, graph_builder=None,
                        vector_loader=None, max_workers_override: int | None = None,
                        structured_providers: Iterable[Callable] | None = None) -> HunterFleet:
    """默认舰队：五个 Hunter 全部接到**既有组件**上（零新增检索实现）。

    · BM25 → 既有候选池 + 时间梯子 + 证据成型；
    · Semantic → 库内已有向量（`intel_article_embeddings`），**不接** `retriever.semantic_search`
      （那条会调 embedding 端点，本阶段明令禁用）；
    · Graph → `qa_retrieval.graph_evidence`（可注入 builder）；
    · Structured → 政策登记表精确命中 + 元数据闸门（可注入外部业务库 provider）；
    · Query Expansion → 既有词表/分词/图谱邻居。
    """
    retriever = retriever or default_retriever(database)
    hunters = [
        BM25Hunter(retriever),
        SemanticHunter(retriever, database=(database if database is not None
                                            else getattr(retriever, "database", None)),
                       vector_loader=vector_loader),
        GraphHunter(builder=graph_builder),
        StructuredHunter(retriever, structured_providers=structured_providers),
        QueryExpansionHunter(builder=graph_builder),
    ]
    return HunterFleet(hunters, retriever=retriever, max_workers=max_workers_override)


def default_retriever(database=None):
    """按需构造 `ArticleRetriever`（不传 semantic_search：舰队不用端点语义通道）。"""
    from qa_retrieval import ArticleRetriever

    if database is None:
        from sqlite_database import sqlite_db

        database = sqlite_db
    return ArticleRetriever(database)


__all__ = [
    "DEFAULT_HUNTER_ORDER", "FleetOutcome", "HunterFleet", "HunterTask",
    "build_default_fleet", "default_retriever", "fleet_enabled", "graph_slot_cap",
    "hunter_timeout_seconds", "max_workers", "merge_hunter_evidence", "retries",
    "total_budget_seconds",
]
