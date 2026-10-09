#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 00 F-1 守门测试：`tools/qa_baseline_inventory.py` 的只读基线清单。

守的是一件事：**清单本身可信、可比对**。具体覆盖：
  1. 工具能跑通并把 JSON 写到指定路径（用 tmp_path，仓库零污染）；
  2. `acceptance.passed` 为 True 且逐项自检全通过；
  3. 七个契约 schema 指纹齐备、两两不同，且两次运行稳定；
  4. 仓库内依赖边非空且不含自环；
  5. 问题集指纹两次运行稳定。

说明：工具以子进程方式调用（顺带验 CLI 与退出码），比 in-process 调用更能挡住
"cwd 不对 / sys.path 没挂仓库根 / stdout 编码不纯" 这类只在命令行触发的问题。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPO_ROOT / "tools" / "qa_baseline_inventory.py"

# 与包内 Phase 00 要求一致的七个契约 schema
SCHEMA_NAMES = (
    "EVIDENCE_SCHEMA",
    "CLAIM_SCHEMA",
    "CONFLICT_SCHEMA",
    "LEVEL1_RESULT_SCHEMA",
    "LEVEL2_RESULT_SCHEMA",
    "FINAL_ANSWER_SCHEMA",
    "QA_EVENT_SCHEMA",
)

# P00-01 清单里必须覆盖的 QA/V2 模块数量（与工具 REQUIRED_MODULES 对齐）
REQUIRED_MODULE_COUNT = 20

