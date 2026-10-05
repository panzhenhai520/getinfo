# -*- coding: utf-8 -*-
"""部署级 LLM 端点自适应（qa_llm_router）：按 RAGFlow 连通性选端点与故障回退。"""
import os
import unittest
from unittest.mock import patch

import qa_llm_router as router

_BLANK = {
    "QA_LLM_SELECTION": "",
    "QA_LLM_FAILOVER": "1",
    "QA_LLM_BASE_URL_LOCAL": "",
    "QA_LLM_MODEL_LOCAL": "",
    "QA_LLM_BASE_URL_RAGFLOW": "",
    "QA_LLM_MODEL_RAGFLOW": "",
}


class RouterTestBase(unittest.TestCase):
    def setUp(self):
        router.clear_cache()
        self._env = patch.dict(os.environ, _BLANK, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        # 默认认为 RAGFlow 凭据没配，避免读到本机 .env
        self._creds = patch.object(router, "ragflow_credentials", return_value=("", ""))
        self._creds.start()
        self.addCleanup(self._creds.stop)

    def env(self, **pairs):
        """临时补充环境变量（空值即视为没配）。"""
        patcher = patch.dict(os.environ, {k: v for k, v in pairs.items() if v is not None}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)


class SelectionTest(RouterTestBase):
    def test_no_endpoint_configured_keeps_caller_value_without_probing(self):
        with patch.object(router, "endpoint_alive") as alive, patch.object(router, "ragflow_reachable") as reach:
            result = router.resolve_llm_endpoint("http://cfg/v1", "cfg-model")
        self.assertEqual(result, ("http://cfg/v1", "cfg-model", "configured"))
        alive.assert_not_called()
        reach.assert_not_called()

    def test_auto_uses_local_endpoint_when_ragflow_not_connected(self):
        # 生产实况 A 型机器：没有 RAGFlow，LLM 在本地推理机
        self.env(QA_LLM_BASE_URL_LOCAL="http://10.88.0.1:8081/v1",
                 QA_LLM_MODEL_LOCAL="qwen3.8-27b-uncensored",
                 QA_LLM_BASE_URL_RAGFLOW="http://192.168.0.64:8106/v1",
                 QA_LLM_MODEL_RAGFLOW="deepseek-v4-flash")
        with patch.object(router, "ragflow_reachable", return_value=False), \
                patch.object(router, "endpoint_alive", return_value=True):
            result = router.resolve_llm_endpoint("http://cfg/v1", "cfg-model")
        self.assertEqual(result, ("http://10.88.0.1:8081/v1", "qwen3.8-27b-uncensored", "local"))

    def test_auto_uses_ragflow_llm_when_connected(self):
        # 生产实况 B 型机器：有 RAGFlow 知识库，LLM 也在 RAGFlow 那台
        self.env(QA_LLM_BASE_URL_LOCAL="http://10.88.0.1:8081/v1",
                 QA_LLM_BASE_URL_RAGFLOW="http://192.168.0.64:8106/v1",
                 QA_LLM_MODEL_RAGFLOW="deepseek-v4-flash")
        with patch.object(router, "ragflow_reachable", return_value=True), \
                patch.object(router, "endpoint_alive", return_value=True):
            result = router.resolve_llm_endpoint("http://cfg/v1", "cfg-model")
        self.assertEqual(result, ("http://192.168.0.64:8106/v1", "deepseek-v4-flash", "ragflow"))

    def test_model_defaults_to_caller_value_when_not_configured(self):
        self.env(QA_LLM_BASE_URL_LOCAL="http://10.88.0.1:8081/v1")
        with patch.object(router, "ragflow_reachable", return_value=False), \
                patch.object(router, "endpoint_alive", return_value=True):
            base_url, model_id, source = router.resolve_llm_endpoint("http://cfg/v1", "cfg-model")
        self.assertEqual((base_url, model_id, source), ("http://10.88.0.1:8081/v1", "cfg-model", "local"))

    def test_forced_local_mode_ignores_connectivity(self):
        self.env(QA_LLM_SELECTION="local",
                 QA_LLM_BASE_URL_LOCAL="http://local/v1",
                 QA_LLM_BASE_URL_RAGFLOW="http://rag/v1")
        with patch.object(router, "ragflow_reachable", return_value=True), \
                patch.object(router, "endpoint_alive", return_value=True):
            self.assertEqual(router.resolve_llm_endpoint("", "m")[2], "local")

    def test_forced_ragflow_mode_ignores_connectivity(self):
        self.env(QA_LLM_SELECTION="ragflow",
                 QA_LLM_BASE_URL_LOCAL="http://local/v1",
                 QA_LLM_BASE_URL_RAGFLOW="http://rag/v1")
        with patch.object(router, "ragflow_reachable", return_value=False), \
                patch.object(router, "endpoint_alive", return_value=True):
            base_url, _, source = router.resolve_llm_endpoint("", "m")
        self.assertEqual((base_url, source), ("http://rag/v1", "ragflow"))

    def test_unknown_selection_falls_back_to_auto(self):
        self.env(QA_LLM_SELECTION="whatever", QA_LLM_BASE_URL_LOCAL="http://local/v1")
        with patch.object(router, "ragflow_reachable", return_value=False):
            self.assertEqual(router.selection_mode(), "auto")

    def test_backup_endpoint_is_used_when_primary_is_not_configured(self):
        # B 型机器只配了 RAGFlow 端点：未连通时也不能退回写死的本机地址
        self.env(QA_LLM_BASE_URL_RAGFLOW="http://rag/v1", QA_LLM_MODEL_RAGFLOW="ds")
        with patch.object(router, "ragflow_reachable", return_value=False), \
                patch.object(router, "endpoint_alive", return_value=True):
            result = router.resolve_llm_endpoint("http://cfg/v1", "cfg-model")
        self.assertEqual(result, ("http://rag/v1", "ds", "ragflow"))


class FailoverTest(RouterTestBase):
    def test_failover_to_backup_when_primary_is_down(self):
        self.env(QA_LLM_BASE_URL_LOCAL="http://local/v1",
                 QA_LLM_BASE_URL_RAGFLOW="http://rag/v1")
        with patch.object(router, "ragflow_reachable", return_value=True), \
                patch.object(router, "endpoint_alive", side_effect=lambda url, api_key="": url == "http://local/v1"):
            result = router.resolve_llm_endpoint("", "m")
        self.assertEqual(result, ("http://local/v1", "m", "failover:local"))

    def test_failover_disabled_keeps_dead_primary(self):
        self.env(QA_LLM_FAILOVER="0",
                 QA_LLM_BASE_URL_LOCAL="http://local/v1",
                 QA_LLM_BASE_URL_RAGFLOW="http://rag/v1")
        with patch.object(router, "ragflow_reachable", return_value=True), \
                patch.object(router, "endpoint_alive", side_effect=lambda url, api_key="": url == "http://local/v1"):
            result = router.resolve_llm_endpoint("", "m")
        self.assertEqual(result, ("http://rag/v1", "m", "ragflow"))

    def test_both_endpoints_down_keeps_primary(self):
        self.env(QA_LLM_BASE_URL_LOCAL="http://local/v1",
                 QA_LLM_BASE_URL_RAGFLOW="http://rag/v1")
        with patch.object(router, "ragflow_reachable", return_value=False), \
                patch.object(router, "endpoint_alive", return_value=False):
            result = router.resolve_llm_endpoint("", "m")
        self.assertEqual(result, ("http://local/v1", "m", "local"))

    def test_probe_exception_falls_back_to_caller_value(self):
        self.env(QA_LLM_BASE_URL_LOCAL="http://local/v1")
        with patch.object(router, "ragflow_reachable", side_effect=RuntimeError("boom")):
            result = router.resolve_llm_endpoint("http://cfg/v1", "cfg-model")
        self.assertEqual(result, ("http://cfg/v1", "cfg-model", "configured"))


class StaticEndpointTest(RouterTestBase):
    def test_returns_builtin_default_when_nothing_configured(self):
        self.assertEqual(
            router.static_endpoint("http://10.88.0.1:8081/v1", "deepseek-v4-flash"),
            ("http://10.88.0.1:8081/v1", "deepseek-v4-flash"),
        )

    def test_prefers_local_endpoint_before_any_probe(self):
        # 纯配置路径不做网络探测，缓存里没有连通结论时按未连通处理
        self.env(QA_LLM_BASE_URL_LOCAL="http://local/v1", QA_LLM_MODEL_LOCAL="qwen",
                 QA_LLM_BASE_URL_RAGFLOW="http://rag/v1")
        with patch.object(router, "_probe_ragflow") as probe:
            self.assertEqual(router.static_endpoint(), ("http://local/v1", "qwen"))
        probe.assert_not_called()

    def test_uses_ragflow_endpoint_after_a_connected_probe(self):
        self.env(QA_LLM_BASE_URL_LOCAL="http://local/v1",
                 QA_LLM_BASE_URL_RAGFLOW="http://rag/v1", QA_LLM_MODEL_RAGFLOW="ds")
        with patch.object(router, "ragflow_credentials", return_value=("http://rag", "key")), \
                patch.object(router, "_probe_ragflow", return_value=True):
            self.assertTrue(router.ragflow_connected())
            self.assertEqual(router.static_endpoint(), ("http://rag/v1", "ds"))


class DecisionLogTest(RouterTestBase):
    def test_last_decision_records_the_chosen_endpoint(self):
        self.env(QA_LLM_BASE_URL_LOCAL="http://local/v1")
        with patch.object(router, "ragflow_reachable", return_value=False), \
                patch.object(router, "endpoint_alive", return_value=True):
            router.resolve_llm_endpoint("", "m")
        self.assertEqual(router.last_decision()["base_url"], "http://local/v1")
        router.clear_cache()
        self.assertEqual(router.last_decision(), {})


if __name__ == "__main__":
    unittest.main()
