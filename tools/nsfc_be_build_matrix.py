#!/usr/bin/env python3
"""Build a unified B/E approval-number coverage matrix and probe queue.

The matrix measures rule-learning coverage, not award-database completeness.
Empty cells are evidence gaps and must never be interpreted as proof that no
project was awarded.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

PROJECT_TYPES = [
    "general",
    "youth_a",
    "youth_b",
    "youth_c",
    "regional",
    "key",
    "major",
    "major_research_plan",
    "joint_fund",
    "innovation_group",
    "excellence_group",
    "major_instrument",
    "key_international_cooperation",
    "original_exploration",
    "special",
    "foreign_scholar",
]
HIGH_VALUE_TYPES = {
    "youth_a",
    "youth_b",
    "key",
    "major",
    "innovation_group",
    "excellence_group",
    "major_instrument",
    "key_international_cooperation",
    "original_exploration",
}
SCOPES = {
    "B": [f"B{i:02d}" for i in range(1, 10)] + ["B05_legacy_material_energy"],
    "E": [f"E{i:02d}" for i in range(1, 14)],
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def require(directory: Path, name: str) -> Path:
    path = directory / name
    if not path.exists():
        raise RuntimeError(f"missing required file: {path}")
    return path


def load_root(root: str, directory: Path) -> dict[str, Any]:
    prefix = f"nsfc_{root.lower()}"
    quality = json.loads(require(directory, f"{prefix}_number_rule_quality.json").read_text(encoding="utf-8"))
    counts = read_csv(require(directory, f"{prefix}_counts_by_year_code_type.csv"))
    segments = read_csv(require(directory, f"{prefix}_number_segments.csv"))
    templates = read_csv(require(directory, f"{prefix}_number_template_rules.csv"))
    transitions = read_csv(require(directory, f"{prefix}_number_pattern_transitions.csv"))
    ambiguities = read_csv(require(directory, f"{prefix}_number_rule_ambiguities.csv"))
    gaps = read_csv(require(directory, f"{prefix}_number_gap_probe.csv"))
    return {
        "root": root,
        "directory": str(directory),
        "quality": quality,
        "counts": counts,
        "segments": segments,
        "templates": templates,
        "transitions": transitions,
        "ambiguities": ambiguities,
        "gaps": gaps,
    }


def parse_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def ambiguity_type_counts(rows: Iterable[dict[str, str]]) -> Counter[tuple[int, str]]:
    counts: Counter[tuple[int, str]] = Counter()
    for row in rows:
        year = parse_int(row.get("approval_year"))
        kind = clean(row.get("ambiguity_kind"))
        if kind == "one_project_type_multiple_templates":
            counts[(year, clean(row.get("subject")))] += 1
        elif kind == "one_template_multiple_project_types":
            for token in clean(row.get("values")).split(";"):
                project_type = token.split(":", 1)[0].strip()
                if project_type:
                    counts[(year, project_type)] += 1
    return counts


def build_matrix(
    datasets: dict[str, dict[str, Any]], start_year: int, end_year: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sample_counts: Counter[tuple[str, int, str, str]] = Counter()
    segments_by_cell: dict[tuple[str, int, str, str], list[dict[str, str]]] = defaultdict(list)
    templates_by_cell: dict[tuple[str, int, str, str], set[str]] = defaultdict(set)
    transition_cells: set[tuple[str, int, str, str]] = set()
    ambiguity_counts: Counter[tuple[str, int, str]] = Counter()
    shared_cells: dict[tuple[str, int, str, str], set[str]] = defaultdict(set)

    for root, data in datasets.items():
        for row in data["counts"]:
            year = parse_int(row.get("approval_year"))
            scope = clean(row.get("discipline_scope"))
            project_type = clean(row.get("project_type"))
            sample_counts[(root, year, scope, project_type)] += parse_int(row.get("unique_projects"))
        for row in data["segments"]:
            year = parse_int(row.get("approval_year"))
            scope = clean(row.get("discipline_scope"))
            project_type = clean(row.get("project_type"))
            segments_by_cell[(root, year, scope, project_type)].append(row)
        for row in data["templates"]:
            scope = clean(row.get("discipline_scope"))
            project_type = clean(row.get("project_type"))
            start = parse_int(row.get("start_year"))
            end = parse_int(row.get("end_year"))
            template = clean(row.get("template"))
            for year in range(max(start_year, start), min(end_year, end) + 1):
                templates_by_cell[(root, year, scope, project_type)].add(template)
        for row in data["transitions"]:
            transition_cells.add(
                (
                    root,
                    parse_int(row.get("change_year")),
                    clean(row.get("discipline_scope")),
                    clean(row.get("project_type")),
                )
            )
        for key, count in ambiguity_type_counts(data["ambiguities"]).items():
            ambiguity_counts[(root, key[0], key[1])] += count

        shared_groups: dict[tuple[int, str, str, str], set[str]] = defaultdict(set)
        for row in data["segments"]:
            group = (
                parse_int(row.get("approval_year")),
                clean(row.get("project_type")),
                clean(row.get("prefix_before_serial")),
                clean(row.get("serial_width")),
            )
            shared_groups[group].add(clean(row.get("discipline_scope")))
        for (year, project_type, _prefix, _width), scopes in shared_groups.items():
            scopes = {scope for scope in scopes if scope}
            if len(scopes) > 1:
                for scope in scopes:
                    shared_cells[(root, year, scope, project_type)].update(scopes - {scope})

    rows: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    for root in ("B", "E"):
        quality = datasets[root]["quality"]
        source_version = f"{quality.get('generated_at','')}|records={quality.get('records','')}"
        for year in range(start_year, end_year + 1):
            for scope in SCOPES[root]:
                for project_type in PROJECT_TYPES:
                    key = (root, year, scope, project_type)
                    sample = sample_counts[key]
                    segments = segments_by_cell.get(key, [])
                    templates = templates_by_cell.get(key, set())
                    transition = key in transition_cells
                    shared_with = sorted(shared_cells.get(key, set()))
                    densities = [parse_float(row.get("density")) for row in segments]
                    segment_count = len(segments)
                    internal_gaps = sum(parse_int(row.get("internal_gap_count")) for row in segments)
                    observed_segment_records = sum(parse_int(row.get("observed_count")) for row in segments)
                    if sample == 0:
                        status = "empty"
                    elif transition:
                        status = "historical_transition"
                    elif shared_with:
                        status = "shared_number_space"
                    elif sample >= 10 and segment_count >= 1 and templates:
                        status = "sufficient"
                    else:
                        status = "partial"
                    if status == "sufficient" and (
                        max(densities, default=0.0) >= 0.80 or sample >= 30
                    ):
                        confidence = "high"
                    elif sample >= 5:
                        confidence = "medium"
                    elif sample > 0:
                        confidence = "low"
                    else:
                        confidence = "none"
                    status_counts[status] += 1
                    min_numbers = sorted(
                        clean(row.get("min_number"))
                        for row in segments
                        if clean(row.get("min_number"))
                    )
                    max_numbers = sorted(
                        clean(row.get("max_number"))
                        for row in segments
                        if clean(row.get("max_number"))
                    )
                    prefixes = sorted(
                        {
                            clean(row.get("prefix_before_serial"))
                            for row in segments
                            if clean(row.get("prefix_before_serial"))
                        }
                    )
                    rows.append(
                        {
                            "discipline_root": root,
                            "discipline_scope": scope,
                            "approval_year": year,
                            "project_type": project_type,
                            "data_layer": "official_completed",
                            "sample_count": sample,
                            "segment_observation_count": observed_segment_records,
                            "segment_count": segment_count,
                            "active_template_count": len(templates),
                            "template_examples": ";".join(sorted(templates)[:8]),
                            "prefix_examples": ";".join(prefixes[:8]),
                            "min_number": min_numbers[0] if min_numbers else "",
                            "max_number": max_numbers[-1] if max_numbers else "",
                            "mean_segment_density": (
                                f"{sum(densities)/len(densities):.6f}" if densities else ""
                            ),
                            "max_segment_density": f"{max(densities):.6f}" if densities else "",
                            "internal_gap_count": internal_gaps,
                            "shared_number_space": "true" if shared_with else "false",
                            "shared_with_scopes": ";".join(shared_with),
                            "transition_status": (
                                "change_point" if transition else "stable_or_unobserved"
                            ),
                            "ambiguity_count_year_type": ambiguity_counts[(root, year, project_type)],
                            "coverage_status": status,
                            "rule_confidence": confidence,
                            "rule_ready_for_gap_probe": (
                                "true"
                                if status == "sufficient" and internal_gaps > 0
                                else "false"
                            ),
                            "source_version": source_version,
                            "warning": "rule-learning coverage only; not an award-completeness claim",
                        }
                    )
    metadata = {
        "status_counts": dict(status_counts),
        "matrix_rows": len(rows),
        "start_year": start_year,
        "end_year": end_year,
    }
    return rows, metadata


def build_high_value_matrix(matrix_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in matrix_rows:
        if row["project_type"] not in HIGH_VALUE_TYPES:
            continue
        status = row["coverage_status"]
        if status == "empty":
            priority = "P0"
        elif status in {"partial", "historical_transition", "shared_number_space"}:
            priority = "P1"
        else:
            priority = "P2"
        rows.append(
            {
                "discipline_root": row["discipline_root"],
                "discipline_scope": row["discipline_scope"],
                "approval_year": row["approval_year"],
                "project_type": row["project_type"],
                "completed_layer_sample_count": row["sample_count"],
                "rule_coverage_status": status,
                "rule_confidence": row["rule_confidence"],
                "anchor_collection_priority": priority,
                "required_next_evidence": (
                    "official award list / division review summary / institutional award announcement"
                ),
                "warning": "zero completed-layer records do not prove zero awards",
            }
        )
    return rows


def priority_rank(label: str) -> int:
    return {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}.get(label, 9)


def build_probe_queue(
    datasets: dict[str, dict[str, Any]],
    matrix_rows: list[dict[str, Any]],
    recent_year: int,
) -> list[dict[str, Any]]:
    cell = {
        (
            row["discipline_root"],
            int(row["approval_year"]),
            row["discipline_scope"],
            row["project_type"],
        ): row
        for row in matrix_rows
    }
    aggregated: dict[tuple[str, str], dict[str, Any]] = {}
    for root, data in datasets.items():
        for row in data["gaps"]:
            number = clean(row.get("candidate_approval_number"))
            if not number:
                continue
            year = parse_int(row.get("approval_year"))
            project_type = clean(row.get("project_type_context"))
            scope = clean(row.get("discipline_scope_context"))
            density = parse_float(row.get("segment_density"))
            context = cell.get((root, year, scope, project_type), {})
            transition_or_shared = context.get("coverage_status") in {
                "historical_transition",
                "shared_number_space",
            }
            if year >= recent_year:
                priority = "P4"
                policy = "quarterly_recheck"
            elif transition_or_shared:
                priority = "P3"
                policy = "manual_context_review_before_probe"
            elif density >= 0.98:
                priority = "P1"
                policy = "immediate_probe"
            elif density >= 0.95:
                priority = "P2"
                policy = "next_probe_batch"
            else:
                priority = "P3"
                policy = "low_rate_probe"
            if (
                project_type in HIGH_VALUE_TYPES
                and priority in {"P2", "P3"}
                and not transition_or_shared
            ):
                priority = "P1" if priority == "P2" else "P2"
            key = (root, number)
            item = aggregated.setdefault(
                key,
                {
                    "discipline_root": root,
                    "candidate_approval_number": number,
                    "approval_year": year,
                    "project_type_contexts": set(),
                    "discipline_scope_contexts": set(),
                    "segment_ids": set(),
                    "max_segment_density": density,
                    "priority": priority,
                    "next_probe_policy": policy,
                    "context_risk": "true" if transition_or_shared else "false",
                },
            )
            item["project_type_contexts"].add(project_type)
            item["discipline_scope_contexts"].add(scope)
            item["segment_ids"].add(clean(row.get("segment_id")))
            item["max_segment_density"] = max(item["max_segment_density"], density)
            if priority_rank(priority) < priority_rank(item["priority"]):
                item["priority"] = priority
                item["next_probe_policy"] = policy
            if transition_or_shared:
                item["context_risk"] = "true"

    rows: list[dict[str, Any]] = []
    for item in aggregated.values():
        number = item["candidate_approval_number"]
        rows.append(
            {
                "discipline_root": item["discipline_root"],
                "candidate_approval_number": number,
                "approval_year": item["approval_year"],
                "project_type_contexts": ";".join(sorted(item["project_type_contexts"])),
                "discipline_scope_contexts": ";".join(
                    sorted(item["discipline_scope_contexts"])
                ),
                "segment_ids": ";".join(sorted(item["segment_ids"])),
                "max_segment_density": f"{item['max_segment_density']:.6f}",
                "priority": item["priority"],
                "context_risk": item["context_risk"],
                "next_probe_policy": item["next_probe_policy"],
                "exact_web_query": f'"{number}"',
                "funding_web_query": (
                    f'"{number}" "National Natural Science Foundation of China"'
                ),
                "crossref_filter": (
                    f"award.number:{number},award.funder:501100001809"
                ),
                "institution_site_query": f'site:edu.cn OR site:ac.cn "{number}"',
                "status": "queued",
                "selected_for_first_batch": "false",
                "warning": "candidate number only; no project fact inferred",
            }
        )
    rows.sort(
        key=lambda row: (
            priority_rank(row["priority"]),
            -parse_float(row["max_segment_density"]),
            parse_int(row["approval_year"]),
            row["discipline_root"],
            row["candidate_approval_number"],
        )
    )
    selected = 0
    for row in rows:
        if selected >= 200:
            break
        if row["priority"] in {"P1", "P2"} and row["context_risk"] == "false":
            row["selected_for_first_batch"] = "true"
            selected += 1
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--b-dir", required=True)
    parser.add_argument("--e-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--start-year", type=int, default=1986)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--recent-year", type=int, default=2022)
    args = parser.parse_args()

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    datasets = {
        "B": load_root("B", Path(args.b_dir)),
        "E": load_root("E", Path(args.e_dir)),
    }
    matrix, matrix_meta = build_matrix(datasets, args.start_year, args.end_year)
    high_value = build_high_value_matrix(matrix)
    probe_queue = build_probe_queue(datasets, matrix, args.recent_year)

    matrix_fields = list(matrix[0].keys())
    high_fields = list(high_value[0].keys()) if high_value else []
    probe_fields = list(probe_queue[0].keys()) if probe_queue else [
        "discipline_root",
        "candidate_approval_number",
        "approval_year",
        "project_type_contexts",
        "discipline_scope_contexts",
        "segment_ids",
        "max_segment_density",
        "priority",
        "context_risk",
        "next_probe_policy",
        "exact_web_query",
        "funding_web_query",
        "crossref_filter",
        "institution_site_query",
        "status",
        "selected_for_first_batch",
        "warning",
    ]
    write_csv(output / "BE_NUMBER_PATTERN_COVERAGE_MATRIX.csv", matrix, matrix_fields)
    write_csv(output / "BE_HIGH_VALUE_PROJECT_MISSING_MATRIX.csv", high_value, high_fields)
    write_csv(output / "BE_PROBE_QUEUE_v1.csv", probe_queue, probe_fields)

    status_counts = Counter(row["coverage_status"] for row in matrix)
    high_counts = Counter(row["anchor_collection_priority"] for row in high_value)
    probe_counts = Counter(row["priority"] for row in probe_queue)
    first_batch = sum(row["selected_for_first_batch"] == "true" for row in probe_queue)
    quality = {
        "generated_at": utc_now(),
        "matrix": matrix_meta,
        "coverage_status_counts": dict(status_counts),
        "high_value_anchor_priority_counts": dict(high_counts),
        "probe_queue_count": len(probe_queue),
        "probe_priority_counts": dict(probe_counts),
        "first_batch_selected": first_batch,
        "B_rule_quality": datasets["B"]["quality"],
        "E_rule_quality": datasets["E"]["quality"],
        "data_boundary": "B/E official completed-project rule-learning coverage only",
    }
    (output / "BE_NUMBER_MATRIX_QUALITY.json").write_text(
        json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    catalog = {
        "generated_at": utc_now(),
        "scope": "NSFC B/E approval-number intelligence",
        "principles": [
            "Number patterns are navigation and completeness probes, not project facts.",
            "Empty cells do not prove that no award existed.",
            "Historical transitions and shared number spaces must be reviewed before probing.",
            "Crossref zero results are negative probes only; HTTP status must be retained.",
            "PI identity requires official or institutional confirmation and is outside this matrix.",
        ],
        "matrix_definition": {
            "dimensions": [
                "discipline_root",
                "discipline_scope",
                "approval_year",
                "project_type",
            ],
            "statuses": [
                "sufficient",
                "partial",
                "empty",
                "historical_transition",
                "shared_number_space",
            ],
        },
        "quality": quality,
    }
    (output / "BE_NUMBER_RULE_CATALOG.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    report = [
        "# NSFC B/E批准号完成矩阵报告",
        "",
        "## 数据基础",
        "",
        f"- B口已结题规则学习记录：{datasets['B']['quality'].get('records', 0):,}。",
        f"- E口已结题规则学习记录：{datasets['E']['quality'].get('records', 0):,}。",
        f"- 矩阵单元：{len(matrix):,}（年度×项目类型×申请代码范围）。",
        "",
        "## 覆盖状态",
        "",
    ]
    for key in [
        "sufficient",
        "partial",
        "empty",
        "historical_transition",
        "shared_number_space",
    ]:
        report.append(f"- `{key}`：{status_counts.get(key, 0):,}。")
    report.extend(
        [
            "",
            "## 高价值项目锚点",
            "",
            f"- 待补锚点矩阵行数：{len(high_value):,}。",
            f"- P0空白单元：{high_counts.get('P0', 0):,}。",
            f"- P1部分/转折/共享单元：{high_counts.get('P1', 0):,}。",
            "",
            "## 缺号探针",
            "",
            f"- 合并去重后的候选批准号：{len(probe_queue):,}。",
            f"- 第一批自动选中：{first_batch:,}。",
            "- 近年编号进入P4季度复查，不按日重复扫描。",
            "- 转折期或共享号段先做人工上下文复核，再进入Web/Crossref/OpenAlex探针。",
            "",
            "## 边界",
            "",
            "该矩阵衡量的是批准号规律学习覆盖度，不是基金项目获批全集完整度。任何空白、断号或连续性均不得自动生成负责人、单位、项目名称、项目类别或申请代码。",
        ]
    )
    (output / "BE_NUMBER_MATRIX_REPORT.md").write_text(
        "\n".join(report) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "success",
                "matrix_rows": len(matrix),
                "high_value_rows": len(high_value),
                "probe_candidates": len(probe_queue),
                "first_batch_selected": first_batch,
                "output_dir": str(output),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
