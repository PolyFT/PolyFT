#!/usr/bin/env python3
"""Validate and merge B01-B09 public completed-project partitions."""
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
EXPECTED_CODES = [f"B{i:02d}" for i in range(1, 10)]


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


def discipline_scope(code: str, approval_year: str) -> str:
    normalized = clean(code).upper()
    year = int(approval_year) if str(approval_year).isdigit() else None
    if year is not None and 2017 <= year <= 2020 and normalized.startswith("B05"):
        return "B05_legacy_material_energy"
    match = re.match(r"^(B0[1-9])", normalized)
    return match.group(1) if match else "B_unknown"


def find_partition(directory: Path) -> tuple[Path, Path, Path]:
    coverages = list(directory.glob("*_coverage.json"))
    csvs = [p for p in directory.glob("*.csv.gz") if "counts" not in p.name]
    raws = list(directory.glob("*_raw.jsonl.gz"))
    if len(coverages) != 1 or len(csvs) != 1 or len(raws) != 1:
        raise RuntimeError(
            f"{directory}: expected one coverage, project CSV and raw JSONL; "
            f"coverage={coverages}, csv={csvs}, raw={raws}"
        )
    return coverages[0], csvs[0], raws[0]


def discover(root: Path) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for coverage_path in root.rglob("*_coverage.json"):
        coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
        code = clean(coverage.get("query_code")).upper()
        if code not in EXPECTED_CODES:
            continue
        if code in found:
            raise RuntimeError(f"duplicate B partition for {code}")
        _, csv_path, raw_path = find_partition(coverage_path.parent)
        found[code] = {
            "coverage": coverage,
            "coverage_path": coverage_path,
            "csv_path": csv_path,
            "raw_path": raw_path,
        }
    return found


def validate_partition(code: str, item: dict[str, Any], start: int, end: int) -> int:
    coverage = item["coverage"]
    if int(coverage.get("start_conclusion_year")) != start or int(coverage.get("end_conclusion_year")) != end:
        raise RuntimeError(f"{code}: conclusion-year range mismatch")
    if not coverage.get("complete"):
        raise RuntimeError(f"{code}: partition is not complete")
    if int(coverage.get("code_contamination_rows") or 0):
        raise RuntimeError(f"{code}: code contamination detected")
    if int(coverage.get("duplicate_conflicts") or 0):
        raise RuntimeError(f"{code}: duplicate conflicts detected")
    annual = coverage.get("annual")
    if not isinstance(annual, dict):
        raise RuntimeError(f"{code}: no annual coverage")
    subtotal = 0
    for year in range(start, end + 1):
        stats = annual.get(str(year))
        if not isinstance(stats, dict):
            raise RuntimeError(f"{code}: missing annual coverage for {year}")
        reported = int(stats.get("reported_total") or 0)
        rows = int(stats.get("rows_seen") or 0)
        valid = int(stats.get("valid_approval_numbers") or 0)
        if reported != rows or reported != valid:
            raise RuntimeError(
                f"{code}/{year}: reported={reported}, rows={rows}, valid={valid}"
            )
        if reported > 1000:
            raise RuntimeError(f"{code}/{year}: exceeds public 100-page cap ({reported})")
        subtotal += reported
    if subtotal != int(coverage.get("raw_rows") or 0):
        raise RuntimeError(f"{code}: annual subtotal differs from raw_rows")
    return subtotal


