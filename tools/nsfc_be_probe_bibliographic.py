#!/usr/bin/env python3
"""Probe one NSFC B/E approval-number batch with public bibliographic APIs.

Input numbers must already exist in the repository's probe queue. This script
never creates candidate numbers and never infers PI, institution, title,
project type, or application code from number patterns.

Sources:
- Crossref works filtered by award.number and award.funder;
- OpenAlex Awards and Works filtered by the NSFC funder and award number.

A successful zero-result query means only "not found in this source at this
retrieval time". It does not prove that an approval number was unused.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import random
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

CROSSREF_URL = "https://api.crossref.org/works"
OPENALEX_AWARDS_URL = "https://api.openalex.org/awards"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"
NSFC_FUNDER_SUFFIX = "501100001809"
NSFC_FUNDER_DOI = "10.13039/501100001809"
OPENALEX_NSFC_FUNDER_ID = "F4320332161"
RETRYABLE = {408, 425, 429, 500, 502, 503, 504}

EVIDENCE_FIELDS = [
    "candidate_approval_number",
    "source",
    "evidence_kind",
    "source_record_id",
    "doi",
    "title",
    "publication_year",
    "container_title",
    "authors",
    "award_id_raw",
    "funder_name",
    "funder_id",
    "award_match",
    "nsfc_funder_match",
    "openalex_award_id",
    "project_title_candidate",
    "lead_investigator_candidate",
    "institution_candidate",
    "landing_page_url",
    "provenance",
    "evidence_level",
    "retrieved_at",
    "query_url",
    "http_status",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalize_award(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", clean(value).upper())


def normalize_doi(value: Any) -> str:
    text = clean(value).lower()
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text)


def last_id(value: Any) -> str:
    return clean(value).rstrip("/").rsplit("/", 1)[-1]


def bool_value(value: Any) -> bool:
    return clean(value).lower() in {"1", "true", "yes", "y"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


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


def is_nsfc_name(value: Any) -> bool:
    text = clean(value).lower()
    return (
        "national natural science foundation of china" in text
        or "国家自然科学基金" in text
        or text == "nsfc"
    )


def is_nsfc_crossref_funder(funder: dict[str, Any]) -> bool:
    doi = normalize_doi(funder.get("DOI"))
    return doi == NSFC_FUNDER_DOI.lower() or is_nsfc_name(funder.get("name"))


def is_nsfc_openalex_funder(funder: Any, funder_id: Any = "") -> bool:
    if last_id(funder_id) == OPENALEX_NSFC_FUNDER_ID:
        return True
    if not isinstance(funder, dict):
        return False
    return (
        last_id(funder.get("id")) == OPENALEX_NSFC_FUNDER_ID
        or normalize_doi(funder.get("doi")) == NSFC_FUNDER_DOI.lower()
        or is_nsfc_name(funder.get("display_name"))
    )


def crossref_year(item: dict[str, Any]) -> str:
    for key in ("published-print", "published-online", "published", "issued", "created"):
        value = item.get(key)
        if not isinstance(value, dict):
            continue
        parts = value.get("date-parts")
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            return clean(parts[0][0])
        timestamp = clean(value.get("date-time"))
        if timestamp:
            return timestamp[:4]
    return ""


def crossref_authors(item: dict[str, Any]) -> str:
    names: list[str] = []
    for author in item.get("author") or []:
        if not isinstance(author, dict):
            continue
        name = " ".join(
            part for part in (clean(author.get("given")), clean(author.get("family"))) if part
        )
        if name:
            names.append(name)
    return "; ".join(names)


def openalex_authors(item: dict[str, Any]) -> str:
    names: list[str] = []
    for authorship in item.get("authorships") or []:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author") or {}
        if isinstance(author, dict) and clean(author.get("display_name")):
            names.append(clean(author.get("display_name")))
    return "; ".join(names)


def lead_investigator_name(person: Any) -> str:
    if not isinstance(person, dict):
        return ""
    display = clean(person.get("display_name"))
    if display:
        return display
    return " ".join(
        part
        for part in (clean(person.get("given_name")), clean(person.get("family_name")))
        if part
    )


def award_institutions(value: Any) -> str:
    names: list[str] = []
    for institution in value or []:
        if not isinstance(institution, dict):
            continue
        name = clean(institution.get("display_name"))
        if name:
            names.append(name)
    return "; ".join(dict.fromkeys(names))


class JsonClient:
    def __init__(self, raw_path: Path, delay: float, retries: int) -> None:
        self.session = requests.Session()
        self.raw_handle = gzip.open(raw_path, "wt", encoding="utf-8")
        self.delay = delay
        self.retries = retries
        self.request_count = 0
        self.status_counts: Counter[str] = Counter()

    def close(self) -> None:
        self.raw_handle.close()
        self.session.close()

    def get(
        self,
        source: str,
        url: str,
        params: dict[str, Any],
        context: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "PolyFT/nsfc-be-probe/1.0 (+https://github.com/PolyFT/PolyFT)",
        }
        if headers:
            request_headers.update(headers)
        last_error = ""
        last_status: int | None = None
        for attempt in range(1, self.retries + 1):
            self.request_count += 1
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=request_headers,
                    timeout=90,
                )
                last_status = response.status_code
                self.status_counts[f"{source}:{last_status}"] += 1
                if last_status in RETRYABLE:
                    retry_after = clean(response.headers.get("Retry-After"))
                    try:
                        wait = float(retry_after) if retry_after else 0.0
                    except ValueError:
                        wait = 0.0
                    if not wait:
                        wait = min(90.0, 1.5 * (2 ** (attempt - 1))) + random.uniform(0, 0.5)
                    last_error = f"HTTP {last_status}"
                    if attempt < self.retries:
                        time.sleep(wait)
                        continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("JSON response is not an object")
                envelope = {
                    "retrieved_at": utc_now(),
                    "source": source,
                    "context": context,
                    "request_url": response.url,
                    "http_status": last_status,
                    "attempt": attempt,
                    "response": payload,
                }
                self.raw_handle.write(json.dumps(envelope, ensure_ascii=False) + "\n")
                time.sleep(self.delay)
                return payload, {
                    "source": source,
                    "http_status": last_status,
                    "request_url": response.url,
                    "attempts": attempt,
                    "error": "",
                }
            except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.retries:
                    time.sleep(min(90.0, 1.5 * (2 ** (attempt - 1))) + random.uniform(0, 0.5))
                    continue
        self.raw_handle.write(
            json.dumps(
                {
                    "retrieved_at": utc_now(),
                    "source": source,
                    "context": context,
                    "params": params,
                    "http_status": last_status,
                    "error": last_error,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        return None, {
            "source": source,
            "http_status": last_status if last_status is not None else "",
            "request_url": url,
            "attempts": self.retries,
            "error": last_error,
        }


def read_queue(path: Path, selected_only: bool, limit: int) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"candidate_approval_number", "discipline_root"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(f"probe queue missing fields: {sorted(missing)}")
        rows: list[dict[str, str]] = []
        for row in reader:
            normalized = {key: clean(value) for key, value in row.items()}
            if selected_only and not bool_value(normalized.get("selected_for_first_batch")):
                continue
            if not normalized.get("candidate_approval_number"):
                continue
            rows.append(normalized)
            if limit and len(rows) >= limit:
                break
    numbers = [row["candidate_approval_number"] for row in rows]
    if len(numbers) != len(set(numbers)):
        raise RuntimeError("selected probe queue contains duplicate approval numbers")
    return rows


def append_crossref_evidence(
    number: str,
    item: dict[str, Any],
    funder: dict[str, Any],
    award_raw: str,
    meta: dict[str, Any],
    kind: str,
    evidence: list[dict[str, Any]],
) -> bool:
    nsfc_match = is_nsfc_crossref_funder(funder)
    evidence.append(
        {
            "candidate_approval_number": number,
            "source": "crossref",
            "evidence_kind": kind,
            "source_record_id": clean(item.get("DOI")),
            "doi": clean(item.get("DOI")),
            "title": clean((item.get("title") or [""])[0]),
            "publication_year": crossref_year(item),
            "container_title": clean((item.get("container-title") or [""])[0]),
            "authors": crossref_authors(item),
            "award_id_raw": award_raw,
            "funder_name": clean(funder.get("name")),
            "funder_id": clean(funder.get("DOI")),
            "award_match": "true",
            "nsfc_funder_match": str(nsfc_match).lower(),
            "openalex_award_id": "",
            "project_title_candidate": "",
            "lead_investigator_candidate": "",
            "institution_candidate": "",
            "landing_page_url": clean(item.get("URL")),
            "provenance": "publisher_deposited_crossref_metadata",
            "evidence_level": "C" if nsfc_match else "D",
            "retrieved_at": utc_now(),
            "query_url": meta["request_url"],
            "http_status": meta["http_status"],
        }
    )
    return nsfc_match


def crossref_probe(
    client: JsonClient,
    number: str,
    evidence: list[dict[str, Any]],
    mailto: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "strict_http_status": "",
        "strict_total_results": 0,
        "strict_exact_nsfc_work_count": 0,
        "fallback_http_status": "",
        "fallback_total_results": 0,
        "fallback_exact_award_count": 0,
        "error": "",
    }
    params: dict[str, Any] = {
        "filter": f"award.number:{number},award.funder:{NSFC_FUNDER_SUFFIX}",
        "rows": 1000,
    }
    if mailto:
        params["mailto"] = mailto
    payload, meta = client.get(
        "crossref_strict",
        CROSSREF_URL,
        params,
        {"approval_number": number, "mode": "award_and_nsfc_funder"},
    )
    result["strict_http_status"] = meta["http_status"]
    if payload is None:
        result["error"] = meta["error"]
        return result
    message = payload.get("message") or {}
    result["strict_total_results"] = int(message.get("total-results") or 0)
    strict_exact = 0
    for item in message.get("items") or []:
        if not isinstance(item, dict):
            continue
        for funder in item.get("funder") or []:
            if not isinstance(funder, dict):
                continue
            for award in funder.get("award") or []:
                if normalize_award(award) != normalize_award(number):
                    continue
                if append_crossref_evidence(
                    number,
                    item,
                    funder,
                    clean(award),
                    meta,
                    "work_funding_metadata",
                    evidence,
                ):
                    strict_exact += 1
    result["strict_exact_nsfc_work_count"] = strict_exact

    if strict_exact == 0:
        fallback_params: dict[str, Any] = {
            "filter": f"award.number:{number}",
            "rows": 1000,
        }
        if mailto:
            fallback_params["mailto"] = mailto
        fallback, fallback_meta = client.get(
            "crossref_award_only",
            CROSSREF_URL,
            fallback_params,
            {"approval_number": number, "mode": "award_only"},
        )
        result["fallback_http_status"] = fallback_meta["http_status"]
        if fallback is None:
            result["error"] = fallback_meta["error"]
            return result
        fallback_message = fallback.get("message") or {}
        result["fallback_total_results"] = int(fallback_message.get("total-results") or 0)
        exact = 0
        for item in fallback_message.get("items") or []:
            if not isinstance(item, dict):
                continue
            for funder in item.get("funder") or []:
                if not isinstance(funder, dict):
                    continue
                for award in funder.get("award") or []:
                    if normalize_award(award) != normalize_award(number):
                        continue
                    exact += 1
                    append_crossref_evidence(
                        number,
                        item,
                        funder,
                        clean(award),
                        fallback_meta,
                        "work_funding_metadata_award_only_fallback",
                        evidence,
                    )
        result["fallback_exact_award_count"] = exact
    return result


def paged_openalex(
    client: JsonClient,
    source: str,
    url: str,
    filter_value: str,
    context: dict[str, Any],
    api_key: str,
    max_pages: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = "*"
    pages = 0
    total_count = 0
    statuses: list[str] = []
    error = ""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    while cursor and pages < max_pages:
        params: dict[str, Any] = {
            "filter": filter_value,
            "per_page": 100,
            "cursor": cursor,
        }
        payload, meta = client.get(
            source,
            url,
            params,
            {**context, "page": pages + 1},
            headers=headers,
        )
        statuses.append(clean(meta["http_status"]))
        if payload is None:
            error = meta["error"]
            break
        if pages == 0:
            total_count = int((payload.get("meta") or {}).get("count") or 0)
        page_items = [item for item in payload.get("results") or [] if isinstance(item, dict)]
        items.extend(page_items)
        next_cursor = clean((payload.get("meta") or {}).get("next_cursor"))
        pages += 1
        if not page_items or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return items, {
        "http_statuses": ";".join(statuses),
        "total_count": total_count,
        "retrieved_count": len(items),
        "pages": pages,
        "truncated": pages >= max_pages and len(items) < total_count,
        "error": error,
    }


def openalex_probe(
    client: JsonClient,
    numbers: list[str],
    evidence: list[dict[str, Any]],
    api_key: str,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    per_number = {
        number: {
            "award_count": 0,
            "work_count": 0,
            "award_http_statuses": "",
            "work_http_statuses": "",
            "error": "",
        }
        for number in numbers
    }
    award_candidates: list[dict[str, Any]] = []

    for batch_index, batch in enumerate(chunks(numbers, 100), start=1):
        award_filter = (
            f"funder.id:{OPENALEX_NSFC_FUNDER_ID},"
            f"funder_award_id:{'|'.join(batch)}"
        )
        awards, award_meta = paged_openalex(
            client,
            "openalex_awards",
            OPENALEX_AWARDS_URL,
            award_filter,
            {"batch_index": batch_index, "approval_numbers": batch},
            api_key,
            max_pages=10,
        )
        for number in batch:
            per_number[number]["award_http_statuses"] = award_meta["http_statuses"]
            if award_meta["error"]:
                per_number[number]["error"] = award_meta["error"]
        batch_normalized = {normalize_award(number): number for number in batch}
        for award in awards:
            award_raw = clean(award.get("funder_award_id"))
            number = batch_normalized.get(normalize_award(award_raw))
            if not number:
                continue
            funder = award.get("funder") or {}
            nsfc_match = is_nsfc_openalex_funder(funder)
            if not nsfc_match:
                continue
            per_number[number]["award_count"] += 1
            lead = lead_investigator_name(award.get("lead_investigator"))
            institutions = award_institutions(award.get("institution_awarded"))
            award_id = last_id(award.get("id"))
            candidate = {
                "candidate_approval_number": number,
                "openalex_award_id": award_id,
                "funder_award_id": award_raw,
                "project_title_candidate": clean(award.get("display_name")),
                "lead_investigator_candidate": lead,
                "institution_candidate": institutions,
                "amount": clean(award.get("amount")),
                "currency": clean(award.get("currency")),
                "start_date": clean(award.get("start_date")),
                "end_date": clean(award.get("end_date")),
                "funded_outputs_count": clean(award.get("funded_outputs_count")),
                "provenance": clean(award.get("provenance")),
                "landing_page_url": clean(award.get("landing_page_url")),
                "status": "aggregator_candidate_only",
                "warning": "OpenAlex award data is evidence, not official NSFC confirmation",
            }
            award_candidates.append(candidate)
            evidence.append(
                {
                    "candidate_approval_number": number,
                    "source": "openalex_awards",
                    "evidence_kind": "award_entity",
                    "source_record_id": award_id,
                    "doi": clean(award.get("doi")),
                    "title": clean(award.get("display_name")),
                    "publication_year": clean(award.get("start_year")),
                    "container_title": "",
                    "authors": "",
                    "award_id_raw": award_raw,
                    "funder_name": clean(funder.get("display_name")) if isinstance(funder, dict) else "",
                    "funder_id": last_id(funder.get("id")) if isinstance(funder, dict) else "",
                    "award_match": "true",
                    "nsfc_funder_match": "true",
                    "openalex_award_id": award_id,
                    "project_title_candidate": clean(award.get("display_name")),
                    "lead_investigator_candidate": lead,
                    "institution_candidate": institutions,
                    "landing_page_url": clean(award.get("landing_page_url")),
                    "provenance": clean(award.get("provenance")),
                    "evidence_level": "C",
                    "retrieved_at": utc_now(),
                    "query_url": OPENALEX_AWARDS_URL,
                    "http_status": award_meta["http_statuses"],
                }
            )

        works_filter = (
            f"awards.funder_id:{OPENALEX_NSFC_FUNDER_ID},"
            f"awards.funder_award_id:{'|'.join(batch)}"
        )
        works, works_meta = paged_openalex(
            client,
            "openalex_works",
            OPENALEX_WORKS_URL,
            works_filter,
            {"batch_index": batch_index, "approval_numbers": batch},
            api_key,
            max_pages=100,
        )
        for number in batch:
            per_number[number]["work_http_statuses"] = works_meta["http_statuses"]
            if works_meta["error"]:
                per_number[number]["error"] = "; ".join(
                    part for part in (per_number[number]["error"], works_meta["error"]) if part
                )
        seen_work_award: set[tuple[str, str]] = set()
        for work in works:
            work_id = last_id(work.get("id"))
            for award in work.get("awards") or []:
                if not isinstance(award, dict):
                    continue
                award_raw = clean(award.get("funder_award_id"))
                number = batch_normalized.get(normalize_award(award_raw))
                if not number:
                    continue
                if not is_nsfc_openalex_funder(award.get("funder"), award.get("funder_id")):
                    continue
                dedupe_key = (work_id, number)
                if dedupe_key in seen_work_award:
                    continue
                seen_work_award.add(dedupe_key)
                per_number[number]["work_count"] += 1
                primary = work.get("primary_location") or {}
                source = primary.get("source") or {} if isinstance(primary, dict) else {}
                evidence.append(
                    {
                        "candidate_approval_number": number,
                        "source": "openalex_works",
                        "evidence_kind": "work_award_link",
                        "source_record_id": work_id,
                        "doi": normalize_doi(work.get("doi")),
                        "title": clean(work.get("display_name")),
                        "publication_year": clean(work.get("publication_year")),
                        "container_title": clean(source.get("display_name")) if isinstance(source, dict) else "",
                        "authors": openalex_authors(work),
                        "award_id_raw": award_raw,
                        "funder_name": clean((award.get("funder") or {}).get("display_name"))
                        if isinstance(award.get("funder"), dict)
                        else "",
                        "funder_id": last_id(award.get("funder_id")),
                        "award_match": "true",
                        "nsfc_funder_match": "true",
                        "openalex_award_id": last_id(award.get("id")),
                        "project_title_candidate": "",
                        "lead_investigator_candidate": "",
                        "institution_candidate": "",
                        "landing_page_url": clean(primary.get("landing_page_url"))
                        if isinstance(primary, dict)
                        else "",
                        "provenance": "openalex_work_awards",
                        "evidence_level": "C",
                        "retrieved_at": utc_now(),
                        "query_url": OPENALEX_WORKS_URL,
                        "http_status": works_meta["http_statuses"],
                    }
                )
    return per_number, award_candidates


def dedupe_evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            clean(row.get("candidate_approval_number")),
            clean(row.get("source")),
            clean(row.get("source_record_id")),
            normalize_award(row.get("award_id_raw")),
            clean(row.get("evidence_kind")),
        )
        selected[key] = row
    return [selected[key] for key in sorted(selected)]


def choose_award_candidate(
    number: str, award_candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    candidates = [
        row for row in award_candidates if row["candidate_approval_number"] == number
    ]
    if not candidates:
        return {}
    candidates.sort(
        key=lambda row: (
            bool(clean(row.get("project_title_candidate"))),
            bool(clean(row.get("lead_investigator_candidate"))),
            bool(clean(row.get("institution_candidate"))),
            int(clean(row.get("funded_outputs_count")) or 0),
        ),
        reverse=True,
    )
    return candidates[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--selected-only", action="store_true")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--delay", type=float, default=0.12)
    parser.add_argument("--retries", type=int, default=7)
    args = parser.parse_args()

    queue_path = Path(args.queue)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    queue_rows = read_queue(queue_path, args.selected_only, args.limit)
    if not queue_rows:
        raise RuntimeError("probe queue selection is empty")
    numbers = [row["candidate_approval_number"] for row in queue_rows]
    queue_by_number = {row["candidate_approval_number"]: row for row in queue_rows}

    raw_path = output / "probe_raw_responses.jsonl.gz"
    client = JsonClient(raw_path, delay=args.delay, retries=args.retries)
    evidence: list[dict[str, Any]] = []
    crossref_results: dict[str, dict[str, Any]] = {}
    try:
        mailto = clean(os.getenv("CROSSREF_MAILTO"))
        for index, number in enumerate(numbers, start=1):
            crossref_results[number] = crossref_probe(
                client, number, evidence, mailto=mailto
            )
            print(
                json.dumps(
                    {
                        "stage": "crossref",
                        "index": index,
                        "total": len(numbers),
                        "approval_number": number,
                        "strict_total": crossref_results[number]["strict_total_results"],
                        "strict_exact_nsfc": crossref_results[number][
                            "strict_exact_nsfc_work_count"
                        ],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        openalex_results, award_candidates = openalex_probe(
            client,
            numbers,
            evidence,
            api_key=clean(os.getenv("OPENALEX_API_KEY")),
        )
    finally:
        client.close()

    evidence = dedupe_evidence(evidence)
    evidence_by_number: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evidence:
        evidence_by_number[row["candidate_approval_number"]].append(row)

    status_rows: list[dict[str, Any]] = []
    confirmed_rows: list[dict[str, Any]] = []
    followup_rows: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    source_confirmation_counts: Counter[str] = Counter()

    for number in numbers:
        queue = queue_by_number[number]
        crossref = crossref_results[number]
        openalex = openalex_results[number]
        number_evidence = evidence_by_number.get(number, [])
        confirmed_sources = sorted(
            {
                row["source"]
                for row in number_evidence
                if clean(row.get("award_match")) == "true"
                and clean(row.get("nsfc_funder_match")) == "true"
            }
        )
        for source in confirmed_sources:
            source_confirmation_counts[source] += 1
        award_only = any(
            clean(row.get("award_match")) == "true"
            and clean(row.get("nsfc_funder_match")) != "true"
            for row in number_evidence
        )
        source_errors = [crossref.get("error", ""), openalex.get("error", "")]
        source_errors = [value for value in source_errors if clean(value)]

        if len(confirmed_sources) >= 2:
            existence_status = "confirmed_multi_channel"
            evidence_level = "C_multi_channel"
        elif "openalex_awards" in confirmed_sources:
            existence_status = "confirmed_openalex_award"
            evidence_level = "C_openalex_award"
        elif confirmed_sources:
            existence_status = "confirmed_bibliographic"
            evidence_level = "C_bibliographic"
        elif award_only:
            existence_status = "award_number_only"
            evidence_level = "D_award_only"
        elif source_errors:
            existence_status = "inconclusive_source_error"
            evidence_level = "unresolved"
        else:
            existence_status = "no_match_all_sources"
            evidence_level = "E_negative_probe_only"
        status_counts[existence_status] += 1

        best_award = choose_award_candidate(number, award_candidates)
        publication_evidence_count = sum(
            1
            for row in number_evidence
            if row["evidence_kind"] in {
                "work_funding_metadata",
                "work_funding_metadata_award_only_fallback",
                "work_award_link",
            }
            and row["nsfc_funder_match"] == "true"
        )
        requires_official_upgrade = existence_status.startswith("confirmed")
        requires_web_review = existence_status in {
            "no_match_all_sources",
            "inconclusive_source_error",
            "award_number_only",
        } or requires_official_upgrade
        next_action = (
            "official_or_institutional_confirmation"
            if requires_official_upgrade
            else "exact_web_and_institution_search"
            if existence_status in {"no_match_all_sources", "award_number_only"}
            else "retry_failed_source_then_web_search"
        )

        status_row = {
            **queue,
            "crossref_strict_http_status": crossref["strict_http_status"],
            "crossref_strict_total_results": crossref["strict_total_results"],
            "crossref_strict_exact_nsfc_work_count": crossref[
                "strict_exact_nsfc_work_count"
            ],
            "crossref_fallback_http_status": crossref["fallback_http_status"],
            "crossref_fallback_total_results": crossref["fallback_total_results"],
            "crossref_fallback_exact_award_count": crossref[
                "fallback_exact_award_count"
            ],
            "openalex_award_count": openalex["award_count"],
            "openalex_work_count": openalex["work_count"],
            "openalex_award_http_statuses": openalex["award_http_statuses"],
            "openalex_work_http_statuses": openalex["work_http_statuses"],
            "confirmed_sources": ";".join(confirmed_sources),
            "publication_evidence_count": publication_evidence_count,
            "existence_status": existence_status,
            "evidence_level": evidence_level,
            "project_title_candidate": clean(best_award.get("project_title_candidate")),
            "lead_investigator_candidate": clean(
                best_award.get("lead_investigator_candidate")
            ),
            "institution_candidate": clean(best_award.get("institution_candidate")),
            "candidate_identity_source": "openalex_awards" if best_award else "",
            "official_confirmation_status": "not_checked",
            "requires_web_review": str(requires_web_review).lower(),
            "next_action": next_action,
            "source_errors": "; ".join(source_errors),
            "retrieved_at": utc_now(),
            "warning": "bibliographic/aggregator evidence only; API channels may share provenance; no PI or project fact inferred",
        }
        status_rows.append(status_row)

        if existence_status.startswith("confirmed"):
            confirmed_rows.append(
                {
                    "candidate_approval_number": number,
                    "discipline_root": queue.get("discipline_root", ""),
                    "approval_year": queue.get("approval_year", ""),
                    "project_type_contexts": queue.get("project_type_contexts", ""),
                    "discipline_scope_contexts": queue.get(
                        "discipline_scope_contexts", ""
                    ),
                    "existence_status": existence_status,
                    "evidence_level": evidence_level,
                    "confirmed_sources": ";".join(confirmed_sources),
                    "publication_evidence_count": publication_evidence_count,
                    "openalex_award_id": clean(best_award.get("openalex_award_id")),
                    "project_title_candidate": clean(
                        best_award.get("project_title_candidate")
                    ),
                    "lead_investigator_candidate": clean(
                        best_award.get("lead_investigator_candidate")
                    ),
                    "institution_candidate": clean(
                        best_award.get("institution_candidate")
                    ),
                    "official_confirmation_status": "not_checked",
                    "next_action": "official_or_institutional_confirmation",
                    "warning": "do not write candidate identity fields into the master project table",
                }
            )

        followup_priority = (
            "P0_official_upgrade"
            if existence_status.startswith("confirmed")
            else f"{queue.get('priority', 'P3')}_no_bibliographic_match"
            if existence_status == "no_match_all_sources"
            else f"{queue.get('priority', 'P3')}_source_error"
            if existence_status == "inconclusive_source_error"
            else f"{queue.get('priority', 'P3')}_award_only"
        )
        followup_rows.append(
            {
                "candidate_approval_number": number,
                "discipline_root": queue.get("discipline_root", ""),
                "approval_year": queue.get("approval_year", ""),
                "project_type_contexts": queue.get("project_type_contexts", ""),
                "discipline_scope_contexts": queue.get(
                    "discipline_scope_contexts", ""
                ),
                "original_priority": queue.get("priority", ""),
                "followup_priority": followup_priority,
                "existence_status": existence_status,
                "confirmed_sources": ";".join(confirmed_sources),
                "project_title_candidate": clean(
                    best_award.get("project_title_candidate")
                ),
                "lead_investigator_candidate": clean(
                    best_award.get("lead_investigator_candidate")
                ),
                "institution_candidate": clean(
                    best_award.get("institution_candidate")
                ),
                "exact_web_query": queue.get(
                    "exact_web_query", f'"{number}"'
                ),
                "funding_web_query": queue.get(
                    "funding_web_query",
                    f'"{number}" "National Natural Science Foundation of China"',
                ),
                "institution_site_query": queue.get(
                    "institution_site_query",
                    f'site:edu.cn OR site:ac.cn "{number}"',
                ),
                "next_action": next_action,
                "official_confirmation_status": "not_checked",
                "warning": "web review must preserve number evidence and PI evidence as separate claims",
            }
        )

    queue_fields = list(queue_rows[0].keys())
    status_fields = queue_fields + [
        "crossref_strict_http_status",
        "crossref_strict_total_results",
        "crossref_strict_exact_nsfc_work_count",
        "crossref_fallback_http_status",
        "crossref_fallback_total_results",
        "crossref_fallback_exact_award_count",
        "openalex_award_count",
        "openalex_work_count",
        "openalex_award_http_statuses",
        "openalex_work_http_statuses",
        "confirmed_sources",
        "publication_evidence_count",
        "existence_status",
        "evidence_level",
        "project_title_candidate",
        "lead_investigator_candidate",
        "institution_candidate",
        "candidate_identity_source",
        "official_confirmation_status",
        "requires_web_review",
        "next_action",
        "source_errors",
        "retrieved_at",
        "warning",
    ]
    confirmed_fields = [
        "candidate_approval_number",
        "discipline_root",
        "approval_year",
        "project_type_contexts",
        "discipline_scope_contexts",
        "existence_status",
        "evidence_level",
        "confirmed_sources",
        "publication_evidence_count",
        "openalex_award_id",
        "project_title_candidate",
        "lead_investigator_candidate",
        "institution_candidate",
        "official_confirmation_status",
        "next_action",
        "warning",
    ]
    followup_fields = [
        "candidate_approval_number",
        "discipline_root",
        "approval_year",
        "project_type_contexts",
        "discipline_scope_contexts",
        "original_priority",
        "followup_priority",
        "existence_status",
        "confirmed_sources",
        "project_title_candidate",
        "lead_investigator_candidate",
        "institution_candidate",
        "exact_web_query",
        "funding_web_query",
        "institution_site_query",
        "next_action",
        "official_confirmation_status",
        "warning",
    ]

    status_path = output / "probe_status.csv"
    evidence_path = output / "bibliographic_evidence.csv.gz"
    confirmed_path = output / "confirmed_project_candidates.csv"
    followup_path = output / "web_followup_queue.csv"
    write_csv(status_path, status_rows, status_fields)
    write_csv_gz(evidence_path, evidence, EVIDENCE_FIELDS)
    write_csv(confirmed_path, confirmed_rows, confirmed_fields)
    write_csv(followup_path, followup_rows, followup_fields)

    root_counts = Counter(row.get("discipline_root", "") for row in queue_rows)
    priority_counts = Counter(row.get("priority", "") for row in queue_rows)
    quality = {
        "generated_at": utc_now(),
        "input_queue": str(queue_path),
        "selected_count": len(queue_rows),
        "discipline_root_counts": dict(root_counts),
        "input_priority_counts": dict(priority_counts),
        "existence_status_counts": dict(status_counts),
        "source_confirmation_counts": dict(source_confirmation_counts),
        "confirmed_candidate_count": len(confirmed_rows),
        "evidence_row_count": len(evidence),
        "followup_queue_count": len(followup_rows),
        "http_status_counts": dict(client.status_counts),
        "request_count": client.request_count,
        "source_scope": ["Crossref", "OpenAlex Awards", "OpenAlex Works"],
        "data_boundary": "bibliographic and aggregator evidence only",
        "negative_result_rule": "zero results do not prove an unused approval number",
        "identity_rule": "lead investigator, institution, and title candidates are not written to the master table without official or institutional confirmation",
    }
    quality_path = output / "probe_quality.json"
    quality_path.write_text(
        json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    report_lines = [
        "# NSFC B/E批准号第一批文献证据探针报告",
        "",
        "## 执行范围",
        "",
        f"- 输入候选批准号：{len(queue_rows):,}。",
        f"- B口：{root_counts.get('B', 0):,}；E口：{root_counts.get('E', 0):,}。",
        "- 自动来源：Crossref、OpenAlex Awards、OpenAlex Works。",
        "- 本轮不执行PI推定，不把OpenAlex候选题名、负责人或单位直接写入项目主表。",
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
        report_lines.append(f"- `{key}`：{status_counts.get(key, 0):,}。")
    report_lines.extend(
        [
            "",
            f"- 文献/奖项证据行：{len(evidence):,}。",
            f"- 进入官方/机构页面升级队列的已确认候选：{len(confirmed_rows):,}。",
            f"- Web后续核查队列：{len(followup_rows):,}。",
            "",
            "## 状态含义",
            "",
            "- `confirmed_multi_channel`：至少两个API证据通道同时确认批准号与NSFC资助关系；OpenAlex可能聚合Crossref等来源，因此不自动视为完全独立证据。",
            "- `confirmed_openalex_award`：OpenAlex Award实体确认批准号与NSFC，但仍需官方或依托单位页面升级。",
            "- `confirmed_bibliographic`：论文资助元数据确认批准号与NSFC。",
            "- `no_match_all_sources`：本次成功查询的自动来源均未命中；不得据此判定空号。",
            "- `inconclusive_source_error`：至少一个来源请求失败，需重试后再判断。",
            "",
            "## 后续",
            "",
            "先对已确认候选进行基金委、高校或研究所页面升级；再对无文献命中的高优先级编号执行精确Web检索。编号存在性与负责人身份继续分开管理。",
        ]
    )
    report_path = output / "PROBE_BATCH_REPORT.md"
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")

    files = [
        raw_path,
        status_path,
        evidence_path,
        confirmed_path,
        followup_path,
        quality_path,
        report_path,
    ]
    manifest = {
        "dataset": "nsfc_be_probe_batch_001",
        "generated_at": utc_now(),
        "input_queue": str(queue_path),
        "selected_count": len(queue_rows),
        "sources": ["crossref", "openalex_awards", "openalex_works"],
        "files": [
            {
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
            for path in files
        ],
    }
    manifest_path = output / "batch_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(
        json.dumps(
            {
                "status": "success",
                "selected": len(queue_rows),
                "confirmed": len(confirmed_rows),
                "evidence_rows": len(evidence),
                "status_counts": dict(status_counts),
                "output_dir": str(output),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
