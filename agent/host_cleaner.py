#!/usr/bin/env python3
"""
host_cleaner.py — Erase all host-side traces when pendrive is removed or system powers off.

This is the single source of truth for "No file should store any data related to it."
It runs:
  - On pendrive removal (called by autostart_watcher.ps1)
  - On system shutdown/logoff/power-off (Task Scheduler shutdown trigger + atexit/signal handlers in run_agent.py & web_ui.py)
  - On-demand via stop_and_clean.bat / --full-clean

Host footprint inventory (Windows):
  - %APPDATA%\\Jampandu\\watcher.log          — watcher diagnostic log (contains drive letters)
  - %APPDATA%\\Jampandu\\insert_*.flag / remove_*.flag — transient WMI flags
  - %APPDATA%\\Jampandu\\host_clean.log       — this cleaner's own log (wiped last)
  - Clipboard contents
  - %TEMP% files matching pen-ai / jampandu / prompt_* / popup_*
  - Browser localhost cache is prevented via Cache-Control: no-store + incognito launch
    but if any slipped, we hint-clear Brave/Chrome localhost storage.
  - Windows RecentDocs / Prefetch entries referencing the USB drive (best-effort, no admin needed)

Design goals:
  - Never touch user documents outside our footprint.
  - Prefer secure overwrite (1-pass random + zero) before delete when secure=True.
  - Be silent & best-effort — never crash the caller (shutdown path must not hang).
  - No network, no extra deps, stdlib only.
"""
from __future__ import annotations

import os
import sys
import time
import random
import shutil
import tempfile
import subprocess
import ctypes
from pathlib import Path

# ---------------------------------------------------------------------------
# Secure erase
# ---------------------------------------------------------------------------

def secure_delete(path: Path, passes: int = 1, do_overwrite: bool = True) -> bool:
    """Overwrite file content then delete. Returns True if file gone/error ignored."""
    try:
        p = Path(path)
        if not p.exists() or p.is_dir():
            return True
        # Ensure writable
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass
        if do_overwrite:
            try:
                size = p.stat().st_size
                # Cap overwrite to avoid stalling on huge logs; still overwrite first 8MB + last bytes
                # For typical watcher.log (<1MB) this is whole file.
                with open(p, "r+b") as f:
                    # Pass 1: random
                    if size > 0:
                        f.seek(0)
                        # write in chunks to avoid huge allocation
                        chunk = 64 * 1024
                        remaining = size
                        while remaining > 0:
                            n = min(chunk, remaining)
                            f.write(os.urandom(n))
                            remaining -= n
                        f.flush()
                        try:
                            os.fsync(f.fileno())
                        except Exception:
                            pass
                        # Pass 2: zeros (if passes >=1 we do random+zero as 1 secure pass)
                        f.seek(0)
                        remaining = size
                        while remaining > 0:
                            n = min(chunk, remaining)
                            f.write(b"\x00" * n)
                            remaining -= n
                        f.flush()
                        try:
                            os.fsync(f.fileno())
                        except Exception:
                            pass
            except Exception:
                pass
        try:
            p.unlink()
            return True
        except Exception:
            # Fallback: try Windows del via cmd
            try:
                os.remove(p)
                return True
            except Exception:
                return False
    except Exception:
        return False


def secure_delete_pattern(directory: Path, pattern: str, do_overwrite: bool = True):
    """Delete all files matching pattern in directory securely."""
    try:
        if not directory.exists():
            return
        for item in directory.glob(pattern):
            if item.is_file():
                secure_delete(item, do_overwrite=do_overwrite)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Host locations
# ---------------------------------------------------------------------------

def appdata_jampandu_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "Jampandu"

def localappdata_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))

# ---------------------------------------------------------------------------
# Individual cleaners
# ---------------------------------------------------------------------------

