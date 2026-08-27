#!/usr/bin/env python3
"""Synchronize one released NSFC B/E probe batch to local storage."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_REPO = "PolyFT/PolyFT"
DEFAULT_TAG = "nsfc-be-probe-batch-001-2026-08-27"
DEFAULT_ASSET = "nsfc-be-probe-batch-001.tar.gz"


def request_json(url: str, token: str | None) -> dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "PolyFT/nsfc-be-probe-local-sync/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=60
    ) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError("GitHub API response is not an object")
    return value


def download(url: str, path: Path, token: str | None) -> None:
    headers = {"User-Agent": "PolyFT/nsfc-be-probe-local-sync/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=300
    ) as response, path.open("wb") as handle:
        shutil.copyfileobj(response, handle, length=1024 * 1024)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_checksum(path: Path, asset_name: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == asset_name:
            return parts[0].lower()
    raise RuntimeError(f"checksum file has no entry for {asset_name}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--tag", default=DEFAULT_TAG)
    parser.add_argument("--asset", default=DEFAULT_ASSET)
    parser.add_argument("--dest", default="data-local/nsfc-be-probes/batch-001")
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"),
    )
    args = parser.parse_args()

    release = request_json(
        f"https://api.github.com/repos/{args.repo}/releases/tags/{args.tag}",
        args.token,
    )
    assets = {
        str(item["name"]): str(item["browser_download_url"])
        for item in release.get("assets", [])
        if isinstance(item, dict)
        and item.get("name")
        and item.get("browser_download_url")
    }
    checksum_name = args.asset + ".sha256"
    if args.asset not in assets or checksum_name not in assets:
        raise RuntimeError(
            f"release {args.tag} lacks {args.asset} or {checksum_name}"
        )

    dest = Path(args.dest).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nsfc-probe-sync-", dir=str(dest.parent)) as temp_dir:
        temp = Path(temp_dir)
        archive = temp / args.asset
        checksum_file = temp / checksum_name
        download(assets[args.asset], archive, args.token)
        download(assets[checksum_name], checksum_file, args.token)
        expected = expected_checksum(checksum_file, args.asset)
        actual = sha256(archive)
        if actual != expected:
            raise RuntimeError(
                f"SHA-256 mismatch for {args.asset}: expected {expected}, got {actual}"
            )
        extracted = temp / "extracted"
        extracted.mkdir()
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(extracted, filter="data")
        children = list(extracted.iterdir())
        source = children[0] if len(children) == 1 and children[0].is_dir() else extracted
        staged = dest.parent / (dest.name + ".staging")
        backup = dest.parent / (dest.name + ".previous")
        shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(source, staged)
        shutil.rmtree(backup, ignore_errors=True)
        if dest.exists():
            dest.rename(backup)
        staged.rename(dest)
        shutil.rmtree(backup, ignore_errors=True)
        (dest / "SYNC_METADATA.json").write_text(
            json.dumps(
                {
                    "repository": args.repo,
                    "release_tag": args.tag,
                    "asset": args.asset,
                    "sha256": actual,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    print(json.dumps({"status": "success", "destination": str(dest)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