def read_csv(path: Path) -> list[dict[str, str]]:
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [field for field in PROJECT_FIELDS if field not in (reader.fieldnames or [])]
        if missing:
            raise RuntimeError(f"{path}: missing fields {missing}")
        return [{field: clean(row.get(field)) for field in PROJECT_FIELDS} for row in reader]


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
        connection.execute("CREATE INDEX idx_b_year ON projects(approval_year)")
        connection.execute("CREATE INDEX idx_b_code ON projects(application_code_1)")
        connection.execute("CREATE INDEX idx_b_type ON projects(project_type_raw)")
        connection.commit()
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--start-conclusion-year", type=int, default=1986)
    parser.add_argument("--end-conclusion-year", type=int, default=2026)
    args = parser.parse_args()

    input_root = Path(args.input_root)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    found = discover(input_root)
    missing = sorted(set(EXPECTED_CODES) - set(found))
    if missing:
        raise RuntimeError(f"missing B partitions: {missing}")

    unique: dict[str, dict[str, str]] = {}
    partition_summaries: list[dict[str, Any]] = []
    annual_totals: dict[str, int] = defaultdict(int)
    raw_total = 0
    for code in EXPECTED_CODES:
        item = found[code]
        subtotal = validate_partition(
            code, item, args.start_conclusion_year, args.end_conclusion_year
        )
        rows = read_csv(item["csv_path"])
        if len(rows) != int(item["coverage"].get("unique_approval_numbers") or 0):
            raise RuntimeError(f"{code}: CSV row count differs from coverage")
        for record in rows:
            approval = record["approval_number"]
            if not approval:
                raise RuntimeError(f"{code}: blank approval number")
            if record["application_code_1"] and not record["application_code_1"].startswith(code):
                raise RuntimeError(
                    f"{code}: {approval} has application code {record['application_code_1']}"
                )
            if approval in unique:
                raise RuntimeError(f"cross-partition duplicate approval number: {approval}")
            unique[approval] = record
        for year, stats in item["coverage"]["annual"].items():
            annual_totals[year] += int(stats.get("reported_total") or 0)
        raw_total += subtotal
        partition_summaries.append(
            {
                "code": code,
                "reported_total": subtotal,
                "unique_approval_numbers": len(rows),
                "coverage_file": str(item["coverage_path"]),
            }
        )

    records = [unique[key] for key in sorted(unique)]
    if len(records) != raw_total:
        raise RuntimeError(
            f"unique approval numbers {len(records)} differ from partition total {raw_total}"
        )

    master_csv = output / "nsfc_b_completed_master.csv.gz"
    master_sqlite = output / "nsfc_b_completed_master.sqlite"
    master_raw = output / "nsfc_b_completed_raw.jsonl.gz"
    observations_csv = output / "nsfc_b_number_learning_observations.csv.gz"
    counts_csv = output / "nsfc_b_counts_by_year_code_type.csv"
    type_values_csv = output / "nsfc_b_project_type_values.csv"
    coverage_json = output / "nsfc_b_completed_coverage.json"
    conflicts_jsonl = output / "nsfc_b_merge_conflicts.jsonl"

    with gzip.open(master_csv, "wt", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PROJECT_FIELDS)
        writer.writeheader()
        writer.writerows(records)
    write_sqlite(master_sqlite, records)

    with gzip.open(master_raw, "wt", encoding="utf-8") as output_handle:
        for code in EXPECTED_CODES:
            with gzip.open(found[code]["raw_path"], "rt", encoding="utf-8") as input_handle:
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
    raw_types: Counter[str] = Counter()
    mapped_types: Counter[str] = Counter()
    for record in records:
        project_type = normalize_type(record["project_type_raw"])
        scope = discipline_scope(record["application_code_1"], record["approval_year"])
        observations.append(
            {
                "approval_number": record["approval_number"],
                "approval_year": record["approval_year"],
                "project_type": project_type,
                "project_type_raw": record["project_type_raw"],
                "discipline_root": "B",
                "discipline_scope": scope,
                "application_code_1": record["application_code_1"],
                "anchor": "true",
                "source": "nsfc_public_completed_project_endpoint",
            }
        )
        counts[(record["approval_year"], scope, project_type)] += 1
        raw_types[record["project_type_raw"]] += 1
        mapped_types[project_type] += 1

    with gzip.open(observations_csv, "wt", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=observation_fields)
        writer.writeheader()
        writer.writerows(observations)
    with counts_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["approval_year", "discipline_scope", "project_type", "unique_projects"])
        for key, count in sorted(counts.items()):
            writer.writerow([*key, count])
    with type_values_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["project_type_raw", "project_type", "count"])
        for raw_value, count in sorted(raw_types.items()):
            writer.writerow([raw_value, normalize_type(raw_value), count])

    coverage = {
        "generated_at": utc_now(),
        "scope": "NSFC public completed-project endpoint; primary application codes B01-B09",
        "conclusion_year_start": args.start_conclusion_year,
        "conclusion_year_end": args.end_conclusion_year,
        "reconciliation_mode": "explicit_B01_B09_nonoverlapping_partition_plan",
        "partition_summaries": partition_summaries,
        "annual_partition_totals": dict(sorted(annual_totals.items(), key=lambda item: int(item[0]))),
        "reported_total_sum": raw_total,
        "unique_approval_numbers": len(records),
        "number_learning_observations": len(observations),
        "duplicate_conflicts": 0,
        "project_type_counts": dict(mapped_types),
        "complete": True,
        "completeness_status": "complete_within_explicit_public_completed_project_partition_plan",
        "data_boundary": "Does not include active or otherwise not-yet-completed awards.",
        "historical_code_note": "B05 records approved in 2017-2020 are learned under B05_legacy_material_energy without altering the raw application code.",
    }
    coverage_json.write_text(json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8")

    files = [
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
        "dataset": "nsfc_b_public_completed_project_base",
        "generated_at": utc_now(),
        "scope": coverage["scope"],
        "files": [
            {"name": path.name, "size_bytes": path.stat().st_size, "sha256": sha256(path)}
            for path in files
        ],
    }
    (output / "dataset_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = [
        "# NSFC B口官方公开已结题基础数据",
        "",
        "- 口径：主申请代码属于 B01—B09。",
        "- 数据源：国家自然科学基金知识库公开结题项目接口。",
        "- 分区方式：B01—B09逐代码、逐结题年度采集，并逐单元校验接口报告数、抓取行数和有效批准号数。",
        f"- 各分区报告数合计：{raw_total:,} 项。",
        f"- 合并后唯一批准号：{len(records):,} 项。",
        "- 跨分区批准号重复：0；合并冲突：0。",
        "- 2017—2020年旧B05在规律学习层标记为 B05_legacy_material_energy，不覆盖原始申请代码。",
        "- 数据边界：公开已结题项目基础数据，不是全部获批或在研B口项目。",
    ]
    (output / "EXECUTION_SUMMARY.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "success",
                "reported_total_sum": raw_total,
                "unique_approval_numbers": len(records),
                "partition_count": len(EXPECTED_CODES),
                "output_dir": str(output),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