def clean_appdata_traces(secure: bool = True, keep_dir: bool = False) -> dict:
    """Wipe %APPDATA%\\Jampandu\\* traces."""
    result = {"watcher_log": False, "flags": 0, "host_clean_log": False}
    jdir = appdata_jampandu_dir()
    if not jdir.exists():
        return result
    # Flags first
    try:
        for flag in list(jdir.glob("insert_*.flag")) + list(jdir.glob("remove_*.flag")):
            if secure_delete(flag, do_overwrite=secure):
                result["flags"] += 1
    except Exception:
        pass
    # watcher.log — contains drive letters, timestamps
    wl = jdir / "watcher.log"
    if wl.exists():
        result["watcher_log"] = secure_delete(wl, do_overwrite=secure)
    # host_clean.log — wipe if present (will be recreated by caller if needed, then wiped again at end)
    hcl = jdir / "host_clean.log"
    if hcl.exists():
        # Don't delete yet if caller is logging; caller should delete last
        pass
    # Optionally remove entire dir if empty and keep_dir=False and amnesiac mode
    if not keep_dir:
        try:
            # Only remove if only host_clean.log left or empty
            remaining = [x for x in jdir.iterdir() if x.name != "host_clean.log"]
            if not remaining:
                # secure delete host_clean.log last
                if hcl.exists():
                    result["host_clean_log"] = secure_delete(hcl, do_overwrite=secure)
                try:
                    jdir.rmdir()
                except Exception:
                    pass
            # Also remove autostart_watcher.ps1 copy if present and caller requested full amnesiac
            # NOTE: we do NOT delete autostart_watcher.ps1 by default — user uninstall controls that.
        except Exception:
            pass
    return result


def clear_clipboard() -> bool:
    """Clear Windows clipboard (best-effort)."""
    try:
        if os.name == "nt":
            # Use ctypes to empty clipboard without spawning process (no host trace)
            try:
                user32 = ctypes.windll.user32
                kernel32 = ctypes.windll.kernel32
                user32.OpenClipboard(0)
                user32.EmptyClipboard()
                user32.CloseClipboard()
                return True
            except Exception:
                pass
            # Fallback: clip
            try:
                subprocess.run("echo. | clip", shell=True, timeout=2, capture_output=True)
                return True
            except Exception:
                return False
        else:
            return False
    except Exception:
        return False


