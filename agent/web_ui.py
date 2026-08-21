#!/usr/bin/env python3
"""Local browser UI for the Pen AI assistant."""

import json
import os
import atexit
import socket
import subprocess
import sys
import threading
import time
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

CONFIG_PATH = BASE_DIR / "config.json"
VALIDATE_PATH = BASE_DIR / "validate_package.py"
START_AGENT_PATH = BASE_DIR / "start-agent.bat"
SYSTEM_PROMPT_PATH = BASE_DIR / "prompts" / "system.txt"
LLAMA_SERVER_PATH = BASE_DIR / "bin" / "llama-server.exe"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
LLAMA_HOST = "127.0.0.1"
LLAMA_PORT = 8766
LLAMA_SERVER_PROC = None
LLAMA_SERVER_LOG = None
LLAMA_SERVER_LOCK = threading.Lock()
CONVERSATION = []


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


atexit.register(stop_llama_server)


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pen AI</title>
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

    body {
      margin: 0;
      min-height: 100vh;
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

    .app {
      display: grid;
      grid-template-columns: 288px minmax(0, 1fr);
      min-height: 100vh;
    }

    aside {
      border-right: 1px solid var(--line);
      background: var(--surface);
      backdrop-filter: blur(22px);
      -webkit-backdrop-filter: blur(22px);
      padding: 22px;
    }

    main {
      display: grid;
      grid-template-rows: auto minmax(0, 1fr) auto;
      min-width: 0;
      min-height: 100vh;
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
      overflow: auto;
      padding: 24px 26px;
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

    form {
      border-top: 1px solid var(--line);
      padding: 16px 26px 22px;
      background: var(--surface-2);
      backdrop-filter: blur(18px);
      -webkit-backdrop-filter: blur(18px);
      display: grid;
      grid-template-columns: minmax(0, 1fr) 112px;
      gap: 12px;
      align-items: end;
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

    @media (max-width: 820px) {
      .app {
        grid-template-columns: 1fr;
      }

      aside {
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }

      main {
        min-height: 68vh;
      }

      form {
        grid-template-columns: 1fr;
      }

      #activity {
        white-space: normal;
      }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <div class="brand">
        <div class="brand-mark" aria-hidden="true">
          <svg width="26" height="26" viewBox="0 0 28 28" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M19.6 3.8l4.6 4.6-11.9 11.9-5.8 1.2 1.2-5.8L19.6 3.8z" stroke="#fff" stroke-width="1.9" stroke-linejoin="round"/>
            <path d="M16.8 6.6l4.6 4.6" stroke="#fff" stroke-width="1.9" stroke-opacity="0.85"/>
            <path d="M8.5 19.5l-3.2 3.2" stroke="#fff" stroke-width="1.9" stroke-linecap="round" stroke-opacity="0.85"/>
          </svg>
        </div>
        <div>
          <h1>Pen AI</h1>
          <p>⚡ Local &middot; Private &middot; Yours</p>
        </div>
      </div>

      <div class="section">
        <h2>Status</h2>
        <div class="status-row"><span>Config</span><span id="config" class="pill">-</span></div>
        <div class="status-row"><span>Model</span><span id="model" class="pill">-</span></div>
        <div class="status-row"><span>Runtime</span><span id="runtime" class="pill">-</span></div>
        <div class="status-row"><span>Model server</span><span id="server" class="pill">-</span></div>
        <div class="status-row"><span>Internet</span><span id="internet" class="pill">-</span></div>
        <div class="status-row"><span>Voice</span><span id="voice" class="pill">-</span></div>
      </div>

      <div class="section">
        <label class="toggle">
          <span>Local memory</span>
          <input id="rag" type="checkbox">
        </label>
      </div>

      <div class="section actions">
        <button id="warm" class="button" type="button">🔥 Warm Model</button>
        <button id="refresh" class="button" type="button">🔄 Refresh</button>
        <button id="validate" class="button" type="button">✅ Validate</button>
        <button id="cli" class="button" type="button">🖥️ Open CLI</button>
      </div>
    </aside>

    <main>
      <header>
        <div class="headline">
          <h2>Conversation</h2>
          <p id="subtitle">Private, local, and under your control.</p>
        </div>
        <div id="activity">Ready</div>
      </header>

      <section id="chat" aria-live="polite"></section>

      <form id="composer">
        <textarea id="message" placeholder="Ask Pen AI..." autocomplete="off"></textarea>
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
      messageInput.disabled = isBusy;
    }

    function addMessage(role, text) {
      const wrapper = document.createElement("article");
      wrapper.className = `message ${role}`;

      const name = document.createElement("div");
      name.className = "name";
      name.textContent = role === "user" ? "You" : role === "assistant" ? "Pen AI" : "System";

      const body = document.createElement("div");
      body.className = "text";
      body.textContent = text;

      wrapper.append(name, body);
      chat.appendChild(wrapper);
      chat.scrollTop = chat.scrollHeight;
    }

    function setPill(id, label, tone) {
      const el = document.querySelector(`#${id}`);
      el.textContent = label;
      el.className = `pill ${tone}`;
    }

    async function api(path, body = null) {
      const options = body ? {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body)
      } : {};
      const response = await fetch(path, options);
      if (!response.ok) {
        throw new Error(`Request failed: ${response.status}`);
      }
      return response.json();
    }

    async function refreshStatus() {
      try {
        const status = await api("/api/status");
        setPill("config", status.config_exists ? "OK" : "Missing", status.config_exists ? "ok" : "bad");
        setPill("model", status.model_exists ? "Ready" : "Missing", status.model_exists ? "ok" : "bad");
        setPill("runtime", status.llama_exists ? "Ready" : "Missing", status.llama_exists ? "ok" : "bad");
        setPill("server", status.model_server_running ? "Warm" : "Cold", status.model_server_running ? "ok" : "warn");
        setPill("internet", status.internet_allowed ? "On" : "Off", status.internet_allowed ? "warn" : "ok");
        setPill("voice", status.voice_enabled ? "On" : "Off", status.voice_enabled ? "ok" : "warn");
        document.querySelector("#subtitle").textContent = status.summary;
      } catch (error) {
        addMessage("system", error.message);
      }
    }

    async function sendMessage(event) {
      event.preventDefault();
      const text = messageInput.value.trim();
      if (!text) return;

      messageInput.value = "";
      addMessage("user", text);
      setBusy(true, "Loading local model...");
      try {
        const result = await api("/api/query", {
          message: text,
          use_rag: document.querySelector("#rag").checked
        });
        addMessage("assistant", result.output || "No response.");
      } catch (error) {
        addMessage("system", error.message);
      } finally {
        setBusy(false);
        refreshStatus();
      }
    }

    async function warmModel() {
      setBusy(true, "Warming model...");
      addMessage("system", "Starting the local model server. First load can take a while.");
      try {
        const result = await api("/api/warm-model", {});
        addMessage("system", result.output || "Model server is ready.");
      } catch (error) {
        addMessage("system", error.message);
      } finally {
        setBusy(false);
        refreshStatus();
      }
    }

    async function runValidation() {
      setBusy(true, "Validating...");
      addMessage("system", "Running package validation...");
      try {
        const result = await api("/api/validate", {});
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

    document.querySelector("#composer").addEventListener("submit", sendMessage);
    document.querySelector("#warm").addEventListener("click", warmModel);
    document.querySelector("#refresh").addEventListener("click", refreshStatus);
    document.querySelector("#validate").addEventListener("click", runValidation);
    document.querySelector("#cli").addEventListener("click", openCli);
    messageInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && event.ctrlKey) {
        sendMessage(event);
      }
    });

    addMessage("system", "Pen AI desktop is ready.");
    refreshStatus();
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


