#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入库收口守卫测试。

背景：入库闸门（关键词必中 / 框架页判废 / 未来日期 / 保留窗口 / 链接目录页）与
包归属兜底都实现在 sqlite_database.insert_article 里。只要有任何一个采集路径绕过它
直接写 articles 表，这些闸门就会被旁路（历史事故：manual_articles.py 直接
INSERT INTO articles，手写文章完全不经过闸门，也没有包归属 → 页面看得到、AI 搜不到）。

因此本测试把两个事实钉死：
  1. 生产模块里不允许出现 "INSERT INTO articles"，唯一写入点是 sqlite_database.py；
  2. 生产模块里不允许直接调 insert_article/add_article，唯一入口是
     article_ingest.ingest_article(source_kind=...)。

范围：仓库根目录下的生产 .py（测试脚本、tools/ 下的隔离夹具脚本不在范围内，
它们各自建临时库，不碰生产数据）。
"""
import ast
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 允许直接写 articles 表 / 直接调 insert_article 的文件
_CHOKE_POINT = "sqlite_database.py"
_FUNNEL = "article_ingest.py"

_INSERT_RE = re.compile(r"INSERT\s+INTO\s+articles", re.IGNORECASE)
_CALL_NAMES = {"insert_article", "add_article"}


def _production_modules():
    """仓库根目录的生产模块：排除测试脚本与临时脚本。"""
    for path in sorted(REPO_ROOT.glob("*.py")):
        name = path.name
        if name.startswith("test_") or name.startswith("_tmp_"):
            continue
        if name.startswith("check_"):
            continue
        yield path


def _direct_insert_calls(path):
    """用 AST 找直接调用 insert_article/add_article 的位置（注释/字符串不算）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _CALL_NAMES:
            hits.append(node.lineno)
        elif isinstance(func, ast.Name) and func.id in _CALL_NAMES:
            hits.append(node.lineno)
    return hits


def _sql_literals(path):
    """用 AST 取字符串常量里的 SQL（注释不算，避免注释提到 INSERT INTO articles 误报）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _INSERT_RE.search(node.value):
                hits.append(node.lineno)
    return hits


def _called_function_names(path):
    """收集被调用的函数名（用于校验某个方法确实被调过）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            names.append(func.attr)
        elif isinstance(func, ast.Name):
            names.append(func.id)
    return names


class IngestChokePointTest(unittest.TestCase):
    def test_only_sqlite_database_writes_articles_table(self):
        offenders = []
        for path in _production_modules():
            if path.name == _CHOKE_POINT:
                continue
            for lineno in _sql_literals(path):
                offenders.append(f"{path.name}:{lineno}")
        self.assertEqual(
            [],
            offenders,
            "生产模块禁止直接 INSERT INTO articles，必须走 insert_article 收口：\n"
            + "\n".join(offenders),
        )

    def test_only_article_ingest_calls_insert_article(self):
        offenders = []
        for path in _production_modules():
            if path.name in (_CHOKE_POINT, _FUNNEL):
                continue
            for lineno in _direct_insert_calls(path):
                offenders.append(f"{path.name}:{lineno}")
        self.assertEqual(
            [],
            offenders,
            "生产模块禁止直接调用 insert_article/add_article，"
            "必须走 article_ingest.ingest_article(source_kind=...)：\n"
            + "\n".join(offenders),
        )

    def test_funnel_rejects_unknown_source_kind(self):
        from article_ingest import SOURCE_KINDS, ingest_article

        self.assertIn("manual_article", SOURCE_KINDS)
        with self.assertRaises(ValueError):
            ingest_article({"url": "https://example.test/x"}, source_kind="not_a_known_source")

    def test_funnel_stamps_source_method_when_absent(self):
        """调用点没写 source_method 时，入口用 source_kind 兜底（可追溯到来源）。"""
        from article_ingest import ingest_article

        class _StubDb:
            def __init__(self):
                self.seen = None
                self.kwargs = None

            def insert_article(self, data, **kwargs):
                self.seen = data
                self.kwargs = kwargs
                return 1

        stub = _StubDb()
        article_id = ingest_article({"url": "https://example.test/a"}, source_kind="ai_chat", db=stub)
        self.assertEqual(1, article_id)
        self.assertEqual("ai_chat", stub.seen["source_method"])
        self.assertFalse(stub.kwargs["skip_pipeline"])

        explicit = _StubDb()
        ingest_article(
            {"url": "https://example.test/b", "source_method": "manual"},
            source_kind="manual_article",
            db=explicit,
            skip_pipeline=True,
        )
        self.assertEqual("manual", explicit.seen["source_method"])
        self.assertTrue(explicit.kwargs["skip_pipeline"])

    def test_attribution_module_is_wired_into_both_write_paths(self):
        """insert / update（去重命中）两条写路径都必须补归属。"""
        calls = [
            name
            for name in _called_function_names(REPO_ROOT / _CHOKE_POINT)
            if name == "ensure_pack_attribution"
        ]
        self.assertEqual(
            2,
            len(calls),
            "insert_article 与 update_article 都必须调用 ensure_pack_attribution，"
            f"实际调用点 {len(calls)} 个",
        )


if __name__ == "__main__":
    unittest.main()