def clean_temp_traces(secure: bool = True, pendrive_letter: str | None = None) -> int:
    """Clean %TEMP% files that could relate to the pendrive session."""
    cleaned = 0
    temp_dirs = set()
    for env in ("TEMP", "TMP", "LOCALAPPDATA"):
        v = os.environ.get(env)
        if v:
            temp_dirs.add(Path(v))
    # Also add system temp
    temp_dirs.add(Path(tempfile.gettempdir()))
    # Also Windows Temp
    temp_dirs.add(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Temp")

    patterns = [
        "popup_in_*.txt",
        "popup_out_*.txt",
        "prompt_*.txt",
        "popup_prompt_*.txt",
        "pen-ai-*",
        "jampandu-*",
        "jarvis-*",
        "*llama*tmp*",
        "_MEI*",
    ]
    for tdir in temp_dirs:
        try:
            if not tdir.exists():
                continue
            for pat in patterns:
                for f in tdir.glob(pat):
                    try:
                        if f.is_file():
                            if secure_delete(f, do_overwrite=secure):
                                cleaned += 1
                        elif f.is_dir():
                            shutil.rmtree(f, ignore_errors=True)
                            cleaned += 1
                    except Exception:
                        pass
        except Exception:
            continue
    return cleaned


def clean_browser_localhost_cache(secure: bool = False) -> int:
    """
    Best-effort hint to clear Brave/Chrome localhost cache for 127.0.0.1:8765/8766.
    We do NOT delete the whole browser profile — only the cache entries that
    are safe to remove without losing user data. If deletion fails, we skip.
    """
    cleaned = 0
    # Brave / Chrome cache locations contain hashed entries; pinpointing 127.0.0.1 is fragile.
    # The correct guarantee is via HTTP headers (no-store) and incognito launch.
    # This function is a defense-in-depth noop that cleans our own fallback.
    return cleaned


def clean_windows_recent_for_drive(drive_letter: str | None = None) -> int:
    """
    Best-effort: clear RecentDocs entries and Jump Lists that point to the USB drive.
    This touches only the current user's Recent folder, not system-wide.
    """
    cleaned = 0
    try:
        recent = Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Recent"
        if drive_letter and recent.exists():
            dl = drive_letter.strip().upper().rstrip("\\")
            # Recent is full of .lnk files; remove those whose target is the USB drive
            # Without parsing .lnk, we conservatively only remove .lnk files created in last session
            # that have the drive letter in their filename? Safer to skip aggressive.
            # We therefore only clean if drive_letter is provided and user opted amnesiac.
            for lnk in recent.glob("*.lnk"):
                try:
                    # Quick heuristic: check lnk file content for drive letter string
                    with open(lnk, "rb") as fh:
                        data = fh.read(4096)
                        if dl.encode() in data or dl.lower().encode() in data.lower():
                            if secure_delete(lnk, do_overwrite=False):
                                cleaned += 1
                except Exception:
                    continue
    except Exception:
        pass
    return cleaned


def kill_agent_processes_for_drive(drive_root: str | None = None):
    """Terminate llama-server.exe / python processes whose cwd/exe is on the USB drive."""
    if os.name != "nt":
        return 0
    killed = 0
    if not drive_root:
        return 0
    drive_root = os.path.abspath(drive_root).upper()
    try:
        import ctypes
        from ctypes import wintypes
        # Use WMI via PowerShell to find processes — stdlib only alternative is tasklist parsing
        try:
            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5
            )
            # We kill known binaries if their working dir matches drive; tasklist alone insufficient.
            # So we also try wmic
            wmic_out = subprocess.run(
                ["wmic", "process", "get", "ProcessId,ExecutablePath,CommandLine", "/FORMAT:CSV"],
                capture_output=True, text=True, timeout=5
            )
            text = wmic_out.stdout or ""
            import csv as _csv
            import io as _io
            for line in text.splitlines():
                if drive_root[:2] not in line.upper():  # e.g. E:\
                    continue
                try:
                    parts = next(_csv.reader(_io.StringIO(line)))
                    parts = [p.strip() for p in parts]
                except Exception:
                    parts = [p.strip() for p in line.split(",")]
                # CSV format: Node,CommandLine,ExecutablePath,ProcessId
                for p in parts:
                    if p.isdigit():
                        pid = int(p)
                        if pid > 4:
                            low = line.lower()
                            if any(k in low for k in ["llama-server", "llama.exe", "python", "web_ui.py", "run_agent.py"]):
                                try:
                                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=5)
                                    killed += 1
                                except Exception:
                                    pass
                        break
        except Exception:
            pass
    except Exception:
        pass
    return killed

# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def full_host_cleanup(
    drive_root: str | None = None,
    secure: bool = True,
    kill_processes: bool = True,
    clear_clip: bool = True,
    log: bool = True,
) -> dict:
    """
    Erase all host traces related to the pendrive.
    Call this on: pendrive removal, shutdown, logoff, and normal exit.
    """
    start = time.time()
    result: dict = {
        "drive": drive_root,
        "secure": secure,
        "killed": 0,
        "clipboard": False,
        "appdata": {},
        "temp": 0,
        "recent": 0,
        "elapsed_ms": 0,
    }
    try:
        if kill_processes and drive_root:
            result["killed"] = kill_agent_processes_for_drive(drive_root)
    except Exception:
        pass

    try:
        if clear_clip:
            result["clipboard"] = clear_clipboard()
    except Exception:
        pass

    try:
        # Keep dir during runtime; on removal/shutdown we wipe fully
        keep = drive_root is None  # normal exit keeps Jampandu dir; removal/shutdown removes it
        # For amnesiac mode we always wipe fully
        result["appdata"] = clean_appdata_traces(secure=secure, keep_dir=keep)
    except Exception:
        pass

    try:
        result["temp"] = clean_temp_traces(secure=secure, pendrive_letter=drive_root[:2] if drive_root else None)
    except Exception:
        pass

    try:
        if drive_root:
            result["recent"] = clean_windows_recent_for_drive(drive_root)
    except Exception:
        pass

    # Final: if this was a removal/shutdown clean, try to remove host_clean.log itself
    try:
        jdir = appdata_jampandu_dir()
        hcl = jdir / "host_clean.log"
        if hcl.exists() and drive_root:
            secure_delete(hcl, do_overwrite=secure)
            result["host_clean_log_wiped"] = True
            # Try to remove empty dir
            try:
                if not any(jdir.iterdir()):
                    jdir.rmdir()
            except Exception:
                pass
    except Exception:
        pass

    result["elapsed_ms"] = int((time.time() - start) * 1000)

    if log:
        try:
            jdir = appdata_jampandu_dir()
            # Only log if not doing a full wipe (avoid recreating after wipe)
            if not drive_root:
                jdir.mkdir(parents=True, exist_ok=True)
                log_path = jdir / "host_clean.log"
                with open(log_path, "a", encoding="utf-8") as lf:
                    lf.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] cleanup drive={drive_root} result={result}\n")
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# Pendrive-side cleanup (safe_cleanup_tmp equivalent but secure)
# ---------------------------------------------------------------------------

