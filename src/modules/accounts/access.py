"""Sessions and the role gate every request passes once accounts exist.

**Sessions.** Signing in sets an HttpOnly, SameSite=Lax cookie (`cp_session`; Secure
over https) holding the user name, the account's `session_epoch` and an expiry, signed
with a key derived from the installation secret. A session ends when it expires
(`CP_SESSION_HOURS`), on sign-out, or for every device at once when the account's
epoch rises (password changed or reset, account disabled, role changed).

**Programs** (the Claude Desktop connector) send `Authorization: Bearer cpat_...`, a
personal access token, instead of the cookie.

**The gate** decides, before any route runs, who may do what:

| Request | Needs |
|---|---|
| the web app's own files, `GET /api/auth/me`, `POST /api/auth/login` | nothing |
| `POST /api/auth/logout`, `POST /api/auth/password` | signed in |
| any other `GET` under `/api/`, `POST /api/mcp/call` (read-only tools) | viewer |
| review decisions and finding statuses (`REVIEW_PATHS`) | reviewer |
| `/api/accounts...`, deleting a project | admin |
| every other change | editor |

An account that must change its password may only use `/api/auth/...` until it has.
The request then runs with `ACTING` set, so project unlock checks pass. Without any
account the gate lets everything through, as before (the server then listens on this
machine only).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from typing import Final, Literal

from src.modules.accounts.store import Account, AccountStore, Role, role_at_least

__all__ = [
    "COOKIE",
    "REVIEW_PATHS",
    "Caller",
    "SessionSigner",
    "decide",
    "required_role",
]

COOKIE: Final = "cp_session"
Need = Literal["public", "signed_in", "viewer", "reviewer", "editor", "admin"]

REVIEW_PATHS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^/api/runs/[^/]+/decisions$"),
    re.compile(r"^/api/projects/[^/]+/gaps/findings/[^/]+/status$"),
    re.compile(r"^/api/projects/[^/]+/sites/[^/]+/runs/[^/]+/verdicts$"),
)
_ADMIN_PATHS: Final = (
    re.compile(r"^/api/accounts(/.*)?$"),
    re.compile(r"^/api/projects/[^/]+/delete$"),
)
_PUBLIC: Final = {("GET", "/api/auth/me"), ("POST", "/api/auth/login")}
_SIGNED_IN: Final = {("POST", "/api/auth/logout"), ("POST", "/api/auth/password")}


def required_role(method: str, path: str) -> Need:
    """What a request needs (see the table above)."""
    verb = "GET" if method in ("GET", "HEAD") else method
    if not path.startswith("/api/") or (verb, path) in _PUBLIC:
        return "public"
    if (verb, path) in _SIGNED_IN:
        return "signed_in"
    if any(p.match(path) for p in _ADMIN_PATHS):
        return "admin"
    if verb == "GET" or path == "/api/mcp/call":
        return "viewer"
    if any(p.match(path) for p in REVIEW_PATHS):
        return "reviewer"
    return "editor"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SessionSigner:
    """Signs and reads session cookies."""

    def __init__(self, key: bytes, *, hours: float = 12.0) -> None:
        """Bind to a 32-byte key (derived from the installation secret)."""
        self._key = key
        self.ttl_s = int(hours * 3600)

    def issue(self, account: Account, *, now: float | None = None) -> str:
        """A cookie value for this account."""
        expires = int((time.time() if now is None else now) + self.ttl_s)
        payload = json.dumps(
            {"u": account.username, "v": account.session_epoch, "e": expires},
            separators=(",", ":"),
        ).encode()
        mac = hmac.new(self._key, payload, hashlib.sha256).digest()
        return f"{_b64(payload)}.{_b64(mac)}"

    def read(self, value: str | None, *, now: float | None = None) -> tuple[str, int] | None:
        """(user name, epoch) of a genuine, unexpired cookie, else None."""
        if not value or value.count(".") != 1:
            return None
        body, mac = value.split(".")
        try:
            payload = _unb64(body)
            expected = hmac.new(self._key, payload, hashlib.sha256).digest()
            if not hmac.compare_digest(_unb64(mac), expected):
                return None
            data = json.loads(payload)
            if float(data["e"]) <= (time.time() if now is None else now):
                return None
            return str(data["u"]), int(data["v"])
        except (ValueError, TypeError, KeyError):
            return None


@dataclass(frozen=True)
class Caller:
    """Who is calling: the account and the role it acts with (a token may act with less)."""

    account: Account
    role: Role
    via: Literal["session", "token"]


def identify(
    accounts: AccountStore,
    sessions: SessionSigner,
    *,
    cookie: str | None,
    authorization: str | None,
) -> Caller | None:
    """The caller of a request, from its session cookie or bearer token."""
    if authorization and authorization.lower().startswith("bearer "):
        found = accounts.account_for_token(authorization[7:].strip())
        return Caller(found[0], found[1], "token") if found else None
    read = sessions.read(cookie)
    if read is None:
        return None
    account = accounts.get(read[0])
    if account is None or account.disabled or account.session_epoch != read[1]:
        return None
    return Caller(account, account.role, "session")


def decide(need: Need, caller: Caller | None, path: str) -> tuple[int, str] | None:
    """None when the request may go on, else (status, plain reason)."""
    if need == "public":
        return None
    if caller is None:
        return 401, "Sign in to continue."
    if caller.account.must_change_password and not path.startswith("/api/auth/"):
        return 403, "Choose a new password first."
    if need == "signed_in" or role_at_least(caller.role, need):
        return None
    words = {
        "viewer": "read this",
        "reviewer": "record review decisions",
        "editor": "make changes",
        "admin": "manage accounts or delete projects",
    }
    return 403, f"Your role ({caller.role}) cannot {words[need]}; ask an admin."
