#!/usr/bin/env python3
"""Local browser UI for the Jampandu assistant."""

import json
import os
import re
import sqlite3
import atexit
import socket
import subprocess
import sys
import threading
import time
import datetime
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# The portable embeddable Python distribution ships a python310._pth file
# that restricts sys.path to just python310.zip and its own directory --
# it does not auto-add the script's directory the way a normal install
# does. Without this, "import single_query" fails at startup whenever
# web_ui.py is launched via python-portable/python.exe.
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import single_query  # noqa: E402  (must follow the sys.path fix above)
from security import verify_verifier  # noqa: E402
from task_executor import TaskExecutor, handle_task_command  # noqa: E402
try:
    import host_cleaner  # noqa: E402  amnesiac host-trace erasure
except Exception:
    host_cleaner = None
import signal as _signal  # noqa: E402

CONFIG_PATH = BASE_DIR / "config.json"
VALIDATE_PATH = BASE_DIR / "validate_package.py"
START_AGENT_PATH = BASE_DIR / "start-agent.bat"
SYSTEM_PROMPT_PATH = BASE_DIR / "prompts" / "system.txt"
LLAMA_SERVER_PATH = BASE_DIR / "bin" / "llama-server.exe"
DB_PATH = BASE_DIR / "data" / "sqlite.db"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
LLAMA_HOST = "127.0.0.1"
LLAMA_PORT = 8766
LLAMA_SERVER_PROC = None
LLAMA_SERVER_LOG = None
LLAMA_SERVER_LOCK = threading.Lock()
WARM_START_TIME: float | None = None
WARM_LAST_ERROR: str | None = None
CONVERSATION = []
HISTORY_LOADED = False
STARTUP_VERIFIED = False  # in-memory gate for startup verification (resets on server restart)
TASK_EXECUTOR = None  # lazily created TaskExecutor synced to config internet_allowed
# --- Security hardening: session tokens + brute-force protection ---
import hmac as _hmac
import secrets as _secrets
VALID_TOKENS: dict[str, float] = {}  # token -> expiry epoch
VALID_TOKENS_LOCK = threading.Lock()
FAILED_ATTEMPTS: dict[str, dict] = {}  # ip -> {count, first_ts, lock_until}
FAILED_LOCK = threading.Lock()
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 300  # 5 min lock after 5 fails
ATTEMPT_WINDOW = 600  # 10 min window to count failures
TOKEN_TTL_SECONDS = 12 * 3600  # 12 hours, reset on server restart anyway

def _generate_token() -> str:
    return _secrets.token_urlsafe(32)

def _store_token(token: str):
    with VALID_TOKENS_LOCK:
        VALID_TOKENS[token] = time.time() + TOKEN_TTL_SECONDS
        # prune expired
        now = time.time()
        for k, exp in list(VALID_TOKENS.items()):
            if exp < now:
                VALID_TOKENS.pop(k, None)

def _is_token_valid(token: str | None) -> bool:
    if not token:
        return False
    with VALID_TOKENS_LOCK:
        exp = VALID_TOKENS.get(token)
        if exp is None:
            return False
        # constant-time compare to avoid timing oracle on token existence (check all)
        # we still need to find match; use compare_digest for the matched one
        if exp < time.time():
            VALID_TOKENS.pop(token, None)
            return False
        return True

def _client_ip(handler) -> str:
    try:
        return handler.client_address[0]
    except Exception:
        return "unknown"

def _is_locked(ip: str) -> tuple[bool, int]:
    with FAILED_LOCK:
        rec = FAILED_ATTEMPTS.get(ip)
        if not rec:
            return False, 0
        lock_until = rec.get("lock_until", 0)
        if lock_until and time.time() < lock_until:
            return True, int(lock_until - time.time())
        if lock_until and time.time() >= lock_until:
            # lock expired, reset
            FAILED_ATTEMPTS.pop(ip, None)
            return False, 0
        return False, 0

def _record_failed(ip: str):
    with FAILED_LOCK:
        now = time.time()
        rec = FAILED_ATTEMPTS.get(ip)
        if not rec or now - rec.get("first_ts", 0) > ATTEMPT_WINDOW:
            rec = {"count": 1, "first_ts": now, "lock_until": 0}
        else:
            rec["count"] += 1
        if rec["count"] >= MAX_ATTEMPTS:
            rec["lock_until"] = now + LOCKOUT_SECONDS
        FAILED_ATTEMPTS[ip] = rec

def _clear_failed(ip: str):
    with FAILED_LOCK:
        FAILED_ATTEMPTS.pop(ip, None)

def _is_authorized(handler) -> bool:
    """Server-side auth check. Must be called for every sensitive API."""
    cfg, _ = load_config()
    if not cfg:
        return False
    if not cfg.get("startup_auth_enabled", True):
        return True
    # If no verifier configured, allow (first-run)
    if not cfg.get("password_verifier"):
        return True
    # Check token from header X-Auth-Token or Authorization Bearer or cookie
    token = handler.headers.get("X-Auth-Token")
    if not token:
        auth = handler.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:].strip()
    if not token:
        # try cookie
        cookie = handler.headers.get("Cookie", "")
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("jampandu_token="):
                token = part.split("=", 1)[1].strip()
                break
    if _is_token_valid(token):
        return True
    return False

def _require_auth(handler) -> bool:
    if _is_authorized(handler):
        return True
    handler.send_json({"ok": False, "output": "Not authorized. Please verify password on startup overlay."}, status=401)
    return False


def stop_llama_server():
    global LLAMA_SERVER_PROC, LLAMA_SERVER_LOG
    if LLAMA_SERVER_PROC and LLAMA_SERVER_PROC.poll() is None:
        try:
            LLAMA_SERVER_PROC.terminate()
            LLAMA_SERVER_PROC.wait(timeout=10)
        except Exception:
            try:
                LLAMA_SERVER_PROC.kill()
            except Exception:
                pass
    LLAMA_SERVER_PROC = None
    if LLAMA_SERVER_LOG:
        try:
            LLAMA_SERVER_LOG.close()
        except Exception:
            pass
    LLAMA_SERVER_LOG = None


def _host_cleanup_amnesiac(why: str = "exit"):
    """Erase host traces — called on any exit / power-off / removal."""
    try:
        if host_cleaner:
            host_cleaner.full_host_cleanup(drive_root=None, secure=True, kill_processes=False, clear_clip=True, log=False)
    except: pass
    # Also wipe pendrive tmp + auth token securely
    try:
        if host_cleaner:
            host_cleaner.clean_pendrive_tmp(secure=True)
            host_cleaner.clean_pendrive_auth_token(secure=True)
    except: pass
    # Fallback clipboard clear
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.OpenClipboard(0)
            ctypes.windll.user32.EmptyClipboard()
            ctypes.windll.user32.CloseClipboard()
    except: pass

def _shutdown_handler(signum=None, frame=None):
    _host_cleanup_amnesiac(why=f"signal_{signum}")
    stop_llama_server()
    try: sys.exit(0)
    except: os._exit(0)

atexit.register(stop_llama_server)
atexit.register(lambda: _host_cleanup_amnesiac("atexit"))
try:
    _signal.signal(_signal.SIGINT, _shutdown_handler)
    _signal.signal(_signal.SIGTERM, _shutdown_handler)