_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_UTC_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _run_tool(out_path: Path, *, extra_args=()) -> subprocess.CompletedProcess:
    """子进程调用工具：产物写临时路径，stdout 抓回来验 `--print` 是否为纯 JSON。"""
    return subprocess.run(
        [sys.executable, str(TOOL_PATH), "--out", str(out_path), "--print", *extra_args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


def _failing_checks(stdout: str) -> str:
    """从 `--print` 的 stdout 里挑出未通过的自检项，便于失败时一眼定位。"""
    try:
        payload = json.loads(stdout)
    except Exception:  # noqa: BLE001 —— 诊断辅助函数，解析不了就退回原始尾部
        return "stdout 无法解析为 JSON：%s" % stdout[-500:]
    bad = [item for item in payload.get("acceptance", {}).get("checks", []) if not item.get("ok")]
    return json.dumps(bad, ensure_ascii=False) if bad else "（无失败自检项，可能是环境/并发编辑导致）"


@pytest.fixture(scope="module")
def inventory_runs(tmp_path_factory):
    """连跑两次工具：第一份断言字段，两份一起断言指纹稳定（跨运行）。"""
    out_dir = tmp_path_factory.mktemp("qa-baseline-inventory")
    runs = []
    for index in (1, 2):
        out_path = out_dir / ("run%d.json" % index)
        result = _run_tool(out_path)
        assert result.returncode == 0, "工具退出码 %s（expect 0），失败自检：%s\nstderr:\n%s" % (
            result.returncode,
            _failing_checks(result.stdout),
            result.stderr[-2000:],
        )
        assert out_path.is_file(), "工具没有写出 JSON 产物：%s" % out_path
        runs.append(
            {
                "path": out_path,
                "payload": json.loads(out_path.read_text(encoding="utf-8")),
                "stdout": result.stdout,
            }
        )
    return runs


@pytest.fixture(scope="module")
def inventory(inventory_runs):
    """第一次运行的清单（字段类断言的公共输入）。"""
    return inventory_runs[0]["payload"]


def test_tool_writes_json_to_given_path(tmp_path):
    """① 工具能跑通并把 JSON 写出来（--out 支持多级不存在的目录）。"""
    out_path = tmp_path / "nested" / "dir" / "inventory.json"
    result = _run_tool(out_path)

    assert result.returncode == 0, "失败自检：%s\nstderr:\n%s" % (
        _failing_checks(result.stdout),
        result.stderr[-2000:],
    )
    assert out_path.is_file(), "产物未生成：%s" % out_path

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    # --print 打的必须是同一份纯 JSON（stdout 掺日志会直接解析失败）
    assert json.loads(result.stdout) == payload, "--print 的 stdout 与产物 JSON 不一致"

    assert payload["manifest_version"] == "qa-baseline-inventory-v1"
    assert _UTC_Z.match(payload["captured_at_utc"]), "captured_at_utc 不是 UTC ISO8601(Z)：%s" % payload[
        "captured_at_utc"
    ]
    # 默认产物路径必须留在 baseline/ 下（改默认值会让留档跑偏）
    assert payload["host"]["repo_root"] == str(REPO_ROOT)


def test_degrades_gracefully_without_repo_modules(tmp_path):
    """契约层取不到时必须优雅降级：仍写出 JSON、schemas 为 null、自检记失败原因，绝不崩。

    做法：把工具单独拷进一个空的"假仓库"里跑（只有 tools/ 一个脚本，没有任何仓库模块）。
    顺带证明工具的所有路径都相对脚本自身定位，不依赖 cwd。
    """
    fake_root = tmp_path / "fake-repo"
    (fake_root / "tools").mkdir(parents=True)
    fake_tool = fake_root / "tools" / "qa_baseline_inventory.py"
    shutil.copyfile(TOOL_PATH, fake_tool)
    out_path = fake_root / "out.json"

    result = subprocess.run(
        [sys.executable, str(fake_tool), "--out", str(out_path), "--print"],
        cwd=str(tmp_path),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )

    # 自检没通过就必须以非零退出码暴露，而不是静默"成功"
    assert result.returncode == 1, "契约层不可用时退出码应为 1，实际 %s" % result.returncode
    payload = json.loads(result.stdout)

    assert payload["contracts"]["schemas"] is None
    assert payload["contracts"]["import_error"], "降级时必须记录 import 失败原因"
    assert payload["acceptance"]["passed"] is False

    failed = {item["name"] for item in payload["acceptance"]["checks"] if not item["ok"]}
    for name in (
        "required_modules_present",
        "seven_schema_fingerprints_present",
        "dependency_edges_nonempty",
        "acceptance_questions_parsable",
    ):
        assert name in failed, "%s 在实际失败时没有被标记" % name

    assert payload["dependencies"] == []
    # 降级路径同样必须落盘，留档不能因为 import 失败就丢
    assert out_path.is_file(), "降级路径也必须写出 JSON 产物"


def test_default_out_path_is_baseline_manifest():
    """默认产物路径固定为 baseline/qa-baseline-inventory.json（无需跑 CLI 即可校验）。"""
    from tools.qa_baseline_inventory import DEFAULT_OUT, MANIFEST_VERSION

    assert MANIFEST_VERSION == "qa-baseline-inventory-v1"
    assert DEFAULT_OUT == Path("baseline") / "qa-baseline-inventory.json"


def test_acceptance_passed_with_all_checks_ok(inventory):
    """② acceptance.passed 为 True，且没有靠"跳过自检"混过去。"""
    acceptance = inventory["acceptance"]
    assert acceptance["passed"] is True, "自检未全通过：%s" % json.dumps(
        [c for c in acceptance["checks"] if not c["ok"]], ensure_ascii=False
    )

    checks = acceptance["checks"]
    assert len(checks) >= 4, "自检项太少，覆盖不足：%d" % len(checks)

    names = {item["name"] for item in checks}
    for required in (
        "required_modules_present",
        "seven_schema_fingerprints_present",
        "dependency_edges_nonempty",
        "acceptance_questions_parsable",
    ):
        assert required in names, "缺少必需自检项：%s" % required

    for item in checks:
        assert set(item) == {"name", "ok", "detail"}, "自检项字段不稳定：%s" % sorted(item)
        assert item["detail"], "自检项 %s 没有说明" % item["name"]


def test_modules_have_fingerprints(inventory):
    """模块段落：20 个必需模块都在，且都有 sha256 与字节数。"""
    modules = inventory["modules"]
    assert modules["count"] == REQUIRED_MODULE_COUNT
    assert modules["present_count"] == REQUIRED_MODULE_COUNT
    assert modules["missing"] == []
    assert len(modules["files"]) == REQUIRED_MODULE_COUNT

    for entry in modules["files"]:
        assert entry["exists"] is True, "%s 不存在" % entry["name"]
        assert _HEX64.match(entry["sha256"] or ""), "%s 的 sha256 非法：%s" % (entry["name"], entry["sha256"])
        assert entry["bytes"] > 0
        assert entry["ast_parsed"] is True, "%s ast 解析失败：%s" % (entry["name"], entry["parse_error"])


def test_schema_fingerprints_present_distinct_and_stable(inventory_runs):
    """③ 七个 schema 指纹齐备、互不相同，且两次运行稳定。"""
    fingerprints = []
    for run in inventory_runs:
        schemas = run["payload"]["contracts"]["schemas"]
        assert schemas is not None, "contracts.schemas 为 null：%s" % run["payload"]["contracts"]["import_error"]
        assert sorted(schemas) == sorted(SCHEMA_NAMES)
        for name in SCHEMA_NAMES:
            entry = schemas[name]
            assert entry is not None, "%s 没有指纹" % name
            assert _HEX16.match(entry["fingerprint"]), "%s 指纹非法：%s" % (name, entry["fingerprint"])
            assert isinstance(entry["required"], list) and entry["required"], "%s 缺少顶层 required" % name
        fingerprints.append({name: schemas[name]["fingerprint"] for name in SCHEMA_NAMES})

    # 同一 schema 两次运行必须完全一致（指纹只随 schema 内容变化）
    assert fingerprints[0] == fingerprints[1], "两次运行的 schema 指纹不稳定：%s" % json.dumps(
        fingerprints, ensure_ascii=False
    )
    # 七个指纹两两不同：防止某个 schema 被误写成另一份
    values = list(fingerprints[0].values())
    assert len(set(values)) == len(SCHEMA_NAMES), "schema 指纹存在重复：%s" % values

    # 版本常量也要留档（P00-02 的"冻结原 API contract"）
    versions = inventory_runs[0]["payload"]["contracts"]["versions"]
    for key in (
        "QA_CONTRACT_VERSION",
        "QA_SSE_PROTOCOL_VERSION",
        "QA_SCHEMA_VERSION",
        "GRAPH_CONTRACT_VERSION",
    ):
        assert versions.get(key), "版本常量没取到：%s" % key


def test_dependency_edges_nonempty_and_acyclic_per_module(inventory):
    """④ 依赖边非空、无自环、无重复，且端点都是仓库内 .py 文件。"""
    edges = inventory["dependencies"]
    assert edges, "依赖边为空：ast 解析或仓库内模块解析失败"

    for edge in edges:
        assert set(edge) == {"from", "to"}, "依赖边字段不稳定：%s" % sorted(edge)
        assert edge["from"] != edge["to"], "出现自环边：%s" % edge
        assert edge["from"].endswith(".py") and edge["to"].endswith(".py"), "端点不是仓库模块：%s" % edge

    pairs = [(edge["from"], edge["to"]) for edge in edges]
    assert len(pairs) == len(set(pairs)), "依赖边有重复"
    assert len(pairs) == inventory["modules"]["dependency_edge_count"]

    # 每个必需模块的 internal_imports 必须与边表自洽
    for entry in inventory["modules"]["files"]:
        expected = {edge["to"] for edge in edges if edge["from"] == entry["name"]}
        assert set(entry["internal_imports"]) == expected, "%s 的 internal_imports 与边表不一致" % entry["name"]


def test_benchmark_question_fingerprints_stable(inventory_runs):
    """⑤ 问题集指纹两次运行稳定，题数与指纹列表长度一致。"""
    benchmarks = [run["payload"]["benchmark"] for run in inventory_runs]
    for benchmark in benchmarks:
        assert benchmark["parse_error"] is None, benchmark["parse_error"]
        assert benchmark["question_count"] >= 1
        assert _HEX64.match(benchmark["file_sha256"] or "")
        assert _HEX16.match(benchmark["questions_fingerprint"] or "")
        assert len(benchmark["question_fingerprints"]) == benchmark["question_count"]
        for item in benchmark["question_fingerprints"]:
            assert _HEX16.match(item["fingerprint"]), "题目指纹非法：%s" % item

    assert benchmarks[0]["file_sha256"] == benchmarks[1]["file_sha256"], "问题集文件两次运行哈希不同"
    assert benchmarks[0]["questions_fingerprint"] == benchmarks[1]["questions_fingerprint"], "问题集指纹不稳定"
    assert benchmarks[0]["question_fingerprints"] == benchmarks[1]["question_fingerprints"], "单题指纹不稳定"
