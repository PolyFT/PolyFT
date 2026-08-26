#!/usr/bin/env python3
"""Independently scan broad-E annual totals from the public completed-project API."""
from __future__ import annotations

import argparse
import base64
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from Crypto.Cipher import DES
from urllib3.exceptions import InsecureRequestWarning

BASE_URL = "https://kd.nsfc.cn"
ENDPOINT = BASE_URL + "/api/baseQuery/completionQueryResultsData"
DES_KEY = b"IFROMC86"
RETRYABLE = {408, 425, 429, 500, 502, 503, 504}
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def unpad(data: bytes) -> bytes:
    if not data or len(data) % 8:
        raise ValueError("invalid DES payload length")
    n = data[-1]
    if n < 1 or n > 8 or data[-n:] != bytes([n]) * n:
        raise ValueError("invalid DES padding")
    return data[:-n]


def decode(text: str) -> dict[str, Any]:
    value = text.strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        encrypted = base64.b64decode(value, validate=True)
        decrypted = DES.new(DES_KEY, DES.MODE_ECB).decrypt(encrypted)
        parsed = json.loads(unpad(decrypted).decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("response is not an object")
    return parsed


def payload(year: int) -> dict[str, Any]:
    return {
        "code": "E",
        "fuzzyKeyword": "",
        "complete": True,
        "isFuzzySearch": False,
        "conclusionYear": str(year),
        "dependUnit": "",
        "keywords": "",
        "pageNum": 0,
        "pageSize": 1,
        "personInCharge": "",
        "projectName": "",
        "projectType": "",
        "subPType": "",
        "psPType": "",
        "ratifyNo": "",
        "ratifyYear": "",
        "queryType": "input",
        "order": "enddate",
        "ordering": "desc",
        "codeScreening": "",
        "dependUnitScreening": "",
        "keywordsScreening": "",
        "projectTypeNameScreening": "",
    }


def query(session: requests.Session, year: int, retries: int) -> int:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": BASE_URL + "/finalProjectInit",
        "Authorization": "Bearer false",
        "User-Agent": "PolyFT/nsfc-e-broad-total-scan/1.0",
    }
    for attempt in range(retries):
        try:
            response = session.post(
                ENDPOINT,
                json=payload(year),
                headers=headers,
                timeout=90,
                verify=False,
            )
            if response.status_code in RETRYABLE:
                raise requests.HTTPError(
                    f"retryable HTTP {response.status_code}", response=response
                )
            response.raise_for_status()
            value = decode(response.text)
            if value.get("code") not in (None, 200, "200"):
                raise RuntimeError(
                    f"NSFC API error {value.get('code')}: {value.get('message')}"
                )
            data = value.get("data")
            if not isinstance(data, dict):
                raise RuntimeError("NSFC response has no data object")
            return int(data.get("itotalRecords") or 0)
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and status not in RETRYABLE:
                raise
            if attempt + 1 >= retries:
                raise RuntimeError(f"broad E scan failed for {year}: {exc}") from exc
            time.sleep(min(30.0, 1.5 * (2**attempt)) + random.uniform(0, 0.35))
    raise RuntimeError("unreachable")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-conclusion-year", type=int, default=1987)
    parser.add_argument("--end-conclusion-year", type=int, default=2024)
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--retries", type=int, default=8)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    session = requests.Session()
    annual: dict[str, int] = {}
    for year in range(args.start_conclusion_year, args.end_conclusion_year + 1):
        annual[str(year)] = query(session, year, args.retries)
        print(
            json.dumps(
                {"query_code": "E", "conclusion_year": year, "total": annual[str(year)]},
                ensure_ascii=False,
            ),
            flush=True,
        )
        time.sleep(args.delay)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "as_of": utc_now(),
                "query_code": "E",
                "scope": "NSFC public completed-project endpoint annual totals",
                "start_conclusion_year": args.start_conclusion_year,
                "end_conclusion_year": args.end_conclusion_year,
                "annual_totals": annual,
                "reported_total_sum": sum(annual.values()),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
