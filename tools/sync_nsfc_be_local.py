#!/usr/bin/env python3
"""Synchronize the released NSFC B base and B/E matrix to local storage."""
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
DEFAULT_B_TAG = "nsfc-b-official-completed-rules-2026-08-26"
DEFAULT_BE_TAG = "nsfc-be-number-matrix-2026-08-26"


def request_json(url: str, token: str | None) -> dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "PolyFT/nsfc-be-local-sync/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError("GitHub API response is not an object")
    return value


def download(url: str, path: Path, token: str | None) -> None:
    headers = {"User-Agent": "PolyFT/nsfc-be-local-sync/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=180
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


def release_assets(repo: str, tag: str, token: str | None) -> dict[str, str]:
    release = request_json(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", token)
    assets = {}
    for item in release.get("assets", []):
        if isinstance(item, dict) and item.get("name") and item.get("browser_download_url"):
            assets[str(item["name"])] = str(item["browser_download_url"])
    return assets


def sync_one(repo: str, tag: str, asset_name: str, dest: Path, token: str | None) -> None:
    checksum_name = asset_name + ".sha256"
    assets = release_assets(repo, tag, token)
    if asset_name not in assets or checksum_name not in assets:
        raise RuntimeError(f"release {tag} lacks {asset_name} or {checksum_name}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nsfc-sync-", dir=str(dest.parent)) as temp_dir:
        temp = Path(temp_dir)
        archive = temp / asset_name
        checksum_file = temp / checksum_name
        download(assets[asset_name], archive, token)
        download(assets[checksum_name], checksum_file, token)
        expected = expected_checksum(checksum_file, asset_name)
        actual = sha256(archive)
        if actual != expected:
            raise RuntimeError(
                f"SHA-256 mismatch for {asset_name}: expected {expected}, got {actual}"
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
                    "repository": repo,
                    "release_tag": tag,
                    "asset": asset_name,
                    "sha256": actual,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--b-tag", default=DEFAULT_B_TAG)
    parser.add_argument("--be-tag", default=DEFAULT_BE_TAG)
    parser.add_argument("--dest", default="data-local/nsfc-be")
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"),
    )
    args = parser.parse_args()

    root = Path(args.dest).resolve()
    root.mkdir(parents=True, exist_ok=True)
    sync_one(
        args.repo,
        args.b_tag,
        "nsfc-b-official-completed-rules.tar.gz",
        root / "b-current",
        args.token,
    )
    sync_one(
        args.repo,
        args.be_tag,
        "nsfc-be-number-matrix.tar.gz",
        root / "matrix-current",
        args.token,
    )
    print(json.dumps({"status": "success", "destination": str(root)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