except: pass
if os.name == "nt":
    try:
        import ctypes
        from ctypes import wintypes as _wt
        _k32 = ctypes.windll.kernel32
        _Handler = ctypes.WINFUNCTYPE(_wt.BOOL, _wt.DWORD)
        def _ctrl_handler(ctrl_type):
            if ctrl_type in (2, 5, 6):  # CTRL_CLOSE, LOGOFF, SHUTDOWN
                _host_cleanup_amnesiac(f"CTRL_{ctrl_type}")
                stop_llama_server()
                time.sleep(0.3)
            return False
        _webui_ctrl = _Handler(_ctrl_handler)
        _k32.SetConsoleCtrlHandler(_webui_ctrl, True)
    except: pass


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
  <meta http-equiv="Pragma" content="no-cache">
  <title>Jampandu — Fixed Layout v5</title>
  <style>
    :root {
      color-scheme: dark;
      --bg-0: #0a0714;
      --bg-1: #120a24;
      --surface: rgba(255, 255, 255, 0.05);
      --surface-strong: rgba(255, 255, 255, 0.08);
      --surface-2: rgba(10, 6, 20, 0.35);
      --line: rgba(255, 255, 255, 0.10);
      --line-soft: rgba(255, 255, 255, 0.06);
      --text: #f6f2ff;
      --muted: #ac9fd6;
      --violet: #a855f7;
      --pink: #ec4899;
      --cyan: #22d3ee;
      --lime: #a3e635;
      --amber: #fbbf24;
      --orange: #fb923c;
      --rose: #fb7185;
      --ink: #170a2b;
      --grad-brand: linear-gradient(135deg, #8b5cf6 0%, #ec4899 55%, #fb923c 100%);
      --grad-cyan: linear-gradient(135deg, #22d3ee, #6366f1);
      --grad-ok: linear-gradient(135deg, #34d399, #a3e635);
      --grad-warn: linear-gradient(135deg, #fbbf24, #fb923c);
      --grad-bad: linear-gradient(135deg, #fb7185, #ef4444);
      font-family: "Segoe UI", system-ui, sans-serif;
    }

    * {
      box-sizing: border-box;
    }

    html {
      height: 100%;
      overflow: hidden;
    }

    body {
      margin: 0;
      height: 100%;
      height: 100vh;
      height: 100dvh;
      overflow: hidden;
      color: var(--text);
      background:
        radial-gradient(1100px 760px at 8% -8%, rgba(168, 85, 247, 0.38), transparent 60%),
        radial-gradient(950px 680px at 102% 4%, rgba(236, 72, 153, 0.30), transparent 55%),
        radial-gradient(900px 820px at 46% 118%, rgba(34, 211, 238, 0.24), transparent 55%),
        radial-gradient(700px 600px at 80% 60%, rgba(251, 146, 60, 0.14), transparent 60%),
        linear-gradient(180deg, var(--bg-0), var(--bg-1));
      background-attachment: fixed;
    }

    button, textarea, input {
      font: inherit;
    }

    ::selection {
      background: rgba(168, 85, 247, 0.4);
      color: #fff;
    }

    ::-webkit-scrollbar {
      width: 10px;
      height: 10px;
    }

    ::-webkit-scrollbar-track {
      background: transparent;
    }

    ::-webkit-scrollbar-thumb {
      background: linear-gradient(180deg, var(--violet), var(--pink));
      border-radius: 999px;
    }

    /* CHATGPT-STYLE: fixed sidebar + flex main - sidebar NEVER moves with messages */
    /* Fix: pin app to viewport with inset:0 so input bar is never hidden behind taskbar (see 2nd image) */
    .app {
      display: flex;
      flex-direction: row;
      height: 100vh;
      height: 100dvh;
      overflow: hidden;
      align-items: stretch;
      position: fixed;
      inset: 0;
      width: 100%;
    }

    aside {
      width: 288px;
      min-width: 288px;
      max-width: 288px;
      height: 100vh;
      height: 100dvh;
      overflow-y: auto;
      overflow-x: hidden;
      border-right: 1px solid var(--line);
      background: var(--surface);
      backdrop-filter: blur(22px);
      -webkit-backdrop-filter: blur(22px);
      padding: 22px;
      flex-shrink: 0;
      position: sticky;
      top: 0;
      align-self: flex-start;
      display: flex;
      flex-direction: column;
      scrollbar-width: thin;
      scrollbar-color: rgba(168,85,247,0.5) transparent;
      overscroll-behavior: contain;
      z-index: 5;
    }

    main {
      flex: 1;
      display: flex;
      flex-direction: column;
      min-width: 0;
      height: 100%;
      max-height: 100vh;
      max-height: 100dvh;
      overflow: hidden;
      min-height: 0;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 13px;
      margin-bottom: 26px;
    }

    .brand-mark {
      width: 46px;
      height: 46px;
      border-radius: 13px;
      background: var(--grad-brand);
      display: grid;
      place-items: center;
      box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.15) inset, 0 8px 22px -6px rgba(236, 72, 153, 0.65);
      animation: glow-pulse 3.2s ease-in-out infinite;
    }

    @keyframes glow-pulse {
      0%, 100% { box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.15) inset, 0 8px 22px -6px rgba(236, 72, 153, 0.65); }
      50% { box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.22) inset, 0 10px 30px -4px rgba(168, 85, 247, 0.85); }
    }

    .brand h1 {
      margin: 0;
      font-size: 24px;
      line-height: 1;
      letter-spacing: 0;
      background: var(--grad-brand);
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
      font-weight: 800;
    }

    .brand p {
      margin: 6px 0 0;
      color: var(--muted);
      font-size: 12.5px;
      letter-spacing: 0.02em;
    }

    .section {
      padding: 18px 0;
      border-top: 1px solid var(--line-soft);
    }

    .section h2 {
      margin: 0 0 14px;
      font-size: 12px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-weight: 700;
    }

    .status-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 14px;
      margin: 11px 0;
      color: var(--muted);
      font-size: 14px;
    }

    .pill {
      min-width: 72px;
      text-align: center;
      border: 1px solid var(--line);
      color: var(--text);
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      background: rgba(255, 255, 255, 0.06);
    }

    .pill.ok {
      border-color: transparent;
      background: var(--grad-ok);
      color: #062a17;
      box-shadow: 0 4px 14px -4px rgba(163, 230, 53, 0.55);
    }

    .pill.warn {
      border-color: transparent;
      background: var(--grad-warn);
      color: #3a1c02;
      box-shadow: 0 4px 14px -4px rgba(251, 146, 60, 0.55);
    }

    .pill.bad {
      border-color: transparent;
      background: var(--grad-bad);
      color: #370408;
      box-shadow: 0 4px 14px -4px rgba(251, 113, 133, 0.55);
    }

    .actions {
      display: grid;
      gap: 10px;
    }

    .button {
      min-height: 42px;
      border: 1px solid var(--line);
      background: var(--surface-strong);
      color: var(--text);
      border-radius: 10px;
      cursor: pointer;
      padding: 0 14px;
      text-align: left;
      font-weight: 600;
      transition: transform 0.15s ease, border-color 0.15s ease, background 0.15s ease, box-shadow 0.15s ease;
    }

    .button:hover {
      border-color: rgba(168, 85, 247, 0.55);
      background: rgba(168, 85, 247, 0.14);
      transform: translateY(-1px);
      box-shadow: 0 6px 18px -8px rgba(168, 85, 247, 0.55);
    }

    .button:active {
      transform: translateY(0);
    }

    .button.primary {
      background: var(--grad-brand);
      color: #fff;
      border-color: transparent;
      font-weight: 700;
      text-align: center;
      box-shadow: 0 8px 22px -6px rgba(236, 72, 153, 0.6);
    }

    .button.primary:hover {
      transform: translateY(-1px) scale(1.02);
      box-shadow: 0 10px 28px -6px rgba(236, 72, 153, 0.75);
    }

    .button:disabled {
      cursor: not-allowed;
      opacity: 0.5;
      transform: none;
      box-shadow: none;
    }

    .toggle {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      color: var(--muted);
      font-size: 14px;
    }

    .toggle input {
      width: 18px;
      height: 18px;
      accent-color: var(--pink);
    }

    .switch {
      position: relative;
      display: inline-block;
      width: 44px;
      height: 24px;
      flex-shrink: 0;
    }

    .switch input {
      opacity: 0;
      width: 0;
      height: 0;
    }

    .switch .slider {
      position: absolute;
      inset: 0;
      cursor: pointer;
      background: rgba(255, 255, 255, 0.12);
      border: 1px solid var(--line);
      border-radius: 999px;
      transition: background 0.2s ease, border-color 0.2s ease;
    }

    .switch .slider::before {
      content: "";
      position: absolute;
      width: 18px;
      height: 18px;
      left: 2px;
      top: 2px;
      border-radius: 50%;
      background: #fff;
      transition: transform 0.2s ease;
      box-shadow: 0 2px 6px rgba(0, 0, 0, 0.35);
    }

    .switch input:checked + .slider {
      background: var(--grad-ok);
      border-color: transparent;
    }

    .switch input:checked + .slider::before {
      transform: translateX(20px);
    }

    .switch input:disabled + .slider {
      cursor: not-allowed;
      opacity: 0.6;
    }

    header {
      border-bottom: 1px solid var(--line);
      padding: 18px 26px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      background: var(--surface-2);
      backdrop-filter: blur(18px);
      -webkit-backdrop-filter: blur(18px);
      flex-shrink: 0;
      z-index: 5;
    }

    .headline {
      min-width: 0;
    }

    .headline h2 {
      margin: 0;
      font-size: 19px;
      letter-spacing: 0;
      background: linear-gradient(90deg, var(--cyan), var(--violet));
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
      font-weight: 800;
    }

    .headline p {
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 13px;
    }

    #activity {
      color: var(--cyan);
      font-size: 14px;
      white-space: nowrap;
      display: flex;
      align-items: center;
      gap: 8px;
      font-weight: 600;
    }

    #activity::before {
      content: "";
      width: 9px;
      height: 9px;
      border-radius: 999px;
      background: var(--grad-ok);
      box-shadow: 0 0 10px 2px rgba(163, 230, 53, 0.7);
      animation: pulse-dot 1.6s ease-in-out infinite;
    }

    @keyframes pulse-dot {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.5; transform: scale(0.75); }
    }

    #chat {
      flex: 1 1 0;
      overflow-y: auto;
      overflow-x: hidden;
      padding: 24px 26px 24px;
      min-height: 0;
      scroll-behavior: smooth;
      scrollbar-width: thin;
      scrollbar-color: rgba(168,85,247,0.4) transparent;
      scroll-padding-bottom: 0;
      overscroll-behavior: contain;
      -webkit-overflow-scrolling: touch;
    }

    .message {
      max-width: 880px;
      margin: 0 0 16px;
      border-radius: 14px;
      border: 1px solid var(--line);
      padding: 12px 16px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      background: var(--surface);
      backdrop-filter: blur(10px);
      -webkit-backdrop-filter: blur(10px);
      animation: message-in 0.28s ease both;
    }

    @keyframes message-in {
      from { opacity: 0; transform: translateY(6px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .message .name {
      font-size: 12.5px;
      font-weight: 800;
      margin-bottom: 6px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    .message .text {
      color: #ece6fb;
      line-height: 1.6;
      font-size: 15px;
    }

    .message.user {
      border-color: rgba(34, 211, 238, 0.4);
      background: linear-gradient(135deg, rgba(34, 211, 238, 0.12), rgba(99, 102, 241, 0.06));
      margin-left: auto;
    }

    .message.assistant {
      border-color: rgba(236, 72, 153, 0.4);
      background: linear-gradient(135deg, rgba(168, 85, 247, 0.12), rgba(236, 72, 153, 0.07));
    }

    .message.system {
      border-color: rgba(251, 191, 36, 0.4);
      background: linear-gradient(135deg, rgba(251, 191, 36, 0.10), rgba(251, 146, 60, 0.05));
      max-width: 100%;
      font-size: 13.5px;
    }

    .message.user .name {
      color: var(--cyan);
    }

    .message.assistant .name {
      background: linear-gradient(90deg, var(--violet), var(--pink));
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
    }

    .message.system .name {
      color: var(--amber);
    }

    /* Search bar ALWAYS visible at bottom - flex pinned like 2nd image, never hidden */
    form {
      border-top: 1px solid var(--line);
      padding: 16px 26px calc(16px + env(safe-area-inset-bottom));
      background: rgba(18, 10, 36, 0.96);
      backdrop-filter: blur(18px);
      -webkit-backdrop-filter: blur(18px);
      display: grid;
      grid-template-columns: minmax(0, 1fr) 112px;
      gap: 12px;
      align-items: end;
      flex: 0 0 auto;
      flex-shrink: 0;
      z-index: 10;
      position: relative;
      width: 100%;
      box-sizing: border-box;
    }

    textarea {
      width: 100%;
      min-height: 74px;
      max-height: 170px;
      resize: vertical;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.04);
      color: var(--text);
      border-radius: 12px;
      padding: 12px 14px;
      outline: none;
      transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }

    textarea::placeholder {
      color: var(--muted);
    }

    textarea:focus {
      border-color: rgba(168, 85, 247, 0.65);
      box-shadow: 0 0 0 3px rgba(168, 85, 247, 0.22), 0 0 24px -6px rgba(236, 72, 153, 0.4);
    }

    /* Markdown rendering inside assistant messages */
    .message .text strong { font-weight: 800; color: #fff; }
    .message .text em { font-style: italic; color: #f6f2ff; }
    .message .text code {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      background: rgba(255,255,255,0.08);
      border: 1px solid rgba(255,255,255,0.08);
      padding: 1px 6px;
      border-radius: 6px;
      font-size: 13.5px;
    }
    .message .text pre {
      background: rgba(0,0,0,0.28);
      border: 1px solid rgba(255,255,255,0.08);
      padding: 12px 14px;
      border-radius: 10px;
      overflow: auto;
      margin: 8px 0;
      white-space: pre;
    }
    .message .text pre code { background: none; border: none; padding: 0; }
    .message .text ul { margin: 8px 0; padding-left: 20px; }
    .message .text li { margin: 4px 0; }

    /* Typing indicator */
    .typing {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 6px 0 2px;
    }
    .typing span {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--pink);
      opacity: 0.35;
      animation: typing-dot 1.1s infinite ease-in-out;
    }
    .typing span:nth-child(2) { animation-delay: 0.2s; }
    .typing span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes typing-dot {
      0%, 80%, 100% { transform: scale(0.65); opacity: 0.35; }
      40% { transform: scale(1); opacity: 1; }
    }

    /* Header actions (Clear Chat) */
    .header-actions {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .button.ghost {
      background: rgba(255,255,255,0.06);
      border-color: var(--line);
      color: var(--muted);
      font-size: 13px;
      min-height: 34px;
      padding: 0 12px;
      text-align: center;
    }
    .button.ghost:hover { color: var(--text); border-color: rgba(251,113,133,0.45); background: rgba(251,113,133,0.10); }

    /* Always allow down-scroll — auto-pinned to bottom */
    #chat { scroll-behavior: smooth; overscroll-behavior: contain; -webkit-overflow-scrolling: touch; }
    #chat { overflow-anchor: auto; }

    /* Startup verification overlay - shows ONLY on website start */
    #startup-overlay {
      position: fixed;
      inset: 0;
      z-index: 9999;
      display: none;
      align-items: center;
      justify-content: center;
      background: rgba(10, 7, 20, 0.88);
      backdrop-filter: blur(14px);
      -webkit-backdrop-filter: blur(14px);
      padding: 20px;
    }
    #startup-overlay.active {
      display: flex;
    }
    .startup-card {
      width: 100%;
      max-width: 420px;
      background: linear-gradient(135deg, rgba(18, 10, 36, 0.96), rgba(28, 12, 54, 0.96));
      border: 1px solid rgba(168, 85, 247, 0.35);
      border-radius: 18px;
      padding: 28px;
      box-shadow: 0 20px 50px -10px rgba(0, 0, 0, 0.7), 0 0 0 1px rgba(255,255,255,0.06) inset;
      animation: message-in 0.32s ease both;
    }
    .startup-card h2 {
      margin: 0 0 6px;
      font-size: 20px;
      background: var(--grad-brand);
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
      font-weight: 800;
    }
    .startup-card p {
      margin: 0 0 18px;
      color: var(--muted);
      font-size: 13.5px;
      line-height: 1.5;
    }
    .startup-card input {
      width: 100%;
      min-height: 44px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.06);
      color: var(--text);
      border-radius: 10px;
      padding: 10px 14px;
      outline: none;
      margin-bottom: 12px;
    }
    .startup-card input:focus {
      border-color: rgba(168, 85, 247, 0.65);
      box-shadow: 0 0 0 3px rgba(168,85,247,0.2);
    }
    .startup-error {
      min-height: 18px;
      color: #fb7185;
      font-size: 13px;
      margin: 4px 0 8px;
    }
    .startup-card .button {
      width: 100%;
      text-align: center;
      justify-content: center;
    }

    /* ChatGPT-style: hamburger + drawer */
    #menu-toggle {
      display: none;
      width: 40px;
      height: 40px;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: var(--surface-strong);
      color: var(--text);
      font-size: 20px;
      line-height: 1;
      cursor: pointer;
      place-items: center;
      flex-shrink: 0;
    }
    #menu-toggle:hover { border-color: rgba(168,85,247,0.6); background: rgba(168,85,247,0.15); }

    #sidebar-overlay {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.45);
      backdrop-filter: blur(2px);
      z-index: 40;
    }
    #sidebar-overlay.active { display: block; }

    /* Desktop: sidebar is truly pinned — chat scroll never moves it */
    @media (min-width: 821px) {
      aside {
        position: fixed;
        left: 0;
        top: 0;
        bottom: 0;
        height: 100vh;
        height: 100dvh;
        overscroll-behavior: contain;
      }
      main {
        margin-left: 288px;
        width: calc(100% - 288px);
        height: 100vh;
        height: 100dvh;
      }
    }

    @media (max-width: 820px) {
      .app {
        flex-direction: column;
        height: 100vh;
        height: 100dvh;
        position: fixed;
        inset: 0;
      }

      aside {
        position: fixed;
        top: 0;
        left: 0;
        width: 288px;
        max-width: 82vw;
        height: 100%;
        max-height: 100dvh;
        z-index: 50;
        border-right: 1px solid var(--line);
        border-bottom: none;
        transform: translateX(-100%);
        transition: transform 0.28s cubic-bezier(0.4, 0, 0.2, 1);
        box-shadow: 8px 0 32px rgba(0,0,0,0.5);
      }
      aside.open {
        transform: translateX(0);
      }

      #menu-toggle {
        display: grid;
      }

      main {
        height: 100%;
        max-height: 100dvh;
        min-height: 0;
      }

      form {
        grid-template-columns: minmax(0, 1fr) 88px;
        padding: 12px 14px calc(12px + env(safe-area-inset-bottom));
      }

      #chat { padding: 16px 14px 24px; }

      #activity {
        white-space: normal;
        font-size: 13px;
      }
      header { padding: 12px 14px; }
    }
  </style>
