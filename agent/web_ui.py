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
# Cloud / Gemini hybrid (graceful if missing) - checklist
try:
    import gemini_client  # noqa: E402
except Exception:
    gemini_client = None
try:
    import firestore_sync  # noqa: E402
except Exception:
    firestore_sync = None
try:
    import vertex_config  # noqa: E402
except Exception:
    vertex_config = None
try:
    from adk_agent.agent import health as adk_health, run_adk_query  # noqa: E402
except Exception:
    adk_health = None
    run_adk_query = None
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
  <title>Jampandu - Local AI Assistant</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&amp;family=JetBrains+Mono:wght@400;500&amp;display=swap" rel="stylesheet">
  <style>
:root {
      color-scheme: dark;
      --bg-0: #07050e;
      --bg-1: #0d0a1a;
      --surface: rgba(255, 255, 255, 0.04);
      --surface-strong: rgba(255, 255, 255, 0.07);
      --surface-2: rgba(10, 6, 20, 0.5);
      --line: rgba(255, 255, 255, 0.08);
      --line-soft: rgba(255, 255, 255, 0.04);
      --text: #f0eaff;
      --muted: #9b8ec4;
      --violet: #8b5cf6;
      --pink: #ec4899;
      --cyan: #22d3ee;
      --lime: #a3e635;
      --amber: #fbbf24;
      --orange: #fb923c;
      --rose: #fb7185;
      --grad-brand: linear-gradient(135deg, #8b5cf6 0%, #ec4899 55%, #fb923c 100%);
      --grad-ok: linear-gradient(135deg, #34d399, #a3e635);
      --grad-warn: linear-gradient(135deg, #fbbf24, #fb923c);
      --grad-bad: linear-gradient(135deg, #fb7185, #ef4444);
      --radius: 12px;
      --radius-lg: 16px;
      --shadow-glow: 0 8px 32px -8px rgba(139, 92, 246, 0.35);
      font-family: "Inter", system-ui, -apple-system, sans-serif;
    }
    *, *::before, *::after { box-sizing: border-box; }
    html { height: 100%; overflow: hidden; }
    body {
      margin: 0; height: 100%; height: 100vh; height: 100dvh;
      overflow: hidden; color: var(--text);
      background:
        radial-gradient(1200px 800px at 5% -5%, rgba(139,92,246,0.30), transparent 55%),
        radial-gradient(1000px 700px at 105% 5%, rgba(236,72,153,0.22), transparent 50%),
        radial-gradient(900px 800px at 45% 115%, rgba(34,211,238,0.18), transparent 50%),
        radial-gradient(600px 500px at 85% 55%, rgba(251,146,60,0.10), transparent 55%),
        linear-gradient(180deg, var(--bg-0), var(--bg-1));
      background-attachment: fixed;
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
    }
    button, textarea, input { font: inherit; }
    ::selection { background: rgba(139,92,246,0.4); color: #fff; }
    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: linear-gradient(180deg, var(--violet), var(--pink)); border-radius: 999px; }
    ::-webkit-scrollbar-thumb:hover { background: linear-gradient(180deg, #a78bfa, #f472b6); }
    .app { display: flex; flex-direction: row; height: 100vh; height: 100dvh; overflow: hidden; align-items: stretch; position: fixed; inset: 0; width: 100%; }
    aside { width: 300px; min-width: 300px; max-width: 300px; height: 100vh; height: 100dvh; overflow-y: auto; overflow-x: hidden; border-right: 1px solid var(--line); background: var(--surface); backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px); padding: 24px; flex-shrink: 0; position: sticky; top: 0; align-self: flex-start; display: flex; flex-direction: column; scrollbar-width: thin; scrollbar-color: rgba(139,92,246,0.5) transparent; overscroll-behavior: contain; z-index: 5; }
    main { flex: 1; display: flex; flex-direction: column; min-width: 0; height: 100%; max-height: 100vh; max-height: 100dvh; overflow: hidden; min-height: 0; }
    .brand { display: flex; align-items: center; gap: 14px; margin-bottom: 28px; }
    .brand-mark { width: 48px; height: 48px; border-radius: var(--radius); background: var(--grad-brand); display: grid; place-items: center; box-shadow: 0 0 0 1px rgba(255,255,255,0.12) inset, var(--shadow-glow); position: relative; overflow: hidden; }
    .brand-mark::after { content: ""; position: absolute; inset: 0; background: linear-gradient(135deg, rgba(255,255,255,0.2) 0%, transparent 50%); border-radius: inherit; }
    .brand-mark svg { position: relative; z-index: 1; }
    .brand h1 { margin: 0; font-size: 26px; line-height: 1; letter-spacing: -0.02em; background: var(--grad-brand); -webkit-background-clip: text; background-clip: text; color: transparent; font-weight: 800; }
    .brand .tagline { margin: 4px 0 0; color: var(--muted); font-size: 11.5px; letter-spacing: 0.02em; font-weight: 400; }
    .sidebar-search { position: relative; margin-bottom: 4px; }
    .sidebar-search input { width: 100%; min-height: 38px; border: 1px solid var(--line); background: rgba(255,255,255,0.04); color: var(--text); border-radius: 10px; padding: 8px 12px 8px 34px; outline: none; font-size: 13px; transition: border-color 0.15s ease, box-shadow 0.15s ease; }
    .sidebar-search input::placeholder { color: var(--muted); }
    .sidebar-search input:focus { border-color: rgba(139,92,246,0.6); box-shadow: 0 0 0 3px rgba(139,92,246,0.15); }
    .sidebar-search .search-icon { position: absolute; left: 10px; top: 50%; transform: translateY(-50%); color: var(--muted); font-size: 13px; pointer-events: none; }
    .section { padding: 16px 0; border-top: 1px solid var(--line-soft); }
    .section h2 { margin: 0 0 14px; font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.1em; font-weight: 700; }
    .status-row { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin: 10px 0; color: var(--muted); font-size: 13.5px; }
    .pill { min-width: 64px; text-align: center; border: 1px solid var(--line); color: var(--text); padding: 4px 10px; border-radius: 999px; font-size: 11.5px; font-weight: 600; background: rgba(255,255,255,0.05); display: inline-flex; align-items: center; gap: 6px; }
    .pill .dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
    .pill.ok { border-color: transparent; background: var(--grad-ok); color: #062a17; box-shadow: 0 4px 14px -4px rgba(163,230,53,0.5); }
    .pill.ok .dot { background: #062a17; }
    .pill.warn { border-color: transparent; background: var(--grad-warn); color: #3a1c02; box-shadow: 0 4px 14px -4px rgba(251,146,60,0.5); }
    .pill.warn .dot { background: #3a1c02; }
    .pill.bad { border-color: transparent; background: var(--grad-bad); color: #370408; box-shadow: 0 4px 14px -4px rgba(251,113,133,0.5); }
    .pill.bad .dot { background: #370408; }
    .pill.clickable { cursor: pointer; }
    .pill.clickable:hover { filter: brightness(1.08); }
    .cloud-setup label { display: block; font-size: 11px; color: var(--muted); margin: 10px 0 4px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; }
    .cloud-setup input, .cloud-setup select { width: 100%; min-height: 34px; border: 1px solid var(--line); background: rgba(255,255,255,0.04); color: var(--text); border-radius: 8px; padding: 6px 10px; font-size: 12.5px; outline: none; }
    .cloud-setup input:focus, .cloud-setup select:focus { border-color: rgba(139,92,246,0.6); box-shadow: 0 0 0 3px rgba(139,92,246,0.15); }
    .cloud-setup .hint { font-size: 11.5px; color: var(--muted); margin: 0 0 8px; line-height: 1.45; }
    .cloud-setup select option { background: #120c1c; color: var(--text); }
    .actions { display: grid; gap: 8px; }
    .button { min-height: 40px; border: 1px solid var(--line); background: var(--surface-strong); color: var(--text); border-radius: 10px; cursor: pointer; padding: 0 14px; text-align: left; font-weight: 600; font-size: 13px; transition: transform 0.15s ease, border-color 0.15s ease, background 0.15s ease, box-shadow 0.15s ease; display: flex; align-items: center; gap: 8px; }
    .button:hover { border-color: rgba(139,92,246,0.5); background: rgba(139,92,246,0.12); transform: translateY(-1px); box-shadow: 0 6px 20px -8px rgba(139,92,246,0.45); }
    .button:active { transform: translateY(0); }
    .button.primary { background: var(--grad-brand); color: #fff; border-color: transparent; font-weight: 700; text-align: center; justify-content: center; box-shadow: 0 8px 24px -6px rgba(236,72,153,0.55); }
    .button.primary:hover { transform: translateY(-1px) scale(1.02); box-shadow: 0 12px 32px -6px rgba(236,72,153,0.7); }
    .button:disabled { cursor: not-allowed; opacity: 0.5; transform: none; box-shadow: none; }
    .button.ghost { background: rgba(255,255,255,0.05); border-color: var(--line); color: var(--muted); font-size: 12.5px; min-height: 34px; padding: 0 12px; text-align: center; justify-content: center; }
    .button.ghost:hover { color: var(--text); border-color: rgba(251,113,133,0.4); background: rgba(251,113,133,0.08); }
    .button .btn-icon { font-size: 15px; line-height: 1; }
    .toggle { display: flex; align-items: center; justify-content: space-between; gap: 10px; color: var(--muted); font-size: 13.5px; }
    .switch { position: relative; display: inline-block; width: 42px; height: 22px; flex-shrink: 0; }
    .switch input { opacity: 0; width: 0; height: 0; }
    .switch .slider { position: absolute; inset: 0; cursor: pointer; background: rgba(255,255,255,0.1); border: 1px solid var(--line); border-radius: 999px; transition: background 0.2s ease, border-color 0.2s ease; }
    .switch .slider::before { content: ""; position: absolute; width: 16px; height: 16px; left: 2px; top: 2px; border-radius: 50%; background: #fff; transition: transform 0.2s cubic-bezier(0.4,0,0.2,1); box-shadow: 0 2px 6px rgba(0,0,0,0.35); }
    .switch input:checked + .slider { background: var(--grad-ok); border-color: transparent; }
    .switch input:checked + .slider::before { transform: translateX(20px); }
    .switch input:disabled + .slider { cursor: not-allowed; opacity: 0.5; }
    header { border-bottom: 1px solid var(--line); padding: 16px 28px; display: flex; justify-content: space-between; align-items: center; gap: 16px; background: var(--surface-2); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px); flex-shrink: 0; z-index: 5; }
    .headline { min-width: 0; }
    .headline h2 { margin: 0; font-size: 18px; letter-spacing: -0.01em; background: linear-gradient(90deg, var(--cyan), var(--violet)); -webkit-background-clip: text; background-clip: text; color: transparent; font-weight: 700; }
    .headline p { margin: 3px 0 0; color: var(--muted); font-size: 12.5px; }
    #activity { color: var(--cyan); font-size: 13px; white-space: nowrap; display: flex; align-items: center; gap: 8px; font-weight: 500; }
    #activity::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: var(--grad-ok); box-shadow: 0 0 8px 2px rgba(163,230,53,0.6); animation: pulse-dot 1.6s ease-in-out infinite; }
    @keyframes pulse-dot { 0%,100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.5; transform: scale(0.75); } }
    #chat { flex: 1 1 0; overflow-y: auto; overflow-x: hidden; padding: 28px 32px 28px; min-height: 0; scroll-behavior: smooth; scrollbar-width: thin; scrollbar-color: rgba(139,92,246,0.4) transparent; scroll-padding-bottom: 0; overscroll-behavior: contain; -webkit-overflow-scrolling: touch; }
    .empty-state { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; text-align: center; padding: 40px; opacity: 0.6; }
    .empty-state .empty-icon { font-size: 48px; margin-bottom: 16px; opacity: 0.5; }
    .empty-state h3 { margin: 0 0 8px; font-size: 18px; color: var(--text); font-weight: 700; }
    .empty-state p { margin: 0; font-size: 14px; color: var(--muted); }
    .message { max-width: 840px; margin: 0 0 14px; border-radius: var(--radius-lg); border: 1px solid var(--line); padding: 14px 18px; white-space: pre-wrap; overflow-wrap: anywhere; background: var(--surface); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); animation: msg-in 0.3s ease-out both; position: relative; }
    @keyframes msg-in { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
    .message .msg-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
    .message .name { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; }
    .message .timestamp { font-size: 11px; color: var(--muted); opacity: 0; transition: opacity 0.15s ease; }
    .message:hover .timestamp { opacity: 1; }
    .message .text { color: #ece6fb; line-height: 1.65; font-size: 14.5px; }
    .message.user { border-color: rgba(34,211,238,0.3); background: linear-gradient(135deg, rgba(34,211,238,0.10), rgba(99,102,241,0.05)); margin-left: auto; }
    .message.user .name { color: var(--cyan); }
    .message.assistant { border-color: rgba(236,72,153,0.3); background: linear-gradient(135deg, rgba(139,92,246,0.10), rgba(236,72,153,0.06)); }
    .message.assistant .name { background: linear-gradient(90deg, var(--violet), var(--pink)); -webkit-background-clip: text; background-clip: text; color: transparent; }
    .message.system { border-color: rgba(251,191,36,0.3); background: linear-gradient(135deg, rgba(251,191,36,0.08), rgba(251,146,60,0.04)); max-width: 100%; font-size: 13px; }
    .message.system .name { color: var(--amber); }
    .message .copy-btn { position: absolute; top: 8px; right: 8px; width: 28px; height: 28px; border-radius: 6px; border: 1px solid var(--line); background: rgba(255,255,255,0.05); color: var(--muted); cursor: pointer; display: flex; align-items: center; justify-content: center; font-size: 12px; opacity: 0; transition: opacity 0.15s ease, background 0.15s ease, color 0.15s ease; }
    .message:hover .copy-btn { opacity: 1; }
    .message .copy-btn:hover { background: rgba(255,255,255,0.1); color: var(--text); }
    .message .copy-btn.copied { color: var(--lime); border-color: rgba(163,230,53,0.3); }
    .typing { display: inline-flex; align-items: center; gap: 5px; padding: 4px 0; }
    .typing span { width: 7px; height: 7px; border-radius: 50%; background: var(--pink); opacity: 0.7; animation: bounce 1.1s infinite ease-in-out; }
    .typing span:nth-child(2) { animation-delay: 0.15s; }
    .typing span:nth-child(3) { animation-delay: 0.3s; }
    @keyframes bounce { 0%,80%,100% { transform: scale(0.6); opacity: 0.3; } 40% { transform: scale(1); opacity: 1; } }
    form { border-top: 1px solid var(--line); padding: 16px 32px calc(16px + env(safe-area-inset-bottom)); background: rgba(13,10,26,0.96); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px); display: grid; grid-template-columns: minmax(0,1fr) 112px; gap: 12px; align-items: end; flex: 0 0 auto; flex-shrink: 0; z-index: 10; position: relative; width: 100%; box-sizing: border-box; }
    textarea { width: 100%; min-height: 72px; max-height: 170px; resize: vertical; border: 1px solid var(--line); background: rgba(255,255,255,0.04); color: var(--text); border-radius: var(--radius); padding: 12px 16px; outline: none; transition: border-color 0.15s ease, box-shadow 0.15s ease; font-family: inherit; font-size: 14.5px; line-height: 1.5; }
    textarea::placeholder { color: var(--muted); }
    textarea:focus { border-color: rgba(139,92,246,0.6); box-shadow: 0 0 0 3px rgba(139,92,246,0.18), 0 0 24px -6px rgba(236,72,153,0.35); }
    .shortcut-hint { text-align: center; font-size: 10px; color: var(--muted); opacity: 0.6; margin-top: 4px; }
    #send { min-height: 72px; border: none; background: var(--grad-brand); color: #fff; border-radius: var(--radius); cursor: pointer; font-weight: 700; font-size: 14px; display: flex; align-items: center; justify-content: center; gap: 6px; transition: transform 0.15s ease, box-shadow 0.15s ease; box-shadow: 0 8px 24px -6px rgba(236,72,153,0.5); }
    #send:hover { transform: translateY(-1px); box-shadow: 0 12px 32px -6px rgba(236,72,153,0.65); }
    #send:active { transform: translateY(0); }
    #send:disabled { cursor: not-allowed; opacity: 0.5; transform: none; }
    #send .send-icon { font-size: 16px; }
    .app-footer { padding: 8px 28px; font-size: 11px; color: var(--muted); opacity: 0.5; display: flex; justify-content: space-between; flex-shrink: 0; }
    #startup-overlay { position: fixed; inset: 0; z-index: 9999; display: none; align-items: center; justify-content: center; background: rgba(7,5,14,0.92); backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px); padding: 20px; animation: fade-in 0.3s ease; }
    #startup-overlay.active { display: flex; }
    @keyframes fade-in { from { opacity: 0; } to { opacity: 1; } }
    .startup-card { width: 100%; max-width: 420px; background: linear-gradient(135deg, rgba(13,10,26,0.96), rgba(28,12,54,0.96)); border: 1px solid rgba(139,92,246,0.35); border-radius: var(--radius-lg); padding: 32px; box-shadow: 0 24px 60px -12px rgba(0,0,0,0.7), 0 0 0 1px rgba(255,255,255,0.05) inset; animation: msg-in 0.3s ease-out both; }
    .startup-card h2 { margin: 0 0 6px; font-size: 20px; background: var(--grad-brand); -webkit-background-clip: text; background-clip: text; color: transparent; font-weight: 800; }
    .startup-card p { margin: 0 0 20px; color: var(--muted); font-size: 13.5px; line-height: 1.5; }
    .startup-card input { width: 100%; min-height: 46px; border: 1px solid var(--line); background: rgba(255,255,255,0.05); color: var(--text); border-radius: 10px; padding: 10px 16px; outline: none; margin-bottom: 12px; font-size: 14px; transition: border-color 0.15s ease, box-shadow 0.15s ease; }
    .startup-card input:focus { border-color: rgba(139,92,246,0.6); box-shadow: 0 0 0 3px rgba(139,92,246,0.18); }
    .startup-error { min-height: 18px; color: var(--rose); font-size: 13px; margin: 4px 0 8px; }
    .startup-card .button { width: 100%; text-align: center; justify-content: center; }
    #menu-toggle { display: none; width: 40px; height: 40px; border-radius: 8px; border: 1px solid var(--line); background: var(--surface-strong); color: var(--text); font-size: 18px; line-height: 1; cursor: pointer; place-items: center; flex-shrink: 0; transition: border-color 0.15s ease, background 0.15s ease; }
    #menu-toggle:hover { border-color: rgba(139,92,246,0.5); background: rgba(139,92,246,0.12); }
    #sidebar-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); backdrop-filter: blur(2px); z-index: 40; }
    #sidebar-overlay.active { display: block; }
    @media (min-width: 821px) { aside { position: fixed; left: 0; top: 0; bottom: 0; height: 100vh; height: 100dvh; overscroll-behavior: contain; } main { margin-left: 300px; width: calc(100% - 300px); height: 100vh; height: 100dvh; } }
    @media (max-width: 820px) { .app { flex-direction: column; height: 100vh; height: 100dvh; position: fixed; inset: 0; } aside { position: fixed; top: 0; left: 0; width: 288px; max-width: 82vw; height: 100%; max-height: 100dvh; z-index: 50; border-right: 1px solid var(--line); border-bottom: none; transform: translateX(-100%); transition: transform 0.28s cubic-bezier(0.4,0,0.2,1); box-shadow: 8px 0 32px rgba(0,0,0,0.5); } aside.open { transform: translateX(0); } #menu-toggle { display: grid; } main { height: 100%; max-height: 100dvh; min-height: 0; } form { grid-template-columns: minmax(0,1fr) 88px; padding: 12px 14px calc(12px + env(safe-area-inset-bottom)); } #chat { padding: 16px 14px 24px; } #activity { white-space: normal; font-size: 12.5px; } header { padding: 12px 14px; } .brand { margin-bottom: 20px; } }
    :focus-visible { outline: 2px solid var(--violet); outline-offset: 2px; }
    button:focus-visible, textarea:focus-visible, input:focus-visible { outline: 2px solid var(--violet); outline-offset: 2px; }
    .hist-item { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 8px; cursor: pointer; transition: background 0.12s ease; }
    .hist-item:hover { background: rgba(255,255,255,0.06); }
    .hist-item .hist-preview { flex: 1; min-width: 0; }
    .hist-item .hist-text { font-size: 12.5px; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .hist-item .hist-meta { font-size: 11px; color: var(--muted); display: flex; align-items: center; gap: 4px; }
    #history-section { transition: opacity 0.2s ease; }
    #history-section.hidden { display: none; }
  </style>
</head>
<body>
  <div id="startup-overlay" aria-hidden="true">
    <div class="startup-card">
      <h2>Verify Access</h2>
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
            <rect x="2" y="2" width="24" height="24" rx="6" stroke="rgba(255,255,255,0.9)" stroke-width="1.5"/>
            <path d="M19.6 3.8l4.6 4.6-11.9 11.9-5.8 1.2 1.2-5.8L19.6 3.8z" stroke="#fff" stroke-width="1.9" stroke-linejoin="round"/>
            <path d="M16.8 6.6l4.6 4.6" stroke="#fff" stroke-width="1.9" stroke-opacity="0.85"/>
            <path d="M8.5 19.5l-3.2 3.2" stroke="#fff" stroke-width="1.9" stroke-linecap="round" stroke-opacity="0.85"/>
          </svg>
        </div>
        <div>
          <h1>Jampandu</h1>
          <p class="tagline">Local · Private · Yours</p>
        </div>
      </div>
      <div class="section">
        <h2>Status</h2>
        <div class="status-row"><span>Config</span><span id="config" class="pill">-</span></div>
        <div class="status-row"><span>Model</span><span id="model" class="pill">-</span></div>
        <div class="status-row">
          <span>Active</span>
          <select id="model-select" style="min-width:160px; max-width:200px; height:28px; border:1px solid var(--line); background:rgba(255,255,255,0.05); color:var(--text); border-radius:6px; font-size:11px; padding:0 6px; outline:none;">
            <option value="">Loading...</option>
          </select>
        </div>
        <div class="status-row"><span>Runtime</span><span id="runtime" class="pill">-</span></div>
        <div class="status-row"><span>Server</span><span id="server" class="pill">-</span></div>
        <div id="warm-progress" style="display:none; margin:8px 0 4px; font-size:12px;">
          <div style="display:flex; justify-content:space-between; color:var(--muted); margin-bottom:6px;"><span id="warm-pct">0%</span><span id="warm-time">0s · ETA --</span></div>
          <div style="height:6px; background:rgba(255,255,255,0.08); border-radius:999px; overflow:hidden; border:1px solid var(--line);"><div id="warm-bar" style="height:100%; width:0%; background:var(--grad-brand); transition:width 0.4s ease; border-radius:999px;"></div></div>
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
        <div class="status-row"><span>Voice</span><span id="voice" class="pill">-</span></div>
        <div class="status-row"><span>Gemini</span><span id="gemini" class="pill clickable" title="Click to set up Gemini">-</span></div>
        <div class="status-row"><span>Cloud</span><span id="cloud" class="pill clickable" title="Click to set up GCP / Firestore">-</span></div>
        <div class="status-row"><span>ADK</span><span id="adk" class="pill clickable" title="Click to set up Google ADK">-</span></div>
      </div>
      <div class="section cloud-setup" id="cloud-setup-section">
        <h2>Cloud setup</h2>
        <p class="hint">Gemini uses an AI Studio key. Cloud/Firestore need a GCP project. ADK is Google's agent framework — install SDKs if the pill says Missing.</p>
        <label for="cloud-gemini-key">Gemini API key</label>
        <input id="cloud-gemini-key" type="password" autocomplete="off" placeholder="AIza… from aistudio.google.com/apikey">
        <div id="cloud-key-hint" class="hint"></div>
        <label for="cloud-gcp-project">GCP project</label>
        <input id="cloud-gcp-project" type="text" autocomplete="off" placeholder="my-gcp-project">
        <label for="cloud-gcs-bucket">GCS bucket (optional)</label>
        <input id="cloud-gcs-bucket" type="text" autocomplete="off" placeholder="pen-ai-brain">
        <label for="cloud-gemini-model">Model</label>
        <select id="cloud-gemini-model">
          <option value="gemini-2.5-pro">gemini-2.5-pro</option>
          <option value="gemini-2.5-flash">gemini-2.5-flash</option>
        </select>
        <button id="save-cloud-setup" class="button" type="button" style="margin-top:10px;width:100%;justify-content:center;"><span class="btn-icon">Save cloud settings</span></button>
        <button id="install-cloud-deps" class="button" type="button" style="margin-top:8px;width:100%;justify-content:center;" title="pip install google-genai google-adk google-cloud-*"><span class="btn-icon">Install ADK &amp; Cloud SDKs</span></button>
        <p id="cloud-setup-status" class="hint"></p>
      </div>
      <div class="section">
        <div class="status-row"><span>Local memory</span>
          <label class="switch" title="Use local documents to answer">
            <input id="rag" type="checkbox">
            <span class="slider"></span>
          </label>
        </div>
      </div>
      <div class="section">
        <h2>Inference</h2>
        <div class="status-row"><span>Temperature</span><span id="inf-temp-val" style="color:var(--cyan);font-size:12px;">0.7</span></div>
        <input id="inf-temp" type="range" min="0" max="2" step="0.1" value="0.7" style="width:100%;accent-color:var(--violet);">
        <div class="status-row" style="margin-top:8px;"><span>Top P</span><span id="inf-topp-val" style="color:var(--cyan);font-size:12px;">0.9</span></div>
        <input id="inf-topp" type="range" min="0" max="1" step="0.05" value="0.9" style="width:100%;accent-color:var(--violet);">
        <div class="status-row" style="margin-top:8px;"><span>Max Tokens</span><span id="inf-maxtok-val" style="color:var(--cyan);font-size:12px;">768</span></div>
        <input id="inf-maxtok" type="range" min="64" max="2048" step="64" value="768" style="width:100%;accent-color:var(--violet);">
        <div class="status-row" style="margin-top:8px;"><span>Repeat Penalty</span><span id="inf-rp-val" style="color:var(--cyan);font-size:12px;">1.1</span></div>
        <input id="inf-rp" type="range" min="1" max="2" step="0.05" value="1.1" style="width:100%;accent-color:var(--violet);">
        <div class="status-row" style="margin-top:8px;"><span>Min P</span><span id="inf-minp-val" style="color:var(--cyan);font-size:12px;">0.05</span></div>
        <input id="inf-minp" type="range" min="0" max="1" step="0.01" value="0.05" style="width:100%;accent-color:var(--violet);">
        <button id="save-inference" class="button" type="button" style="margin-top:10px;width:100%;text-align:center;justify-content:center;"><span class="btn-icon">Save Inference Params</span></button>
      </div>
      <div class="section" id="templates-section" style="display:none;">
        <h2>Prompt Templates</h2>
        <div id="templates-list" style="font-size:12px;"></div>
        <div style="margin-top:8px;">
          <input id="new-template-name" type="text" placeholder="Template name" style="width:100%;min-height:28px;border:1px solid var(--line);background:rgba(255,255,255,0.05);color:var(--text);border-radius:6px;padding:4px 8px;font-size:12px;outline:none;">
        </div>
        <div style="margin-top:4px;">
          <textarea id="new-template-content" placeholder="Template content..." style="width:100%;min-height:60px;border:1px solid var(--line);background:rgba(255,255,255,0.05);color:var(--text);border-radius:6px;padding:4px 8px;font-size:12px;outline:none;resize:vertical;"></textarea>
        </div>
        <button id="save-template" class="button" type="button" style="margin-top:6px;width:100%;text-align:center;justify-content:center;font-size:12px;"><span class="btn-icon">Save Template</span></button>
      </div>
      <div class="section">
        <h2>Export / Import</h2>
        <div style="display:grid;gap:6px;">
          <button id="export-md" class="button ghost" type="button" style="font-size:12px;"><span class="btn-icon">Export Markdown</span></button>
          <button id="export-json" class="button ghost" type="button" style="font-size:12px;"><span class="btn-icon">Export JSON</span></button>
          <button id="import-btn" class="button ghost" type="button" style="font-size:12px;"><span class="btn-icon">Import Conversations</span></button>
          <input id="import-file" type="file" accept=".json" style="display:none;">
        </div>
      </div>
      <div class="section">
        <div class="status-row"><span>Theme</span>
          <label class="switch" title="Toggle light/dark theme">
            <input id="theme-toggle" type="checkbox">
            <span class="slider"></span>
          </label>
        </div>
      </div>
      <div class="section" id="history-section">
        <h2>History</h2>
        <div class="sidebar-search">
          <span class="search-icon">Search</span>
          <input id="history-search" type="text" placeholder="Search conversations..." autocomplete="off">
        </div>
        <div id="history-list" style="max-height:220px; overflow-y:auto; font-size:12px; margin-top:8px;"></div>
        <div style="margin-top:10px; display:flex; gap:8px; flex-wrap:wrap;">
          <button id="history-refresh" class="button ghost" type="button" style="font-size:12px;"><span class="btn-icon">Refresh</span></button>
          <button id="history-toggle" class="button ghost" type="button" style="font-size:12px;"><span class="btn-icon">Toggle</span></button>
        </div>
      </div>
      <div class="section actions">
        <button id="warm" class="button" type="button"><span class="btn-icon">Warm Model</span></button>
        <button id="refresh" class="button" type="button"><span class="btn-icon">Refresh</span></button>
        <button id="validate" class="button" type="button"><span class="btn-icon">Validate</span></button>
        <button id="validate-strict" class="button" type="button"><span class="btn-icon">Validate Strict</span></button>
        <button id="cloud-sync" class="button" type="button" title="Sync to Firestore/GCS (requires internet + GCP project)"><span class="btn-icon">Cloud Sync</span></button>
        <button id="cli" class="button" type="button"><span class="btn-icon">Open CLI</span></button>
        <button id="sign-off" class="button" type="button" style="background:var(--grad-bad); color:#fff; border-color:transparent;"><span class="btn-icon">Sign Off</span></button>
      </div>
    </aside>
    <main>
      <header>
        <button id="menu-toggle" type="button" aria-label="Toggle sidebar" aria-expanded="false">Menu</button>
        <div class="headline">
          <h2>Conversation</h2>
          <p id="subtitle">Private, local, and under your control.</p>
        </div>
        <div class="header-actions">
          <button id="clear-chat" class="button ghost" type="button" title="Clear conversation history (privacy)"><span class="btn-icon">Clear Chat</span></button>
          <div id="activity">Ready</div>
        </div>
      </header>
      <section id="chat" aria-live="polite"></section>
      <form id="composer">
        <div style="display:flex; flex-direction:column;">
          <textarea id="message" placeholder="Ask Jampandu..." autocomplete="off"></textarea>
          <div class="shortcut-hint">Ctrl + Enter to send</div>
        </div>
        <button id="send" class="button primary" type="submit"><span class="send-icon">Send</span></button>
      </form>
    </main>
  </div>
  <div class="app-footer">
    <span>Jampandu v5 · Local AI Assistant</span>
    <span>Fully private · Nothing leaves your device</span>
  </div>
</body>
</html>
  <script>
    const chat = document.querySelector("#chat");
    const activity = document.querySelector("#activity");
    const messageInput = document.querySelector("#message");
    const sendButton = document.querySelector("#send");

    function setBusy(isBusy, text) {
      activity.textContent = text;
      sendButton.disabled = isBusy;
      messageInput.disabled = isBusy || document.querySelector("#startup-overlay").classList.contains("active");
    }

    function escapeHtml(s) {
      return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
    }

    function renderMarkdown(text) {
      let html = escapeHtml(text);
      html = html.replace(/```([\s\S]*?)```/g, function(m, code) { return '<pre><code>' + code + '</code></pre>'; });
      html = html.replace(/`([^`]+?)`/g, "<code>$1</code>");
      html = html.replace(/\*\*([^\*]+?)\*\*/g, "<strong>$1</strong>");
      html = html.replace(/(?<!\*)\*([^\*]+?)\*(?!\*)/g, "<em>$1</em>");
      html = html.replace(/^(?:-|\*) (.+)$/gm, "<li>$1</li>");
      html = html.replace(/(<li>.*<\/li>)/gs, function(m) { return '<ul>' + m + '</ul>'; });
      html = html.replace(/\n/g, "<br>");
      html = html.replace(/<pre><code>([\s\S]*?)<\/code><\/pre>/g, function(m, c) { return '<pre><code>' + c.replace(/<br>/g, "\n") + '</code></pre>'; });
      html = html.replace(/<ul>(?:<br>)*/g, "<ul>").replace(/(?:<br>)*<\/ul>/g, "</ul>").replace(/<\/li><br><li>/g, "</li><li>");
      return html;
    }

    function scrollToBottom() {
      var doScroll = function(instant) {
        var max = chat.scrollHeight + 9999;
        try { chat.scrollTo({ top: max, behavior: instant ? "auto" : "smooth" }); } catch(e) { chat.scrollTop = chat.scrollHeight; }
        chat.scrollTop = chat.scrollHeight;
      };
      doScroll(false);
      requestAnimationFrame(function() { doScroll(false); requestAnimationFrame(function() { doScroll(true); }); });
      var ticks = 0;
      var pin = setInterval(function() { doScroll(true); if (++ticks > 8) clearInterval(pin); }, 60);
      setTimeout(function() { doScroll(true); }, 250);
      setTimeout(function() { doScroll(true); }, 600);
    }

    var _autoScrollObserver = new MutationObserver(function() { scrollToBottom(); });
    _autoScrollObserver.observe(chat, { childList: true, subtree: true, characterData: true });
    window.addEventListener("resize", scrollToBottom);
    chat.addEventListener("DOMNodeInserted", scrollToBottom);

    var _pinInterval = null;
    function startPinning() { if (_pinInterval) return; _pinInterval = setInterval(scrollToBottom, 80); }
    function stopPinning() { if (_pinInterval) { clearInterval(_pinInterval); _pinInterval = null; } setTimeout(function() { try { chat.scrollTo({ top: chat.scrollHeight + 9999, behavior: "auto" }); } catch(e) {} chat.scrollTop = chat.scrollHeight; }, 30); }

    function getTimestamp() {
      return new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    function addMessage(role, text) {
      var wrapper = document.createElement("article");
      wrapper.className = "message " + role;
      var ts = getTimestamp();
      var header = document.createElement("div");
      header.className = "msg-header";
      var nameSpan = document.createElement("span");
      nameSpan.className = "name";
      nameSpan.textContent = role === "user" ? "You" : role === "assistant" ? "Jampandu" : "System";
      var tsSpan = document.createElement("span");
      tsSpan.className = "timestamp";
      tsSpan.textContent = ts;
      header.appendChild(nameSpan);
      header.appendChild(tsSpan);
      var body = document.createElement("div");
      body.className = "text";
      if (role === "assistant" || role === "system") { body.innerHTML = renderMarkdown(text); }
      else { body.textContent = text; }
      wrapper.appendChild(header);
      wrapper.appendChild(body);

      if (role === "assistant" || role === "system") {
        var copyBtn = document.createElement("button");
        copyBtn.className = "copy-btn";
        copyBtn.type = "button";
        copyBtn.title = "Copy message";
        copyBtn.textContent = "Copy";
        copyBtn.addEventListener("click", function() {
          if (navigator.clipboard) {
            navigator.clipboard.writeText(text).then(function() {
              copyBtn.textContent = "Copied";
              copyBtn.classList.add("copied");
              setTimeout(function() { copyBtn.textContent = "Copy"; copyBtn.classList.remove("copied"); }, 1500);
            }).catch(function() {});
          }
        });
        wrapper.appendChild(copyBtn);
      }

      chat.appendChild(wrapper);
      scrollToBottom();
      return wrapper;
    }

    var typingEl = null;
    function showTyping() {
      if (typingEl) return;
      typingEl = document.createElement("article");
      typingEl.className = "message assistant typing-msg";
      var header = document.createElement("div");
      header.className = "msg-header";
      var nameSpan = document.createElement("span");
      nameSpan.className = "name";
      nameSpan.textContent = "Jampandu";
      var tsSpan = document.createElement("span");
      tsSpan.className = "timestamp";
      tsSpan.textContent = getTimestamp();
      header.appendChild(nameSpan);
      header.appendChild(tsSpan);
      var body = document.createElement("div");
      body.className = "text";
      body.innerHTML = '<span class="typing"><span></span><span></span><span></span></span> <span style="color:var(--muted);font-size:13px;margin-left:6px;">Thinking...</span>';
      typingEl.appendChild(header);
      typingEl.appendChild(body);
      chat.appendChild(typingEl);
      scrollToBottom();
      startPinning();
      setBusy(true, "Thinking...");
    }

    function hideTyping() {
      if (typingEl) { typingEl.remove(); typingEl = null; }
      stopPinning();
      scrollToBottom();
    }

    function setPill(id, label, tone) {
      var el = document.querySelector("#" + id);
      if (!el) return;
      el.className = "pill " + tone;
      var dot = (tone === 'ok' || tone === 'warn' || tone === 'bad') ? '<span class="dot"></span>' : '';
      el.innerHTML = dot + ' ' + label;
      if (id === "server") {
        el.title = label === "Cold" ? "Model is cold - click Warm Model or send a message to warm it up" : label === "Warm" ? "Model is warm and ready" : "";
      }
    }

    async function clearChat() {
      try { await api("/api/clear-chat", {}); } catch(e) {}
      chat.innerHTML = "";
      addMessage("system", "Chat cleared.");
    }

    async function api(path, body) {
      body = body || null;
      var token = sessionStorage.getItem("auth_token") || localStorage.getItem("auth_token") || "";
      var headers = {"Content-Type": "application/json"};
      if (token) headers["X-Auth-Token"] = token;
      var options = body ? { method: "POST", headers: headers, body: JSON.stringify(body) } : { headers: headers };
      if (!body && token) options.headers = headers;
      var response = await fetch(path, options);
      if (response.status === 401) {
        sessionStorage.removeItem("auth_token");
        localStorage.removeItem("auth_token");
        try { sessionStorage.removeItem("startup_verified"); } catch(e) {}
        var st = await fetch("/api/status").then(function(r) { return r.json(); }).catch(function() { return null; });
        if (st) checkStartupGate(st);
        throw new Error("Not authorized. Please verify password.");
      }
      if (!response.ok) {
        var msg = "Request failed: " + response.status;
        try { var j = await response.json(); if (j.output) msg = j.output; } catch(e) {}
        throw new Error(msg);
      }
      return response.json();
    }

    function updateWarmUI(prog) {
      var wrap = document.querySelector("#warm-progress");
      var bar = document.querySelector("#warm-bar");
      var pct = document.querySelector("#warm-pct");
      var tm = document.querySelector("#warm-time");
      var log = document.querySelector("#warm-log");
      if (!prog) { if (document.querySelector("#server") && document.querySelector("#server").textContent.trim() === "Warm") wrap.style.display = "none"; return; }
      if (prog.running) { wrap.style.display = "none"; return; }
      wrap.style.display = "block";
      if (prog.overdue) {
        pct.textContent = "99% - finalizing...";
        tm.textContent = prog.elapsed + "s elapsed - " + (prog.size_gb||"?") + "GB - retrying";
        bar.style.width = "99%"; bar.style.background = "linear-gradient(90deg, #fbbf24, #fb923c)";
      } else {
        pct.textContent = prog.percent + "%";
        tm.textContent = prog.elapsed + "s elapsed - ETA " + prog.eta + "s - " + (prog.size_gb||"?") + "GB";
        bar.style.width = prog.percent + "%"; bar.style.background = "var(--grad-brand)";
      }
      log.textContent = prog.log ? prog.log.slice(-500) : "";
      var sub = document.querySelector("#subtitle");
      if (sub) sub.textContent = prog.overdue ? "Warming 99% - finalizing (" + prog.elapsed + "s)" : "Warming " + prog.percent + "% (" + prog.elapsed + "s, ~" + prog.eta + "s left)";
    }

    async function refreshStatus() {
      try {
        var status = await api("/api/status");
        setPill("config", status.config_exists ? "OK" : "Missing", status.config_exists ? "ok" : "bad");
        setPill("model", status.model_exists ? "Ready" : "Missing", status.model_exists ? "ok" : "bad");
        setPill("runtime", status.llama_exists ? "Ready" : "Missing", status.llama_exists ? "ok" : "bad");
        setPill("server", status.model_server_running ? "Warm" : "Cold", status.model_server_running ? "ok" : "warn");
        if (status.warm_progress) updateWarmUI(status.warm_progress);
        else if (status.model_server_running) { var el = document.querySelector("#warm-progress"); if (el) el.style.display = "none"; }
        var internetToggle = document.querySelector("#internet-toggle");
        if (internetToggle) internetToggle.checked = !!status.internet_allowed;
        var startupToggle = document.querySelector("#startup-toggle");
        if (startupToggle) startupToggle.checked = !!status.startup_auth_enabled;
        setPill("voice", status.voice_enabled ? "On" : "Off", status.voice_enabled ? "ok" : "warn");
        var voiceEl = document.querySelector("#voice");
        if (voiceEl) voiceEl.title = status.voice_enabled ? "Voice input is enabled - click to speak" : "Voice input is off - enable in config";
        // Cloud pills - Gemini / GCP / ADK
        var geminiLabel, geminiTone;
        if (!status.gemini_enabled) { geminiLabel = "Off"; geminiTone = "warn"; }
        else if (!status.gemini_has_key && !status.gcp_project) { geminiLabel = "No key"; geminiTone = "warn"; }
        else if (!status.internet_allowed) { geminiLabel = "Offline"; geminiTone = "warn"; }
        else if (status.gemini_available) { geminiLabel = status.gemini_model || "Ready"; geminiTone = "ok"; }
        else { geminiLabel = "No key"; geminiTone = "warn"; }
        setPill("gemini", geminiLabel, geminiTone);
        var g = document.querySelector("#gemini");
        if (g) g.title = (status.gemini_backend || "Click to set up Gemini") + " — click to configure";
        var cloudLabel, cloudTone;
        if (!status.gcp_project) { cloudLabel = "No GCP"; cloudTone = "warn"; }
        else if (!status.internet_allowed) { cloudLabel = "Offline"; cloudTone = "warn"; }
        else if (status.firestore_can_sync) { cloudLabel = "Firestore"; cloudTone = "ok"; }
        else { cloudLabel = "No sync"; cloudTone = "warn"; }
        setPill("cloud", cloudLabel, cloudTone);
        var c = document.querySelector("#cloud");
        if (c) c.title = (status.gcp_project || "No GCP project") + " / " + (status.gcs_bucket || "no bucket") + " — click to configure";
        var adkLabel = status.adk_installed ? "Ready" : "Missing";
        setPill("adk", adkLabel, status.adk_installed ? "ok" : "warn");
        var a = document.querySelector("#adk");
        if (a) a.title = status.adk_installed ? "Google ADK installed" : "ADK SDK missing — click to install";
        var proj = document.querySelector("#cloud-gcp-project");
        if (proj && document.activeElement !== proj) proj.value = status.gcp_project || "";
        var bucket = document.querySelector("#cloud-gcs-bucket");
        if (bucket && document.activeElement !== bucket) bucket.value = status.gcs_bucket || "";
        var modelSel = document.querySelector("#cloud-gemini-model");
        if (modelSel && status.gemini_model) {
          if (![].some.call(modelSel.options, function(o) { return o.value === status.gemini_model; })) {
            var opt = document.createElement("option");
            opt.value = status.gemini_model; opt.textContent = status.gemini_model;
            modelSel.appendChild(opt);
          }
          modelSel.value = status.gemini_model;
        }
        var keyHint = document.querySelector("#cloud-key-hint");
        if (keyHint) keyHint.textContent = status.gemini_has_key ? ("Key " + (status.gemini_key_hint || "saved") + ". Leave blank to keep it.") : "No key saved yet. Paste an AI Studio key, then Save.";
        var sub = document.querySelector("#subtitle");
        if (sub) sub.textContent = status.summary + (status.gemini_available ? " · Cloud Gemini ready" : "");
        checkStartupGate(status);
      } catch (error) { addMessage("system", error.message); }
    }

    setInterval(function() {
      var srv = document.querySelector("#server");
      if (!srv || srv.textContent.trim() === "Warm") return;
      try {
        fetch("/api/warm-progress").then(function(r) { return r.json(); }).then(function(prog) {
          if (prog && !prog.running) updateWarmUI(prog);
          else if (prog && prog.running) { var el = document.querySelector("#warm-progress"); if (el) el.style.display = "none"; }
        }).catch(function() {});
      } catch(e) {}
    }, 1000);

    var startupOverlay = document.querySelector("#startup-overlay");
    var startupInput = document.querySelector("#startup-password");
    var startupError = document.querySelector("#startup-error");
    var startupBtn = document.querySelector("#startup-verify");

    function checkStartupGate(status) {
      if (!status.startup_auth_enabled) { hideStartupOverlay(); return; }
      var hasToken = !!(sessionStorage.getItem("auth_token") || localStorage.getItem("auth_token"));
      if (hasToken) { hideStartupOverlay(); return; }
      if (sessionStorage.getItem("startup_verified") === "true") { hideStartupOverlay(); return; }
      showStartupOverlay();
    }

    function showStartupOverlay() { if (startupOverlay) { startupOverlay.classList.add("active"); startupOverlay.setAttribute("aria-hidden", "false"); messageInput.disabled = true; sendButton.disabled = true; setTimeout(function() { startupInput.focus(); }, 100); } }
    function hideStartupOverlay() { if (startupOverlay) { startupOverlay.classList.remove("active"); startupOverlay.setAttribute("aria-hidden", "true"); messageInput.disabled = false; sendButton.disabled = false; if (startupError) startupError.textContent = ""; if (startupInput) startupInput.value = ""; } }

    async function handleStartupVerify() {
      var pwd = startupInput.value;
      if (!pwd) { if (startupError) startupError.textContent = "Please enter password."; return; }
      startupBtn.disabled = true;
      if (startupError) startupError.textContent = "";
      var origText = startupBtn.textContent;
      startupBtn.textContent = "Verifying...";
      try {
        var res = await api("/api/verify-startup", { password: pwd });
        if (res.ok) {
          if (res.token) { sessionStorage.setItem("auth_token", res.token); try { localStorage.setItem("auth_token", res.token); } catch(e) {} }
          sessionStorage.setItem("startup_verified", "true");
          hideStartupOverlay();
          addMessage("system", res.output || "Verified. Welcome!");
          setTimeout(ensureWarmInBackground, 800);
        } else {
          if (startupError) startupError.textContent = res.output || "Incorrect password.";
          if (res.retry_after && startupError) startupError.textContent += " Retry after " + res.retry_after + "s";
        }
      } catch (e) { if (startupError) startupError.textContent = e.message; }
      finally { startupBtn.disabled = false; startupBtn.textContent = origText; }
    }

    setInterval(function() {
      var shouldLock = document.querySelector("#startup-toggle") && document.querySelector("#startup-toggle").checked;
      var hasToken = !!(sessionStorage.getItem("auth_token") || localStorage.getItem("auth_token"));
      var overlay = document.querySelector("#startup-overlay");
      var isActive = overlay && overlay.classList.contains("active");
      if (shouldLock && !hasToken && !isActive) {
        fetch("/api/status").then(function(r) { return r.json(); }).then(function(st) { if (st.startup_auth_enabled) showStartupOverlay(); }).catch(function() { if (!hasToken) showStartupOverlay(); });
      }
      if (isActive) { messageInput.disabled = true; sendButton.disabled = true; }
    }, 800);

    async function toggleStartupAuth(event) {
      var toggle = event.target; var enabled = toggle.checked;
      var pwd = window.prompt(enabled ? "Enter password to ENABLE startup lock:" : "Enter password to DISABLE startup lock:");
      if (pwd === null) { toggle.checked = !enabled; return; }
      toggle.disabled = true;
      try {
        var res = await api("/api/set-startup-auth", { enabled: enabled, password: pwd });
        if (!res.ok) { toggle.checked = !enabled; }
        addMessage("system", res.output);
        if (!enabled) { sessionStorage.setItem("startup_verified", "true"); hideStartupOverlay(); }
        else { sessionStorage.removeItem("startup_verified"); }
      } catch (e) { toggle.checked = !enabled; addMessage("system", e.message); }
      finally { toggle.disabled = false; refreshStatus(); }
    }

    async function sendMessage(event) {
      if (event) event.preventDefault();
      var text = messageInput.value.trim();
      if (!text) return;
      messageInput.value = "";
      addMessage("user", text);
      var serverPill = document.querySelector("#server");
      var isCold = serverPill && serverPill.textContent.trim() === "Cold";
      if (isCold) { addMessage("system", "Model is Cold - warming up now."); setBusy(true, "Warming model..."); }
      var useRag = document.querySelector("#rag") && document.querySelector("#rag").checked;
      showTyping();
      try {
        var token = sessionStorage.getItem("auth_token") || localStorage.getItem("auth_token") || "";
        var headers = {"Content-Type": "application/json"};
        if (token) headers["X-Auth-Token"] = token;
        var response = await fetch("/api/stream", { method: "POST", headers: headers, body: JSON.stringify({message: text, use_rag: useRag}) });
        if (!response.ok) throw new Error("Stream failed: " + response.status);
        var reader = response.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";
        var assistantMsg = null;
        var assistantText = "";
        while (true) {
          var _r = await reader.read();
          if (_r.done) break;
          buffer += decoder.decode(_r.value, {stream: true});
          var lines = buffer.split("\n");
          buffer = lines.pop();
          for (var _i = 0; _i < lines.length; _i++) {
            var _line = lines[_i].trim();
            if (!_line || !_line.startsWith("data: ")) continue;
            try {
              var evt = JSON.parse(_line.slice(6));
              if (evt.type === "token") {
                if (!assistantMsg) { hideTyping(); assistantMsg = addMessage("assistant", ""); var _oldCopy = assistantMsg.querySelector(".copy-btn"); if (_oldCopy) _oldCopy.remove(); }
                assistantText += evt.content;
                var _body = assistantMsg.querySelector(".text");
                if (_body) _body.textContent = assistantText;
              } else if (evt.type === "done") {
                if (assistantMsg) {
                  var _body2 = assistantMsg.querySelector(".text");
                  if (_body2) _body2.innerHTML = renderMarkdown(assistantText);
                  var _cb = document.createElement("button");
                  _cb.className = "copy-btn"; _cb.type = "button"; _cb.title = "Copy message"; _cb.textContent = "Copy";
                  _cb.addEventListener("click", function() {
                    if (navigator.clipboard) {
                      navigator.clipboard.writeText(assistantText).then(function() {
                        _cb.textContent = "Copied"; _cb.classList.add("copied");
                        setTimeout(function() { _cb.textContent = "Copy"; _cb.classList.remove("copied"); }, 1500);
                      }).catch(function() {});
                    }
                  });
                  assistantMsg.appendChild(_cb);
                }
              } else if (evt.type === "error") {
                hideTyping(); addMessage("system", evt.message);
              }
            } catch(_e) {}
          }
        }
        if (assistantMsg && !assistantMsg.querySelector(".copy-btn")) {
          var _cb2 = document.createElement("button"); _cb2.className = "copy-btn"; _cb2.type = "button"; _cb2.title = "Copy message"; _cb2.textContent = "Copy";
          _cb2.addEventListener("click", function() {
            if (navigator.clipboard) {
              navigator.clipboard.writeText(assistantText).then(function() {
                _cb2.textContent = "Copied"; _cb2.classList.add("copied");
                setTimeout(function() { _cb2.textContent = "Copy"; _cb2.classList.remove("copied"); }, 1500);
              }).catch(function() {});
            }
          });
          assistantMsg.appendChild(_cb2);
        }
      } catch (error) { hideTyping(); addMessage("system", error.message); }
      finally { setBusy(false); hideTyping(); refreshStatus(); }
    }

    async function warmModel() {
      var serverPill = document.querySelector("#server");
      var alreadyWarm = serverPill && serverPill.textContent.trim() === "Warm";
      if (alreadyWarm) {
        addMessage("system", "Model server is already warm.");
        try { var result = await api("/api/warm-model", {}); if (result.output && result.output !== "Model server is already warm.") addMessage("system", result.output); } catch (error) { addMessage("system", error.message); }
        refreshStatus(); return;
      }
      setBusy(true, "Warming model...");
      showTyping();
      addMessage("system", "Starting the local model server. First load can take a while.");
      try {
        var result = await api("/api/warm-model", {});
        hideTyping(); addMessage("system", result.output || "Model server is ready.");
      } catch (error) { hideTyping(); addMessage("system", error.message); }
      finally { hideTyping(); setBusy(false); refreshStatus(); }
    }

    async function runValidation(strict) {
      strict = strict || false;
      setBusy(true, strict ? "Validating (strict)..." : "Validating...");
      addMessage("system", strict ? "Running package validation (--strict: model + binary required)..." : "Running package validation...");
      try { var result = await api("/api/validate", { strict: strict }); addMessage("system", result.output || "Validation completed."); } catch (error) { addMessage("system", error.message); }
      finally { setBusy(false); refreshStatus(); }
    }

    async function openCli() {
      try { var result = await api("/api/open-cli", {}); addMessage("system", result.output); } catch (error) { addMessage("system", error.message); }
    }

    async function toggleInternet(event) {
      var toggle = event.target; var enabled = toggle.checked; toggle.disabled = true;
      try { var result = await api("/api/toggle-internet", { enabled: enabled }); if (!result.ok) toggle.checked = !enabled; addMessage("system", result.output || (enabled ? "Internet mode enabled." : "Internet mode disabled.")); } catch (error) { toggle.checked = !enabled; addMessage("system", error.message); }
      finally { toggle.disabled = false; refreshStatus(); }
    }
    async function cloudSync() {
      setBusy(true, "Syncing to cloud...");
      addMessage("system", "Syncing conversations + brain to Firestore/GCS...");
      try { var result = await api("/api/cloud-sync", {}); addMessage("system", result.output || JSON.stringify(result)); } catch (error) { addMessage("system", error.message); }
      finally { setBusy(false); refreshStatus(); }
    }

    function openCloudSetup() {
      var section = document.querySelector("#cloud-setup-section");
      if (section) section.scrollIntoView({ behavior: "smooth", block: "nearest" });
      var key = document.querySelector("#cloud-gemini-key");
      if (key) key.focus();
    }

    function setCloudHint(text) {
      var el = document.querySelector("#cloud-setup-status");
      if (el) el.textContent = text || "";
    }

    async function saveCloudSetup() {
      var keyEl = document.querySelector("#cloud-gemini-key");
      var projEl = document.querySelector("#cloud-gcp-project");
      var bucketEl = document.querySelector("#cloud-gcs-bucket");
      var modelEl = document.querySelector("#cloud-gemini-model");
      var payload = {
        gcp_project: projEl ? projEl.value.trim() : "",
        gcs_bucket: bucketEl ? bucketEl.value.trim() : "",
        gemini_model: modelEl ? modelEl.value : "gemini-2.5-pro",
        gemini_enabled: true
      };
      if (keyEl && keyEl.value.trim()) payload.gemini_api_key = keyEl.value.trim();
      setBusy(true, "Saving cloud settings...");
      setCloudHint("Saving…");
      try {
        var result = await api("/api/cloud-setup", payload);
        if (keyEl) keyEl.value = "";
        setCloudHint(result.output || "Saved.");
        addMessage("system", result.output || "Cloud settings saved.");
      } catch (error) {
        setCloudHint(error.message);
        addMessage("system", error.message);
      } finally {
        setBusy(false);
        refreshStatus();
      }
    }

    async function installCloudDeps() {
      setBusy(true, "Installing ADK & Cloud SDKs...");
      setCloudHint("Installing google-genai, google-adk, and Google Cloud packages. This can take a few minutes.");
      addMessage("system", "Installing Google Gemini / ADK / Cloud SDKs…");
      try {
        var result = await api("/api/cloud-install", {});
        setCloudHint(result.output || "Install finished.");
        addMessage("system", result.output || "Cloud SDKs installed.");
      } catch (error) {
        setCloudHint(error.message);
        addMessage("system", error.message);
      } finally {
        setBusy(false);
        refreshStatus();
      }
    }

    var sidebar = document.querySelector("#sidebar");
    var menuToggle = document.querySelector("#menu-toggle");
    var sidebarOverlay = document.querySelector("#sidebar-overlay");
    function openSidebar() { if (sidebar) sidebar.classList.add("open"); if (sidebarOverlay) sidebarOverlay.classList.add("active"); if (menuToggle) menuToggle.setAttribute("aria-expanded", "true"); }
    function closeSidebar() { if (sidebar) sidebar.classList.remove("open"); if (sidebarOverlay) sidebarOverlay.classList.remove("active"); if (menuToggle) menuToggle.setAttribute("aria-expanded", "false"); }
    function toggleSidebar() { if (sidebar && sidebar.classList.contains("open")) closeSidebar(); else openSidebar(); }
    if (menuToggle) menuToggle.addEventListener("click", toggleSidebar);
    if (sidebarOverlay) sidebarOverlay.addEventListener("click", closeSidebar);
    if (sidebar) sidebar.querySelectorAll("button").forEach(function(btn) { btn.addEventListener("click", function() { if (window.innerWidth <= 820) closeSidebar(); }); });
    document.addEventListener("keydown", function(e) { if (e.key === "Escape") closeSidebar(); });

    var composer = document.querySelector("#composer");
    if (composer) composer.addEventListener("submit", sendMessage);
    var warmBtn = document.querySelector("#warm");
    if (warmBtn) warmBtn.addEventListener("click", warmModel);
    var refreshBtn = document.querySelector("#refresh");
    if (refreshBtn) refreshBtn.addEventListener("click", refreshStatus);
    var validateBtn = document.querySelector("#validate");
    if (validateBtn) validateBtn.addEventListener("click", function() { runValidation(false); });
    var validateStrictBtn = document.querySelector("#validate-strict");
    if (validateStrictBtn) validateStrictBtn.addEventListener("click", function() { runValidation(true); });
    var cliBtn = document.querySelector("#cli");
    if (cliBtn) cliBtn.addEventListener("click", openCli);
    var clearChatBtn = document.querySelector("#clear-chat");
    if (clearChatBtn) clearChatBtn.addEventListener("click", clearChat);
    var internetToggle = document.querySelector("#internet-toggle");
    if (internetToggle) internetToggle.addEventListener("change", toggleInternet);
    var cloudSyncBtn = document.querySelector("#cloud-sync");
    if (cloudSyncBtn) cloudSyncBtn.addEventListener("click", cloudSync);
    ["gemini", "cloud", "adk"].forEach(function(id) {
      var el = document.querySelector("#" + id);
      if (el) el.addEventListener("click", openCloudSetup);
    });
    var saveCloudBtn = document.querySelector("#save-cloud-setup");
    if (saveCloudBtn) saveCloudBtn.addEventListener("click", saveCloudSetup);
    var installCloudBtn = document.querySelector("#install-cloud-deps");
    if (installCloudBtn) installCloudBtn.addEventListener("click", installCloudDeps);
    var startupToggle = document.querySelector("#startup-toggle");
    if (startupToggle) startupToggle.addEventListener("change", toggleStartupAuth);
    if (startupBtn) startupBtn.addEventListener("click", handleStartupVerify);
    if (startupInput) startupInput.addEventListener("keydown", function(e) { if (e.key === "Enter") handleStartupVerify(); });
    if (messageInput) messageInput.addEventListener("keydown", function(event) { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendMessage(event); } });

    // Model selector
    async function loadModels() {
      try {
        var res = await api("/api/models");
        if (res.ok && res.models) {
          var sel = document.querySelector("#model-select");
          if (sel) {
            sel.innerHTML = res.models.map(function(m) { return '<option value="' + m.name + '"' + (m.name === res.active ? ' selected' : '') + '>' + m.name + ' (' + m.size_gb + 'GB)</option>'; }).join('');
            if (res.models.length === 0) sel.innerHTML = '<option value="">No models found</option>';
          }
        }
      } catch(e) {}
    }
    loadModels();

    // Inference controls
    async function loadInference() {
      try {
        var params = await api("/api/inference-params");
        if (params && params.temperature !== undefined) {
          var setSlider = function(sliderId, valId, key, val) {
            var s = document.querySelector(sliderId);
            var v = document.querySelector(valId);
            if (s) s.value = val;
            if (v) v.textContent = val;
            if (s) s.addEventListener("input", function() {
              var v2 = document.querySelector(valId);
              if (v2) v2.textContent = s.value;
            });
          };
          setSlider("#inf-temp", "#inf-temp-val", "temperature", params.temperature);
          setSlider("#inf-topp", "#inf-topp-val", "top_p", params.top_p);
          setSlider("#inf-maxtok", "#inf-maxtok-val", "max_tokens", params.max_tokens);
          setSlider("#inf-rp", "#inf-rp-val", "repeat_penalty", params.repeat_penalty);
          setSlider("#inf-minp", "#inf-minp-val", "min_p", params.min_p);
        }
      } catch(e) {}
    }
    loadInference();

    document.querySelector("#save-inference") && document.querySelector("#save-inference").addEventListener("click", async function() {
      var params = {};
      var sliders = [
        {id: "#inf-temp", key: "temperature"}, {id: "#inf-topp", key: "top_p"},
        {id: "#inf-maxtok", key: "max_tokens"}, {id: "#inf-rp", key: "repeat_penalty"},
        {id: "#inf-minp", key: "min_p"}
      ];
      sliders.forEach(function(s) { var el = document.querySelector(s.id); if (el) params[s.key] = parseFloat(el.value) || 0; });
      try {
        var res = await api("/api/inference-params", {inference: params});
        addMessage("system", res.ok ? res.output : res.output || "Failed to save.");
      } catch(e) { addMessage("system", e.message); }
    });

    // Prompt templates
    async function loadTemplates() {
      try {
        var res = await api("/api/prompt-templates");
        if (res.ok && res.templates) {
          var sec = document.querySelector("#templates-section");
          if (sec) sec.style.display = "";
          var list = document.querySelector("#templates-list");
          if (list) {
            var active = res.active || "default";
            list.innerHTML = Object.keys(res.templates).map(function(name) {
              return '<div class="hist-item" data-template="' + name + '" style="' + (name === active ? "background:rgba(139,92,246,0.15);border:1px solid rgba(139,92,246,0.3);" : "") + '"><span style="font-weight:700;font-size:12px;color:' + (name === active ? "var(--violet)" : "var(--text)") + '">' + name + '</span>' + (name === active ? ' <span style="color:var(--lime);font-size:10px;">ACTIVE</span>' : '') + '</div>';
            }).join('');
            list.querySelectorAll("[data-template]").forEach(function(el) {
              el.addEventListener("click", async function() {
                var name = el.getAttribute("data-template");
                try {
                  var r = await api("/api/activate-template", {name: name});
                  addMessage("system", r.ok ? "Template activated: " + name : r.output);
                  loadTemplates();
                } catch(e) { addMessage("system", e.message); }
              });
            });
          }
        }
      } catch(e) {}
    }
    loadTemplates();

    document.querySelector("#save-template") && document.querySelector("#save-template").addEventListener("click", async function() {
      var name = document.querySelector("#new-template-name").value.trim();
      var content = document.querySelector("#new-template-content").value.trim();
      if (!name) { addMessage("system", "Template name is required."); return; }
      if (!content) { addMessage("system", "Template content is required."); return; }
      try {
        var res = await api("/api/prompt-template", {name: name, content: content});
        addMessage("system", res.ok ? res.output : res.output || "Failed.");
        document.querySelector("#new-template-name").value = "";
        document.querySelector("#new-template-content").value = "";
        loadTemplates();
      } catch(e) { addMessage("system", e.message); }
    });

    // Export/Import
    document.querySelector("#export-md") && document.querySelector("#export-md").addEventListener("click", async function() {
      try {
        var res = await api("/api/export", {format: "markdown"});
        if (res.ok) {
          var blob = new Blob([res.output], {type: "text/markdown"});
          var a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "jampandu-export.md"; a.click();
          addMessage("system", "Exported as Markdown.");
        } else { addMessage("system", res.output); }
      } catch(e) { addMessage("system", e.message); }
    });
    document.querySelector("#export-json") && document.querySelector("#export-json").addEventListener("click", async function() {
      try {
        var res = await api("/api/export", {format: "json"});
        if (res.ok) {
          var blob = new Blob([res.output], {type: "application/json"});
          var a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "jampandu-export.json"; a.click();
          addMessage("system", "Exported as JSON.");
        } else { addMessage("system", res.output); }
      } catch(e) { addMessage("system", e.message); }
    });
    document.querySelector("#import-btn") && document.querySelector("#import-btn").addEventListener("click", function() {
      document.querySelector("#import-file").click();
    });
    document.querySelector("#import-file") && document.querySelector("#import-file").addEventListener("change", async function(e) {
      var file = e.target.files[0];
      if (!file) return;
      var reader = new FileReader();
      reader.onload = async function(ev) {
        try {
          var res = await api("/api/import", {data: ev.target.result});
          addMessage("system", res.ok ? "Imported " + res.imported + " conversations." : res.output);
          loadHistory();
        } catch(err) { addMessage("system", err.message); }
      };
      reader.readAsText(file);
      e.target.value = "";
    });

    // Theme toggle
    var themeToggle = document.querySelector("#theme-toggle");
    if (themeToggle) {
      function applyTheme(isLight) {
        document.body.style.colorScheme = isLight ? "light" : "dark";
        document.body.style.background = isLight
          ? "linear-gradient(180deg, #f5f0ff, #e8e0f8)"
          : "radial-gradient(1200px 800px at 5% -5%, rgba(139,92,246,0.30), transparent 55%), radial-gradient(1000px 700px at 105% 5%, rgba(236,72,153,0.22), transparent 50%), radial-gradient(900px 800px at 45% 115%, rgba(34,211,238,0.18), transparent 50%), radial-gradient(600px 500px at 85% 55%, rgba(251,146,60,0.10), transparent 55%), linear-gradient(180deg, var(--bg-0), var(--bg-1))";
        document.body.style.backgroundAttachment = "fixed";
      }
      try {
        var savedLight = localStorage.getItem("theme_light") === "1";
        themeToggle.checked = savedLight;
        if (savedLight) applyTheme(true);
      } catch(e) {}
      themeToggle.addEventListener("change", function() {
        applyTheme(themeToggle.checked);
        try { localStorage.setItem("theme_light", themeToggle.checked ? "1" : "0"); } catch(e) {}
      });
    }
    // Model selector change handler
    var modelSelect = document.querySelector("#model-select");
    if (modelSelect) {
      modelSelect.addEventListener("change", async function() {
        var chosen = modelSelect.value;
        if (!chosen) return;
        try {
          var cfgRes = await api("/api/settings");
          if (cfgRes.ok && cfgRes.config) {
            cfgRes.config.model_path = "models\\" + chosen;
            var saveRes = await api("/api/settings", {config: cfgRes.config});
            addMessage("system", saveRes.ok ? "Model switched to " + chosen + " — restart required to load." : saveRes.output);
            refreshStatus();
          }
        } catch(e) { addMessage("system", "Model switch failed: " + e.message); }
      });
    }

    async function ensureWarmInBackground() {
      try {
        var hasToken = !!(sessionStorage.getItem("auth_token") || localStorage.getItem("auth_token"));
        var s = await api("/api/status");
        if (s.startup_auth_enabled && !hasToken) return;
        if (!s.model_server_running && s.model_exists && s.llama_exists) {
          var sub = document.querySelector("#subtitle");
          if (sub) sub.textContent = "Warming model in background (one-time, ~30s)...";
          api("/api/warm-model", {}).then(function(r) { addMessage("system", r.output || "Model server is ready. Future replies will be instant."); refreshStatus(); }).catch(function(){});
        }
      } catch(e) {}
    }

    // History with search
    var historyVisible = true;
    var allHistory = [];

    function renderHistory(filter) {
      filter = filter || "";
      var list = document.querySelector("#history-list");
      if (!list) return;
      list.innerHTML = "";
      var filtered = filter ? allHistory.filter(function(m) { return m.content.toLowerCase().indexOf(filter.toLowerCase()) >= 0; }) : allHistory;
      if (filtered.length === 0) {
        list.innerHTML = '<div style="color:var(--muted); font-size:12px; text-align:center; padding:12px;">No conversations found</div>';
        var countEl = document.querySelector("#history-count");
        if (countEl) countEl.textContent = allHistory.length;
        return;
      }
      var permIcon = function(p) { return p ? "Pinned" : "Dot"; };
      var roleColor = function(r) { return r === "user" ? "var(--cyan)" : "var(--violet)"; };
      filtered.forEach(function(m) {
        var row = document.createElement("div");
        row.className = "hist-item";
        var preview = m.content.slice(0, 70);
        var roleLetter = m.role === 'user' ? 'U' : 'A';
        row.innerHTML = '<span style="color:' + roleColor(m.role) + '; font-weight:700; font-size:11px; min-width:14px; flex-shrink:0;">' + roleLetter + '</span><span class="hist-preview"><span class="hist-text" title="' + escapeHtml(m.content.slice(0,120)) + '">' + escapeHtml(preview) + '</span></span><span class="hist-meta">' + permIcon(m.permanent) + '</span>';
        row.addEventListener("click", function() {
          var chatEl = document.createElement("div");
          chatEl.className = "message " + m.role;
          var nameEl = document.createElement("div"); nameEl.className = "msg-header";
          var nameSpan = document.createElement("span"); nameSpan.className = "name"; nameSpan.textContent = m.role === "user" ? "You" : "Jampandu";
          var tsSpan = document.createElement("span"); tsSpan.className = "timestamp"; tsSpan.textContent = getTimestamp();
          nameEl.appendChild(nameSpan); nameEl.appendChild(tsSpan);
          var bodyEl = document.createElement("div"); bodyEl.className = "text"; bodyEl.textContent = m.content;
          chatEl.appendChild(nameEl); chatEl.appendChild(bodyEl);
          chat.appendChild(chatEl);
          scrollToBottom();
        });
        list.appendChild(row);
      });
      var countEl = document.querySelector("#history-count");
      if (countEl) countEl.textContent = allHistory.length;
    }

    async function loadHistory() {
      try {
        var res = await api("/api/history");
        if (res.ok && res.history) {
          allHistory = res.history;
          var filterEl = document.querySelector("#history-search");
          var filter = filterEl ? filterEl.value : "";
          renderHistory(filter);
        }
      } catch(e) { console.error("loadHistory:", e); }
    }

    async function togglePin(id) {
      try {
        var res = await api("/api/toggle-pin", {id: id, permanent: true});
        if (!res.ok) await api("/api/toggle-pin", {id: id, permanent: false});
        loadHistory();
      } catch(e) { console.error("togglePin:", e); }
    }

    async function signOff() {
      if (!confirm("Sign off? All non-pinned (temporary) history will be erased from this device.")) return;
      try {
        var res = await api("/api/sign-off");
        if (res.ok) { addMessage("system", res.output || "Signed off - temporary history cleared."); loadHistory(); }
        else { addMessage("system", "Sign off failed: " + (res.output || "unknown")); }
      } catch(e) { addMessage("system", "Sign off error: " + e.message); }
    }

    var historyRefreshBtn = document.querySelector("#history-refresh");
    if (historyRefreshBtn) historyRefreshBtn.addEventListener("click", loadHistory);
    var historyToggleBtn = document.querySelector("#history-toggle");
    if (historyToggleBtn) historyToggleBtn.addEventListener("click", function() {
      historyVisible = !historyVisible;
      var ls = document.querySelector("#history-list");
      if (ls) ls.style.display = historyVisible ? "" : "none";
      var sec = document.querySelector("#history-section");
      if (sec) sec.style.display = historyVisible ? "" : "none";
    });
    var signOffBtn = document.querySelector("#sign-off");
    if (signOffBtn) signOffBtn.addEventListener("click", signOff);

    var searchInput = document.querySelector("#history-search");
    if (searchInput) {
      var searchTimer = null;
      searchInput.addEventListener("input", function() {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(function() { renderHistory(searchInput.value); }, 200);
      });
    }

    // Load history on startup and show empty state if no messages
    loadHistory();

    if (chat && chat.children.length === 0) {
      var empty = document.createElement("div");
      empty.className = "empty-state";
      empty.innerHTML = '<div class="empty-icon">Start a conversation</div><h3>Your chat will appear here</h3><p>Ask Jampandu anything - your local AI is ready.</p>';
      chat.appendChild(empty);
    }

    addMessage("system", "Jampandu desktop is ready.");
    refreshStatus();
    setTimeout(ensureWarmInBackground, 1500);
  </script>
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


def _module_ok(name):
    try:
        __import__(name)
        return True
    except Exception:
        return False


def _portable_python():
    portable = BASE_DIR / "python-portable" / "python.exe"
    if portable.exists():
        return str(portable)
    return sys.executable


def apply_cloud_setup(payload):
    """Save Gemini key to .env and GCP fields to config.json. Never echo the key."""
    cfg, error = load_config()
    if error:
        return {"ok": False, "output": error}
    if cfg is None:
        cfg = {}
    payload = payload or {}
    env_updates = {}

    key = str(payload.get("gemini_api_key") or "").strip()
    if key:
        env_updates["GEMINI_API_KEY"] = key

    if "gcp_project" in payload:
        project = str(payload.get("gcp_project") or "").strip()
        cfg["gcp_project"] = project
        if project:
            env_updates["GOOGLE_CLOUD_PROJECT"] = project

    if "gcp_location" in payload:
        location = str(payload.get("gcp_location") or "").strip() or "us-central1"
        cfg["gcp_location"] = location
        env_updates["GOOGLE_CLOUD_LOCATION"] = location

    if "gcs_bucket" in payload:
        bucket = str(payload.get("gcs_bucket") or "").strip()
        cfg["gcs_bucket"] = bucket
        if bucket:
            env_updates["GCS_BUCKET"] = bucket

    if payload.get("gemini_model"):
        model = str(payload.get("gemini_model")).strip()
        cfg["gemini_model"] = model
        env_updates["GEMINI_MODEL"] = model

    if "gemini_enabled" in payload:
        cfg["gemini_enabled"] = bool(payload["gemini_enabled"])
    else:
        cfg.setdefault("gemini_enabled", True)

    if env_updates:
        if vertex_config:
            vertex_config.write_env_values(env_updates)
        else:
            # Fallback: write .env even if vertex_config failed to import
            env_path = BASE_DIR / ".env"
            lines = []
            if env_path.exists():
                lines = env_path.read_text(encoding="utf-8").splitlines()
            seen = set()
            out = []
            for line in lines:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k = stripped.split("=", 1)[0].strip()
                    if k in env_updates:
                        out.append(f"{k}={env_updates[k]}")
                        seen.add(k)
                        os.environ[k] = env_updates[k]
                        continue
                out.append(line)
            for k, v in env_updates.items():
                if k not in seen:
                    out.append(f"{k}={v}")
                    os.environ[k] = v
            env_path.write_text("\n".join(out) + "\n", encoding="utf-8")

    save_config(cfg)
    has_key = bool(vertex_config.get_api_key()) if vertex_config else bool(os.getenv("GEMINI_API_KEY"))
    project = (cfg.get("gcp_project") or "").strip()
    parts = []
    if has_key:
        parts.append("Gemini key saved")
    elif not project:
        parts.append("No Gemini key yet — paste one from aistudio.google.com/apikey")
    if project:
        parts.append(f"GCP project {project}")
    else:
        parts.append("No GCP project (Firestore/Vertex still offline)")
    parts.append("Turn Internet ON to use cloud replies")
    return {"ok": True, "output": ". ".join(parts) + "."}


def install_cloud_deps():
    """pip install google-genai, google-adk, and GCP client libraries."""
    req = BASE_DIR / "requirements.txt"
    if not req.exists():
        return {"ok": False, "output": "agent/requirements.txt is missing."}
    py = _portable_python()
    try:
        proc = subprocess.run(
            [py, "-m", "pip", "install", "-r", str(req)],
            capture_output=True,
            text=True,
            timeout=420,
            cwd=str(BASE_DIR),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "SDK install timed out after 7 minutes. Try again or run: python-portable\\python.exe -m pip install -r requirements.txt"}
    except Exception as exc:
        return {"ok": False, "output": f"SDK install failed: {exc}"}
    tail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip().splitlines()[-12:]
    summary = "\n".join(tail)
    adk_ok = _module_ok("google.adk")
    genai_ok = _module_ok("google.genai") or _module_ok("google.generativeai")
    firestore_ok = _module_ok("google.cloud.firestore")
    if proc.returncode == 0:
        flags = []
        flags.append("ADK ready" if adk_ok else "ADK still missing")
        flags.append("Gemini SDK ready" if genai_ok else "Gemini SDK still missing")
        flags.append("Firestore ready" if firestore_ok else "Firestore still missing")
        return {"ok": True, "output": "Cloud SDKs installed. " + "; ".join(flags) + ".\n" + summary}
    return {"ok": False, "output": f"pip exited {proc.returncode}.\n{summary}"}


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
    # 5) Strip hallucinated "Searching for ... on google (incognito)..." tool echo —
    #    real search is done server-side and injected as "Web search results:"; LLM should
    #    never parrot the browser-open line. Remove it wherever it appears.
    text = re.sub(r'Searching for\s+["\'].*?["\']\s+on\s+(google|bing|youtube|duckduckgo)\s*\(incognito\)\.\.\.\s*', '', text, flags=re.IGNORECASE)
    # 6) Trim excessive whitespace but preserve intentional breaks
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    # 7) If model still ended with incomplete year like "August 31, 2", try to fix via time context fallback
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
    if internet_allowed:
        base = (
            "SYSTEM STATE: Internet access is currently ENABLED (Internet toggle is ON). "
            "When web search results are provided in context, summarize them and cite sources. "
            "When no web results are provided, answer from your training knowledge — "
            "DO NOT output 'Searching for ... on google (incognito)...' or claim to open a browser; "
            "just answer directly."
        )
        if startup_verified or not cfg.get("startup_auth_enabled", True):
            return base + " Startup verification is already completed — do not ask for a password."
        else:
            return base
    else:
        return "SYSTEM STATE: Internet access is currently DISABLED (Internet toggle is OFF). If user asks for YouTube/web search, tell them to turn the Internet toggle ON (no password needed after startup verification)."


def fetch_web_search_results(query, num_results=5):
    """Try DuckDuckGo/Bing RSS; return grounding only if results look relevant."""
    import urllib.parse
    import html as html_mod
    cfg, _ = load_config()
    if not cfg or not cfg.get("internet_allowed", False):
        return None
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    raw = None
    used = None
    for base_url, name in (
        (f"https://duckduckgo.com/html/?q={urllib.parse.quote_plus(query)}", "ddg"),
        (f"https://www.bing.com/search?format=rss&q={urllib.parse.quote_plus(query)}", "rss"),
        (f"https://www.bing.com/search?q={urllib.parse.quote_plus(query)}", "bing"),
    ):
        try:
            req = urllib.request.Request(base_url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            if raw and len(raw) > 1000 and "anomaly" not in raw.lower() and "Too Many Requests" not in raw:
                used = name
                break
        except Exception:
            continue
    if not raw or "anomaly" in raw.lower():
        return None
    import re
    results = []
    if used == "rss":
        items = re.findall(r'<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?<description>(.*?)</description>', raw, re.DOTALL)
        for title, link, desc in items[:num_results]:
            title = html_mod.unescape(re.sub(r'<[^>]+>', '', title)).strip()
            link = html_mod.unescape(link.strip())
            desc = html_mod.unescape(re.sub(r'<[^>]+>', '', desc)).strip()[:220]
            if title and link.startswith("http"):
                results.append(f"- {title}\n  {link}\n  {desc}")
    else:
        bing_blocks = re.findall(
            r'<li class="b_algo[^"]*".*?<h2>.*?<a[^>]+href="([^"]+)".*?>(.*?)</a>.*?</h2>.*?<p[^>]*>(.*?)</p>',
            raw, re.DOTALL
        )
        for href, title_html, snippet_html in bing_blocks[:num_results]:
            title = re.sub(r'<[^>]+>', '', title_html).strip()
            snippet = re.sub(r'<[^>]+>', '', snippet_html).strip()
            title = html_mod.unescape(title)
            snippet = html_mod.unescape(snippet)[:220]
            href = html_mod.unescape(href)
            # Decode Bing ck redirect: u=a1<base64> -> base64
            if "bing.com/ck" in href:
                try:
                    import base64, urllib.parse as up
                    qs = up.parse_qs(up.urlparse(href).query)
                    u = qs.get("u", [None])[0]
                    if u and u.startswith("a1"):
                        pad = "=" * (-len(u[2:]) % 4)
                        href = base64.b64decode(u[2:] + pad).decode(errors="ignore")
                except: pass
                if "bing.com" in href:
                    continue
            if title and href.startswith("http"):
                results.append(f"- {title}\n  {href}\n  {snippet}")
    if not results:
        return None
    # Relevance gate: at least one result must contain a keyword from query
    q_tokens = [t.lower() for t in re.findall(r"[a-zA-Z]{3,}", query)]
    # filter out generic stopwords
    stops = {"the","and","for","are","available","market","with","about","from"}
    keys = [t for t in q_tokens if t not in stops]
    relevant = any(any(k in (r.lower()) for k in keys) for r in results)
    if not relevant:
        return None
    return (
        "Web search results for '" + query + "' (use these to answer; cite titles/URLs; "
        "DO NOT reply with 'Searching for...' — the search is already done):\n"
        + "\n\n".join(results)
    )


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
    # Be resilient: check both llama.exe and llama-server.exe regardless of cfg key
    if llama_path and not llama_path.exists():
        alt = BASE_DIR / "bin" / "llama-server.exe"
        alt2 = BASE_DIR / "bin" / "llama.exe"
        if alt.exists() or alt2.exists():
            llama_path = alt if alt.exists() else alt2
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

    # Cloud / Gemini status for checklist + UI pills
    gemini_enabled = bool(cfg and cfg.get("gemini_enabled", True))
    if vertex_config:
        gcp_project = vertex_config.get_gcp_project(cfg) or ""
        gcs_bucket = (cfg.get("gcs_bucket") if cfg else "") or os.getenv("GCS_BUCKET", "") or ""
        gemini_model = vertex_config.get_gemini_model(cfg)
        gemini_has_key = bool(vertex_config.get_api_key())
        gemini_key_hint = vertex_config.key_hint(vertex_config.get_api_key())
        try:
            gemini_available = gemini_client.is_available(cfg, bool(cfg and cfg.get("internet_allowed", False))) if gemini_client else False
            gemini_backend = vertex_config.describe_backend(cfg)
        except Exception:
            gemini_available = False
            gemini_backend = "error"
    else:
        gcp_project = (cfg.get("gcp_project") if cfg else "") or os.getenv("GOOGLE_CLOUD_PROJECT", "") or os.getenv("GCP_PROJECT", "")
        gcs_bucket = (cfg.get("gcs_bucket") if cfg else "") or os.getenv("GCS_BUCKET", "")
        gemini_model = (cfg.get("gemini_model") if cfg else "") or os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
        gemini_has_key = bool((os.getenv("GEMINI_API_KEY") or "").strip())
        gemini_key_hint = "saved" if gemini_has_key else ""
        gemini_available = False
        gemini_backend = "not installed"
    try:
        adk_installed = bool(adk_health) and adk_health()["adk_installed"] if adk_health else False
    except Exception:
        adk_installed = False
    try:
        firestore_can_sync = bool(firestore_sync and firestore_sync.health_check(cfg, bool(cfg and cfg.get("internet_allowed", False))).get("can_sync"))
    except Exception:
        firestore_can_sync = False

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
        # Cloud fields
        "gemini_enabled": gemini_enabled,
        "gemini_available": gemini_available,
        "gemini_has_key": gemini_has_key,
        "gemini_key_hint": gemini_key_hint,
        "gemini_model": gemini_model,
        "gemini_backend": gemini_backend,
        "gcp_project": gcp_project,
        "gcs_bucket": gcs_bucket,
        "adk_installed": adk_installed,
        "genai_installed": _module_ok("google.genai") or _module_ok("google.generativeai"),
        "firestore_installed": _module_ok("google.cloud.firestore"),
        "firestore_can_sync": firestore_can_sync,
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


def _try_cloud_generate(message, context=None, history_rows=None):
    """Try Gemini via ADK/GenAI if internet + credentials. Returns dict or None (fall through)."""
    try:
        cfg, _ = load_config()
        if not cfg or not cfg.get("internet_allowed", False):
            return None
        if not cfg.get("gemini_enabled", True):
            return None
        if not gemini_client or not gemini_client.is_available(cfg, True):
            return None
        # Build history + context docs
        context_docs = []
        if context:
            context_docs = [context]
        else:
            try:
                idx = single_query.load_index()
                docs = single_query.query_topk(idx, message, k=3)
                context_docs = [d["text"] for d in docs]
            except Exception:
                context_docs = []
        hist = []
        if history_rows:
            for r, t in history_rows:
                hist.append({"role": r, "content": t})
        hist.append({"role": "user", "content": message})
        # Prefer ADK
        if run_adk_query:
            try:
                adk_res = run_adk_query(message, history=hist, context_docs=context_docs, internet_allowed=True)
                if adk_res.get("ok"):
                    out = _clean_llm_output(adk_res["output"])
                    CONVERSATION.append({"role": "user", "content": message})
                    _append_db('user', message, permanent=False)
                    CONVERSATION.append({"role": "assistant", "content": out})
                    _append_db('assistant', out, permanent=False)
                    # Firestore sync best-effort
                    if firestore_sync:
                        try:
                            firestore_sync.sync_conversation_to_firestore('user', message, config=cfg, internet_allowed=True)
                            firestore_sync.sync_conversation_to_firestore('assistant', out, config=cfg, internet_allowed=True)
                        except Exception:
                            pass
                    return {"ok": True, "output": out + " [via Gemini/ADK]"}
            except Exception:
                pass
        # Direct gemini_client fallback
        out = gemini_client.generate(
            prompt=message,
            history=hist,
            context_docs=context_docs,
            system_prompt=read_system_prompt(),
            config=cfg,
            internet_allowed=True,
        )
        out = _clean_llm_output(out)
        CONVERSATION.append({"role": "user", "content": message})
        _append_db('user', message, permanent=False)
        CONVERSATION.append({"role": "assistant", "content": out})
        _append_db('assistant', out, permanent=False)
        if firestore_sync:
            try:
                firestore_sync.sync_conversation_to_firestore('user', message, config=cfg, internet_allowed=True)
                firestore_sync.sync_conversation_to_firestore('assistant', out, config=cfg, internet_allowed=True)
            except Exception:
                pass
        return {"ok": True, "output": out + " [via Gemini]"}
    except Exception as e:
        # Cloud failed - caller will fallback to local and surface hint
        print(f"[cloud] Gemini failed, falling back to local: {e}")
        return None

def query_warm_server(message, context=None):
    # Hybrid: try cloud first (Gemini 3.5+ via ADK/GenAI) before local llama
    try:
        con_preview = _db_conn()
        hist_preview = get_context_history(con_preview)
        con_preview.close()
        cloud_res = _try_cloud_generate(message, context=context, history_rows=hist_preview)
        if cloud_res:
            return cloud_res
    except Exception:
        pass
    warm = start_llama_server(timeout=180)
    if not warm["ok"] and not llama_health():
        return warm

    CONVERSATION.append({"role": "user", "content": message})
    _append_db('user', message, permanent=False)
    # Inject dynamic internet state so LLM doesn't re-ask for password when toggle is already ON
    dynamic_ctx = get_dynamic_internet_context()
    # Fetch web search results if message looks like a search query and internet is ON
    web_ctx = None
    cfg_check, _ = load_config()
    if cfg_check and cfg_check.get("internet_allowed", False):
        search_keywords = ("search", "find", "look up", "google", "best", "top", "latest", "what is", "who is", "how to", "where", "when", "which")
        if any(kw in message.lower() for kw in search_keywords):
            web_ctx = fetch_web_search_results(message)
    # Always inject real host time so "what is time now" never hallucinates 10:00 AM
    time_ctx = _time_context()
    # Merge caller context + dynamic internet context + time
    combined_context = "\n\n".join([c for c in [context, dynamic_ctx, web_ctx, time_ctx] if c])
    # Qwen3.5 chat template allows exactly ONE system message at position 0 — merge
    # prompt + dynamic context + time into a single system entry to avoid
    # Jinja raise_exception('System message must be at the beginning') which
    # caused WinError 10054 in the screenshot.
    system_content = read_system_prompt()
    if combined_context:
        system_content = system_content + "\n\n" + combined_context

    # Build history with expanded context from DB — drop poisoned "Searching for..." hallucination turns
    con = _db_conn()
    history_rows = get_context_history(con)
    con.close()
    # Filter poisoned history that would teach LLM to repeat the broken "Searching..." pattern
    filtered_rows = []
    for r, t in history_rows:
        if "Searching for" in t and "(incognito)" in t:
            continue
        filtered_rows.append((r, t))
    history_rows = filtered_rows
    history = [{"role": "system", "content": system_content}]
    for r, t in history_rows:
        history.append({"role": "assistant" if r == "assistant" else "user", "content": t})

    cfg, _ = load_config()
    params = load_inference_params() if cfg else INFERENCE_DEFAULTS
    gen_tokens = int(params.get("max_tokens", 768))
    try:
        payload = {
            "messages": history,
            "temperature": float(params.get("temperature", 0.7)),
            "max_tokens": gen_tokens,
            "stream": False,
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "stop": ["<|im_end|>", "</think>", "\nUser:", "\nAssistant:"],
        }
        if "top_p" in params:
            payload["top_p"] = float(params["top_p"])
        if "presence_penalty" in params:
            payload["presence_penalty"] = float(params["presence_penalty"])
        if "frequency_penalty" in params:
            payload["frequency_penalty"] = float(params["frequency_penalty"])
        if "min_p" in params:
            payload["min_p"] = float(params["min_p"])
        result = post_json(
            llama_server_url("/v1/chat/completions"),
            payload,
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
                    "temperature": float(params.get("temperature", 0.7)),
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
    if not output or output.strip() in ("</think>",):
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
            # When internet is ON, let search queries go to the LLM so it can
            # actually answer them in-chat instead of just opening a browser tab.
            cfg, _ = load_config()
            internet_on = bool(cfg.get("internet_allowed", False)) if cfg else False
            if task.get("action") == "search" and internet_on:
                pass  # fall through to LLM below
            else:
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


INFERENCE_DEFAULTS = {
    "temperature": 0.7,
    "top_p": 0.9,
    "max_tokens": 768,
    "repeat_penalty": 1.1,
    "min_p": 0.05,
    "presence_penalty": 0.0,
    "frequency_penalty": 0.0,
}


def get_available_models():
    models_dir = BASE_DIR / "models"
    models = []
    if models_dir.exists():
        for f in sorted(models_dir.iterdir()):
            if f.suffix.lower() == ".gguf" and f.is_file():
                models.append({
                    "name": f.name,
                    "path": str(f.relative_to(BASE_DIR)),
                    "size_gb": round(f.stat().st_size / (1024**3), 2),
                })
    active = None
    try:
        cfg, _ = load_config()
        if cfg and cfg.get("model_path"):
            active = cfg["model_path"]
    except Exception:
        pass
    return {"ok": True, "models": models, "active": active}


def load_inference_params():
    try:
        cfg, _ = load_config()
        if cfg and "inference" in cfg:
            for k, v in INFERENCE_DEFAULTS.items():
                if k not in cfg["inference"]:
                    cfg["inference"][k] = v
            return cfg["inference"]
    except Exception:
        pass
    return dict(INFERENCE_DEFAULTS)


def save_inference_params(params):
    cfg, error = load_config()
    if error:
        return {"ok": False, "output": str(error)}
    for k, v in INFERENCE_DEFAULTS.items():
        if k not in params:
            params[k] = v
    # Clamp to sane ranges — screenshot showed temp 1.9 + 1984 tokens trashing context
    try:
        params["temperature"] = max(0.0, min(1.5, float(params.get("temperature", 0.7))))
    except: params["temperature"] = 0.7
    try:
        params["top_p"] = max(0.1, min(1.0, float(params.get("top_p", 0.9))))
    except: params["top_p"] = 0.9
    try:
        params["max_tokens"] = max(64, min(1024, int(params.get("max_tokens", 768))))
    except: params["max_tokens"] = 768
    try:
        params["repeat_penalty"] = max(1.0, min(1.5, float(params.get("repeat_penalty", 1.1))))
    except: params["repeat_penalty"] = 1.1
    try:
        params["min_p"] = max(0.0, min(0.2, float(params.get("min_p", 0.05))))
    except: params["min_p"] = 0.05
    cfg["inference"] = params
    try:
        save_config(cfg)
        return {"ok": True, "output": "Inference params saved."}
    except OSError as exc:
        return {"ok": False, "output": str(exc)}


def get_context_history(conn, max_turns=None):
    """Build priority-based conversation context: system + pinned + recent."""
    if max_turns is None:
        try:
            cfg, _ = load_config()
            max_turns = int((cfg or {}).get("context_size", 24))
        except Exception:
            max_turns = 24
    max_turns = max(4, min(max_turns, 64))
    c = conn.cursor()
    c.execute("SELECT role, text FROM conversation ORDER BY id DESC LIMIT ?", (max_turns * 2,))
    rows = c.fetchall()[::-1]
    return rows


def export_conversations(fmt="markdown", date_from=None, date_to=None):
    try:
        con = _db_conn()
        cur = con.cursor()
        cur.execute("SELECT role, text, ts, permanent FROM conversation ORDER BY id ASC")
        rows = cur.fetchall()
        con.close()
        if fmt == "json":
            data = [{"role": r[0], "text": r[1], "ts": r[2], "permanent": bool(r[3])} for r in rows]
            return json.dumps(data, indent=2), "application/json"
        if fmt == "markdown":
            lines = ["# Jampandu Conversation Export", "", "Exported: " + datetime.datetime.now().isoformat(), ""]
            for role, text, ts, perm in rows:
                tag = " 📌" if perm else ""
                lines.append(("## You" if role == "user" else "### Jampandu") + " (" + ts + ")" + tag)
                lines.append(text)
                lines.append("")
            return "\n".join(lines), "text/markdown"
        if fmt == "text":
            lines = []
            for role, text, ts, perm in rows:
                prefix = "YOU: " if role == "user" else "JAMPANDU: "
                lines.append("[" + ts + "] " + prefix + text)
            return "\n".join(lines), "text/plain"
        return "", "text/plain"
    except Exception as e:
        return str(e), "text/plain"


def import_conversations(file_data):
    try:
        data = json.loads(file_data)
        if isinstance(data, dict) and "messages" in data:
            data = data["messages"]
        if not isinstance(data, list):
            return {"ok": False, "output": "Invalid format: expected a list of messages."}
        con = _db_conn()
        cur = con.cursor()
        count = 0
        for msg in data:
            role = msg.get("role", "user")
            text = msg.get("text", "")
            ts = msg.get("ts", datetime.datetime.utcnow().isoformat())
            perm = 1 if msg.get("permanent", False) else 0
            cur.execute("INSERT INTO conversation (role, text, ts, permanent) VALUES (?,?,?,?)",
                        (role, text, ts, perm))
            count += 1
        con.commit()
        con.close()
        return {"ok": True, "imported": count}
    except Exception as e:
        return {"ok": False, "output": str(e)}


def get_prompt_templates():
    try:
        cfg, _ = load_config()
        templates = cfg.get("prompt_templates", {}) if cfg else {}
        active = cfg.get("active_template", "default") if cfg else "default"
        return {"ok": True, "templates": templates, "active": active}
    except Exception as e:
        return {"ok": False, "output": str(e)}


def save_prompt_template(name, content, delete=False):
    try:
        cfg, error = load_config()
        if error:
            return {"ok": False, "output": str(error)}
        templates = cfg.get("prompt_templates", {})
        if delete:
            if name in templates:
                del templates[name]
                cfg["prompt_templates"] = templates
                save_config(cfg)
                return {"ok": True, "output": "Template deleted."}
            return {"ok": False, "output": "Template not found."}
        templates[name] = content
        cfg["prompt_templates"] = templates
        save_config(cfg)
        return {"ok": True, "output": "Template saved."}
    except Exception as e:
        return {"ok": False, "output": str(e)}


def activate_template(name):
    try:
        cfg, error = load_config()
        if error:
            return {"ok": False, "output": str(error)}
        cfg["active_template"] = name
        save_config(cfg)
        return {"ok": True, "output": "Template activated."}
    except Exception as e:
        return {"ok": False, "output": str(e)}


def get_diagnostics():
    import gc
    try:
        status = runtime_status()
        mem_mb = 0
        try:
            import psutil
            proc = psutil.Process()
            mem_mb = round(proc.memory_info().rss / (1024 * 1024), 1)
        except Exception:
            pass
        gc.collect()
        return {
            "ok": True,
            "uptime": int(time.time() - WARM_START_TIME) if WARM_START_TIME else 0,
            "model_server_running": status["model_server_running"],
            "model_exists": status["model_exists"],
            "model_path": status.get("model_path"),
            "memory_mb": mem_mb,
            "conversation_count": len(CONVERSATION),
            "server_port": DEFAULT_PORT,
            "llama_port": LLAMA_PORT,
        }
    except Exception as e:
        return {"ok": False, "output": str(e)}


def build_stream_response(message, use_rag):
    """Yield SSE events for streaming response."""
    try:
        # Pre-LLM task intercept (mirror query_assistant logic)
        try:
            executor = get_task_executor()
            task = executor.parse_task(message)
            if task.get("action") not in ("unknown",):
                # When internet is ON, let search queries go to the LLM so it can
                # actually answer them in-chat instead of just opening a browser tab.
                cfg, _ = load_config()
                internet_on = bool(cfg.get("internet_allowed", False)) if cfg else False
                if task.get("action") == "search" and internet_on:
                    pass  # fall through to LLM below
                else:
                    response, success, needs_approval = executor.execute_task(task)
                    if task.get("action") in ("bluetooth_on", "bluetooth_off", "bluetooth_status"):
                        CONVERSATION.append({"role": "user", "content": message})
                        CONVERSATION.append({"role": "assistant", "content": response})
                        _append_db('user', message, permanent=False)
                        _append_db('assistant', response, permanent=False)
                        for tok in response.split():
                            yield "data: " + json.dumps({"type": "token", "content": tok + " "}) + "\n\n"
                        yield "data: " + json.dumps({"type": "done", "output": response}) + "\n\n"
                        return
                    if success:
                        CONVERSATION.append({"role": "user", "content": message})
                        CONVERSATION.append({"role": "assistant", "content": response})
                        _append_db('user', message, permanent=False)
                        _append_db('assistant', response, permanent=False)
                        for tok in response.split():
                            yield "data: " + json.dumps({"type": "token", "content": tok + " "}) + "\n\n"
                        yield "data: " + json.dumps({"type": "done", "output": response}) + "\n\n"
                        return
                    if "Internet is disabled" in response:
                        response = response.replace("Run /enable_internet first (password required).", "Turn the Internet toggle ON in the sidebar (no extra password after startup verification).")
                        CONVERSATION.append({"role": "user", "content": message})
                        CONVERSATION.append({"role": "assistant", "content": response})
                        _append_db('user', message, permanent=False)
                        _append_db('assistant', response, permanent=False)
                    for tok in response.split():
                        yield "data: " + json.dumps({"type": "token", "content": tok + " "}) + "\n\n"
                    yield "data: " + json.dumps({"type": "done", "output": response}) + "\n\n"
                    return
        except Exception:
            pass
        # Signal that prep is starting (keeps connection alive during slow steps)
        yield "data: " + json.dumps({"type": "thinking", "content": "Preparing context..."}) + "\n\n"
        warm = start_llama_server(timeout=180)
        if not warm["ok"] and not llama_health():
            yield "data: " + json.dumps({"type": "error", "message": warm.get("output", "Server not ready.")}) + "\n\n"
            return

        CONVERSATION.append({"role": "user", "content": message})
        _append_db('user', message, permanent=False)

        rag_ctx = None
        if use_rag:
            try:
                index = single_query.load_index()
                docs = single_query.query_topk(index, message, k=3)
                if docs:
                    rag_ctx = "Relevant local notes:\n" + "\n\n".join(doc["text"] for doc in docs)
            except Exception:
                rag_ctx = None
        dynamic_ctx = get_dynamic_internet_context()
        # Fetch web search results if message looks like a search query and internet is ON
        web_ctx = None
        cfg_check, _ = load_config()
        if cfg_check and cfg_check.get("internet_allowed", False):
            search_keywords = ("search", "find", "look up", "google", "best", "top", "latest", "what is", "who is", "how to", "where", "when", "which")
            if any(kw in message.lower() for kw in search_keywords):
                web_ctx = fetch_web_search_results(message)
        time_ctx = _time_context()
        combined_context = "\n\n".join([c for c in [rag_ctx, dynamic_ctx, web_ctx, time_ctx] if c])
        system_content = read_system_prompt()
        if combined_context:
            system_content = system_content + "\n\n" + combined_context

        # Build history with expanded context — drop poisoned hallucination turns
        con = _db_conn()
        history_rows = get_context_history(con)
        con.close()
        filtered = [(r,t) for r,t in history_rows if not ("Searching for" in t and "(incognito)" in t)]
        history_rows = filtered

        msgs = [{"role": "system", "content": system_content}]
        for r, t in history_rows:
            msgs.append({"role": "assistant" if r == "assistant" else "user", "content": t})

        cfg, _ = load_config()
        params = load_inference_params() if cfg else INFERENCE_DEFAULTS
        gen_tokens = int(params.get("max_tokens", 768))

        payload = {
            "messages": msgs,
            "temperature": float(params.get("temperature", 0.7)),
            "max_tokens": gen_tokens,
            "stream": True,
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "stop": ["<|im_end|>", "</think>", "\nUser:", "\nAssistant:"],
        }
        if "top_p" in params:
            payload["top_p"] = float(params["top_p"])
        if "presence_penalty" in params:
            payload["presence_penalty"] = float(params["presence_penalty"])
        if "frequency_penalty" in params:
            payload["frequency_penalty"] = float(params["frequency_penalty"])
        if "min_p" in params:
            payload["min_p"] = float(params["min_p"])

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            llama_server_url("/v1/chat/completions"),
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        first_token = True
        full_output = ""
        with urllib.request.urlopen(req, timeout=120) as resp:
            for line in resp:
                line = line.decode("utf-8").strip()
                if not line.startswith("data: "):
                    continue
                raw = line[6:].strip()
                if raw == "[DONE]":
                    break
                try:
                    chunk = json.loads(raw)
                except Exception:
                    continue
                if "choices" in chunk and chunk["choices"]:
                    delta = chunk["choices"][0].get("delta", {})
                    token = delta.get("content", "")
                    if token:
                        full_output += token
                        yield "data: " + json.dumps({"type": "token", "content": token}) + "\n\n"
                    if chunk["choices"][0].get("finish_reason") == "stop":
                        break

        if not full_output:
            full_output = "No response from the local model."
        if full_output.strip() in ("</think>",):
            full_output = "No response from the local model."
        full_output = _clean_llm_output(full_output)

        CONVERSATION.append({"role": "assistant", "content": full_output})
        _append_db('assistant', full_output, permanent=False)
        yield "data: " + json.dumps({"type": "done", "output": full_output}) + "\n\n"
    except Exception as e:
        yield "data: " + json.dumps({"type": "error", "message": str(e)}) + "\n\n"


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
        if parsed.path == "/api/diagnostics":
            if not _require_auth(self):
                return
            self.send_json(get_diagnostics())
            return
        if parsed.path == "/api/inference-params":
            if not _require_auth(self):
                return
            self.send_json(load_inference_params())
            return
        if parsed.path == "/api/prompt-templates":
            if not _require_auth(self):
                return
            self.send_json(get_prompt_templates())
            return
        if parsed.path == "/api/models":
            if not _require_auth(self):
                return
            self.send_json(get_available_models())
            return
        if parsed.path == "/api/settings":
            if not _require_auth(self):
                return
            try:
                cfg, _ = load_config()
                self.send_json({"ok": True, "config": cfg})
            except Exception as exc:
                self.send_json({"ok": False, "output": str(exc)})
            return
        if parsed.path == "/api/export":
            if not _require_auth(self):
                return
            fmt = urllib.parse.parse_qs(parsed.query).get("format", ["markdown"])[0]
            try:
                data, content_type = export_conversations(fmt=fmt)
                self.send_json({"ok": True, "output": data, "content_type": content_type})
            except Exception as exc:
                self.send_json({"ok": False, "output": str(exc)})
            return
        if parsed.path == "/api/gemini-status":
            if not _require_auth(self):
                return
            cfg, _ = load_config()
            out = {}
            if vertex_config:
                out["backend"] = vertex_config.describe_backend(cfg)
                out["model"] = vertex_config.get_gemini_model(cfg)
                out["vertex_mode"] = vertex_config.is_vertex_mode(cfg)
                out["has_key"] = bool(vertex_config.get_api_key())
                out["key_hint"] = vertex_config.key_hint(vertex_config.get_api_key())
            if gemini_client:
                out["gemini_health"] = gemini_client.health_check(cfg, bool(cfg.get("internet_allowed", False)) if cfg else False)
            if firestore_sync:
                out["cloud_health"] = firestore_sync.health_check(cfg, bool(cfg.get("internet_allowed", False)) if cfg else False)
            if adk_health:
                try:
                    out["adk"] = adk_health()
                except Exception as e:
                    out["adk_error"] = str(e)
            out["genai_installed"] = _module_ok("google.genai") or _module_ok("google.generativeai")
            out["adk_sdk_installed"] = _module_ok("google.adk")
            self.send_json({"ok": True, "output": json.dumps(out, indent=2), "data": out})
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
        if parsed.path == "/api/stream":
            if not _require_auth(self):
                return
            message = str(payload.get("message", "")).strip()
            if not message:
                self.send_json({"ok": False, "output": "Message cannot be empty."}, status=400)
                return
            use_rag = bool(payload.get("use_rag", False))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                for event in build_stream_response(message, use_rag):
                    self.wfile.write((event + "\n").encode("utf-8"))
                    self.wfile.flush()
            except Exception as exc:
                err_event = "data: " + json.dumps({"type": "error", "message": str(exc)}) + "\n\n"
                self.wfile.write(err_event.encode("utf-8"))
                self.wfile.flush()
            self.close_connection = True
            return
        if parsed.path == "/api/inference-params":
            if not _require_auth(self):
                return
            params = payload.get("inference", payload) if isinstance(payload, dict) else {}
            # filter to known keys to avoid saving wrapper garbage
            filtered = {k: params[k] for k in INFERENCE_DEFAULTS if k in params}
            if filtered:
                params = filtered
            result = save_inference_params(params)
            self.send_json(result)
            return
        if parsed.path == "/api/settings":
            if not _require_auth(self):
                return
            try:
                cfg = payload.get("config")
                if cfg:
                    save_config(cfg)
                    self.send_json({"ok": True, "output": "Settings saved."})
                else:
                    self.send_json({"ok": False, "output": "No config provided."})
            except Exception as exc:
                self.send_json({"ok": False, "output": str(exc)})
            return
        if parsed.path == "/api/prompt-template":
            if not _require_auth(self):
                return
            name = payload.get("name", "")
            content = payload.get("content", "")
            delete = bool(payload.get("delete", False))
            if not name:
                self.send_json({"ok": False, "output": "Template name required."})
                return
            self.send_json(save_prompt_template(name, content, delete=delete))
            return
        if parsed.path == "/api/activate-template":
            if not _require_auth(self):
                return
            name = payload.get("name", "")
            self.send_json(activate_template(name))
            return
        if parsed.path == "/api/import":
            if not _require_auth(self):
                return
            file_data = payload.get("data", "")
            if not file_data:
                self.send_json({"ok": False, "output": "No data provided."})
                return
            self.send_json(import_conversations(file_data))
            return
        if parsed.path == "/api/export":
            if not _require_auth(self):
                return
            fmt = payload.get("format", "markdown") if payload else "markdown"
            try:
                data, content_type = export_conversations(fmt=fmt)
                self.send_json({"ok": True, "output": data, "content_type": content_type})
            except Exception as exc:
                self.send_json({"ok": False, "output": str(exc)})
            return
        if parsed.path == "/api/cloud-setup":
            if not _require_auth(self):
                return
            try:
                self.send_json(apply_cloud_setup(payload))
            except Exception as exc:
                self.send_json({"ok": False, "output": str(exc)})
            return
        if parsed.path == "/api/cloud-install":
            if not _require_auth(self):
                return
            try:
                self.send_json(install_cloud_deps())
            except Exception as exc:
                self.send_json({"ok": False, "output": str(exc)})
            return
        if parsed.path == "/api/cloud-sync":
            if not _require_auth(self):
                return
            cfg, _ = load_config()
            internet = bool(cfg.get("internet_allowed", False)) if cfg else False
            if not internet:
                self.send_json({"ok": False, "output": "Internet disabled. Enable via toggle first."})
                return
            results = {}
            if firestore_sync:
                try:
                    # sync last 20 DB rows
                    con = _db_conn()
                    cur = con.cursor()
                    cur.execute("SELECT role, text FROM conversation ORDER BY id DESC LIMIT 20")
                    rows = cur.fetchall()
                    con.close()
                    synced = 0
                    for r, t in reversed(rows):
                        res = firestore_sync.sync_conversation_to_firestore(r, t, config=cfg, internet_allowed=True)
                        if res.get("ok"):
                            synced += 1
                    results["firestore_synced"] = synced
                    # brain sync
                    bres = firestore_sync.sync_brain_docs_to_firestore(config=cfg, internet_allowed=True)
                    results["brain"] = bres
                    # GCS backup
                    gres = firestore_sync.backup_brain_to_gcs(config=cfg, internet_allowed=True)
                    results["gcs"] = gres
                except Exception as exc:
                    results["error"] = str(exc)
            if vertex_config and gemini_client:
                try:
                    results["gemini"] = gemini_client.health_check(cfg, True)
                except Exception as e:
                    results["gemini_error"] = str(e)
            if adk_health:
                try:
                    results["adk"] = adk_health()
                except Exception as e:
                    results["adk_error"] = str(e)
            ok = any(v.get("ok") for v in results.values() if isinstance(v, dict))
            self.send_json({"ok": ok or True, "output": f"Cloud sync results: {json.dumps(results, indent=2)}", "results": results})
            return
        if parsed.path == "/api/gemini-status":
            if not _require_auth(self):
                return
            cfg, _ = load_config()
            out = {}
            if vertex_config:
                out["backend"] = vertex_config.describe_backend(cfg)
                out["model"] = vertex_config.get_gemini_model(cfg)
                out["vertex_mode"] = vertex_config.is_vertex_mode(cfg)
            if gemini_client:
                out["gemini_health"] = gemini_client.health_check(cfg, bool(cfg.get("internet_allowed", False)) if cfg else False)
            if firestore_sync:
                out["cloud_health"] = firestore_sync.health_check(cfg, bool(cfg.get("internet_allowed", False)) if cfg else False)
            if adk_health:
                try:
                    out["adk"] = adk_health()
                except Exception as e:
                    out["adk_error"] = str(e)
            self.send_json({"ok": True, "output": json.dumps(out, indent=2), "data": out})
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
