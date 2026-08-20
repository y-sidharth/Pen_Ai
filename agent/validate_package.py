#!/usr/bin/env python3
"""Validate that the portable Jarvis package is safely configured to launch."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


BASE_DIR = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true", help="Treat a missing model or inference binary as an error.")
    args = parser.parse_args()
    errors: list[str] = []
    warnings: list[str] = []

    required = ["run_agent.py", "security.py", "command_policy.py", "config.json", "prompts/system.txt"]
    for relative in required:
        if not (BASE_DIR / relative).is_file():
            errors.append(f"Missing required file: {relative}")

    try:
        config = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"Invalid config.json: {exc}")
        config = {}
    if "password" in config:
        errors.append("config.json contains a legacy plaintext password")
    for key in ("model_path", "llama_bin"):
        value = str(config.get(key, ""))
        if not value:
            errors.append(f"config.json is missing {key}")
        elif Path(value).is_absolute():
            errors.append(f"{key} must be relative to the agent directory")

    source = (BASE_DIR / "run_agent.py").read_text(encoding="utf-8") if (BASE_DIR / "run_agent.py").exists() else ""
    if "shell=True" in source:
        errors.append("run_agent.py still contains shell=True")
    if "validate_command" not in source:
        errors.append("run_agent.py does not use the command policy")

    for key, label in (("llama_bin", "inference binary"), ("model_path", "GGUF model")):
        candidate = BASE_DIR / str(config.get(key, ""))
        if not candidate.exists():
            message = f"Missing {label}: {candidate}"
            (errors if args.strict else warnings).append(message)

    for message in warnings:
        print(f"WARNING: {message}")
    for message in errors:
        print(f"ERROR: {message}")
    if errors:
        return 1
    print("Package validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
