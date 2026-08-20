#!/usr/bin/env python3
"""
Run-agent controller with internet toggle, pendrive-only temp storage and safe cleanup.
"""
import os
import sys
import json
import shlex
import subprocess
import sqlite3
import time
from getpass import getpass
from datetime import datetime

from command_policy import validate_command
from security import create_nonce, setup_credential, verify_verifier
from voice_assistant import VoiceAssistant, check_dependencies
from media_controller import MediaController, handle_media_command, parse_media_command
from task_executor import TaskExecutor, handle_task_command

# On Windows, the console's default codepage (e.g. cp1252/cp437) cannot encode
# the box-drawing/gradient characters used below, and the crash happens before
# the very first prompt is shown. Force UTF-8 output so the header/colors
# always print, regardless of the active console codepage.
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
PROMPTS_DIR = os.path.join(BASE_DIR, 'prompts')
SYSTEM_PROMPT_PATH = os.path.join(PROMPTS_DIR, 'system.txt')
DATA_DIR = os.path.join(BASE_DIR, 'data')
TMP_DIR = os.path.join(DATA_DIR, 'tmp')
AUTH_TOKEN_PATH = os.path.join(BASE_DIR, 'auth', 'allowlist.token')
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(TMP_DIR, exist_ok=True)

DEFAULT_CONFIG = {
    "model_path": "models\\13b.gguf",
    "llama_bin": "bin\\llama.exe",
    "llama_cmd_template": "{bin} -m {model} --cuda --threads 8 --temp 0.7 --repeat_penalty 1.1 --n_predict 512 --prompt-file {prompt_file}",
    "no_internet": True,
    "internet_allowed": False,
    "theme": "neon",
    # Voice settings
    "voice_enabled": False,
    "tts_enabled": True,
    "stt_enabled": True,
    "stt_engine": "whisper",
    "tts_engine": "sapi5",
    "tts_rate": 150,
    "tts_volume": 0.9,
    "tts_voice": None,
}

CSI = "\x1b["
RESET = CSI + "0m"

def rgb(r, g, b):
    return f"{CSI}38;2;{r};{g};{b}m"

def bold(text):
    return CSI + "1m" + text + RESET

def gradient_text(text, start_rgb=(255,0,128), end_rgb=(0,200,255)):
    out = []
    n = max(1, len(text))
    for i, ch in enumerate(text):
        t = i / (n - 1) if n > 1 else 0
        r = int(start_rgb[0] + (end_rgb[0] - start_rgb[0]) * t)
        g = int(start_rgb[1] + (end_rgb[1] - start_rgb[1]) * t)
        b = int(start_rgb[2] + (end_rgb[2] - start_rgb[2]) * t)
        out.append(f"{rgb(r,g,b)}{ch}")
    out.append(RESET)
    return ''.join(out)

def print_header(title='Jarvis - Local Assistant'):
    try:
        cols = os.get_terminal_size().columns
    except OSError:
        cols = 80
    bar = '═' * min(60, cols - 2)
    g1 = gradient_text(bar, (255,80,180), (90,200,255))
    print('\n' + g1)
    t = gradient_text(f'  {title}  ', (255,200,0), (255,0,200))
    print(bold(t))
    print(g1 + '\n')

# Config, prompts, DB

def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        print(f'Created default config at {CONFIG_PATH}. Please update model/bin paths if needed.')
        return DEFAULT_CONFIG
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        cfg = json.load(f)
    changed = False
    # Remove the legacy plaintext password rather than silently preserving it.
    if "password" in cfg:
        cfg.pop("password", None)
        changed = True
        print("Removed legacy plaintext password from config. A new password is required.")
    # ensure non-secret keys exist
    for k,v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k]=v
            changed = True
    if changed:
        save_config(cfg)
    return cfg


def save_config(cfg):
    temp_path = CONFIG_PATH + '.tmp'
    with open(temp_path, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)
    os.replace(temp_path, CONFIG_PATH)


def read_system_prompt():
    if os.path.exists(SYSTEM_PROMPT_PATH):
        with open(SYSTEM_PROMPT_PATH, 'r', encoding='utf-8') as f:
            return f.read().strip()
    return 'You are Jarvis, a local offline assistant. Always ask for explicit permission before executing system commands.'


