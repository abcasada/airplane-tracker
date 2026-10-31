"""Signed links for notification buttons ("Mute 1 day", "Not useful").

The phone calls these without a web session, so each button carries a token that names exactly
one action on one aircraft or alert, expires after a day, and is signed with the app's secret key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re

MUTE_SECONDS = 86400.0
TOKEN_TTL_S = 86400.0
_VALUE = {"mute": re.compile(r"^~?[0-9a-f]{6}$"), "down": re.compile(r"^\d+(,\d+){0,20}$")}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sig(secret: str, payload: str) -> str:
    mac = hmac.new(secret.encode(), b"notify-action:" + payload.encode(), hashlib.sha256)
    return _b64(mac.digest()[:18])


def sign(secret: str, action: str, value: str, now: float) -> str:
    if action not in _VALUE or not _VALUE[action].match(value):
        raise ValueError(f"bad action {action}:{value}")
    payload = f"{action}:{value}:{int(now + TOKEN_TTL_S)}"
    return f"{_b64(payload.encode())}.{_sig(secret, payload)}"


def verify(secret: str, token: str, now: float) -> tuple[str, str] | None:
    """(action, value) if the token is genuine and unexpired, else None."""
    try:
        body, sig = token.split(".", 1)
        payload = _unb64(body).decode()
        action, value, exp = payload.split(":")
    except (ValueError, UnicodeDecodeError):
        return None
    if not hmac.compare_digest(sig, _sig(secret, payload)):
        return None
    if action not in _VALUE or not _VALUE[action].match(value) or float(exp) < now:
        return None
    return action, value
