"""Credential and approval helpers for the local Jarvis agent.

This module deliberately uses only the Python standard library so it works with
the bundled portable interpreter.  It protects against accidental disclosure of
credentials, but a USB drive that an attacker can modify is not a trusted secret
store; that limitation is documented in the README.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from getpass import getpass


PBKDF2_ITERATIONS = 310_000


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"), validate=True)


def make_verifier(secret: str) -> dict[str, object]:
    """Return a serialisable, salted PBKDF2 verifier for a non-empty secret."""
    if not secret:
        raise ValueError("Credential cannot be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", secret.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return {
        "salt": _encode(salt),
        "hash": _encode(digest),
        "iterations": PBKDF2_ITERATIONS,
    }


def verify_verifier(secret: str, verifier: object) -> bool:
    if not secret or not isinstance(verifier, dict):
        return False
    try:
        salt = _decode(str(verifier["salt"]))
        expected = _decode(str(verifier["hash"]))
        iterations = int(verifier.get("iterations", PBKDF2_ITERATIONS))
        if iterations < 100_000 or iterations > 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", secret.encode("utf-8"), salt, iterations
        )
    except (KeyError, TypeError, ValueError, base64.binascii.Error):
        return False
    return hmac.compare_digest(actual, expected)


def setup_credential(label: str, input_func=getpass, output=print) -> dict[str, object]:
    """Interactively create a verifier without exposing the entered value."""
    output(f"No {label} is configured. Create one now.")
    while True:
        first = input_func(f"Enter new {label}: ")
        second = input_func(f"Repeat new {label}: ")
        if not first:
            output(f"{label.capitalize()} cannot be empty.")
        elif first != second:
            output("Values do not match; try again.")
        else:
            return make_verifier(first)


def create_nonce(length: int = 16) -> str:
    """Generate a readable cryptographically secure approval nonce."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))
