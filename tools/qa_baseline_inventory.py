#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 00 F-1：graph-rag-v2 通用包的 QA/V2 模块与依赖「只读」基线清单。

产出 `baseline/qa-baseline-inventory.json`，把三件事固化成可留档、可比对的指纹：

1. 环境：Python 版本 / 平台 / 关键三方包版本（import 得到的才算，取不到写 null）；
2. 模块与依赖：20 个 QA/V2 相关顶层模块的 sha256 + 字节数，以及用 `ast` 解析出的
   仓库内依赖边（形如 `qa_pipeline.py -> qa_retrieval.py`）；
3. 契约指纹：`qa_contracts` 里七个 schema 的规范化 sha256 前 16 位 + 顶层 required；
   以及 `config/qa_acceptance_questions.json` 的题数与题目指纹。

**只读约束（本工具的全部外部行为）**：
    - 不连数据库、不调 LLM、不发网络请求；
    - 只读仓库文件；唯一写入是它自己的 JSON 产物（`--out` 指定，默认 baseline/ 下）；
    - 依赖只有标准库 + 仓库内 `qa_contracts` / `qa_schema`（后者仅取版本常量）。
      `qa_contracts` import 失败时**优雅降级**：schema 段落写 null，并在
      `acceptance.checks` 里留下失败原因，工具不崩。

CLI：
    python tools/qa_baseline_inventory.py                      # 写到默认路径
    python tools/qa_baseline_inventory.py --out <path>          # 覆盖产物路径
    python tools/qa_baseline_inventory.py --print               # 额外把 JSON 打到 stdout
退出码：`acceptance.passed` 为真返回 0，否则返回 1（可直接当 Phase Gate 的一环）。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

# 清单结构版本：字段增删改时必须递增，避免旧留档与新留档被误当成同格式对比
MANIFEST_VERSION = "qa-baseline-inventory-v1"

# 仓库根目录 = tools/ 的上一级。所有相对路径都以此为基准，不受调用时 cwd 影响
REPO_ROOT = Path(__file__).resolve().parents[1]

# 默认产物（相对仓库根）
DEFAULT_OUT = Path("baseline") / "qa-baseline-inventory.json"

# P00-01 要求覆盖的 QA/V2 相关模块（均为仓库顶层模块文件）
REQUIRED_MODULES = (
    "qa_contracts.py",
    "qa_schema.py",
    "qa_storage.py",
    "qa_orchestrator.py",
    "qa_pipeline.py",
    "qa_planner.py",
    "qa_query_decompose.py",
    "qa_retrieval.py",
    "qa_ranking_weights.py",
    "qa_level1.py",
    "qa_synthesis.py",
    "qa_attribution.py",
    "qa_observability.py",
    "qa_reasoning.py",
    "qa_graph_contracts.py",
    "kg_builder.py",
    "business_rules.py",
    "intel_schema.py",
    "intel_worker.py",
    "intel_database.py",
)

# P00-02 要求冻结字段指纹的七个契约 schema（都定义在 qa_contracts.py）
SCHEMA_NAMES = (
    "EVIDENCE_SCHEMA",
    "CLAIM_SCHEMA",
    "CONFLICT_SCHEMA",
    "LEVEL1_RESULT_SCHEMA",
    "LEVEL2_RESULT_SCHEMA",
    "FINAL_ANSWER_SCHEMA",
    "QA_EVENT_SCHEMA",
)

# 需要留档的版本常量：值来源模块 + 常量名
VERSION_CONSTANTS = (
    ("QA_CONTRACT_VERSION", "qa_contracts"),
    ("QA_SSE_PROTOCOL_VERSION", "qa_contracts"),
    ("QA_SCHEMA_VERSION", "qa_schema"),
    ("GRAPH_CONTRACT_VERSION", "qa_contracts"),
)

# 关键三方包：能 import 才取版本，取不到写 null 并在 host.packages_unavailable 说明原因
HOST_PACKAGES = (
    "flask",
    "jsonschema",
    "psycopg2",
    "requests",
    "redis",
    "playwright",
    "pydantic",
)

# 验收问题集（benchmark 段落）
QUESTIONS_FILE = Path("config") / "qa_acceptance_questions.json"

# 指纹长度（前 16 位十六进制，够用且便于人工比对）
FINGERPRINT_CHARS = 16


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------
def _normalize_json(value) -> str:
    """规范化序列化：sort_keys 保证键序无关，ensure_ascii=False 保证中文原样参与哈希。"""
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _fingerprint(value) -> str:
    """对任意 JSON 可序列化对象取规范化 sha256 前 16 位。"""
    return hashlib.sha256(_normalize_json(value).encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]


