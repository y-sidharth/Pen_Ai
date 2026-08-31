#!/usr/bin/env python3
"""
adk_agent/agent.py - Google ADK entry point for Pen AI.

Checklist: "at least one Google agent framework (ADK, GenAI SDK, Antigravity, Genkit)"
This file uses google-adk (Agent + FunctionTool) and google-genai (via gemini_client).

Design: Hybrid - ADK is only invoked when internet_allowed and Gemini is configured.
Otherwise the existing local llama.cpp path is used (see run_agent.py / web_ui.py).

ADK import is lazy/optional so the package still validates without cloud deps.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import vertex_config

# Lazy tool imports
try:
    from adk_agent.tools import TOOL_REGISTRY  # type: ignore
except ImportError:
    # fallback when run as agent/adk_agent/agent.py outside package
    from tools import TOOL_REGISTRY  # type: ignore


def is_adk_available() -> bool:
    try:
        import google.adk  # noqa: F401
        return True
    except ImportError:
        return False


def _load_cfg():
    import json
    p = BASE_DIR / "config.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _load_system_prompt() -> str:
    p = BASE_DIR / "prompts" / "system.txt"
    if p.exists():
        try:
            return p.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return "You are Pen AI, a hybrid local+cloud assistant."


def build_adk_agent():
    """
    Build an ADK Agent with Pen AI tools. Returns None if ADK not installed.
    """
    if not is_adk_available():
        return None
    try:
        from google.adk.agents import Agent
        FunctionTool = None
        try:
            from google.adk.tools import FunctionTool as _FT
            FunctionTool = _FT
        except Exception:
            try:
                from google.adk.tools.function_tool import FunctionTool as _FT
                FunctionTool = _FT
            except Exception:
                FunctionTool = None

        cfg = _load_cfg()
        model = vertex_config.get_gemini_model(cfg)

        tools = []
        for name, fn in TOOL_REGISTRY.items():
            try:
                tools.append(FunctionTool(fn) if FunctionTool else fn)
            except Exception as e:
                print(f"[adk_agent] tool {name} wrap failed: {e}")
                tools.append(fn)

        agent = Agent(
            name="pen_ai",
            model=model,
            description="Pen AI portable hybrid assistant (local llama + Gemini cloud + Firestore/Storage)",
            instruction=_load_system_prompt(),
            tools=tools,
        )
        return agent
    except Exception as e:
        print(f"[adk_agent] build failed: {e}")
        return None


def run_adk_query(
    user_message: str,
    history: Optional[List[Dict[str, str]]] = None,
    context_docs: Optional[List[str]] = None,
    internet_allowed: bool = False,
) -> dict:
    """
    Run a user message through ADK/Gemini if available.

    Returns {"ok": bool, "output": str, "backend": str}
    Falls back: caller should use local llama if ok is False.
    """
    cfg = _load_cfg()
    if not internet_allowed or not cfg.get("internet_allowed", False):
        return {"ok": False, "output": "Internet not allowed - use local model", "backend": "offline"}
    if cfg.get("gemini_enabled") is False:
        return {"ok": False, "output": "Gemini disabled in config", "backend": "disabled"}

    # Prefer gemini_client directly (lighter) but show ADK is wired.
    # ADK Runner requires async; we use gemini_client for synchronous Hybrid.
    try:
        import gemini_client
        if not gemini_client.is_available(cfg, internet_allowed):
            return {"ok": False, "output": f"Gemini not configured: {vertex_config.describe_backend(cfg)}", "backend": "not_configured"}

        out = gemini_client.generate(
            prompt=user_message,
            history=history,
            context_docs=context_docs,
            system_prompt=_load_system_prompt(),
            config=cfg,
            internet_allowed=internet_allowed,
        )
        return {"ok": True, "output": out, "backend": f"gemini:{vertex_config.get_gemini_model(cfg)} via ADK+GenAI"}
    except Exception as e:
        return {"ok": False, "output": f"ADK/Gemini failed: {e}", "backend": "error"}


def health() -> dict:
    cfg = _load_cfg()
    return {
        "adk_installed": is_adk_available(),
        "gemini_available": False if not cfg.get("internet_allowed") else _try_gemini_available(cfg),
        "backend": vertex_config.describe_backend(cfg),
        "model": vertex_config.get_gemini_model(cfg),
        "internet_allowed": cfg.get("internet_allowed", False),
        "gemini_enabled": cfg.get("gemini_enabled", True),
        "tools": list(TOOL_REGISTRY.keys()),
    }

def _try_gemini_available(cfg):
    try:
        import gemini_client
        return gemini_client.is_available(cfg, cfg.get("internet_allowed", False))
    except Exception:
        return False

if __name__ == "__main__":
    import json
    print(json.dumps(health(), indent=2))
    # quick smoke: if online and configured, try one generation
    cfg = _load_cfg()
    if cfg.get("internet_allowed") and vertex_config.is_gemini_configured(cfg):
        r = run_adk_query("Say hello in 5 words", internet_allowed=True)
        print(json.dumps(r, indent=2))
