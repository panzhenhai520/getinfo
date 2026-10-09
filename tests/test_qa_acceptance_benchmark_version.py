#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""benchmark 冻结与指标口径的守门测试（Phase 00 · F-3）。

覆盖四件事（外加两条：留档行带版本、cost 聚合/降级）：
  1. 问题集两形态解析：旧 list 形态与冻结对象形态解析出**同样的题目列表**；
  2. summary 里必须有 benchmark_version（未冻结就写 legacy-unversioned，不许缺字段）；
  3. 分母为 0 时 recall_at_k / citation_precision / citation_recall 输出 None 而不是 0
     （同时验证"真的有 0"的情况仍然是 0.0，别把 0 和 null 混了）；
  4. 新增字段不破坏既有字段（原 summary 的键一个不少），且 evaluate() 真的把新字段接上了。

测试纪律：**不连真库**——检索器用假的、cost 注入或喂内存库，
避免测试结果随本机语料漂移（会连真库的只有 evaluate，所以它必须被打桩）。
"""
import json
import os
import sqlite3
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.qa_retrieval_acceptance as acceptance  # noqa: E402

# 既有 summary 字段：一个都不许少（F-3 只允许"加"，不允许"删改"）
EXISTING_SUMMARY_KEYS = (
    "questions", "hit_rate", "traceable_rate", "grounded_rate", "avg_evidence",
    "graph_rate", "errors", "avg_ms", "by_kind",
)
# F-3 新增字段
NEW_SUMMARY_KEYS = (
    "benchmark_version", "benchmark_format", "benchmark_questions_file",
    "benchmark_generated_at", "corpus_snapshot_id", "pack_filter",
    "k", "recall_at_k", "recall_at_k_note",
    "citation_precision", "citation_recall",
    "citation_evidence_total", "citation_evidence_matched",
    "citation_questions_labeled", "citation_questions_matched", "cost",
)

QUESTIONS = [
    {"id": "q1", "question": "比亚迪最近有什么动态？", "kind": "event",
     "industry_pack_id": "automotive_industry", "expect_terms": ["比亚迪"]},
    {"id": "q2", "question": "低延迟模型的应用场景是什么？", "kind": "attribute",
     "industry_pack_id": "ai_news", "expect_terms": ["低延迟模型"]},
]


def _row(**overrides) -> dict:
    """一条逐题明细（默认全 0/False）：让汇总口径可以脱库测试。"""
    row = {
        "id": "q1", "question": "问题", "kind": "event", "industry_pack_id": "pack",
        "evidence": 0, "hit": False, "traceable": False, "traceable_ratio": 0.0,
        "grounded": False, "terms": [], "graph": 0, "ms": 0,
        "expect_terms_count": 0, "expect_terms_hit": 0,
        "cited_total": 0, "cited_matched": 0, "citation_hit": False,
    }
    row.update(overrides)
    return row


def _cost(**overrides) -> dict:
    """注入用的 cost（默认没数据，避免 summarize 去读真库）。"""
    cost = {
        "tokens_in": None, "tokens_out": None, "tokens_total": None,
        "runs_counted": 0, "stage_rows": 0, "stage_rows_with_tokens": 0,
        "source": "qa_stage_runs.token_usage_json", "degraded": False, "note": "test",
    }
    cost.update(overrides)
    return cost


# ---------------------------------------------------------------------------
# 1) 两形态解析
# ---------------------------------------------------------------------------
def test_two_forms_parse_to_same_questions(tmp_path):
    """list 形态与对象形态必须解析出同一份题目列表（向后兼容）。"""
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(json.dumps(QUESTIONS, ensure_ascii=False), encoding="utf-8")
    frozen_path = tmp_path / "frozen.json"
    frozen_path.write_text(json.dumps({
        "benchmark_version": "qa-acceptance-v1",
        "corpus_snapshot_id": "pending:first-run",
        "generated_at": "2026-10-08T23:48:58Z",
        "questions": QUESTIONS,
    }, ensure_ascii=False), encoding="utf-8")

    legacy_questions, legacy_meta = acceptance.load_questions(str(legacy_path))
    frozen_questions, frozen_meta = acceptance.load_questions(str(frozen_path))

    assert legacy_questions == QUESTIONS
    assert frozen_questions == QUESTIONS
    assert legacy_questions == frozen_questions, "两形态的题目列表必须一致"
    # 题目内容之外，元数据要如实反映"冻结没冻结"
    assert legacy_meta["benchmark_version"] == acceptance.LEGACY_BENCHMARK_VERSION == "legacy-unversioned"
    assert legacy_meta["benchmark_format"] == "legacy-list"
    assert legacy_meta["corpus_snapshot_id"] is None
    assert frozen_meta["benchmark_version"] == "qa-acceptance-v1"
    assert frozen_meta["benchmark_format"] == "object"
    assert frozen_meta["corpus_snapshot_id"] == "pending:first-run"
    assert frozen_meta["benchmark_generated_at"] == "2026-10-08T23:48:58Z"
    # parse_questions 与 load_questions 同口径（内存对象也认）
    assert acceptance.parse_questions(list(QUESTIONS))[0] == QUESTIONS
    # 既不是 list 也不是 dict → 明确报错，不静默当成空题目集
    try:
        acceptance.parse_questions("不是问题集")
    except ValueError:
        pass
    else:
        raise AssertionError("非法形态必须抛 ValueError")


def test_repo_questions_file_is_frozen_object_form():
    """仓库里的真问题集：必须是冻结对象形态，且题目一个不少（内容由 F-3 冻结）。"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config", "qa_acceptance_questions.json")
    questions, meta = acceptance.load_questions(path)
    assert meta["benchmark_format"] == "object"
    assert meta["benchmark_version"] == "qa-acceptance-v1"
    assert meta["corpus_snapshot_id"], "必须有语料快照绑定（占位也算，不能为空）"
    assert meta["benchmark_generated_at"], "必须有生成时间"
    assert len(questions) == 12, "F-3 冻结的 12 道题不许被改动"
    assert [q["id"] for q in questions] == [
        "e1", "e2", "a3", "a4", "a5", "a6", "t7", "t8", "t9", "t10", "t11", "t12"]
    assert all(str(q.get("question") or "").strip() for q in questions)


