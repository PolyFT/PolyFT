#!/usr/bin/env python3
"""Merge multiple NSFC B/E bibliographic-probe batches.

The merger enforces one status row per candidate approval number, deduplicates
source evidence, and produces separate official-upgrade and exact-Web queues.
It does not promote bibliographic or aggregator identity candidates into the
master project table.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def truthy(value: Any) -> bool:
    return clean(value).lower() in {"1", "true", "yes", "y"}


def to_int(value: Any) -> int:
    try:
        return int(float(clean(value)))
    except (TypeError, ValueError):
        return 0


def to_float(value: Any) -> float:
    try:
        return float(clean(value))
    except (TypeError, ValueError):
        return 0.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        return fields, [
            {field: clean(row.get(field)) for field in fields} for row in reader
        ]


def read_csv_gz(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        return fields, [
            {field: clean(row.get(field)) for field in fields} for row in reader
        ]


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_csv_gz(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def discover(input_roots: list[Path]) -> list[Path]:
    found: dict[str, Path] = {}
    for root in input_roots:
        for status_path in root.rglob("probe_status.csv"):
            directory = status_path.parent.resolve()
            key = str(directory)
            found[key] = directory
    return [found[key] for key in sorted(found)]


def require_files(directory: Path) -> dict[str, Path]:
    names = {
        "status": "probe_status.csv",
        "evidence": "bibliographic_evidence.csv.gz",
        "confirmed": "confirmed_project_candidates.csv",
        "followup": "web_followup_queue.csv",
        "quality": "probe_quality.json",
        "report": "PROBE_BATCH_REPORT.md",
        "raw": "probe_raw_responses.jsonl.gz",
    }
    paths = {key: directory / value for key, value in names.items()}
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise RuntimeError(f"incomplete probe batch {directory}: missing={missing}")
    return paths


def priority_rank(value: str) -> int:
    text = clean(value).upper()
    return {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}.get(text[:2], 9)


def status_rank(value: str) -> int:
    return {
        "confirmed_multi_channel": 0,
        "confirmed_openalex_award": 1,
        "confirmed_bibliographic": 2,
        "award_number_only": 3,
        "inconclusive_source_error": 4,
        "no_match_all_sources": 5,
    }.get(clean(value), 9)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", action="append", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--expected-count", type=int, default=0)
    parser.add_argument("--official-upgrade-limit", type=int, default=200)
    parser.add_argument("--web-review-limit", type=int, default=300)
    args = parser.parse_args()

    batch_dirs = discover([Path(value) for value in args.input_root])
    if not batch_dirs:
        raise RuntimeError("no probe batches discovered")

    status_fields: list[str] = []
    evidence_fields: list[str] = []
    confirmed_fields: list[str] = []
    followup_fields: list[str] = []
    status_by_number: dict[str, dict[str, str]] = {}
    evidence_by_key: dict[tuple[str, str, str, str, str], dict[str, str]] = {}
    confirmed_by_number: dict[str, dict[str, str]] = {}
    followup_by_number: dict[str, dict[str, str]] = {}
    qualities: list[dict[str, Any]] = []
    batch_summaries: list[dict[str, Any]] = []

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    raw_output = output / "all_probe_raw_responses.jsonl.gz"
    with gzip.open(raw_output, "wt", encoding="utf-8") as raw_handle:
        for directory in batch_dirs:
            paths = require_files(directory)
            fields, rows = read_csv(paths["status"])
            if not status_fields:
                status_fields = fields
            elif fields != status_fields:
                raise RuntimeError(
                    f"probe_status schema differs in {directory}: {fields} != {status_fields}"
                )
            for row in rows:
                number = row.get("candidate_approval_number", "")
                if not number:
                    raise RuntimeError(f"blank approval number in {paths['status']}")
                if number in status_by_number:
                    raise RuntimeError(
                        f"candidate approval number appears in multiple batches: {number}"
                    )
                row["batch_source_directory"] = str(directory)
                status_by_number[number] = row

            fields, rows = read_csv_gz(paths["evidence"])
            if not evidence_fields:
                evidence_fields = fields
            elif fields != evidence_fields:
                raise RuntimeError(
                    f"evidence schema differs in {directory}: {fields} != {evidence_fields}"
                )
            for row in rows:
                key = (
                    row.get("candidate_approval_number", ""),
                    row.get("source", ""),
                    row.get("source_record_id", ""),
                    row.get("award_id_raw", ""),
                    row.get("evidence_kind", ""),
                )
                evidence_by_key[key] = row

            fields, rows = read_csv(paths["confirmed"])
            if not confirmed_fields:
                confirmed_fields = fields
            elif fields != confirmed_fields:
                raise RuntimeError(
                    f"confirmed schema differs in {directory}: {fields} != {confirmed_fields}"
                )
            for row in rows:
                number = row.get("candidate_approval_number", "")
                if number:
                    confirmed_by_number[number] = row

            fields, rows = read_csv(paths["followup"])
            if not followup_fields:
                followup_fields = fields
            elif fields != followup_fields:
                raise RuntimeError(
                    f"followup schema differs in {directory}: {fields} != {followup_fields}"
                )
            for row in rows:
                number = row.get("candidate_approval_number", "")
                if number:
                    followup_by_number[number] = row

            quality = json.loads(paths["quality"].read_text(encoding="utf-8"))
            qualities.append(quality)
            batch_summaries.append(
                {
                    "directory": str(directory),
                    "selected_count": int(quality.get("selected_count") or len(rows)),
                    "confirmed_candidate_count": int(
                        quality.get("confirmed_candidate_count") or 0
                    ),
                    "evidence_row_count": int(quality.get("evidence_row_count") or 0),
                    "existence_status_counts": quality.get(
                        "existence_status_counts", {}
                    ),
                    "request_count": int(quality.get("request_count") or 0),
                }
            )

            with gzip.open(paths["raw"], "rt", encoding="utf-8") as input_handle:
                for line in input_handle:
                    if line.strip():
                        raw_handle.write(line if line.endswith("\n") else line + "\n")

    status_rows = [status_by_number[key] for key in sorted(status_by_number)]
    evidence_rows = [evidence_by_key[key] for key in sorted(evidence_by_key)]
    confirmed_rows = [confirmed_by_number[key] for key in sorted(confirmed_by_number)]
    followup_rows = [followup_by_number[key] for key in sorted(followup_by_number)]

    if args.expected_count and len(status_rows) != args.expected_count:
        raise RuntimeError(
            f"merged status count {len(status_rows)} != expected {args.expected_count}"
        )
    if set(followup_by_number) != set(status_by_number):
        raise RuntimeError("followup queue does not cover every probed approval number")
    if not set(confirmed_by_number).issubset(status_by_number):
        raise RuntimeError("confirmed candidates include numbers absent from status table")

    all_status_path = output / "ALL_PROBE_STATUS.csv"
    all_evidence_path = output / "ALL_BIBLIOGRAPHIC_EVIDENCE.csv.gz"
    all_confirmed_path = output / "ALL_CONFIRMED_PROJECT_CANDIDATES.csv"
    all_followup_path = output / "ALL_WEB_FOLLOWUP_QUEUE.csv"
    write_csv(
        all_status_path,
        status_rows,
        status_fields + ["batch_source_directory"],
    )
    write_csv_gz(all_evidence_path, evidence_rows, evidence_fields)
    write_csv(all_confirmed_path, confirmed_rows, confirmed_fields)
    write_csv(all_followup_path, followup_rows, followup_fields)

    status_counts = Counter(row.get("existence_status", "") for row in status_rows)
    root_counts = Counter(row.get("discipline_root", "") for row in status_rows)
    priority_counts = Counter(row.get("priority", "") for row in status_rows)
    source_counts: Counter[str] = Counter()
    for row in status_rows:
        for source in clean(row.get("confirmed_sources")).split(";"):
            if source.strip():
                source_counts[source.strip()] += 1
    status_by_root: dict[str, Counter[str]] = defaultdict(Counter)
    for row in status_rows:
        status_by_root[row.get("discipline_root", "")][
            row.get("existence_status", "")
        ] += 1

    confirmed_status_rows = [
        row
        for row in status_rows
        if clean(row.get("existence_status")).startswith("confirmed")
    ]
    confirmed_status_rows.sort(
        key=lambda row: (
            0 if row.get("discipline_root") == "B" else 1,
            priority_rank(row.get("priority", "")),
            status_rank(row.get("existence_status", "")),
            -to_int(row.get("publication_evidence_count")),
            -to_float(row.get("max_segment_density")),
            row.get("candidate_approval_number", ""),
        )
    )
    official_upgrade = confirmed_status_rows[: args.official_upgrade_limit]
    official_fields = [
        "candidate_approval_number",
        "discipline_root",
        "approval_year",
        "project_type_contexts",
        "discipline_scope_contexts",
        "priority",
        "existence_status",
        "evidence_level",
        "confirmed_sources",
        "publication_evidence_count",
        "project_title_candidate",
        "lead_investigator_candidate",
        "institution_candidate",
        "exact_web_query",
        "funding_web_query",
        "institution_site_query",
        "official_confirmation_status",
        "next_action",
        "warning",
    ]
    write_csv(
        output / "OFFICIAL_UPGRADE_QUEUE_v1.csv",
        official_upgrade,
        official_fields,
    )

    web_candidates = [
        row
        for row in status_rows
        if not clean(row.get("existence_status")).startswith("confirmed")
    ]
    web_candidates.sort(
        key=lambda row: (
            priority_rank(row.get("priority", "")),
            truthy(row.get("context_risk")),
            status_rank(row.get("existence_status", "")),
            -to_float(row.get("max_segment_density")),
            to_int(row.get("approval_year")) if to_int(row.get("approval_year")) else 9999,
            row.get("candidate_approval_number", ""),
        )
    )
    exact_web = web_candidates[: args.web_review_limit]
    web_fields = [
        "candidate_approval_number",
        "discipline_root",
        "approval_year",
        "project_type_contexts",
        "discipline_scope_contexts",
        "priority",
        "context_risk",
        "max_segment_density",
        "existence_status",
        "evidence_level",
        "crossref_strict_http_status",
        "crossref_fallback_http_status",
        "openalex_award_http_statuses",
        "openalex_work_http_statuses",
        "exact_web_query",
        "funding_web_query",
        "institution_site_query",
        "next_action",
        "source_errors",
        "warning",
    ]
    write_csv(output / "EXACT_WEB_REVIEW_QUEUE_v1.csv", exact_web, web_fields)

    total = len(status_rows)
    confirmed_count = len(confirmed_status_rows)
    confirmation_rate = confirmed_count / total if total else 0.0
    quality = {
        "generated_at": utc_now(),
        "batch_count": len(batch_dirs),
        "candidate_count": total,
        "expected_count": args.expected_count or None,
        "discipline_root_counts": dict(root_counts),
        "input_priority_counts": dict(priority_counts),
        "existence_status_counts": dict(status_counts),
        "existence_status_by_root": {
            key: dict(value) for key, value in sorted(status_by_root.items())
        },
        "confirmed_candidate_count": confirmed_count,
        "confirmation_rate": confirmation_rate,
        "source_confirmation_counts": dict(source_counts),
        "evidence_row_count": len(evidence_rows),
        "official_upgrade_queue_count": len(official_upgrade),
        "exact_web_review_queue_count": len(exact_web),
        "batch_summaries": batch_summaries,
        "data_boundary": "bibliographic and aggregator evidence only",
        "negative_result_rule": "successful zero-result queries do not prove an unused approval number",
        "identity_rule": "candidate title, investigator, and institution fields require official or institutional confirmation before master-table use",
    }
    quality_path = output / "ALL_PROBE_QUALITY.json"
    quality_path.write_text(
        json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    report = [
        "# NSFC B/E批准号全候选文献证据探针报告",
        "",
        "## 范围",
        "",
        f"- 合并批次：{len(batch_dirs):,}。",
        f"- 候选批准号：{total:,}。",
        f"- B口：{root_counts.get('B', 0):,}；E口：{root_counts.get('E', 0):,}。",
        "- 自动来源：Crossref、OpenAlex Awards、OpenAlex Works。",
        "",
        "## 结果",
        "",
    ]
    for key in (
        "confirmed_multi_channel",
        "confirmed_openalex_award",
        "confirmed_bibliographic",
        "award_number_only",
        "no_match_all_sources",
        "inconclusive_source_error",
    ):
        report.append(f"- `{key}`：{status_counts.get(key, 0):,}。")
    report.extend(
        [
            "",
            f"- 已确认候选：{confirmed_count:,}，自动文献证据恢复率：{confirmation_rate:.1%}。",
            f"- 证据行：{len(evidence_rows):,}。",
            f"- 官方/机构升级队列：{len(official_upgrade):,}。",
            f"- 第一轮精确Web复核队列：{len(exact_web):,}。",
            "",
            "## 边界",
            "",
            "该结果确认的是文献或聚合器中出现了候选批准号及NSFC资助关系，不等同于基金委官方项目主记录。Crossref与OpenAlex可能共享上游元数据，不能仅因双通道命中就机械视为两个完全独立来源。零结果不等于空号。负责人、依托单位和正式项目名称仍需基金委、依托单位或其他高等级页面确认。",
        ]
    )
    report_path = output / "ALL_PROBE_REPORT.md"
    report_path.write_text("\n".join(report) + "\n", encoding="utf-8")

    output_files = [
        raw_output,
        all_status_path,
        all_evidence_path,
        all_confirmed_path,
        all_followup_path,
        output / "OFFICIAL_UPGRADE_QUEUE_v1.csv",
        output / "EXACT_WEB_REVIEW_QUEUE_v1.csv",
        quality_path,
        report_path,
    ]
    manifest = {
        "dataset": "nsfc_be_probe_all_candidates",
        "generated_at": utc_now(),
        "candidate_count": total,
        "batch_directories": [str(path) for path in batch_dirs],
        "files": [
            {
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
            for path in output_files
        ],
    }
    (output / "ALL_PROBE_MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(
        json.dumps(
            {
                "status": "success",
                "batch_count": len(batch_dirs),
                "candidate_count": total,
                "confirmed_candidate_count": confirmed_count,
                "confirmation_rate": confirmation_rate,
                "evidence_row_count": len(evidence_rows),
                "output_dir": str(output),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
