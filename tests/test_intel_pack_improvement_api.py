#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""行业包【主题文章入库情况评估】+【改进】接口测试。

覆盖点（对应用户验收清单）：
  ① 评估引擎不可用/超时：接口一律不 500（评估卡 200 + success=false，写接口 503）
  ② GET /improvements 默认只返回 status=verified，非 verified 带 hidden_from_review=true
  ③ apply 缺 confirm / plan_sha256 → 400（不允许一键直发）
  ④ apply 成功路径：先 activation.preview → activate → mark_applied（打桩断言）
  ⑤ reject：写 reason（引擎 mark_rejected 钩子 + 引擎缺失时的临时兜底 SQL）
  ⑥ 发布后复测：assess_pack(refresh) → record_after_apply(metrics.before/after/delta)
"""

import threading
import unittest
from unittest.mock import patch

from flask import Flask

import intel_api
from intel_api import intel_bp

PACK_ID = "ai_news"
SUGGESTION_ID = "sg-verified-1"


class _LoaderStub:
    """行业包加载器打桩：只要求 load() 不抛错。"""

    def load(self, pack_id):  # noqa: D102 - 测试桩
        return {"id": pack_id}


class _FakeCursor:
    def __init__(self, log):
        self._log = log

    def execute(self, sql, params):
        self._log.append((sql, params))

    def fetchall(self):
        return []

    def close(self):
        pass


class _FakeConnection:
    def __init__(self):
        self.log = []
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return _FakeCursor(self.log)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class _FakeDb:
    """假库：用于兜底 SQL 路径断言，不碰真实数据库。"""

    def __init__(self):
        self.connection = _FakeConnection()
        self.lock = threading.Lock()

    def _ensure_connection(self):
        return True


class _RepoStub:
    def __init__(self, db=None):
        self.db = db

    def active_industry_pack_id(self):
        return PACK_ID


class _ActivationStub:
    """activation 服务打桩：preview 固定计划，activate 记录调用。"""

    def __init__(self, plan_sha256="plan-sha-abc", target_version_id=7):
        self.plan_sha256 = plan_sha256
        self.target_version_id = target_version_id
        self.preview_calls = []
        self.activate_calls = []

    def preview(self, target_pack_id, **kwargs):
        self.preview_calls.append((target_pack_id, kwargs))
        return {
            "previous_pack_id": target_pack_id,
            "previous_version_id": 3,
            "target_pack_id": target_pack_id,
            "target_version_id": self.target_version_id,
            "plan_sha256": self.plan_sha256,
            "source_counts": {},
        }

    def activate(self, target_pack_id, *, target_version_id, expected_plan_sha256, actor="", **kwargs):
        self.activate_calls.append(
            {
                "target_pack_id": target_pack_id,
                "target_version_id": target_version_id,
                "expected_plan_sha256": expected_plan_sha256,
                "actor": actor,
            }
        )
        return {
            "activation_id": "act-0001",
            "active_pack_id": target_pack_id,
            "active_version_id": target_version_id,
            "plan_sha256": expected_plan_sha256,
        }


class _EngineStub:
    """评估引擎打桩（字段与真实 intel_pack_improvement.py 一致：比率一律是百分数 0~100）。"""

    def __init__(self, suggestions=None, staged_status="verified", staged_reason="影子自测通过"):
        self.suggestions = suggestions or {}
        self.staged_status = staged_status
        self.staged_reason = staged_reason
        self.calls = []
        self.mark_applied_calls = []
        self.record_after_apply_calls = []
        self.mark_rejected_calls = []
        # 形状照抄 assess_pack() 的真实返回
        self.assess_result = {
            "pack_id": PACK_ID,
            "pack_version": "2.0.3",
            "assessed_at": "2026-10-11T10:00:00Z",
            "articles": 1200,
            "categories": {"trend": 6, "event": 5, "other": 1189},
            "other_pct": 97.7,
            "peer_other_pct": 43.18,
            "peer_pack_count": 7,
            "gate_failures": {
                "total": 96,
                "counts": {"no_anchor": 91, "no_signal": 5},
                "buckets": [
                    {
                        "key": "no_anchor",
                        "label": "未命中行业包锚点词（anchor/core/expanded 全未命中）",
                        "count": 91,
                        "pct": 94.79,
                        "fixable_by_keywords": True,
                        "examples": [],
                    },
                    {
                        "key": "no_signal",
                        "label": "命中锚点但未命中趋势/事件信号",
                        "count": 5,
                        "pct": 5.21,
                        "fixable_by_keywords": False,
                        "examples": [],
                    },
                ],
            },
            "sources": [
                {
                    "source_id": 65,
                    "source_name": "工业和信息化部",
                    "source_url": "https://www.miit.gov.cn",
                    "article_count": 36,
                    "admitted_count": 0,
                    "admitted_pct": 0.0,
                    "last_article_at": "2026-10-05 16:00:00",
                    "verdict": "准入率低",
                },
                {
                    "source_id": 9,
                    "source_name": "停更站点",
                    "source_url": "https://example.com/feed",
                    "article_count": 0,
                    "admitted_count": 0,
                    "admitted_pct": 0.0,
                    "last_article_at": "",
                    "verdict": "零产出",
                },
            ],
            "source_verdict_counts": {"准入率低": 1, "零产出": 1},
            "candidates": {"core_keywords": [], "entity_keywords": [], "anchors": []},
            "thresholds": {"min_admit_rate_gain_pct": 15.0, "min_admit_rate_after_pct": 30.0},
        }

    def assess_pack(self, pack_id, *, sample_limit=500):
        self.calls.append(("assess_pack", pack_id, sample_limit))
        return dict(self.assess_result)

    def run_self_test_and_stage(self, pack_id, *, sample_limit=400):
        self.calls.append(("run_self_test_and_stage", pack_id, sample_limit))
        return {
            "suggestion_id": SUGGESTION_ID,
            "status": self.staged_status,
            "reason": self.staged_reason,
            "metrics": {
                "before": {"admit_rate": 42.0, "false_positive": 11.0, "topic_assoc": 38.0, "other_pct": 97.7},
                "after": {"admit_rate": 61.0, "false_positive": 9.0, "topic_assoc": 55.0, "other_pct": 62.0},
                "delta": {"admit_rate": 19.0, "false_positive": -2.0, "topic_assoc": 17.0, "other_pct": -35.7},
                "crawl_probe": {
                    "fetched_total": 12,
                    "parsed_total": 9,
                    "parse_failed_total": 3,
                    "new_article_admit_rate_before": 0.0,
                    "new_article_admit_rate_after": 33.33,
                    "sources": [
                        {
                            "source_name": "汽车之家",
                            "fetched": 4,
                            "parse_failed": 1,
                            "admit_rate_before": 0.0,
                            "admit_rate_after": 33.33,
                            "listing_status": "ok",
                        }
                    ],
                },
            },
        }

    def list_suggestions(self, pack_id, *, status=""):
        self.calls.append(("list_suggestions", pack_id, status))
        return [dict(item) for item in self.suggestions.values()]

    def get_suggestion(self, suggestion_id):
        item = self.suggestions.get(suggestion_id)
        return dict(item) if item else None

    def mark_applied(self, suggestion_id, *, activation_result=None):
        self.mark_applied_calls.append((suggestion_id, activation_result))
        return {"suggestion_id": suggestion_id, "status": "applied"}

    def record_after_apply(self, suggestion_id, metrics):
        self.record_after_apply_calls.append((suggestion_id, metrics))
        return {"suggestion_id": suggestion_id, "status": "applied", "verified_after": True}

    def mark_rejected(self, suggestion_id, *, reason=""):
        self.mark_rejected_calls.append((suggestion_id, reason))
        return {"suggestion_id": suggestion_id, "status": "rejected", "reason": reason}


class _NoRejectEngine(_EngineStub):
    """没有 mark_rejected 钩子的引擎：走【临时降级路径】兜底 SQL。

    钩子置 None：接口层用 callable(getattr(...)) 判断，None 即视为「没提供」。
    """

    mark_rejected = None
    reject_suggestion = None
    update_suggestion_status = None
    set_suggestion_status = None


class _PrepareHookEngine(_EngineStub):
    """提供「把候选词写进新发布版本」可选钩子的引擎（冻结接口之外的能力）。"""

    def __init__(self, *args, target_version_id=11, **kwargs):
        super().__init__(*args, **kwargs)
        self.prepared = []
        self.prepared_version_id = target_version_id

    def prepare_pack_version(self, pack_id, suggestion_id):
        self.prepared.append((pack_id, suggestion_id))
        return {
            "target_version_id": self.prepared_version_id,
            "pack_version": "2.2.1",
            "merged_terms": ["风洞测试", "空气动力学"],
        }


def _suggestion(suggestion_id, status, kind="keyword"):
    # 形状照抄 _serialize_suggestion()：payload.candidates 用 term/hits/examples，metrics 用百分数
    return {
        "suggestion_id": suggestion_id,
        "id": suggestion_id,
        "industry_pack_id": PACK_ID,
        "pack_id": PACK_ID,
        "kind": kind,
        "status": status,
        "created_at": "2026-10-11T09:00:00Z",
        "updated_at": "2026-10-11T09:00:00Z",
        "applied_at": None,
        "activation_id": "",
        "after_apply": {},
        "reason": "命中 37 篇 other 文章，且几乎不出现在其它包语料里",
        "payload": {
            "candidates": {
                "core_keywords": [{"term": "风洞测试", "hits": 37, "examples": ["某车风洞测试完成"]}],
                "entity_keywords": [{"term": "中国汽车工程学会", "hits": 12, "examples": ["学会发布标准"]}],
                "anchors": [{"term": "空气动力学", "hits": 21, "examples": ["空气动力学优化"]}],
            },
            "pack_version": "2.0.3",
            "merge_targets": ["core_keywords", "candidate_gate.entity_keywords", "candidate_gate.anchor_keywords"],
        },
        "metrics": {
            "before": {"admit_rate": 42.0, "false_positive": 11.0, "topic_assoc": 38.0, "other_pct": 97.7},
            "after": {"admit_rate": 61.0, "false_positive": 9.0, "topic_assoc": 55.0, "other_pct": 62.0},
            "delta": {"admit_rate": 19.0, "false_positive": -2.0, "topic_assoc": 17.0, "other_pct": -35.7},
            "crawl_probe": {
                "fetched_total": 12,
                "parsed_total": 9,
                "parse_failed_total": 3,
                "new_article_admit_rate_before": 0.0,
                "new_article_admit_rate_after": 33.33,
                "sources": [{"source_name": "汽车之家", "fetched": 4, "parse_failed": 1}],
            },
        },
        "evidence": {"assessment": {"other_pct": 97.7}},
    }


class IntelPackImprovementApiTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True)
        self.app.register_blueprint(intel_bp)
        self.client = self.app.test_client()
        self.headers = {"Authorization": "Bearer fixture"}
        # 打桩登录身份（管理员）
        self.auth_patch = patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": "admin-1", "role": "admin"},
        )
        self.auth_patch.start()
        self.loader_patch = patch.object(intel_api, "industry_pack_loader", _LoaderStub())
        self.loader_patch.start()
        self.repo_patch = patch.object(intel_api, "intel_repository", _RepoStub())
        self.repo_patch.start()
        self.activation = _ActivationStub()
        self.activation_patch = patch.object(
            intel_api, "industry_pack_activation_service", self.activation
        )
        self.activation_patch.start()
        intel_api._assessment_cache_invalidate()  # 进程内缓存跨用例清理

    def tearDown(self):
        self.activation_patch.stop()
        self.repo_patch.stop()
        self.loader_patch.stop()
        self.auth_patch.stop()
        intel_api._assessment_cache_invalidate()

    def _use_engine(self, engine):
        patcher = patch.object(intel_api, "_improvement_engine", lambda: engine)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _use_unavailable_engine(self, message="评估引擎未就绪：intel_pack_improvement 不可用（ImportError: 模块未落地）"):
        def _boom():
            raise intel_api._IntelPackEngineUnavailable(message)

        patcher = patch.object(intel_api, "_improvement_engine", _boom)
        patcher.start()
        self.addCleanup(patcher.stop)

    # ① 引擎不可用：接口不 500 ────────────────────────────────────────────────
    def test_engine_unavailable_never_returns_500(self):
        self._use_unavailable_engine()
        cases = [
            ("get", f"/api/intel/packs/{PACK_ID}/topic-ingestion-assessment", None),
            ("get", f"/api/intel/packs/{PACK_ID}/improvements", None),
            ("post", f"/api/intel/packs/{PACK_ID}/improvements/assess", {}),
            (
                "post",
                f"/api/intel/packs/{PACK_ID}/improvements/{SUGGESTION_ID}/apply",
                {"confirm": True, "plan_sha256": "plan-sha-abc"},
            ),
            ("post", f"/api/intel/packs/{PACK_ID}/improvements/{SUGGESTION_ID}/reject", {"reason": "证据不足"}),
            (
                "post",
                f"/api/intel/packs/{PACK_ID}/improvements/{SUGGESTION_ID}/verify-after-apply",
                {},
            ),
        ]
        for method, url, body in cases:
            with self.subTest(url=url):
                response = (
                    self.client.get(url, headers=self.headers)
                    if method == "get"
                    else self.client.post(url, json=body or {}, headers=self.headers)
                )
                self.assertNotEqual(response.status_code, 500)
                self.assertIn(response.status_code, (200, 503))
                payload = response.get_json()
                self.assertFalse(payload["success"])
                self.assertFalse(payload["engine_ready"])
                self.assertIn("评估引擎未就绪", payload["reason"])
        # 读接口必须是 200（评估卡要能渲染「引擎未就绪」，不能抛错误）
        read_response = self.client.get(
            f"/api/intel/packs/{PACK_ID}/topic-ingestion-assessment", headers=self.headers
        )
        self.assertEqual(read_response.status_code, 200)
        # 发布接口不能因为引擎缺失就去动配置
        self.assertEqual(self.activation.activate_calls, [])

    # ①b 引擎慢/超时同样不 500 ──────────────────────────────────────────────
    def test_engine_slow_and_timeout_degrades(self):
        import time as _time

        # ① 超时：引擎睡 6 秒，等待上限 5 秒 → 200 + success=false（引擎侧超时，非未就绪）。
        #    sample_limit 特意取 21：被放弃的 daemon 线程跑完仍会写缓存，
        #    用独立缓存键避免污染其它用例（它们用默认 300）。
        slow_engine = _EngineStub()
        slow_engine.assess_pack = lambda *a, **k: (_time.sleep(6), slow_engine.assess_result)[1]
        self._use_engine(slow_engine)
        response = self.client.get(
            f"/api/intel/packs/{PACK_ID}/topic-ingestion-assessment?refresh=1&sample_limit=21&timeout_seconds=5",
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload["success"])
        self.assertTrue(payload["engine_ready"])
        self.assertIn("超过 5 秒未返回", payload["reason"])

        # ② 慢但在预算内：睡 2 秒、上限 20 秒 → 正常返回（限时不代表一刀切失败）
        quick_engine = _EngineStub()
        quick_engine.assess_pack = lambda *a, **k: (_time.sleep(2), quick_engine.assess_result)[1]
        self._use_engine(quick_engine)
        ok_response = self.client.get(
            f"/api/intel/packs/{PACK_ID}/topic-ingestion-assessment?refresh=1&sample_limit=23&timeout_seconds=20",
            headers=self.headers,
        )
        self.assertEqual(ok_response.status_code, 200)
        ok_payload = ok_response.get_json()
        self.assertTrue(ok_payload["success"])
        self.assertEqual(ok_payload["assessment"]["articles"], 1200)
        self.assertFalse(ok_payload["cached"])

    # ② 列表默认只返回 verified ─────────────────────────────────────────────
    def test_improvements_default_only_verified(self):
        engine = _EngineStub(
            suggestions={
                "sg-verified-1": _suggestion("sg-verified-1", "verified"),
                "sg-staged-1": _suggestion("sg-staged-1", "staged"),
                "sg-failed-1": _suggestion("sg-failed-1", "failed", kind="source"),
                "sg-rejected-1": _suggestion("sg-rejected-1", "rejected"),
            }
        )
        self._use_engine(engine)
        response = self.client.get(f"/api/intel/packs/{PACK_ID}/improvements", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual([item["suggestion_id"] for item in payload["suggestions"]], ["sg-verified-1"])
        self.assertTrue(payload["suggestions"][0]["display"]["verified"])
        self.assertFalse(payload["suggestions"][0]["hidden_from_review"])
        self.assertEqual(payload["hidden_count"], 3)
        self.assertEqual(payload["status_filter"], "verified")
        self.assertEqual(payload["counts_by_status"]["staged"], 1)

        # 规范化：关键词新增值带命中数与示例标题，指标表 before→after 齐全
        display = payload["suggestions"][0]["display"]
        self.assertEqual(display["kind"], "keyword")
        self.assertEqual(display["keyword_additions"][0]["value"], "风洞测试")
        self.assertEqual(display["keyword_additions"][0]["group_label"], "核心关键词")
        self.assertEqual(display["keyword_additions"][0]["hits"], 37)
        self.assertEqual(display["keyword_additions"][0]["examples"], ["某车风洞测试完成"])
        self.assertEqual(display["keyword_additions"][2]["group_label"], "锚点词")
        metrics = {row["key"]: row for row in display["metrics"]["rows"]}
        self.assertEqual(metrics["admit_rate"]["before"], "42.0%")
        self.assertEqual(metrics["admit_rate"]["after"], "61.0%")
        self.assertEqual(metrics["admit_rate"]["delta"], "+19.0pp")
        self.assertTrue(metrics["admit_rate"]["improved"])
        self.assertTrue(metrics["false_positive"]["improved"])
        self.assertTrue(metrics["other_pct"]["improved"])  # 其他类占比下降 = 变好
        self.assertEqual(metrics["other_pct"]["delta"], "-35.7pp")
        self.assertEqual(display["crawl_probe"]["parsed_total"], 9)

        # 后台排查：显式 status 查询非 verified，必须带 hidden_from_review=true
        debug = self.client.get(
            f"/api/intel/packs/{PACK_ID}/improvements?status=staged", headers=self.headers
        ).get_json()
        self.assertEqual([item["suggestion_id"] for item in debug["suggestions"]], ["sg-staged-1"])
        self.assertTrue(debug["suggestions"][0]["hidden_from_review"])

    # ②b 信源类建议（引擎当前只产 keyword，这里锁定 forward-compatible 归一化）──
    def test_source_kind_suggestion_normalization(self):
        engine = _EngineStub(
            suggestions={
                "sg-source-1": {
                    "suggestion_id": "sg-source-1",
                    "industry_pack_id": PACK_ID,
                    "kind": "source",
                    "status": "verified",
                    "reason": "零产出信源占 5 个",
                    "payload": {
                        "disable_sources": [
                            {
                                "source_name": "停更站点",
                                "source_url": "https://example.com/feed",
                                "reason": "连续 30 天零产出",
                                "article_count": 0,
                                "admitted_pct": 0.0,
                            }
                        ],
                        "replace_sources": [
                            {
                                "source_name": "汽车之家",
                                "reason": "入口改版，列表页 404",
                                "suggested_url": "https://www.autohome.com.cn/news/",
                            }
                        ],
                    },
                    "metrics": {},
                }
            }
        )
        self._use_engine(engine)
        payload = self.client.get(
            f"/api/intel/packs/{PACK_ID}/improvements", headers=self.headers
        ).get_json()
        display = payload["suggestions"][0]["display"]
        self.assertEqual(display["kind"], "source")
        actions = {(item["source"], item["action"]) for item in display["source_actions"]}
        self.assertIn(("停更站点", "建议停用"), actions)
        self.assertIn(("汽车之家", "建议替换"), actions)
        by_source = {item["source"]: item for item in display["source_actions"]}
        self.assertEqual(by_source["停更站点"]["reason"], "连续 30 天零产出")
        self.assertTrue(by_source["停更站点"]["zero_output"])
        self.assertEqual(by_source["汽车之家"]["url"], "https://www.autohome.com.cn/news/")

    # ③ apply 二次确认缺参 → 400 ────────────────────────────────────────────
    def test_apply_requires_confirm_and_plan_sha256(self):
        engine = _EngineStub(suggestions={SUGGESTION_ID: _suggestion(SUGGESTION_ID, "verified")})
        self._use_engine(engine)
        url = f"/api/intel/packs/{PACK_ID}/improvements/{SUGGESTION_ID}/apply"
        for body in ({}, {"confirm": True}, {"plan_sha256": "plan-sha-abc"}):
            with self.subTest(body=body):
                response = self.client.post(url, json=body, headers=self.headers)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()["success"])
        self.assertEqual(self.activation.activate_calls, [])
        self.assertEqual(engine.mark_applied_calls, [])

    # ③b 未通过自测的建议禁止发布（D-009）──────────────────────────────────
    def test_apply_rejected_for_not_verified_suggestion(self):
        engine = _EngineStub(suggestions={SUGGESTION_ID: _suggestion(SUGGESTION_ID, "staged")})
        self._use_engine(engine)
        response = self.client.post(
            f"/api/intel/packs/{PACK_ID}/improvements/{SUGGESTION_ID}/apply",
            json={"confirm": True, "plan_sha256": "plan-sha-abc"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("未通过影子自测", response.get_json()["error"])
        self.assertEqual(self.activation.activate_calls, [])

    # ④ apply 成功路径：activation + mark_applied ───────────────────────────
    def test_apply_success_calls_activation_and_mark_applied(self):
        engine = _EngineStub(suggestions={SUGGESTION_ID: _suggestion(SUGGESTION_ID, "verified")})
        self._use_engine(engine)
        response = self.client.post(
            f"/api/intel/packs/{PACK_ID}/improvements/{SUGGESTION_ID}/apply",
            json={"confirm": True, "plan_sha256": "plan-sha-abc", "target_version_id": 7},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["target_version_id"], 7)
        self.assertEqual(payload["status"], "applied")
        # activation 走的是 preview(pack_id) → activate(target_version_id, expected_plan_sha256)
        self.assertEqual(self.activation.preview_calls[0][0], PACK_ID)
        self.assertEqual(len(self.activation.activate_calls), 1)
        activate_call = self.activation.activate_calls[0]
        self.assertEqual(activate_call["target_pack_id"], PACK_ID)
        self.assertEqual(activate_call["target_version_id"], 7)
        self.assertEqual(activate_call["expected_plan_sha256"], "plan-sha-abc")
        # mark_applied 收到真实的 activation 结果
        self.assertEqual(len(engine.mark_applied_calls), 1)
        self.assertEqual(engine.mark_applied_calls[0][0], SUGGESTION_ID)
        self.assertEqual(engine.mark_applied_calls[0][1]["activation_id"], "act-0001")

    # ④b 候选词写版本：引擎没钩子时必须明确标注"新词不会生效"（不假装发布成功）──
    def test_apply_without_prepare_hook_flags_warning(self):
        engine = _EngineStub(suggestions={SUGGESTION_ID: _suggestion(SUGGESTION_ID, "verified")})
        self._use_engine(engine)
        listed = self.client.get(
            f"/api/intel/packs/{PACK_ID}/improvements", headers=self.headers
        ).get_json()
        self.assertFalse(listed["candidate_apply_supported"])
        payload = self.client.post(
            f"/api/intel/packs/{PACK_ID}/improvements/{SUGGESTION_ID}/apply",
            json={"confirm": True, "plan_sha256": "plan-sha-abc"},
            headers=self.headers,
        ).get_json()
        self.assertTrue(payload["success"])
        self.assertFalse(payload["candidate_terms_applied"])
        self.assertIn("新词不会生效", payload["warning"])
        # 没有钩子时仍按「当前已发布版本」预览（target_version_id=None）
        self.assertIsNone(self.activation.preview_calls[0][1].get("target_version_id"))

    # ④c 引擎提供 prepare 钩子时：先写新版本，再按新版本预览 + 激活 ──────────
    def test_apply_uses_prepare_hook_and_targets_new_version(self):
        engine = _PrepareHookEngine(
            suggestions={SUGGESTION_ID: _suggestion(SUGGESTION_ID, "verified")}
        )
        self._use_engine(engine)
        listed = self.client.get(
            f"/api/intel/packs/{PACK_ID}/improvements", headers=self.headers
        ).get_json()
        self.assertTrue(listed["candidate_apply_supported"])
        self.assertEqual(listed["prepare_hook"], "prepare_pack_version")

        self.activation.target_version_id = 11  # 预览返回新版本
        payload = self.client.post(
            f"/api/intel/packs/{PACK_ID}/improvements/{SUGGESTION_ID}/apply",
            json={"confirm": True, "plan_sha256": "plan-sha-abc", "target_version_id": 11},
            headers=self.headers,
        ).get_json()
        self.assertEqual(engine.prepared, [(PACK_ID, SUGGESTION_ID)])
        self.assertEqual(self.activation.preview_calls[0][1]["target_version_id"], 11)
        self.assertEqual(self.activation.activate_calls[0]["target_version_id"], 11)
        self.assertTrue(payload["candidate_terms_applied"])
        self.assertEqual(payload["warning"], "")
        self.assertEqual(payload["prepare_result"]["pack_version"], "2.2.1")

    def test_apply_reports_prepare_hook_failure(self):
        engine = _PrepareHookEngine(
            suggestions={SUGGESTION_ID: _suggestion(SUGGESTION_ID, "verified")}
        )

        def _boom(*args, **kwargs):
            raise ValueError("草稿校验未通过：anchor_keywords 含单字")

        engine.prepare_pack_version = _boom
        self._use_engine(engine)
        response = self.client.post(
            f"/api/intel/packs/{PACK_ID}/improvements/{SUGGESTION_ID}/apply",
            json={"confirm": True, "plan_sha256": "plan-sha-abc"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("草稿校验未通过", response.get_json()["error"])
        self.assertEqual(self.activation.activate_calls, [])
        self.assertEqual(engine.mark_applied_calls, [])

    def test_apply_rejects_stale_plan(self):
        engine = _EngineStub(suggestions={SUGGESTION_ID: _suggestion(SUGGESTION_ID, "verified")})
        self._use_engine(engine)
        response = self.client.post(
            f"/api/intel/packs/{PACK_ID}/improvements/{SUGGESTION_ID}/apply",
            json={"confirm": True, "plan_sha256": "outdated-plan"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.activation.activate_calls, [])

    def test_apply_reports_activation_error(self):
        engine = _EngineStub(suggestions={SUGGESTION_ID: _suggestion(SUGGESTION_ID, "verified")})
        self._use_engine(engine)

        def _fail(*args, **kwargs):
            raise ValueError("目标行业包尚无已发布版本，请先校验并发布")

        self.activation.activate = _fail
        response = self.client.post(
            f"/api/intel/packs/{PACK_ID}/improvements/{SUGGESTION_ID}/apply",
            json={"confirm": True, "plan_sha256": "plan-sha-abc"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("尚无已发布版本", response.get_json()["error"])
        self.assertEqual(engine.mark_applied_calls, [])

    # ⑤ reject 写 reason ────────────────────────────────────────────────────
    def test_reject_writes_reason_via_engine_hook(self):
        engine = _EngineStub(suggestions={SUGGESTION_ID: _suggestion(SUGGESTION_ID, "verified")})
        self._use_engine(engine)
        response = self.client.post(
            f"/api/intel/packs/{PACK_ID}/improvements/{SUGGESTION_ID}/reject",
            json={"reason": "示例标题与该行业无关，误准入风险高"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["updated_via"], "engine:mark_rejected")
        self.assertEqual(
            engine.mark_rejected_calls,
            [(SUGGESTION_ID, "示例标题与该行业无关，误准入风险高")],
        )

    def test_reject_requires_reason(self):
        engine = _EngineStub(suggestions={SUGGESTION_ID: _suggestion(SUGGESTION_ID, "verified")})
        self._use_engine(engine)
        response = self.client.post(
            f"/api/intel/packs/{PACK_ID}/improvements/{SUGGESTION_ID}/reject",
            json={},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(engine.mark_rejected_calls, [])

    def test_reject_falls_back_to_database_when_engine_has_no_hook(self):
        engine = _NoRejectEngine(suggestions={SUGGESTION_ID: _suggestion(SUGGESTION_ID, "verified")})
        self._use_engine(engine)
        fake_db = _FakeDb()
        self.repo_patch.stop()
        self.repo_patch = patch.object(intel_api, "intel_repository", _RepoStub(db=fake_db))
        self.repo_patch.start()
        response = self.client.post(
            f"/api/intel/packs/{PACK_ID}/improvements/{SUGGESTION_ID}/reject",
            json={"reason": "该信源连续 30 天零产出"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["updated_via"], "database:fallback")
        self.assertEqual(payload["status"], "rejected")
        sql, params = fake_db.connection.log[0]
        self.assertIn("UPDATE intel_pack_improvements SET status=?", sql)
        self.assertEqual(params[0], "rejected")  # 只允许往 rejected 方向改
        self.assertEqual(params[1], "该信源连续 30 天零产出")
        self.assertTrue(fake_db.connection.committed)

    def test_reject_without_any_write_path_is_graceful(self):
        engine = _NoRejectEngine(suggestions={SUGGESTION_ID: _suggestion(SUGGESTION_ID, "verified")})
        self._use_engine(engine)
        response = self.client.post(
            f"/api/intel/packs/{PACK_ID}/improvements/{SUGGESTION_ID}/reject",
            json={"reason": "证据不足"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 503)  # 兜底库不可用也不能 500
        self.assertFalse(response.get_json()["success"])

    # ⑥ 发布后复测闭环 ──────────────────────────────────────────────────────
    def test_verify_after_apply_records_before_after(self):
        engine = _EngineStub(suggestions={SUGGESTION_ID: _suggestion(SUGGESTION_ID, "applied")})
        # 模拟「发布后真实抓取+分类」后的变化：other% 下降、整包准入率上升（30/50 = 60%）
        engine.assess_result["other_pct"] = 55.0
        engine.assess_result["sources"] = [
            {
                "source_id": 65,
                "source_name": "汽车之家",
                "source_url": "https://www.autohome.com.cn",
                "article_count": 50,
                "admitted_count": 30,
                "admitted_pct": 60.0,
                "last_article_at": "2026-10-12 10:00:00",
                "verdict": "有效",
            }
        ]
        self._use_engine(engine)
        response = self.client.post(
            f"/api/intel/packs/{PACK_ID}/improvements/{SUGGESTION_ID}/verify-after-apply",
            json={"sample_limit": 300},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload["recorded"])
        self.assertEqual(("assess_pack", PACK_ID, 300), engine.calls[0])
        self.assertEqual(len(engine.record_after_apply_calls), 1)
        suggestion_id, metrics = engine.record_after_apply_calls[0]
        self.assertEqual(suggestion_id, SUGGESTION_ID)
        self.assertEqual(metrics["before"]["other_pct"], 97.7)
        self.assertEqual(metrics["after"]["other_pct"], 55.0)
        self.assertEqual(metrics["after"]["admit_rate"], 60.0)
        rows = {row["key"]: row for row in payload["rows"]}
        self.assertEqual(rows["other_pct"]["delta"], "-42.7pp")
        self.assertTrue(rows["other_pct"]["improved"])
        self.assertEqual(rows["admit_rate"]["delta"], "+18.0pp")
        self.assertTrue(rows["admit_rate"]["improved"])

    def test_assessment_endpoint_caches_and_refreshes(self):
        engine = _EngineStub()
        self._use_engine(engine)
        first = self.client.get(
            f"/api/intel/packs/{PACK_ID}/topic-ingestion-assessment", headers=self.headers
        ).get_json()
        second = self.client.get(
            f"/api/intel/packs/{PACK_ID}/topic-ingestion-assessment", headers=self.headers
        ).get_json()
        refreshed = self.client.get(
            f"/api/intel/packs/{PACK_ID}/topic-ingestion-assessment?refresh=1", headers=self.headers
        ).get_json()
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertFalse(refreshed["cached"])
        self.assertEqual(refreshed["assessment"]["other_pct"], 97.7)
        self.assertEqual(refreshed["assessment"]["peer_pack_count"], 7)
        self.assertEqual(refreshed["sample_limit"], 300)
        # 信源表真实口径：article_count / admitted_count → 整包准入率（百分数）
        self.assertEqual(intel_api._assessment_source_rate(refreshed["assessment"]), 0.0)
        self.assertEqual([call[0] for call in engine.calls].count("assess_pack"), 2)

    def test_self_test_endpoint_returns_status_and_elapsed(self):
        engine = _EngineStub(staged_status="staged", staged_reason="误准入率反而升高")
        self._use_engine(engine)
        response = self.client.post(
            f"/api/intel/packs/{PACK_ID}/improvements/assess",
            json={"sample_limit": 200},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["status"], "staged")
        self.assertFalse(payload["visible_in_review"])
        self.assertTrue(payload["hidden_from_review"])
        self.assertIn("误准入率反而升高", payload["reason"])
        self.assertIn("elapsed_ms", payload)
        self.assertEqual(payload["timeout_seconds"], 240)
        self.assertEqual(("run_self_test_and_stage", PACK_ID, 200), engine.calls[0])


if __name__ == "__main__":
    unittest.main()