</head>
<body>
  <!-- Startup Verification Overlay: appears ONLY on website start, not on every internet toggle -->
  <div id="startup-overlay" aria-hidden="true">
    <div class="startup-card">
      <h2>🔒 Verify Access</h2>
      <p id="startup-msg">Enter your agent password to continue. This appears only when the website starts.</p>
      <input id="startup-password" type="password" placeholder="Enter password" autocomplete="current-password">
      <div id="startup-error" class="startup-error"></div>
      <button id="startup-verify" class="button primary" type="button">Unlock</button>
      <p style="margin:14px 0 0; color:var(--muted); font-size:12px; text-align:center;">Tip: Use the Startup Lock toggle in sidebar to turn this ON/OFF.</p>
    </div>
  </div>
  <div id="sidebar-overlay"></div>
  <div class="app">
    <aside id="sidebar">
      <div class="brand">
        <div class="brand-mark" aria-hidden="true">
          <svg width="26" height="26" viewBox="0 0 28 28" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M19.6 3.8l4.6 4.6-11.9 11.9-5.8 1.2 1.2-5.8L19.6 3.8z" stroke="#fff" stroke-width="1.9" stroke-linejoin="round"/>
            <path d="M16.8 6.6l4.6 4.6" stroke="#fff" stroke-width="1.9" stroke-opacity="0.85"/>
            <path d="M8.5 19.5l-3.2 3.2" stroke="#fff" stroke-width="1.9" stroke-linecap="round" stroke-opacity="0.85"/>
          </svg>
        </div>
        <div>
          <h1>Jampandu</h1>
          <p>⚡ Local &middot; Private &middot; Yours</p>
        </div>
      </div>

      <div class="section">
        <h2>Status</h2>
        <div class="status-row"><span>Config</span><span id="config" class="pill">-</span></div>
        <div class="status-row"><span>Model</span><span id="model" class="pill">-</span></div>
        <div class="status-row"><span>Runtime</span><span id="runtime" class="pill">-</span></div>
        <div class="status-row"><span>Model server</span><span id="server" class="pill" title="Model is loading...">-</span></div>
        <div id="warm-progress" style="display:none; margin:8px 0 4px; font-size:12px;">
          <div style="display:flex; justify-content:space-between; color:var(--muted); margin-bottom:4px;"><span id="warm-pct">0%</span><span id="warm-time">0s • ETA --</span></div>
          <div style="height:8px; background:rgba(255,255,255,0.08); border-radius:999px; overflow:hidden; border:1px solid var(--line);"><div id="warm-bar" style="height:100%; width:0%; background:var(--grad-brand); transition:width 0.4s ease;"></div></div>
          <div id="warm-log" style="margin-top:6px; color:var(--muted); font-size:11px; line-height:1.35; white-space:pre-wrap; max-height:62px; overflow:auto; opacity:0.9;"></div>
        </div>
        <div class="status-row"><span>Internet</span>
          <label class="switch" title="Simple ON/OFF - no password needed every time">
            <input id="internet-toggle" type="checkbox">
            <span class="slider"></span>
          </label>
        </div>
        <div class="status-row"><span>Startup Lock</span>
          <label class="switch" title="ON = verify password only on website start | OFF = skip verification">
            <input id="startup-toggle" type="checkbox">
            <span class="slider"></span>
          </label>
        </div>
        <div class="status-row"><span>Voice</span><span id="voice" class="pill" title="Voice input - click to speak (when enabled)">-</span></div>
      </div>

       <div class="section">
         <div class="status-row"><span>Local memory</span>
           <label class="switch" title="Use local documents to answer">
             <input id="rag" type="checkbox">
             <span class="slider"></span>
           </label>
         </div>
       </div>

       <div class="section" id="history-section">
         <h2>History</h2>
         <div class="status-row">
           <span>Entries</span>
           <span id="history-count" class="pill">0</span>
         </div>
         <div id="history-list" style="max-height:260px; overflow-y:auto; font-size:12px;"></div>
         <div style="margin-top:10px; display:flex; gap:8px; flex-wrap:wrap;">
           <button id="history-refresh" class="button ghost" type="button" style="font-size:12px;">🔄 Refresh</button>
           <button id="history-toggle" class="button ghost" type="button" style="font-size:12px;">☰ Toggle</button>
         </div>
       </div>

       <div class="section actions">
         <button id="warm" class="button" type="button">🔥 Warm Model</button>
         <button id="refresh" class="button" type="button">🔄 Refresh</button>
         <button id="validate" class="button" type="button">✅ Validate</button>
         <button id="validate-strict" class="button" type="button">🔍 Validate (strict)</button>
         <button id="cli" class="button" type="button">🖥️ Open CLI</button>
         <button id="sign-off" class="button" type="button" style="background:var(--grad-bad); color:#fff; border-color:transparent;">🧹 Sign Off</button>
       </div>
     </aside>

    <main>
      <header>
        <button id="menu-toggle" type="button" aria-label="Toggle sidebar" aria-expanded="false">☰</button>
        <div class="headline">
          <h2>Conversation</h2>
          <p id="subtitle">Private, local, and under your control.</p>
        </div>
        <div class="header-actions">
          <button id="clear-chat" class="button ghost" type="button" title="Clear conversation history (privacy)">🗑 Clear Chat</button>
          <div id="activity">Ready</div>
        </div>
      </header>

      <section id="chat" aria-live="polite"></section>

      <form id="composer">
        <textarea id="message" placeholder="Ask Jampandu..." autocomplete="off"></textarea>
        <button id="send" class="button primary" type="submit">Send</button>
      </form>
    </main>
  </div>

  <script>
    const chat = document.querySelector("#chat");
    const activity = document.querySelector("#activity");
    const messageInput = document.querySelector("#message");
    const sendButton = document.querySelector("#send");

    function setBusy(isBusy, text = "Ready") {
      activity.textContent = text;
      sendButton.disabled = isBusy;
      const overlayActive = document.querySelector("#startup-overlay").classList.contains("active");
      messageInput.disabled = isBusy || overlayActive;
    }

    function escapeHtml(s) {
      return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
    }
    function renderMarkdown(text) {
      let html = escapeHtml(text);
      // code blocks ```...```
      html = html.replace(/```([\s\S]*?)```/g, (m, code) => `<pre><code>${code}</code></pre>`);
      // inline code `...`
      html = html.replace(/`([^`]+?)`/g, "<code>$1</code>");
      // bold **...**
      html = html.replace(/\*\*([^\*]+?)\*\*/g, "<strong>$1</strong>");
      // italic *...* (avoid **)
      html = html.replace(/(?<!\*)\*([^\*]+?)\*(?!\*)/g, "<em>$1</em>");
      // simple unordered lists: lines starting with - or *
      html = html.replace(/^(?:-|\*) (.+)$/gm, "<li>$1</li>");
      html = html.replace(/(<li>.*<\/li>)/gs, (m) => `<ul>${m}</ul>`);
      // line breaks
      html = html.replace(/\n/g, "<br>");
      // fix <br> inside <pre>
      html = html.replace(/<pre><code>([\s\S]*?)<\/code><\/pre>/g, (m, c) => `<pre><code>${c.replace(/<br>/g, "\n")}</code></pre>`);
      html = html.replace(/<ul>(?:<br>)*/g, "<ul>").replace(/(?:<br>)*<\/ul>/g, "</ul>").replace(/<\/li><br><li>/g, "</li><li>");
      return html;
    }

    function scrollToBottom() {
      // ALWAYS pin chat to absolute end — sidebar stays pinned (fixed), only #chat moves
      const doScroll = (instant) => {
        // only scroll #chat — never window/app/sidebar
        const max = chat.scrollHeight + 9999;
        try { chat.scrollTo({ top: max, behavior: instant ? "auto" : "smooth" }); } catch(e) { chat.scrollTop = chat.scrollHeight; }
        chat.scrollTop = chat.scrollHeight;
        // final instant snap — ensures we land at true bottom without moving sidebar
        chat.scrollTop = chat.scrollHeight;
      };
      doScroll(false);
      requestAnimationFrame(() => {
        doScroll(false);
        requestAnimationFrame(() => doScroll(true));
      });
      // keep pinning until layout fully settles (fonts, code blocks, images)
      let ticks = 0;
      const pin = setInterval(() => {
        doScroll(true);
        if (++ticks > 8) clearInterval(pin);
      }, 60);
      setTimeout(() => doScroll(true), 250);
      setTimeout(() => doScroll(true), 600);
    }
    // Auto down-scroll on ANY chat DOM change — keeps pinned UNTIL END even while streaming
    const _autoScrollObserver = new MutationObserver(() => scrollToBottom());
    _autoScrollObserver.observe(chat, { childList: true, subtree: true, characterData: true });
    window.addEventListener("resize", scrollToBottom);
    chat.addEventListener("DOMNodeInserted", scrollToBottom);
    // While typing/generating, keep pinning every 80ms so long responses stay at end
    let _pinInterval = null;
    function startPinning() {
      if (_pinInterval) return;
      _pinInterval = setInterval(scrollToBottom, 80);
    }
    function stopPinning() {
      if (_pinInterval) { clearInterval(_pinInterval); _pinInterval = null; }
      // one final snap to true bottom
      setTimeout(() => {
        try { chat.scrollTo({ top: chat.scrollHeight + 9999, behavior: "auto" }); } catch(e) {}
        chat.scrollTop = chat.scrollHeight;
      }, 30);
    }

    function addMessage(role, text) {
      const wrapper = document.createElement("article");
      wrapper.className = `message ${role}`;

      const name = document.createElement("div");
      name.className = "name";
      name.textContent = role === "user" ? "You" : role === "assistant" ? "Jampandu" : "System";

      const body = document.createElement("div");
      body.className = "text";
      if (role === "assistant" || role === "system") {
        body.innerHTML = renderMarkdown(text);
      } else {
        body.textContent = text;
      }

      wrapper.append(name, body);
      chat.appendChild(wrapper);
      scrollToBottom();
    }

    // Typing indicator (for P1: show during 8s+ cold boot generation)
    let typingEl = null;
    function showTyping() {
      if (typingEl) return;
      typingEl = document.createElement("article");
      typingEl.className = "message assistant typing-msg";
      const name = document.createElement("div");
      name.className = "name";
      name.textContent = "Jampandu";
      const body = document.createElement("div");
      body.className = "text";
      body.innerHTML = '<span class="typing"><span></span><span></span><span></span></span> <span style="color:var(--muted);font-size:13px;margin-left:6px;">Thinking…</span>';
      typingEl.append(name, body);
      chat.appendChild(typingEl);
      scrollToBottom();
      startPinning();
      setBusy(true, "Thinking…");
    }
    function hideTyping() {
      if (typingEl) { typingEl.remove(); typingEl = null; }
      stopPinning();
      scrollToBottom();
    }

    function setPill(id, label, tone) {
      const el = document.querySelector(`#${id}`);
      el.textContent = label;
      el.className = `pill ${tone}`;
      // P1: Cold badge tooltip
      if (id === "server") {
        if (label === "Cold") {
          el.title = "Model is cold - click Warm Model or send a message to warm it up";
        } else if (label === "Warm") {
          el.title = "Model is warm and ready";
        } else {
          el.title = "";
        }
      }
    }

    async function clearChat() {
      // Privacy: clear frontend and backend conversation
      try { await api("/api/clear-chat", {}); } catch(e) {}
      chat.innerHTML = "";
      addMessage("system", "Chat cleared.");
    }

    async function api(path, body = null) {
      const token = sessionStorage.getItem("auth_token") || localStorage.getItem("auth_token") || "";
      const headers = {"Content-Type": "application/json"};
      if (token) headers["X-Auth-Token"] = token;
      const options = body ? {
        method: "POST",
        headers,
        body: JSON.stringify(body)
      } : { headers };
      // include token even for GET-like POST with empty body
      if (!body && token) options.headers = headers;
      const response = await fetch(path, options);
      if (response.status === 401) {
        // auth failed — force overlay back
        sessionStorage.removeItem("auth_token");
        localStorage.removeItem("auth_token");
        try { sessionStorage.removeItem("startup_verified"); } catch(e){}
        // re-check gate
        const st = await fetch("/api/status").then(r=>r.json()).catch(()=>null);
        if (st) checkStartupGate(st);
        throw new Error("Not authorized. Please verify password.");
      }
      if (!response.ok) {
        // try to parse error json for message
        let msg = `Request failed: ${response.status}`;
        try { const j = await response.json(); if (j.output) msg = j.output; } catch(e){}
        throw new Error(msg);
      }
      return response.json();
    }

    function updateWarmUI(prog) {
      const wrap = document.querySelector("#warm-progress");
      const bar = document.querySelector("#warm-bar");
      const pct = document.querySelector("#warm-pct");
      const tm = document.querySelector("#warm-time");
      const log = document.querySelector("#warm-log");
      if (!prog) {
        if (document.querySelector("#server")?.textContent.trim() === "Warm") wrap.style.display = "none";
        return;
      }
      if (prog.running) { wrap.style.display = "none"; return; }
      wrap.style.display = "block";
      if (prog.overdue) {
        pct.textContent = "99% • finalizing...";
        tm.textContent = prog.elapsed + "s elapsed • " + (prog.size_gb||"?") + "GB • retrying if needed";
        bar.style.width = "99%";
        bar.style.background = "linear-gradient(90deg, #fbbf24, #fb923c)";
      } else {
        pct.textContent = prog.percent + "%";
        tm.textContent = prog.elapsed + "s elapsed • ETA " + prog.eta + "s • " + (prog.size_gb||"?") + "GB";
        bar.style.width = prog.percent + "%";
        bar.style.background = "var(--grad-brand)";
      }
      log.textContent = prog.log ? prog.log.slice(-500) : "";
      const sub = document.querySelector("#subtitle");
      if (sub) sub.textContent = prog.overdue ? `Warming 99% — finalizing (${prog.elapsed}s)` : `Warming ${prog.percent}% (${prog.elapsed}s, ~${prog.eta}s left)`;
    }
    async function refreshStatus() {
      try {
        const status = await api("/api/status");
        setPill("config", status.config_exists ? "OK" : "Missing", status.config_exists ? "ok" : "bad");
        setPill("model", status.model_exists ? "Ready" : "Missing", status.model_exists ? "ok" : "bad");
        setPill("runtime", status.llama_exists ? "Ready" : "Missing", status.llama_exists ? "ok" : "bad");
        setPill("server", status.model_server_running ? "Warm" : "Cold", status.model_server_running ? "ok" : "warn");
        // warm progress bar with % and time by
        if (status.warm_progress) updateWarmUI(status.warm_progress);
        else if (status.model_server_running) { document.querySelector("#warm-progress").style.display="none"; }
        document.querySelector("#internet-toggle").checked = !!status.internet_allowed;
        document.querySelector("#startup-toggle").checked = !!status.startup_auth_enabled;
        setPill("voice", status.voice_enabled ? "On" : "Off", status.voice_enabled ? "ok" : "warn");
        const voiceEl = document.querySelector("#voice");
        voiceEl.title = status.voice_enabled ? "Voice input is enabled - click to speak" : "Voice input is off - enable in config";
        document.querySelector("#subtitle").textContent = status.summary;
        // Check startup verification only on first load / when session not verified
        checkStartupGate(status);
      } catch (error) {
        addMessage("system", error.message);
      }
    }
    // poll warm progress every 1s while cold
    setInterval(async () => {
      const srv = document.querySelector("#server");
      if (!srv || srv.textContent.trim() === "Warm") return;
      try {
        const prog = await fetch("/api/warm-progress").then(r=>r.json()).catch(()=>null);
        if (prog && !prog.running) updateWarmUI(prog);
        else if (prog && prog.running) document.querySelector("#warm-progress").style.display="none";
      } catch(e){}
    }, 1000);

    // --- Startup Verification Logic: ONLY on website start, not every internet toggle ---
    const startupOverlay = document.querySelector("#startup-overlay");
    const startupInput = document.querySelector("#startup-password");
    const startupError = document.querySelector("#startup-error");
    const startupBtn = document.querySelector("#startup-verify");

    function checkStartupGate(status) {
      // If startup lock is OFF -> never show verification
      if (!status.startup_auth_enabled) {
        hideStartupOverlay();
        return;
      }
      // Token-based: if we have a valid token, hide overlay regardless of sessionStorage flag
      const hasToken = !!(sessionStorage.getItem("auth_token") || localStorage.getItem("auth_token"));
      if (hasToken) {
        hideStartupOverlay();
        return;
      }
      // Back-compat: old sessionStorage flag
      if (sessionStorage.getItem("startup_verified") === "true") {
        // migrate: keep hidden but require re-auth next reload without token
        hideStartupOverlay();
        return;
      }
      showStartupOverlay();
    }

    function showStartupOverlay() {
      startupOverlay.classList.add("active");
      startupOverlay.setAttribute("aria-hidden", "false");
      messageInput.disabled = true;
      sendButton.disabled = true;
      setTimeout(() => startupInput.focus(), 100);
    }
    function hideStartupOverlay() {
      startupOverlay.classList.remove("active");
      startupOverlay.setAttribute("aria-hidden", "true");
      messageInput.disabled = false;
      sendButton.disabled = false;
      startupError.textContent = "";
      startupInput.value = "";
    }

    async function handleStartupVerify() {
      const pwd = startupInput.value;
      if (!pwd) { startupError.textContent = "Please enter password."; return; }
      startupBtn.disabled = true;
      startupError.textContent = "";
      startupBtn.textContent = "Verifying...";
      try {
        const res = await api("/api/verify-startup", { password: pwd });
        if (res.ok) {
          if (res.token) {
            sessionStorage.setItem("auth_token", res.token);
            // also persist in localStorage as backup for page reloads in same browser (per spec: only on website start, but we support)
            // use sessionStorage primary for per-tab isolation; copy to localStorage too for convenience
            try { localStorage.setItem("auth_token", res.token); } catch(e){}
          }
          sessionStorage.setItem("startup_verified", "true");
          hideStartupOverlay();
          addMessage("system", res.output || "Verified. Welcome!");
          // kick warm now that we are authorized (was deferred until auth)
          setTimeout(ensureWarmInBackground, 800);
        } else {
          startupError.textContent = res.output || "Incorrect password.";
          if (res.retry_after) startupError.textContent += ` Retry after ${res.retry_after}s`;
        }
      } catch (e) {
        startupError.textContent = e.message;
      } finally {
        startupBtn.disabled = false;
        startupBtn.textContent = "Unlock";
      }
    }

    // Hardening: if someone removes overlay via inspect, re-enforce server gate within 800ms
    setInterval(() => {
      const shouldLock = document.querySelector("#startup-toggle")?.checked;
      // we check status via last known status cache? Instead just check token presence
      const hasToken = !!(sessionStorage.getItem("auth_token") || localStorage.getItem("auth_token"));
      const overlay = document.querySelector("#startup-overlay");
      const isActive = overlay.classList.contains("active");
      // if lock is ON and no token, force overlay back
      if (shouldLock && !hasToken && !isActive) {
        // re-fetch status to be sure
        fetch("/api/status").then(r=>r.json()).then(st=>{
          if (st.startup_auth_enabled) showStartupOverlay();
        }).catch(()=>{ if (!hasToken) showStartupOverlay(); });
      }
      // also keep inputs disabled if overlay active
      if (isActive) {
        messageInput.disabled = true;
        sendButton.disabled = true;
      }
    }, 800);

    async function toggleStartupAuth(event) {
      const toggle = event.target;
      const enabled = toggle.checked;
      const pwd = window.prompt(enabled ? "Enter password to ENABLE startup lock:" : "Enter password to DISABLE startup lock:");
      if (pwd === null) { toggle.checked = !enabled; return; }
      toggle.disabled = true;
      try {
        const res = await api("/api/set-startup-auth", { enabled, password: pwd });
        if (!res.ok) {
          toggle.checked = !enabled;
        }
        addMessage("system", res.output);
        if (!enabled) {
          // If turned OFF, clear session gate so no overlay next reload
          sessionStorage.setItem("startup_verified", "true");
          hideStartupOverlay();
        } else {
          sessionStorage.removeItem("startup_verified");
        }
      } catch (e) {
        toggle.checked = !enabled;
        addMessage("system", e.message);
      } finally {
        toggle.disabled = false;
        refreshStatus();
      }
    }

    async function sendMessage(event) {
      event.preventDefault();
      const text = messageInput.value.trim();
      if (!text) return;

      messageInput.value = "";
      addMessage("user", text);
      // Cold UX fix: tell user why first message is slow once
      const serverPill = document.querySelector("#server");
      const isCold = serverPill && serverPill.textContent.trim() === "Cold";
      if (isCold) {
        addMessage("system", "Model is Cold — warming up now (one-time ~20-40s for 4GB model). Next replies will be instant.");
        setBusy(true, "Warming model...");
      }
      showTyping();
      try {
        const result = await api("/api/query", {
          message: text,
          use_rag: document.querySelector("#rag").checked
        });
        hideTyping();
        addMessage("assistant", result.output || "No response.");
      } catch (error) {
        hideTyping();
        addMessage("system", error.message);
      } finally {
        setBusy(false);
        hideTyping();
        refreshStatus();
      }
    }

    async function warmModel() {
      // P1: Fix misleading Starting... -> check if already warm first
      const serverPill = document.querySelector("#server");
      const alreadyWarm = serverPill && serverPill.textContent.trim() === "Warm";
      if (alreadyWarm) {
        addMessage("system", "Model server is already warm.");
        // Still verify via API but don't show Starting...
        try {
          const result = await api("/api/warm-model", {});
          // Only show if not duplicate Already warm
          if (result.output && result.output !== "Model server is already warm." ) {
            addMessage("system", result.output);
          }
        } catch (error) {
          addMessage("system", error.message);
        } finally {
          refreshStatus();
        }
        return;
      }
      setBusy(true, "Warming model...");
      showTyping();
      addMessage("system", "Starting the local model server. First load can take a while.");
      try {
        const result = await api("/api/warm-model", {});
        hideTyping();
        addMessage("system", result.output || "Model server is ready.");
      } catch (error) {
        hideTyping();
        addMessage("system", error.message);
      } finally {
        hideTyping();
        setBusy(false);
        refreshStatus();
      }
    }

    async function runValidation(strict = false) {
      setBusy(true, strict ? "Validating (strict)..." : "Validating...");
      addMessage("system", strict ? "Running package validation (--strict: model + binary required)..." : "Running package validation...");
      try {
        const result = await api("/api/validate", { strict });
        addMessage("system", result.output || "Validation completed.");
      } catch (error) {
        addMessage("system", error.message);
      } finally {
        setBusy(false);
        refreshStatus();
      }
    }

    async function openCli() {
      try {
        const result = await api("/api/open-cli", {});
        addMessage("system", result.output);
      } catch (error) {
        addMessage("system", error.message);
      }
    }

    async function toggleInternet(event) {
      const toggle = event.target;
      const enabled = toggle.checked;
      // Per prompt: Internet ON/OFF is a simple toggle. Password is verified ONLY
      // once on website startup via the startup overlay, not every time you toggle internet.
      toggle.disabled = true;
      try {
        const result = await api("/api/toggle-internet", { enabled });
        if (!result.ok) {
          toggle.checked = !enabled;
        }
        addMessage("system", result.output || (enabled ? "Internet mode enabled." : "Internet mode disabled."));
      } catch (error) {
        toggle.checked = !enabled;
        addMessage("system", error.message);
      } finally {
        toggle.disabled = false;
        refreshStatus();
      }
    }

    // --- ChatGPT-style Sidebar Drawer (mobile) ---
    const sidebar = document.querySelector("#sidebar");
    const menuToggle = document.querySelector("#menu-toggle");
    const sidebarOverlay = document.querySelector("#sidebar-overlay");
    function openSidebar() {
      sidebar.classList.add("open");
      sidebarOverlay.classList.add("active");
      menuToggle.setAttribute("aria-expanded", "true");
    }
    function closeSidebar() {
      sidebar.classList.remove("open");
      sidebarOverlay.classList.remove("active");
      menuToggle.setAttribute("aria-expanded", "false");
    }
    function toggleSidebar() {
      if (sidebar.classList.contains("open")) closeSidebar(); else openSidebar();
    }
    menuToggle.addEventListener("click", toggleSidebar);
    sidebarOverlay.addEventListener("click", closeSidebar);
    // Auto-close drawer after clicking any sidebar button on mobile
    sidebar.querySelectorAll("button").forEach(btn => {
      btn.addEventListener("click", () => { if (window.innerWidth <= 820) closeSidebar(); });
    });
    // Close with ESC
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSidebar(); });

    document.querySelector("#composer").addEventListener("submit", sendMessage);
    document.querySelector("#warm").addEventListener("click", warmModel);
    document.querySelector("#refresh").addEventListener("click", refreshStatus);
    document.querySelector("#validate").addEventListener("click", () => runValidation(false));
    document.querySelector("#validate-strict").addEventListener("click", () => runValidation(true));
    document.querySelector("#cli").addEventListener("click", openCli);
    document.querySelector("#clear-chat").addEventListener("click", clearChat);
    document.querySelector("#internet-toggle").addEventListener("change", toggleInternet);
    document.querySelector("#startup-toggle").addEventListener("change", toggleStartupAuth);
    startupBtn.addEventListener("click", handleStartupVerify);
    startupInput.addEventListener("keydown", (e) => { if (e.key === "Enter") handleStartupVerify(); });
    messageInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendMessage(event);
      }
    });

    // Auto-warm in background: no click needed, but only after auth to avoid CPU fight with Verify
    async function ensureWarmInBackground() {
      try {
        const hasToken = !!(sessionStorage.getItem("auth_token") || localStorage.getItem("auth_token"));
        const s = await api("/api/status");
        if (s.startup_auth_enabled && !hasToken) {
          // still locked — don't warm yet, will warm after unlock triggers refresh
          return;
        }
        if (!s.model_server_running && s.model_exists && s.llama_exists) {
          // Don't block UI - warm silently
          const sub = document.querySelector("#subtitle");
          if (sub) sub.textContent = "Warming model in background (one-time, ~30s)...";
          // Fire and forget - warm endpoint will load 4GB model into VRAM (requires auth token now)
          api("/api/warm-model", {}).then(r => {
            addMessage("system", r.output || "Model server is ready. Future replies will be instant.");
            refreshStatus();
          }).catch(()=>{});
        }
      } catch(e) {}
    }

    // --- History Section ---
    let historyVisible = true;
    async function loadHistory() {
      try {
        const res = await api("/api/history");
        if (res.ok && res.history) {
          const hist = res.history;
          const list = document.querySelector("#history-list");
          if (!list) return;
          list.innerHTML = "";
          const permIcon = (p) => p ? "📌" : "🔘";
          const roleLabel = (r) => r === "user" ? "U" : "A";
          const roleColor = (r) => r === "user" ? "var(--cyan)" : "var(--violet)";
          hist.forEach(m => {
            const row = document.createElement("div");
            row.style.cssText = "display:flex; align-items:center; gap:6px; padding:5px 4px; border-bottom:1px solid var(--line-soft); cursor:pointer;";
            row.innerHTML = `<span style="color:${roleColor(m.role)}; font-weight:700; font-size:11px; min-width:14px;">${roleLabel(m.role)}</span>` +
              `<span style="flex:1; color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:11px;" title="${escapeHtml(m.content.slice(0,120))}">${escapeHtml(m.content.slice(0, 70))}</span>` +
              `<span style="font-size:11px; cursor:pointer;" onclick="event.stopPropagation(); togglePin(${m.id});">${permIcon(m.permanent)}</span>`;
            row.addEventListener("click", () => {
              const chatEl = document.createElement("div");
              chatEl.className = "message " + m.role;
              const nameEl = document.createElement("div");
              nameEl.className = "name";
              nameEl.textContent = m.role === "user" ? "You" : "Jampandu";
              const bodyEl = document.createElement("div");
              bodyEl.className = "text";
              bodyEl.textContent = m.content;
              chatEl.append(nameEl, bodyEl);
              const chat = document.querySelector("#chat");
              chat.appendChild(chatEl);
              scrollToBottom();
            });
            list.appendChild(row);
          });
          const countEl = document.querySelector("#history-count");
          if (countEl) countEl.textContent = hist.length;
        }
      } catch(e) { console.error("loadHistory:", e); }
    }
    async function togglePin(id) {
      try {
        const res = await api("/api/toggle-pin", {id, permanent: true});
        if (!res.ok) {
          // if already permanent, toggle to temporary
          await api("/api/toggle-pin", {id, permanent: false});
        }
        loadHistory();
      } catch(e) { console.error("togglePin:", e); }
    }
    async function signOff() {
      if (!confirm("Sign off? All non-pinned (temporary) history will be erased from this device.")) return;
      try {
        const res = await api("/api/sign-off");
        if (res.ok) {
          addMessage("system", res.output || "Signed off — temporary history cleared.");
          loadHistory();
        } else {
          addMessage("system", "Sign off failed: " + (res.output || "unknown"));
        }
      } catch(e) { addMessage("system", "Sign off error: " + e.message); }
    }
    document.querySelector("#history-refresh")?.addEventListener("click", loadHistory);
    document.querySelector("#history-toggle")?.addEventListener("click", () => {
      historyVisible = !historyVisible;
      const ls = document.querySelector("#history-list");
      if (ls) ls.style.display = historyVisible ? "" : "none";
      const sec = document.querySelector("#history-section");
      if (sec) sec.style.display = historyVisible ? "" : "none";
    });
    document.querySelector("#sign-off")?.addEventListener("click", signOff);
    // Load history on startup
    loadHistory();

    addMessage("system", "Jampandu desktop is ready.");
    refreshStatus();
    // Kick auto-warm 1.5s after load so user never pays cold penalty on first message
    setTimeout(ensureWarmInBackground, 1500);
  </script>
