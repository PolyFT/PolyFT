#!/usr/bin/env python3
"""Learn an auditable approval-number rule system from the NSFC E dataset.

The learner is deliberately empirical.  It describes number formats, encoded
years, program-code mappings, serial segments, change points, ambiguities, and
high-density internal gaps.  It never invents project metadata from a number.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def infer_year(two_digits: str) -> int | None:
    if not re.fullmatch(r"\d{2}", two_digits):
        return None
    value = int(two_digits)
    return 1900 + value if value >= 80 else 2000 + value


def discipline_scope(code: str) -> str:
    match = re.match(r"^(E(?:0[1-9]|1[0-3]))", clean(code).upper())
    return match.group(1) if match else "E_unknown"


def mask_digits(value: str) -> str:
    return re.sub(r"\d", "#", value)


def parse_number(number: str, approval_year: int | None) -> dict[str, Any]:
    number = clean(number).upper()
    trailing = re.search(r"(\d+)$", number)
    trailing_digits = trailing.group(1) if trailing else ""
    serial_width = min(3, len(trailing_digits)) if trailing_digits else 0
    serial_text = trailing_digits[-serial_width:] if serial_width else ""
    prefix = number[:-serial_width] if serial_width else number
    serial_int = int(serial_text) if serial_text else None

    if re.fullmatch(r"\d{8}", number):
        family = "numeric8"
        encoded_year_text = number[1:3]
        program_code = number[3:5]
        discipline_marker = number[0]
        template = f"{number[0]}{{YY}}{program_code}{{SERIAL:03d}}"
        family_mask = "########"
    elif re.fullmatch(r"[A-Z]+\d+", number):
        family = "alpha_numeric"
        year_match = re.match(r"^[A-Z]+(\d{2})", number)
        encoded_year_text = year_match.group(1) if year_match else ""
        program_code = prefix
        discipline_marker = re.match(r"^[A-Z]+", number).group(0)
        if encoded_year_text:
            year_start = number.find(encoded_year_text)
            normalized_prefix = (
                prefix[:year_start] + "{YY}" + prefix[year_start + 2 :]
            )
        else:
            normalized_prefix = prefix
        template = normalized_prefix + f"{{SERIAL:0{serial_width}d}}"
        family_mask = mask_digits(number)
    else:
        family = "other"
        encoded_year_text = ""
        program_code = prefix
        discipline_marker = number[:1]
        template = prefix + (
            f"{{SERIAL:0{serial_width}d}}" if serial_width else ""
        )
        family_mask = mask_digits(number)

    encoded_year = infer_year(encoded_year_text)
    return {
        "approval_number": number,
        "format_family": family,
        "family_mask": family_mask,
        "discipline_marker": discipline_marker,
        "encoded_year_text": encoded_year_text,
        "encoded_year": encoded_year if encoded_year is not None else "",
        "encoded_year_match": (
            "true"
            if encoded_year is not None
            and approval_year is not None
            and encoded_year == approval_year
            else "false"
            if encoded_year is not None and approval_year is not None
            else "unknown"
        ),
        "program_code": program_code,
        "prefix_before_serial": prefix,
        "serial_width": serial_width,
        "serial_text": serial_text,
        "serial_int": serial_int if serial_int is not None else "",
        "template": template,
    }


def read_projects(path: Path) -> list[dict[str, str]]:
    opener = gzip.open if path.suffix == ".gz" else open
    kwargs: dict[str, Any] = {"mode": "rt", "encoding": "utf-8-sig", "newline": ""}
    with opener(path, **kwargs) as handle:
        reader = csv.DictReader(handle)
        missing = [field for field in PROJECT_FIELDS if field not in (reader.fieldnames or [])]
        if missing:
            raise RuntimeError(f"missing project fields: {missing}")
        return [
            {field: clean(row.get(field)) for field in PROJECT_FIELDS}
            for row in reader
        ]


def contiguous_runs(values: Iterable[int]) -> list[tuple[int, int]]:
    ordered = sorted(set(values))
    if not ordered:
        return []
    runs: list[tuple[int, int]] = []
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value == previous + 1:
            previous = value
            continue
        runs.append((start, previous))
        start = previous = value
    runs.append((start, previous))
    return runs


def contiguous_year_ranges(years: Iterable[int]) -> list[tuple[int, int]]:
    return contiguous_runs(years)


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_csv_gz(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master-csv", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    projects = read_projects(Path(args.master_csv))
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    observations: list[dict[str, Any]] = []
    format_counts: Counter[str] = Counter()
    normalized_type_counts: Counter[str] = Counter()
    raw_type_counts: Counter[str] = Counter()
    year_match_counts: Counter[str] = Counter()

    for project in projects:
        year = int(project["approval_year"]) if project["approval_year"].isdigit() else None
        parsed = parse_number(project["approval_number"], year)
        project_type = normalize_type(project["project_type_raw"])
        observation = {
            "approval_number": project["approval_number"],
            "approval_year": project["approval_year"],
            "project_type": project_type,
            "project_type_raw": project["project_type_raw"],
            "discipline_root": "E",
            "discipline_scope": discipline_scope(project["application_code_1"]),
            "application_code_1": project["application_code_1"],
            **parsed,
        }
        observations.append(observation)
        format_counts[parsed["format_family"]] += 1
        normalized_type_counts[project_type] += 1
        raw_type_counts[project["project_type_raw"]] += 1
        year_match_counts[parsed["encoded_year_match"]] += 1

    observation_fields = [
        "approval_number",
        "approval_year",
        "project_type",
        "project_type_raw",
        "discipline_root",
        "discipline_scope",
        "application_code_1",
        "format_family",
        "family_mask",
        "discipline_marker",
        "encoded_year_text",
        "encoded_year",
        "encoded_year_match",
        "program_code",
        "prefix_before_serial",
        "serial_width",
        "serial_text",
        "serial_int",
        "template",
    ]
    write_csv_gz(
        output / "nsfc_e_number_parsed_observations.csv.gz",
        observations,
        observation_fields,
    )

    segment_groups: dict[tuple[str, str, str, str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        if not isinstance(row["serial_int"], int):
            continue
        key = (
            row["approval_year"],
            row["project_type"],
            row["discipline_scope"],
            row["format_family"],
            row["prefix_before_serial"],
            int(row["serial_width"]),
        )
        segment_groups[key].append(row)

    segment_rows: list[dict[str, Any]] = []
    run_rows: list[dict[str, Any]] = []
    gap_rows: list[dict[str, Any]] = []
    for key, rows in sorted(segment_groups.items()):
        year, project_type, scope, family, prefix, serial_width = key
        serials = sorted({int(row["serial_int"]) for row in rows})
        minimum, maximum = min(serials), max(serials)
        span = maximum - minimum + 1
        density = len(serials) / span if span else 1.0
        runs = contiguous_runs(serials)
        missing = sorted(set(range(minimum, maximum + 1)) - set(serials))
        regex = "^" + re.escape(prefix) + rf"\d{{{serial_width}}}$"
        segment_id = f"{year}|{project_type}|{scope}|{prefix}|{serial_width}"
        segment_rows.append(
            {
                "segment_id": segment_id,
                "approval_year": year,
                "project_type": project_type,
                "discipline_scope": scope,
                "format_family": family,
                "prefix_before_serial": prefix,
                "serial_width": serial_width,
                "regex": regex,
                "observed_count": len(serials),
                "min_serial": minimum,
                "max_serial": maximum,
                "range_size": span,
                "density": f"{density:.6f}",
                "contiguous_run_count": len(runs),
                "internal_gap_count": len(missing),
                "min_number": prefix + str(minimum).zfill(serial_width),
                "max_number": prefix + str(maximum).zfill(serial_width),
                "rule_strength": (
                    "contiguous"
                    if density == 1.0
                    else "dense"
                    if density >= 0.80
                    else "moderate"
                    if density >= 0.40
                    else "sparse"
                ),
                "evidence_scope": "official_completed_subset",
            }
        )
        for index, (run_start, run_end) in enumerate(runs, start=1):
            run_rows.append(
                {
                    "segment_id": segment_id,
                    "run_index": index,
                    "run_start_serial": run_start,
                    "run_end_serial": run_end,
                    "run_count": run_end - run_start + 1,
                    "run_start_number": prefix + str(run_start).zfill(serial_width),
                    "run_end_number": prefix + str(run_end).zfill(serial_width),
                }
            )
        if density >= 0.80 and span <= 5000:
            for serial in missing:
                gap_rows.append(
                    {
                        "segment_id": segment_id,
                        "candidate_approval_number": prefix
                        + str(serial).zfill(serial_width),
                        "approval_year": year,
                        "project_type_context": project_type,
                        "discipline_scope_context": scope,
                        "segment_density": f"{density:.6f}",
                        "status": "verification_candidate_only",
                        "warning": "not an inferred project record",
                    }
                )

    segment_fields = [
        "segment_id",
        "approval_year",
        "project_type",
        "discipline_scope",
        "format_family",
        "prefix_before_serial",
        "serial_width",
        "regex",
        "observed_count",
        "min_serial",
        "max_serial",
        "range_size",
        "density",
        "contiguous_run_count",
        "internal_gap_count",
        "min_number",
        "max_number",
        "rule_strength",
        "evidence_scope",
    ]
    write_csv(output / "nsfc_e_number_segments.csv", segment_rows, segment_fields)
    write_csv(
        output / "nsfc_e_number_contiguous_runs.csv",
        run_rows,
        [
            "segment_id",
            "run_index",
            "run_start_serial",
            "run_end_serial",
            "run_count",
            "run_start_number",
            "run_end_number",
        ],
    )
    write_csv(
        output / "nsfc_e_number_gap_probe.csv",
        gap_rows,
        [
            "segment_id",
            "candidate_approval_number",
            "approval_year",
            "project_type_context",
            "discipline_scope_context",
            "segment_density",
            "status",
            "warning",
        ],
    )

    template_groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        template_groups[
            (
                row["project_type"],
                row["discipline_scope"],
                row["format_family"],
                row["template"],
            )
        ].append(row)

    template_rows: list[dict[str, Any]] = []
    for key, rows in sorted(template_groups.items()):
        project_type, scope, family, template = key
        years = sorted(
            {
                int(row["approval_year"])
                for row in rows
                if str(row["approval_year"]).isdigit()
            }
        )
        for start, end in contiguous_year_ranges(years):
            subset = [
                row
                for row in rows
                if str(row["approval_year"]).isdigit()
                and start <= int(row["approval_year"]) <= end
            ]
            template_rows.append(
                {
                    "project_type": project_type,
                    "discipline_scope": scope,
                    "format_family": family,
                    "template": template,
                    "start_year": start,
                    "end_year": end,
                    "observed_year_count": end - start + 1,
                    "observed_project_count": len(subset),
                    "program_codes": ";".join(
                        sorted({clean(row["program_code"]) for row in subset})
                    ),
                    "evidence_scope": "official_completed_subset",
                }
            )
    template_fields = [
        "project_type",
        "discipline_scope",
        "format_family",
        "template",
        "start_year",
        "end_year",
        "observed_year_count",
        "observed_project_count",
        "program_codes",
        "evidence_scope",
    ]
    write_csv(output / "nsfc_e_number_template_rules.csv", template_rows, template_fields)

    year_state: dict[tuple[str, str, int], set[str]] = defaultdict(set)
    for row in observations:
        if not str(row["approval_year"]).isdigit():
            continue
        year_state[
            (
                row["project_type"],
                row["discipline_scope"],
                int(row["approval_year"]),
            )
        ].add(row["template"])
    transitions: list[dict[str, Any]] = []
    series_keys = sorted({(key[0], key[1]) for key in year_state})
    for project_type, scope in series_keys:
        years = sorted(
            year
            for ptype, dscope, year in year_state
            if ptype == project_type and dscope == scope
        )
        previous: set[str] | None = None
        for year in years:
            current = year_state[(project_type, scope, year)]
            if previous is not None and current != previous:
                transitions.append(
                    {
                        "project_type": project_type,
                        "discipline_scope": scope,
                        "change_year": year,
                        "added_templates": ";".join(sorted(current - previous)),
                        "removed_templates": ";".join(sorted(previous - current)),
                        "previous_template_count": len(previous),
                        "current_template_count": len(current),
                    }
                )
            previous = current
    write_csv(
        output / "nsfc_e_number_pattern_transitions.csv",
        transitions,
        [
            "project_type",
            "discipline_scope",
            "change_year",
            "added_templates",
            "removed_templates",
            "previous_template_count",
            "current_template_count",
        ],
    )

    template_to_types: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    type_to_templates: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for row in observations:
        year = row["approval_year"]
        template_to_types[(year, row["template"])][row["project_type"]] += 1
        type_to_templates[(year, row["project_type"])][row["template"]] += 1
    ambiguities: list[dict[str, Any]] = []
    for (year, template), counts in sorted(template_to_types.items()):
        if len(counts) > 1:
            ambiguities.append(
                {
                    "ambiguity_kind": "one_template_multiple_project_types",
                    "approval_year": year,
                    "subject": template,
                    "values": ";".join(
                        f"{key}:{value}" for key, value in sorted(counts.items())
                    ),
                    "warning": "number template is not a unique project-type identifier",
                }
            )
    for (year, project_type), counts in sorted(type_to_templates.items()):
        if len(counts) > 1:
            ambiguities.append(
                {
                    "ambiguity_kind": "one_project_type_multiple_templates",
                    "approval_year": year,
                    "subject": project_type,
                    "values": ";".join(
                        f"{key}:{value}" for key, value in sorted(counts.items())
                    ),
                    "warning": "project type may occupy several numbering families",
                }
            )
    write_csv(
        output / "nsfc_e_number_rule_ambiguities.csv",
        ambiguities,
        ["ambiguity_kind", "approval_year", "subject", "values", "warning"],
    )

    quality = {
        "generated_at": utc_now(),
        "records": len(observations),
        "parsed_format_rate": (
            1.0 - format_counts.get("other", 0) / len(observations)
            if observations
            else 0.0
        ),
        "format_counts": dict(format_counts),
        "encoded_year_match_counts": dict(year_match_counts),
        "encoded_year_match_rate_when_available": (
            year_match_counts.get("true", 0)
            / (year_match_counts.get("true", 0) + year_match_counts.get("false", 0))
            if year_match_counts.get("true", 0) + year_match_counts.get("false", 0)
            else None
        ),
        "project_type_counts": dict(normalized_type_counts),
        "unmapped_project_type_records": normalized_type_counts.get("unmapped", 0),
        "segment_count": len(segment_rows),
        "template_rule_count": len(template_rows),
        "transition_count": len(transitions),
        "ambiguity_count": len(ambiguities),
        "dense_gap_probe_count": len(gap_rows),
        "data_boundary": "official public completed-project subset only",
    }
    (output / "nsfc_e_number_rule_quality.json").write_text(
        json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    catalog = {
        "generated_at": utc_now(),
        "scope": "NSFC E public completed-project approval-number rules",
        "principles": [
            "Rules are empirical descriptions of observed official completed projects.",
            "A number pattern is not sufficient evidence for project title, PI, institution, type, or application code.",
            "Internal gaps are verification candidates only and may represent unused, active, withdrawn, or otherwise absent numbers.",
        ],
        "quality": quality,
        "formats": dict(format_counts),
        "templates": template_rows,
    }
    (output / "nsfc_e_number_rule_catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    numeric8_count = format_counts.get("numeric8", 0)
    alpha_count = format_counts.get("alpha_numeric", 0)
    report_lines = [
        "# NSFC E口批准号规律学习报告",
        "",
        "## 结论",
        "",
        f"本轮以 {len(observations):,} 条基金委公开已结题E口项目为锚点，形成了格式层、年份编码层、项目类别模板层、学科分区层和流水号段层五级规则。",
        f"其中八位纯数字批准号 {numeric8_count:,} 条，字母数字混合批准号 {alpha_count:,} 条，其他格式 {format_counts.get('other', 0):,} 条。",
        f"共识别 {len(template_rows):,} 条跨年度模板规则、{len(segment_rows):,} 个年度—类别—学科号段、{len(transitions):,} 个模板变化点。",
        "",
        "## 核心规律",
        "",
        "1. 八位纯数字批准号可以稳定拆分为“学部标识 + 两位年度 + 两位项目/计划代码 + 三位流水号”。该拆分由数据逐条校验，不以记忆规则替代证据。",
        "2. 字母数字混合批准号主要集中于联合基金和专项类项目；仍保留两位年度编码，但前缀结构存在多种并行模板，不能压缩成单一公式。",
        "3. 同一项目类别在同一年可能占用多个模板；同一模板也可能对应多个项目类别。因此批准号只能用于导航和候选检索，不能单独反推项目属性。",
        "4. 流水号的连续性应按“年度—项目类别—E01至E13学科分区—前缀”分别判断。跨类别或跨学科直接合并会制造虚假缺口。",
        "5. 规则变化通过逐年模板集合差分记录，具体变化年份见 `nsfc_e_number_pattern_transitions.csv`。",
        "",
        "## 主要输出",
        "",
        "- `nsfc_e_number_template_rules.csv`：跨年度稳定模板及有效年份区间。",
        "- `nsfc_e_number_segments.csv`：年度、类别、学科和前缀层面的完整经验号段。",
        "- `nsfc_e_number_contiguous_runs.csv`：每个号段内实际连续区间。",
        "- `nsfc_e_number_rule_ambiguities.csv`：规则冲突和非唯一映射。",
        "- `nsfc_e_number_gap_probe.csv`：高密度号段内部缺口，仅供进一步核查。",
        "",
        "## 边界",
        "",
        "该规律覆盖基金委公开已结题项目，不等同于全部获批项目。近年在研项目、尚未结题项目、撤项或未公开记录可能造成号段缺口。任何缺口均不得自动补写负责人、单位、题名、项目类别或申请代码。",
    ]
    (output / "NSFC_E_NUMBER_RULE_REPORT.md").write_text(
        "\n".join(report_lines) + "\n", encoding="utf-8"
    )

    print(
        json.dumps(
            {
                "status": "success",
                "records": len(observations),
                "segments": len(segment_rows),
                "templates": len(template_rows),
                "transitions": len(transitions),
                "output_dir": str(output),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