def _sha256_file(path: Path) -> str:
    """文件内容 sha256（分块读取，避免大文件一次性进内存）。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now_z() -> str:
    """UTC ISO8601，带 Z 后缀（秒级，避免留档出现本地时区歧义）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check(name: str, ok: bool, detail: str) -> dict:
    """构造一条自检记录。"""
    return {"name": name, "ok": bool(ok), "detail": detail}


def _ensure_repo_on_path(root: Path) -> None:
    """把仓库根挂到 sys.path。

    本脚本位于 tools/ 下，`python tools/qa_baseline_inventory.py` 启动时 sys.path[0]
    是 tools/ 而不是仓库根，直接 `import qa_contracts` 会 ModuleNotFoundError。
    """
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


# --------------------------------------------------------------------------
# host：Python / 平台 / 关键包版本
# --------------------------------------------------------------------------
def _package_version(name: str):
    """尝试 import 目标包并取版本。

    返回 (version_or_None, note_or_None)。「能 import 到的才算」：
    import 失败直接判为不可用；import 成功但缺发行版元数据时退回模块 __version__。
    """
    try:
        module = importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001 —— 任何 import 异常都只降级为 null
        return None, "import 失败：%s: %s" % (type(exc).__name__, exc)

    try:
        return importlib.metadata.version(name), None
    except importlib.metadata.PackageNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        return None, "元数据读取失败：%s: %s" % (type(exc).__name__, exc)

    version = getattr(module, "__version__", None)
    if version:
        # 部分包（如 psycopg2）的 __version__ 带构建标记 "2.9.12 (dt dec pq3 ext lo64)"，
        # 留档只取首个 token 避免构建差异污染版本对比，完整值写进说明
        version_text = str(version)
        return version_text.split()[0], "无发行版元数据，退回模块 __version__（原值：%s）" % version_text
    return None, "import 成功但既无发行版元数据也无模块 __version__"


def _collect_host(root: Path) -> dict:
    """采集宿主环境信息（纯读取，无副作用）。"""
    packages = {}
    unavailable = {}
    notes = {}
    for name in HOST_PACKAGES:
        version, note = _package_version(name)
        packages[name] = version
        if version is None:
            unavailable[name] = note or "不可用"
        if note:
            # 取到版本但走了降级路径（如无发行版元数据）时单独留说明，避免与 null 混淆
            notes[name] = note

    return {
        "python_version": platform.python_version(),
        "python_full_version": sys.version.replace("\n", " "),
        "python_implementation": platform.python_implementation(),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "repo_root": str(root),
        "cwd": os.getcwd(),
        "packages": packages,
        "packages_unavailable": unavailable,
        "package_notes": notes,
    }


# --------------------------------------------------------------------------
# modules：文件指纹 + ast 依赖边
# --------------------------------------------------------------------------
def _resolve_internal(target: str, root: Path):
    """把 import 目标名解析成仓库内模块文件（相对 root 的 posix 路径），不在仓库内返回 None。

    逐级下沉匹配：`a.b.c` 依次尝试 a/b/c/__init__.py、a/b/c.py、a/b/__init__.py、
    a/b.py、a/__init__.py、a.py，取最具体的一个。
    """
    if not target:
        return None
    parts = target.split(".")
    for depth in range(len(parts), 0, -1):
        candidate = parts[:depth]
        pkg_init = root.joinpath(*candidate, "__init__.py")
        if pkg_init.is_file():
            return pkg_init.relative_to(root).as_posix()
        mod_file = root.joinpath(*candidate).with_suffix(".py")
        if mod_file.is_file():
            return mod_file.relative_to(root).as_posix()
    return None


def _relative_import_target(node: ast.ImportFrom, source_rel: str) -> str:
    """把相对 import（level>0）还原成绝对模块名；无法还原时返回空串。"""
    parts = list(Path(source_rel).parts)
    base = parts[:-1]  # 当前文件所在目录 = 其所属包
    # level=1 指当前包本身，level=2 再向上一层，以此类推
    up = max(node.level - 1, 0)
    if up:
        base = base[: max(len(base) - up, 0)]
    if node.module:
        base = base + node.module.split(".")
    return ".".join(base)


