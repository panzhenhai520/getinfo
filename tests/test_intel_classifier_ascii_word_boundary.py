#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""钉住「短 ASCII 关键词用 ASCII lookaround 词边界匹配」这条护栏。

背景（影子实验实测）：把 PE/VC/FA/LP/GP 这类短词写进行业包锚点后，原来的子串匹配会把
误准入从 6.33% 顶到 36.67%（+30.33pp）——`openai`/`paper`/`huggingface`/`openpore`/
`personinfo` 里的 `pe` 全被当成私募股权。加上本护栏后误准入只 +3.67pp（砍掉 23.33pp）。

关键实现约束（实测）：必须用 **ASCII lookaround** `(?<![A-Za-z0-9])PE(?![A-Za-z0-9])`，
**不能用 `\\b`**——Python 里中文是 `\\w`，`\\bPE\\b` 匹配不到 `PE基金`，会系统性漏掉真命中
（实测 IPO 命中 56→9、LP 25→1）。下面第 1、2 组用例就是这对回归钉子。

本测试全部跑在隔离的临时 SQLite 上（导入 config 之前就把 DATABASE_TYPE/DATABASE_PATH/
SQLITE_BACKUP_PATH 指向临时库，setUp 再断言 db.backend == 'sqlite'），不调 LLM、
不连任何远程机器。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# 必须在 import config 之前：config._load_dotenv_file() 只在键"不在环境里"时才写 .env 的值。
_TEMP_MAIN = tempfile.mkdtemp(prefix="collectinfo-ascii-wb-")
os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["DATABASE_PATH"] = os.path.join(_TEMP_MAIN, "main.sqlite3")
os.environ["SQLITE_BACKUP_PATH"] = os.path.join(_TEMP_MAIN, "main.sqlite3")

import config  # noqa: E402

config.DATABASE_TYPE = "sqlite"
config.INTEL_LLM_ENABLED = False

import intel_classifier  # noqa: E402
from industry_packs import normalize_intel_text  # noqa: E402
from intel_classifier import (  # noqa: E402
    _contains_keyword,
    _industry_signal,
    _is_short_ascii_keyword,
    _matches,
    classify_article,
)
from sqlite_database import SQLiteDatabase  # noqa: E402

PACK_ID = "test_pack_ascii_boundary"


def _norm(text: str) -> str:
    """生产链路里 `_matches` 收到的 text 都是 normalize_intel_text 归一化过的。"""
    return normalize_intel_text(text)


def _pack(*, anchors, core=None, expanded=None, trend=None, event=None, negative=None) -> dict:
    """桩行业包：锚点可注入短 ASCII 词，用于验证准入面的变化。"""
    return {
        "id": PACK_ID,
        "pack_version": "test-pack-v1",
        "classification": {
            "core_weight": 3,
            "expanded_weight": 1,
            "trend_weight": 2,
            "event_weight": 3,
            "negative_weight": -3,
            "minimum_relevance_score": 2,
            "llm_confidence_threshold": 0.9,
            "tie_break_order": ["trend", "event", "other"],
            "recent_today_window_days": 5,
            "recent_trend_window_days": 21,
        },
        "candidate_gate": {"anchor_keywords": list(anchors), "entity_keywords": []},
        "core_keywords": list(core if core is not None else anchors),
        "expanded_keywords": list(expanded or []),
        "trend_keywords": list(trend or []),
        "event_keywords": list(event or []),
        "negative_keywords": list(negative or []),
        "brands": [],
        "fixed_topics": [],
        "default_sources": [],
    }


