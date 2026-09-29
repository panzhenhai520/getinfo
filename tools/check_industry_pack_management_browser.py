#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Chromium acceptance for create/edit/delete and two-phase industry switching."""

from __future__ import annotations

import argparse
import copy
import json
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from playwright.sync_api import sync_playwright
from werkzeug.serving import WSGIRequestHandler, make_server


ROOT = Path(__file__).resolve().parents[1]


def _chromium(explicit: str = "") -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if path.is_file():
            return path
        raise FileNotFoundError(f"Chromium 不存在：{path}")
    candidates = sorted(
        (ROOT / "data" / "ms-playwright").glob(
            "chromium-*/chrome-linux*/chrome"
        ),
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError("未找到项目内 Playwright Chromium")
    return candidates[0].resolve()


class _Quiet(WSGIRequestHandler):
    def log_request(self, *args, **kwargs):
        return None


def _fixture_app(calls: list[dict]) -> Flask:
    app = Flask(
        "industry-pack-management-browser",
        template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
    )
    manifests = {
        pack_id: json.loads(
            (ROOT / "config" / "industry_packs" / f"{pack_id}.json").read_text(
                encoding="utf-8"
            )
        )
        for pack_id in ("family_office", "education_news")
    }
    manifests["education_news"]["default_sources"][0].update(
        {
            "source_import_id": "browser_fixture_feed",
            "validation_status": "blocked_403",
            "error": "HTTP 403",
            "is_enabled": False,
        }
    )
    manifests["education_news"]["fixed_topics"][0]["preferred_source_ids"] = [
        "browser_fixture_feed"
    ]
    revisions = {"family_office": 1, "education_news": 1}
    origins = {"family_office": "system", "education_news": "system"}
    published_ids = {"family_office", "education_news"}
    lifecycle_events: list[dict] = []

    @app.get("/")
    def page():
        return render_template("industry_pack_management.html")

    @app.get("/api/intel/source-authority-profiles")
    def authority_profiles():
        payload = json.loads(
            (ROOT / "config" / "source_authority_levels.json").read_text(
                encoding="utf-8"
            )
        )
        return jsonify(
            {
                "success": True,
                "schema_version": payload["schema_version"],
                "profiles": payload["roles"],
                "policies": payload["policies"],
            }
        )

    @app.get("/api/intel/industry-packs/admin")
    def packs():
        return jsonify(
            {
                "success": True,
                "default_industry_pack_id": "family_office",
                "industry_packs": [
                    {
                        "id": pack_id,
                        "name": manifests[pack_id]["name"],
                        "pack_version": manifests[pack_id]["pack_version"],
                        "default_market": manifests[pack_id]["default_market"],
                        "timezone": manifests[pack_id]["timezone"],
                        "origin": origins[pack_id],
                        "draft_only": pack_id not in published_ids,
                    }
                    for pack_id in sorted(manifests)
                ],
            }
        )

    @app.post("/api/intel/industry-packs/custom")
    def create_custom():
        payload = request.get_json(silent=True) or {}
        pack_id = str(payload.get("id") or "")
        manifest = copy.deepcopy(manifests["education_news"])
        manifest.update(
            {
                "id": pack_id,
                "name": str(payload.get("name") or ""),
                "default_market": str(payload.get("default_market") or "GLOBAL"),
                "timezone": str(payload.get("timezone") or "Asia/Hong_Kong"),
                "core_keywords": [],
                "expanded_keywords": [],
                "default_sources": [],
                "fixed_topics": [],
            }
        )
        manifests[pack_id] = manifest
        revisions[pack_id] = 1
        origins[pack_id] = "custom"
        calls.append({"kind": "create", "pack_id": pack_id, "payload": payload})
        lifecycle_events.insert(
            0,
            {
                "id": 1,
                "industry_pack_id": pack_id,
                "pack_name": manifest["name"],
                "event_type": "created",
                "actor": "browser-admin",
                "details": {},
                "created_at": "2026-08-06T01:00:00Z",
            },
        )
        return (
            jsonify(
                {
                    "success": True,
                    "draft": {"revision": 1, "manifest": manifest},
                    "industry_pack": {
                        "id": pack_id,
                        "name": manifest["name"],
                        "origin": "custom",
                        "draft_only": True,
                    },
                }
            ),
            201,
        )

    @app.route("/api/intel/industry-packs/<pack_id>/draft", methods=["GET", "PUT"])
    def draft(pack_id):
        if request.method == "PUT":
            payload = request.get_json(silent=True) or {}
            calls.append({"kind": "save", "pack_id": pack_id, "payload": payload})
            manifests[pack_id] = copy.deepcopy(payload["manifest"])
            revisions[pack_id] += 1
        return jsonify(
            {
                "success": True,
                "draft": {
                    "revision": revisions[pack_id],
                    "manifest": manifests[pack_id],
                },
                "diff": {"sources": {"added": [], "removed": [], "modified": []}},
            }
        )

    @app.post("/api/intel/industry-packs/<pack_id>/draft/validate")
    def validate(pack_id):
        payload = request.get_json(silent=True) or {}
        calls.append({"kind": "validate", "pack_id": pack_id, "payload": payload})
        return jsonify(
            {
                "success": True,
                "valid": True,
                "manifest": payload.get("manifest"),
                "diff": {"sources": {"added": [], "removed": [], "modified": []}},
            }
        )

    @app.post("/api/intel/industry-packs/<pack_id>/draft/publish")
    def publish(pack_id):
        payload = request.get_json(silent=True) or {}
        calls.append({"kind": "publish", "pack_id": pack_id, "payload": payload})
        published_ids.add(pack_id)
        return (
            jsonify(
                {
                    "success": True,
                    "published": {
                        "id": 22,
                        "version_number": 2,
                        "pack_version": manifests[pack_id]["pack_version"],
                    },
                }
            ),
            201,
        )

    @app.post("/api/schedule-management/smart-url-suggestions")
    def smart_url_suggestions():
        payload = request.get_json(silent=True) or {}
        calls.append({"kind": "smart-url", "payload": payload})
        current_url = str(payload.get("url") or "")
        replacement_url = "https://education.example.test/policy-news"
        return jsonify(
            {
                "success": True,
                "status": "suggestions_found",
                "homepage": "https://education.example.test",
                "current_url": current_url,
                "current_match_count": 0,
                "best_match_count": 3,
                "suggestions": [
                    {
                        "url": replacement_url,
                        "title": "教育政策资讯",
                        "link_text": "教育政策",
                        "source": "section_sample",
                        "score": 9,
                        "match_count": 3,
                        "sample_total": 4,
                        "matched_keywords": ["教育", "政策"],
                        "is_current": False,
                    }
                ],
                "exploration_urls": [replacement_url],
                "all_top5": [replacement_url],
                "verified_articles": [],
            }
        )

    @app.get("/api/intel/industry-packs/<pack_id>/versions")
    def versions(pack_id):
        return jsonify(
            {
                "success": True,
                "versions": ([
                    {
                        "id": 22,
                        "version_number": 2,
                        "pack_version": manifests[pack_id]["pack_version"],
                        "schema_version": 3,
                        "published_at": "2026-08-06T00:00:00Z",
                        "content_sha256": "a" * 64,
                    }
                ] if pack_id in published_ids else []),
            }
        )

    @app.get("/api/intel/industry-packs/activations")
    def activations():
        return jsonify(
            {
                "success": True,
                "activations": [],
                "lifecycle_events": lifecycle_events,
            }
        )

    @app.delete("/api/intel/industry-packs/<pack_id>")
    def delete(pack_id):
        payload = request.get_json(silent=True) or {}
        calls.append({"kind": "delete", "pack_id": pack_id, "payload": payload})
        preview = {
            "industry_pack_id": pack_id,
            "name": manifests[pack_id]["name"],
            "deletable": origins.get(pack_id) == "custom",
            "blockers": [],
            "counts": {
                "published_versions": 1,
                "classifications": 3,
                "reports": 0,
                "source_associations": 2,
                "pending_jobs": 1,
            },
            "confirmation_text": pack_id,
            "plan_sha256": "delete-plan-e2e",
        }
        if not payload.get("confirm"):
            return jsonify({"success": True, "dry_run": True, "preview": preview})
        lifecycle_events.insert(
            0,
            {
                "id": 2,
                "industry_pack_id": pack_id,
                "pack_name": manifests[pack_id]["name"],
                "event_type": "deleted",
                "actor": "browser-admin",
                "details": {
                    "impact": preview["counts"],
                    "retention_policy": {"versions_retained": True},
                },
                "created_at": "2026-08-06T02:00:00Z",
            },
        )
        manifests.pop(pack_id)
        revisions.pop(pack_id)
        origins.pop(pack_id)
        published_ids.discard(pack_id)
        return jsonify(
            {
                "success": True,
                "result": {
                    "industry_pack_id": pack_id,
                    "deleted": True,
                    "logical_delete": True,
                },
            }
        )

    @app.post("/api/intel/industry-packs/switch")
    def switch():
        payload = request.get_json(silent=True) or {}
        calls.append({"kind": "switch", "payload": payload})
        if not payload.get("confirm"):
            return jsonify(
                {
                    "success": True,
                    "dry_run": True,
                    "preview": {
                        "target_pack_id": payload.get("industry_pack_id"),
                        "target_version_id": 22,
                        "plan_sha256": "plan-e2e",
                        "source_counts": {
                            "add_sources": 2,
                            "update_sources": 0,
                            "upsert_associations": 2,
                            "deactivate_associations": 3,
                        },
                    },
                }
            )
        return jsonify(
            {
                "success": True,
                "result": {
                    "activation_id": "activation-browser-e2e",
                    "active_version_id": 22,
                    "initial_scan_job_id": 91,
                    "backup": {"integrity": "ok"},
                },
            }
        )

    return app


def run(chromium_path: str = "", screenshot_path: str = "") -> dict:
    executable = _chromium(chromium_path)
    calls: list[dict] = []
    app = _fixture_app(calls)
    server = make_server(
        "127.0.0.1", 0, app, threaded=True, request_handler=_Quiet
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    page_errors: list[str] = []
    dialogs: list[str] = []
    measurements = {}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(executable), headless=True
            )
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: page_errors.append(str(error)))

            def accept_dialog(dialog):
                dialogs.append(dialog.type)
                dialog.accept()

            page.on("dialog", accept_dialog)
            page.goto(
                f"http://127.0.0.1:{server.server_port}/",
                wait_until="networkidle",
            )
            page.locator("button", has_text="新增行业包").click()
            page.locator("#newPackId").fill("robotics_news")
            page.locator("#newPackName").fill("机器人行业")
            page.locator("#createSubmit").click()
            page.wait_for_function(
                "() => document.querySelectorAll('.pack-card').length === 3"
            )
            page.locator("#packName").fill("机器人与自动化")
            page.locator('[data-tab="keywords"]').click()
            page.locator("#coreKeywords").fill("机器人\n自动化")
            page.locator('[data-tab="topics"]').click()
            page.locator("button", has_text="添加主题").click()
            topic = page.locator("#topics .topic-row").last
            topic.locator('[data-key="key"]').fill("robot_control")
            topic.locator('[data-key="name"]').fill("机器人控制")
            topic.locator('[data-key="keywords"]').fill("运动控制\n机器人控制器")
            page.locator("button", has_text="保存草稿").click()
            page.wait_for_function("() => document.querySelector('#status').textContent.includes('草稿已保存')")
            page.locator("button", has_text="发布新版本").click()
            page.wait_for_function("() => document.querySelector('#status').textContent.includes('已发布')")
            page.locator("#deleteButton").click()
            page.wait_for_function("() => document.querySelector('#deleteImpact').textContent.includes('历史分类')")
            page.locator("#deleteConfirmation").fill("robotics_news")
            page.locator("#deleteConfirmButton").click()
            page.wait_for_function(
                "() => document.querySelectorAll('.pack-card').length === 2 && document.querySelector('#lifecycle').textContent.includes('已删除')"
            )
            page.locator('.pack-card[data-id="education_news"]').click()
            page.wait_for_function("() => document.querySelector('#title').textContent.includes('教育')")
            page.locator('[data-tab="sources"]').click()
            smart_button = page.locator("#sources .source-smart-button").first
            smart_button.hover()
            page.wait_for_function(
                "() => !document.querySelector('#sourceSmartTooltip').hidden"
            )
            measurements["smart_probe"] = page.evaluate(
                """() => {
                    const button = document.querySelector('#sources .source-smart-button');
                    const tip = document.querySelector('#sourceSmartTooltip');
                    return {
                        icon_visible: Boolean(button?.querySelector('svg.source-smart-icon')),
                        width: button?.getBoundingClientRect().width || 0,
                        tooltip_visible: Boolean(tip && !tip.hidden),
                        tooltip_text: tip?.textContent || '',
                        described_by: button?.getAttribute('aria-describedby') || '',
                    };
                }"""
            )
            smart_button.click()
            page.wait_for_function(
                "() => document.querySelector('#sourceSmartDialog').open && document.querySelectorAll('#sourceSmartResults .smart-source-option').length === 1"
            )
            page.locator("#sourceSmartApply").click()
            page.wait_for_function(
                "() => !document.querySelector('#sourceSmartDialog').open && document.querySelector('#sources [data-key=\"url\"]').value.includes('education.example.test/policy-news')"
            )
            page.locator("button", has_text="验证全部配置").click()
            page.wait_for_function("() => document.querySelector('#status').textContent.includes('验证通过')")
            page.locator("button", has_text="保存草稿").click()
            page.wait_for_function("() => document.querySelector('#status').textContent.includes('草稿已保存')")
            page.locator("button", has_text="发布新版本").click()
            page.wait_for_function("() => document.querySelector('#status').textContent.includes('已发布')")
            page.locator("#switchButton").click()
            page.wait_for_function(
                "() => document.querySelectorAll('.pack-card').length === 2"
            )
            page.wait_for_timeout(100)
            page.locator('[data-tab="sources"]').click()
            measurements["desktop"] = page.evaluate(
                """() => ({
                    overflow: document.documentElement.scrollWidth > innerWidth,
                    cards: document.querySelectorAll('.pack-card').length,
                    editor_visible: !document.querySelector('#editor').classList.contains('hidden'),
                    switch_visible: !document.querySelector('#switchButton').classList.contains('hidden'),
                    sources: document.querySelectorAll('#sources .source-row').length,
                    source_roles: [...document.querySelectorAll('#sources [data-key="source_role"]')].map(node => node.value),
                    source_enabled: [...document.querySelectorAll('#sources [data-key="is_enabled"]')].map(node => node.checked),
                    source_urls: [...document.querySelectorAll('#sources [data-key="url"]')].map(node => node.value),
                    source_validation_statuses: [...document.querySelectorAll('#sources .source-validation')].map(node => node.textContent),
                    source_smart_buttons: document.querySelectorAll('#sources .source-smart-button:not([hidden])').length,
                    authority_role_options: document.querySelector('#sources [data-key="source_role"]')?.options.length || 0,
                    source_gate_note: [...document.querySelectorAll('.muted')].some(node => node.textContent.includes('不能绕过行业关键词门控')),
                    ragflow_upload_enabled: document.querySelector('#ragflowUploadEnabled')?.checked,
                    ragflow_knowledge_base: document.querySelector('#ragflowKnowledgeBase')?.value || '',
                    ragflow_knowledge_base_disabled: document.querySelector('#ragflowKnowledgeBase')?.disabled,
                    tabs: document.querySelectorAll('.tab-button').length,
                    active_tab: document.querySelector('.tab-button.active')?.dataset.tab || '',
                    source_metrics: [...document.querySelectorAll('.source-metrics .metric strong')].map(node => Number(node.textContent)),
                    topics: document.querySelectorAll('#topics .topic-row').length,
                    keyword_guidance: [...document.querySelectorAll('[data-keyword-guidance]')].map(node => ({
                        key: node.dataset.keywordGuidance,
                        text: node.textContent.replace(/\s+/g, ' ').trim(),
                    })),
                    keyword_descriptions_linked: ['coreKeywords','expandedKeywords','trendKeywords','eventKeywords','serpapiQueries']
                        .every(id => document.querySelector(`#${id}`)?.getAttribute('aria-describedby') === `${id}Help`),
                    workspace_columns: getComputedStyle(document.querySelector('.workspace')).gridTemplateColumns,
                })"""
            )
            if screenshot_path:
                screenshot = Path(screenshot_path).expanduser().resolve()
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshot), full_page=True)
                measurements["desktop"]["screenshot"] = str(screenshot)
            page.set_viewport_size({"width": 390, "height": 844})
            page.reload(wait_until="networkidle")
            page.locator('.pack-card[data-id="education_news"]').click()
            page.wait_for_function("() => !document.querySelector('#editor').classList.contains('hidden')")
            measurements["mobile"] = page.evaluate(
                """() => ({
                    overflow: document.documentElement.scrollWidth > innerWidth,
                    cards: document.querySelectorAll('.pack-card').length,
                    editor_visible: !document.querySelector('#editor').classList.contains('hidden'),
                    source_columns: getComputedStyle(document.querySelector('#sources .source-row')).gridTemplateColumns,
                    topic_columns: getComputedStyle(document.querySelector('#topics .topic-row')).gridTemplateColumns,
                    workspace_columns: getComputedStyle(document.querySelector('.workspace')).gridTemplateColumns,
                })"""
            )
            if screenshot_path:
                mobile = screenshot.with_name(
                    f"{screenshot.stem}-mobile{screenshot.suffix}"
                )
                page.screenshot(path=str(mobile), full_page=True)
                measurements["mobile"]["screenshot"] = str(mobile)
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)

    kinds = [item["kind"] for item in calls]
    save_calls = [item["payload"] for item in calls if item["kind"] == "save"]
    switch_calls = [item["payload"] for item in calls if item["kind"] == "switch"]
    delete_calls = [item["payload"] for item in calls if item["kind"] == "delete"]
    passed = bool(
        not page_errors
        and kinds.count("create") == 1
        and kinds.count("validate") == 1
        and kinds.count("smart-url") == 1
        and kinds.count("save") == 2
        and kinds.count("publish") == 2
        and save_calls[0]["manifest"]["fixed_topics"] == [
            {
                "key": "robot_control",
                "name": "机器人控制",
                "keywords": ["运动控制", "机器人控制器"],
            }
        ]
        and save_calls[1]["manifest"]["default_sources"][0]["source_import_id"]
        == "browser_fixture_feed"
        and save_calls[1]["manifest"]["default_sources"][0]["validation_status"]
        == "manual_review_required"
        and save_calls[1]["manifest"]["default_sources"][0]["is_enabled"] is False
        and save_calls[1]["manifest"]["default_sources"][0]["url"]
        == "https://education.example.test/policy-news"
        and save_calls[1]["manifest"]["fixed_topics"][0]["preferred_source_ids"]
        == ["browser_fixture_feed"]
        and len(delete_calls) == 2
        and not delete_calls[0].get("confirm")
        and delete_calls[1].get("confirm") is True
        and delete_calls[1].get("plan_sha256") == "delete-plan-e2e"
        and delete_calls[1].get("confirmation_text") == "robotics_news"
        and len(switch_calls) == 2
        and not switch_calls[0].get("confirm")
        and switch_calls[1].get("confirm") is True
        and switch_calls[1].get("target_version_id") == 22
        and switch_calls[1].get("plan_sha256") == "plan-e2e"
        and measurements["smart_probe"]["icon_visible"]
        and measurements["smart_probe"]["width"] >= 34
        and measurements["smart_probe"]["tooltip_visible"]
        and "分析当前站点的栏目链接和文章样本" in measurements["smart_probe"]["tooltip_text"]
        and measurements["smart_probe"]["described_by"] == "sourceSmartTooltip"
        and measurements["desktop"]["cards"] == 2
        and measurements["desktop"]["editor_visible"]
        and measurements["desktop"]["keyword_descriptions_linked"]
        and measurements["desktop"]["topics"] == 4
        and measurements["desktop"]["tabs"] == 5
        and measurements["desktop"]["active_tab"] == "sources"
        and measurements["desktop"]["source_metrics"][0] == 3
        and measurements["desktop"]["authority_role_options"] >= 10
        and measurements["desktop"]["source_gate_note"]
        and measurements["desktop"]["ragflow_upload_enabled"] is False
        and measurements["desktop"]["ragflow_knowledge_base"] == "news"
        and measurements["desktop"]["ragflow_knowledge_base_disabled"] is True
        and all(measurements["desktop"]["source_roles"])
        and measurements["desktop"]["source_enabled"][0] is False
        and measurements["desktop"]["source_urls"][0]
        == "https://education.example.test/policy-news"
        and measurements["desktop"]["source_validation_statuses"][0]
        == "需人工处理"
        and measurements["desktop"]["source_smart_buttons"] >= 1
        and [item["key"] for item in measurements["desktop"]["keyword_guidance"]]
        == ["core", "expanded", "trend", "event", "search"]
        and "定义行业身份" in measurements["desktop"]["keyword_guidance"][0]["text"]
        and "准入权重较低" in measurements["desktop"]["keyword_guidance"][1]["text"]
        and "不参与最终分类" in measurements["desktop"]["keyword_guidance"][4]["text"]
        and not measurements["desktop"]["overflow"]
        and measurements["mobile"]["cards"] == 2
        and measurements["mobile"]["editor_visible"]
        and not measurements["mobile"]["overflow"]
        and "confirm" in dialogs
        and "alert" in dialogs
    )
    return {
        "check_version": "industry-pack-management-browser-v8",
        "passed": passed,
        "browser": str(executable),
        "call_kinds": kinds,
        "switch_calls": switch_calls,
        "delete_calls": delete_calls,
        "dialogs": dialogs,
        "measurements": measurements,
        "page_errors": page_errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chromium-path", default="")
    parser.add_argument("--screenshot-path", default="")
    args = parser.parse_args()
    result = run(args.chromium_path, args.screenshot_path)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
