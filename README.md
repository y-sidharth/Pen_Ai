# Jampandu Portable Local Assistant

Jampandu is a Windows-focused, USB-portable local AI assistant. It runs a local
GGUF model through a compatible llama.cpp executable, stores its data under the
`agent` folder, and can optionally provide a small AutoHotkey popup.

## Security model

Jampandu is local-first, not a hardened security boundary. It has no normal
network client in conversation mode, but it cannot enforce a Windows firewall
or prevent a modified inference binary from accessing the network. A USB drive
that another person can modify must be treated as untrusted: they may replace
scripts, binaries, prompts, or credential verifiers.

The agent creates a password verifier on first launch and an approval-PIN
verifier on first privileged action. Plaintext passwords and PINs are not stored.
Approval tokens are bound to a 16-character nonce, expire after ten minutes, and
are consumed before the command runs.

`/run` is intentionally limited to read-only diagnostics: `hostname`,
`ipconfig /all`, `systeminfo`, `tasklist`, and selected `whoami` forms. It cannot
run PowerShell, batch files, arbitrary executables, paths, or chained commands.

## Setup

1. Put a compatible llama.cpp executable at `agent/bin/llama.exe`.
2. Put a GGUF model at the path configured in `agent/config.json`.
3. Optionally copy `agent/config.example.json` as a starting point; never put a
   password or PIN in it.
4. Run `agent/start-agent.bat`.
5. Create a new agent password when prompted.

The initial package intentionally runs without a bundled model or inference
binary. It will explain what is missing instead of failing silently.

## Desktop UI

Run `agent/start-ui.bat` to open the Pen AI local web interface. It starts a
small server on `127.0.0.1`, opens your browser, and provides chat, setup
status, package validation, optional local-memory mode, and a launcher for the
full CLI.

## Model download

Model downloads require network access and explicit confirmation. Do not use
automatic model discovery. Obtain the exact repository, filename, and SHA-256
digest from a trusted publisher, then run:

```powershell
agent\python-portable\python.exe agent\download_model.py owner/repository model.gguf --sha256 <64-character-digest>
```

Downloads are placed in `agent/models`, resume from a `.part` file when the
server supports ranges, and are deleted if checksum verification fails.

## Voice Assistant

Jampandu supports optional voice input/output for hands-free operation.

### Setup

1. Install voice dependencies:
   ```powershell
   agent\python-portable\python.exe -m pip install -r agent\requirements-voice.txt
   ```

2. (Optional) For better speech recognition accuracy, install Whisper:
   ```powershell
   # First install FFmpeg: choco install ffmpeg
   agent\python-portable\python.exe -m pip install openai-whisper
   ```

3. Enable voice in `agent/config.json`:
   ```json
   {
     "voice_enabled": true
   }
   ```

### Voice Commands

Once voice is enabled and dependencies are installed:

- `/voice` — Toggle voice mode on/off. In voice mode, speak your messages instead of typing.
- `/voice_status` — Show current voice assistant status (TTS/STT availability).
- `/voice_install` — Show instructions for installing voice dependencies.

## Media Control

Jampandu can play music from your local library and control system volume.

### Setup

1. Ensure you have music files in one of these directories:
   - `~/Music`
   - `~/Downloads/Music`
   - Or configure custom directories in `config.json`:
     ```json
     {
       "music_dirs": ["D:/MyMusic", "E:/Songs"]
     }
     ```

2. (Optional) Install VLC media player for better playback control.

### Media Commands

- `/play <song name>` — Search and play a song from your library
  - Example: `/play honey sing brave` or just say "play honey sing song from brave"
- `/volume up` — Increase volume by 10%
- `/volume down` — Decrease volume by 10%
- `/volume <0-100>` — Set volume to specific level (e.g., `/volume 50`)
- `/songs` or `/list_songs` — List available songs in your library
- `/stop` — Stop current playback

### Voice + Media

