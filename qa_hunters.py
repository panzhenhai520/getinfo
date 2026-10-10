#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 04（P04-01…P04-05）· 检索舰队：统一 Hunter 接口 + 五个 Hunter 适配器。

通用包 01_V2_ARCHITECTURE §8 把 Retrieval Fleet 拆成 BM25 / Semantic / Graph /
Metadata / Query Expansion / Structured 六个 Hunter。本仓库**已经**有一个内含
keyword / semantic / graph / policy_exact / page_context 多通道的 `ArticleRetriever`，
所以本阶段的活是"把既有能力抽成统一 Hunter 接口 + 让每个 Hunter 可单独调用/可超时/可降级"，
**不是**另写一套检索。并行风扇、总预算、部分结果回退在 `qa_hunter_fleet.py`（P04-06）。

复用清单（新增代码刻意做薄，每处都写明"复用了什么"）
--------------------------------------------------------------------------
· BM25 Hunter（P04-01）
    复用 `ArticleRetriever._rows()`（候选池 + 既有全部闸门：inactive/stale/quality/unsafe_url）、
    `qa_retrieval._terms` / `_semantic_anchor_terms` / `_tokenize_query`（同一套 jieba 分词与停用词）、
    `qa_query_normalize.expand_terms`（繁简 + 英文别名词表）、`qa_retrieval.apply_time_gate`
    （同一把时间梯子）、`qa_retrieval._article_evidence`（证据成型，键集与既有通道逐字一致）。
    新增：Okapi BM25 打分（k1/b 可配）+ 文档侧分词（候选剪枝，避免对 1000 篇做 jieba）。
· Semantic Hunter（P04-02）
    复用**库内已有向量** `intel_article_embeddings`（默认委托 `chat_api._load_vector_matrix`，
    同一条 SQL、同样 L2 归一化、同样 120s 进程内缓存）。
    **零端点调用**（GPU 推理机与语音机器人共用、已明令停用）：查询向量不来自模型，而是用
    "词面种子文章的向量质心"（离线可复算）当查询向量 —— 取舍与能力边界写在 `_pivot_query_vector`。
    新增：种子选择、质心查询向量、`semantic_only` 统计（词面不命中、靠向量找回的条数）。
· Graph Hunter adapter（P04-03）
    **纯适配**：直接调 `qa_retrieval.graph_evidence()`（既有图通道：事件边/属性边 + 有效期过滤），
    外加统一接口 / 超时 / 降级包装。新增代码 ≈ 30 行，图逻辑一行没重写。
· Structured / DB adapter（P04-04）
    复用 `ArticleRetriever._policy_exact_evidence()`（政策登记表精确命中，通道名沿用
    `policy_exact`）+ `RetrievalPool` 候选池 + 既有时间闸门；新增"确定性元数据闸门"
    （时间/权威度/文档类型/域名/分类）。外部业务库（通用包 §8.6 的 SQL/API/EMR/HIS/LIS/PACS）
    按 DECISION_LOG D-002 抽象成"内部证据库"，留 `structured_providers` 注入点，
    **本轮不新增任何网络依赖**。
· Query Expansion Hunter（P04-05）
    复用 `qa_query_normalize.expand_terms/expand_query`（繁简 + 别名）、
    `qa_retrieval._terms/_semantic_anchor_terms`（jieba）、
    `kg_builder.KnowledgeGraphBuilder.neighborhood()`（图谱邻居 = 相关实体）。
    按 §8.5 硬约束：**只产词、不产证据、不做最终回答**（`evidence` 恒为空数组，有用例钉死）。

不做什么（边界，防止谎报）
--------------------------------------------------------------------------
· 不做 Planner / Gap Analyzer / Context Pack / Memory（Phase 05/07/08/09）。
· 不写 SearchTrace 表（Phase 05 的执行图落地后再统一写；这里只产回执）。
· 不改 `ArticleRetriever.retrieve()` 的既有行为：舰队是**并列的新入口**，
  由 `qa_pipeline` 的 `QA_HUNTER_FLEET`（默认关）切换，关掉即逐字回到旧路径。
