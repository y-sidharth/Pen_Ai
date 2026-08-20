#!/usr/bin/env python3
"""Explicit, integrity-checked GGUF downloader.

Example:
  python download_model.py owner/repository model.Q4_K_M.gguf --sha256 <64-hex-digest>
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import sys

import requests


BASE_DIR = Path(__file__).resolve().parent
MODELS_DIR = BASE_DIR / "models"
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
FILENAME = re.compile(r"^[A-Za-z0-9_. -]+\.gguf$", re.IGNORECASE)
SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, destination: Path, token: str | None) -> None:
    partial = destination.with_suffix(destination.suffix + ".part")
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    offset = partial.stat().st_size if partial.exists() else 0
    if offset:
        headers["Range"] = f"bytes={offset}-"
    with requests.get(url, headers=headers, stream=True, timeout=60) as response:
        if offset and response.status_code != 206:
            partial.unlink(missing_ok=True)
            return download(url, destination, token)
        response.raise_for_status()
        mode = "ab" if offset else "wb"
        total = response.headers.get("Content-Length")
        total_bytes = offset + int(total) if total and total.isdigit() else None
        with partial.open(mode) as stream:
            downloaded = offset
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    stream.write(chunk)
                    downloaded += len(chunk)
                    if total_bytes:
                        print(f"Downloaded {downloaded / 1024 / 1024:.1f} / {total_bytes / 1024 / 1024:.1f} MiB", end="\r")
    print()
    os.replace(partial, destination)


def main() -> int:
    parser = argparse.ArgumentParser(description="Download a confirmed GGUF file from Hugging Face.")
    parser.add_argument("repository", help="Hugging Face repository, e.g. owner/repository")
    parser.add_argument("filename", help="Exact .gguf filename in that repository")
    parser.add_argument("--sha256", required=True, help="Expected 64-character SHA-256 digest")
    parser.add_argument("--output", help="Output filename under the local models directory")
    args = parser.parse_args()

    if not REPOSITORY.fullmatch(args.repository) or not FILENAME.fullmatch(args.filename):
        parser.error("repository or filename contains unsupported characters")
    if not SHA256.fullmatch(args.sha256):
        parser.error("--sha256 must be a 64-character hexadecimal digest")
    output_name = args.output or args.filename
    if not FILENAME.fullmatch(output_name):
        parser.error("--output must be a .gguf filename without a path")

    destination = MODELS_DIR / output_name
    MODELS_DIR.mkdir(exist_ok=True)
    url = f"https://huggingface.co/{args.repository}/resolve/main/{args.filename}"
    print("Network access is required for this operation.")
    print(f"Repository: {args.repository}\nFile: {args.filename}\nDestination: {destination}\nURL: {url}")
    if input("Type YES to download this model: ").strip() != "YES":
        print("Download cancelled.")
        return 1

    download(url, destination, os.environ.get("HF_TOKEN"))
    actual = sha256_file(destination)
    if actual.lower() != args.sha256.lower():
        destination.unlink(missing_ok=True)
        print("Checksum verification failed; the downloaded file was removed.", file=sys.stderr)
        return 2
    print(f"Downloaded and verified: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
