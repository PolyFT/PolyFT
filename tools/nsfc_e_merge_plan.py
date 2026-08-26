#!/usr/bin/env python3
"""Merge an explicit, audited NSFC E completed-project partition plan.

The plan may mix historical first-level partitions (for example E05 through
2014) and later second-level partitions (for example E0501--E0512 from 2015).
Every selected partition must be complete within the public completed-project
endpoint.  The union is reconciled year-by-year against an independent broad-E
total scan before a final dataset is emitted.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_FIELDS = [
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
OBSERVATION_FIELDS = [
    "approval_number",
    "approval_year",
    "project_type",
    "project_type_raw",
    "discipline_root",
    "discipline_scope",
    "application_code_1",
    "anchor",
    "source",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_type(value: str) -> str:
    text = clean(value).replace(" ", "")
    exact = {
        "面上项目": "general",
        "青年科学基金项目": "youth_c",
        "青年科学基金项目（C类）": "youth_c",
        "青年科学基金项目(C类)": "youth_c",
        "地区科学基金项目": "regional",
        "重点项目": "key",
        "重大项目": "major",
        "重大研究计划": "major_research_plan",
        "联合基金项目": "joint_fund",
        "专项基金项目": "special",
        "专项项目": "special",
        "国家杰出青年科学基金项目": "youth_a",
        "国家杰出青年科学基金": "youth_a",
        "青年科学基金项目（A类）": "youth_a",
        "青年科学基金项目(A类)": "youth_a",
        "优秀青年科学基金项目": "youth_b",
        "优秀青年科学基金": "youth_b",
        "青年科学基金项目（B类）": "youth_b",
        "青年科学基金项目(B类)": "youth_b",
        "创新研究群体项目": "innovation_group",
        "基础科学中心项目": "excellence_group",
        "卓越研究群体项目": "excellence_group",
        "国家重大科研仪器研制项目": "major_instrument",
        "重点国际（地区）合作研究项目": "key_international_cooperation",
        "重点国际(地区)合作研究项目": "key_international_cooperation",
        "原创探索计划项目": "original_exploration",
        "外国学者研究基金项目": "foreign_scholar",
    }
    if text in exact:
        return exact[text]
    if "杰出青年" in text or "青年科学基金项目（A类）" in text:
        return "youth_a"
    if "优秀青年" in text or "青年科学基金项目（B类）" in text:
        return "youth_b"
    if "青年" in text:
        return "youth_c"
    if "重大研究计划" in text:
        return "major_research_plan"
    if "重大科研仪器" in text:
        return "major_instrument"
    if "创新研究群体" in text:
        return "innovation_group"
    if "基础科学中心" in text or "卓越研究群体" in text:
        return "excellence_group"
    if "重点国际" in text:
        return "key_international_cooperation"
    if "原创探索" in text:
        return "original_exploration"
    if "外国学者" in text:
        return "foreign_scholar"
    if "重大项目" in text:
        return "major"
    if "重点项目" in text:
        return "key"
    if "面上" in text:
        return "general"
    if "地区" in text:
        return "regional"
    if "联合基金" in text:
        return "joint_fund"
    if "专项" in text:
        return "special"
    return "unmapped"


def discipline_scope(application_code: str) -> str:
    match = re.match(r"^(E(?:0[1-9]|1[0-3]))", clean(application_code).upper())
    return match.group(1) if match else "E_unknown"


def find_partition_files(directory: Path) -> tuple[Path, Path, Path]:
    coverage = list(directory.glob("*_coverage.json"))
    csv_files = [
        path
        for path in directory.glob("*.csv.gz")
        if "counts" not in path.name and "observations" not in path.name
    ]
    raw_files = list(directory.glob("*_raw.jsonl.gz"))
    if len(coverage) != 1 or len(csv_files) != 1 or len(raw_files) != 1:
        raise RuntimeError(
            f"{directory}: expected exactly one coverage, project CSV, and raw JSONL; "
            f"coverage={coverage}, csv={csv_files}, raw={raw_files}"
        )
    return coverage[0], csv_files[0], raw_files[0]


def discover_partitions(input_roots: list[Path]) -> dict[tuple[str, int, int], dict[str, Any]]:
    discovered: dict[tuple[str, int, int], dict[str, Any]] = {}
    for root in input_roots:
        for coverage_path in root.rglob("*_coverage.json"):
            directory = coverage_path.parent
            coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
            code = clean(coverage.get("query_code")).upper()
            start = int(coverage.get("start_conclusion_year"))
            end = int(coverage.get("end_conclusion_year"))
            key = (code, start, end)
            _, csv_path, raw_path = find_partition_files(directory)
            item = {
                "key": key,
                "directory": directory,
                "coverage_path": coverage_path,
                "csv_path": csv_path,
                "raw_path": raw_path,
                "coverage": coverage,
            }
            if key in discovered:
                previous = discovered[key]
                raise RuntimeError(
                    f"duplicate partition {key}: {previous['directory']} and {directory}"
                )
            discovered[key] = item
    return discovered


def load_plan(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    partitions = value.get("partitions")
    if not isinstance(partitions, list) or not partitions:
        raise RuntimeError("partition plan must contain a non-empty partitions list")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for item in partitions:
        code = clean(item.get("code")).upper()
        start = int(item.get("start_conclusion_year"))
        end = int(item.get("end_conclusion_year"))
        if not re.fullmatch(r"E\d{2,6}", code):
            raise RuntimeError(f"invalid plan code: {code!r}")
        if start < 1986 or end < start:
            raise RuntimeError(f"invalid plan range for {code}: {start}-{end}")
        key = (code, start, end)
        if key in seen:
            raise RuntimeError(f"duplicate plan partition: {key}")
        seen.add(key)
        normalized.append(
            {
                "code": code,
                "start_conclusion_year": start,
                "end_conclusion_year": end,
                "notes": clean(item.get("notes")),
            }
        )
    return normalized


def validate_partition(item: dict[str, Any], expected: dict[str, Any]) -> None:
    coverage = item["coverage"]
    code = expected["code"]
    start = expected["start_conclusion_year"]
    end = expected["end_conclusion_year"]
    if clean(coverage.get("query_code")).upper() != code:
        raise RuntimeError(f"{item['directory']}: query-code mismatch")
    if int(coverage.get("start_conclusion_year")) != start:
        raise RuntimeError(f"{item['directory']}: start-year mismatch")
    if int(coverage.get("end_conclusion_year")) != end:
        raise RuntimeError(f"{item['directory']}: end-year mismatch")
    if not coverage.get("complete"):
        raise RuntimeError(f"{item['directory']}: partition not marked complete")
    if int(coverage.get("code_contamination_rows") or 0):
        raise RuntimeError(f"{item['directory']}: code contamination present")
    if int(coverage.get("duplicate_conflicts") or 0):
        raise RuntimeError(f"{item['directory']}: within-partition conflicts present")
    for year in range(start, end + 1):
        stats = coverage.get("annual", {}).get(str(year))
        if not isinstance(stats, dict):
            raise RuntimeError(f"{item['directory']}: missing annual coverage for {year}")
        reported = int(stats.get("reported_total") or 0)
        seen = int(stats.get("rows_seen") or 0)
        valid = int(stats.get("valid_approval_numbers") or 0)
        if reported != seen or reported != valid:
            raise RuntimeError(
                f"{item['directory']}: annual mismatch {year}: "
                f"reported={reported}, rows={seen}, valid={valid}"
            )
        if reported > 1000:
            raise RuntimeError(
                f"{item['directory']}: {code}/{year} exceeds public page cap: {reported}"
            )


def read_project_csv(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [field for field in PROJECT_FIELDS if field not in (reader.fieldnames or [])]
        if missing:
            raise RuntimeError(f"{path}: missing fields {missing}")
        for raw in reader:
            rows.append({field: clean(raw.get(field)) for field in PROJECT_FIELDS})
    return rows


def write_sqlite(path: Path, records: list[dict[str, str]]) -> None:
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
            "INSERT INTO projects VALUES (:approval_number,:title,:project_type_raw,:institution,"
            ":person_in_charge,:amount_wan,:approval_year,:keywords,:application_code_1,"
            ":conclusion_year,:source_record_id,:query_code,:source)",
            records,
        )
        connection.execute("CREATE INDEX idx_e_year ON projects(approval_year)")
        connection.execute("CREATE INDEX idx_e_code ON projects(application_code_1)")
        connection.execute("CREATE INDEX idx_e_type ON projects(project_type_raw)")
        connection.commit()
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", action="append", required=True)
    parser.add_argument("--partition-plan", required=True)
    parser.add_argument("--broad-scan", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    input_roots = [Path(value) for value in args.input_root]
    plan = load_plan(Path(args.partition_plan))
    discovered = discover_partitions(input_roots)
    selected: list[dict[str, Any]] = []
    for expected in plan:
        key = (
            expected["code"],
            expected["start_conclusion_year"],
            expected["end_conclusion_year"],
        )
        item = discovered.get(key)
        if item is None:
            available = sorted(discovered)
            raise RuntimeError(f"missing planned partition {key}; available={available}")
        validate_partition(item, expected)
        selected.append({**item, "plan": expected})

    broad = json.loads(Path(args.broad_scan).read_text(encoding="utf-8"))
    if clean(broad.get("query_code")).upper() != "E":
        raise RuntimeError("broad scan must use query code E")
    broad_annual = {
        str(year): int(total)
        for year, total in broad.get("annual_totals", {}).items()
    }
    if not broad_annual:
        raise RuntimeError("broad scan contains no annual totals")

    partition_annual: dict[str, int] = defaultdict(int)
    partition_summaries: list[dict[str, Any]] = []
    unique: dict[str, dict[str, str]] = {}
    duplicate_conflicts: list[dict[str, Any]] = []
    duplicate_identical = 0
    for item in selected:
        coverage = item["coverage"]
        code = item["plan"]["code"]
        start = item["plan"]["start_conclusion_year"]
        end = item["plan"]["end_conclusion_year"]
        subtotal = 0
        for year in range(start, end + 1):
            total = int(coverage["annual"][str(year)]["reported_total"])
            partition_annual[str(year)] += total
            subtotal += total
        rows = read_project_csv(item["csv_path"])
        if len(rows) != int(coverage.get("unique_approval_numbers") or 0):
            raise RuntimeError(
                f"{item['csv_path']}: row count {len(rows)} differs from coverage "
                f"{coverage.get('unique_approval_numbers')}"
            )
        for record in rows:
            approval = record["approval_number"]
            app_code = record["application_code_1"]
            if not approval:
                raise RuntimeError(f"{item['csv_path']}: blank approval number")
            if app_code and not app_code.startswith(code):
                raise RuntimeError(
                    f"{item['csv_path']}: {approval} has {app_code}, outside {code}"
                )
            existing = unique.get(approval)
            if existing is None:
                unique[approval] = record
            elif existing == record:
                duplicate_identical += 1
            else:
                duplicate_conflicts.append(
                    {
                        "approval_number": approval,
                        "existing": existing,
                        "new": record,
                        "new_partition": code,
                    }
                )
        partition_summaries.append(
            {
                "code": code,
                "start_conclusion_year": start,
                "end_conclusion_year": end,
                "reported_total": subtotal,
                "unique_approval_numbers": len(rows),
                "coverage_file": str(item["coverage_path"]),
                "csv_file": str(item["csv_path"]),
                "raw_file": str(item["raw_path"]),
            }
        )

    all_years = sorted(set(broad_annual) | set(partition_annual), key=int)
    annual_comparison = {
        year: {
            "broad_e_total": int(broad_annual.get(year, 0)),
            "partition_sum": int(partition_annual.get(year, 0)),
            "difference": int(partition_annual.get(year, 0))
            - int(broad_annual.get(year, 0)),
        }
        for year in all_years
    }
    mismatched_years = {
        year: values
        for year, values in annual_comparison.items()
        if values["difference"] != 0
    }
    if mismatched_years:
        raise RuntimeError(
            "partition plan does not reconcile with broad E scan: "
            + json.dumps(mismatched_years, ensure_ascii=False)
        )
    if duplicate_conflicts or duplicate_identical:
        raise RuntimeError(
            f"planned partitions overlap: identical={duplicate_identical}, "
            f"conflicts={len(duplicate_conflicts)}"
        )

    records = [unique[key] for key in sorted(unique)]
    official_total = sum(broad_annual.values())
    if len(records) != official_total:
        raise RuntimeError(
            f"unique approval numbers {len(records)} differ from broad E total {official_total}"
        )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    master_csv = output_dir / "nsfc_e_completed_master.csv.gz"
    master_sqlite = output_dir / "nsfc_e_completed_master.sqlite"
    master_raw = output_dir / "nsfc_e_completed_raw.jsonl.gz"
    observations_csv = output_dir / "nsfc_e_number_learning_observations.csv.gz"
    counts_csv = output_dir / "nsfc_e_counts_by_year_code_type.csv"
    type_values_csv = output_dir / "nsfc_e_project_type_values.csv"
    coverage_json = output_dir / "nsfc_e_completed_coverage.json"
    conflicts_jsonl = output_dir / "nsfc_e_merge_conflicts.jsonl"

    with gzip.open(master_csv, "wt", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PROJECT_FIELDS)
        writer.writeheader()
        writer.writerows(records)
    write_sqlite(master_sqlite, records)

    with gzip.open(master_raw, "wt", encoding="utf-8") as output_handle:
        for item in selected:
            with gzip.open(item["raw_path"], "rt", encoding="utf-8") as input_handle:
                for line in input_handle:
                    if line.strip():
                        output_handle.write(line if line.endswith("\n") else line + "\n")

    conflicts_jsonl.write_text("", encoding="utf-8")

    observations: list[dict[str, str]] = []
    counts: Counter[tuple[str, str, str]] = Counter()
    raw_type_counts: Counter[str] = Counter()
    mapped_type_counts: Counter[str] = Counter()
    for record in records:
        project_type = normalize_type(record["project_type_raw"])
        scope = discipline_scope(record["application_code_1"])
        observations.append(
            {
                "approval_number": record["approval_number"],
                "approval_year": record["approval_year"],
                "project_type": project_type,
                "project_type_raw": record["project_type_raw"],
                "discipline_root": "E",
                "discipline_scope": scope,
                "application_code_1": record["application_code_1"],
                "anchor": "true",
                "source": "nsfc_public_completed_project_endpoint",
            }
        )
        counts[(record["approval_year"], scope, project_type)] += 1
        raw_type_counts[record["project_type_raw"]] += 1
        mapped_type_counts[project_type] += 1

    with gzip.open(observations_csv, "wt", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OBSERVATION_FIELDS)
        writer.writeheader()
        writer.writerows(observations)

    with counts_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["approval_year", "discipline_scope", "project_type", "unique_projects"]
        )
        for key, count in sorted(counts.items()):
            writer.writerow([*key, count])

    with type_values_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["project_type_raw", "project_type", "count"])
        for raw_value, count in sorted(raw_type_counts.items()):
            writer.writerow([raw_value, normalize_type(raw_value), count])

    coverage = {
        "generated_at": utc_now(),
        "scope": "NSFC public completed-project endpoint; primary application codes E01-E13",
        "data_boundary": "complete for the public completed-project endpoint and selected conclusion-year window; not all awarded or active E projects",
        "broad_scan": broad,
        "partition_plan": plan,
        "partition_summaries": partition_summaries,
        "annual_reconciliation": annual_comparison,
        "official_reported_total": official_total,
        "unique_approval_numbers": len(records),
        "number_learning_observations": len(observations),
        "duplicate_identical": duplicate_identical,
        "duplicate_conflicts": len(duplicate_conflicts),
        "project_type_counts": dict(mapped_type_counts),
        "complete": True,
        "completeness_status": "official_completed_subset_complete",
    }
    coverage_json.write_text(
        json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    output_files = [
        master_csv,
        master_sqlite,
        master_raw,
        observations_csv,
        counts_csv,
        type_values_csv,
        coverage_json,
        conflicts_jsonl,
    ]
    manifest = {
        "dataset": "nsfc_e_public_completed_project_base",
        "generated_at": utc_now(),
        "scope": coverage["scope"],
        "files": [
            {
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
            for path in output_files
        ],
    }
    manifest_path = output_dir / "dataset_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    summary_path = output_dir / "EXECUTION_SUMMARY.md"
    summary_path.write_text(
        "\n".join(
            [
                "# NSFC E口官方公开已结题基础数据",
                "",
                "- 口径：主申请代码属于 E01—E13。",
                "- 数据源：国家自然科学基金知识库公开结题项目接口。",
                f"- 独立广义E口逐年扫描合计：{official_total:,} 项。",
                f"- 分区合并后唯一批准号：{len(records):,} 项。",
                "- 校验：每个分区逐页完整；分区逐年合计与独立广义E口逐年扫描完全一致；跨分区批准号无重复。",
                f"- 号段学习观测：{len(observations):,} 条。",
                "- 数据边界：这是公开已结题项目完整子集，不是全部获批或在研E口项目。",
                "- 编号规则边界：仅用于导航、缺口探针和异常检测，不用于自动推断负责人、单位、题名或申请代码。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "status": "success",
                "official_completed_projects": len(records),
                "number_learning_observations": len(observations),
                "output_dir": str(output_dir),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
