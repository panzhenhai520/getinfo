# -*- coding: utf-8 -*-
"""Run trend aggregation for the active pack and report the result."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, str(ROOT))

from trend_aggregate_service import TrendAggregateService


def main():
    service = TrendAggregateService()
    snap = service.composition.snapshot()
    pack_id = str(snap.get("active_industry_pack_id") or "")
    activation_id = str(snap.get("active_industry_activation_id") or "")
    print("active pack:", pack_id, "| activation:", activation_id)

    result = service.run(pack_id=pack_id, dimension="trend_keyword")
    print("RESULT:", {k: v for k, v in result.items() if k != "states"})

    states = result.get("states") or {}
    print("states summary (top 30):")
    for idx, (kw, st) in enumerate(sorted(states.items(), key=lambda x: -len(x[0]))[:30], 1):
        print(f"  {idx:2d}. {kw}: {st}")

    # Show DB summary for the pack
    db = service.repository.db
    db._ensure_connection()
    with db.lock:
        rows = db.connection.execute(
            "SELECT keyword, state, sum(article_count) AS total, max(is_burst) AS burst "
            "FROM intel_topic_trends WHERE industry_pack_id=? AND dimension='trend_keyword' "
            "GROUP BY keyword, state ORDER BY total DESC LIMIT 40",
            (pack_id,),
        ).fetchall()
    print("\nintel_topic_trends rows in DB (top 40 by article total):")
    for r in rows:
        print(f"  {r[0]:<24} state={r[1]:<12} articles={r[2]:<4} burst={r[3]}")


if __name__ == "__main__":
    main()