def init_db():
    db_path = os.path.join(DATA_DIR, 'sqlite.db')
    conn = sqlite3.connect(db_path, timeout=30)
    # durability pragmas
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=FULL;")
    except Exception:
        pass
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS conversation (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT,
        text TEXT,
        ts TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cmd TEXT,
        nonce TEXT,
        status TEXT,
        ts_request TEXT,
        ts_complete TEXT
    )''')
    conn.commit()
    return conn


def append_db(conn, role, text):
    ts = datetime.utcnow().isoformat()
    c = conn.cursor()
    c.execute('INSERT INTO conversation (role, text, ts) VALUES (?,?,?)', (role, text, ts))
    conn.commit()


def log_action_request(conn, cmd, nonce):
    ts = datetime.utcnow().isoformat()
    c = conn.cursor()
    c.execute('INSERT INTO actions (cmd, nonce, status, ts_request) VALUES (?,?,?,?)', (cmd, nonce, 'requested', ts))
    conn.commit()


def update_action_status(conn, nonce, status):
    ts = datetime.utcnow().isoformat()
    c = conn.cursor()
    if status == 'completed':
        c.execute('UPDATE actions SET status=?, ts_complete=? WHERE nonce=?', (status, ts, nonce))
    else:
        c.execute('UPDATE actions SET status=? WHERE nonce=?', (status, nonce))
    conn.commit()

# Approval & internet helpers

def wait_for_approval(nonce, timeout=300, poll_interval=2):
    """
    Wait for signed token file at AUTH_TOKEN_PATH. Token format: nonce|timestamp|hex_signature
    Signature HMAC-SHA256 over "nonce|timestamp" using secret stored at auth/secret.key (base64).
    """
    start = time.time()
    print(bold(rgb(180,255,200) + f'Waiting for signed approval. Run: approve-action.bat {nonce} on this pendrive to approve.'))
    audit_path = os.path.join(LOG_DIR, f'audit_action_{nonce}.log')
    with open(audit_path, 'a', encoding='utf-8') as audit:
        audit.write(f'Action {nonce} requested at {datetime.utcnow().isoformat()}\n')
        while time.time() - start < timeout:
            if os.path.exists(AUTH_TOKEN_PATH):
                try:
                    with open(AUTH_TOKEN_PATH, 'r', encoding='utf-8') as f:
                        content = f.read().strip()
                    parts = content.split('|')
                    if len(parts) != 3:
                        audit.write(f'Invalid token format found at {datetime.utcnow().isoformat()}\n')
                        time.sleep(poll_interval)
                        continue
                    token_nonce, ts_str, sig_hex = parts
                    if token_nonce != nonce:
                        audit.write(f'Nonce mismatch in token at {datetime.utcnow().isoformat()}\n')
                        time.sleep(poll_interval)
                        continue
                    try:
                        ts = int(ts_str)
                    except Exception:
                        audit.write(f'Invalid timestamp in token at {datetime.utcnow().isoformat()}\n')
                        time.sleep(poll_interval)
                        continue
                    # check timestamp freshness (allow small drift)
                    if abs(time.time() - ts) > 600:
                        audit.write(f'Token timestamp out of allowed window at {datetime.utcnow().isoformat()}\n')
                        time.sleep(poll_interval)
                        continue
                    # load secret
                    try:
                        if os.path.exists(os.path.join(os.path.dirname(AUTH_TOKEN_PATH), 'secret.key')):
                            import base64
                            with open(os.path.join(os.path.dirname(AUTH_TOKEN_PATH), 'secret.key'), 'rb') as secret_file:
                                sk = base64.b64decode(secret_file.read(), validate=True)
                            if len(sk) < 32:
                                raise ValueError('approval secret is too short')
                        else:
                            audit.write(f'Secret not found for token verification at {datetime.utcnow().isoformat()}\n')
                            time.sleep(poll_interval)
                            continue
                    except Exception:
                        time.sleep(poll_interval)
                        continue
                    import hmac, hashlib
                    payload = f"{token_nonce}|{ts}".encode('utf-8')
                    expected = hmac.new(sk, payload, hashlib.sha256).hexdigest()
                    if not hmac.compare_digest(expected, sig_hex):
                        audit.write(f'Signature mismatch for token at {datetime.utcnow().isoformat()}\n')
                        time.sleep(poll_interval)
                        continue
                    # Consume the one-time token before returning. This prevents
                    # replay if a previous approval file is left on the drive.
                    try:
                        os.remove(AUTH_TOKEN_PATH)
                    except FileNotFoundError:
                        audit.write(f'Approval token disappeared before consumption at {datetime.utcnow().isoformat()}\n')
                        time.sleep(poll_interval)
                        continue
                    audit.write(f'Action {nonce} approved and consumed at {datetime.utcnow().isoformat()}\n')
                    return True
                except Exception as e:
                    audit.write(f'Error while reading token: {e} at {datetime.utcnow().isoformat()}\n')
            time.sleep(poll_interval)
        audit.write(f'Action {nonce} timed out at {datetime.utcnow().isoformat()}\n')
    return False


def build_media_config(cfg):
    """Build MediaController config from the app config.

    Only forward 'music_dirs' when the user actually configured directories;
    passing an empty list would shadow MediaController's own sensible
    defaults (~/Music, ~/Downloads/Music, etc.) because dict.get() returns
    whatever key is present, even if it's an empty list.
    """
    media_config = {'media_player': cfg.get('media_player', 'vlc')}
    configured_dirs = cfg.get('music_dirs')
    if configured_dirs:
        media_config['music_dirs'] = configured_dirs
    return media_config


def safe_cleanup_tmp():
    try:
        for name in os.listdir(TMP_DIR):
            path = os.path.join(TMP_DIR, name)
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except Exception:
                pass
    except Exception:
        pass


def _pid_is_running(pid):
    """Check whether a PID is a live process, using only the stdlib (no psutil dependency)."""
    if os.name != 'nt':
        return False
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def detect_unclean_shutdown():
    # if there are temp files or a missing clean exit marker, consider unclean
    try:
        lock = os.path.join(TMP_DIR, 'session.lock')
        if os.path.exists(lock):
            # read pid
            try:
                pid = int(open(lock).read().strip())
                # check if pid is running
                if pid and _pid_is_running(pid):
                    return False
            except Exception:
                pass
            return True
        # also if any tmp files present, treat as potential unclean
        if os.listdir(TMP_DIR):
            return True
    except Exception:
        pass
    return False

# Main loop

def main():
    cfg = load_config()
    print_header()
    if not cfg.get('password_verifier'):
        cfg['password_verifier'] = setup_credential('agent password')
        save_config(cfg)
        print('Password setup complete.')
    # Password gate. The password is never stored in plaintext.
    # Allow non-interactive startup when JARVIS_AUTO_PASSWORD is set in the environment
    pw_env = os.environ.get('JARVIS_AUTO_PASSWORD')
    if pw_env is not None:
        pw = pw_env
        print(rgb(180,255,200) + 'Using JARVIS_AUTO_PASSWORD from environment.' + RESET)
    else:
        pw = getpass(rgb(200,200,255) + 'Enter agent password: ' + RESET)
    if not verify_verifier(pw, cfg.get('password_verifier')):
        print(rgb(255,120,120) + 'Incorrect password. Exiting.' + RESET)
        sys.exit(1)
    print(rgb(120,255,180) + 'Password accepted. Starting agent (local-only mode).' + RESET)
    print('Note: this program does not enforce an operating-system firewall; do not treat a USB drive as a secure trust boundary.')

    system_prompt = read_system_prompt()
    conn = init_db()

    # detect unclean shutdown and handle
    if detect_unclean_shutdown():
        print(rgb(255,200,150) + 'Detected previous unclean shutdown or leftover temp files. Cleaning temp files to protect host integrity.' + RESET)
        safe_cleanup_tmp()

    llama_bin = os.path.join(BASE_DIR, cfg.get('llama_bin')) if not os.path.isabs(cfg.get('llama_bin')) else cfg.get('llama_bin')
    model_path = os.path.join(BASE_DIR, cfg.get('model_path')) if not os.path.isabs(cfg.get('model_path')) else cfg.get('model_path')
    cmd_template = cfg.get('llama_cmd_template')

    if not os.path.exists(llama_bin):
        print(rgb(255,200,150) + f'Warning: llama binary not found at {llama_bin}. Inference will not run until you place a compatible binary there.' + RESET)
    if not os.path.exists(model_path):
        print(rgb(255,200,150) + f'Warning: model file not found at {model_path}. Place your quantized GGUF model at that path relative to the agent folder.' + RESET)

    history = []
    session_lock = os.path.join(TMP_DIR, 'session.lock')
    try:
        open(session_lock, 'w').write(str(os.getpid()))
    except Exception:
        pass

    internet_allowed = bool(cfg.get('internet_allowed', False))

    # Initialize voice assistant if enabled
    voice = None
    voice_enabled = cfg.get('voice_enabled', False)
    if voice_enabled:
        deps = check_dependencies()
        if deps.get('speech_recognition') and deps.get('pyttsx3'):
            voice_config = {
                'tts_enabled': cfg.get('tts_enabled', True),
                'stt_enabled': cfg.get('stt_enabled', True),
                'stt_engine': cfg.get('stt_engine', 'whisper'),
                'tts_engine': cfg.get('tts_engine', 'sapi5'),
                'tts_rate': cfg.get('tts_rate', 150),
                'tts_volume': cfg.get('tts_volume', 0.9),
                'tts_voice': cfg.get('tts_voice'),
                'internet_allowed': internet_allowed,
            }
            voice = VoiceAssistant(voice_config)
            if voice.initialize():
                print(rgb(180,220,255) + 'Voice assistant initialized. Use /voice to toggle voice mode.' + RESET)
            else:
                print(rgb(255,200,150) + 'Voice assistant failed to initialize. Falling back to text-only.' + RESET)
                voice = None
        else:
            print(rgb(255,200,150) + 'Voice dependencies not installed. Run: pip install SpeechRecognition pyttsx3' + RESET)
            print('Voice features disabled. Use /voice_install for instructions.' + RESET)

    # Voice mode state
    voice_mode = False

    # Initialize media controller
    media = MediaController(build_media_config(cfg))

    # Initialize task executor for system actions
    task_executor = TaskExecutor({
        'default_search': cfg.get('default_search', 'google'),
    })

    try:
        print('\n' + bold(rgb(200,200,255) + 'Type messages. Commands: /run <cmd> (privileged), /help, /voice, /play, /volume, /enable_internet, /disable_internet, exit' + RESET) + '\n')
        while True:
            try:
                # Voice input mode
                if voice_mode and voice and voice.config.get('stt_enabled'):
                    print(rgb(200,220,255) + 'Listening... (speak now)' + RESET, end=' ', flush=True)
                    user = voice.listen(timeout=10)
                    if user:
                        print(f'\r{rgb(200,220,255)}You: {user}{RESET}  ')
                    else:
                        print('\rNo speech detected. Try again or type normally.')
                        continue
                else:
                    user = input(rgb(200,220,255) + 'You: ' + RESET).strip()
            except (EOFError, KeyboardInterrupt):
                print('\n' + rgb(220,220,220) + 'Exiting agent.' + RESET)
                break
            if not user:
                continue
            if user.lower() in ('exit', 'quit'):
                if voice_mode and voice:
                    voice.speak('Goodbye.')
                print(rgb(255,200,200) + 'Goodbye.' + RESET)
                break
            if user.lower() in ('/help', 'help'):
                print(bold(rgb(180,220,255) + '\nAvailable commands:\n' + RESET))
                print(rgb(200,255,200) + '=== Media Commands ===')
                print('/play <song>      - Play a song from your music library')
                print('/volume up/down   - Adjust system volume')
                print('/volume <0-100>   - Set volume level')
                print('/songs            - List available songs')
                print('/stop             - Stop playback')
                print()
                print('=== Voice Commands ===')
                print('/voice            - Toggle voice input/output mode')
                print('/voice_status     - Show voice assistant status')
                print()
                print('=== System Commands ===')
                print('/run <cmd>        - Request to run a privileged system command (requires approval)')
                print('/enable_internet  - Temporarily allow internet access (requires password)')
                print('/disable_internet - Disable internet access')
                print('/help             - Show this help')
                print('exit              - Quit agent' + RESET + '\n')
                continue

            # Voice commands
            if user.lower() == '/voice':
                if not voice:
                    print(rgb(255,200,150) + 'Voice assistant not available.' + RESET)
                    continue
                voice_mode = not voice_mode
                if voice_mode:
                    print(rgb(180,255,200) + 'Voice mode ENABLED. Speak your messages.' + RESET)
                    if voice.config.get('tts_enabled'):
                        voice.speak('Voice mode enabled')
                else:
                    print(rgb(255,220,200) + 'Voice mode DISABLED. Switching to text input.' + RESET)
                continue

            if user.lower() == '/voice_status':
                if voice:
                    status = voice.get_status()
                    print(bold(rgb(180,220,255) + '\nVoice Assistant Status:' + RESET))
                    print(f"  Initialized: {status['initialized']}")
                    print(f"  TTS Available: {status['tts_available']}")
                    print(f"  STT Available: {status['stt_available']}")
                    print(f"  Voice Mode: {voice_mode}")
                    print(f"  TTS Engine: {voice.config.get('tts_engine')}")
                    print(f"  STT Engine: {voice.config.get('stt_engine')}")
                else:
                    print(rgb(255,200,150) + 'Voice assistant not available.' + RESET)
                continue

            if user.lower() == '/voice_install':
                from voice_assistant import install_dependencies
                install_dependencies()
                continue

            # Media commands - handle before conversation
            user_lower = user.lower()
            if user_lower.startswith('/play ') or user_lower.startswith('play '):
                query = user.split(' ', 1)[1] if ' ' in user else ''
                if user.startswith('/'):
                    query = query  # Already stripped
                else:
                    query = user  # Full input for natural speech
                response, success = handle_media_command(media, f"play {query}")
                print(rgb(200,255,200) + response + RESET)
                if voice_mode and voice and voice.config.get('tts_enabled'):
                    voice.speak(response)
                continue

            if user_lower.startswith('/volume ') or user_lower in ('/volume up', '/volume down', '/volume stop'):
                response, success = handle_media_command(media, user.lstrip('/'))
                print(rgb(200,255,200) + response + RESET)
                if voice_mode and voice and voice.config.get('tts_enabled'):
                    voice.speak(response)
                continue

            if user_lower == '/songs' or user_lower == '/list_songs':
                songs = media.get_song_list(10)
                if songs:
                    response = f"Available songs: {', '.join(songs[:5])}{'...' if len(songs) > 5 else ''}"
                else:
                    response = "No songs found in your music library."
                print(rgb(200,255,200) + response + RESET)
                if voice_mode and voice and voice.config.get('tts_enabled'):
                    voice.speak(response)
                continue

            if user.lower() == '/enable_internet':
                # require password confirmation
                pw2 = getpass('Confirm password to enable internet: ')
                if not verify_verifier(pw2, cfg.get('password_verifier')):
                    print(rgb(255,120,120) + 'Incorrect password. Internet not enabled.' + RESET)
                    continue
                internet_allowed = True
                print(rgb(200,255,180) + 'Internet access enabled for this session. Use /disable_internet to turn it off.' + RESET)
                continue
            if user.lower() == '/disable_internet':
                internet_allowed = False
                print(rgb(255,220,200) + 'Internet access disabled.' + RESET)
                continue

            if user.startswith('/run '):
                cmd = user[len('/run '):].strip()
                argv, rejection = validate_command(cmd)
                if rejection:
                    print(rgb(255,180,180) + 'Command rejected: ' + rejection + RESET)
                    continue
                nonce = create_nonce()
                print(bold(gradient_text(' Privileged Action Requested ', (255,100,180), (120,240,255))))
                print(rgb(220,220,255) + f'Command: {argv}' + RESET)
                print(rgb(220,255,220) + f'Approval nonce: {nonce}' + RESET)
                print(rgb(200,255,200) + f'To approve, run (on this pendrive): approve-action.bat {nonce}' + RESET)

                append_db(conn, 'system', f'PRIV_REQUEST: {cmd} (nonce={nonce})')
                log_action_request(conn, cmd, nonce)

                approved = wait_for_approval(nonce, timeout=300)
                if not approved:
                    print(rgb(255,180,180) + 'Action not approved or timed out.' + RESET)
                    update_action_status(conn, nonce, 'timed_out')
                    continue

                print(rgb(180,255,200) + 'Approved. Executing command...' + RESET)
                update_action_status(conn, nonce, 'approved')
                try:
                    proc = subprocess.run(argv, shell=False, cwd=BASE_DIR, capture_output=True, text=True, timeout=60)
                    out = proc.stdout.strip() or proc.stderr.strip()
                    print('\n' + bold(rgb(200,255,200) + 'Command output:' + RESET))
                    print(out + '\n')
                    append_db(conn, 'assistant', f'EXECUTED: {cmd}\nOUTPUT:\n{out}')
                    update_action_status(conn, nonce, 'completed')
                except Exception as e:
                    print(rgb(255,150,150) + f'Error executing command: {e}' + RESET)
                    append_db(conn, 'assistant', f'EXEC_ERROR: {cmd} -> {e}')
                    update_action_status(conn, nonce, 'error')
                continue

            # Task executor - handle system actions before conversation
            # This catches commands like "play X", "open X", "search for X", etc.
            task_response, task_success, needs_approval = handle_task_command(task_executor, user)
            if task_response and not user.startswith('/'):  # Only for natural language, not slash commands
                task_parsed = task_executor.parse_task(user)
                if task_parsed.get('action') != 'unknown':
                    print(rgb(200,255,200) + task_response + RESET)
                    if voice_mode and voice and voice.config.get('tts_enabled'):
                        voice.speak(task_response)
                    if needs_approval:
                        print(rgb(255,200,150) + 'This action requires approval. Use /run command with approval.' + RESET)
                    append_db(conn, 'assistant', task_response)
                    continue

            # conversational flow
            append_db(conn, 'user', user)

            prompt_parts = [system_prompt, '\n']
            slice_history = []
            # include last up to 6 turns by reading recent from DB (lightweight)
            try:
                c = conn.cursor()
                c.execute('SELECT role, text FROM conversation ORDER BY id DESC LIMIT 12')
                rows = c.fetchall()[::-1]
                for r in rows:
                    slice_history.append((r[0], r[1]))
            except Exception:
                pass
            for role, text in slice_history:
                if role == 'user':
                    prompt_parts.append('User: ' + text + '\n')
                else:
                    prompt_parts.append('Assistant: ' + text + '\n')
            prompt_parts.append('Assistant:')
            full_prompt = '\n'.join(prompt_parts)

            # write prompt to pendrive-only temp file
            prompt_file = os.path.join(TMP_DIR, f'prompt_{int(time.time()*1000)}.txt')
            with open(prompt_file, 'w', encoding='utf-8') as pf:
                pf.write(full_prompt)

            if os.path.exists(llama_bin) and os.path.exists(model_path):
                # if internet is not allowed, ensure model command won't attempt network
                if not internet_allowed and cfg.get('no_internet'):
                    # run inference normally offline
                    pass
                cmd_str = cmd_template.format(bin=shlex.quote(llama_bin), model=shlex.quote(model_path), prompt_file=shlex.quote(prompt_file))
                cmd = shlex.split(cmd_str)
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                    output = proc.stdout.strip() or proc.stderr.strip()
                    print('\n' + bold(rgb(200,240,255) + 'Assistant:') + '\n')
                    print(gradient_text(output[:200], (180,255,200), (200,200,255)))
                    if len(output) > 200:
                        print(output[200:])
                    print('\n')
                    append_db(conn, 'assistant', output)
                    # Speak response in voice mode
                    if voice_mode and voice and voice.config.get('tts_enabled'):
                        # Speak a shortened version if too long
                        speak_text = output[:300] + '...' if len(output) > 300 else output
                        voice.speak(speak_text)
                except Exception as e:
                    print(rgb(255,150,150) + f'Error running inference binary: {e}' + RESET)
                    error_msg = 'Assistant: (inference failed — see logs)'
                    print(error_msg)
                    append_db(conn, 'assistant', '(inference failed)')
                    if voice_mode and voice and voice.config.get('tts_enabled'):
                        voice.speak('Sorry, I encountered an error.')
            else:
                fallback = "(No local model/binary found. Place model at '{}' and binary at '{}' and update config.json.)".format(model_path, llama_bin)
                print('\n' + bold(rgb(255,220,200) + 'Assistant:') + '\n')
                print(fallback + '\n')
                append_db(conn, 'assistant', fallback)
                if voice_mode and voice and voice.config.get('tts_enabled'):
                    voice.speak(fallback)

            # cleanup prompt file immediately
            try:
                if os.path.exists(prompt_file):
                    os.remove(prompt_file)
            except Exception:
                pass

    finally:
        try:
            if os.path.exists(session_lock):
                os.remove(session_lock)
        except Exception:
            pass
        safe_cleanup_tmp()
        # save whether internet was allowed this session? Do not persist by default
        conn.close()

if __name__ == '__main__':
    main()