# ---------------------------------------------------------------------------
# 2) benchmark_version 必须在 summary 里
# ---------------------------------------------------------------------------
def test_summary_carries_benchmark_version():
    summary = acceptance.summarize(
        [_row()], limit=7, cost=_cost(),
        benchmark_meta={"benchmark_version": "qa-acceptance-v1",
                        "benchmark_format": "object",
                        "corpus_snapshot_id": "pending:first-run",
                        "benchmark_generated_at": "2026-10-08T23:48:58Z"})
    assert summary["benchmark_version"] == "qa-acceptance-v1"
    assert summary["corpus_snapshot_id"] == "pending:first-run"
    assert summary["benchmark_generated_at"] == "2026-10-08T23:48:58Z"
    assert summary["k"] == 7, "K 口径要显式落库"


def test_summary_never_lacks_benchmark_version():
    """没有元数据（旧调用方）也要有版本号，且如实写 legacy-unversioned，不假装有版本。"""
    summary = acceptance.summarize([_row()], limit=12, cost=_cost())
    assert summary["benchmark_version"] == "legacy-unversioned"
    assert summary["benchmark_format"] == acceptance.INLINE_BENCHMARK_FORMAT
    assert summary["corpus_snapshot_id"] is None


# ---------------------------------------------------------------------------
# 3) 分母为 0 → null（不许用 0 冒充）
# ---------------------------------------------------------------------------
def test_zero_denominator_is_null_not_zero():
    """没有任何 expect_terms 弱标注时，三个比率都必须是 None 并在 note 里说明。"""
    rows = [
        _row(id="q1", evidence=3, hit=True, graph=1, terms=["比亚迪"],
             expect_terms_count=0, expect_terms_hit=0),
    ]
    summary = acceptance.summarize(rows, limit=5, cost=_cost())
    assert summary["recall_at_k"] is None
    assert summary["citation_precision"] is None
    assert summary["citation_recall"] is None
    # 计数口径也必须是 0 分母（不是"算不出来就填 0 结果"）
    assert summary["recall_at_k_terms"] == 0
    assert summary["citation_evidence_total"] == 0
    assert summary["citation_questions_labeled"] == 0
    # 空题目集同理
    empty = acceptance.summarize([], limit=5, cost=_cost())
    assert empty["recall_at_k"] is None
    assert empty["citation_precision"] is None
    assert empty["citation_recall"] is None
    # note 要写明"本次分母为 0 → null"，而不是只写通用口径
    for key in ("recall_at_k_note", "citation_precision_note", "citation_recall_note"):
        assert "null" in summary[key]
    assert "本次分母为 0" in summary["citation_precision_note"]
    assert "本次分母为 0" in summary["citation_recall_note"]
    assert "本次分母为 0" in summary["recall_at_k_note"]