def resolve_agent_path(value):
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return BASE_DIR / path


def read_system_prompt():
    if SYSTEM_PROMPT_PATH.exists():
        try:
            return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return "You are Pen AI, a local offline assistant."


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


def start_llama_server(timeout=180):
    global LLAMA_SERVER_PROC, LLAMA_SERVER_LOG

    if llama_health():
        return {"ok": True, "output": "Model server is already warm."}

    status = runtime_status()
    if not status["model_exists"]:
        return {"ok": False, "output": "Model file is missing."}
    if not LLAMA_SERVER_PATH.exists():
        return {"ok": False, "output": "llama-server.exe is missing."}

    with LLAMA_SERVER_LOCK:
        if LLAMA_SERVER_PROC and LLAMA_SERVER_PROC.poll() is None:
            pass
        else:
            log_dir = BASE_DIR / "logs"
            log_dir.mkdir(exist_ok=True)
            LLAMA_SERVER_LOG = (log_dir / "web_ui_llama_server.log").open("a", encoding="utf-8")
            cmd = [
                str(LLAMA_SERVER_PATH),
                "-m",
                status["model_path"],
                "--host",
                LLAMA_HOST,
                "--port",
                str(LLAMA_PORT),
                "-ngl",
                "99",
                "--threads",
                "8",
                "-n",
                "192",
                "--ctx-size",
                "4096",
                "--no-webui",
                "--offline",
                "--log-disable",
            ]
            LLAMA_SERVER_PROC = subprocess.Popen(
                cmd,
                cwd=str(BASE_DIR),
                stdout=LLAMA_SERVER_LOG,
                stderr=subprocess.STDOUT,
                text=True,
            )

    deadline = time.time() + timeout
    while time.time() < deadline:
        if LLAMA_SERVER_PROC and LLAMA_SERVER_PROC.poll() is not None:
            return {"ok": False, "output": "Model server stopped during startup. See logs/web_ui_llama_server.log."}
        if llama_health():
            return {"ok": True, "output": "Model server is ready. Future replies should start faster."}
        time.sleep(1)

    return {"ok": False, "output": "Model server is still loading. Try again in a moment."}


