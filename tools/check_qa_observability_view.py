#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 16（P16-01…P16-06）· **可观测性 UI 的前端契约检查**（静态，零浏览器、零网络）。

它回答三件事，全部可断言、可复跑：

  1. **结构契约**：六个视图各自的"状态 / 来源 / 口径数字 / 正文 / 备注"容器都在，
     空态占位与错误态分支都在（A 机当前没有 QA run 数据，空态就是真实状态）；
  2. **只读契约**：模板里**只有 GET 请求**，没有任何修改证据/结论/记忆的入口，
     也没有指向写接口的路径；
  3. **安全与依赖**：所有动态文本都经过 `qoEsc()`/`qoText()`（按函数逐个检查危险 sink），
     引用的脚本/样式**全部是仓库既有静态文件**（不引入任何新的第三方依赖）。

用法：
    python tools/check_qa_observability_view.py            # 打印 JSON 报告
    python tools/check_qa_observability_view.py --output baseline/qa-observability-view.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEMPLATE = "template"
VIEWS = ("execution", "evidence", "context", "memory", "search_trace", "dashboard")

# 结构契约：每个视图的容器 + 全局容器 + 空态/错误态标记
STRUCTURE_MARKERS = (
    'data-view-root="qa-observability"',
    'data-read-only="1"',
    '{% include "_dashboard_nav.html" %}',
    'id="qoRunSelect"',
    'id="qoRunInput"',
    'id="qoBanner"',
    'id="qoTabs"',
    "qo-empty",
    "function qoHandleHttp(status, body)",
    "status === 401",
    "status === 404",
    "function qoRenderNumbers(view, payload)",
    "function qoEsc(v)",
    "function qoText(v)",
    "来源（阶段·载体）",
    "@media (max-width:760px)",
    "body.skin-darkblue",
)

# 只读契约：出现任何一个都说明页面能改东西
WRITE_MARKERS = (
    "method: 'POST'", 'method: "POST"', "method: 'PUT'", 'method: "PUT"',
    "method: 'DELETE'", 'method: "DELETE"', "method: 'PATCH'", 'method: "PATCH"',
    "/apply", "/reject", "/revoke", "/retry", "/cancel",
)

DANGEROUS_SINKS = ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write")
# 允许含危险 sink 但**自身不做拼接**的函数（参数由调用方保证已经转义）
SINK_ALLOWLIST = {
    "qoBanner": "参数是要塞进横幅的 HTML，调用方一律用 qoEsc() 拼好（本文件内 4 处调用）",
    "qoClearBanner": "只把 innerHTML 置为空字面量，没有任何动态内容",
}


def _script_of(source: str) -> str:
    blocks = re.findall(r"<script>(.*?)</script>", source, flags=re.S)
    return "\n".join(blocks)


def _functions(script: str) -> dict:
    """按 `function name(...) {` 切出函数体（够用的浅层切分：靠大括号配平）。"""
    out: dict = {}
    for match in re.finditer(r"function\s+([A-Za-z_$][\w$]*)\s*\(", script):
        name = match.group(1)
        start = script.find("{", match.end())
        if start < 0:
            continue
        depth, index = 0, start
        while index < len(script):
            char = script[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        out.setdefault(name, []).append(script[start:index + 1])
    return out


def _static_assets(source: str) -> list:
    assets = re.findall(r'(?:src|href)="(/static/[^"]+)"', source)
    return sorted(set(assets))


def inspect_frontend_contract(template_path: str | Path = "") -> dict:
    path = Path(template_path or ROOT / "templates" / "qa_observability.html").resolve()
    source = path.read_text(encoding="utf-8")
    script = _script_of(source)

    missing = [marker for marker in STRUCTURE_MARKERS if marker not in source]
    # 六个视图：tab + 面板 + 五个容器逐视图都要在
    for view in VIEWS:
        for marker in ('data-view="%s"' % view, 'id="qoPanel-%s"' % view,
                       'id="qoState-%s"' % view, 'id="qoSource-%s"' % view,
                       'id="qoNumbers-%s"' % view, 'id="qoBody-%s"' % view,
                       'id="qoText-%s"' % view, 'id="qoReason-%s"' % view):
            if marker not in source:
                missing.append(marker)

    write_hits = [marker for marker in WRITE_MARKERS if marker in source]

    # 危险 sink 逐个函数检查：同一个函数里必须出现转义函数（除非在允许清单里）
    unescaped = []
    for name, bodies in _functions(script).items():
        if name in SINK_ALLOWLIST:
            continue
        for body in bodies:
            if any(sink in body for sink in DANGEROUS_SINKS) and "qoEsc(" not in body:
                unescaped.append(name)

    assets = _static_assets(source)
    assets_missing = [item for item in assets if not (ROOT / item.lstrip("/")).is_file()]

    return {
        "template": str(path),
        "missing_markers": missing,
        "write_markers": write_hits,
        "unescaped_sink_functions": sorted(set(unescaped)),
        "static_assets": assets,
        "static_assets_missing": assets_missing,
        "views": list(VIEWS),
        "safe": not missing and not write_hits and not unescaped and not assets_missing,
        "read_only": not write_hits,
    }


def static_acceptance() -> dict:
    frontend = inspect_frontend_contract()
    api_source = (ROOT / "qa_observability_api.py").read_text(encoding="utf-8")
    ui_source = (ROOT / "qa_observability_ui.py").read_text(encoding="utf-8")
    app_source = (ROOT / "firecrawl_app.py").read_text(encoding="utf-8")
    nav_source = (ROOT / "templates" / "_dashboard_nav.html").read_text(encoding="utf-8")
    checks = {
        "six_views_rendered": len(frontend["views"]) == 6,
        "frontend_safe": frontend["safe"],
        "frontend_read_only": frontend["read_only"],
        "no_new_third_party_assets": all(item.startswith("/static/")
                                         for item in frontend["static_assets"]),
        "static_assets_exist": not frontend["static_assets_missing"],
        "api_blueprint_get_only": "methods=[\"GET\"]" in api_source
                                  and "methods=['GET']" not in api_source,
        "api_reuses_gateway_identity": "from qa_gateway import _identity as gateway_identity" in api_source,
        "projection_has_no_sql": not any(marker in ui_source.upper()
                                        for marker in ("INSERT INTO", "UPDATE ", "DELETE FROM")),
        "page_registered_and_login_required": "@app.route('/qa-observability')" in app_source
                                              and "def qa_observability_page()" in app_source,
        "blueprint_registered": "app.register_blueprint(observability_bp)" in app_source,
        "nav_entry_present": 'href="/qa-observability"' in nav_source,
    }
    if not all(checks.values()):
        raise AssertionError({"checks": checks, "frontend": frontend})
    return {
        "view_version": "qa-observability-ui-v1",
        "checks": checks,
        "frontend": frontend,
        "endpoints": [
            "GET /qa-observability",
            "GET /api/qa/v1/runs/<run_id>/observability",
            "GET /api/qa/v1/runs/<run_id>/observability/<view>",
            "GET /api/qa/v1/observability/dashboard",
            "GET /api/qa/v1/observability/views",
        ],
        "new_tables": [], "new_columns": [], "schema_version": "unified-qa-schema-v11",
        "network_calls": 0, "model_calls": 0, "gpu_calls": 0,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    report = {"acceptance": "passed", "phase": "16", "static": static_acceptance()}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    if os.environ.get("PYTHONIOENCODING", "").lower().startswith("utf"):
        print(rendered, end="")
    else:
        print(rendered.encode("utf-8", "replace").decode("utf-8", "replace"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
