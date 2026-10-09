#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""守门测试：**阶段链里的每个阶段都必须在 SSE 契约的 stage 白名单里**。

为什么需要这个：阶段 9 新增 `logic_validation` 时忘了同步 `qa_contracts.QA_STAGES`，
结果**任何走到该阶段的问答 run 都会在事件校验处直接失败**——而且只在真跑全链路时才会暴露
（单测只测分解器，读代码看不出问题）。这个测试把"阶段链 ⊆ 事件契约"钉死，
以后再加阶段忘了改契约就会在这里红。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qa_contracts import QA_STAGES  # noqa: E402
from qa_orchestrator import FAST_STAGES, FULL_STAGES  # noqa: E402


class StageContractAlignmentTests(unittest.TestCase):
    def test_full_stages_are_all_declared(self):
        missing = [stage for stage in FULL_STAGES if stage not in QA_STAGES]
        self.assertEqual(missing, [], "FULL_STAGES 里有阶段没进 qa_contracts.QA_STAGES：%s" % missing)

    def test_fast_stages_are_all_declared(self):
        missing = [stage for stage in FAST_STAGES if stage not in QA_STAGES]
        self.assertEqual(missing, [], "FAST_STAGES 里有阶段没进 qa_contracts.QA_STAGES：%s" % missing)

    def test_stage_plan_outputs_are_declared(self):
        """三种调用方式（fast / level2 开 / level2 关）产出的链都要合规。"""
        from qa_orchestrator import QaOrchestrator

        for plan in (
            QaOrchestrator.stage_plan("standard", level2_enabled=True),
            QaOrchestrator.stage_plan("standard", level2_enabled=False),
            QaOrchestrator.stage_plan("fast"),
        ):
            for stage in plan:
                self.assertIn(stage, QA_STAGES, "阶段 %s 不在事件契约里" % stage)

    def test_stage_handlers_cover_every_stage(self):
        """阶段链里的每个阶段都必须有对应的处理函数（否则 run 到那里就抛"未配置"）。"""
        from qa_pipeline import build_qa_stage_handlers

        handlers = build_qa_stage_handlers()
        for stage in set(FULL_STAGES) | set(FAST_STAGES):
            if stage in ("completed",):
                continue
            self.assertIn(stage, handlers, "阶段 %s 没有处理函数" % stage)

    def test_stage_functions_take_emit_stage_event_from_context(self):
        """用了 `emit_stage_event` 的阶段函数必须先从 context 取出它。

        真实教训（阶段 9）：`logic_validation` 里直接写 `emit_stage_event(...)` 而忘了
        `emit_stage_event = context.get("_emit_stage_event")` → NameError →
        **每个 run 走到这一阶段都 INTERNAL_ERROR**，而且单元测试全绿、只有真跑全链路才暴露）。
        """
        import inspect
        import re

        from qa_pipeline import build_qa_stage_handlers

        source = inspect.getsource(build_qa_stage_handlers)
        # 按 `def <name>(context):` 切开每个阶段函数
        blocks = re.split(r"\n(?=    def )", source)
        offenders = []
        for block in blocks:
            match = re.match(r"\s*def (\w+)\(context\)", block)
            if not match:
                continue
            name = match.group(1)
            if name.startswith("_"):
                continue
            uses = re.search(r"(?<![_.\w])emit_stage_event\s*\(", block)
            declares = re.search(r"(?<![_.\w])emit_stage_event\s*=\s*context\.get\(", block)
            if uses and not declares:
                offenders.append(name)
        self.assertEqual(offenders, [],
                         "这些阶段函数用了 emit_stage_event 却没从 context 取：%s（会 NameError 打断 run）"
                         % offenders)


if __name__ == "__main__":
    unittest.main()
