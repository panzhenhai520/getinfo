#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务模板与能力声明：让"问题分析"从"拆一句问句"升级为"先给体检清单 + 边界 + 能力声明"。

为什么需要：现在的计划阶段只做"把问句拆成子问题"，用户看到的计划长这样
「已识别为 1 个单一问题…我会先行业影响类」——没有检查项、没有边界、没有能力声明，
所以遇到"帮我检查当前网络环境：出口 IP / 代理设置 / WebRTC 泄露"这类**系统性体检**需求时，
它只会当成一个普通的资料检索问题，显得很弱智。

对标 Codex 那种回复，缺的其实是三样**结构**（不是模型能力）：
  1) 检查清单：把需求拆成可逐条核对的项（分组、可验收）；
  2) 边界声明：明确"只读 / 不改动 / 脱敏"，让用户敢授权；
  3) 能力声明：说清哪些项本助手真能做、哪些需要本机探测能力——做不到就明说，不要假装。

本模块给出模板注册表与匹配逻辑；`qa_planner` 把结果落到 plan 的
`task_template / plan_checklist / plan_boundary / capability` 字段，并展示给用户。
"""
from __future__ import annotations

import re
from typing import Dict, List, Mapping

# capability 取值：
#   document_research —— 资料/知识库检索（本助手的主能力）
#   local_probe —— 需要读取本机环境（网卡/代理/进程/浏览器），当前助手不具备
#   mixed —— 资料检索为主，部分检查项需要本机探测
TASK_TEMPLATES: tuple = (
    {
        "key": "system_environment_audit",
        "label": "环境/系统体检",
        "triggers": (
            "网络环境", "出口ip", "出口 ip", "公网ip", "公网 ip", "代理设置", "系统代理",
            "winhttp", "v2ray", "v2rayn", "clash", "tun模式", "tun 模式", "虚拟网卡",
            "webrtc", "时区撕裂", "时区", "代理残留", "环境变量", "dns", "路由表",
            "network environment", "proxy settings",
        ),
        "boundary": "只读核对，不修改任何系统/网络设置；涉及密钥、订阅地址、节点 UUID 只做脱敏统计，不打印原文",
        "capability": "mixed",
        "checklist": (
            {"group": "出口与链路", "items": ("公网出口 IP 与归属地", "出口链路是否走代理", "DNS 解析结果与出口是否一致")},
            {"group": "代理配置", "items": ("系统代理 / WinHTTP / 终端环境变量三者是否一致",
                                            "v2rayN/Clash 进程与监听端口", "TUN 模式与虚拟网卡状态、路由表是否冲突")},
            {"group": "泄露与一致性", "items": ("WebRTC 候选地址是否暴露本地/内网 IP",
                                                "浏览器时区与出口 IP 时区是否撕裂",
                                                "残留代理/环境变量是否造成部分应用绕过代理")},
        ),
        "capability_note": "“出口 IP / WebRTC 候选 / 代理进程 / 虚拟网卡”这些项需要读取本机环境，"
                           "当前统一问答只能在资料库里检索解释性内容，不能替你在本机执行探测；"
                           "这类需求请交给能跑命令的本地助手（Claude Code / Codex 等）。",
    },
    {
        "key": "document_deep_read",
        "label": "文件/政策精读",
        "triggers": ("政策全文", "逐段", "逐条", "条文", "原文", "第几条", "章节",
                     "实施细则", "征求意见稿", "全文"),
        "boundary": "只依据可引用原文作答；找不到原文时明确说明缺口，不做推断性补全",
        "capability": "document_research",
        "checklist": (
            {"group": "原文可得性", "items": ("是否已获取官方原文/全文本", "发布机关、文号、生效日期是否確认")},
            {"group": "逐条解读", "items": ("适用主体与适用范围", "豁免/例外情形", "与其他条款的衔接")},
        ),
        "capability_note": "若知识库中没有该文件全文，会先说明缺口并按现有证据给出有限结论。",
    },
    {
        "key": "comparison",
        "label": "对比分析",
        "triggers": ("对比", "区别", "差异", "哪个更好", "优缺点", "相比", "vs", "versus"),
        "boundary": "对比维度必须来自可引用证据；不同口径的数据不混用",
        "capability": "document_research",
        "checklist": (
            {"group": "对比口径", "items": ("同一口径下的可比指标", "数据时点是否一致")},
            {"group": "结论", "items": ("各自优势与代价", "适用场景差异")},
        ),
        "capability_note": "",
    },
    {
        "key": "troubleshooting",
        "label": "问题排查",
        "triggers": ("排查", "为什么总是", "报错", "失败原因", "不正常", "异常", "定位问题"),
        "boundary": "先列可验证的假设与验证方法，再给结论；不把推测写成事实",
        "capability": "document_research",
        "checklist": (
            {"group": "现象与范围", "items": ("现象复现条件", "影响范围与开始时间")},
            {"group": "假设与验证", "items": ("可能原因排序", "每个原因的验证方法")},
        ),
        "capability_note": "",
    },
)

DEFAULT_TEMPLATE: Dict = {
    "key": "topic_research",
    "label": "行业资料检索",
    "triggers": (),
    "boundary": "结论必须绑定可引用证据；证据不足时明确说明",
    "capability": "document_research",
    "checklist": (
        {"group": "检索范围", "items": ("行业包内相关文章", "知识库官方原文/报告（若已开启）")},
        {"group": "结论", "items": ("事实与数据", "影响与风险", "时间线")},
    ),
    "capability_note": "",
}


def _matches(question: str, triggers) -> int:
    text = str(question or "").casefold()
    return sum(1 for word in triggers if str(word).casefold() in text)


def match_task_template(question: str) -> Dict:
    """按触发词匹配任务模板；命中多个时取命中数最多者，都没命中用默认模板。"""
    best, best_score = None, 0
    for template in TASK_TEMPLATES:
        score = _matches(question, template.get("triggers") or ())
        if score > best_score:
            best, best_score = template, score
    if not best:
        best = DEFAULT_TEMPLATE
    return {
        "key": best["key"],
        "label": best["label"],
        "boundary": best.get("boundary") or "",
        "capability": best.get("capability") or "document_research",
        "capability_note": best.get("capability_note") or "",
        "checklist": [dict(group) for group in best.get("checklist") or ()],
        "matched_terms": _matches(question, best.get("triggers") or ()),
    }


def render_task_brief(template: Mapping) -> List[str]:
    """把模板渲染成给用户看的"我准备怎么做"清单（对标 Codex 那种先讲做法与边界的回复）。"""
    if not template:
        return []
    lines = ["【本次任务类型】%s" % str(template.get("label") or "")]
    for group in template.get("checklist") or []:
        items = "；".join(str(item) for item in (group.get("items") or ()))
        lines.append("- %s：%s" % (str(group.get("group") or ""), items))
    if template.get("boundary"):
        lines.append("【边界】%s" % str(template["boundary"]))
    if template.get("capability_note"):
        lines.append("【能力说明】%s" % str(template["capability_note"]))
    return lines


__all__ = ["TASK_TEMPLATES", "DEFAULT_TEMPLATE", "match_task_template", "render_task_brief"]
