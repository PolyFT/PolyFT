#!/usr/bin/env python3
"""Run the audited E-style approval-number learner for NSFC B projects.

The learner intentionally treats 2017-2020 B05 records as the historical
"materials chemistry and energy chemistry" scope. Raw application codes remain
unchanged in the final master dataset; only the rule-learning scope is versioned.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import nsfc_e_learn_number_rules as engine


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def b_scope(code: str) -> str:
    normalized = clean(code).upper()
    if normalized.startswith("B05LEGACY|"):
        return "B05_legacy_material_energy"
    match = re.match(r"^(B0[1-9])", normalized)
    return match.group(1) if match else "B_unknown"


def prepare_learning_input(source: Path, destination: Path) -> None:
    opener = gzip.open if source.suffix == ".gz" else open
    with opener(source, "rt", encoding="utf-8-sig", newline="") as input_handle:
        reader = csv.DictReader(input_handle)
        missing = [field for field in engine.PROJECT_FIELDS if field not in (reader.fieldnames or [])]
        if missing:
            raise RuntimeError(f"missing project fields: {missing}")
        rows = []
        for raw in reader:
            row = {field: clean(raw.get(field)) for field in engine.PROJECT_FIELDS}
            year = int(row["approval_year"]) if row["approval_year"].isdigit() else None
            if year is not None and 2017 <= year <= 2020 and row["application_code_1"].upper().startswith("B05"):
                row["application_code_1"] = "B05LEGACY|" + row["application_code_1"]
            rows.append(row)
    with gzip.open(destination, "wt", encoding="utf-8-sig", newline="") as output_handle:
        writer = csv.DictWriter(output_handle, fieldnames=engine.PROJECT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def fix_observations(path: Path) -> None:
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        rows = []
        for row in reader:
            row["discipline_root"] = "B"
            value = clean(row.get("application_code_1"))
            if value.upper().startswith("B05LEGACY|"):
                row["application_code_1"] = value.split("|", 1)[1]
            rows.append(row)
    with gzip.open(path, "wt", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def rename_outputs(output: Path) -> None:
    for path in sorted(output.iterdir()):
        if path.name.startswith("nsfc_e_"):
            path.rename(output / path.name.replace("nsfc_e_", "nsfc_b_", 1))
    report = output / "NSFC_E_NUMBER_RULE_REPORT.md"
    if report.exists():
        report.rename(output / "NSFC_B_NUMBER_RULE_REPORT.md")


def postprocess(output: Path) -> None:
    observations = output / "nsfc_b_number_parsed_observations.csv.gz"
    if observations.exists():
        fix_observations(observations)

    quality_path = output / "nsfc_b_number_rule_quality.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality["discipline_root"] = "B"
    quality["historical_scope_rule"] = (
        "Approval years 2017-2020 with application_code_1 starting B05 are learned "
        "as B05_legacy_material_energy; raw application codes are not overwritten."
    )
    quality_path.write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")

    catalog_path = output / "nsfc_b_number_rule_catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["scope"] = "NSFC B public completed-project approval-number rules"
    catalog["quality"] = quality
    catalog.setdefault("principles", []).append(
        "The 2017-2020 B05 historical scope is versioned separately and must not be forced into the modern B05/B09 split."
    )
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")

    report_path = output / "NSFC_B_NUMBER_RULE_REPORT.md"
    text = report_path.read_text(encoding="utf-8")
    replacements = {
        "NSFC E口": "NSFC B口",
        "E口项目": "B口项目",
        "E01至E13学科分区": "B01至B09学科分区（含B05历史口径）",
        "nsfc_e_": "nsfc_b_",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    historical = (
        "\n## B05历史口径\n\n"
        "2017—2020年申请代码B05仍对应“材料化学与能源化学”。本轮在规律学习层将其标记为"
        "`B05_legacy_material_energy`，不以现行B05/B09定义拆分，也不修改原始申请代码。\n"
    )
    if "## B05历史口径" not in text:
        text = text.rstrip() + "\n" + historical
    report_path.write_text(text, encoding="utf-8")

    (output / "nsfc_b_historical_scope_note.json").write_text(
        json.dumps(
            {
                "scope": "B05_legacy_material_energy",
                "approval_year_start": 2017,
                "approval_year_end": 2020,
                "raw_code_preserved": True,
                "warning": "Do not infer modern B05 or B09 solely from the historical approval number.",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master-csv", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nsfc-b-rule-") as temp_dir:
        prepared = Path(temp_dir) / "nsfc_b_learning_master.csv.gz"
        prepare_learning_input(Path(args.master_csv), prepared)
        original_scope = engine.discipline_scope
        original_argv = sys.argv[:]
        try:
            engine.discipline_scope = b_scope
            sys.argv = [
                "nsfc_e_learn_number_rules.py",
                "--master-csv",
                str(prepared),
                "--output-dir",
                str(output),
            ]
            engine.main()
        finally:
            engine.discipline_scope = original_scope
            sys.argv = original_argv

    rename_outputs(output)
    postprocess(output)
    quality = json.loads((output / "nsfc_b_number_rule_quality.json").read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "status": "success",
                "records": quality.get("records"),
                "segments": quality.get("segment_count"),
                "templates": quality.get("template_rule_count"),
                "transitions": quality.get("transition_count"),
                "output_dir": str(output),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
