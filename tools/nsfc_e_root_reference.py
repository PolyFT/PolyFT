#!/usr/bin/env python3
"""Collect one authoritative root-level total per conclusion year for E.

The E root query can report more than 1,000 rows even though deep pagination is
capped. We therefore use it only as a reference-count probe and retrieve rows
through E01-E13 partitions.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from nsfc_e_collect import api_payload, parse_payload, post_json


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-conclusion-year", type=int, default=1986)
    parser.add_argument("--end-conclusion-year", type=int, default=2026)
    parser.add_argument("--retries", type=int, default=8)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    annual: dict[str, int] = {}
    for year in range(args.start_conclusion_year, args.end_conclusion_year + 1):
        response = post_json(api_payload("E", year, 0, 10), retries=args.retries)
        total, _ = parse_payload(response)
        annual[str(year)] = total
        print(json.dumps({"code": "E", "conclusion_year": year, "reported_total": total}), flush=True)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "generated_at": utc_now(),
                "query_code": "E",
                "scope": "root-level reference totals only; no deep-page completeness claim",
                "annual_reported_total": annual,
                "grand_total_reported": sum(annual.values()),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
