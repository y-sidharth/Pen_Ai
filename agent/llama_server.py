#!/usr/bin/env python3
"""
llama_server.py — shared warm llama-server client for the CLI and popup paths.

The web UI (web_ui.py) keeps a resident llama-server.exe on 127.0.0.1:8766 so
replies don't cold-load the ~3.7GB model on every message. This module lets the
CLI (run_agent.py) and the popup (single_query.py) reuse that SAME warm server:

  - if it's already running (started by the web UI or an earlier CLI turn) we
    just POST to it — no reload;
  - if not, ensure_server() can start it once (GPU offload per config, with a
    one-time CPU fallback) and reuse it for the rest of the session.

Host / port / binary MUST match web_ui.py so both share one process. If anything
here fails, callers fall back to their existing cold `llama.exe` path, so this is
purely an accelerator — never a new hard dependency.

Stdlib only (works with the bundled portable interpreter).
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LLAMA_SERVER_PATH = BASE_DIR / "bin" / "llama-server.exe"

# MUST match web_ui.py LLAMA_HOST / LLAMA_PORT so the warm server is shared.
LLAMA_HOST = "127.0.0.1"
LLAMA_PORT = 8766

_PROC = None  # a server this process started (if any)


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _url(path: str) -> str:
    return f"http://{LLAMA_HOST}:{LLAMA_PORT}{path}"


def _is_port_open() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((LLAMA_HOST, LLAMA_PORT)) == 0


def is_healthy() -> bool:
    """True if a warm llama-server is already answering on the shared port."""
    if not _is_port_open():
        return False
    try:
        with urllib.request.urlopen(_url("/health"), timeout=1) as r:
            r.read()
        return True
    except Exception:
        return False


def _resolve(path_value) -> Path:
    p = Path(path_value)
    return p if p.is_absolute() else (BASE_DIR / p)


def _launch_cmd(ngl, cfg, model_path) -> list:
    threads = str(cfg.get("threads") or min(8, os.cpu_count() or 8))
    ctx_size = str(cfg.get("ctx_size") or 4096)
    max_tokens = str(cfg.get("max_tokens") or 768)
    cmd = [
        str(LLAMA_SERVER_PATH),
        "-m", str(model_path),
        "--host", LLAMA_HOST,
        "--port", str(LLAMA_PORT),
    ]
    # "auto" => omit -ngl so llama.cpp fits as many layers as free VRAM allows.
    # Forcing -ngl 99 on a 4GB GPU disables auto-fit and aborts the load. A concrete
    # integer (including 0 for pure CPU) is passed through unchanged.
    if str(ngl) != "auto":
        cmd += ["-ngl", str(ngl)]
    cmd += [
        "--threads", threads,
        "-n", max_tokens,
        "--ctx-size", ctx_size,
        "--no-webui",
        "--offline",
    ]
    return cmd


def ensure_server(timeout: float = 180.0) -> bool:
    """Ensure the warm server is running; return True if healthy.

    Reuses an already-running server if present. Otherwise starts one with GPU
    offload (config `gpu_layers`, default full) and falls back once to CPU if
    the GPU launch dies. Safe to call repeatedly.
    """
    global _PROC
    if is_healthy():
        return True

    cfg = _load_config()
    model_path = _resolve(cfg["model_path"]) if cfg.get("model_path") else None
    if not LLAMA_SERVER_PATH.exists() or not model_path or not model_path.exists():
        return False

    gpu_raw = cfg.get("gpu_layers", "auto")
    if isinstance(gpu_raw, str) and gpu_raw.strip().lower() == "auto":
        gpu_layers = "auto"
    else:
        try:
            gpu_layers = str(int(gpu_raw))
        except (TypeError, ValueError):
            gpu_layers = "auto"

    log_dir = BASE_DIR / "logs"
    try:
        log_dir.mkdir(exist_ok=True)
    except Exception:
        pass

    def _spawn(ngl):
        global _PROC
        logf = subprocess.DEVNULL
        logf_handle = None
        try:
            logf_handle = (log_dir / "cli_llama_server.log").open("a", encoding="utf-8")
            logf_handle.write(f"\n=== cli warm start {time.strftime('%H:%M:%S')} ngl={ngl} ===\n")
            logf_handle.flush()
            logf = logf_handle
        except Exception:
            if logf_handle:
                try:
                    logf_handle.close()
                except Exception:
                    pass
            logf = subprocess.DEVNULL
        try:
            _PROC = subprocess.Popen(
                _launch_cmd(ngl, cfg, model_path),
                cwd=str(BASE_DIR),
                stdout=logf,
                stderr=subprocess.STDOUT,
                text=True,
            )
            return True
        except Exception:
            # Popen failed — close log file handle that leaked
            if logf_handle is not None:
                try:
                    logf_handle.close()
                except Exception:
                    pass
            return False

    def _wait(deadline):
        while time.time() < deadline:
            if _PROC and _PROC.poll() is not None:
                return "died"
            if is_healthy():
                return "ready"
            time.sleep(0.5)
        return "loading"

    if not _spawn(gpu_layers):
        return False
    result = _wait(time.time() + timeout)
    if result == "died" and gpu_layers != "0":
        # GPU init likely failed (VRAM/driver) — retry once on CPU.
        if not _spawn("0"):
            return False
        _wait(time.time() + timeout)
    return is_healthy()


def _clean(text: str) -> str:
    if not text:
        return text
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'</?think>', '', text, flags=re.IGNORECASE)
    m = re.search(r'\n\s*(User|Assistant)\s*:\s*', text)
    if m:
        text = text[:m.start()].rstrip()
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def chat(messages, temperature=0.7, max_tokens=None, timeout=120):
    """POST a chat-completions request to the warm server.

    Returns the assistant text, or None on any failure (so callers can fall
    back to their cold path). Does NOT start the server — call ensure_server()
    first if you want it started.
    """
    if not is_healthy():
        return None
    if max_tokens is None:
        max_tokens = int(_load_config().get("max_tokens") or 768)
    payload = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "enable_thinking": False,
        "chat_template_kwargs": {"enable_thinking": False},
        "stop": ["<|im_end|>", "<think>", "</think>", "\nUser:", "\nAssistant:"],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _url("/v1/chat/completions"),
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            result = json.loads(r.read().decode("utf-8"))
        raw = (result["choices"][0]["message"]["content"] or "").strip()
        cleaned = _clean(raw)
        return cleaned if cleaned else None
    except Exception:
        return None
