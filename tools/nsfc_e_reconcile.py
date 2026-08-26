#!/usr/bin/env python3
"""Reconcile E01-E13 partition totals against the E root reference totals."""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", required=True)
    parser.add_argument("--reference-json", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    root = Path(args.input_root)
    reference = json.loads(Path(args.reference_json).read_text(encoding="utf-8"))
    expected = {str(k): int(v) for k, v in reference.get("annual_reported_total", {}).items()}

    partition_totals: dict[str, int] = defaultdict(int)
    code_year: dict[tuple[str, str], int] = {}
    coverage_files = sorted(root.rglob("nsfc_e_completed_coverage_*.json"))
    for path in coverage_files:
        value = json.loads(path.read_text(encoding="utf-8"))
        code = str(value.get("query_code") or value.get("code") or "").upper()
        if code == "E":
            continue
        for year, stats in value.get("annual", {}).items():
            total = int(stats.get("reported_total") or 0)
            partition_totals[str(year)] += total
            code_year[(code, str(year))] = total

    years = sorted(set(expected) | set(partition_totals), key=int)
    rows = []
    mismatch_years = []
    for year in years:
        root_total = int(expected.get(year, 0))
        partition_total = int(partition_totals.get(year, 0))
        difference = partition_total - root_total
        status = "matched" if difference == 0 else "mismatch"
        if status != "matched":
            mismatch_years.append(year)
        rows.append(
            {
                "conclusion_year": year,
                "root_reported_total": root_total,
                "partition_reported_total": partition_total,
                "difference": difference,
                "status": status,
            }
        )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "nsfc_e_partition_reconciliation.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else [])
        writer.writeheader()
        writer.writerows(rows)

    code_path = output_dir / "nsfc_e_partition_counts_by_code_year.csv"
    with code_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["query_code", "conclusion_year", "reported_total"])
        for (code, year), count in sorted(code_year.items(), key=lambda item: (item[0][0], int(item[0][1]))):
            writer.writerow([code, year, count])

    result = {
        "generated_at": utc_now(),
        "reference_grand_total": sum(expected.values()),
        "partition_grand_total": sum(partition_totals.values()),
        "difference": sum(partition_totals.values()) - sum(expected.values()),
        "year_count": len(years),
        "matched_years": len(years) - len(mismatch_years),
        "mismatch_years": mismatch_years,
        "reconciliation_status": "matched" if not mismatch_years else "mismatch_requires_investigation",
        "coverage_files": [path.name for path in coverage_files],
    }
    (output_dir / "nsfc_e_partition_reconciliation.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
