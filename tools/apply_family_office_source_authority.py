#!/usr/bin/env python3
"""Assign explicit global authority roles to the family-office seed sources."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from source_authority import authority_registry, resolve_source_authority


PACK_PATH = ROOT / "config" / "industry_packs" / "family_office.json"

GOVERNMENT_HOSTS = {
    "www.familyofficehk.gov.hk",
    "familyofficehk.gov.hk",
    "www.hkma.gov.hk",
    "hkma.gov.hk",
    "www.investhk.gov.hk",
    "investhk.gov.hk",
    "www.mas.gov.sg",
    "mas.gov.sg",
    "www.nfra.gov.cn",
    "nfra.gov.cn",
}
ACADEMIC_MARKERS = (
    "大学",
    "学院",
    "hkust",
    "tsinghua",
    "知网",
    "cnki",
)
ASSOCIATION_MARKERS = (
    "协会",
    "学会",
    "association",
    "actec foundation",
    "step",
)
RESEARCH_MARKERS = (
    "pitchbook",
    "preqin",
    "mckinsey",
    "campden wealth",
    "family wealth report",
    "研究",
    "智库",
)
ADVISOR_MARKERS = (
    "律师",
    " law",
    "kpmg",
    "deloitte",
    "pwc",
    "ey",
    "dentons",
    "maples",
    "appleby",
    "carey olsen",
    "conyers",
    "harneys",
    "ogier",
    "walkers",
    "acclime",
    "iqeq",
    "vistra",
    "trident trust",
    "jtc group",
    "apex group",
    "kaizen",
)
MEDIA_MARKERS = (
    "36氪",
    "bloomberg",
    "财新",
    "信报",
    "新闻",
    "时报",
    "参考报",
    "界面",
    "联合早报",
    "magazine",
    "insider",
    "foinsight",
    "wealthbriefing",
)


def role_for_source(source: dict) -> str:
    name = str(source.get("name") or "").casefold()
    host = (urlsplit(str(source.get("url") or "")).hostname or "").casefold()
    if host in GOVERNMENT_HOSTS:
        return "government_regulator"
    if any(marker in name or marker in host for marker in ACADEMIC_MARKERS):
        return "academic_research"
    if any(marker in name or marker in host for marker in ASSOCIATION_MARKERS):
        return "industry_association"
    if any(marker in name or marker in host for marker in ADVISOR_MARKERS):
        return "professional_advisor"
    if any(marker in name or marker in host for marker in MEDIA_MARKERS):
        return "professional_trade_media"
    if any(marker in name or marker in host for marker in RESEARCH_MARKERS):
        return "independent_research"
    content_type = str(source.get("content_type") or "")
    if content_type == "media":
        return "professional_trade_media"
    if content_type == "report":
        return "independent_research"
    if int(source.get("authority_level") or 0) >= 5:
        return "government_regulator"
    if int(source.get("authority_level") or 0) >= 4:
        return "independent_research"
    if int(source.get("authority_level") or 0) >= 3:
        return "issuer_official"
    return "professional_advisor"


def build_manifest() -> tuple[dict, Counter]:
    manifest = json.loads(PACK_PATH.read_text(encoding="utf-8"))
    counts = Counter()
    for source in manifest.get("default_sources") or []:
        role = role_for_source(source)
        default_level = int(authority_registry()["roles"][role]["weight"])
        profile = resolve_source_authority(
            {
                "url": source.get("url"),
                "source_role": role,
                "authority_level": default_level,
            },
            strict=True,
        )
        source["source_role"] = role
        source["authority_level"] = default_level
        source["authority_scope"] = profile["authority_scope"]
        source["publisher_key"] = profile["publisher_key"]
        counts[role] += 1
    return manifest, counts


def atomic_write(payload: dict) -> None:
    fd, temporary = tempfile.mkstemp(
        prefix="family-office-authority-", suffix=".json", dir=str(PACK_PATH.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, PACK_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    manifest, counts = build_manifest()
    result = {
        "source_count": len(manifest.get("default_sources") or []),
        "role_counts": dict(sorted(counts.items())),
        "all_sources_classified": sum(counts.values())
        == len(manifest.get("default_sources") or []),
        "applied": bool(args.apply),
    }
    if args.apply:
        atomic_write(manifest)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["all_sources_classified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
