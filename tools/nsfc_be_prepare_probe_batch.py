#!/usr/bin/env python3
"""Select a deterministic slice from the NSFC B/E probe queue."""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def truthy(value: object) -> bool:
    return clean(value).lower() in {"1", "true", "yes", "y"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", required=True)
    parser.add_argument(
        "--mode",
        choices=["first", "remaining", "all"],
        default="remaining",
    )
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--metadata-output")
    parser.add_argument("--batch-id", default="")
    args = parser.parse_args()

    if args.offset < 0 or args.limit < 1:
        raise SystemExit("offset must be >=0 and limit must be >=1")

    queue_path = Path(args.queue)
    with queue_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        if "candidate_approval_number" not in fields:
            raise RuntimeError("queue lacks candidate_approval_number")
        rows = [
            {field: clean(row.get(field)) for field in fields}
            for row in reader
            if clean(row.get("candidate_approval_number"))
        ]

    if args.mode == "first":
        eligible = [row for row in rows if truthy(row.get("selected_for_first_batch"))]
    elif args.mode == "remaining":
        eligible = [row for row in rows if not truthy(row.get("selected_for_first_batch"))]
    else:
        eligible = rows

    selected = eligible[args.offset : args.offset + args.limit]
    if not selected:
        raise RuntimeError(
            f"empty batch selection: mode={args.mode}, offset={args.offset}, limit={args.limit}"
        )
    numbers = [row["candidate_approval_number"] for row in selected]
    if len(numbers) != len(set(numbers)):
        raise RuntimeError("selected batch contains duplicate approval numbers")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(selected)

    metadata = {
        "generated_at": utc_now(),
        "batch_id": args.batch_id,
        "source_queue": str(queue_path),
        "mode": args.mode,
        "source_queue_count": len(rows),
        "eligible_count": len(eligible),
        "offset": args.offset,
        "requested_limit": args.limit,
        "selected_count": len(selected),
        "first_approval_number": numbers[0],
        "last_approval_number": numbers[-1],
    }
    metadata_path = (
        Path(args.metadata_output)
        if args.metadata_output
        else output.with_suffix(output.suffix + ".metadata.json")
    )
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"status": "success", **metadata}, ensure_ascii=False))


if __name__ == "__main__":
    main()
