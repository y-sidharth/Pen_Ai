"""Strict, local-only command policy for the Jampandu agent.

The agent is intentionally limited to a few read-only diagnostic programs.
Adding a command requires an explicit code change and review; configuration
cannot widen this allowlist.
"""
from __future__ import annotations

import re
import shlex


SAFE_COMMANDS: dict[str, set[str] | None] = {
    "hostname": set(),
    "ipconfig": {"/all"},
    "systeminfo": set(),
    "tasklist": set(),
    "whoami": {"/all", "/groups", "/priv", "/user"},
}

_FORBIDDEN = re.compile(r"[&|;<>`\r\n]|\$\(|%[A-Za-z0-9_]+%")


def validate_command(command: str) -> tuple[list[str] | None, str | None]:
    """Return safe argv, or a human-readable rejection reason.

    Paths, scripts, shell operators, chained commands, and unrecognised command
    arguments are rejected.  Commands run with ``shell=False``.
    """
    if not command or not command.strip():
        return None, "No command provided."
    if _FORBIDDEN.search(command):
        return None, "Shell operators, variable expansion, and line breaks are not allowed."
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:
        return None, "The command contains malformed quoting."
    if not parts:
        return None, "No command provided."

    program = parts[0].strip('"').lower()
    if program.endswith(".exe"):
        program = program[:-4]
    if not re.fullmatch(r"[a-z0-9_-]+", program):
        return None, "Command paths and scripts are not allowed."
    allowed_args = SAFE_COMMANDS.get(program)
    if allowed_args is None and program not in SAFE_COMMANDS:
        return None, f"'{program}' is not in the safe command allowlist."
    args = [part.strip('"') for part in parts[1:]]
    if not allowed_args and args:
        return None, f"'{program}' does not allow arguments."
    if allowed_args and any(arg.lower() not in allowed_args for arg in args):
        return None, f"One or more arguments for '{program}' are not allowed."
    return [program, *args], None
