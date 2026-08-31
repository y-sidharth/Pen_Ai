#!/usr/bin/env python3
"""
firestore_sync.py - Firestore + Cloud Storage helpers for Pen AI.

Checklist: Google Cloud Service (Firestore + Cloud Storage + Vertex AI via vertex_config)

- Firestore: sync conversation history and brain docs
- Cloud Storage: backup brain/ and data/index.json

All functions are safe to call when no credentials / offline - they return
{"ok": False, "reason": "..."} instead of raising, so local mode never breaks.

Env:
  GOOGLE_CLOUD_PROJECT (or gcp_project in config.json)
  FIRESTORE_COLLECTION (default jarvis_conversations)
  GCS_BUCKET (or gcs_bucket in config.json)

Collections:
  {collection}/{device_id}/conversations/{docId}
  {collection}/{device_id}/brain/{docId}
"""
from __future__ import annotations

import os
import json
import hashlib
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import vertex_config

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
BRAIN_DIR = BASE_DIR / "brain"
DB_PATH = DATA_DIR / "sqlite.db"

DEFAULT_COLLECTION = "jarvis_conversations"


def _device_id() -> str:
    # Stable-ish per machine+user, no PII. Use hashed username+machine.
    raw = f"{os.getenv('COMPUTERNAME','')}|{os.getenv('USERNAME','')}|{BASE_DIR}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _get_collection(config: dict | None) -> str:
    if config and config.get("firestore_collection"):
        return str(config["firestore_collection"]).strip()
    return os.getenv("FIRESTORE_COLLECTION") or DEFAULT_COLLECTION


def _get_bucket(config: dict | None) -> str | None:
    if config and config.get("gcs_bucket"):
        return str(config["gcs_bucket"]).strip()
    return os.getenv("GCS_BUCKET")


def _require_online(config: dict | None, internet_allowed: bool) -> Optional[str]:
    if not internet_allowed:
        return "Internet not allowed (offline mode)"
    if not vertex_config.get_gcp_project(config):
        return "GOOGLE_CLOUD_PROJECT not set (Firestore/Storage need a GCP project)"
    return None


def _firestore_client(config: dict | None):
    try:
        from google.cloud import firestore  # type: ignore
        project = vertex_config.get_gcp_project(config)
        return firestore.Client(project=project)
    except Exception as e:
        raise RuntimeError(f"Firestore client init failed: {e}. Run pip install google-cloud-firestore and gcloud auth application-default login") from e


def _storage_client(config: dict | None):
    try:
        from google.cloud import storage  # type: ignore
        project = vertex_config.get_gcp_project(config)
        return storage.Client(project=project)
    except Exception as e:
        raise RuntimeError(f"Storage client init failed: {e}. Run pip install google-cloud-storage") from e


# ---- Firestore ----

