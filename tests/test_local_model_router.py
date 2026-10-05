# -*- coding: utf-8 -*-
"""本地模型自适应（local_model_router）的选择规则与两个接入点。"""
import unittest
from unittest.mock import patch

import local_model_router as router


class EndpointUrlTest(unittest.TestCase):
    def test_openai_style_base_splits_root_and_v1(self):
        ps, models = router.endpoint_urls("http://host:11434/v1")
        self.assertEqual(ps, "http://host:11434/api/ps")
        self.assertEqual(models, "http://host:11434/v1/models")

    def test_bare_base_gets_v1_appended(self):
        ps, models = router.endpoint_urls("http://host:11434")
        self.assertEqual(ps, "http://host:11434/api/ps")
        self.assertEqual(models, "http://host:11434/v1/models")


class ChooseModelTest(unittest.TestCase):
    def test_configured_model_loaded_wins(self):
        self.assertEqual(
            router.choose_model("gemma431b-32k:latest", ["gemma431b-32k:latest"], ["a", "b"]),
            "gemma431b-32k:latest",
        )

    def test_follows_the_only_loaded_model_when_config_points_elsewhere(self):
        # 生产实况：配置写 qwen3-coder，显存里装的是别的 → 跟随当前的，避免重新加载
        self.assertEqual(
            router.choose_model("qwen3-coder:latest", ["qwen3.8-27b-uncensored:latest"], ["x", "y"]),
            "qwen3.8-27b-uncensored:latest",
        )

    def test_latest_suffix_is_ignored_when_comparing(self):
        self.assertTrue(router.same_model("gemma431b-32k", "gemma431b-32k:latest"))
        self.assertEqual(
            router.choose_model("gemma431b-32k", ["gemma431b-32k:latest"], []),
            "gemma431b-32k",
        )

    def test_multiple_loaded_and_config_absent_keeps_config(self):
        self.assertEqual(router.choose_model("zzz", ["a", "b"], ["a", "b"]), "zzz")

    def test_no_probe_data_keeps_config(self):
        self.assertEqual(router.choose_model("configured", [], []), "configured")

    def test_single_installed_model_replaces_stale_config(self):
        self.assertEqual(router.choose_model("stale", [], ["only:latest"]), "only:latest")

    def test_single_installed_equal_to_config_keeps_config(self):
        self.assertEqual(router.choose_model("only", [], ["only:latest"]), "only")


class ResolveTest(unittest.TestCase):
    def setUp(self):
        router.clear_cache()
        self.addCleanup(router.clear_cache)

    def test_resolve_uses_probe_result(self):
        with patch.object(router, "probe_models",
                          return_value=(["loaded-model:latest"], ["loaded-model:latest", "other"])):
            self.assertEqual(
                router.resolve_local_model("http://host:11434/v1", "configured"),
                "loaded-model:latest",
            )

    def test_resolve_falls_back_to_config_when_probe_fails(self):
        with patch.object(router, "probe_models", return_value=([], [])):
            self.assertEqual(
                router.resolve_local_model("http://host:11434/v1", "configured"),
                "configured",
            )

    def test_resolve_returns_config_when_probe_raises(self):
        with patch.object(router, "probe_models", side_effect=RuntimeError("boom")):
            self.assertEqual(
                router.resolve_local_model_quiet("http://host:11434/v1", "configured"),
                "configured",
            )

    def test_autoselect_can_be_disabled(self):
        with patch.dict("os.environ", {"LOCAL_MODEL_AUTOSELECT": "0"}):
            with patch.object(router, "probe_models",
                              return_value=(["loaded"], ["loaded"])) as probe:
                self.assertEqual(
                    router.resolve_local_model("http://host:11434/v1", "configured"),
                    "configured",
                )
                probe.assert_not_called()

    def test_result_is_cached_per_base_url(self):
        with patch.object(router, "probe_models",
                          return_value=(["loaded"], ["loaded"])) as probe:
            router.resolve_local_model("http://host:11434/v1", "configured")
            router.resolve_local_model("http://host:11434/v1", "configured")
            self.assertEqual(probe.call_count, 1)


class IntegrationPointTest(unittest.TestCase):
    """两个接入点：chat_api 的运行时配置 与 统一 QA 的 provider 解析。"""

    def setUp(self):
        router.clear_cache()
        self.addCleanup(router.clear_cache)

    def test_chat_runtime_config_follows_loaded_model(self):
        import chat_api

        cfg = {"active_model": "local",
               "models": {"local": {"api_key": "x", "model_id": "configured-model",
                                    "use_proxy": False, "base_url": "http://host:11434/v1"}}}
        with patch.object(chat_api, "_load_config", return_value=cfg), \
                patch.object(router, "probe_models", return_value=(["live-model:latest"], ["live-model:latest"])):
            runtime = chat_api.get_chat_model_runtime_config("local")
        self.assertEqual(runtime["model_id"], "live-model:latest")
        self.assertEqual(runtime["base_url"], "http://host:11434/v1")

    def test_qa_provider_registry_follows_loaded_model(self):
        from qa_provider_registry import QaProviderRegistry

        runtime = {"provider_id": "local", "name": "本地 LLM", "type": "openai",
                   "base_url": "http://host:11434/v1", "model_id": "configured-model",
                   "api_key": "x", "use_proxy": False}
        registry = QaProviderRegistry(runtime_loader=lambda *a, **k: runtime)
        with patch.object(router, "probe_models", return_value=(["live-model:latest"], [])):
            profile = registry.resolve("draft", "local")
        self.assertEqual(profile.model_id, "live-model:latest")


if __name__ == "__main__":
    unittest.main()
