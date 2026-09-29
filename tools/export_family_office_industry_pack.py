#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Export live family-office configuration read-only and replay it in isolation."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from industry_pack_migration import (
    FamilyOfficePackExporter,
    replay_export,
    write_family_office_seed,
)
from industry_packs import IndustryPackLoader


def run(
    database_path: str,
    output_path: str = "",
    *,
    target_pack_version: str = "",
    apply_family_seed: bool = False,
) -> dict:
    database = Path(database_path).expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError(f"数据库不存在：{database}")
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        exporter = FamilyOfficePackExporter(
            connection,
            pack_loader=IndustryPackLoader(
                str(ROOT / "config" / "industry_packs"), use_published_store=False
            ),
            target_pack_version=target_pack_version,
        )
        exported = exporter.export()
    finally:
        connection.close()
    replay = replay_export(
        exported, config_dir=str(ROOT / "config" / "industry_packs")
    )
    result = {**exported, "replay": replay, "passed": bool(replay["passed"])}
    if apply_family_seed:
        result["installed_family_seed"] = write_family_office_seed(
            result,
            destination=str(
                ROOT / "config" / "industry_packs" / "family_office.json"
            ),
        )
    if output_path:
        target = Path(output_path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result["output_path"] = str(target)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=str(config.DATABASE_PATH))
    parser.add_argument("--output", default="")
    parser.add_argument("--full", action="store_true")
    parser.add_argument(
        "--target-pack-version",
        default="",
        help="为迁移后的家族办公室包设置显式语义版本",
    )
    parser.add_argument(
        "--apply-family-seed",
        action="store_true",
        help="仅在全部迁移门禁通过时原子更新家族办公室种子包",
    )
    args = parser.parse_args()
    result = run(
        args.database,
        args.output,
        target_pack_version=args.target_pack_version,
        apply_family_seed=args.apply_family_seed,
    )
    displayed = result if args.full else {
        "export_version": result["export_version"],
        "read_only": result["read_only"],
        "database_writes": result["database_writes"],
        "counts": result["counts"],
        "configuration_sha256": result["configuration_sha256"],
        "replay": result["replay"],
        "output_path": result.get("output_path", ""),
        "installed_family_seed": result.get("installed_family_seed"),
        "passed": result["passed"],
    }
    print(json.dumps(displayed, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
