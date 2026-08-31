#!/usr/bin/env python3
"""
adk_agent/tools.py - ADK FunctionTools wrapping existing Pen AI controllers.

Each function is a thin JSON-schema typed wrapper so ADK can discover it.
No shell=True anywhere. All side-effects respect internet_allowed where relevant.

Exposed tools:
 - local_rag_search(query, k=3)
 - media_play(query)
 - media_volume(level_or_direction)
 - system_task(command)
 - firestore_sync_text(role, text)
 - cloud_backup()
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Lazy imports to avoid hard deps at module load
def _load_cfg():
    import json
    p = BASE_DIR / "config.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def local_rag_search(query: str, k: int = 3) -> dict:
    """Search local brain docs via TF-IDF. Always offline-safe."""
    try:
        import single_query
        idx = single_query.load_index()
        docs = single_query.query_topk(idx, query, k=k)
        return {"ok": True, "docs": [{"text": d["text"][:2000], "id": d["id"]} for d in docs], "count": len(docs)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def media_play(query: str) -> dict:
    """Play a song from local library by natural language query."""
    try:
        from media_controller import MediaController
        cfg = _load_cfg()
        mc = MediaController({"music_dirs": cfg.get("music_dirs"), "media_player": cfg.get("media_player", "vlc")})
        msg, success = mc.play_song_by_name(query)
        return {"ok": success, "message": msg}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def media_volume(level_or_direction: str) -> dict:
    """Set or adjust volume. Accepts 'up', 'down', or '0-100'."""
    try:
        from media_controller import MediaController, handle_media_command
        cfg = _load_cfg()
        mc = MediaController({"music_dirs": cfg.get("music_dirs"), "media_player": cfg.get("media_player", "vlc")})
        v = level_or_direction.strip().lower()
        if v in ("up", "down"):
            cmd = f"volume {v}"
        else:
            # numeric
            try:
                n = int(v)
                cmd = f"volume {n}"
            except ValueError:
                return {"ok": False, "error": "Use 'up', 'down', or 0-100"}
        msg, ok = handle_media_command(mc, cmd)
        return {"ok": ok, "message": msg}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def system_task(command: str) -> dict:
    """Parse and preview a system task (open app, search, time, etc.) without executing privileged actions."""
    try:
        from task_executor import TaskExecutor
        cfg = _load_cfg()
        ex = TaskExecutor({"default_search": cfg.get("default_search", "google")})
        parsed = ex.parse_task(command)
        # Do not auto-execute shutdown/restart/lock in ADK preview
        if parsed.get("action") in ("shutdown", "restart", "lock", "sleep"):
            return {"ok": False, "parsed": parsed, "error": "Privileged action requires /run approval"}
        msg, success, needs_approval = ex.execute_task(parsed)
        return {"ok": success, "message": msg, "needs_approval": needs_approval, "parsed": parsed}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def firestore_sync_text(role: str, text: str) -> dict:
    """Sync one chat turn to Firestore (requires internet_allowed + GCP project)."""
    try:
        import firestore_sync
        cfg = _load_cfg()
        return firestore_sync.sync_conversation_to_firestore(
            role=role, text=text, config=cfg, internet_allowed=cfg.get("internet_allowed", False)
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}

def cloud_backup() -> dict:
    """Backup brain/ + index to GCS bucket (requires GCS_BUCKET)."""
    try:
        import firestore_sync
        cfg = _load_cfg()
        return firestore_sync.backup_brain_to_gcs(config=cfg, internet_allowed=cfg.get("internet_allowed", False))
    except Exception as e:
        return {"ok": False, "error": str(e)}

# Registry for ADK
TOOL_REGISTRY = {
    "local_rag_search": local_rag_search,
    "media_play": media_play,
    "media_volume": media_volume,
    "system_task": system_task,
    "firestore_sync_text": firestore_sync_text,
    "cloud_backup": cloud_backup,
}

def get_tool_schemas():
    """Return JSON-schema-ish descriptions for ADK / Genkit."""
    return [
        {"name": "local_rag_search", "description": "Search local brain TF-IDF index", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "k": {"type": "integer"}}, "required": ["query"]}},
        {"name": "media_play", "description": "Play song from local library", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
        {"name": "media_volume", "description": "Set volume (0-100) or up/down", "parameters": {"type": "object", "properties": {"level_or_direction": {"type": "string"}}, "required": ["level_or_direction"]}},
        {"name": "system_task", "description": "Preview/execute safe system tasks", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
        {"name": "firestore_sync_text", "description": "Sync chat turn to Firestore", "parameters": {"type": "object", "properties": {"role": {"type": "string"}, "text": {"type": "string"}}, "required": ["role","text"]}},
        {"name": "cloud_backup", "description": "Backup brain to GCS", "parameters": {"type": "object", "properties": {}, "required": []}},
    ]
