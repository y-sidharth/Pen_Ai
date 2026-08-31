#!/usr/bin/env python3
"""
gemini_client.py - Hybrid Gemini wrapper for Pen AI.

Checklist coverage:
 - Gemini 3.5 or newer (uses gemini-2.5-pro by default, accepts gemini-3.5-pro alias)
 - Google GenAI SDK (google-genai) + Vertex AI (google-cloud-aiplatform)

Behavior:
 - If internet_allowed is False, raises RuntimeError (caller must fallback to local llama).
 - If no credentials (no GEMINI_API_KEY and no GOOGLE_CLOUD_PROJECT), raises.
 - Supports Vertex AI mode and AI Studio mode automatically.

Usage:
    from gemini_client import generate, generate_stream, is_available
    text = generate(prompt="Explain USB security", history=[...], context_docs=[...])
"""
from __future__ import annotations

import os
from typing import List, Dict, Optional

import vertex_config

DEFAULT_SYSTEM_PROMPT = "You are Pen AI, a helpful local+cloud hybrid assistant."

class GeminiNotConfigured(RuntimeError):
    pass

class GeminiOffline(RuntimeError):
    pass


def is_available(config: dict | None = None, internet_allowed: bool = True) -> bool:
    if not internet_allowed:
        return False
    # allow gemini_enabled flag in config to explicitly disable
    if config and config.get("gemini_enabled") is False:
        return False
    return vertex_config.is_gemini_configured(config)


def _build_client(config: dict | None = None):
    """Build google-genai Client for either Vertex or AI Studio."""
    model = vertex_config.get_gemini_model(config)
    # Try new google-genai SDK first
    try:
        from google import genai  # type: ignore
        if vertex_config.is_vertex_mode(config):
            vertex_config.ensure_vertex_init(config)
            client = genai.Client(
                vertexai=True,
                project=vertex_config.get_gcp_project(config),
                location=vertex_config.get_gcp_location(config),
            )
        else:
            api_key = vertex_config.get_api_key()
            if not api_key:
                raise GeminiNotConfigured("GEMINI_API_KEY or GOOGLE_CLOUD_PROJECT required")
            client = genai.Client(api_key=api_key)
        return client, model, "genai"
    except ImportError:
        pass
    except Exception as e:
        # fall through to generative-ai fallback
        print(f"[gemini_client] genai Client init failed: {e}")

    # Fallback: google-generativeai (older SDK)
    try:
        import google.generativeai as genai_old  # type: ignore
        api_key = vertex_config.get_api_key()
        if not api_key:
            raise GeminiNotConfigured("GEMINI_API_KEY required for generativeai fallback")
        genai_old.configure(api_key=api_key)
        return genai_old, model, "generativeai"
    except ImportError:
        raise GeminiNotConfigured(
            "No Google GenAI SDK installed. Run: pip install google-genai google-cloud-aiplatform"
        )


def _format_history(history: List[Dict[str, str]]) -> List[Dict]:
    """Convert simple {role, content} history to GenAI content format."""
    contents = []
    for turn in history[-12:]:
        role = "user" if turn.get("role") == "user" else "model"
        # GenAI expects 'user' and 'model' (not 'assistant')
        if turn.get("role") == "system":
            # System turns are injected into system_instruction instead
            continue
        contents.append({"role": role, "parts": [{"text": str(turn.get("content", ""))}]})
    return contents


def generate(
    prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
    context_docs: Optional[List[str]] = None,
    system_prompt: Optional[str] = None,
    config: Optional[dict] = None,
    internet_allowed: bool = True,
    temperature: float = 0.7,
    max_tokens: int = 1024,
) -> str:
    """
    Generate a completion via Gemini. Falls back behavior is caller's responsibility.

    Raises GeminiOffline if internet_allowed is False, GeminiNotConfigured if no creds.
    """
    if not internet_allowed:
        raise GeminiOffline("Internet not allowed - use local model")
    if config and config.get("gemini_enabled") is False:
        raise GeminiOffline("Gemini disabled in config")
    if not vertex_config.is_gemini_configured(config):
        raise GeminiNotConfigured(vertex_config.describe_backend(config))

    system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
    if context_docs:
        system_prompt += "\n\nRelevant local notes:\n" + "\n\n".join(context_docs[:3])

    client, model, backend = _build_client(config)
    contents = _format_history(history or [])
    contents.append({"role": "user", "parts": [{"text": prompt}]})

    # New SDK: client.models.generate_content
    if backend == "genai":
        try:
            resp = client.models.generate_content(
                model=model,
                contents=contents,
                config={
                    "system_instruction": system_prompt,
                    "temperature": temperature,
                    "max_output_tokens": max_tokens,
                } if hasattr(client, "models") else None,
            )
            # SDK variants: resp.text or resp.candidates[0].content.parts[0].text
            text = getattr(resp, "text", None)
            if text:
                return text.strip()
            # fallback parsing
            if getattr(resp, "candidates", None):
                parts = resp.candidates[0].content.parts
                return "".join(getattr(p, "text", "") for p in parts).strip()
            return str(resp).strip()
        except Exception as e:
            raise RuntimeError(f"Gemini generate failed ({model} via {vertex_config.describe_backend(config)}): {e}") from e
    else:
        # Old SDK
        try:
            genai_old = client  # type: ignore
            m = genai_old.GenerativeModel(model, system_instruction=system_prompt)
            # Convert history to chat
            chat_history = []
            for turn in (history or [])[-12:]:
                if turn.get("role") in ("user", "model", "assistant"):
                    role = "user" if turn.get("role") == "user" else "model"
                    chat_history.append({"role": role, "parts": [turn.get("content", "")]})
            chat = m.start_chat(history=chat_history)
            resp = chat.send_message(prompt, generation_config={"temperature": temperature, "max_output_tokens": max_tokens})
            return resp.text.strip()
        except Exception as e:
            raise RuntimeError(f"Gemini (generativeai) failed: {e}") from e


def generate_stream(*args, **kwargs):
    """Simple wrapper - currently non-streaming, yields full text."""
    text = generate(*args, **kwargs)
    yield text


def health_check(config: dict | None = None, internet_allowed: bool = True) -> dict:
    """Return status dict for UI / validation without making a network call if offline."""
    return {
        "available": is_available(config, internet_allowed),
        "backend": vertex_config.describe_backend(config),
        "model": vertex_config.get_gemini_model(config),
        "vertex_mode": vertex_config.is_vertex_mode(config),
        "internet_allowed": internet_allowed,
        "gemini_enabled": (config or {}).get("gemini_enabled", True),
    }


if __name__ == "__main__":
    import json
    import pathlib
    cfg_path = pathlib.Path(__file__).parent / "config.json"
    cfg = {}
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    print(json.dumps(health_check(cfg, internet_allowed=cfg.get("internet_allowed", False)), indent=2))
    if is_available(cfg, internet_allowed=True):
        try:
            out = generate("Say hello in one short sentence.", config=cfg, internet_allowed=True)
            print("Gemini reply:", out)
        except Exception as e:
            print("Generate failed:", e)
    else:
        print("Gemini not configured. Set GEMINI_API_KEY or GOOGLE_CLOUD_PROJECT in agent/.env")