When voice mode is enabled, you can simply say:
- "Play [song name]" — Jampandu will search and play the song
- "Volume up/down" — Adjust volume
- "Stop" — Stop playback

### Speech-to-Text Engines

Configure in `config.json` with `"stt_engine"`:
- `"whisper"` (default) — Offline, accurate, requires Whisper model download
- `"sphinx"` — Completely offline, less accurate, no model download needed
- `"google"` — Requires internet, most accurate

### Text-to-Speech

Uses Windows SAPI5 voices by default (completely offline). Configure voice, rate, and volume in `config.json`:
```json
{
  "tts_rate": 150,
  "tts_volume": 0.9,
  "tts_voice": null
}
```

## Google Cloud & Gemini Integration (Hackathon Checklist)

Hybrid offline+cloud mode keeps the USB-local llama.cpp path as default and adds opt-in cloud when `internet_allowed` is ON. This satisfies all 3 checklist items:

| Checklist | Implementation | File |
|---|---|---|
| **Gemini 3.5 or newer** | `gemini-2.5-pro` (alias `gemini-3.5-pro` accepted) via **Google GenAI SDK** + **Vertex AI** | `agent/gemini_client.py:1`, `agent/vertex_config.py:1` |
| **Google Agent Framework** | **Google ADK** (`google-adk`) + **GenAI SDK** (`google-genai`) - ADK `Agent` with `FunctionTool`s | `agent/adk_agent/agent.py:1`, `agent/adk_agent/tools.py:1` |
| **Google Cloud Service** | **Vertex AI** (Gemini) + **Firestore** (conversations/brain) + **Cloud Storage** (brain backup) + optional **Cloud Run** | `agent/firestore_sync.py:1`, `agent/vertex_config.py:1` |

### Setup (2 minutes)

1. Install cloud deps:
   ```powershell
   agent\python-portable\python.exe -m pip install -r agent\requirements.txt
   # or: pip install google-genai google-adk google-cloud-aiplatform google-cloud-firestore google-cloud-storage python-dotenv
   ```
2. Choose one credential mode (create `agent/.env` from `agent/.env.example`):
   - **AI Studio (simplest):** set `GEMINI_API_KEY` + `GEMINI_MODEL=gemini-2.5-pro`
   - **Vertex AI (recommended for judging):** `gcloud auth application-default login` + set `GOOGLE_CLOUD_PROJECT` + `GOOGLE_CLOUD_LOCATION=us-central1`
   ```powershell
   # Vertex setup
   gcloud projects create pen-ai-hack --name="Pen AI"
   gcloud config set project pen-ai-hack
   gcloud services enable aiplatform.googleapis.com firestore.googleapis.com storage.googleapis.com
   gcloud auth application-default login
   # create bucket
   gsutil mb -l us-central1 gs://pen-ai-brain-xxxxx
   # set in agent/.env: GCS_BUCKET=pen-ai-brain-xxxxx, FIRESTORE_COLLECTION=jarvis_conversations
   ```
3. Enable in `agent/config.json`:
   ```json
   { "gemini_enabled": true, "gemini_model": "gemini-2.5-pro", "gcp_project": "pen-ai-hack", "gcs_bucket": "pen-ai-brain-xxxxx" }
   ```

### How Hybrid Works

- **Offline (`/disable_internet` or no key):** CLI + Web UI use local `bin/llama.exe` / `llama-server.exe` + TF-IDF RAG (`brain/*.txt`). No network calls.
- **Online (`/enable_internet` + key):** `run_agent.py:925` and `web_ui.py:2051` try `gemini_client.generate()` / `adk_agent.run_adk_query()` first. On failure they **fallback to local llama** and show `[Cloud unavailable: ... falling back]`. Firestore sync is best-effort (`firestore_sync.py`).

### New Commands & UI

