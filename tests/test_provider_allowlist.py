#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""出站白名单回归测试。

背景（生产事故）：B 机的模型端点配在 .env 的 QA_LLM_BASE_URL_RAGFLOW（私网 192.168.0.64），
而白名单只取 chat_api.MODEL_META 与 QA_ALLOWED_PROVIDER_HOSTS，导致**管理员自己配的端点被拦**，
每次 level1_draft 都降级成证据锚定兜底 —— 那台机器上的 AI 回答从来没真正调用过模型。
"""
import os
import unittest
from unittest import mock

from qa_observability import provider_allowed_hosts


class ProviderAllowedHostsTests(unittest.TestCase):
    def test_env_configured_llm_hosts_are_trusted(self):
        env = {
            "QA_LLM_BASE_URL_RAGFLOW": "http://192.168.0.64:8106/v1",
            "INTEL_LLM_BASE_URL": "http://10.88.0.1:8081/v1",
            "INTEL_EMBEDDING_BASE_URL": "http://192.168.0.18:8082",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            hosts = provider_allowed_hosts()
        for host in ("192.168.0.64", "10.88.0.1", "192.168.0.18"):
            self.assertIn(host, hosts)

    def test_explicit_allowlist_still_honored(self):
        with mock.patch.dict(os.environ, {"QA_ALLOWED_PROVIDER_HOSTS": "example.internal, 10.0.0.9"},
                             clear=False):
            hosts = provider_allowed_hosts()
        self.assertIn("example.internal", hosts)
        self.assertIn("10.0.0.9", hosts)

    def test_invalid_values_do_not_crash(self):
        with mock.patch.dict(os.environ, {"QA_LLM_BASE_URL_BAD": "not a url", "RAGFLOW_BASE_URL": ""},
                             clear=False):
            hosts = provider_allowed_hosts()
        self.assertIsInstance(hosts, set)


if __name__ == "__main__":
    unittest.main()
