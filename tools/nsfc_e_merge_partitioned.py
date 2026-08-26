#!/usr/bin/env python3
"""Merge E01--E13 public completed-project partitions and audit completeness.

The result is the complete dataset exposed by the public completed-project API
for primary application codes E01--E13 within the requested conclusion-year
window.  It is not the universe of all awarded or active E projects.
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
from typing import Any, Iterable

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
EXPECTED_CODES = [f"E{i:02d}" for i in range(1, 14)]


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
    code = clean(application_code).upper()
    match = re.match(r"^(E(?:0[1-9]|1[0-3]))", code)
    return match.group(1) if match else "E_unknown"


def read_records(
    paths: Iterable[Path],
) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]], int]:
    unique: dict[str, dict[str, str]] = {}
    conflicts: list[dict[str, Any]] = []
    duplicate_count = 0
    for path in sorted(paths):
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = [field for field in PROJECT_FIELDS if field not in (reader.fieldnames or [])]
            if missing:
                raise RuntimeError(f"{path.name}: missing columns {missing}")
            for raw in reader:
                record = {field: clean(raw.get(field)) for field in PROJECT_FIELDS}
                approval = record["approval_number"]
                if not approval:
                    continue
                query_code = record["query_code"]
                app_code = record["application_code_1"]
                if query_code not in EXPECTED_CODES:
                    raise RuntimeError(
                        f"{path.name}: unexpected query_code {query_code!r} for {approval}"
                    )
                if app_code and not app_code.startswith(query_code):
                    raise RuntimeError(
                        f"{path.name}: application code {app_code} is outside {query_code} for {approval}"
                    )
                existing = unique.get(approval)
                if existing is None:
                    unique[approval] = record
                else:
                    duplicate_count += 1
                    if existing != record:
                        conflicts.append(
                            {
                                "approval_number": approval,
                                "existing": existing,
                                "new": record,
                                "source_file": path.name,
                            }
                        )
    return unique, conflicts, duplicate_count


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


def longest_common_prefix(values: list[str]) -> str:
    if not values:
        return ""
    low, high = min(values), max(values)
    index = 0
    for left, right in zip(low, high):
        if left != right:
            break
        index += 1
    return low[:index]


def learn_seed_segments(observations: list[dict[str, str]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    root_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in observations:
        groups[
            (row["discipline_scope"], row["approval_year"], row["project_type"])
        ].append(row["approval_number"])
        root_groups[(row["approval_year"], row["project_type"])].append(
            row["approval_number"]
        )

    learned: list[dict[str, Any]] = []
    for (scope, year, project_type), values in sorted(groups.items()):
        numbers = sorted(set(values))
        numeric = all(item.isdigit() for item in numbers)
        lengths = sorted(set(len(item) for item in numbers))
        prefix = (
            longest_common_prefix(numbers)
            if len(numbers) >= 2 and len(lengths) == 1
            else ""
        )
        serial_width = lengths[0] - len(prefix) if prefix and len(lengths) == 1 else None
        density = None
        range_size = None
        gaps: list[str] = []
        if numeric and serial_width and serial_width > 0:
            serials = [int(item[len(prefix) :]) for item in numbers]
            minimum, maximum = min(serials), max(serials)
            range_size = maximum - minimum + 1
            density = len(serials) / range_size if range_size else None
            if range_size <= 5000:
                have = set(serials)
                gaps = [
                    prefix + str(item).zfill(serial_width)
                    for item in range(minimum, maximum + 1)
                    if item not in have
                ]
        if len(numbers) == 1:
            quality = "singleton"
        elif density is None:
            quality = "complex"
        elif density >= 0.999:
            quality = "contiguous"
        elif density >= 0.8:
            quality = "dense"
        elif density >= 0.4:
            quality = "moderate"
        else:
            quality = "sparse"
        root_values = sorted(set(root_groups[(year, project_type)]))
        root_prefix = longest_common_prefix(root_values) if len(root_values) >= 2 else ""
        learned.append(
            {
                "discipline_root": "E",
                "discipline_scope": scope,
                "approval_year": year,
                "project_type": project_type,
                "observed_count": len(numbers),
                "number_kind": "numeric" if numeric else "alphanumeric",
                "number_length": "|".join(map(str, lengths)),
                "common_prefix": prefix,
                "root_type_prefix": root_prefix,
                "prefix_extension": (
                    prefix[len(root_prefix) :] if prefix.startswith(root_prefix) else ""
                ),
                "serial_width": serial_width if serial_width is not None else "",
                "min_number": numbers[0],
                "max_number": numbers[-1],
                "range_size": range_size if range_size is not None else "",
                "density": f"{density:.6f}" if density is not None else "",
                "internal_gap_count": len(gaps),
                "gap_examples": ";".join(gaps[:30]),
                "segment_quality": quality,
                "evidence_scope": "official_completed_subset",
            }
        )
    return learned


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", required=True)
    parser.add_argument("--broad-scan", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    input_root = Path(args.input_root)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_shards = list(input_root.rglob("nsfc_e??_completed_*.csv.gz"))
    raw_shards = list(input_root.rglob("nsfc_e??_completed_*_raw.jsonl.gz"))
    coverage_shards = list(input_root.rglob("nsfc_e??_completed_*_coverage.json"))
    if not csv_shards:
        raise SystemExit("no E01-E13 CSV shards found")

    broad_scan = json.loads(Path(args.broad_scan).read_text(encoding="utf-8"))
    broad_annual = {
        str(year): int(value)
        for year, value in broad_scan.get("annual_totals", {}).items()
    }
    if not broad_annual:
        raise SystemExit("broad E scan has no annual totals")

    partition_codes: set[str] = set()
    aggregate_annual: dict[str, int] = defaultdict(int)
    partition_totals: dict[str, int] = {}
    coverage_documents: list[dict[str, Any]] = []
    for path in sorted(coverage_shards):
        value = json.loads(path.read_text(encoding="utf-8"))
        coverage_documents.append(value)
        code = clean(value.get("query_code"))
        if code not in EXPECTED_CODES:
            raise RuntimeError(f"{path.name}: unexpected query code {code!r}")
        if not value.get("complete"):
            raise RuntimeError(f"{path.name}: partition is not complete")
        if int(value.get("code_contamination_rows") or 0):
            raise RuntimeError(f"{path.name}: code contamination detected")
        partition_codes.add(code)
        partition_total = 0
        for year, stats in value.get("annual", {}).items():
            reported = int(stats.get("reported_total") or 0)
            seen = int(stats.get("rows_seen") or 0)
            if reported != seen:
                raise RuntimeError(
                    f"{path.name}: annual mismatch for {year}: {reported} != {seen}"
                )
            if reported > 1000:
                raise RuntimeError(
                    f"{path.name}: {code}/{year} exceeds public page cap: {reported}"
                )
            aggregate_annual[str(year)] += reported
            partition_total += reported
        partition_totals[code] = partition_total

    missing_codes = sorted(set(EXPECTED_CODES) - partition_codes)
    extra_codes = sorted(partition_codes - set(EXPECTED_CODES))
    if missing_codes or extra_codes:
        raise RuntimeError(
            f"partition set mismatch; missing={missing_codes}, extra={extra_codes}"
        )

    all_years = sorted(set(broad_annual) | set(aggregate_annual), key=int)
    annual_differences = {
        year: {
            "broad_e_total": int(broad_annual.get(year, 0)),
            "e01_e13_sum": int(aggregate_annual.get(year, 0)),
            "difference": int(aggregate_annual.get(year, 0))
            - int(broad_annual.get(year, 0)),
        }
        for year in all_years
        if int(aggregate_annual.get(year, 0)) != int(broad_annual.get(year, 0))
    }
    if annual_differences:
        raise RuntimeError(
            "E01-E13 totals do not reconcile with independent broad-E scan: "
            + json.dumps(annual_differences, ensure_ascii=False)
        )

    unique, conflicts, duplicate_count = read_records(csv_shards)
    records = [unique[key] for key in sorted(unique)]
    expected_total = sum(broad_annual.values())
    raw_row_total = sum(aggregate_annual.values())
    if raw_row_total != expected_total:
        raise RuntimeError(
            f"partition row total {raw_row_total} differs from broad E total {expected_total}"
        )
    if duplicate_count or conflicts:
        raise RuntimeError(
            f"E01-E13 partitions are not disjoint: duplicates={duplicate_count}, "
            f"conflicts={len(conflicts)}"
        )
    if len(records) != expected_total:
        raise RuntimeError(
            f"unique approval numbers {len(records)} differ from official total {expected_total}"
        )

    csv_path = output_dir / "nsfc_e_completed_master.csv.gz"
    sqlite_path = output_dir / "nsfc_e_completed_master.sqlite"
    raw_path = output_dir / "nsfc_e_completed_raw.jsonl.gz"
    conflict_path = output_dir / "nsfc_e_merge_conflicts.jsonl"
    observations_path = output_dir / "nsfc_e_number_learning_observations.csv.gz"
    seed_rules_path = output_dir / "nsfc_e_number_segment_rules_seed.csv"
    counts_path = output_dir / "nsfc_e_counts_by_year_code_type.csv"
    coverage_path = output_dir / "nsfc_e_completed_coverage.json"

    with gzip.open(csv_path, "wt", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PROJECT_FIELDS)
        writer.writeheader()
        writer.writerows(records)
    write_sqlite(sqlite_path, records)

    with gzip.open(raw_path, "wt", encoding="utf-8") as output_handle:
        for shard in sorted(raw_shards):
            with gzip.open(shard, "rt", encoding="utf-8") as input_handle:
                for line in input_handle:
                    if line.strip():
                        output_handle.write(line if line.endswith("\n") else line + "\n")

    conflict_path.write_text("", encoding="utf-8")

    observations: list[dict[str, str]] = []
    counts: Counter[tuple[str, str, str]] = Counter()
    unmapped_types: Counter[str] = Counter()
    for record in records:
        project_type = normalize_type(record["project_type_raw"])
        if project_type == "unmapped":
            unmapped_types[record["project_type_raw"]] += 1
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
    with gzip.open(observations_path, "wt", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=observation_fields)
        writer.writeheader()
        writer.writerows(observations)

    seed_rules = learn_seed_segments(observations)
    seed_fields = list(seed_rules[0].keys()) if seed_rules else []
    with seed_rules_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=seed_fields)
        writer.writeheader()
        writer.writerows(seed_rules)

    with counts_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["approval_year", "discipline_scope", "project_type", "unique_projects"]
        )
        for key, count in sorted(counts.items()):
            writer.writerow([*key, count])

    coverage = {
        "generated_at": utc_now(),
        "scope": "NSFC public completed-project endpoint; E01-E13 partitions",
        "conclusion_year_start": min(map(int, broad_annual)),
        "conclusion_year_end": max(map(int, broad_annual)),
        "partition_strategy": "E01-E13 to remain below the public 100-page cap",
        "partition_codes": EXPECTED_CODES,
        "partition_totals": dict(sorted(partition_totals.items())),
        "annual_totals": dict(sorted(broad_annual.items(), key=lambda item: int(item[0]))),
        "annual_partition_sums": dict(
            sorted(aggregate_annual.items(), key=lambda item: int(item[0]))
        ),
        "reported_total_sum": expected_total,
        "unique_approval_numbers": len(records),
        "merge_duplicate_count": duplicate_count,
        "merge_conflict_count": len(conflicts),
        "number_learning_observations": len(observations),
        "seed_number_segment_rows": len(seed_rules),
        "unmapped_project_types": dict(unmapped_types),
        "broad_scan_reconciled": True,
        "complete": True,
        "completeness_status": "official_completed_subset_complete",
        "limitation": "This does not include active or otherwise not-yet-completed awards.",
        "partition_coverage_documents": coverage_documents,
    }
    coverage_path.write_text(
        json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    output_files = [
        csv_path,
        sqlite_path,
        raw_path,
        conflict_path,
        observations_path,
        seed_rules_path,
        counts_path,
        coverage_path,
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
                "- 口径：主申请代码为 E01—E13。",
                "- 数据源：国家自然科学基金知识库公开结题项目接口。",
                "- 分区：E01—E13，用于规避公开接口单查询最多100页的技术上限。",
                f"- 官方广义E口逐年合计：{expected_total:,} 项。",
                f"- E01—E13合并后唯一批准号：{len(records):,} 项。",
                "- 校验：E01—E13逐年合计与独立广义E口扫描逐年完全一致；批准号无跨分区重复。",
                f"- 号段学习观测：{len(observations):,} 条。",
                f"- 初步经验号段单元：{len(seed_rules):,} 条。",
                "- 数据边界：这是公开已结题项目完整子集，不是全部获批或在研E口项目。",
                "- 规则边界：批准号规律仅用于导航、缺口探针和异常检测，不用于自动推断负责人、单位、题名或申请代码。",
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
                "seed_number_segment_rows": len(seed_rules),
                "output_dir": str(output_dir),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
