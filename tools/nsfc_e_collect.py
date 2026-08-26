#!/usr/bin/env python3
"""Collect one NSFC E-code public completed-project partition.

The endpoint is public and does not require bypassing CAPTCHA or authentication.
The output is an official completed-project evidence subset, not all awarded E
projects.  E01--E13 partitioning is required because the public endpoint exposes
at most 100 pages for one query.
"""
from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import json
import random
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from Crypto.Cipher import DES
from urllib3.exceptions import InsecureRequestWarning

BASE_URL = "https://kd.nsfc.cn"
ENDPOINT = BASE_URL + "/api/baseQuery/completionQueryResultsData"
DES_KEY = b"IFROMC86"
RETRYABLE = {408, 425, 429, 500, 502, 503, 504}
MAX_PUBLIC_PAGES = 100
FIELDS = [
    "approval_number",
    "title",
    "project_type_raw",
    "institution",
    "person_in_charge",
    "amount_wan",
    "approval_year",
    "keywords",
    "application_code_1",
    "conclusion_year",
    "source_record_id",
    "query_code",
    "source",
]

requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pkcs7_unpad(data: bytes, block_size: int = 8) -> bytes:
    if not data or len(data) % block_size:
        raise ValueError("invalid DES payload length")
    padding = data[-1]
    if padding < 1 or padding > block_size:
        raise ValueError("invalid DES padding")
    if data[-padding:] != bytes([padding]) * padding:
        raise ValueError("invalid DES padding bytes")
    return data[:-padding]


def decode_response(text: str) -> dict[str, Any]:
    payload = text.strip()
    if not payload:
        raise ValueError("empty response")
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        encrypted = base64.b64decode(payload, validate=True)
        decrypted = DES.new(DES_KEY, DES.MODE_ECB).decrypt(encrypted)
        value = json.loads(pkcs7_unpad(decrypted).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("response is not a JSON object")
    return value


def query_payload(code: str, year: int, page: int, page_size: int) -> dict[str, Any]:
    return {
        "code": code,
        "fuzzyKeyword": "",
        "complete": True,
        "isFuzzySearch": False,
        "conclusionYear": str(year),
        "dependUnit": "",
        "keywords": "",
        "pageNum": page,
        "pageSize": page_size,
        "personInCharge": "",
        "projectName": "",
        "projectType": "",
        "subPType": "",
        "psPType": "",
        "ratifyNo": "",
        "ratifyYear": "",
        "queryType": "input",
        "order": "enddate",
        "ordering": "desc",
        "codeScreening": "",
        "dependUnitScreening": "",
        "keywordsScreening": "",
        "projectTypeNameScreening": "",
    }


def post_json(session: requests.Session, payload: dict[str, Any], retries: int) -> dict[str, Any]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": BASE_URL + "/finalProjectInit",
        "Authorization": "Bearer false",
        "User-Agent": "PolyFT/nsfc-e-completed-base/1.1",
    }
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = session.post(
                ENDPOINT,
                json=payload,
                headers=headers,
                timeout=90,
                verify=False,
            )
            if response.status_code in RETRYABLE:
                raise requests.HTTPError(
                    f"retryable HTTP {response.status_code}", response=response
                )
            response.raise_for_status()
            return decode_response(response.text)
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and status not in RETRYABLE:
                raise
            if attempt + 1 >= retries:
                raise RuntimeError(
                    f"request failed after {retries} attempts: {exc}"
                ) from exc
            time.sleep(min(30.0, 1.5 * (2**attempt)) + random.uniform(0, 0.35))
    raise RuntimeError(f"request failed: {last_error}")


def parse_page(response: dict[str, Any]) -> tuple[int, list[list[Any]]]:
    code = response.get("code")
    if code not in (None, 200, "200"):
        raise RuntimeError(f"NSFC API error {code}: {response.get('message')}")
    section = response.get("data")
    if not isinstance(section, dict):
        raise RuntimeError("NSFC response has no data object")
    total = int(section.get("itotalRecords") or 0)
    rows = section.get("resultsData") or []
    if not isinstance(rows, list):
        raise RuntimeError("resultsData is not a list")
    valid: list[list[Any]] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        if len(row) < 16:
            raise RuntimeError(
                f"row schema changed: expected >=16 columns, got {len(row)}"
            )
        valid.append(row)
    return total, valid


