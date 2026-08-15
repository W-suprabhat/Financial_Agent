"""
Who is using this, and how the app will one day be told properly.

This is deliberately not authentication. It is a shared access code plus a name the
user types, which means anyone holding the code can claim to be anyone. What it buys
is *identity*: every approval, correction and withdrawal can name a person instead of
the literal string "analyst", and a fix learned on one engagement can be scoped to it.
Those are data-model facts the rest of the app needs now, and they do not depend on how
the identity was established.

The seam is current_user(). Everything else asks that dependency who is here and does
not care how it found out. Moving to Entra ID later means rewriting this one function
and its two routes - no call site changes, because no call site knows the difference.

Until that happens this is a prototype gate: it keeps a demo URL from being wide open,
and it must not be treated as protection for client documents.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from fastapi import Cookie, HTTPException

from .config import settings

COOKIE = "fa_session"
MAX_AGE = 12 * 60 * 60  # a working day; a prototype session should not outlive one


@dataclass(frozen=True)
class User:
    """The person acting. `name` is self-asserted - see the module docstring."""

    name: str
    issued_at: int

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "issued_at": self.issued_at}


def _secret() -> bytes:
    return settings.session_secret.encode()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(payload: Dict[str, Any]) -> str:
    """A cookie value the client cannot edit: base64(json).base64(hmac)."""
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    mac = hmac.new(_secret(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def unsign(token: str) -> Optional[Dict[str, Any]]:
    """The payload, or None if the token was tampered with, malformed or expired."""
    try:
        body, mac = token.split(".", 1)
    except ValueError:
        return None

    expected = hmac.new(_secret(), body.encode(), hashlib.sha256).digest()
    # compare_digest rather than == so a forged cookie cannot be refined byte by byte
    # from response timing.
    if not hmac.compare_digest(_unb64(mac), expected):
        return None

    try:
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    if int(time.time()) - int(payload.get("issued_at", 0)) > MAX_AGE:
        return None
    return payload


def issue(name: str) -> str:
    """A session cookie for someone who supplied the access code."""
    return sign({"name": name, "issued_at": int(time.time())})


def check_access_code(supplied: str) -> bool:
    """
    Whether the shared code is right.

    compare_digest again: the code is one secret shared by everyone, so it is worth not
    leaking its length or prefix through timing.
    """
    return hmac.compare_digest((supplied or "").strip(), settings.access_code)


def user_from_cookie(token: Optional[str]) -> Optional[User]:
    payload = unsign(token) if token else None
    if not payload or not payload.get("name"):
        return None
    return User(name=str(payload["name"]), issued_at=int(payload.get("issued_at", 0)))


async def current_user(fa_session: Optional[str] = Cookie(default=None)) -> User:
    """
    FastAPI dependency: the signed-in user, or 401.

    Applied to the API itself rather than only to the page that calls it. A login form
    in front of the UI while /extract and /api/provenance/{job_id} still answered every
    caller would look like protection without being any.
    """
    user = user_from_cookie(fa_session)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in to use this")
    return user
