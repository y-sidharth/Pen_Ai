# Jarvis Portable Local Assistant

Jarvis is a Windows-focused, USB-portable local AI assistant. It runs a local
GGUF model through a compatible llama.cpp executable, stores its data under the
`agent` folder, and can optionally provide a small AutoHotkey popup.

## Security model

Jarvis is local-first, not a hardened security boundary. It has no normal
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

Jarvis supports optional voice input/output for hands-free operation.

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

Jarvis can play music from your local library and control system volume.

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
- "Play [song name]" — Jarvis will search and play the song
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

## Commands

- `exit` — close the agent and clean temporary prompt files.
- `/help` — show the in-app help.
- `/run <safe diagnostic command>` — requests an approval token. Run
  `approve-action.bat <nonce>` from the USB drive and enter the approval PIN.
- `/enable_internet` and `/disable_internet` — session markers only. They do not
  change Windows firewall rules or grant the model a network client.
- `/voice` — Toggle voice input/output mode (requires voice setup).
- `/voice_status` — Show voice assistant status.

## Popup and autostart

`agent/jarvis_popup.ahk` requires AutoHotkey v1. It passes selected text to the
Python query script through a temporary UTF-8 file so text is not interpolated
into a command line.

### USB Autostart (Auto-run when USB is inserted)

Jarvis can automatically start when you insert your USB drive into any Windows
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