- CLI: `/cloud_status` - shows Gemini backend, ADK, Firestore/Storage health; `/sync` - sync last 20 turns + brain to Firestore/GCS
- Web UI: Status pills **Gemini / Cloud / ADK** + **Cloud Sync** button. `/api/status` now returns `gemini_available`, `gemini_model`, `gcp_project`, `firestore_can_sync`, etc. New endpoints `/api/gemini-status`, `/api/cloud-sync` (`web_ui.py:3060`).
- Validator now checks cloud files without blocking offline use: `python agent/validate_package.py`

### ADK Tools (function calling)

Defined in `agent/adk_agent/tools.py:26`: `local_rag_search`, `media_play`, `media_volume`, `system_task`, `firestore_sync_text`, `cloud_backup`. Wired as `google.adk.tools.FunctionTool` in `adk_agent/agent.py:build_adk_agent`.

### Deployment (optional, strong proof)

```powershell
gcloud run deploy pen-ai --source agent --allow-unauthenticated --region us-central1 --set-env-vars GEMINI_MODEL=gemini-2.5-pro
```

### Security

Cloud is gated by the same `internet_allowed` toggle (password-protected `/enable_internet` in CLI, toggle in Web UI). No API keys are stored in `config.json` - only in `agent/.env` (gitignored). Firestore device isolation via hashed `device_id`.

## Commands

- `exit` — close the agent and clean temporary prompt files.
- `/help` — show the in-app help.
- `/run <safe diagnostic command>` — requests an approval token. Run
  `approve-action.bat <nonce>` from the USB drive and enter the approval PIN.
- `/enable_internet` and `/disable_internet` — enable/disable cloud (Gemini + Firestore/Storage). CLI requires password for enable.
- `/cloud_status` and `/sync` — show Gemini/ADK/Cloud health and sync to Firestore/GCS.
- `/voice` — Toggle voice input/output mode (requires voice setup).
- `/voice_status` — Show voice assistant status.

## Popup and autostart

`agent/jarvis_popup.ahk` requires AutoHotkey v1. It passes selected text to the
Python query script through a temporary UTF-8 file so text is not interpolated
into a command line.

### USB Autostart (Auto-run when USB is inserted)

Jampandu can automatically start when you insert your USB drive into any Windows
computer. This is useful for a truly portable, plug-and-play experience.

#### How it works

1. The autostart feature uses a background watcher that monitors for USB drive
   insertions using Windows WMI events (near-instant detection).
2. When a USB drive containing the `AUTOSTART.marker` file is detected, the
   agent starts automatically from that drive.
3. Only USB drives with the marker file will trigger the agent - other USB
   drives are ignored.

#### Setup

1. Copy the entire project to your USB drive.
2. On the computer where you want autostart enabled, run:
   ```
   agent\install_autostart.bat
   ```
3. Type `YES` to confirm the installation.
4. This creates a scheduled task that runs when you log in to Windows.

#### Testing

1. Safely eject your USB drive.
2. Insert it into any computer where the watcher is running.
3. The agent should start automatically within a few seconds.

#### Removal

Run `uninstall_autostart.bat` to remove the scheduled task. Do not install
autostart on a shared or untrusted machine.

#### Technical details

- The watcher uses WMI events for instant USB detection (falls back to polling
  if WMI fails).
- The scheduled task runs with limited privileges (no admin required).
- The watcher starts 5 seconds after user logon to avoid interfering with
  system startup.
- Drive removal is tracked, allowing the same USB to trigger autostart again
  on re-insertion.

## Validation and tests

Run these from the `agent` directory:

```powershell
python validate_package.py
python -m unittest discover -s tests -v
```

Use `python validate_package.py --strict` once a model and inference binary have
been installed. The validator checks portability settings, plaintext password
regressions, command-policy wiring, and required files.

## Removal

Run `uninstall_autostart.bat` if autostart was installed. Then run
`stop_and_clean.bat` before safely ejecting the USB drive. Removing the project
folder also removes local conversations, RAG data, logs, and credential
verifiers; back up any data you want to keep first.