def _collect_module_imports(path: Path, source_rel: str, root: Path, stdlib_names):
    """用 ast 解析一个模块的 import，产出 (仓库内依赖, 标准库名, 三方包名)。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        # 解析失败不致命：记录错误，该模块的依赖边为空，由自检项兜住
        return set(), set(), set(), "%s: %s" % (type(exc).__name__, exc)
    except Exception as exc:  # noqa: BLE001
        return set(), set(), set(), "%s: %s" % (type(exc).__name__, exc)

    targets = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            # `import a.b.c` 绑定的是 a，但真正引入的模块是 a.b.c
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative = _relative_import_target(node, source_rel)
                if relative:
                    targets.append(relative)
            elif node.module:
                targets.append(node.module)

    internal, stdlib, third_party = set(), set(), set()
    for target in targets:
        internal_path = _resolve_internal(target, root)
        if internal_path:
            internal.add(internal_path)
            continue
        top = target.split(".")[0]
        if top in stdlib_names:
            stdlib.add(top)
        else:
            third_party.add(top)
    return internal, stdlib, third_party, None


def _collect_modules(root: Path) -> tuple:
    """扫描必需模块：存在性 / 字节数 / sha256 / 依赖分类，并汇总全仓库依赖边。"""
    stdlib_names = getattr(sys, "stdlib_module_names", frozenset())
    files = []
    missing = []
    parse_errors = {}
    edges = set()

    for name in REQUIRED_MODULES:
        path = root / name
        if not path.is_file():
            missing.append(name)
            files.append(
                {
                    "name": name,
                    "path": name,
                    "exists": False,
                    "bytes": None,
                    "sha256": None,
                    "ast_parsed": False,
                    "parse_error": None,
                    "internal_imports": [],
                    "stdlib_imports": [],
                    "third_party_imports": [],
                }
            )
            continue

        internal, stdlib, third_party, parse_error = _collect_module_imports(
            path, name, root, stdlib_names
        )
        # 排除自环：模块 import 到自己不可能产生真实依赖边
        internal.discard(name)
        for target in internal:
            if target != name:
                edges.add((name, target))
        if parse_error:
            parse_errors[name] = parse_error

        files.append(
            {
                "name": name,
                "path": name,
                "exists": True,
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
                "ast_parsed": parse_error is None,
                "parse_error": parse_error,
                "internal_imports": sorted(internal),
                "stdlib_imports": sorted(stdlib),
                "third_party_imports": sorted(third_party),
            }
        )

    # 三元组排序保证输出稳定（同一份代码两次运行字节一致，captured_at 除外）
    dependencies = [{"from": src, "to": dst} for src, dst in sorted(edges)]
    # 只作为边目标出现、本身不在必需清单里的仓库模块：给 P00-01 依赖清查做指引
    outside = sorted({edge["to"] for edge in dependencies} - set(REQUIRED_MODULES))

    summary = {
        "required": list(REQUIRED_MODULES),
        "count": len(REQUIRED_MODULES),
        "present_count": len(REQUIRED_MODULES) - len(missing),
        "missing": missing,
        "files": files,
        "parse_errors": parse_errors,
        "dependency_edge_count": len(dependencies),
        "dependency_targets_outside_manifest": outside,
    }
    return summary, dependencies


# --------------------------------------------------------------------------
# contracts：七个 schema 的字段指纹 + 版本常量
# --------------------------------------------------------------------------
def _collect_contracts() -> tuple:
    """取七个 schema 的指纹与版本常量；import 失败时整段降级为 null。"""
    contracts = {
        "source_module": "qa_contracts.py",
        "versions": {name: None for name, _ in VERSION_CONSTANTS},
        "schemas": None,
        "import_error": None,
        "schema_errors": {},
        "version_errors": {},
    }

    # 版本常量可能分处两个模块，逐个 import：任一失败只影响它自己的那条
    for const_name, module_name in VERSION_CONSTANTS:
        try:
            module = importlib.import_module(module_name)
            contracts["versions"][const_name] = getattr(module, const_name, None)
            if contracts["versions"][const_name] is None:
                contracts["version_errors"][const_name] = "%s 里没有该常量" % module_name
        except Exception as exc:  # noqa: BLE001
            contracts["version_errors"][const_name] = "import %s 失败：%s: %s" % (
                module_name,
                type(exc).__name__,
                exc,
            )

    try:
        qa_contracts = importlib.import_module("qa_contracts")
    except Exception as exc:  # noqa: BLE001 —— 契约层取不到时不崩，交给自检项判定
        contracts["import_error"] = "%s: %s" % (type(exc).__name__, exc)
        return contracts, "import qa_contracts 失败：%s" % contracts["import_error"]

    schemas = {}
    for schema_name in SCHEMA_NAMES:
        schema = getattr(qa_contracts, schema_name, None)
        if schema is None:
            contracts["schema_errors"][schema_name] = "qa_contracts 里没有该 schema"
            schemas[schema_name] = None
            continue
        try:
            properties = schema.get("properties") or {}
            schemas[schema_name] = {
                "fingerprint": _fingerprint(schema),
                "required": list(schema.get("required") or []),
                "property_count": len(properties),
                "top_level_type": schema.get("type"),
                "additional_properties": schema.get("additionalProperties"),
            }
        except Exception as exc:  # noqa: BLE001
            contracts["schema_errors"][schema_name] = "%s: %s" % (type(exc).__name__, exc)
            schemas[schema_name] = None

    contracts["schemas"] = schemas
    return contracts, None


# --------------------------------------------------------------------------
# benchmark：验收问题集
# --------------------------------------------------------------------------
def _collect_benchmark(root: Path) -> dict:
    """统计问题集题数并取题目指纹（id+question 规范化后 sha256 前 16 位）。

    兼容两种形态（该文件在演进中，两种都可能是当前形态）：
      - 裸数组：`[{id, question, ...}, ...]`；
      - 信封对象：`{benchmark_version, corpus_snapshot_id, generated_at, questions: [...]}`，
        信封里的元数据一并留档，便于把题目指纹绑定到具体 benchmark 版本。
    """
    path = root / QUESTIONS_FILE
    benchmark = {
        "path": QUESTIONS_FILE.as_posix(),
        "exists": path.is_file(),
        "file_sha256": None,
        "container": None,
        "metadata": {},
        "question_count": 0,
        "questions_fingerprint": None,
        "question_fingerprints": [],
        "parse_error": None,
    }
    if not path.is_file():
        benchmark["parse_error"] = "文件不存在"
        return benchmark

    benchmark["file_sha256"] = _sha256_file(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        benchmark["parse_error"] = "%s: %s" % (type(exc).__name__, exc)
        return benchmark

    if isinstance(payload, list):
        benchmark["container"] = "list"
        questions = payload
    elif isinstance(payload, dict):
        benchmark["container"] = "dict"
        benchmark["metadata"] = {
            "keys": sorted(payload),
            "benchmark_version": payload.get("benchmark_version"),
            "corpus_snapshot_id": payload.get("corpus_snapshot_id"),
            "generated_at": payload.get("generated_at"),
        }
        questions = payload.get("questions")
        if not isinstance(questions, list):
            benchmark["parse_error"] = "信封对象里没有 questions 数组（实际 keys：%s）" % sorted(payload)
            return benchmark
    else:
        benchmark["parse_error"] = "顶层既不是数组也不是对象（实际为 %s）" % type(payload).__name__
        return benchmark

    items = []
    for index, entry in enumerate(questions):
        if isinstance(entry, dict):
            # 题目指纹：id + question 拼成固定分隔串再规范化取哈希（换行分隔避免拼接歧义）
            question_id = str(entry.get("id", ""))
            question_text = str(entry.get("question", ""))
            fingerprint = _fingerprint("%s\n%s" % (question_id, question_text))
        else:
            # 结构异常时退回整条记录指纹，保证指纹列表长度与题数一致
            question_id = "#%d" % index
            question_text = ""
            fingerprint = _fingerprint(entry)
        items.append({"id": question_id, "fingerprint": fingerprint})

    benchmark["question_count"] = len(questions)
    benchmark["question_fingerprints"] = items
    # 汇总指纹：整份题目列表（id+question）的规范化哈希，便于一眼比对问题集是否漂移
    benchmark["questions_fingerprint"] = _fingerprint(
        [(item["id"], item["fingerprint"]) for item in items]
    )
    return benchmark


# --------------------------------------------------------------------------
# acceptance：自检
# --------------------------------------------------------------------------
def _build_acceptance(modules: dict, dependencies: list, contracts: dict, benchmark: dict, contracts_error) -> dict:
    """把「Phase 00 交付物是否齐备」翻译成可机读的自检清单。"""
    checks = []

    missing = modules["missing"]
    checks.append(
        _check(
            "required_modules_present",
            not missing,
            "20 个必需模块全部存在" if not missing else "缺失模块：%s" % ", ".join(missing),
        )
    )

    no_fingerprint = [f["name"] for f in modules["files"] if f["exists"] and not f["sha256"]]
    checks.append(
        _check(
            "module_fingerprints_complete",
            not no_fingerprint,
            "所有存在的模块都拿到 sha256"
            if not no_fingerprint
            else "以下模块没拿到 sha256：%s" % ", ".join(no_fingerprint),
        )
    )

    checks.append(
        _check(
            "modules_ast_parsed",
            not modules["parse_errors"],
            "全部模块 ast 解析成功"
            if not modules["parse_errors"]
            else "解析失败：%s" % json.dumps(modules["parse_errors"], ensure_ascii=False),
        )
    )

    schemas = contracts.get("schemas") or {}
    missing_schemas = [name for name in SCHEMA_NAMES if not schemas.get(name)]
    checks.append(
        _check(
            "seven_schema_fingerprints_present",
            not missing_schemas and contracts_error is None,
            "七个契约 schema 指纹齐备"
            if not missing_schemas and contracts_error is None
            else "取不到指纹：%s%s"
            % (
                ", ".join(missing_schemas) or "无",
                "" if contracts_error is None else "（%s）" % contracts_error,
            ),
        )
    )

    fingerprints = [schemas[name]["fingerprint"] for name in SCHEMA_NAMES if schemas.get(name)]
    checks.append(
        _check(
            "seven_schema_fingerprints_distinct",
            len(fingerprints) == len(set(fingerprints)) and len(fingerprints) == len(SCHEMA_NAMES),
            "七个指纹两两不同（%d 个）" % len(set(fingerprints))
            if len(fingerprints) == len(set(fingerprints))
            else "指纹存在重复，schema 可能被误写成同一份：%s" % ", ".join(fingerprints),
        )
    )

    self_loops = [edge for edge in dependencies if edge["from"] == edge["to"]]
    checks.append(
        _check(
            "dependency_edges_nonempty",
            len(dependencies) > 0,
            "仓库内依赖边 %d 条" % len(dependencies),
        )
    )
    checks.append(
        _check(
            "dependency_edges_no_self_loop",
            not self_loops,
            "无自环边"
            if not self_loops
            else "自环边：%s" % json.dumps(self_loops, ensure_ascii=False),
        )
    )

    checks.append(
        _check(
            "acceptance_questions_parsable",
            benchmark["parse_error"] is None and benchmark["question_count"] >= 1,
            "问题集可解析（顶层为 %s），题数 %d" % (benchmark["container"], benchmark["question_count"])
            if benchmark["parse_error"] is None and benchmark["question_count"] >= 1
            else "问题集不可用：%s" % (benchmark["parse_error"] or "题数为 0"),
        )
    )

    return {"passed": all(item["ok"] for item in checks), "checks": checks}


# --------------------------------------------------------------------------
# 组装 + CLI
# --------------------------------------------------------------------------
def build_inventory(root: Path = REPO_ROOT) -> dict:
    """采集完整清单（纯读；不连库、不调 LLM、不写除产物外的任何东西）。"""
    _ensure_repo_on_path(root)
    host = _collect_host(root)
    modules, dependencies = _collect_modules(root)
    contracts, contracts_error = _collect_contracts()
    benchmark = _collect_benchmark(root)
    acceptance = _build_acceptance(modules, dependencies, contracts, benchmark, contracts_error)

    return {
        "manifest_version": MANIFEST_VERSION,
        "captured_at_utc": _utc_now_z(),
        "scope": (
            "只读基线：不连数据库、不调 LLM、不发网络请求；"
            "除本 JSON 产物外不写仓库任何文件。"
        ),
        "host": host,
        "modules": modules,
        "dependencies": dependencies,
        "contracts": contracts,
        "benchmark": benchmark,
        "acceptance": acceptance,
    }


def _dump_json(payload: dict) -> str:
    """统一产物序列化：缩进 2 空格、中文原样（不转义），便于人工 review 与 diff。"""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)


def _emit_stdout(text: str) -> None:
    """把 JSON 按 UTF-8 写 stdout。

    Windows 上 Python 的 stdout 默认跟随 locale（本机实测 gbk），直接 print 中文会乱码；
    这里显式写 UTF-8 字节，与控制台 UTF-8 输出编码一致。
    """
    data = (text + "\n").encode("utf-8")
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(data)
        buffer.flush()
        return
    # 极少数没有 buffer 的宿主（如被重定向包装）退回文本写，允许丢字符但不炸
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase 00 F-1：QA/V2 模块与依赖只读基线清单（graph-rag-v2 通用包）"
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="产物路径（相对路径按仓库根解析），默认 baseline/qa-baseline-inventory.json",
    )
    parser.add_argument("--print", dest="print_json", action="store_true", help="把 JSON 打到 stdout")
    args = parser.parse_args(argv)

    # 抑制 import 期间的 DeprecationWarning 等噪音，保证 --print 的 stdout 是纯 JSON
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        payload = build_inventory(REPO_ROOT)

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = REPO_ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = _dump_json(payload)
    out_path.write_text(text + "\n", encoding="utf-8")

    if args.print_json:
        _emit_stdout(text)

    return 0 if payload["acceptance"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