def test_real_zero_is_zero_and_real_one_is_one():
    """分母 > 0 时：真命中给 1.0，真没命中给 0.0（0 与 null 不能混）。"""
    hit = acceptance.summarize(
        [_row(id="q1", expect_terms_count=1, expect_terms_hit=1, cited_total=2,
              cited_matched=1, citation_hit=True, evidence=2, hit=True)],
        limit=12, cost=_cost())
    assert hit["recall_at_k"] == 1.0          # 标注 1 个期待词且命中 1 个 → 1.0（K 只管取多少条，不管分母）
    assert hit["recall_at_k_terms_hit"] == 1
    assert hit["citation_precision"] == 0.5   # 2 条采纳证据里 1 条含期待词
    assert hit["citation_recall"] == 1.0

    miss = acceptance.summarize(
        [_row(id="q1", expect_terms_count=1, expect_terms_hit=0, cited_total=2,
              cited_matched=0, citation_hit=False, evidence=2, hit=True)],
        limit=12, cost=_cost())
    assert miss["recall_at_k"] == 0.0
    assert miss["citation_precision"] == 0.0
    assert miss["citation_recall"] == 0.0
    assert miss["recall_at_k"] is not None and miss["citation_recall"] is not None


def test_unlabeled_questions_stay_out_of_weak_label_denominator():
    """没标注的题目不进弱标注分母（否则会白扣 precision/recall）。"""
    rows = [
        _row(id="q1", expect_terms_count=1, expect_terms_hit=1, cited_total=1,
             cited_matched=1, citation_hit=True, evidence=1, hit=True),
        _row(id="q2", expect_terms_count=0, cited_total=9, cited_matched=0,
             citation_hit=False, evidence=9, hit=True),
    ]
    summary = acceptance.summarize(rows, limit=12, cost=_cost())
    assert summary["citation_evidence_total"] == 1, "未标注题目的 9 条证据不许进 precision 分母"
    assert summary["citation_precision"] == 1.0
    assert summary["citation_recall"] == 1.0
    assert summary["recall_at_k"] == 1.0
    assert summary["citation_questions_labeled"] == 1


# ---------------------------------------------------------------------------
# 4) 新字段不破坏既有字段（含 evaluate 全链路）
# ---------------------------------------------------------------------------
def test_new_fields_do_not_break_existing_summary_keys():
    rows = [
        _row(id="q1", evidence=2, hit=True, traceable=True, traceable_ratio=1.0,
             grounded=True, graph=1, ms=10, expect_terms_count=1, expect_terms_hit=1,
             cited_total=2, cited_matched=1, citation_hit=True),
        _row(id="q2", kind="attribute", evidence=0, hit=False, ms=20,
             expect_terms_count=1, expect_terms_hit=0),
    ]
    summary = acceptance.summarize(rows, limit=12, cost=_cost())
    for key in EXISTING_SUMMARY_KEYS:
        assert key in summary, "既有字段 %s 被删了" % key
    for key in NEW_SUMMARY_KEYS:
        assert key in summary, "新字段 %s 缺失" % key
    assert summary["questions"] == 2
    assert summary["hit_rate"] == 0.5
    assert summary["by_kind"]["event"]["questions"] == 1
    assert summary["by_kind"]["attribute"]["questions"] == 1
    assert set(summary["cost"]) >= {"tokens_in", "tokens_out", "tokens_total", "runs_counted"}


class _FakeCursor:
    """假游标：evaluate 只用它解析证据引用，这里让所有引用都解析不到（走外部 URL 兜底）。"""

    def execute(self, *args, **kwargs):
        return None

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        return None


class _FakeConnection:
    def cursor(self):
        return _FakeCursor()