"""
from __future__ import annotations

import math
import os
import re
import threading
import time
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
    QA_HUNTER_ROUTE_BY_ID,
)
from qa_retrieval import (
    ArticleRetriever,
    _article_evidence,
    _article_day,
    _env_flag,
    _env_int,
    _json,
    _semantic_anchor_terms,
    _terms,
    _window_from_adjustment,
    apply_time_gate,
    graph_evidence,
)

# ── 配置（一律环境变量可调，默认值不动既有行为）──────────────────────────────


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(str(os.getenv(name, str(default))).strip())
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def bm25_k1() -> float:
    return _env_float("QA_HUNTER_BM25_K1", 1.5, 0.1, 5.0)


def bm25_b() -> float:
    return _env_float("QA_HUNTER_BM25_B", 0.75, 0.0, 1.0)


def bm25_body_chars() -> int:
    return _env_int("QA_HUNTER_BM25_BODY_CHARS", 1500, 200, 12000)


def bm25_scale() -> float:
    """BM25 → 证据 score 的换算系数。

    BM25 是无界分数，而证据 `score` 在既有通道里是"加权命中分"（量级 ~10–100）。
    这里只做**单调线性换算**用于展示/排序，不宣称两者可比；排序只按 BM25 原分排。
    """
    return _env_float("QA_HUNTER_BM25_SCALE", 10.0, 0.1, 1000.0)


def semantic_seed_count() -> int:
    return _env_int("QA_HUNTER_SEMANTIC_SEEDS", 24, 1, 200)


def semantic_min_score() -> float:
    """语义通道采纳门槛（余弦）。默认 0.02 > 0：正交/无关向量（cos≈0）不许被带进证据包。"""
    return _env_float("QA_HUNTER_SEMANTIC_MIN_SCORE", 0.02, -1.0, 1.0)


def expansion_max_terms() -> int:
    return _env_int("QA_HUNTER_EXPANSION_MAX_TERMS", 24, 4, 120)


def hunter_enabled(hunter_id: str) -> bool:
    """单个 Hunter 的开关：`QA_HUNTER_<ID>_ENABLED`（默认开）。"""
    return _env_flag("QA_HUNTER_%s_ENABLED" % str(hunter_id or "").upper(), True)


# ── 请求 / 结果契约 ─────────────────────────────────────────────────────────


@dataclass
class HunterRequest:
    """一次 Hunter 调用的输入（typed contract，全字段只读语义）。

    `terms` / `anchor_terms` / `phrase_terms` 在 `from_plan` 里**一次性算好**：
    它们复用 `ArticleRetriever.retrieve()` 里同一套展开逻辑（`expand_terms(_terms(...))` 等），
    避免每个 Hunter 各算一遍、也避免多线程里重复触发 jieba。
    """

    question: str = ""
    industry_pack_id: str = ""
    queries: tuple = ()
    limit: int = 12
    plan: dict = field(default_factory=dict)
    page_context: dict = field(default_factory=dict)
    timeout_s: float = 0.0
    deadline: float = 0.0           # time.monotonic() 截止时刻；0.0 = 不限
    pool: "RetrievalPool | None" = None
    terms: tuple = ()
    anchor_terms: tuple = ()
    phrase_terms: tuple = ()
    structured_filters: dict = field(default_factory=dict)
    allowed_ids: frozenset | None = None

    @classmethod
    def from_plan(cls, plan: Mapping, *, industry_pack_id: str, limit: int = 12,
                  page_context: Mapping | None = None, pool: "RetrievalPool | None" = None,
                  timeout_s: float = 0.0, structured_filters: Mapping | None = None,
                  allowed_ids: Iterable | None = None) -> "HunterRequest":
        plan = dict(plan or {})
        question = str(plan.get("question") or "")
        queries = tuple(str(item) for item in (plan.get("queries") or []) if str(item).strip())
        source = list(queries) or [question]
        return cls(
            question=question,
            industry_pack_id=str(industry_pack_id or ""),
            queries=queries,
            limit=max(1, int(limit or 12)),
            plan=plan,
            page_context=dict(page_context or {}),
            timeout_s=float(timeout_s or 0.0),
            pool=pool,
            terms=tuple(_expand_terms(_terms(source))),
            anchor_terms=tuple(_expand_terms(_semantic_anchor_terms(source))),
            phrase_terms=tuple(_expand_terms(_query_phrases(source))),
            structured_filters=dict(structured_filters or plan.get("structured_filters") or {}),
            allowed_ids=frozenset(int(item) for item in (allowed_ids or []) if item) or None,
        )

    def remaining_seconds(self) -> float:
        """距截止时刻还剩多少秒；0.0 = 不限时（调用方不要再自行截断）。"""
        if not self.deadline:
            return 0.0
        return max(0.0, float(self.deadline) - time.monotonic())


def _expand_terms(terms: Iterable[str]) -> list:
    """复用 `qa_query_normalize.expand_terms`（简繁 + 英文别名），失败退回原词表。

    与 `ArticleRetriever.retrieve()` 里的同名闭包逐字同口径（那里也是 try/except 兜底）。
    """
    values = list(terms or [])
    try:
        from qa_query_normalize import expand_terms

        return expand_terms(values)
    except Exception:
        return values


def _query_phrases(values: Iterable[str]) -> list:
    from qa_retrieval import _query_phrases as _impl

    return _impl(values)


def hunter_outcome(hunter_id: str, *, route: str = "", status: str = "ok", evidence: Iterable = (),
                   attempts: int = 1, latency_ms: int = 0, stats: Mapping | None = None,
                   reason_code: str = "", failure_policy: str = "", error: str = "",
                   timed_out: bool = False, queries: Iterable = (), terms: Iterable = (),
                   fallback_from: str = "") -> dict:
    """构造一份符合 `qa_graph_contracts.HUNTER_RESULT_SCHEMA` 的 Hunter 结果。

    `degraded`/`ok` 由 `status` 派生，避免调用方各写一套判断：
      ok / empty → 正常运行（ok=True）；degraded / timeout / error / skipped → ok=False。
    """
    route = str(route or QA_HUNTER_ROUTE_BY_ID.get(hunter_id, ""))
    policy = str(failure_policy or QA_FAILURE_DEGRADE)
    return {
        "hunter_id": str(hunter_id),
        "contract_version": HUNTER_CONTRACT_VERSION,
        "route": route,
        "status": str(status),
        "ok": str(status) in ("ok", "empty"),
        "degraded": str(status) not in ("ok", "empty"),
        "timed_out": bool(timed_out),
        "reason_code": str(reason_code or ""),
        "failure_policy": policy,
        "attempts": max(1, int(attempts or 1)),
        "latency_ms": max(0, int(latency_ms or 0)),
        "evidence": [dict(item) for item in (evidence or []) if isinstance(item, Mapping)],
        "queries": [str(item) for item in (queries or []) if str(item).strip()],
        "terms": [dict(item) for item in (terms or []) if isinstance(item, Mapping)],
        "stats": dict(stats or {}),
        "error": str(error or ""),
        "fallback_from": str(fallback_from or ""),
    }


class BaseHunter:
    """Hunter 基类：统一 `hunter_id` / `route` / 开关 / 单跑安全壳。

    · `route` 取 `qa_graph_contracts.QA_HUNTER_ROUTE_BY_ID`（既有 7 个通道值），
      这样 Hunter 的回执能直接喂给 SearchTrace，不用扩通道枚举；
    · `safe_run()` 是"可单独调用"的入口：任何异常都变成一份 error 结果，
      调用方拿到的永远是契约对象（舰队另有一层超时/重试/回退，见 qa_hunter_fleet）。
    """

    hunter_id = ""
    #: 失败策略（通用包 §28 五值）。默认 DEGRADE：降级返回空证据，绝不拖垮整条检索。
    failure_policy = QA_FAILURE_DEGRADE
    #: 该 Hunter 失败时的兜底 Hunter（§28 例：Vector Hunter 超时 → fallback BM25）。
    fallback_hunter = ""

    @property
    def route(self) -> str:
        return QA_HUNTER_ROUTE_BY_ID.get(self.hunter_id, "")

    def enabled(self) -> bool:
        return hunter_enabled(self.hunter_id)

    def run(self, request: HunterRequest) -> dict:
        raise NotImplementedError

    def safe_run(self, request: HunterRequest) -> dict:
        """单跑安全壳：开关关 → skipped；抛错 → error（都符合契约）。"""
        started = time.monotonic()
        if not self.enabled():
            return hunter_outcome(
                self.hunter_id, status="skipped", reason_code="hunter_disabled",
                failure_policy=QA_FAILURE_SKIP, latency_ms=0,
                stats={"enabled": False})
        try:
            outcome = self.run(request)
        except Exception as exc:  # 单个 Hunter 的任何异常都不许冒泡
            return hunter_outcome(
                self.hunter_id, status="error", reason_code="hunter_exception",
                failure_policy=self.failure_policy,
                error="%s: %s" % (type(exc).__name__, str(exc)[:200]),
                latency_ms=int((time.monotonic() - started) * 1000))
        outcome.setdefault("contract_version", HUNTER_CONTRACT_VERSION)
        outcome["fallback_hunter"] = self.fallback_hunter
        return outcome


# ── 共享候选池（一次加载，多 Hunter 共用；线程安全）────────────────────────────


class RetrievalPool:
    """候选池：`ArticleRetriever._rows()` 的一次性、进程内、线程安全缓存。

    BM25 与 Structured 两个 Hunter 都需要这批行。如果各取一次，就是两次全表扫描 + 两遍
    闸门判定（实测 1000 行池子约 0.3–1s）。这里做成"谁先用谁加载、之后共用"，并把
    加载次数记进 `stats.pool_loads` 供验收核对。**只读**：不改任何行、不写库。
    """

    def __init__(self, retriever: ArticleRetriever, pack_id: str):
        self._retriever = retriever
        self._pack_id = str(pack_id or "")
        self._lock = threading.Lock()
        self._rows: list | None = None
        self._excluded: dict = {}
        self.loads = 0
        self.load_ms = 0

    @property
    def pack_id(self) -> str:
        return self._pack_id

    def load(self) -> tuple[list, dict]:
        with self._lock:
            if self._rows is None:
                started = time.monotonic()
                rows, excluded = self._retriever._rows(self._pack_id)
                self._rows = list(rows)
                self._excluded = dict(excluded)
                self.loads += 1
                self.load_ms = int((time.monotonic() - started) * 1000)
            return self._rows, self._excluded

    @property
    def rows(self) -> list:
        return self.load()[0]

    @property
    def excluded(self) -> dict:
        return dict(self.load()[1])

    @property
    def by_id(self) -> dict:
        return {int(row.get("id") or 0): row for row in self.rows}

    def stats(self) -> dict:
        return {"pool_rows": len(self.rows), "pool_loads": self.loads,
                "pool_load_ms": self.load_ms, "pool_excluded": self.excluded}


# ── BM25 Hunter（P04-01）────────────────────────────────────────────────────


def _tokenize_doc(text: str) -> list:
    """文档侧分词：与 `qa_retrieval._tokenize_query` **同一个 jieba 分词器 + 同一套停用词**。

    为什么不能直接用 `_tokenize_query`：那是给"问题"用的（去重后截 32 个词），
    拿来给 1500 字正文分词会把词表砍到 32，BM25 的 tf/idf 直接失真。
    这里复用它的分词器（jieba，缺库时退回 `_WORD_RE`）与停用词集合（`_STOP`），只改长度上限。
    """
    from qa_retrieval import _STOP, _WORD_RE

    text = str(text or "")
    try:
        import jieba  # type: ignore

        cutter = getattr(jieba, "lcut", None)
        raw_tokens = cutter(text) if callable(cutter) else list(jieba.cut(text))
    except Exception:
        raw_tokens = _WORD_RE.findall(text)
    tokens = []
    for raw in raw_tokens:
        term = str(raw or "").strip().casefold()
        if not term or term in _STOP or re.fullmatch(r"\d+", term):
            continue
        # 与 `_tokenize_query` 同一条纪律：单字没有检索意义（「新」「天」「有」会把不相干的
        # 文章顶上来，实测就是这个把汽车促销文排到问题实体前面）
        if len(term) < 2:
            continue
        # 纯标点/符号（jieba 会把「？」「，」单独切出来）没有检索意义，且会虚增文档长度
        if not re.search(r"[0-9a-z\u3400-\u9fff]", term):
            continue
        tokens.append(term)
        # 查询侧的实词来自 `_terms`（长 CJK 串 + 2/3 字 n-gram），而 jieba 只会给出自己的切分
        # （例如「家族办公室」切成「家族」+「办公室」）。这里把长 CJK 词也补上 2/3 字 n-gram，
        # 让两侧的词表落在同一个字面族里，否则精确短语会被分词差异吃掉。
        if re.fullmatch(r"[\u3400-\u9fff]{4,}", term):
            for width in (2, 3):
                for index in range(len(term) - width + 1):
                    part = term[index:index + width]
                    if part not in _STOP:
                        tokens.append(part)
    return tokens


def bm25_scores(query_tokens: Sequence[str], doc_tokens: Sequence[Sequence[str]], *,
                k1: float | None = None, b: float | None = None,
                dfs: Mapping | None = None, corpus_size: int | None = None) -> list:
    """Okapi BM25（纯 Python、无依赖、确定性）。

    `idf = ln(1 + (N - df + 0.5) / (df + 0.5))`，`tf` 饱和用 k1、长度归一用 b。

    `dfs`/`corpus_size` 用来**在整批候选池上统计文档频率**（Hunter 在剪枝扫描时顺手统计，零额外
    开销）。这一点很关键：如果只在"被剪枝剩下的候选"上算 idf，idf 会反过来奖励那些"因为稀有才
    没被剪掉"的泛词（实测把「奔驰上市」这种无关文章顶到「蔚来」前面）；用全池 df 才是 BM25 的
    正确口径。
    """
    k1 = bm25_k1() if k1 is None else float(k1)
    b = bm25_b() if b is None else float(b)
    total = int(corpus_size or len(doc_tokens)) or len(doc_tokens)
    if not total or not query_tokens:
        return [0.0] * len(doc_tokens)
    lengths = [len(tokens) for tokens in doc_tokens]
    avgdl = (sum(lengths) / len(doc_tokens)) or 1.0 if doc_tokens else 1.0
    scores = [0.0] * len(doc_tokens)
    unique_terms = list(dict.fromkeys(str(item) for item in query_tokens if str(item or "")))
    for term in unique_terms:
        if dfs is not None:
            df = int(dfs.get(term, 0) or 0)
        else:
            df = sum(1 for tokens in doc_tokens if term in tokens)
        df = max(df, sum(1 for tokens in doc_tokens if term in tokens))
        if not df:
            continue
        idf = math.log(1.0 + (total - df + 0.5) / (df + 0.5))
        for index, tokens in enumerate(doc_tokens):
            if not tokens:
                continue
            tf = tokens.count(term)
            if not tf:
                continue
            norm = 1.0 - b + b * (lengths[index] / avgdl)
            scores[index] += idf * (tf * (k1 + 1.0)) / (tf + k1 * norm)
    return scores


def _searchable_text(row: Mapping, body_chars: int = 4000) -> str:
    """行 → 检索文本（标题 + 分类词 + 主题标签 + 正文前段），与既有通道同一取材范围。"""
    title = str(row.get("title") or "")
    keywords = " ".join(str(item) for item in _json(row.get("matched_keywords_json"), []))
    topics = " ".join(str(item) for item in _json(row.get("topic_tags_json"), []))
    body = str(row.get("content") or "")[:body_chars]
    return " ".join(part for part in (title, keywords, topics, body) if part)


def _time_window_for(request: HunterRequest) -> dict:
    """问题文本 + 用户调整 → 时间窗（与 `ArticleRetriever.retrieve()` 逐字同口径）。"""
    source = list(request.queries) or [request.question]
    try:
        from qa_query_normalize import parse_time_window

        window = parse_time_window(" ".join(str(item) for item in source))
    except Exception:
        window = {"has_time": False, "days": None, "start": None, "end": None,
                  "label": "", "source": "none"}
    adjusted = _window_from_adjustment(request.plan or {})
    return adjusted if adjusted is not None else window


def time_window_receipt(window: Mapping) -> dict:
    """时间窗回执：JSON 安全（datetime → isoformat），字段口径与既有通道一致。"""
    out = dict(window or {})
    for key in ("start", "end"):
        value = out.get(key)
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
    return out


BM25_QUERY_FRAME_WORDS = frozenset({
    # 提问框架词：它们描述"我要什么形式的信息"，不描述"关于谁/关于什么"，
    # 在 BM25 里会变成高 idf 的噪声词（实测「有什么动态」把汽车促销文顶到「蔚来」前面）。
    "动态", "新动态", "进展", "情况", "消息", "资讯", "变化", "近况", "最近", "最新",
    "近期", "近日", "目前", "现在",
})
"""BM25 查询侧的局部停用词（**只影响 BM25 Hunter**，不动 `qa_retrieval._STOP` 的既有口径）。"""


def bm25_query_tokens(request: "HunterRequest") -> list:
    """BM25 的查询词表：**优先复用既有"实词"抽取**（`_semantic_anchor_terms` 的产出），
    再退到与文档同一个 jieba 分词器，最后才退到 `request.terms`。

    口径来源（都能在既有代码里指出来）：
      · `request.anchor_terms` = `expand_terms(_semantic_anchor_terms(queries))` —— 既有的
        "什么算实词"口径（jieba + `_STOP`/`_BROAD_INDUSTRY_TERMS`/`_QUERY_TARGET_TERMS`/
        长度与噪声片段过滤 + 繁简/别名展开）。实测它正好把「什么/最近/90/天/有/新」这类提问
        框架词滤掉，留下「智能/驾驶」——这正是 BM25 想要的词级单位；
      · 没有实词（例如整句都是停用词）时退回 jieba 分词（与文档侧同源），再不行退回
        `request.terms`（子串匹配口径），**绝不因为分词问题让通道哑火**。
    提问框架词（`BM25_QUERY_FRAME_WORDS`）按**简体形**比对，所以「動態」这类繁体变体也会被滤掉。
    """
    from qa_query_normalize import to_simplified

    def _is_frame(term: str) -> bool:
        try:
            return to_simplified(str(term)).casefold() in BM25_QUERY_FRAME_WORDS
        except Exception:
            return str(term).casefold() in BM25_QUERY_FRAME_WORDS

    anchors = [str(item).strip().casefold() for item in (request.anchor_terms or ())]
    tokens = [term for term in dict.fromkeys(anchors) if term and len(term) >= 2
              and not _is_frame(term)]
    if not tokens:
        for text in [request.question, *(request.queries or ())]:
            for term in _tokenize_doc(str(text or "")):
                if term not in tokens and not _is_frame(term):
                    tokens.append(term)
    if not tokens:
        tokens = [str(item).strip().casefold() for item in (request.terms or ())
                  if str(item).strip() and not _is_frame(item)]
    return tokens[:24]


class BM25Hunter(BaseHunter):
    """P04-01：词面精确通道，用 BM25 排序（药名/编码/法规号/专业术语这类精确短语）。

    语料 = `RetrievalPool`（既有闸门筛过的候选池）；候选剪枝：正文里**一个查询词都不含**
    的文档 BM25 必然得 0（BM25 的 term 只来自文档词表，文档不含该词 → tf=0），
    所以先做一次子串剪枝再分词，等价且省掉整个池子的 jieba 开销。
    分词结果按 (行业包, 文章 id, 正文长度) 记进程内缓存，同一批问题上不重复分词
    （池子内容变了 → 长度变 → 自动失效）。
    """

    hunter_id = HUNTER_BM25
    failure_policy = QA_FAILURE_RETRY
    fallback_hunter = ""

    def __init__(self, retriever: ArticleRetriever, *, pool: RetrievalPool | None = None,
                 max_docs: int | None = None):
        self.retriever = retriever
        self._pool = pool
        self.max_docs = int(max_docs if max_docs is not None
                            else _env_int("QA_HUNTER_BM25_MAX_DOCS", 300, 10, 2000))
        self._doc_cache: dict = {}
        self._cache_lock = threading.Lock()

    def _pool_for(self, request: HunterRequest) -> RetrievalPool:
        if request.pool is not None:
            return request.pool
        if self._pool is None:
            self._pool = RetrievalPool(self.retriever, request.industry_pack_id)
        return self._pool

    def doc_tokens(self, pack_id: str, row: Mapping, blob: str) -> list:
        """文档词表（带缓存）：标题 ×3（重复 2 次）+ 分类词 + 主题标签 + 正文前段。"""
        body_chars = bm25_body_chars()
        key = (str(pack_id), int(row.get("id") or 0), len(str(row.get("content") or "")))
        with self._cache_lock:
            cached = self._doc_cache.get(key)
        if cached is not None:
            return cached
        tokens = _tokenize_doc(blob[:body_chars])
        for _ in range(2):
            tokens.extend(_tokenize_doc(str(row.get("title") or "")))
        tokens.extend(_tokenize_doc(" ".join(
            str(item) for item in _json(row.get("matched_keywords_json"), []))))
        tokens.extend(_tokenize_doc(" ".join(
            str(item) for item in _json(row.get("topic_tags_json"), []))))
        with self._cache_lock:
            self._doc_cache[key] = tokens
        return tokens

    def run(self, request: HunterRequest) -> dict:
        started = time.monotonic()
        pool = self._pool_for(request)
        rows, _excluded = pool.load()
        query_tokens = bm25_query_tokens(request)
        if not query_tokens:
            return hunter_outcome(
                self.hunter_id, status="empty", reason_code="no_query_terms",
                failure_policy=self.failure_policy,
                stats={**pool.stats(), "candidates": 0, "scored": 0})
        # ① 子串剪枝 + 全池文档频率（等价裁剪：不含任何查询词的文档 BM25 恒为 0）
        candidates = []
        dfs = {term: 0 for term in query_tokens}
        for row in rows:
            blob = _searchable_text(row)
            folded = blob.casefold()
            title = str(row.get("title") or "").casefold()
            hits = [term for term in query_tokens if term in folded]
            for term in hits:
                dfs[term] += 1
            if not hits:
                continue
            title_hits = sum(1 for term in hits if term in title)
            candidates.append((title_hits * 2 + len(hits), row, blob, folded, hits))
        candidates.sort(key=lambda item: (-item[0], -int(item[1].get("id") or 0)))
        pruned = max(0, len(candidates) - self.max_docs)
        candidates = candidates[: self.max_docs]
        # ② 只在候选上分词 + 打分（idf 用全池 df，见 bm25_scores 的注释）
        doc_tokens = [self.doc_tokens(request.industry_pack_id, row, blob)
                      for _weight, row, blob, _folded, _hits in candidates]
        scores = bm25_scores(query_tokens, doc_tokens, dfs=dfs, corpus_size=len(rows))
        window = _time_window_for(request)
        hard_filter = _env_flag("QA_TIME_HARD_FILTER", True)
        min_in_window = _env_int("QA_TIME_MIN_IN_WINDOW", 1, 1, 50)
        ranked = []
        for (_weight, row, _blob, folded, hits), score in zip(candidates, scores):
            if score <= 0:
                continue
            reason_hits = [term for term in (request.terms or ()) if term in folded][:6] or hits[:6]
            ranked.append((float(score), str(row.get("publish_date") or row.get("first_crawled") or ""),
                           int(row.get("id") or 0), row,
                           "BM25 词面命中：" + "、".join(reason_hits), _article_day(row)))
        ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        gated, gate_receipt = apply_time_gate(
            ranked, window, hard_filter=hard_filter, min_in_window=min_in_window)
        evidence, seen_ids = [], set()
        for score, _date, article_id, row, reason, _day in gated:
            if len(evidence) >= request.limit:
                break
            if article_id in seen_ids:
                continue
            seen_ids.add(article_id)
            evidence.append(_article_evidence(
                row, score=round(score * bm25_scale(), 6), method="bm25", reason=reason))
        return hunter_outcome(
            self.hunter_id,
            status="ok" if evidence else "empty",
            reason_code="" if evidence else "no_bm25_match",
            failure_policy=self.failure_policy,
            evidence=evidence,
            latency_ms=int((time.monotonic() - started) * 1000),
            stats={**pool.stats(), "candidates": len(candidates), "pruned": pruned,
                   "scored": len(ranked), "query_tokens": query_tokens[:24],
                   "token_source": "jieba+anchor", "idf_corpus": len(rows),
                   "k1": bm25_k1(), "b": bm25_b(), "body_chars": bm25_body_chars(),
                   "max_docs": self.max_docs, "time_gate": gate_receipt,
                   "window": time_window_receipt(window).get("label", "")})


# ── Semantic Hunter（P04-02）────────────────────────────────────────────────


def load_article_vectors(database=None):
    """读**库内已有**向量 → `(ids, matrix)`；拿不到就 `(None, None)`。

    · `database is None`：委托 `chat_api._load_vector_matrix()`（既有实现，含 120s 缓存）；
    · 传入 database：走**同一条 SQL**（`intel_article_embeddings`，status='ready'）并做同样的
      L2 归一化 —— 这样单测/离线回放能在临时库上跑，不必碰全局单例。
    **全程零网络调用**：只读本地/内网库表，绝不请求 embedding 端点。
    """
    if database is None:
        try:
            from chat_api import _load_vector_matrix

            return _load_vector_matrix()
        except Exception:
            return None, None
    try:
        import numpy as np
    except Exception:
        return None, None
    try:
        database._ensure_connection()
        with database.lock:
            rows = database.connection.execute(
                "SELECT article_id, embedding_dim, embedding FROM intel_article_embeddings "
                "WHERE status='ready' ORDER BY article_id"
            ).fetchall()
    except Exception:
        return None, None
    ids, arrays = [], []
    for raw in rows:
        row = dict(raw)
        blob = bytes(row.get("embedding") or b"")
        dim = int(row.get("embedding_dim") or 1024)
        if dim <= 0 or len(blob) < dim * 4:
            continue
        arr = np.frombuffer(blob, dtype=np.float32)
        if arr.size != dim:
            continue
        ids.append(int(row.get("article_id") or 0))
        arrays.append(arr)
    if not arrays:
        return [], None
    matrix = np.vstack(arrays).astype(np.float32, copy=False)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix = matrix / np.maximum(norms, 1e-9)
    return ids, matrix


class SemanticHunter(BaseHunter):
    """P04-02：语义近似通道，**只消费库内已有向量**，查询向量离线构造。

    ## 取舍（能力边界，必须写明）
    GPU 推理机（bge-m3）与语音机器人共用、已明令停用，所以**不许**调 embedding 端点。
    没有查询编码器，就用"词面种子文章的向量质心"（`_pivot_query_vector`）当查询向量：
    它能把"与命中文章**语义相近但用词不同**"的文章捞回来（这是 §8.2 的核心诉求），
    但**捞不回**"与查询毫无词面交集"的文章（没有种子 → 没有质心）。这是明确的能力边界，
    不是"语义检索精度差"——真实语义召回要等嵌入服务解禁（或改用离线编码器）。

    `query_vector_provider` 是注入点：将来有查询编码器（或查询向量缓存）时，
    注册一个 `(request) -> np.ndarray|None` 即可切回真语义路径，本类其余逻辑不动。
    """

    hunter_id = HUNTER_SEMANTIC
    failure_policy = QA_FAILURE_FALLBACK
    fallback_hunter = HUNTER_BM25     # §28 例：Vector Hunter 超时 → fallback BM25

    def __init__(self, retriever: ArticleRetriever, *, database=None,
                 vector_loader: Callable | None = None,
                 query_vector_provider: Callable | None = None,
                 pool: RetrievalPool | None = None, vector_ttl_seconds: int = 120):
        self.retriever = retriever
        self.database = database if database is not None else getattr(retriever, "database", None)
        self._vector_loader = vector_loader
        self._query_vector_provider = query_vector_provider
        self._pool = pool
        self._vector_ttl = max(0, int(vector_ttl_seconds or 0))
        self._vectors = None
        self._vectors_ts = 0.0
        self._lock = threading.Lock()

    # -- 向量 ---------------------------------------------------------------
    def vectors(self):
        with self._lock:
            now = time.monotonic()
            if self._vectors is not None and (now - self._vectors_ts) < self._vector_ttl:
                return self._vectors
            loader = self._vector_loader
            if loader is None:
                ids, matrix = load_article_vectors(self.database)
            else:
                ids, matrix = loader()
            self._vectors = (ids or [], matrix)
            self._vectors_ts = now
            return self._vectors

    def _pool_for(self, request: HunterRequest) -> RetrievalPool:
        if request.pool is not None:
            return request.pool
        if self._pool is None:
            self._pool = RetrievalPool(self.retriever, request.industry_pack_id)
        return self._pool

    # -- 离线查询向量 -------------------------------------------------------
    def seed_weights(self, request: HunterRequest, rows: Sequence[Mapping]) -> dict:
        """词面种子：查询词命中标题 ×2 / 分类词·主题 ×1 / 正文 ×0.5，取权重最高的 N 篇。"""
        terms = [str(item) for item in (request.terms or ()) if str(item).strip()]
        if not terms:
            return {}
        weights = {}
        for row in rows:
            title = str(row.get("title") or "").casefold()
            keywords = " ".join(str(item) for item in _json(row.get("matched_keywords_json"), [])).casefold()
            topics = " ".join(str(item) for item in _json(row.get("topic_tags_json"), [])).casefold()
            body = str(row.get("content") or "")[:2000].casefold()
            weight = 0.0
            for term in terms:
                if term in title:
                    weight += 2.0
                if term in keywords or term in topics:
                    weight += 1.0
                if term in body:
                    weight += 0.5
            if weight > 0:
                weights[int(row.get("id") or 0)] = weight
        limit = semantic_seed_count()
        ordered = sorted(weights.items(), key=lambda item: (-item[1], item[0]))[:limit]
        return dict(ordered)

    def _pivot_query_vector(self, request: HunterRequest, ids: Sequence[int], matrix,
                            seeds: Mapping | None = None):
        """词面种子文章的向量质心（离线、确定性、可复算）。

        没有查询编码器时的替代口径：种子 = 词面命中的文章，质心 = 它们的向量按词面权重加权平均。
        返回 `(query_vector, stats)`；构造不出来时 query_vector 为 None 并给出原因码。
        """
        import numpy as np

        seeds = dict(seeds if seeds is not None
                     else self.seed_weights(request, self._pool_for(request).rows))
        if not seeds:
            return None, {"pivot": "lexical_centroid", "seeds": 0,
                          "reason": "no_lexical_seed"}
        position = {int(article_id): index for index, article_id in enumerate(ids)}
        chosen = [(position[article_id], weight) for article_id, weight in seeds.items()
                  if article_id in position]
        if not chosen:
            return None, {"pivot": "lexical_centroid", "seeds": len(seeds),
                          "seeds_with_vector": 0, "reason": "seed_has_no_vector"}
        vector = np.zeros(matrix.shape[1], dtype=np.float32)
        total = 0.0
        for index, weight in chosen:
            vector += matrix[index] * float(weight)
            total += float(weight)
        norm = float(np.linalg.norm(vector))
        if total <= 0 or norm <= 0:
            return None, {"pivot": "lexical_centroid", "seeds": len(seeds),
                          "seeds_with_vector": len(chosen), "reason": "degenerate_centroid"}
        return vector / norm, {"pivot": "lexical_centroid", "seeds": len(seeds),
                               "seeds_with_vector": len(chosen)}

    def run(self, request: HunterRequest) -> dict:
        started = time.monotonic()
        ids, matrix = self.vectors()
        if matrix is None or not len(ids):
            # 库里没有 ready 向量 → 可解释降级（§28 的 FALLBACK：舰队会退到 BM25）
            return hunter_outcome(
                self.hunter_id, status="degraded", reason_code="no_vectors",
                failure_policy=self.failure_policy,
                stats={"vectors": 0, "note": "库内没有 status='ready' 的文章向量，"
                                            "语义通道降级（不调用 embedding 端点）"})
        import numpy as np

        pool = self._pool_for(request)
        pool_rows = pool.rows
        pool_ids = {int(row.get("id") or 0) for row in pool_rows}
        if request.allowed_ids is not None:
            pool_ids &= set(request.allowed_ids)
        seeds = self.seed_weights(request, pool_rows)
        if self._query_vector_provider is not None:
            query_vector = self._query_vector_provider(request)
            pivot_stats = {"pivot": "injected_provider", "seeds": len(seeds)}
        else:
            query_vector, pivot_stats = self._pivot_query_vector(request, ids, matrix, seeds)
        if query_vector is None:
            return hunter_outcome(
                self.hunter_id, status="degraded",
                reason_code=pivot_stats.get("reason") or "no_query_vector",
                failure_policy=self.failure_policy,
                stats={"vectors": len(ids), **pivot_stats})
        query_vector = np.asarray(query_vector, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(query_vector))
        if norm <= 0:
            return hunter_outcome(
                self.hunter_id, status="degraded", reason_code="degenerate_query_vector",
                failure_policy=self.failure_policy,
                stats={"vectors": len(ids), **pivot_stats})
        similarities = matrix @ (query_vector / norm)
        order = np.argsort(-similarities)
        threshold = semantic_min_score()
        by_id = pool.by_id
        seed_ids = set(seeds)
        evidence, semantic_only = [], 0
        for index in order:
            article_id = int(ids[int(index)])
            if article_id not in pool_ids or article_id not in by_id:
                continue
            score = float(similarities[int(index)])
            if score < threshold:
                continue
            if len(evidence) >= max(1, request.limit):
                break
            if article_id not in seed_ids:
                semantic_only += 1
            evidence.append(_article_evidence(
                by_id[article_id], score=round(score * 100.0, 6), method="semantic",
                reason="向量语义近似（离线质心查询向量，余弦 %.3f）" % score))
        return hunter_outcome(
            self.hunter_id, status="ok" if evidence else "empty",
            reason_code="" if evidence else "no_semantic_hit",
            failure_policy=self.failure_policy, evidence=evidence,
            latency_ms=int((time.monotonic() - started) * 1000),
            stats={"vectors": len(ids), "pool_rows": len(pool_rows), **pivot_stats,
                   "semantic_only": semantic_only,
                   "note": "查询向量为离线质心（无嵌入端点调用）；语义通道无法召回"
                           "与查询毫无词面交集的文章"})


# ── Graph Hunter adapter（P04-03）──────────────────────────────────────────


class GraphHunter(BaseHunter):
    """P04-03：**适配器**，把既有 `qa_retrieval.graph_evidence()` 包成 Hunter。

    图逻辑（意图权重 event/attribute、属性边有效期过滤、边→证据成型）一行没重写；
    这里只加：统一契约、注入点（`builder`）、异常降级（图库不可用 → DEGRADE 而不是报错，
    对应 §28 的 "Graph DB unavailable → DEGRADE"）。
    """

    hunter_id = HUNTER_GRAPH
    failure_policy = QA_FAILURE_DEGRADE

    def __init__(self, *, builder=None, graph_runner: Callable | None = None):
        self.builder = builder
        self._runner = graph_runner

    def run(self, request: HunterRequest) -> dict:
        started = time.monotonic()
        limit = max(1, int(request.plan.get("graph_limit") or min(4, max(1, request.limit // 3))))
        runner = self._runner or graph_evidence
        result = runner(request.plan or {}, industry_pack_id=request.industry_pack_id,
                        limit=limit, builder=self.builder)
        stats = dict(result.get("stats") or {})
        evidence = list(result.get("evidence") or [])
        return hunter_outcome(
            self.hunter_id, status="ok" if evidence else "empty",
            reason_code="" if evidence else "graph_no_hit",
            failure_policy=self.failure_policy, evidence=evidence,
            latency_ms=int((time.monotonic() - started) * 1000), stats=stats)


# ── Structured / DB adapter（P04-04）───────────────────────────────────────


def normalize_structured_filters(raw: Mapping | None) -> dict:
    """确定性结构化过滤条件（时间/权威度/文档类型/域名/分类）。

    §8.4 Metadata Hunter 的口径（时间/科室/来源级别/人群/文档类型/版本）在本仓库落到
    `articles` / `article_intel_classifications` / `article_ragflow_documents` 的既有列上。
    只认白名单键，非法值一律忽略（宁少过滤，不误滤）。
    """
    raw = dict(raw or {})

    def _strings(*keys) -> list:
        out = []
        for key in keys:
            value = raw.get(key)
            if value in (None, ""):
                continue
            if isinstance(value, (list, tuple, set)):
                values = value
            else:
                values = str(value).replace(",", " ").split()
            for item in values:
                text = str(item or "").strip()
                if text and text not in out:
                    out.append(text)
        return out

    def _int(key, low, high):
        try:
            value = int(str(raw.get(key)).strip())
        except (TypeError, ValueError):
            return None
        return max(low, min(high, value))

    return {
        "doc_types": _strings("doc_type", "doc_types"),
        "domains": [item.casefold() for item in _strings("domain", "domains")],
        "categories": _strings("category", "categories", "final_category"),
        "min_authority": _int("min_authority", 0, 100),
        "source_roles": _strings("source_role", "source_roles"),
    }


class StructuredHunter(BaseHunter):
    """P04-04：结构化/业务库适配器。

    两个子来源，都复用既有代码：
      ① `policy_registry`：`ArticleRetriever._policy_exact_evidence()` —— 政策登记表
         （`article_ragflow_documents` 的 doc_no/issuer/effective_date/authority_level）精确命中，
         通道名沿用既有的 `policy_exact`；
      ② `metadata_gate`：`RetrievalPool` 候选池 + 白名单化的确定性过滤（权威度/文档类型/域名/分类）
         + 既有时间闸门（`apply_time_gate`）。

    外部业务库（SQL/API/EMR/HIS/LIS/PACS）按 D-002 抽象为"内部证据库"：本轮**不新增网络依赖**，
    只留 `structured_providers` 注入点 —— 注册一个 `(request) -> list[evidence]` 即可接入，
    接口与失败语义（DEGRADE、单源失败不影响其它源）已经在这里定好。
    """

    hunter_id = HUNTER_STRUCTURED
    failure_policy = QA_FAILURE_DEGRADE

    def __init__(self, retriever: ArticleRetriever, *, pool: RetrievalPool | None = None,
                 structured_providers: Iterable[Callable] | None = None):
        self.retriever = retriever
        self._pool = pool
        self.providers = list(structured_providers or [])

    def _pool_for(self, request: HunterRequest) -> RetrievalPool:
        if request.pool is not None:
            return request.pool
        if self._pool is None:
            self._pool = RetrievalPool(self.retriever, request.industry_pack_id)
        return self._pool

    def metadata_gate(self, request: HunterRequest, rows: Sequence[Mapping]) -> tuple[list, dict]:
        """确定性元数据闸门：只有**明确命中过滤条件**的行才产出证据。"""
        filters = normalize_structured_filters(request.structured_filters)
        window = _time_window_for(request)
        hard_filter = _env_flag("QA_TIME_HARD_FILTER", True)
        min_in_window = _env_int("QA_TIME_MIN_IN_WINDOW", 1, 1, 50)

        accepted = []
        for row in rows:
            reasons = []
            if filters["min_authority"] is not None:
                if int(row.get("authority_level") or 0) < int(filters["min_authority"]):
                    continue
                reasons.append("权威度≥%d" % int(filters["min_authority"]))
            if filters["doc_types"]:
                doc_type = str(row.get("policy_doc_type") or "").strip()
                if doc_type not in filters["doc_types"]:
                    continue
                reasons.append("文档类型=%s" % doc_type)
            if filters["domains"]:
                domain = str(row.get("domain") or "").casefold()
                if not any(item in domain for item in filters["domains"]):
                    continue
                reasons.append("来源域名命中")
            if filters["categories"]:
                category = str(row.get("final_category") or "").strip()
                if category not in filters["categories"]:
                    continue
                reasons.append("分类=%s" % category)
            if not reasons:
                # 没给出任何过滤条件 → 该子通道不产证据（避免变成"再来一遍关键词检索"）
                continue
            accepted.append((0.0, str(row.get("publish_date") or row.get("first_crawled") or ""),
                             int(row.get("id") or 0), row,
                             "结构化字段命中：" + "、".join(reasons), _article_day(row)))
        if not accepted:
            return [], {"filters": filters, "candidates": 0, "adopted": 0,
                        "time_gate": {"ladder_step": 0}}
        gated, gate_receipt = apply_time_gate(
            accepted, window, hard_filter=hard_filter, min_in_window=min_in_window)
        evidence, seen = [], set()
        for _score, _date, article_id, row, reason, _day in gated:
            if len(evidence) >= max(1, int(request.limit)):
                break
            if article_id in seen:
                continue
            seen.add(article_id)
            evidence.append(_article_evidence(
                row, score=float(row.get("authority_level") or 1), method="structured_metadata",
                reason=reason))
        return evidence, {"filters": filters, "candidates": len(accepted),
                          "adopted": len(evidence), "time_gate": gate_receipt}

    def run(self, request: HunterRequest) -> dict:
        started = time.monotonic()
        stats = {"policy_registry": {}, "metadata_gate": {}, "providers": []}
        evidence, seen = [], set()

        # ① 政策登记表（复用既有精确命中通道）
        try:
            policy_evidence, policy_audit = self.retriever._policy_exact_evidence(
                request.plan or {}, industry_pack_id=request.industry_pack_id,
                limit=min(6, max(1, int(request.limit))))
            stats["policy_registry"] = dict(policy_audit)
            for item in policy_evidence:
                article_id = int(item.get("article_id") or 0)
                if article_id and article_id in seen:
                    continue
                if article_id:
                    seen.add(article_id)
                evidence.append(item)
        except Exception as exc:
            stats["policy_registry"] = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}

        # ② 元数据闸门（既有候选池 + 确定性过滤）
        try:
            rows, _excluded = self._pool_for(request).load()
            meta_evidence, meta_stats = self.metadata_gate(request, rows)
            stats["metadata_gate"] = dict(meta_stats, **self._pool_for(request).stats())
            for item in meta_evidence:
                article_id = int(item.get("article_id") or 0)
                if article_id and article_id in seen:
                    continue
                if article_id:
                    seen.add(article_id)
                evidence.append(item)
        except Exception as exc:
            stats["metadata_gate"] = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}

        # ③ 外部结构化源（注入点；单源失败不影响其它源）
        for provider in self.providers:
            name = getattr(provider, "__name__", provider.__class__.__name__)
            try:
                items = list(provider(request) or [])
                stats["providers"].append({"name": name, "items": len(items)})
                for item in items:
                    if isinstance(item, Mapping):
                        evidence.append(dict(item))
            except Exception as exc:
                stats["providers"].append({"name": name, "error": str(exc)[:160]})

        return hunter_outcome(
            self.hunter_id, status="ok" if evidence else "empty",
            reason_code="" if evidence else "no_structured_hit",
            failure_policy=self.failure_policy, evidence=evidence,
            latency_ms=int((time.monotonic() - started) * 1000), stats=stats)


# ── Query Expansion Hunter（P04-05）───────────────────────────────────────


class QueryExpansionHunter(BaseHunter):
    """P04-05：只生成"同义词/缩写/上下位概念/专业术语/相关实体"，**不产证据、不做回答**。

    词源（每条都带 `source`，可追溯）：
      · `original` / `normalize` / `traditional` / `entity_alias`：`qa_query_normalize.expand_terms`
      · `token` / `anchor`：`qa_retrieval._terms` / `_semantic_anchor_terms`（jieba + 行业词表）
      · `graph_neighbor`：`kg_builder.KnowledgeGraphBuilder.neighborhood()`（图谱邻居 = 相关实体/上下位）
    """

    hunter_id = HUNTER_QUERY_EXPANSION
    failure_policy = QA_FAILURE_SKIP

    def __init__(self, *, builder=None, max_terms: int | None = None,
                 graph_neighbor_limit: int = 6):
        self.builder = builder
        self.max_terms = max_terms
        self.graph_neighbor_limit = max(0, int(graph_neighbor_limit or 0))

    def graph_neighbors(self, request: HunterRequest, terms: Sequence[str]) -> tuple[list, dict]:
        """相关实体：对最靠前的几个主词在图谱里取 1 跳邻域标签。"""
        stats = {"enabled": False, "queries": 0, "neighbors": 0, "graph_note": ""}
        if self.graph_neighbor_limit <= 0:
            stats["graph_note"] = "图谱邻居已关闭（graph_neighbor_limit=0）"
            return [], stats
        seeds = [str(item) for item in terms if str(item).strip()][:3]
        if not seeds:
            stats["graph_note"] = "没有问题主词"
            return [], stats
        try:
            from kg_builder import KnowledgeGraphBuilder

            graph = self.builder or KnowledgeGraphBuilder()
        except Exception as exc:
            stats["graph_note"] = "图构建器不可用：%s" % str(exc)[:80]
            return [], stats
        found, labels = [], []
        for seed in seeds:
            try:
                result = graph.neighborhood(seed, pack_id=request.industry_pack_id, depth=1,
                                            limit=self.graph_neighbor_limit)
            except Exception as exc:
                stats["graph_note"] = "邻域查询失败：%s" % str(exc)[:80]
                continue
            stats["enabled"] = True
            stats["queries"] += 1
            for node in result.get("neighbors") or []:
                label = str(node.get("label") or node.get("node_key") or "").strip()
                if label and label not in labels:
                    labels.append(label)
        stats["neighbors"] = len(labels)
        return labels[: self.graph_neighbor_limit], stats

    def run(self, request: HunterRequest) -> dict:
        started = time.monotonic()
        source = list(request.queries) or [request.question]
        terms: list = []
        seen = set()

        def _add(term: str, origin: str) -> None:
            text = str(term or "").strip()
            key = text.casefold()
            if not text or key in seen:
                return
            seen.add(key)
            terms.append({"term": text, "source": origin})

        # 词源按"信息量"排序入表：别名/归一 > 图谱邻居（相关实体，最稀缺） > 原查询 >
        # 行业锚点 > jieba 实词。截断只砍尾部（jieba 的 2 字碎片），不砍稀缺来源。
        try:
            from qa_query_normalize import expand_query

            payload = expand_query(" ".join(str(item) for item in source))
            _add(payload.get("normalized"), "normalize")
            _add(payload.get("simplified"), "normalize")
            for term in payload.get("entity_terms") or []:
                _add(term, "entity_alias")
        except Exception:
            pass
        neighbors, graph_stats = self.graph_neighbors(request, list(request.terms or ())[:3])
        for label in neighbors:
            _add(label, "graph_neighbor")
        for item in source:
            _add(item, "original")
        for term in request.anchor_terms or ():
            _add(term, "anchor")
        for term in request.terms or ():
            _add(term, "token")

        cap = int(self.max_terms or expansion_max_terms())
        terms = terms[:cap]
        # 扩展检索式：只把"新增词"拼成补充查询，不改原查询（调用方自己决定用不用）
        extra = [item["term"] for item in terms if item["source"] != "original"]
        queries = []
        for item in source:
            text = str(item)
            addition = " ".join(term for term in extra[:6] if term.casefold() not in text.casefold())
            queries.append(("%s %s" % (text, addition)).strip() if addition else text)
        return hunter_outcome(
            self.hunter_id, status="ok" if terms else "empty",
            reason_code="" if terms else "no_expansion",
            failure_policy=self.failure_policy,
            evidence=[],                       # §8.5：本 Hunter 绝不产证据
            queries=queries, terms=terms,
            latency_ms=int((time.monotonic() - started) * 1000),
            stats={**graph_stats, "term_sources": _source_histogram(terms),
                   "new_terms": len(extra), "note": "只产词，不产证据（§8.5）"})


def _source_histogram(terms: Sequence[Mapping]) -> dict:
    histogram: dict = {}
    for item in terms or []:
        key = str(item.get("source") or "")
        histogram[key] = histogram.get(key, 0) + 1
    return histogram


__all__ = [
    "BaseHunter", "BM25Hunter", "GraphHunter", "HunterRequest", "QueryExpansionHunter",
    "RetrievalPool", "SemanticHunter", "StructuredHunter",
    "bm25_b", "bm25_k1", "bm25_scores", "hunter_outcome", "load_article_vectors",
    "normalize_structured_filters", "time_window_receipt",
]
