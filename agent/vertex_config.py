#!/usr/bin/env python3
"""
vertex_config.py - Vertex AI / Google Cloud configuration helper.

Centralizes project/region resolution for all Google Cloud calls.
Supports both Vertex AI (GCP) and AI Studio (API key) modes.

Env priority:
  GOOGLE_CLOUD_PROJECT / GCP_PROJECT / GCLOUD_PROJECT
  GOOGLE_CLOUD_LOCATION (default us-central1)
  VERTEX_LOCATION
  GEMINI_API_KEY (AI Studio fallback)
  GEMINI_MODEL (default gemini-2.5-pro, alias gemini-3.5-pro accepted)

Loads agent/.env via python-dotenv if available, else a tiny parser so
keys work even before cloud deps are installed.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

DEFAULT_MODEL = "gemini-2.5-pro"
DEFAULT_LOCATION = "us-central1"

PLACEHOLDER_KEYS = {
    "",
    "your_ai_studio_key_here",
    "changeme",
    "replace_me",
    "none",
    "null",
}

# Map legacy / hackathon alias -> real model
MODEL_ALIASES = {
    "gemini-3.5-pro": "gemini-2.5-pro",
    "gemini-3.5-flash": "gemini-2.5-flash",
    "gemini-3.0-pro": "gemini-2.5-pro",
}


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def load_env(override: bool = False) -> None:
    """Load agent/.env into os.environ. dotenv if present, else parse manually."""
    try:
        from dotenv import load_dotenv  # type: ignore
        if ENV_PATH.exists():
            load_dotenv(dotenv_path=ENV_PATH, override=override)
            return
    except Exception:
        pass
    for key, value in _parse_env_file(ENV_PATH).items():
        if override or key not in os.environ:
            os.environ[key] = value


def write_env_values(updates: dict[str, str]) -> None:
    """Merge keys into agent/.env and apply them to this process immediately."""
    if not updates:
        return
    existing_lines: list[str] = []
    if ENV_PATH.exists():
        try:
            existing_lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
        except OSError:
            existing_lines = []
    seen: set[str] = set()
    new_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                seen.add(key)
                os.environ[key] = updates[key]
                continue
        new_lines.append(line)
    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={value}")
            os.environ[key] = value
    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def key_hint(key: str | None) -> str:
    """Masked hint for UI (never the full secret)."""
    if not key:
        return ""
    if len(key) <= 8:
        return "saved"
    return f"saved · …{key[-4:]}"


load_env(override=False)


def resolve_model(raw: str | None) -> str:
    if not raw:
        return DEFAULT_MODEL
    raw = raw.strip()
    return MODEL_ALIASES.get(raw, raw)


def get_gcp_project(config: dict | None = None) -> str | None:
    cfg_val = (config or {}).get("gcp_project") if config else None
    value = (
        os.getenv("GOOGLE_CLOUD_PROJECT")
        or os.getenv("GCP_PROJECT")
        or os.getenv("GCLOUD_PROJECT")
        or (str(cfg_val).strip() if cfg_val else None)
    )
    if not value or value.strip().lower() in PLACEHOLDER_KEYS:
        return None
    return value.strip()


def get_gcp_location(config: dict | None = None) -> str:
    cfg_val = (config or {}).get("gcp_location") if config else None
    return (
        os.getenv("GOOGLE_CLOUD_LOCATION")
        or os.getenv("VERTEX_LOCATION")
        or (str(cfg_val).strip() if cfg_val else None)
        or DEFAULT_LOCATION
    )


def get_gemini_model(config: dict | None = None) -> str:
    cfg_val = (config or {}).get("gemini_model") if config else None
    raw = os.getenv("GEMINI_MODEL") or (str(cfg_val).strip() if cfg_val else None)
    return resolve_model(raw)


def get_api_key() -> str | None:
    key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    if not key or key.lower() in PLACEHOLDER_KEYS:
        return None
    return key


def is_vertex_mode(config: dict | None = None) -> bool:
    """True if Vertex AI (project-based) should be used."""
    return bool(get_gcp_project(config))


def is_gemini_configured(config: dict | None = None) -> bool:
    return bool(get_gcp_project(config) or get_api_key())


def describe_backend(config: dict | None = None) -> str:
    if is_vertex_mode(config):
        return f"Vertex AI ({get_gcp_project(config)}/{get_gcp_location(config)} / {get_gemini_model(config)})"
    if get_api_key():
        return f"AI Studio (API key / {get_gemini_model(config)})"
    return "not configured (set GOOGLE_CLOUD_PROJECT or GEMINI_API_KEY)"


def ensure_vertex_init(config: dict | None = None) -> bool:
    """Initialize Vertex AI if in Vertex mode. Returns True on success or if not needed."""
    if not is_vertex_mode(config):
        return False
    try:
        import vertexai  # from google-cloud-aiplatform
        vertexai.init(project=get_gcp_project(config), location=get_gcp_location(config))
        return True
    except Exception as e:
        print(f"[vertex_config] Vertex init failed: {e}")
        return False