class AsciiWordBoundaryTest(unittest.TestCase):
    """短 ASCII 词边界护栏（纯函数级 + 一个 classify_article 端到端用例）。"""

    def setUp(self):
        self._orig_db_type = getattr(config, "DATABASE_TYPE", None)
        self._orig_llm_enabled = getattr(config, "INTEL_LLM_ENABLED", None)
        config.DATABASE_TYPE = "sqlite"
        config.INTEL_LLM_ENABLED = False

        self.temp_dir = tempfile.TemporaryDirectory(prefix="ascii-wb-case-")
        self.db_path = os.path.join(self.temp_dir.name, "ascii_wb.sqlite3")
        self.db = SQLiteDatabase(self.db_path)
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        self.assertEqual(
            "sqlite",
            self.db.backend,
            "测试必须跑在隔离的临时 SQLite 上：连到主库会写入夹具、污染真实数据",
        )

    def tearDown(self):
        try:
            self.db.disconnect()
        except Exception:
            pass
        self.temp_dir.cleanup()
        config.DATABASE_TYPE = self._orig_db_type
        config.INTEL_LLM_ENABLED = self._orig_llm_enabled

    # ---------------- 1. 不能用 \b 的回归钉子：中文紧邻必须命中 ----------------

    def test_中文紧邻的短ASCII锚点必须命中(self):
        """`\\bPE\\b` 在这里会全部失配（中文是 \\w），所以护栏必须用 ASCII lookaround。"""
        cases = [
            ("PE基金完成新一轮募集", "PE"),
            ("对pe的投资逻辑", "PE"),
            ("本轮Pe融资由国资领投", "Pe"),
            ("VC机构募资回暖", "VC"),
            ("LP出资人结构变化", "LP"),
            ("GP管理人登记新规", "GP"),
            ("FA财务顾问业务收缩", "FA"),
        ]
        for text, keyword in cases:
            found = _matches(_norm(text), [keyword])
            self.assertEqual(
                [keyword],
                found,
                "中文紧邻时 %r 必须命中 %r（用 \\b 会漏；实测 IPO 56→9、LP 25→1）"
                % (text, keyword),
            )
            self.assertTrue(_contains_keyword(_norm(text), _norm(keyword)))

    def test_中文紧邻时行业信号与准入不受影响(self):
        pack = _pack(anchors=["PE"])
        self.assertTrue(_industry_signal("PE基金完成新一轮募集", pack))
        self.assertTrue(_industry_signal("对pe的投资逻辑", pack))
        result = classify_article(
            {"title": "PE基金完成新一轮募集", "content": "PE基金完成新一轮募集，规模 20 亿元。"},
            pack,
        )
        self.assertTrue(result["admitted"])
        self.assertEqual(["PE"], result["score_details"]["hits"]["anchor"])

    # ---------------- 2. 子串误命中的回归钉子 ----------------

    def test_子串包含PE的英文词不得命中(self):
        """影子实验证明：这些词的 `pe` 子串会把误准入从 6.33% 顶到 36.67%。"""
        for text in ("openai", "paper", "huggingface", "openpore", "personinfo"):
            self.assertEqual(
                [],
                _matches(_norm(text), ["PE"]),
                "%r 里的 pe 只是子串，不应命中 PE 锚点" % text,
            )
            self.assertFalse(_contains_keyword(_norm(text), _norm("PE")))

    def test_子串包含PE的长句不得命中PE锚点(self):
        pack = _pack(anchors=["PE"])
        for text in (
            "OpenAI 发布了新的推理模型",
            "该团队在 paper 中给出了新的评测协议",
            "huggingface 上的开源权重更新",
            "openpore 材料的孔径分布研究",
            "personinfo 字段补全说明",
        ):
            self.assertFalse(_industry_signal(text, pack), text)
            result = classify_article({"title": text, "content": text}, pack)
            self.assertFalse(result["admitted"], text)
            self.assertEqual("other", result["final_category"], text)
            self.assertEqual([], (result["score_details"]["hits"] or {}).get("anchor", []), text)

    def test_生产实测的真实子串误命中不再命中(self):
        """A 机现网语料（2026-10-10 只读对照，1299 对里 15 对）实测被护栏剔除的误命中。

        每条都来自真实文章片段：这些短 ASCII 词原本会顶出一个假锚点/假核心词，
        轻则污染 matched_keywords，重则把不相干文章误准入（OTA/EY/UBS 三条就是）。
        """
        cases = [
            # 汽车包：OTA（空中升级）被 kota-k / robotaxi / quota 顶出来
            ("OTA kotak", "OTA", "此次ipo共有19家银行参与承销,包括kotak mahindra capital、摩根士丹利"),
            ("OTA robotaxi", "OTA", "小鹏robotaxi正式定名“小鹏悠游”,目前仅限广州邀请码体验"),
            ("OTA quota", "OTA", "将作业巡检、quota态势分析、数据查询或成本分析等操作设置为周期性自动任务"),
            # 医疗包：LIS（检验信息系统）被 waitlist 顶出来；HIS（医院信息系统）被 history 顶出来
            ("LIS waitlist", "LIS", "i got approval from a waitlist. i was approved after just a few days"),
            ("HIS history", "HIS", "roms 会生成体积较大的 history、average、restart 等 netcdf 文件"),
            # 投资包：PE 被 openmp / prepare / pedaily.cn 顶出来
            ("PE openmp", "PE", "计算优化:探索 openmp 并行、simd 向量化、连续内存访问优化、数据复用"),
            ("PE pedaily", "PE", "原文:https://news.pedaily.cn/202603/562107.shtml"),
            # 家办包：来源名 EY / UBS 被子串顶出来（这一条会让"零关键词命中"的文章被误准入）
            ("EY pedigree", "EY", "with a serious silicon valley pedigree — before striking a deal"),
            ("UBS subscription", "UBS", "there is no app and no monthly subscription fee (for now)"),
            # 已知取舍：VC 嵌在 CVC（公司创投）里也会被判为"非 VC"——词边界只认独立词，
            # 这是本次护栏的既定边界（宁少不错），需要补词表时把 CVC 单独配成关键词即可。
            ("VC cvc", "VC", "本轮融资获得了多家aidc产业链cvc基金、lavender hill capital 的参与"),
        ]
        for label, keyword, text in cases:
            self.assertEqual([], _matches(_norm(text), [keyword]), label)

    def test_边界只排除ASCII字母数字(self):
        # 前后是 ASCII 字母/数字 → 不算边界，不命中。
        self.assertEqual([], _matches(_norm("apex pe2 3pe xpe"), ["PE"]))
        # 前后是标点/中文/空格/字符串首尾 → 算边界，命中。
        self.assertEqual(["PE"], _matches(_norm("pe-投资"), ["PE"]))
        self.assertEqual(["PE"], _matches(_norm("（pe）基金"), ["PE"]))
        self.assertEqual(["PE"], _matches(_norm("PE"), ["PE"]))
        # 注意：下划线**不在** [A-Za-z0-9] 里，按当前实现（与影子实验给出的正则一致）
        # 算边界，所以 pe_1 / _pe 仍会命中。这是刻意与实测结论保持一致的行为。
        self.assertEqual(["PE"], _matches(_norm("pe_1 与 _pe"), ["PE"]))

    # ---------------- 3. 已知同形异义：记录当前行为，不假装语义正确 ----------------

    def test_已知同形异义_vc均热板仍然命中VC(self):
        """中文里 `vc` 也是均热板/电解液添加剂的缩写，护栏只减少误命中、不做语义判断。

        期望行为（当前就是如此，故意钉住）：`vc均热板` 仍然命中 VC 锚点 ——
        vc 两侧是"中文紧邻"，lookaround 只排除 ASCII 字母数字，挡不住这种同形异义。
        真要治它必须靠词表/语义层，不属于本护栏范围。
        """
        self.assertEqual(["VC"], _matches(_norm("vc均热板散热方案升级"), ["VC"]))
        self.assertEqual(["VC"], _matches(_norm("电解液添加剂vc价格下行"), ["VC"]))
        pack = _pack(anchors=["VC"])
        self.assertTrue(_industry_signal("vc均热板散热方案升级", pack))

    # ---------------- 4. 其它关键词（中文 / 长度>4 的 ASCII）保持原样子串匹配 ----------------

    def test_长度大于4的ASCII词保持子串匹配(self):
        # 只有「长度 ≤4 且纯 ASCII」才加词边界；更长的 ASCII 词行为完全不变。
        self.assertFalse(_is_short_ascii_keyword("openai"))
        self.assertEqual(["openai"], _matches(_norm("openai 发布了新模型"), ["openai"]))
        self.assertEqual(["openai"], _matches(_norm("myopenaiclient 网关"), ["openai"]))
        self.assertEqual(["scada"], _matches(_norm("scadax 采集器"), ["scada"]))
        self.assertEqual(["invest_management"], _matches(_norm("invest_management 平台"), ["invest_management"]))

    def test_中文词保持子串匹配(self):
        self.assertFalse(_is_short_ascii_keyword("智算"))
        self.assertEqual(["智算"], _matches(_norm("智算中心扩容"), ["智算"]))
        # 中文没有词边界概念：子串命中依旧算命中（这是历史行为，不能变）。
        self.assertEqual(["电解液"], _matches(_norm("电解液添加剂vc价格下行"), ["电解液"]))

    def test_含符号或空格的短ASCII词保持子串匹配(self):
        # 非纯字母数字（含 /、+、空格、点等）不做词边界处理，避免扩大改动面。
        for keyword in ("A/B", "C++", "PE VC", "U.S."):
            self.assertFalse(_is_short_ascii_keyword(keyword), keyword)
        self.assertEqual(["A/B"], _matches(_norm("A/B测试平台上线"), ["A/B"]))

    # ---------------- 5. 大小写不敏感行为不变 ----------------

    def test_大小写不敏感_pe_PE_Pe都应命中(self):
        text = "pe基金与PE基金以及Pe基金"
        for keyword in ("PE", "pe", "Pe", "pE"):
            self.assertEqual([keyword], _matches(_norm(text), [keyword]))
            self.assertTrue(_is_short_ascii_keyword(keyword), keyword)

    def test_大小写不敏感_子串判定同样不受大小写影响(self):
        for text in ("OpenAI", "OPENAI", "openai"):
            self.assertEqual([], _matches(_norm(text), ["PE"]))
        self.assertEqual(["PAPER"], _matches(_norm("paper 里的评测协议"), ["PAPER"]))

    # ---------------- 6. 辅助函数本身的门槛 ----------------

    def test_短ASCII关键词判定门槛(self):
        for keyword in ("PE", "VC", "LP", "GP", "FA", "pe", "5G", "pevc"):
            self.assertTrue(_is_short_ascii_keyword(keyword), keyword)
        for keyword in ("pevca", "openai", "智算", "A/B", "PE VC", "", "中pe"):
            self.assertFalse(_is_short_ascii_keyword(keyword), keyword)

    def test_空输入不炸(self):
        self.assertFalse(_contains_keyword("", "PE"))
        self.assertFalse(_contains_keyword("pe基金", ""))
        self.assertEqual([], _matches(_norm("pe基金"), None))
        self.assertEqual([], _matches(_norm("pe基金"), []))

    # ---------------- 7. 各分组命中（core/expanded/trend/event/negative）都走护栏 ----------------

    def test_各分组关键词都走同一护栏(self):
        pack = _pack(
            anchors=["PE"],
            core=["PE"],
            expanded=["VC"],
            trend=["LP"],
            event=["GP"],
            negative=["FA"],
        )
        article = {"title": "PE基金募集", "content": "PE基金与VC机构、LP出资人、GP管理人共同参与。"}
        result = classify_article(article, pack)
        self.assertEqual(["PE"], result["score_details"]["hits"]["anchor"])
        self.assertEqual(["PE"], result["score_details"]["hits"]["core"])
        self.assertEqual(["VC"], result["score_details"]["hits"]["expanded"])
        self.assertEqual(["LP"], result["score_details"]["hits"]["trend"])
        self.assertEqual(["GP"], result["score_details"]["hits"]["event"])
        self.assertEqual([], result["score_details"]["hits"]["negative"])
        # 子串噪声仍在同一篇文章里：不得被任何分组命中
        noisy = {"title": "openai 与 paper", "content": "huggingface/openpore/personinfo 的说明"}
        noisy_result = classify_article(noisy, pack)
        for group in ("anchor", "core", "expanded", "trend", "event", "negative", "brand"):
            self.assertEqual(
                [], (noisy_result["score_details"]["hits"] or {}).get(group, []), group
            )
        self.assertFalse(noisy_result["admitted"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