def sync_conversation_to_firestore(
    role: str,
    text: str,
    ts: str | None = None,
    config: dict | None = None,
    internet_allowed: bool = False,
) -> dict:
    """Write one conversation turn to Firestore. Safe to call offline."""
    reason = _require_online(config, internet_allowed)
    if reason:
        return {"ok": False, "reason": reason}
    if not is_firestore_available(config):
        return {"ok": False, "reason": "google-cloud-firestore not installed"}
    try:
        db = _firestore_client(config)
        coll = _get_collection(config)
        device = _device_id()
        doc_id = f"{datetime.now(timezone.utc).isoformat()}_{hashlib.sha256(text.encode()).hexdigest()[:8]}"
        doc_ref = db.collection(coll).document(device).collection("conversations").document(doc_id)
        doc_ref.set({
            "role": role,
            "text": text[:5000],
            "ts": ts or datetime.now(timezone.utc).isoformat(),
            "device_id": device,
        })
        return {"ok": True, "doc": f"{coll}/{device}/conversations/{doc_id}"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def sync_brain_docs_to_firestore(config: dict | None = None, internet_allowed: bool = False) -> dict:
    """Upload all brain/*.txt docs to Firestore."""
    reason = _require_online(config, internet_allowed)
    if reason:
        return {"ok": False, "reason": reason}
    if not BRAIN_DIR.exists():
        return {"ok": False, "reason": "brain/ not found"}
    try:
        db = _firestore_client(config)
        coll = _get_collection(config)
        device = _device_id()
        count = 0
        for txt_file in BRAIN_DIR.rglob("*.txt"):
            try:
                text = txt_file.read_text(encoding="utf-8")[:8000]
            except Exception:
                continue
            doc_id = hashlib.sha256(str(txt_file).encode()).hexdigest()[:16]
            db.collection(coll).document(device).collection("brain").document(doc_id).set({
                "path": str(txt_file.relative_to(BASE_DIR)),
                "text": text,
                "synced_at": datetime.now(timezone.utc).isoformat(),
            })
            count += 1
        return {"ok": True, "count": count, "collection": f"{coll}/{device}/brain"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def fetch_recent_conversations(limit: int = 20, config: dict | None = None, internet_allowed: bool = False) -> dict:
    reason = _require_online(config, internet_allowed)
    if reason:
        return {"ok": False, "reason": reason}
    try:
        db = _firestore_client(config)
        coll = _get_collection(config)
        device = _device_id()
        docs = (
            db.collection(coll).document(device).collection("conversations")
            .order_by("ts", direction="DESCENDING").limit(limit).stream()
        )
        out = [d.to_dict() for d in docs]
        return {"ok": True, "conversations": out}
    except Exception as e:
        return {"ok": False, "reason": str(e)}

# ---- Cloud Storage ----

def backup_brain_to_gcs(config: dict | None = None, internet_allowed: bool = False) -> dict:
    """Upload brain/*.txt + data/index.json to GCS bucket."""
    reason = _require_online(config, internet_allowed)
    if reason:
        return {"ok": False, "reason": reason}
    bucket_name = _get_bucket(config)
    if not bucket_name:
        return {"ok": False, "reason": "GCS_BUCKET / gcs_bucket not set"}
    try:
        client = _storage_client(config)
        bucket = client.bucket(bucket_name)
        # Ensure bucket exists (create if not)
        if not bucket.exists():
            try:
                bucket.create(location=vertex_config.get_gcp_location(config))
            except Exception:
                pass
        uploaded = 0
        device = _device_id()
        # brain files
        if BRAIN_DIR.exists():
            for f in BRAIN_DIR.rglob("*"):
                if f.is_file():
                    rel = f.relative_to(BASE_DIR).as_posix()
                    blob = bucket.blob(f"{device}/{rel}")
                    blob.upload_from_filename(str(f))
                    uploaded += 1
        # index
        idx = DATA_DIR / "index.json"
        if idx.exists():
            bucket.blob(f"{device}/data/index.json").upload_from_filename(str(idx))
            uploaded += 1
        return {"ok": True, "uploaded": uploaded, "bucket": bucket_name, "prefix": device}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def restore_brain_from_gcs(config: dict | None = None, internet_allowed: bool = False) -> dict:
    bucket_name = _get_bucket(config)
    if not bucket_name:
        return {"ok": False, "reason": "GCS_BUCKET not set"}
    reason = _require_online(config, internet_allowed)
    if reason:
        return {"ok": False, "reason": reason}
    try:
        client = _storage_client(config)
        bucket = client.bucket(bucket_name)
        device = _device_id()
        blobs = list(client.list_blobs(bucket_name, prefix=f"{device}/brain/"))
        restored = 0
        for blob in blobs:
            rel = blob.name[len(device)+1 :]
            dest = BASE_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(str(dest))
            restored += 1
        return {"ok": True, "restored": restored}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def is_firestore_available(config: dict | None = None) -> bool:
    try:
        import google.cloud.firestore  # noqa
        return True
    except ImportError:
        return False

def is_storage_available(config: dict | None = None) -> bool:
    try:
        import google.cloud.storage  # noqa
        return True
    except ImportError:
        return False

def health_check(config: dict | None = None, internet_allowed: bool = False) -> dict:
    return {
        "firestore_installed": is_firestore_available(config),
        "storage_installed": is_storage_available(config),
        "gcp_project": vertex_config.get_gcp_project(config),
        "gcs_bucket": _get_bucket(config),
        "firestore_collection": _get_collection(config),
        "device_id": _device_id(),
        "internet_allowed": internet_allowed,
        "can_sync": _require_online(config, internet_allowed) is None and is_firestore_available(config),
    }

if __name__ == "__main__":
    import json as _json
    cfg = {}
    p = BASE_DIR / "config.json"
    if p.exists():
        cfg = _json.loads(p.read_text(encoding="utf-8"))
    print(_json.dumps(health_check(cfg, internet_allowed=cfg.get("internet_allowed", False)), indent=2))
