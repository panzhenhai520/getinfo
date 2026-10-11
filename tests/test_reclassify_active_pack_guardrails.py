#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/reclassify_active_pack.py 删除护栏的回归用例（事实 B）。

事故：该工具原先在 `admitted=False` 分支直接 DELETE 分类行 + 主题关联，某行业包一次跑下来
删掉 84 行 —— 分类分母 973→889、前后结论不可比，用户还会莫名少 84 篇文章。
现在的契约：**默认不删任何行**；删除必须显式 `--delete-unadmitted`，并在交互式终端里二次确认
（非交互环境拒绝），或用 `--yes` 双显式授权。

用例用假 sqlite_db 记录所有 SQL，断言默认路径**不产生任何 DELETE**。
"""
import contextlib
import importlib.util
import io
import os
import sys
import threading
import unittest
from unittest.mock import patch

_BOOTSTRAP_TEMP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_BOOTSTRAP_TEMP_DIR))

_TOOL_PATH = os.path.join(
    os.path.dirname(_BOOTSTRAP_TEMP_DIR), "tools", "reclassify_active_pack.py"
)


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "reclassify_active_pack_under_test", _TOOL_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeCursor:
    def __init__(self, recorder):
        self._recorder = recorder

    def execute(self, sql, params=None):
        self._recorder.sql.append(" ".join(str(sql).split()))
        self._recorder.params.append(params)
        return self

    def fetchall(self):
        return [(0,)]

    def fetchone(self):
        return (0,)

    def close(self):
        return None


class _FakeConnection:
    def __init__(self, recorder):
        self._recorder = recorder

    def execute(self, sql, params=None):
        return _FakeCursor(self._recorder).execute(sql, params)

    def cursor(self):
        return _FakeCursor(self._recorder)

    def commit(self):
        self._recorder.commits += 1


class _Recorder:
    def __init__(self):
        self.sql = []
        self.params = []
        self.commits = 0
        self.lock = threading.RLock()
        self.connection = _FakeConnection(self)

    def _ensure_connection(self):
        return None

    def deletes(self):
        return [statement for statement in self.sql if "DELETE" in statement.upper()]


class _FakePackService:
    def __init__(self, payload):
        self._payload = payload

    def snapshot(self):
        return dict(self._payload)


class _FakeClassifier:
    def __init__(self, admitted):
        self._admitted = admitted
        self.calls = 0

    def classify_article_id(self, article_id, pack_id, activation_id=""):
        self.calls += 1
        return {"admitted": self._admitted, "final_category": "event"}


class ReclassifyActivePackGuardrailTests(unittest.TestCase):
    def setUp(self):
        self.tool = _load_tool()
        self.recorder = _Recorder()
        self.tool.sqlite_db = self.recorder
        self.tool.active_industry_composition_service = _FakePackService(
            {
                "active_industry_pack_id": "test_pack",
                "active_industry_activation_id": "act-1",
            }
        )
        self.tool.active_article_ids = lambda pack, act: [11, 12, 13]
        self.tool.count_anchor = lambda pack, act: 0
        self.tool.count_total = lambda pack, act: 3

    # ---------- 参数契约 ----------

    def test_delete_flag_defaults_to_off(self):
        args = self.tool._parse_args([])
        self.assertFalse(args.delete_unadmitted, "默认必须是「不删行」")
        self.assertFalse(args.yes)
        explicit = self.tool._parse_args(["--delete-unadmitted", "--yes"])
        self.assertTrue(explicit.delete_unadmitted)
        self.assertTrue(explicit.yes)

    # ---------- 默认路径：不产生任何 DELETE ----------

    def test_prune_unadmitted_rows_default_issues_no_delete(self):
        removed = self.tool.prune_unadmitted_rows(
            [11, 12, 13], pack_id="test_pack", activation_id="act-1"
        )
        self.assertEqual(removed, 0)
        self.assertEqual(self.recorder.deletes(), [])
        self.assertEqual(self.recorder.commits, 0)

    def test_prune_refuses_when_confirmation_declined(self):
        removed = self.tool.prune_unadmitted_rows(
            [11, 12, 13],
            pack_id="test_pack",
            activation_id="act-1",
            delete_unadmitted=True,
            confirm=lambda _count: False,
        )
        self.assertEqual(removed, 0)
        self.assertEqual(self.recorder.deletes(), [])

    def test_interactive_confirm_refuses_on_non_tty(self):
        class _NotATty:
            def isatty(self):
                return False

        with patch("sys.stdin", _NotATty()):
            self.assertFalse(self.tool._interactive_confirm(84))

    def test_main_default_path_never_deletes(self):
        self.tool.classification_service = _FakeClassifier(admitted=False)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.tool.main([])
        output = buffer.getvalue()
        self.assertEqual(self.recorder.deletes(), [], "默认路径不得产生任何 DELETE")
        self.assertEqual(self.recorder.commits, 0)
        self.assertIn("默认不删行", output)
        self.assertIn("未准入（admitted=False）行数: 3", output)
        self.assertIn("removed non-industry: 0", output)

    def test_main_with_delete_flag_but_no_confirmation_never_deletes(self):
        self.tool.classification_service = _FakeClassifier(admitted=False)
        self.tool._interactive_confirm = lambda _count: False
        self.tool.main(["--delete-unadmitted"])
        self.assertEqual(self.recorder.deletes(), [], "未确认时不得删除任何行")

    # ---------- 显式授权路径：确实删（且只删被拦下的行） ----------

    def test_main_with_explicit_authorization_deletes_each_unadmitted_row(self):
        self.tool.classification_service = _FakeClassifier(admitted=False)
        self.tool.main(["--delete-unadmitted", "--yes"])
        deletes = self.recorder.deletes()
        self.assertEqual(len(deletes), 6, "每行 2 条 DELETE（分类行 + 主题关联）")
        self.assertTrue(all("article_intel_classifications" in sql or "intel_topic_articles" in sql
                            for sql in deletes))
        self.assertEqual(self.recorder.commits, 1)
        deleted_ids = sorted(
            params[0] for sql, params in zip(self.recorder.sql, self.recorder.params)
            if "DELETE FROM article_intel_classifications" in sql
        )
        self.assertEqual(deleted_ids, [11, 12, 13])

    def test_admitted_rows_are_never_deleted(self):
        self.tool.classification_service = _FakeClassifier(admitted=True)
        self.tool.main(["--delete-unadmitted", "--yes"])
        self.assertEqual(self.recorder.deletes(), [], "准入的行永远不该被删")


if __name__ == "__main__":
    unittest.main()