class _FakeDatabase:
    """只提供 evaluate 用到的最小接口：_ensure_connection / lock / connection。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.connection = _FakeConnection()

    def _ensure_connection(self):
        return None


class _FakeRetriever:
    """假检索器：q1 能给到含"比亚迪"的证据，q2 只给无关证据。"""

    def __init__(self, database=None):
        self.database = database

    def retrieve(self, plan, industry_pack_id="", limit=12):
        question = str(plan.get("question") or "")
        if "比亚迪" in question:
            return {"evidence": [{
                "evidence_ref": "web:1",
                "source_url": "https://example.com/1",
                "title": "比亚迪发布新款车型",
                "content_excerpt": "比亚迪在发布会上宣布新款车型上市",
                "source_type": "web",
            }]}
        return {"evidence": [{
            "evidence_ref": "web:2",
            "source_url": "https://example.com/2",
            "title": "与问题无关的新闻",
            "content_excerpt": "正文和期待词没有交集",
            "source_type": "web",
        }]}


def test_evaluate_wires_new_fields_without_touching_old_ones(monkeypatch):
    """evaluate 全链路（不连真库）：既有字段照旧，新字段真的算出来了。"""
    import qa_retrieval

    monkeypatch.setattr(qa_retrieval, "ArticleRetriever", _FakeRetriever)
    monkeypatch.setattr(acceptance, "sqlite_db", _FakeDatabase())

    outcome = acceptance.evaluate(
        QUESTIONS, limit=3, verbose=False, cost=_cost(),
        benchmark_meta={"benchmark_version": "qa-acceptance-v1",
                        "benchmark_format": "object",
                        "benchmark_questions_file": "config/qa_acceptance_questions.json",
                        "corpus_snapshot_id": "pending:first-run"})
    summary = outcome["summary"]

    # 既有字段照旧
    assert summary["questions"] == 2
    assert summary["hit_rate"] == 1.0
    assert summary["traceable_rate"] == 1.0
    assert summary["errors"] == 0
    assert summary["by_kind"]["event"]["questions"] == 1
    # 新字段
    assert summary["benchmark_version"] == "qa-acceptance-v1"
    assert summary["benchmark_questions_file"] == "config/qa_acceptance_questions.json"
    assert summary["k"] == 3, "K 必须是本次实际使用的 --limit"
    assert summary["recall_at_k"] == 0.5, "2 个期待词命中 1 个"
    assert summary["recall_at_k_terms"] == 2
    assert summary["recall_at_k_terms_hit"] == 1
    assert summary["citation_precision"] == 0.5, "2 条采纳证据里 1 条含期待词"
    assert summary["citation_recall"] == 0.5, "2 道有期待词的题里 1 道命中"
    assert summary["citation_questions_labeled"] == 2
    # 逐题明细也带上了新的弱标注计数（便于复盘是哪道题拖后腿）
    first = outcome["results"][0]
    assert first["expect_terms_count"] == 1
    assert first["expect_terms_hit"] == 1
    assert first["cited_total"] == 1
    assert first["cited_matched"] == 1
    assert outcome["results"][1]["cited_matched"] == 0


# ---------------------------------------------------------------------------
# 附加：cost 聚合 / 降级；留档行带版本 + 快照
# ---------------------------------------------------------------------------
class _StubDatabase:
    """包一个真 sqlite3 连接，只为测 cost 聚合（不碰真库）。"""

    def __init__(self, connection, *, fail=False):
        self.lock = threading.Lock()
        self.connection = connection
        self._fail = fail

    def _ensure_connection(self):
        if self._fail:
            raise RuntimeError("database is locked")


def test_cost_is_null_when_table_or_column_missing(monkeypatch):
    """qa_stage_runs 缺表 / 缺列 → 降级成 null + note，绝不用 0 冒充。"""
    empty = sqlite3.connect(":memory:")  # 没建表
    monkeypatch.setattr(acceptance, "sqlite_db", _StubDatabase(empty))
    missing_table = acceptance._cost_summary()
    assert missing_table["degraded"] is True
    assert missing_table["tokens_in"] is None
    assert missing_table["tokens_out"] is None
    assert missing_table["tokens_total"] is None
    assert missing_table["runs_counted"] == 0
    assert "降级" in missing_table["note"]

    no_column = sqlite3.connect(":memory:")  # 有表但缺 token_usage_json 列
    no_column.execute("CREATE TABLE qa_stage_runs (id INTEGER PRIMARY KEY, run_id TEXT)")
    monkeypatch.setattr(acceptance, "sqlite_db", _StubDatabase(no_column))
    assert acceptance._cost_summary()["tokens_total"] is None

    # 读库直接抛异常（库挂了）也要降级，不许把异常冒出去打断验收
    monkeypatch.setattr(acceptance, "sqlite_db", _StubDatabase(None, fail=True))
    assert acceptance._cost_summary()["degraded"] is True


def test_cost_sums_token_usage_per_run(monkeypatch):
    """三种 token 键名都要认；同 run 多 stage 累加；没 token 的行不计入 runs_counted。"""
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE qa_stage_runs (id INTEGER PRIMARY KEY, run_id TEXT, stage TEXT,"
        " token_usage_json TEXT)")
    connection.executemany(
        "INSERT INTO qa_stage_runs(run_id, stage, token_usage_json) VALUES (?,?,?)",
        [
            ("r1", "level1_draft", json.dumps({"tokens_in": 10, "tokens_out": 4})),
            ("r1", "synthesis", json.dumps({"input_tokens": 5, "output_tokens": 1})),
            ("r2", "level1_draft", json.dumps({"usage": {"prompt_tokens": 7, "completion_tokens": 2}})),
            ("r3", "level1_draft", "{}"),
            ("r4", "level1_draft", "不是 JSON"),
        ])
    connection.commit()
    monkeypatch.setattr(acceptance, "sqlite_db", _StubDatabase(connection))

    cost = acceptance._cost_summary()
    assert cost["degraded"] is False
    assert (cost["tokens_in"], cost["tokens_out"], cost["tokens_total"]) == (22, 7, 29)
    assert cost["runs_counted"] == 2, "r1/r2 有 token 记录；r3/r4 没有，不许算进去"
    assert cost["stage_rows"] == 5
    assert cost["stage_rows_with_tokens"] == 3
    assert cost["source"] == "qa_stage_runs.token_usage_json"


def test_cost_zero_rows_keeps_zero_but_says_so(monkeypatch):
    """表存在但没行 → 如实 0，并在 note 里说明"表存在、确无记录"（区别于降级）。"""
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE qa_stage_runs (run_id TEXT, token_usage_json TEXT)")
    monkeypatch.setattr(acceptance, "sqlite_db", _StubDatabase(connection))
    cost = acceptance._cost_summary()
    assert cost["degraded"] is False
    assert cost["tokens_total"] == 0
    assert cost["runs_counted"] == 0
    assert "0 行" in cost["note"]


def test_history_record_carries_benchmark_version_and_corpus_snapshot(tmp_path, monkeypatch):
    """--save-history 落盘行必须带 benchmark_version + corpus_snapshot_id。"""
    monkeypatch.setattr(acceptance, "_corpus_snapshot",
                        lambda: {"coverage": {"denominator_all": 3}, "nodes": 2})
    outcome = {"summary": acceptance.summarize(
        [_row(id="q1", expect_terms_count=1, expect_terms_hit=1, cited_total=1,
              cited_matched=1, citation_hit=True, evidence=1, hit=True)],
        limit=5, cost=_cost(),
        benchmark_meta={"benchmark_version": "qa-acceptance-v1",
                        "corpus_snapshot_id": "pending:first-run"})}
    path = tmp_path / "history.jsonl"
    record = acceptance._append_history(str(path), outcome)

    assert record["benchmark_version"] == "qa-acceptance-v1"
    assert record["corpus_snapshot_id"].startswith("corpus:")
    assert record["benchmark_corpus_snapshot_id"] == "pending:first-run"
    assert record["benchmark_corpus_snapshot_state"] == "pending"
    assert record["recorded_at_utc"].endswith("Z")

    line = json.loads(path.read_text(encoding="utf-8").strip())
    assert line["benchmark_version"] == "qa-acceptance-v1"
    assert line["corpus_snapshot_id"] == record["corpus_snapshot_id"]
    assert line["summary"]["benchmark_version"] == "qa-acceptance-v1"
    assert line["summary"]["corpus_snapshot_id"] == "pending:first-run"
    assert line["corpus"] == record["corpus"]
    # 快照 id 必须可复算（同内容同 id），否则历史之间没法比对语料
    assert acceptance._snapshot_id({"nodes": 2}) == acceptance._snapshot_id({"nodes": 2})
    assert acceptance._snapshot_id({"nodes": 2}) != acceptance._snapshot_id({"nodes": 3})