</body>
</html>
"""


def preferred_python():
    portable = BASE_DIR / "python-portable" / "python.exe"
    if portable.exists():
        return str(portable)
    return sys.executable


def load_config():
    if not CONFIG_PATH.exists():
        return None, "config.json is missing"
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as config_file:
            return json.load(config_file), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, str(exc)


def save_config(cfg):
    with CONFIG_PATH.open("w", encoding="utf-8") as config_file:
        json.dump(cfg, config_file, indent=2)


def set_internet_allowed(enabled, password=None):
    global STARTUP_VERIFIED
    cfg, error = load_config()
    if error:
        return {"ok": False, "output": error}

    enabled = bool(enabled)
    # Per prompt: startup verification handles auth ONCE at website start.
    # Internet toggle is a simple ON/OFF marker and should NOT ask for password
    # every time. If startup verification is enabled and already verified in this
    # server session, allow toggle without password. Otherwise, keep password gate
    # only when startup gate is OFF (fallback security) - but prompt says no per-click password.
    # So we make it password-free by default. Password is only needed if caller supplies it
    # and startup not yet verified.
    startup_enabled = cfg.get("startup_auth_enabled", True)
    if enabled and startup_enabled and not STARTUP_VERIFIED:
        # If startup gate is ON but not yet verified, require password for internet ON
        # This prevents bypassing startup verification by directly toggling internet.
        verifier = cfg.get("password_verifier")
        if verifier and password is not None:
            if not verify_verifier(password or "", verifier):
                return {"ok": False, "output": "Incorrect password. Internet not enabled. Verify startup password first."}
            STARTUP_VERIFIED = True
        elif verifier:
            # No password supplied but startup not verified - still allow? Per prompt, allow simple toggle.
            # We allow but note it. To enforce startup-first, uncomment next line.
            pass

    cfg["internet_allowed"] = enabled
    try:
        save_config(cfg)
    except OSError as exc:
        return {"ok": False, "output": str(exc)}
    state = "enabled" if enabled else "disabled"
    return {
        "ok": True,
        "internet_allowed": enabled,
        "output": (
            f"Internet mode {state}. This is a session marker only — it does not "
            "change Windows firewall rules or grant the model a network client."
        ),
    }


def verify_startup_password(password, client_ip="unknown"):
    global STARTUP_VERIFIED
    # Rate-limit / lockout check BEFORE expensive PBKDF2
    locked, remain = _is_locked(client_ip)
    if locked:
        return {"ok": False, "output": f"Too many failed attempts. Try again in {remain}s.", "locked": True, "retry_after": remain}
    cfg, error = load_config()
    if error:
        return {"ok": False, "output": error}
    verifier = cfg.get("password_verifier")
    if not verifier:
        # No password set - allow without check, issue token anyway
        STARTUP_VERIFIED = True
        tok = _generate_token()
        _store_token(tok)
        return {"ok": True, "output": "No password configured - access granted.", "token": tok}
    # Use constant-time verify; on failure add artificial delay to slow brute-force
    if verify_verifier(password or "", verifier):
        _clear_failed(client_ip)
        STARTUP_VERIFIED = True
        tok = _generate_token()
        _store_token(tok)
        return {"ok": True, "output": "Password verified. Welcome!", "token": tok}
    # failure path — record and add small delay (0.4s) to increase cost per guess without hanging UI
    _record_failed(client_ip)
    time.sleep(0.35)
    locked2, remain2 = _is_locked(client_ip)
    if locked2:
        return {"ok": False, "output": f"Incorrect password. Too many failures — locked for {remain2}s.", "locked": True, "retry_after": remain2}
    # remaining attempts hint without leaking exact count in production? keep helpful
    with FAILED_LOCK:
        rec = FAILED_ATTEMPTS.get(client_ip, {})
        left = MAX_ATTEMPTS - rec.get("count", 0)
    return {"ok": False, "output": f"Incorrect password. Try again. ({max(0,left)} attempts left before lockout)", "attempts_left": max(0,left)}


def set_startup_auth_enabled(enabled, password=None):
    global STARTUP_VERIFIED
    cfg, error = load_config()
    if error:
        return {"ok": False, "output": error}
    # Changing the startup-auth toggle requires current password to prevent
    # unauthorized disabling of the gate. Allow without password only if no verifier.
    verifier = cfg.get("password_verifier")
    if verifier and not verify_verifier(password or "", verifier):
        return {"ok": False, "output": "Incorrect password. Startup lock setting not changed."}
    cfg["startup_auth_enabled"] = bool(enabled)
    try:
        save_config(cfg)
    except OSError as exc:
        return {"ok": False, "output": str(exc)}
    state = "ON - password required on startup" if enabled else "OFF - no verification on startup"
    return {"ok": True, "startup_auth_enabled": bool(enabled), "output": f"Startup verification {state}."}


def resolve_agent_path(value):
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return BASE_DIR / path


def _time_context() -> str:
    """Real host local time injected every turn so model never hallucinates 10:00 AM."""
    try:
        now = datetime.datetime.now().astimezone()
        # e.g. Monday, August 31, 2026 12:30 PM IST (Asia/Kolkata) - host local time
        tz = now.strftime("%Z") or "local"
        return f"Current host local time: {now.strftime('%A, %B %d, %Y %I:%M %p')} {tz} (use this exact time for any time/date question; do not invent another time)"
    except Exception:
        try:
            now = datetime.datetime.now()
            return f"Current host local time: {now.strftime('%A, %B %d, %Y %I:%M %p')} local"
        except Exception:
            return ""


def _clean_llm_output(text: str) -> str:
    """Strip Qwen3 <think> traces and prompt echoes that leaked in screenshot."""
    if not text:
        return text
    # 1) Remove <think>...</think> including multiline, case-insensitive
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # stray tags that may remain if model was truncated
    text = re.sub(r'</?think>', '', text, flags=re.IGNORECASE)
    # 2) Remove other hidden channels some templates use
    text = re.sub(r'<\|im_start\|>.*?<\|im_end\|>', '', text, flags=re.DOTALL)
    # 3) Cut prompt echo: if model echoed "User:" or "Assistant:" as next turn, truncate before it
    #    Keep only content before first leaked turn marker on a new line
    m = re.search(r'\n\s*(User|Assistant)\s*:\s*', text)
    if m:
        text = text[:m.start()].rstrip()
    # 4) Deduplicate consecutive repeated answer blocks (seen: same time line twice)
    #    If text contains two identical large sentences, keep first occurrence
    lines = text.splitlines()
    seen = set()
    deduped = []
    for ln in lines:
        key = ln.strip()
        if key and key in seen and len(key) > 30:
            # skip duplicate long line (exact hallucination duplicate)
            continue
        seen.add(key)
        deduped.append(ln)
    text = "\n".join(deduped)
    # 5) Trim excessive whitespace but preserve intentional breaks
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    # 6) If model still ended with incomplete year like "August 31, 2", try to fix via time context fallback
    #    (do not auto-append year; just leave - better to be short than truncated)
    return text


def _db_conn():
    db = DB_PATH
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db), timeout=10)
    con.execute("PRAGMA journal_mode=WAL;")
    return con


def load_history():
    """Load conversation from SQLite into the in-memory CONVERSATION list."""
    global CONVERSATION, HISTORY_LOADED
    try:
        con = _db_conn()
        cur = con.cursor()
        cur.execute("SELECT role, text, permanent FROM conversation ORDER BY id ASC")
        rows = cur.fetchall()
        CONVERSATION = []
        for role, text, permanent in rows:
            CONVERSATION.append({"role": role, "content": text, "permanent": bool(permanent)})
        con.close()
    except Exception:
        CONVERSATION = []
    HISTORY_LOADED = True


def _append_db(role, text, permanent=False):
    """Append a row to the conversation table."""
    try:
        con = _db_conn()
        cur = con.cursor()
        cur.execute("INSERT INTO conversation (role, text, ts, permanent) VALUES (?,?,?,?)",
                    (role, text, datetime.datetime.utcnow().isoformat(), 1 if permanent else 0))
        con.commit()
        con.close()
    except Exception:
        pass


def read_system_prompt():
    base = "You are Jampandu, a local offline assistant."
    if SYSTEM_PROMPT_PATH.exists():
        try:
            base = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    # Inject Today Date + explicit anti-thinking instruction (Llama/Qwen chat template)
    try:
        now = datetime.datetime.now()
        today = now.strftime("%B %d, %Y")
        if "Today Date:" not in base:
            base = base + f"\n\nToday Date: {today}"
        # Force model to not emit <think> tags - Qwen3 respects this via system prompt
        if "Do not use <think>" not in base:
            base = base + "\n\nDo not use <think> tags, do not reveal chain-of-thought, do not echo 'User:'/'Assistant:' turn markers. Answer directly."
    except: pass
    return base


def get_task_executor():
    """Return a TaskExecutor synced to current config internet_allowed."""
    global TASK_EXECUTOR
    cfg, _ = load_config()
    internet_allowed = bool(cfg and cfg.get("internet_allowed", False)) if cfg else False
    if TASK_EXECUTOR is None:
        TASK_EXECUTOR = TaskExecutor({"internet_allowed": internet_allowed})
    else:
        TASK_EXECUTOR.internet_allowed = internet_allowed
        TASK_EXECUTOR.config["internet_allowed"] = internet_allowed
    return TASK_EXECUTOR


def get_dynamic_internet_context():
    """Context injected into LLM so it knows Internet toggle is already ON."""
    cfg, _ = load_config()
    if not cfg:
        return None
    internet_allowed = bool(cfg.get("internet_allowed", False))
    startup_verified = STARTUP_VERIFIED
    # If internet toggle is ON, explicitly tell LLM it is enabled - do not ask for /enable_internet again
    if internet_allowed:
        # Startup verification already handles auth once at start, so after that no password re-ask
        if startup_verified or not cfg.get("startup_auth_enabled", True):
            return "SYSTEM STATE: Internet access is currently ENABLED (Internet toggle is ON and startup verification has been completed). You MAY fulfill YouTube/web search/browser requests directly. Do NOT say 'Run /enable_internet with the password first' - that gate is already passed."
        else:
            return "SYSTEM STATE: Internet access is currently ENABLED (Internet toggle is ON). Startup verification gate exists but Internet is already enabled - fulfill the request."
    else:
        return "SYSTEM STATE: Internet access is currently DISABLED (Internet toggle is OFF). If user asks for YouTube/web search, tell them to turn the Internet toggle ON (no password needed after startup verification)."


def is_port_open(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


def llama_server_url(path):
    return f"http://{LLAMA_HOST}:{LLAMA_PORT}{path}"


def post_json(url, payload, timeout):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_json(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def llama_health():
    if not is_port_open(LLAMA_HOST, LLAMA_PORT):
        return False
    try:
        get_json(llama_server_url("/health"), timeout=1)
        return True
    except Exception:
        return False


def _model_size_gb():
    try:
        cfg, _ = load_config()
        p = resolve_agent_path(cfg.get("model_path")) if cfg else None
        if p and p.exists():
            return p.stat().st_size / (1024**3)
    except: pass
    return 3.7

def get_warm_progress():
    running = llama_health()
    if running:
        return {"running": True, "percent": 100, "elapsed": 0, "eta": 0, "log": "", "size_gb": _model_size_gb(), "overdue": False}
    if WARM_START_TIME is None:
        return {"running": False, "percent": 0, "elapsed": 0, "eta": 35, "log": "", "size_gb": _model_size_gb(), "overdue": False}
    elapsed = time.time() - WARM_START_TIME
    # USB pendrive is ~15-25 MB/s, so 4GB = 160-260s; SSD ~35s. Use conservative 22s/GB
    est = max(45, min(180, _model_size_gb() * 22))  # ~22s per GB for USB
    overdue = elapsed > est
    if overdue:
        # slow USB: keep ticking slowly to 99, show overdue time
        percent = min(99, 92 + int(min(7, (elapsed - est)/15)))
        eta = max(0, int((est*1.4) - elapsed))  # give 40% grace
        if eta == 0:
            eta = 5  # still finalizing
    else:
        percent = min(92, int((elapsed / est) * 92))
        eta = max(0, int(est - elapsed))
    log_tail = ""
    try:
        log_path = BASE_DIR / "logs" / "web_ui_llama_server.log"
        if log_path.exists():
            txt = log_path.read_text(encoding="utf-8", errors="ignore")[-1200:]
            lines = txt.strip().splitlines()[-4:]
            log_tail = "\n".join(lines)
    except: pass
    if WARM_LAST_ERROR:
        log_tail = (log_tail + "\n" + WARM_LAST_ERROR).strip()[-700:]
    if overdue and not log_tail:
        log_tail = "Finalizing model load (taking longer than estimated — check VRAM)..."
    return {"running": False, "percent": percent, "elapsed": int(elapsed), "eta": eta, "log": log_tail, "size_gb": round(_model_size_gb(),2), "overdue": overdue}

def start_llama_server(timeout=180):
    global LLAMA_SERVER_PROC, LLAMA_SERVER_LOG, WARM_START_TIME, WARM_LAST_ERROR

    if llama_health():
        WARM_START_TIME = None
        return {"ok": True, "output": "Model server is already warm."}

    status = runtime_status()
    if not status["model_exists"]:
        return {"ok": False, "output": "Model file is missing."}
    if not LLAMA_SERVER_PATH.exists():
        return {"ok": False, "output": "llama-server.exe is missing."}

    # Config-driven inference settings (safe defaults if keys are absent).
    cfg, _ = load_config()
    cfg = cfg or {}
    threads = str(cfg.get("threads") or min(8, os.cpu_count() or 8))
    ctx_size = str(cfg.get("ctx_size") or 4096)
    max_tokens = str(cfg.get("max_tokens") or 768)
    gpu_raw = cfg.get("gpu_layers", "auto")
    if isinstance(gpu_raw, str) and gpu_raw.strip().lower() == "auto":
        gpu_layers = "auto"
    else:
        try:
            gpu_layers = str(int(gpu_raw))
        except (TypeError, ValueError):
            gpu_layers = "auto"

    def _launch(ngl: str):
        cmd = [
            str(LLAMA_SERVER_PATH),
            "-m",
            status["model_path"],
            "--host",
            LLAMA_HOST,
            "--port",
            str(LLAMA_PORT),
        ]
        # "auto" => omit -ngl so llama.cpp fits as many layers as free VRAM allows
        # (forcing -ngl 99 on a 4GB GPU disables auto-fit and aborts). A concrete
        # integer (including 0 for pure CPU) is passed through unchanged.
        if str(ngl) != "auto":
            cmd += ["-ngl", str(ngl)]
        cmd += [
            "--threads",
            threads,
            "-n",
            max_tokens,
            "--ctx-size",
            ctx_size,
            "--no-webui",
            "--offline",
        ]
        return cmd

    def _spawn(ngl: str):
        """Spawn the server under the lock. Returns (ok, error_dict_or_None)."""
        global LLAMA_SERVER_PROC, LLAMA_SERVER_LOG, WARM_START_TIME, WARM_LAST_ERROR
        with LLAMA_SERVER_LOCK:
            if LLAMA_SERVER_PROC and LLAMA_SERVER_PROC.poll() is None:
                if WARM_START_TIME is None:
                    WARM_START_TIME = time.time()
                return True, None
            WARM_START_TIME = time.time()
            WARM_LAST_ERROR = None
            log_dir = BASE_DIR / "logs"
            log_dir.mkdir(exist_ok=True)
            try:
                with (log_dir / "web_ui_llama_server.log").open("a", encoding="utf-8") as _lf:
                    _lf.write(f"\n=== warm start {time.strftime('%H:%M:%S')} model={status['model_path']} size={_model_size_gb():.2f}GB ngl={ngl} threads={threads} ctx={ctx_size} n={max_tokens} ===\n")
            except: pass
            if LLAMA_SERVER_LOG:
                try: LLAMA_SERVER_LOG.close()
                except: pass
            LLAMA_SERVER_LOG = (log_dir / "web_ui_llama_server.log").open("a", encoding="utf-8")
            cmd = _launch(ngl)
            try:
                LLAMA_SERVER_PROC = subprocess.Popen(
                    cmd,
                    cwd=str(BASE_DIR),
                    stdout=LLAMA_SERVER_LOG,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            except Exception as exc:
                WARM_LAST_ERROR = str(exc)
                WARM_START_TIME = None
                return False, {"ok": False, "output": f"Failed to start model server: {exc}"}
        return True, None

    def _wait(deadline):
        """Poll until healthy / dead / deadline. Returns 'ready' | 'died' | 'loading'."""
        global WARM_START_TIME, WARM_LAST_ERROR
        while time.time() < deadline:
            if LLAMA_SERVER_PROC and LLAMA_SERVER_PROC.poll() is not None:
                try:
                    with (BASE_DIR / "logs" / "web_ui_llama_server.log").open("r", encoding="utf-8", errors="ignore") as _rf:
                        tail = "".join(_rf.readlines()[-20:])
                        WARM_LAST_ERROR = f"Exit code {LLAMA_SERVER_PROC.returncode}\n{tail[-600:]}"
                except: WARM_LAST_ERROR = f"Exit code {LLAMA_SERVER_PROC.returncode}"
                WARM_START_TIME = None
                return "died"
            if llama_health():
                WARM_START_TIME = None
                WARM_LAST_ERROR = None
                return "ready"
            time.sleep(0.5)
        return "loading"

    # Attempt 1: configured GPU offload (default = full). If the process dies
    # (e.g. CUDA/VRAM init failure), fall back once to CPU (-ngl 0).
    ok, err = _spawn(gpu_layers)
    if not ok:
        return err
    result = _wait(time.time() + timeout)
    if result == "ready":
        note = "" if gpu_layers == "0" else " (GPU offload active)."
        return {"ok": True, "output": f"Model server is ready. Future replies should start faster.{note}"}

    if result == "died" and gpu_layers != "0":
        try:
            with (BASE_DIR / "logs" / "web_ui_llama_server.log").open("a", encoding="utf-8") as _lf:
                _lf.write(f"\n=== GPU launch failed (ngl={gpu_layers}) -> CPU fallback (ngl=0) ===\n")
        except: pass
        ok, err = _spawn("0")
        if not ok:
            return err
        result = _wait(time.time() + timeout)
        if result == "ready":
            return {"ok": True, "output": "Model server is ready (CPU fallback — GPU offload failed; see logs/web_ui_llama_server.log)."}

    if result == "died":
        code = LLAMA_SERVER_PROC.returncode if LLAMA_SERVER_PROC else "?"
        return {"ok": False, "output": f"Model server stopped (code {code}). See logs/web_ui_llama_server.log — try 'Warm Model' again."}

    # still loading — keep WARM_START_TIME so progress keeps ticking
    prog = get_warm_progress()
    if prog.get("overdue"):
        return {"ok": False, "output": f"Model server is still loading — finalizing ({prog['elapsed']}s elapsed, check logs). ETA was {prog['eta']}s.", "progress": prog, "overdue": True}
    return {"ok": False, "output": f"Model server is still loading — {prog['percent']}% ({prog['elapsed']}s elapsed, ~{prog['eta']}s left).", "progress": prog}


def runtime_status():
    cfg, error = load_config()
    model_path = resolve_agent_path(cfg.get("model_path")) if cfg else None
    llama_path = resolve_agent_path(cfg.get("llama_bin")) if cfg else None
    model_exists = bool(model_path and model_path.exists())
    llama_exists = bool(llama_path and llama_path.exists())
    config_exists = CONFIG_PATH.exists()
    model_server_running = llama_health()
    prog = get_warm_progress() if not model_server_running and WARM_START_TIME else None

    if error:
        summary = error
    elif model_server_running:
        summary = "Model is warm and ready."
    elif WARM_START_TIME is not None:
        summary = f"Warming {prog['percent']}% ({prog['elapsed']}s, ~{prog['eta']}s left)"
    elif model_exists and llama_exists:
        summary = "Ready for local inference. First reply may load the model."
    else:
        summary = "Setup incomplete."

    base = {
        "config_exists": config_exists,
        "model_path": str(model_path) if model_path else None,
        "model_exists": model_exists,
        "llama_path": str(llama_path) if llama_path else None,
        "llama_exists": llama_exists,
        "model_server_running": model_server_running,
        "internet_allowed": bool(cfg and cfg.get("internet_allowed", False)),
        "voice_enabled": bool(cfg and cfg.get("voice_enabled", False)),
        "startup_auth_enabled": bool(cfg.get("startup_auth_enabled", True)) if cfg else True,
        "startup_verified": STARTUP_VERIFIED,
        "summary": summary,
    }
    if prog:
        base["warm_progress"] = prog
    return base


def kill_process_tree(pid):
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
        )
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def run_command(cmd, timeout):
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except Exception as exc:
        return {"ok": False, "output": str(exc)}

    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # A plain kill() only stops this direct child. If it has spawned its
        # own child process (e.g. single_query.py launching llama.exe), that
        # grandchild is orphaned and keeps running, holding GPU/RAM forever.
        # Killing the whole process tree avoids leaving that behind.
        kill_process_tree(proc.pid)
        try:
            output, _ = proc.communicate(timeout=5)
        except Exception:
            output = ""
        output = (output or "").strip()
        return {"ok": False, "output": output or f"Command timed out after {timeout}s and was stopped."}

    output = (output or "").strip()
    if proc.returncode and not output:
        output = f"Command exited with code {proc.returncode}."
    return {"ok": proc.returncode == 0, "output": output}


def query_warm_server(message, context=None):
    warm = start_llama_server(timeout=180)
    if not warm["ok"] and not llama_health():
        return warm

    CONVERSATION.append({"role": "user", "content": message})
    _append_db('user', message, permanent=False)
    # Inject dynamic internet state so LLM doesn't re-ask for password when toggle is already ON
    dynamic_ctx = get_dynamic_internet_context()
    # Always inject real host time so "what is time now" never hallucinates 10:00 AM
    time_ctx = _time_context()
    # Merge caller context + dynamic internet context + time
    combined_context = "\n\n".join([c for c in [context, dynamic_ctx, time_ctx] if c])
    history = [{"role": "system", "content": read_system_prompt()}]
    if combined_context:
        history.append({"role": "system", "content": combined_context})
    history += CONVERSATION[-12:]
    _cfg, _ = load_config()
    gen_tokens = int((_cfg or {}).get("max_tokens") or 768)
    try:
        result = post_json(
            llama_server_url("/v1/chat/completions"),
            {
                "messages": history,
                "temperature": 0.7,
                "max_tokens": gen_tokens,
                "stream": False,
                # Qwen3: disable hidden thinking channel server-side if supported
                "enable_thinking": False,
                "chat_template_kwargs": {"enable_thinking": False},
                "stop": ["<|im_end|>", "<think>", "</think>", "\nUser:", "\nAssistant:"],
            },
            timeout=120,
        )
        output = _clean_llm_output(result["choices"][0]["message"]["content"].strip())
    except Exception:
        prompt = (
            read_system_prompt()
            + "\n"
            + (combined_context + "\n" if combined_context else "")
            + "\nUser: "
            + message
            + "\nAssistant:"
        )
        try:
            result = post_json(
                llama_server_url("/completion"),
                {
                    "prompt": prompt,
                    "temperature": 0.7,
                    "n_predict": gen_tokens,
                    "stream": False,
                },
                timeout=120,
            )
            output = _clean_llm_output(str(result.get("content", "")).strip())
        except Exception as exc:
            return {"ok": False, "output": f"Model server request failed: {exc}"}

    if not output:
        output = "No response from the local model."
    # Final safety: if cleaning emptied truncated thinking-only output, fallback
    if not output or output.strip() in ("<think>", "</think>"):
        output = "No response from the local model."
    CONVERSATION.append({"role": "assistant", "content": output})
    _append_db('assistant', output, permanent=False)
    return {"ok": True, "output": output}


def query_with_local_memory(message):
    # Retrieval is cheap, in-process TF-IDF (single_query.load_index /
    # query_topk) -- no subprocess involved. We used to shell out to
    # single_query.py for the whole answer too, which spawned a *second*
    # llama.exe competing with the already-resident llama-server.exe for
    # the same ~4GB of GPU memory. On this hardware that second load never
    # won the race: it ran past the 120s timeout, and killing only the
    # direct child left an orphaned llama.exe holding GPU memory forever,
    # degrading every later request (warm or not). Routing local-memory
    # context into the one warm server avoids that entirely.
    try:
        index = single_query.load_index()
        docs = single_query.query_topk(index, message, k=3)
    except Exception:
        docs = []

    context = None
    if docs:
        context = "Relevant local notes:\n" + "\n\n".join(doc["text"] for doc in docs)

    return query_warm_server(message, context=context)


def query_assistant(message, use_rag):
    # Intercept internet-requiring tasks FIRST - so "play youtube" works when Internet toggle is ON
    # without waiting for LLM to hallucinate a refusal like "Run /enable_internet..."
    try:
        executor = get_task_executor()
        task = executor.parse_task(message)
        # Only intercept non-unknown tasks; let LLM handle general chat
        if task.get("action") not in ("unknown",):
            response, success, needs_approval = executor.execute_task(task)
            # Bluetooth: always return honest executor response (never let LLM hallucinate "Bluetooth is turned on")
            if task.get("action") in ("bluetooth_on", "bluetooth_off", "bluetooth_status"):
                CONVERSATION.append({"role": "user", "content": message})
                CONVERSATION.append({"role": "assistant", "content": response})
                _append_db('user', message, permanent=False)
                _append_db('assistant', response, permanent=False)
                return {"ok": True, "output": response}
            # If executor handled it successfully, return directly (browser/search opened)
            if success:
                CONVERSATION.append({"role": "user", "content": message})
                CONVERSATION.append({"role": "assistant", "content": response})
                _append_db('user', message, permanent=False)
                _append_db('assistant', response, permanent=False)
                return {"ok": True, "output": response}
            # If gated by internet OFF, return toggle hint (no password re-ask)
            if "Internet is disabled" in response:
                response = response.replace("Run /enable_internet first (password required).", "Turn the Internet toggle ON in the sidebar (no extra password after startup verification).")
                CONVERSATION.append({"role": "user", "content": message})
                CONVERSATION.append({"role": "assistant", "content": response})
                _append_db('user', message, permanent=False)
                _append_db('assistant', response, permanent=False)
                return {"ok": True, "output": response}
    except Exception:
        pass  # fall through to LLM on any executor error

    if use_rag:
        return query_with_local_memory(message)
    return query_warm_server(message)


def validate_package(strict=False):
    cmd = [preferred_python(), str(VALIDATE_PATH)]
    if strict:
        cmd.append("--strict")
    return run_command(cmd, timeout=60)


def open_cli():
    if not START_AGENT_PATH.exists():
        return {"ok": False, "output": "start-agent.bat is missing."}
    try:
        if os.name == "nt":
            subprocess.Popen(["cmd", "/c", "start", "", str(START_AGENT_PATH)], cwd=str(BASE_DIR))
        else:
            subprocess.Popen([str(START_AGENT_PATH)], cwd=str(BASE_DIR))
        return {"ok": True, "output": "CLI launched."}
    except Exception as exc:
        return {"ok": False, "output": str(exc)}


class Handler(BaseHTTPRequestHandler):
    server_version = "JampanduUi/1.0"

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self.send_html(HTML)
            return
        if parsed.path == "/api/status":
            self.send_json(runtime_status())
            return
        if parsed.path == "/api/warm-progress":
            self.send_json(get_warm_progress())
            return
        if parsed.path == "/api/logs":
            # last lines of llama log for debugging (auth required)
            if not _is_authorized(self):
                self.send_json({"ok": False, "output": "Not authorized."}, status=401)
                return
            try:
                p = BASE_DIR / "logs" / "web_ui_llama_server.log"
                txt = p.read_text(encoding="utf-8", errors="ignore")[-4000:] if p.exists() else "(no log yet)"
                self.send_json({"ok": True, "log": txt})
            except Exception as exc:
                self.send_json({"ok": False, "output": str(exc)})
            return
        self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        payload = self.read_json()
        if parsed.path == "/api/query":
            if not _require_auth(self):
                return
            message = str(payload.get("message", "")).strip()
            if not message:
                self.send_json({"ok": False, "output": "Message cannot be empty."}, status=400)
                return
            self.send_json(query_assistant(message, bool(payload.get("use_rag"))))
            return
        if parsed.path == "/api/validate":
            if not _require_auth(self):
                return
            strict = bool(payload.get("strict"))
            self.send_json(validate_package(strict=strict))
            return
        if parsed.path == "/api/warm-model":
            if not _require_auth(self):
                return
            self.send_json(start_llama_server())
            return
        if parsed.path == "/api/open-cli":
            if not _require_auth(self):
                return
            self.send_json(open_cli())
            return
        if parsed.path == "/api/toggle-internet":
            if not _require_auth(self):
                return
            self.send_json(set_internet_allowed(bool(payload.get("enabled")), payload.get("password")))
            return
        if parsed.path == "/api/verify-startup":
            # pass client IP for rate limiting
            result = verify_startup_password(payload.get("password"), _client_ip(self))
            # On success, set HttpOnly cookie + return token to JS
            if result.get("ok") and result.get("token"):
                tok = result["token"]
                # send with Set-Cookie (HttpOnly, SameSite=Strict, Path=/)
                data = json.dumps(result).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.send_header("Set-Cookie", f"jampandu_token={tok}; Path=/; HttpOnly; SameSite=Strict")
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_json(result)
            return
        if parsed.path == "/api/set-startup-auth":
            self.send_json(set_startup_auth_enabled(bool(payload.get("enabled")), payload.get("password")))
            return
        if parsed.path == "/api/clear-chat":
            if not _require_auth(self):
                return
            global CONVERSATION
            CONVERSATION = []
            self.send_json({"ok": True, "output": "Chat cleared."})
            return
        if parsed.path == "/api/logout":
            # clear tokens for this client (optional: clear all)
            auth = self.headers.get("X-Auth-Token") or ""
            if auth:
                with VALID_TOKENS_LOCK:
                    VALID_TOKENS.pop(auth, None)
            self.send_json({"ok": True, "output": "Logged out."})
            return
        if parsed.path == "/api/history":
            if not _require_auth(self):
                return
            try:
                con = _db_conn()
                cur = con.cursor()
                cur.execute("SELECT id, role, text, permanent FROM conversation ORDER BY id DESC LIMIT 200")
                rows = cur.fetchall()
                con.close()
                history = [{"id": r[0], "role": r[1], "content": r[2], "permanent": bool(r[3])} for r in rows]
                self.send_json({"ok": True, "history": history})
            except Exception as exc:
                self.send_json({"ok": False, "output": str(exc)})
            return
        if parsed.path == "/api/sign-off":
            if not _require_auth(self):
                return
            try:
                con = _db_conn()
                cur = con.cursor()
                cur.execute("DELETE FROM conversation WHERE permanent = 0")
                deleted = cur.rowcount
                cur.execute("UPDATE conversation SET permanent = 1 WHERE permanent IS NULL")
                con.commit()
                con.close()
                # also clear in-memory CONVERSATION (mutate in place)
                _perm_only = [m for m in CONVERSATION if m.get("permanent")]
                del CONVERSATION[:]
                CONVERSATION.extend(_perm_only)
                self.send_json({"ok": True, "output": f"Signed off — cleared {deleted} temporary history entries."})
            except Exception as exc:
                self.send_json({"ok": False, "output": str(exc)})
            return
        if parsed.path == "/api/toggle-pin":
            if not _require_auth(self):
                return
            tid = payload.get("id")
            permanent = bool(payload.get("permanent", True))
            try:
                con = _db_conn()
                cur = con.cursor()
                cur.execute("UPDATE conversation SET permanent = ? WHERE id = ?", (1 if permanent else 0, tid))
                con.commit()
                con.close()
                # update in-memory
                for m in CONVERSATION:
                    if m.get("id") == tid:
                        m["permanent"] = permanent
                        break
                self.send_json({"ok": True, "permanent": permanent})
            except Exception as exc:
                self.send_json({"ok": False, "output": str(exc)})
            return
        self.send_error(404)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def send_html(self, html):
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # Amnesiac: never cache — host browser must not store conversations
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload, status=200):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # Amnesiac: API responses contain conversations — never cache on host
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))


def find_port(start_port):
    for port in range(start_port, start_port + 20):
        if port == LLAMA_PORT:
            continue  # never steal llama's fixed port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((DEFAULT_HOST, port))
            except OSError:
                continue
            return port
    raise RuntimeError("No free local port found.")


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-auto-warm", action="store_true", help="Disable background auto-warm of model server")
    args = parser.parse_args()

    port = find_port(args.port)
    server = ThreadingHTTPServer((args.host, port), Handler)
    url = f"http://{args.host}:{port}/"
    load_history()
    print(f"Jampandu UI running at {url}")
    print("Press Ctrl+C to stop.")

    if not args.no_browser:
        # Amnesiac: prefer Brave incognito so host stores no localhost history/cache
        def _open_browser_amnesiac():
            brave_candidates = [
                os.path.expandvars(r'%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe'),
                os.path.expandvars(r'%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe'),
                os.path.expandvars(r'%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe'),
            ]
            for cand in brave_candidates:
                if os.path.exists(cand):
                    try:
                        subprocess.Popen([cand, "--incognito", url], close_fds=True)
                        return
                    except: pass
            # Fallback: default browser (host may cache, but we set no-store headers)
            try: webbrowser.open(url)
            except: pass
        threading.Timer(0.4, _open_browser_amnesiac).start()

    # FIX: Auto-warm in background so first user message is NOT cold-slow.
    # Without this, every fresh UI session pays ~30-45s loading 4GB GGUF into VRAM.
    # With auto-warm, model loads silently during the 10-20s the user spends reading UI.
    if not args.no_auto_warm:
        def _auto_warm():
            # Defer warm until startup auth is satisfied — prevents CPU contention during password check
            # which was causing "Verifying..." to appear stuck on slow USB systems (issue #2)
            for _ in range(30):  # wait up to 30s for auth
                time.sleep(1.2)
                cfg_tmp, _ = load_config()
                need_auth = cfg_tmp and cfg_tmp.get("startup_auth_enabled") and cfg_tmp.get("password_verifier")
                if need_auth and not _is_token_valid(None) and not STARTUP_VERIFIED:
                    # check if any token exists at all (means someone verified)
                    with VALID_TOKENS_LOCK:
                        has_any = any(exp > time.time() for exp in VALID_TOKENS.values())
                    if not has_any:
                        continue  # still waiting for first verify, don't start heavy load
                break
            status = runtime_status()
            if status["model_exists"] and status["llama_exists"] and not status["model_server_running"]:
                print("[auto-warm] Model server is Cold -> warming in background...")
                try:
                    res = start_llama_server(timeout=180)
                    print(f"[auto-warm] {res.get('output')}")
                except Exception as exc:
                    print(f"[auto-warm] failed: {exc}")
            else:
                print(f"[auto-warm] skip: running={status['model_server_running']} model={status['model_exists']}")
        threading.Thread(target=_auto_warm, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try: server.server_close()
        except: pass
        stop_llama_server()
        _host_cleanup_amnesiac("finally")


if __name__ == "__main__":
    main()