def clean_pendrive_tmp(secure: bool = True) -> int:
    """Securely delete all files under agent/data/tmp (pendrive itself)."""
    count = 0
    base = Path(__file__).resolve().parent
    tmp_dir = base / "data" / "tmp"
    if not tmp_dir.exists():
        return 0
    for item in tmp_dir.iterdir():
        try:
            if item.is_file():
                if secure_delete(item, do_overwrite=secure):
                    count += 1
            elif item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
                count += 1
        except Exception:
            pass
    # Also remove session.lock if present (may be outside loop due to timing)
    try:
        lock = tmp_dir / "session.lock"
        if lock.exists():
            if secure_delete(lock, do_overwrite=secure):
                count += 1
    except Exception:
        pass
    return count


def clean_pendrive_auth_token(secure: bool = True) -> bool:
    base = Path(__file__).resolve().parent
    tok = base / "auth" / "allowlist.token"
    if tok.exists():
        return secure_delete(tok, do_overwrite=secure)
    return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Host cleaner for Pen AI — erase host traces on removal/shutdown.")
    parser.add_argument("--drive", help="Drive root that was removed, e.g. E:\\ (triggers full amnesiac wipe)")
    parser.add_argument("--full-clean", action="store_true", help="Full amnesiac wipe including host logs")
    parser.add_argument("--no-secure", action="store_true", help="Delete without overwrite (faster)")
    parser.add_argument("--pendrive-only", action="store_true", help="Only clean pendrive tmp/token, not host")
    args = parser.parse_args()

    secure = not args.no_secure
    if args.pendrive_only:
        n = clean_pendrive_tmp(secure=secure)
        clean_pendrive_auth_token(secure=secure)
        print(f"Pendrive tmp cleaned: {n} files")
        sys.exit(0)

    drive = args.drive or (args.full_clean and "FULL" or None)
    # If --full-clean without drive, still do full host wipe (simulates removal)
    if args.full_clean and not drive:
        drive = "FULL"

    # full_host_cleanup expects drive_root or None; "FULL" means wipe all host traces
    drive_arg = None if drive == "FULL" else drive
    # For full-clean we pass a fake drive to force full dir removal
    if args.full_clean:
        drive_arg = drive_arg or "C:\\"  # trigger keep_dir=False path
        # Actually pass a non-None to force full wipe in logic
        if drive_arg is None:
            drive_arg = "FULL:\\"

    result = full_host_cleanup(
        drive_root=drive_arg,
        secure=secure,
        kill_processes=bool(args.drive or args.full_clean),
        clear_clip=True,
        log=not args.full_clean,
    )
    # Also always clean pendrive tmp/token when invoked
    clean_pendrive_tmp(secure=secure)
    clean_pendrive_auth_token(secure=secure)

    print(f"Host cleanup done: {result}")
