"""Project passwords and unlock tokens.

Passwords are stored only as salted scrypt hashes. Entering the right password
unlocks one project for `TOKEN_TTL`: the server returns a token signed with an
app secret (HMAC-SHA256) that names the project and its expiry; every write
must carry it. Wrong passwords are slowed down per project.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

__all__ = [
    "MIN_PASSWORD_LENGTH",
    "TOKEN_TTL_S",
    "AttemptLimiter",
    "TokenSigner",
    "hash_password",
    "verify_password",
]

MIN_PASSWORD_LENGTH = 8
TOKEN_TTL_S = 8 * 3600
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    """`scrypt$<n>$<r>$<p>$<salt>$<hash>`; the password itself is never stored."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of a password against a stored hash."""
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        expected = _unb64(digest)
        got = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


class TokenSigner:
    """Signs and checks `project id + expiry` tokens with a per-installation secret."""

    def __init__(self, secret: bytes, *, ttl_s: int = TOKEN_TTL_S) -> None:
        """Bind to a secret (at least 32 bytes)."""
        if len(secret) < 32:
            raise ValueError("the token secret must be at least 32 bytes")
        self._secret = secret
        self.ttl_s = ttl_s

    @classmethod
    def from_file(cls, path: Path, *, ttl_s: int = TOKEN_TTL_S) -> TokenSigner:
        """Load the secret, creating it on first use (readable by this user only)."""
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
            tmp.write_bytes(secrets.token_bytes(32))
            os.replace(tmp, path)
            with contextlib.suppress(OSError):  # Windows ignores POSIX modes
                path.chmod(0o600)
        return cls(path.read_bytes(), ttl_s=ttl_s)

    def _mac(self, payload: bytes) -> bytes:
        return hmac.new(self._secret, payload, hashlib.sha256).digest()

    def issue(self, project_id: str, *, now: float | None = None) -> tuple[str, float]:
        """A token for one project and its expiry time (epoch seconds)."""
        expires = (time.time() if now is None else now) + self.ttl_s
        payload = json.dumps({"p": project_id, "e": int(expires)}, separators=(",", ":")).encode()
        return f"{_b64(payload)}.{_b64(self._mac(payload))}", float(int(expires))

    def check(self, token: str | None, project_id: str, *, now: float | None = None) -> bool:
        """True when the token is genuine, unexpired and for this project."""
        if not token or token.count(".") != 1:
            return False
        body, mac = token.split(".")
        try:
            payload = _unb64(body)
            if not hmac.compare_digest(_unb64(mac), self._mac(payload)):
                return False
            data = json.loads(payload)
        except (ValueError, TypeError):
            return False
        current = time.time() if now is None else now
        return data.get("p") == project_id and float(data.get("e", 0)) > current


class AttemptLimiter:
    """After `free` wrong passwords, each further try for that project waits longer."""

    def __init__(
        self, *, free: int = 5, base_wait_s: float = 2.0, max_wait_s: float = 300.0
    ) -> None:
        """Allow `free` failures, then wait base * 2^(extra) seconds, capped."""
        self._free = free
        self._base = base_wait_s
        self._max = max_wait_s
        self._state: dict[str, tuple[int, float]] = {}

    def wait_s(self, key: str, *, now: float | None = None) -> float:
        """Seconds until the next try is allowed (0 when it is allowed now)."""
        failures, last = self._state.get(key, (0, 0.0))
        if failures < self._free:
            return 0.0
        wait = min(self._max, self._base * 2.0 ** (failures - self._free))
        current = time.time() if now is None else now
        return max(0.0, last + wait - current)

    def failed(self, key: str, *, now: float | None = None) -> None:
        """Record a wrong password."""
        failures, _ = self._state.get(key, (0, 0.0))
        self._state[key] = (failures + 1, time.time() if now is None else now)

    def succeeded(self, key: str) -> None:
        """Forget the failures after a right password."""
        self._state.pop(key, None)