def runtime_status():
    cfg, error = load_config()
    model_path = resolve_agent_path(cfg.get("model_path")) if cfg else None
    llama_path = resolve_agent_path(cfg.get("llama_bin")) if cfg else None
    model_exists = bool(model_path and model_path.exists())
    llama_exists = bool(llama_path and llama_path.exists())
    config_exists = CONFIG_PATH.exists()
    model_server_running = llama_health()

    if error:
        summary = error
    elif model_server_running:
        summary = "Model is warm and ready."
    elif model_exists and llama_exists:
        summary = "Ready for local inference. First reply may load the model."
    else:
        summary = "Setup incomplete."

    return {
        "config_exists": config_exists,
        "model_path": str(model_path) if model_path else None,
        "model_exists": model_exists,
        "llama_path": str(llama_path) if llama_path else None,
        "llama_exists": llama_exists,
        "model_server_running": model_server_running,
        "internet_allowed": bool(cfg and cfg.get("internet_allowed", False)),
        "voice_enabled": bool(cfg and cfg.get("voice_enabled", False)),
        "summary": summary,
    }


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
    history = [{"role": "system", "content": read_system_prompt()}]
    if context:
        history.append({"role": "system", "content": context})
    history += CONVERSATION[-12:]
    try:
        result = post_json(
            llama_server_url("/v1/chat/completions"),
            {
                "messages": history,
                "temperature": 0.7,
                "max_tokens": 192,
                "stream": False,
            },
            timeout=120,
        )
        output = result["choices"][0]["message"]["content"].strip()
    except Exception:
        prompt = (
            read_system_prompt()
            + "\n"
            + (context + "\n" if context else "")
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
                    "n_predict": 192,
                    "stream": False,
                },
                timeout=120,
            )
            output = str(result.get("content", "")).strip()
        except Exception as exc:
            return {"ok": False, "output": f"Model server request failed: {exc}"}

    if not output:
        output = "No response from the local model."
    CONVERSATION.append({"role": "assistant", "content": output})
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
    if use_rag:
        return query_with_local_memory(message)
    return query_warm_server(message)


def validate_package():
    return run_command([preferred_python(), str(VALIDATE_PATH)], timeout=60)


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
    server_version = "PenAiUi/1.0"

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self.send_html(HTML)
            return
        if parsed.path == "/api/status":
            self.send_json(runtime_status())
            return
        self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        payload = self.read_json()
        if parsed.path == "/api/query":
            message = str(payload.get("message", "")).strip()
            if not message:
                self.send_json({"ok": False, "output": "Message cannot be empty."}, status=400)
                return
            self.send_json(query_assistant(message, bool(payload.get("use_rag"))))
            return
        if parsed.path == "/api/validate":
            self.send_json(validate_package())
            return
        if parsed.path == "/api/warm-model":
            self.send_json(start_llama_server())
            return
        if parsed.path == "/api/open-cli":
            self.send_json(open_cli())
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
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload, status=200):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))


def find_port(start_port):
    for port in range(start_port, start_port + 20):
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
    args = parser.parse_args()

    port = find_port(args.port)
    server = ThreadingHTTPServer((args.host, port), Handler)
    url = f"http://{args.host}:{port}/"
    print(f"Pen AI UI running at {url}")
    print("Press Ctrl+C to stop.")

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
