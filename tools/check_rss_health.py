#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全行业包 RSS 订阅体检：逐个 feed 真订阅一次，用生产同一套判据验收。

为什么需要这个工具（2026-10-08 实测）：
  · RSS 是**最稳、日期最全**的一类来源（实测成功的 feed 全部"条数 = 带日期条数"），
    但全项目只有 2 个行业包配了 RSS，其余 6 个包一个都没有；
  · 两台生产机的行为不一致：RSSHub 内网源（http://10.88.0.1:1200/...）在 A 机正常、
    在 B 机被出站策略判为"受限网络地址"，同一份配置一边能跑一边白跑；
  · 库里还登记了若干失效源（源本身空、403、返回 HTML 不是 feed），一直占着轮询。
所以把体检固化下来：**判断"能不能订阅"用生产入库的同一套校验器**
（rss_feed_contract.validate_rss_feed_response），避免"看着像能跑"。

用法：
    python tools/check_rss_health.py                 # 全部行业包 + 生产库登记
    python tools/check_rss_health.py --only-pack financial_markets
    python tools/check_rss_health.py --json > rss.json
    python tools/check_rss_health.py --timeout 15
退出码：0 = 全部通过；1 = 有失败项（可直接用于定时巡检 / CI）。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intel_http import SafeHTTPClient  # noqa: E402
from rss_feed_contract import validate_rss_feed_response  # noqa: E402

_APP_DIR = "/app"
_DEFAULT_PACK_DIR = (
    os.path.join(_APP_DIR, "config", "industry_packs")
    if os.path.isdir(os.path.join(_APP_DIR, "config", "industry_packs"))
    else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "config", "industry_packs")
)

HEADERS = {
    "User-Agent": "MarketIntelRadar/1.0 (+rss-health-check)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
}


def collect_from_seeds(pack_dir: str, only_pack: str = "") -> Dict[str, Dict[str, Any]]:
    """从行业包种子里收集 RSS 源（source_type=rss，或任意源上挂了 metadata.rss_url）。"""
    found: Dict[str, Dict[str, Any]] = {}
    for path in sorted(glob.glob(os.path.join(pack_dir, "*.json"))):
        name = os.path.basename(path)
        if name == "schema.json":
            continue
        pack_id = name[:-5]
        if only_pack and pack_id != only_pack:
            continue
        try:
            pack = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        for src in pack.get("default_sources") or []:
            meta = src.get("metadata") or {}
            url = str(src.get("url") or src.get("source_url") or "").strip()
            feed = str(meta.get("rss_url") or "").strip()
            is_rss = str(src.get("source_type") or "") == "rss" or bool(feed)
            target = feed or url
            if not is_rss or not target.startswith("http"):
                continue
            item = found.setdefault(target, {"url": target, "name": str(src.get("name") or ""),
                                             "packs": [], "origin": "seed"})
            item["packs"].append(pack_id)
    return found


def collect_from_db(only_pack: str = "") -> Dict[str, Dict[str, Any]]:
    """生产库里实际登记为 rss 的信源（可能比种子多，也可能多出失效源）。"""
    found: Dict[str, Dict[str, Any]] = {}
    try:
        from intel_sources import intel_source_registry

        result = intel_source_registry.list_sources(source_type="rss", per_page=500)
        items = result[0] if isinstance(result, tuple) else (
            result.get("items") if isinstance(result, dict) else result)
        for row in items or []:
            pack_id = str(row.get("industry_pack_id") or "")
            if only_pack and pack_id and pack_id != only_pack:
                continue
            url = str(row.get("source_url") or "").strip()
            if not url.startswith("http"):
                continue
            item = found.setdefault(url, {"url": url, "name": str(row.get("name") or ""),
                                          "packs": [], "origin": "db",
                                          "enabled": bool(row.get("is_enabled"))})
            if pack_id and pack_id not in item["packs"]:
                item["packs"].append(pack_id)
    except Exception as exc:
        print(f"（生产库 rss 信源读取跳过：{type(exc).__name__}: {str(exc)[:80]}）", file=sys.stderr)
    return found


