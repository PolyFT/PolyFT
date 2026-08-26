#!/usr/bin/env python3
"""Synchronize the latest NSFC E dataset release to a local directory.

This script is cross-platform and requires only Python 3.  It downloads the
GitHub Release asset, verifies the published SHA-256 checksum, extracts to a
staging directory, and atomically replaces the local `current` directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_REPO = "PolyFT/PolyFT"
DEFAULT_TAG = "nsfc-e-official-completed-2026-08-26"
DEFAULT_ASSET = "nsfc-e-official-completed-base.tar.gz"


def request_json(url: str, token: str | None = None) -> dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "PolyFT/nsfc-e-local-sync/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError("GitHub API response is not an object")
    return value


def download(url: str, path: Path, token: str | None = None) -> None:
    headers = {"User-Agent": "PolyFT/nsfc-e-local-sync/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=120) as response, path.open("wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_checksum(path: Path, expected_name: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == expected_name:
            return parts[0].lower()
    raise RuntimeError(f"checksum file has no entry for {expected_name}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--tag", default=DEFAULT_TAG)
    parser.add_argument("--asset", default=DEFAULT_ASSET)
    parser.add_argument("--dest", default="data/nsfc-e")
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"),
        help="Optional token; only needed for private repositories or rate limits",
    )
    args = parser.parse_args()

    api_url = f"https://api.github.com/repos/{args.repo}/releases/tags/{args.tag}"
    try:
        release = request_json(api_url, args.token)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"cannot read release {args.tag}: HTTP {exc.code}") from exc

    assets = {
        item.get("name"): item.get("browser_download_url")
        for item in release.get("assets", [])
        if isinstance(item, dict)
    }
    checksum_name = args.asset + ".sha256"
    if args.asset not in assets or checksum_name not in assets:
        raise SystemExit(
            f"release is missing {args.asset!r} or {checksum_name!r}; available={sorted(assets)}"
        )

    destination = Path(args.dest).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nsfc-e-sync-") as temp_name:
        temp = Path(temp_name)
        archive = temp / args.asset
        checksum_file = temp / checksum_name
        download(str(assets[args.asset]), archive, args.token)
        download(str(assets[checksum_name]), checksum_file, args.token)

        expected = parse_checksum(checksum_file, args.asset)
        actual = sha256(archive)
        if actual != expected:
            raise SystemExit(
                f"SHA-256 mismatch for {args.asset}: expected {expected}, got {actual}"
            )

        extracted = temp / "extracted"
        extracted.mkdir()
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            for member in members:
                target = (extracted / member.name).resolve()
                if extracted.resolve() not in target.parents and target != extracted.resolve():
                    raise SystemExit(f"unsafe archive member: {member.name}")
            tar.extractall(extracted)

        children = [path for path in extracted.iterdir()]
        source = children[0] if len(children) == 1 and children[0].is_dir() else extracted
        staged = destination / ".current.new"
        current = destination / "current"
        previous = destination / ".current.previous"
        if staged.exists():
            shutil.rmtree(staged)
        shutil.copytree(source, staged)
        if previous.exists():
            shutil.rmtree(previous)
        if current.exists():
            current.rename(previous)
        staged.rename(current)
        if previous.exists():
            shutil.rmtree(previous)

        sync_state = {
            "repository": args.repo,
            "release_tag": args.tag,
            "asset": args.asset,
            "sha256": actual,
            "release_published_at": release.get("published_at"),
            "local_path": str(current),
        }
        (destination / "sync_state.json").write_text(
            json.dumps(sync_state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(json.dumps(sync_state, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
