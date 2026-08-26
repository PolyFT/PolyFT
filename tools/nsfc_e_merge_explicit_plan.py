#!/usr/bin/env python3
"""Merge an explicit NSFC E completed-project partition plan.

This merger does not depend on the broad `code=E` query, because GitHub-hosted
runners can receive persistent HTTP 403 responses for that root query. Instead,
it validates and merges a complete, non-overlapping application-code plan. Each
partition is checked against the official endpoint's reported annual total.
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
EXPECTED_ROOTS = [f"E{i:02d}" for i in range(1, 14)]


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


def load_plan(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    raw = document.get("partitions")
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("partition plan must contain a non-empty partitions list")
    partitions: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for item in raw:
        code = clean(item.get("code")).upper()
        start = int(item.get("start_conclusion_year"))
        end = int(item.get("end_conclusion_year"))
        if not re.fullmatch(r"E\d{2,6}", code):
            raise RuntimeError(f"invalid partition code: {code!r}")
        if start < 1986 or end < start:
            raise RuntimeError(f"invalid partition range: {code} {start}-{end}")
        key = (code, start, end)
        if key in seen:
            raise RuntimeError(f"duplicate partition-plan entry: {key}")
        seen.add(key)
        partitions.append(
            {
                "code": code,
                "start_conclusion_year": start,
                "end_conclusion_year": end,
                "notes": clean(item.get("notes")),
            }
        )
    return document, partitions


def validate_plan(partitions: list[dict[str, Any]]) -> tuple[int, int]:
    start = min(item["start_conclusion_year"] for item in partitions)
    end = max(item["end_conclusion_year"] for item in partitions)
    roots_seen: set[str] = set()
    for year in range(start, end + 1):
        for root in EXPECTED_ROOTS:
            selected = [
                item["code"]
                for item in partitions
                if item["code"].startswith(root)
                and item["start_conclusion_year"] <= year <= item["end_conclusion_year"]
            ]
            if not selected:
                raise RuntimeError(f"partition plan has no coverage for {root}/{year}")
            roots_seen.add(root)
            for index, left in enumerate(selected):
                for right in selected[index + 1 :]:
                    if left.startswith(right) or right.startswith(left):
                        raise RuntimeError(
                            f"overlapping plan prefixes for {root}/{year}: {left}, {right}"
                        )
            if root in selected and len(selected) != 1:
                raise RuntimeError(
                    f"root partition overlaps child partitions for {root}/{year}: {selected}"
                )
    if roots_seen != set(EXPECTED_ROOTS):
        raise RuntimeError(
            f"partition roots mismatch: missing={sorted(set(EXPECTED_ROOTS)-roots_seen)}"
        )
    return start, end


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
            f"{directory}: expected one coverage, project CSV, and raw JSONL; "
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
            if key in discovered:
                raise RuntimeError(
                    f"duplicate discovered partition {key}: "
                    f"{discovered[key]['directory']} and {directory}"
                )
            discovered[key] = {
                "directory": directory,
                "coverage_path": coverage_path,
                "csv_path": csv_path,
                "raw_path": raw_path,
                "coverage": coverage,
            }
    return discovered


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
        raise RuntimeError(f"{item['directory']}: partition is not complete")
    if int(coverage.get("code_contamination_rows") or 0):
        raise RuntimeError(f"{item['directory']}: code contamination detected")
    if int(coverage.get("duplicate_conflicts") or 0):
        raise RuntimeError(f"{item['directory']}: duplicate conflicts detected")
    annual = coverage.get("annual")
    if not isinstance(annual, dict):
        raise RuntimeError(f"{item['directory']}: no annual coverage object")
    total = 0
    for year in range(start, end + 1):
        stats = annual.get(str(year))
        if not isinstance(stats, dict):
            raise RuntimeError(f"{item['directory']}: missing annual coverage for {year}")
        reported = int(stats.get("reported_total") or 0)
        rows_seen = int(stats.get("rows_seen") or 0)
        valid = int(stats.get("valid_approval_numbers") or 0)
        if reported != rows_seen or reported != valid:
            raise RuntimeError(
                f"{item['directory']}: annual mismatch {year}: "
                f"reported={reported}, rows={rows_seen}, valid={valid}"
            )
        if reported > 1000:
            raise RuntimeError(
                f"{item['directory']}: {code}/{year} exceeds public page cap: {reported}"
            )
        total += reported
    if total != int(coverage.get("raw_rows") or 0):
        raise RuntimeError(
            f"{item['directory']}: annual total {total} differs from raw_rows "
            f"{coverage.get('raw_rows')}"
        )


def read_project_csv(path: Path) -> list[dict[str, str]]:
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [field for field in PROJECT_FIELDS if field not in (reader.fieldnames or [])]
        if missing:
            raise RuntimeError(f"{path}: missing fields {missing}")
        return [
            {field: clean(row.get(field)) for field in PROJECT_FIELDS}
            for row in reader
        ]


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
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    plan_document, plan = load_plan(Path(args.partition_plan))
    start_year, end_year = validate_plan(plan)
    discovered = discover_partitions([Path(value) for value in args.input_root])

    selected: list[dict[str, Any]] = []
    for expected in plan:
        key = (
            expected["code"],
            expected["start_conclusion_year"],
            expected["end_conclusion_year"],
        )
        item = discovered.get(key)
        if item is None:
            raise RuntimeError(
                f"missing planned partition {key}; available={sorted(discovered)}"
            )
        validate_partition(item, expected)
        selected.append({**item, "plan": expected})

    annual_totals: dict[str, int] = defaultdict(int)
    partition_summaries: list[dict[str, Any]] = []
    unique: dict[str, dict[str, str]] = {}
    identical_duplicates = 0
    conflicts: list[dict[str, Any]] = []

    for item in selected:
        expected = item["plan"]
        code = expected["code"]
        start = expected["start_conclusion_year"]
        end = expected["end_conclusion_year"]
        coverage = item["coverage"]
        subtotal = 0
        for year in range(start, end + 1):
            total = int(coverage["annual"][str(year)]["reported_total"])
            annual_totals[str(year)] += total
            subtotal += total
        rows = read_project_csv(item["csv_path"])
        if len(rows) != int(coverage.get("unique_approval_numbers") or 0):
            raise RuntimeError(
                f"{item['csv_path']}: CSV rows {len(rows)} differ from coverage "
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
            previous = unique.get(approval)
            if previous is None:
                unique[approval] = record
            elif previous == record:
                identical_duplicates += 1
            else:
                conflicts.append(
                    {
                        "approval_number": approval,
                        "existing": previous,
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
            }
        )

    if identical_duplicates or conflicts:
        raise RuntimeError(
            f"planned partitions overlap: identical={identical_duplicates}, "
            f"conflicts={len(conflicts)}"
        )

    records = [unique[key] for key in sorted(unique)]
    reported_total = sum(annual_totals.values())
    if len(records) != reported_total:
        raise RuntimeError(
            f"unique approval numbers {len(records)} differ from partition total {reported_total}"
        )

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    master_csv = output / "nsfc_e_completed_master.csv.gz"
    master_sqlite = output / "nsfc_e_completed_master.sqlite"
    master_raw = output / "nsfc_e_completed_raw.jsonl.gz"
    observations_csv = output / "nsfc_e_number_learning_observations.csv.gz"
    counts_csv = output / "nsfc_e_counts_by_year_code_type.csv"
    type_values_csv = output / "nsfc_e_project_type_values.csv"
    coverage_json = output / "nsfc_e_completed_coverage.json"
    conflicts_jsonl = output / "nsfc_e_merge_conflicts.jsonl"

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

    observation_fields = [
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
        writer = csv.DictWriter(handle, fieldnames=observation_fields)
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

    annual_sorted = dict(sorted(annual_totals.items(), key=lambda item: int(item[0])))
    coverage = {
        "generated_at": utc_now(),
        "scope": "NSFC public completed-project endpoint; primary application codes E01-E13",
        "conclusion_year_start": start_year,
        "conclusion_year_end": end_year,
        "reconciliation_mode": "explicit_official_nonoverlapping_partition_plan",
        "independent_broad_root_scan": {
            "status": "not_used",
            "reason": "GitHub-hosted runners returned persistent HTTP 403 for the broad code=E query",
        },
        "partition_plan": plan_document,
        "partition_summaries": partition_summaries,
        "annual_partition_totals": annual_sorted,
        "reported_total_sum": reported_total,
        "unique_approval_numbers": len(records),
        "number_learning_observations": len(observations),
        "duplicate_identical": identical_duplicates,
        "duplicate_conflicts": len(conflicts),
        "project_type_counts": dict(mapped_type_counts),
        "complete": True,
        "completeness_status": "complete_within_explicit_public_completed_project_partition_plan",
        "data_boundary": "Does not include active or otherwise not-yet-completed awards.",
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
    manifest_path = output / "dataset_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    summary = [
        "# NSFC E口官方公开已结题基础数据",
        "",
        "- 口径：主申请代码属于 E01—E13。",
        "- 数据源：国家自然科学基金知识库公开结题项目接口。",
        "- 分区方式：E01—E13；其中 E05 自2015年起细分为 E0501—E0512，E08 自2022年起细分为 E0801—E0810，以规避单查询100页上限。",
        f"- 各分区官方报告数合计：{reported_total:,} 项。",
        f"- 合并后唯一批准号：{len(records):,} 项。",
        "- 校验：每个分区逐页抓取数、有效批准号数与接口报告数一致；显式分区在年度和代码前缀上不重叠；跨分区批准号无重复。",
        "- 广义E口根查询：GitHub托管运行器持续返回HTTP 403，因此未将其作为独立校验条件，也未声称完成该项核验。",
        f"- 号段学习观测：{len(observations):,} 条。",
        "- 数据边界：这是公开已结题项目完整分区数据，不是全部获批或在研E口项目。",
    ]
    (output / "EXECUTION_SUMMARY.md").write_text(
        "\n".join(summary) + "\n", encoding="utf-8"
    )

    print(
        json.dumps(
            {
                "status": "success",
                "reported_total_sum": reported_total,
                "unique_approval_numbers": len(records),
                "partition_count": len(selected),
                "output_dir": str(output),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