def check_feed(url: str, timeout: int = 25) -> Dict[str, Any]:
    """真订阅一次并做生产同款校验。任何异常都收敛成 ok=False 的结果。"""
    try:
        response = SafeHTTPClient().get(url, headers=HEADERS, timeout=(10, timeout))
        info = validate_rss_feed_response(response, limit=200)
        return {
            "url": url,
            "ok": True,
            "entries": int(info.get("entry_count") or 0),
            "dated": int(info.get("dated_entry_count") or 0),
            "error": "",
        }
    except Exception as exc:
        return {"url": url, "ok": False, "entries": 0, "dated": 0,
                "error": f"{type(exc).__name__}: {str(exc)[:140]}"}


def _reason_hint(error: str) -> str:
    """把常见失败翻译成可执行的中文提示。

    注意顺序：按"具体报文"判断，不能拿异常类名里的 unsafe/restricted 去匹配，
    否则"域名解析失败"也会被误判成"内网地址被策略拦"。
    """
    text = str(error or "").casefold()
    if "域名解析失败" in error or "name or service not known" in text:
        return "域名解析不了——源可能已下线，应纳入自动废弃"
    if "受限网络地址" in error:
        return "出站策略拦了内网/受限地址——若该 feed 是自建 RSSHub，需在本机放行"
    if "403" in error:
        return "被反爬拦（403）——等反爬识别器按信源止损"
    if "content-type" in text:
        return "返回的不是 feed（多为网页），地址可能写错"
    if "未包含可用 item" in error:
        return "源本身空/已失效——应纳入自动废弃"
    if "timed out" in text or "timeout" in text:
        return "连接或读取超时——源太慢或网络不通"
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="全行业包 RSS 订阅体检")
    parser.add_argument("--pack-dir", default=_DEFAULT_PACK_DIR)
    parser.add_argument("--only-pack", default="", help="只查某个行业包")
    parser.add_argument("--timeout", type=int, default=25, help="单个 feed 读取超时（秒）")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--json", action="store_true", help="输出 JSON（便于巡检归档）")
    args = parser.parse_args()

    targets = collect_from_seeds(args.pack_dir, args.only_pack)
    db_targets = collect_from_db(args.only_pack)
    for url, meta in db_targets.items():
        if url in targets:
            for pack_id in meta.get("packs") or []:
                if pack_id and pack_id not in targets[url]["packs"]:
                    targets[url]["packs"].append(pack_id)
        else:
            targets[url] = meta

    urls = sorted(targets)
    if not urls:
        print("没有找到任何 RSS 源", file=sys.stderr)
        return 1

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = list(pool.map(lambda u: check_feed(u, args.timeout), urls))

    for row in results:
        meta = targets.get(row["url"]) or {}
        row["name"] = meta.get("name") or ""
        row["packs"] = sorted({p for p in (meta.get("packs") or []) if p})
        row["origin"] = meta.get("origin") or ""
        row["hint"] = "" if row["ok"] else _reason_hint(row["error"])

    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    by_pack: Dict[str, Dict[str, int]] = {}
    for row in results:
        for pack_id in row["packs"] or ["(未标包)"]:
            bucket = by_pack.setdefault(pack_id, {"ok": 0, "bad": 0})
            bucket["ok" if row["ok"] else "bad"] += 1

    summary = {
        "total": len(results),
        "ok": len(ok),
        "bad": len(bad),
        "packs": by_pack,
        "results": results,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 1 if bad else 0

    print("=" * 96)
    print(f"全行业包 RSS 订阅体检：共 {len(results)} 个 feed，"
          f"成功 {len(ok)}，失败 {len(bad)}")
    print("=" * 96)

    print("\n按行业包汇总：")
    for pack_id, bucket in sorted(by_pack.items()):
        mark = "✅" if not bucket["bad"] else "⚠️"
        print(f"  {mark} {pack_id:<28} 成功 {bucket['ok']:>3} / 失败 {bucket['bad']:>3}")

    print("\n成功的 feed：")
    for row in sorted(ok, key=lambda r: r["url"]):
        packs = ",".join(row["packs"])[:30]
        print(f"  ✅ {packs:<30} {row['entries']:>4} 条 / {row['dated']:>4} 条带日期  {row['url'][:56]}")

    if bad:
        print("\n失败的 feed：")
        for row in sorted(bad, key=lambda r: r["url"]):
            packs = ",".join(row["packs"])[:30]
            print(f"  ❌ {packs:<30} {row['name'][:16]:<16} {row['url'][:56]}")
            print(f"      原因: {row['error']}")
            if row["hint"]:
                print(f"      建议: {row['hint']}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