def normalize_row(row: list[Any], query_code: str) -> dict[str, Any]:
    def cell(index: int) -> str:
        value = row[index] if index < len(row) else ""
        return "" if value is None else str(value).strip()

    return {
        "approval_number": cell(2),
        "title": cell(1),
        "project_type_raw": cell(3),
        "institution": cell(4),
        "person_in_charge": cell(5),
        "amount_wan": cell(6),
        "approval_year": cell(7),
        "keywords": cell(8),
        "application_code_1": cell(14).upper(),
        "conclusion_year": cell(15),
        "source_record_id": cell(0),
        "query_code": query_code,
        "source": "nsfc_public_completed_project_endpoint",
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_sqlite(path: Path, records: list[dict[str, Any]]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE IF EXISTS projects")
        connection.execute(
            """
            CREATE TABLE projects (
                approval_number TEXT PRIMARY KEY,
                title TEXT,
                project_type_raw TEXT,
                institution TEXT,
                person_in_charge TEXT,
                amount_wan TEXT,
                approval_year INTEGER,
                keywords TEXT,
                application_code_1 TEXT,
                conclusion_year INTEGER,
                source_record_id TEXT,
                query_code TEXT,
                source TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO projects VALUES (
                :approval_number, :title, :project_type_raw, :institution,
                :person_in_charge, :amount_wan, :approval_year, :keywords,
                :application_code_1, :conclusion_year, :source_record_id,
                :query_code, :source
            )
            """,
            records,
        )
        connection.execute("CREATE INDEX idx_projects_year ON projects(approval_year)")
        connection.execute("CREATE INDEX idx_projects_code ON projects(application_code_1)")
        connection.execute("CREATE INDEX idx_projects_type ON projects(project_type_raw)")
        connection.commit()
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", required=True, help="E01 through E13")
    parser.add_argument("--start-conclusion-year", type=int, required=True)
    parser.add_argument("--end-conclusion-year", type=int, required=True)
    parser.add_argument("--page-size", type=int, default=10)
    parser.add_argument("--delay", type=float, default=0.55)
    parser.add_argument("--retries", type=int, default=8)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    code = args.code.strip().upper()
    if not re.fullmatch(r"E(?:0[1-9]|1[0-3])", code):
        raise SystemExit("--code must be one of E01 through E13")
    if args.start_conclusion_year < 1986 or args.end_conclusion_year < args.start_conclusion_year:
        raise SystemExit("invalid conclusion-year range")
    if not 1 <= args.page_size <= 10:
        raise SystemExit("page-size must be between 1 and 10 for the public endpoint")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    code_label = code.lower()
    range_label = f"{args.start_conclusion_year}-{args.end_conclusion_year}"
    stem = f"nsfc_{code_label}_completed_{range_label}"
    raw_path = output_dir / f"{stem}_raw.jsonl.gz"
    csv_path = output_dir / f"{stem}.csv.gz"
    sqlite_path = output_dir / f"{stem}.sqlite"
    coverage_path = output_dir / f"{stem}_coverage.json"
    counts_path = output_dir / f"{stem}_counts.csv"
    conflicts_path = output_dir / f"{stem}_duplicate_conflicts.jsonl"
    contamination_path = output_dir / f"{stem}_code_contamination.jsonl"

    session = requests.Session()
    unique: dict[str, dict[str, Any]] = {}
    raw_rows = 0
    duplicate_rows = 0
    conflict_rows = 0
    contamination_rows = 0
    annual: dict[str, dict[str, int]] = {}
    returned_code_prefixes: Counter[str] = Counter()

    with (
        gzip.open(raw_path, "wt", encoding="utf-8") as raw_handle,
        conflicts_path.open("w", encoding="utf-8") as conflict_handle,
        contamination_path.open("w", encoding="utf-8") as contamination_handle,
    ):
        for year in range(args.start_conclusion_year, args.end_conclusion_year + 1):
            page = 0
            rows_seen = 0
            valid_approval_numbers = 0
            reported_total = 0
            previous_page_signature: tuple[str, ...] | None = None
            while True:
                response = post_json(
                    session,
                    query_payload(code, year, page, args.page_size),
                    retries=args.retries,
                )
                reported_total, rows = parse_page(response)
                if reported_total > MAX_PUBLIC_PAGES * args.page_size:
                    raise RuntimeError(
                        f"partition exceeds public 100-page cap: code={code}, "
                        f"year={year}, total={reported_total}; use a finer code partition"
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
                    envelope = {
                        "query_code": code,
                        "conclusion_year_query": year,
                        "page": page,
                        "row": row,
                    }
                    raw_handle.write(json.dumps(envelope, ensure_ascii=False) + "\n")
                    record = normalize_row(row, code)
                    approval = record["approval_number"]
                    app_code = record["application_code_1"]
                    returned_code_prefixes[app_code[:3] if app_code else ""] += 1
                    if app_code and not app_code.startswith(code):
                        contamination_rows += 1
                        contamination_handle.write(
                            json.dumps(record, ensure_ascii=False) + "\n"
                        )
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
            if rows_seen != reported_total:
                raise RuntimeError(
                    f"coverage mismatch for code={code}, conclusion year={year}: "
                    f"reported={reported_total}, collected={rows_seen}"
                )
            print(
                json.dumps(
                    {
                        "code": code,
                        "conclusion_year": year,
                        "total": reported_total,
                        "pages": page + 1,
                    },
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
    with gzip.open(csv_path, "wt", encoding="utf-8-sig", newline="") as csv_handle:
        writer = csv.DictWriter(csv_handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(records)

    write_sqlite(sqlite_path, records)

    counts: Counter[tuple[str, str, str]] = Counter()
    for record in records:
        counts[(record["approval_year"], record["application_code_1"], record["project_type_raw"])] += 1
    with counts_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["approval_year", "application_code_1", "project_type_raw", "unique_projects"]
        )
        for key, count in sorted(counts.items()):
            writer.writerow([*key, count])

    coverage = {
        "as_of": utc_now(),
        "query_code": code,
        "scope": "NSFC public completed-project endpoint only",
        "partition_strategy": "E01-E13 because the endpoint caps one query at 100 pages",
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
    coverage_path.write_text(
        json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    files = [
        raw_path,
        csv_path,
        sqlite_path,
        coverage_path,
        counts_path,
        conflicts_path,
        contamination_path,
    ]
    manifest = {
        "dataset": "nsfc_e_public_completed_projects_partition",
        "generated_at": utc_now(),
        "query_code": code,
        "scope": coverage["scope"],
        "range": range_label,
        "files": [
            {
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
            for path in files
        ],
    }
    manifest_path = output_dir / f"manifest_{code_label}_{range_label}.json"
    manifest_path.write_text(
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
