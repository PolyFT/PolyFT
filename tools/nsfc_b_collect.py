#!/usr/bin/env python3
"""Collect one NSFC B-code public completed-project partition.

This reuses the audited E collector transport/schema implementation but keeps a
separate B-facing CLI and B-labelled outputs. The endpoint is public; no CAPTCHA
or authenticated funded-project search is bypassed. Results are an official
completed-project evidence layer, not the universe of all awarded B projects.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

import nsfc_e_collect as core


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", required=True, help="B prefix, e.g. B01 or B0501")
    parser.add_argument("--start-conclusion-year", type=int, required=True)
    parser.add_argument("--end-conclusion-year", type=int, required=True)
    parser.add_argument("--page-size", type=int, default=10)
    parser.add_argument("--delay", type=float, default=0.35)
    parser.add_argument("--retries", type=int, default=10)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    code = args.code.strip().upper()
    if not re.fullmatch(r"B\d{2,6}", code):
        raise SystemExit("--code must be a B application-code prefix such as B01 or B0501")
    if args.start_conclusion_year < 1986 or args.end_conclusion_year < args.start_conclusion_year:
        raise SystemExit("invalid conclusion-year range")
    if not 1 <= args.page_size <= 10:
        raise SystemExit("page-size must be between 1 and 10 for the public endpoint")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    range_label = f"{args.start_conclusion_year}-{args.end_conclusion_year}"
    stem = f"nsfc_{code.lower()}_completed_{range_label}"
    raw_path = output_dir / f"{stem}_raw.jsonl.gz"
    csv_path = output_dir / f"{stem}.csv.gz"
    sqlite_path = output_dir / f"{stem}.sqlite"
    coverage_path = output_dir / f"{stem}_coverage.json"
    counts_path = output_dir / f"{stem}_counts.csv"
    conflicts_path = output_dir / f"{stem}_duplicate_conflicts.jsonl"
    contamination_path = output_dir / f"{stem}_code_contamination.jsonl"

    session = core.requests.Session()
    unique: dict[str, dict[str, Any]] = {}
    raw_rows = duplicate_rows = conflict_rows = contamination_rows = 0
    annual: dict[str, dict[str, int]] = {}
    returned_code_prefixes: Counter[str] = Counter()

    with (
        gzip.open(raw_path, "wt", encoding="utf-8") as raw_handle,
        conflicts_path.open("w", encoding="utf-8") as conflict_handle,
        contamination_path.open("w", encoding="utf-8") as contamination_handle,
    ):
        for year in range(args.start_conclusion_year, args.end_conclusion_year + 1):
            page = rows_seen = valid_approval_numbers = reported_total = 0
            previous_page_signature: tuple[str, ...] | None = None
            while True:
                response = core.post_json(
                    session,
                    core.query_payload(code, year, page, args.page_size),
                    retries=args.retries,
                )
                reported_total, rows = core.parse_page(response)
                if reported_total > core.MAX_PUBLIC_PAGES * args.page_size:
                    raise RuntimeError(
                        f"partition exceeds public 100-page cap: code={code}, "
                        f"year={year}, total={reported_total}; use a finer code prefix"
                    )
                signature = tuple(str(row[0]) for row in rows)
                if rows and signature == previous_page_signature and rows_seen < reported_total:
                    raise RuntimeError(
                        f"pagination did not advance: code={code}, year={year}, page={page}"
                    )
                previous_page_signature = signature

                for row in rows:
                    raw_rows += 1
                    rows_seen += 1
                    raw_handle.write(
                        json.dumps(
                            {
                                "query_code": code,
                                "conclusion_year_query": year,
                                "page": page,
                                "row": row,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    record = core.normalize_row(row, code)
                    approval = record["approval_number"]
                    app_code = record["application_code_1"]
                    returned_code_prefixes[
                        app_code[: max(3, len(code))] if app_code else ""
                    ] += 1
                    if app_code and not app_code.startswith(code):
                        contamination_rows += 1
                        contamination_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    if not approval:
                        continue
                    valid_approval_numbers += 1
                    existing = unique.get(approval)
                    if existing is None:
                        unique[approval] = record
                    else:
                        duplicate_rows += 1
                        if existing != record:
                            conflict_rows += 1
                            conflict_handle.write(
                                json.dumps(
                                    {
                                        "approval_number": approval,
                                        "existing": existing,
                                        "new": record,
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n"
                            )
                if reported_total == 0 or not rows or rows_seen >= reported_total:
                    break
                page += 1
                time.sleep(args.delay)

            annual[str(year)] = {
                "reported_total": int(reported_total),
                "rows_seen": rows_seen,
                "valid_approval_numbers": valid_approval_numbers,
                "pages": page + 1,
            }
            if reported_total != rows_seen or reported_total != valid_approval_numbers:
                raise RuntimeError(
                    f"coverage mismatch {code}/{year}: reported={reported_total}, "
                    f"rows={rows_seen}, valid={valid_approval_numbers}"
                )
            print(
                json.dumps(
                    {"code": code, "conclusion_year": year, "total": reported_total, "pages": page + 1},
                    ensure_ascii=False,
                ),
                flush=True,
            )

    if contamination_rows:
        raise RuntimeError(
            f"official query returned {contamination_rows} rows outside {code}; "
            f"see {contamination_path}"
        )

    records = [unique[key] for key in sorted(unique)]
    with gzip.open(csv_path, "wt", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=core.FIELDS)
        writer.writeheader()
        writer.writerows(records)
    core.write_sqlite(sqlite_path, records)

    counts: Counter[tuple[str, str, str]] = Counter()
    for record in records:
        counts[(record["approval_year"], record["application_code_1"], record["project_type_raw"])] += 1
    with counts_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["approval_year", "application_code_1", "project_type_raw", "unique_projects"])
        for key, count in sorted(counts.items()):
            writer.writerow([*key, count])

    coverage = {
        "as_of": core.utc_now(),
        "query_code": code,
        "scope": "NSFC public completed-project endpoint only",
        "partition_strategy": "B01-B09 application-code prefixes, subdivide only if a year exceeds the public 100-page cap",
        "start_conclusion_year": args.start_conclusion_year,
        "end_conclusion_year": args.end_conclusion_year,
        "raw_rows": raw_rows,
        "unique_approval_numbers": len(records),
        "duplicate_rows": duplicate_rows,
        "duplicate_conflicts": conflict_rows,
        "code_contamination_rows": contamination_rows,
        "returned_application_code_prefixes": dict(returned_code_prefixes),
        "annual": annual,
        "complete": True,
        "completeness_status": "official_completed_subset_only",
    }
    coverage_path.write_text(json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8")

    files = [raw_path, csv_path, sqlite_path, coverage_path, counts_path, conflicts_path, contamination_path]
    manifest = {
        "dataset": "nsfc_b_public_completed_projects_partition",
        "generated_at": core.utc_now(),
        "query_code": code,
        "scope": coverage["scope"],
        "range": range_label,
        "files": [
            {"name": path.name, "size_bytes": path.stat().st_size, "sha256": core.sha256(path)}
            for path in files
        ],
    }
    (output_dir / f"manifest_{code.lower()}_{range_label}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "success",
                "query_code": code,
                "range": range_label,
                "unique_approval_numbers": len(records),
                "raw_rows": raw_rows,
                "output_dir": str(output_dir),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
