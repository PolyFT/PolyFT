#!/usr/bin/env python3
"""Merge E completed-project shards and derive E number-learning observations."""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import sqlite3
from collections import Counter
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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


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
        "青年科学基金项目（A类）": "youth_a",
        "青年科学基金项目(A类)": "youth_a",
        "优秀青年科学基金项目": "youth_b",
        "青年科学基金项目（B类）": "youth_b",
        "青年科学基金项目(B类)": "youth_b",
        "创新研究群体项目": "innovation_group",
        "基础科学中心项目": "excellence_group",
        "卓越研究群体项目": "excellence_group",
        "国家重大科研仪器研制项目": "major_instrument",
        "重点国际（地区）合作研究项目": "key_international_cooperation",
        "重点国际(地区)合作研究项目": "key_international_cooperation",
        "原创探索计划项目": "original_exploration",
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
    if code.startswith("E") and len(code) >= 3 and code[1:3].isdigit():
        return code[:3]
    return "E_unknown"


def read_records(paths: Iterable[Path]) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]]]:
    unique: dict[str, dict[str, str]] = {}
    conflicts: list[dict[str, Any]] = []
    for path in sorted(paths):
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for raw in reader:
                record = {field: clean(raw.get(field)) for field in PROJECT_FIELDS}
                approval = record["approval_number"]
                if not approval:
                    continue
                existing = unique.get(approval)
                if existing is None:
                    unique[approval] = record
                elif existing != record:
                    conflicts.append(
                        {"approval_number": approval, "existing": existing, "new": record, "source_file": path.name}
                    )
    return unique, conflicts


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


def learn_segments(observations: list[dict[str, str]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[str]] = {}
    root_groups: dict[tuple[str, str], list[str]] = {}
    for row in observations:
        key = (row["discipline_scope"], row["approval_year"], row["project_type"])
        groups.setdefault(key, []).append(row["approval_number"])
        root_groups.setdefault((row["approval_year"], row["project_type"]), []).append(row["approval_number"])

    learned: list[dict[str, Any]] = []
    for (scope, year, project_type), values in sorted(groups.items()):
        numbers = sorted(set(values))
        numeric = all(item.isdigit() for item in numbers)
        lengths = sorted(set(len(item) for item in numbers))
        prefix = longest_common_prefix(numbers) if len(numbers) >= 2 and len(lengths) == 1 else ""
        serial_width = lengths[0] - len(prefix) if prefix and len(lengths) == 1 else None
        density = None
        range_size = None
        gaps: list[str] = []
        if numeric and serial_width and serial_width > 0:
            serials = [int(item[len(prefix):]) for item in numbers]
            minimum, maximum = min(serials), max(serials)
            range_size = maximum - minimum + 1
            density = len(serials) / range_size if range_size else None
            if range_size <= 5000:
                have = set(serials)
                gaps = [prefix + str(item).zfill(serial_width) for item in range(minimum, maximum + 1) if item not in have]
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
                "prefix_extension": prefix[len(root_prefix):] if prefix.startswith(root_prefix) else "",
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
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    input_root = Path(args.input_root)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_shards = list(input_root.rglob("nsfc_e_completed_*.csv.gz"))
    raw_shards = list(input_root.rglob("nsfc_e_completed_raw_*.jsonl.gz"))
    coverage_shards = list(input_root.rglob("nsfc_e_completed_coverage_*.json"))
    if not csv_shards:
        raise SystemExit("no E CSV shards found")

    unique, conflicts = read_records(csv_shards)
    records = [unique[key] for key in sorted(unique)]

    csv_path = output_dir / "nsfc_e_completed_master.csv.gz"
    sqlite_path = output_dir / "nsfc_e_completed_master.sqlite"
    raw_path = output_dir / "nsfc_e_completed_raw.jsonl.gz"
    evidence_conflict_path = output_dir / "nsfc_e_merge_conflicts.jsonl"
    observations_path = output_dir / "nsfc_e_number_learning_observations.csv.gz"
    rules_path = output_dir / "nsfc_e_number_segment_rules.csv"
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

    with evidence_conflict_path.open("w", encoding="utf-8") as handle:
        for conflict in conflicts:
            handle.write(json.dumps(conflict, ensure_ascii=False) + "\n")

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

    rule_rows = learn_segments(observations)
    rule_fields = list(rule_rows[0].keys()) if rule_rows else []
    with rules_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rule_fields)
        writer.writeheader()
        writer.writerows(rule_rows)

    with counts_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["approval_year", "discipline_scope", "project_type", "unique_projects"])
        for key, count in sorted(counts.items()):
            writer.writerow([*key, count])

    annual: dict[str, dict[str, int]] = {}
    shard_summaries = []
    for path in sorted(coverage_shards):
        value = json.loads(path.read_text(encoding="utf-8"))
        shard_summaries.append(value)
        for year, stats in value.get("annual", {}).items():
            annual[year] = stats

    coverage = {
        "generated_at": utc_now(),
        "scope": "NSFC public completed-project endpoint only",
        "unique_approval_numbers": len(records),
        "merge_conflict_count": len(conflicts),
        "number_learning_observations": len(observations),
        "number_segment_rule_rows": len(rule_rows),
        "unmapped_project_types": dict(unmapped_types),
        "annual": dict(sorted(annual.items())),
        "shards": shard_summaries,
        "completeness_status": "official_completed_subset_only",
    }
    coverage_path.write_text(json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8")

    output_files = [
        csv_path,
        sqlite_path,
        raw_path,
        evidence_conflict_path,
        observations_path,
        rules_path,
        counts_path,
        coverage_path,
    ]
    manifest = {
        "dataset": "nsfc_e_public_completed_project_base",
        "generated_at": utc_now(),
        "scope": coverage["scope"],
        "files": [
            {"name": path.name, "size_bytes": path.stat().st_size, "sha256": sha256(path)}
            for path in output_files
        ],
    }
    manifest_path = output_dir / "dataset_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    summary_path = output_dir / "EXECUTION_SUMMARY.md"
    summary_path.write_text(
        "\n".join(
            [
                "# NSFC E口官方公开已结题基础数据",
                "",
                f"- 唯一批准号：{len(records):,}",
                f"- 原始分片：{len(csv_shards)}",
                f"- 合并字段冲突：{len(conflicts)}",
                f"- 号段学习观测：{len(observations):,}",
                f"- E口经验号段单元：{len(rule_rows):,}",
                "- 数据边界：仅为基金委公开已结题项目接口子集，不是全部获批E口项目。",
                "- 编号规则边界：仅用于导航、缺口探针和异常检测，不用于自动推断负责人、题名、单位或申请代码。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "status": "success",
                "unique_approval_numbers": len(records),
                "number_segment_rule_rows": len(rule_rows),
                "output_dir": str(output_dir),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
