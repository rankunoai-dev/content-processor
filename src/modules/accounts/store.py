"""Accounts, their roles and personal access tokens, kept in `<app data>/accounts/`.

- **Accounts** (`accounts.json`): a user name (sign-in name, lower case), a display
  name, a role, the scrypt hash of the password (never the password), whether the
  password must be changed at the next sign-in (true after an admin sets one), whether
  the account is disabled, and a `session_epoch`: raising it ends every session of
  the account (password change, reset, disable, role change).
- **Roles**, each including the ones below it: `viewer` reads and downloads;
  `reviewer` also records review decisions (verdicts, contradictions, finding
  statuses); `editor` also changes projects (websites, addresses, scrapes, pulls,
  builds, analyses); `admin` also manages accounts.
- **Personal access tokens** (`tokens.json`): for programs such as the Claude Desktop
  connector; stored only as SHA-256 hashes, each acting with its account's role (or
  less), shown once when created.

Accounts exist only when an admin was created (`accounts create-admin`); until then the
app runs as before, on this machine only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import Field

from src.core.schemas import StrictModel
from src.modules.content_processor.projects.security import hash_password, verify_password

__all__ = [
    "MIN_ACCOUNT_PASSWORD",
    "ROLES",
    "Account",
    "AccountError",
    "AccountStore",
    "ApiToken",
    "Role",
    "role_at_least",
]

Role = Literal["viewer", "reviewer", "editor", "admin"]
ROLES: Final[tuple[Role, ...]] = ("viewer", "reviewer", "editor", "admin")
MIN_ACCOUNT_PASSWORD: Final = 10
TOKEN_PREFIX: Final = "cpat_"  # noqa: S105 - a public prefix, not a secret
_USERNAME: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """A real hash of a random password: a missing account costs the same time to check."""
    return hash_password(secrets.token_urlsafe(16))


class AccountError(ValueError):
    """An account could not be created or changed (the message says why, for people)."""


def role_at_least(role: str, wanted: Role) -> bool:
    """True when `role` includes the rights of `wanted`."""
    return role in ROLES and ROLES.index(role) >= ROLES.index(wanted)


class Account(StrictModel):
    """One person's account (the password hash never leaves the server)."""

    username: str
    display_name: str = Field(min_length=1, max_length=120)
    role: Role
    password_hash: str
    must_change_password: bool = True
    disabled: bool = False
    session_epoch: int = 0
    created_at: datetime
    created_by: str = ""
    last_login_at: datetime | None = None
    password_changed_at: datetime | None = None


class ApiToken(StrictModel):
    """A personal access token (only its hash is kept)."""

    token_id: str
    username: str
    label: str = Field(max_length=120)
    role: Role
    token_hash: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked: bool = False


def _now() -> datetime:
    return datetime.now(UTC)


def _check_password(password: str) -> None:
    if len(password) < MIN_ACCOUNT_PASSWORD:
        msg = f"a password needs at least {MIN_ACCOUNT_PASSWORD} characters"
        raise AccountError(msg)
    if len(password) > 200:
        msg = "a password can have at most 200 characters"
        raise AccountError(msg)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AccountStore:
    """Every account and token of the installation (thread-safe, atomic writes)."""

    def __init__(self, root: Path) -> None:
        """Bind to `<app data>/accounts`."""
        self.root = root
        self._lock = threading.RLock()

    # -- files ---------------------------------------------------------------------------

    def _read(self, name: str) -> dict[str, dict[str, object]]:
        path = self.root / name
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def _write(self, name: str, data: Mapping[str, object]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / name
        tmp = path.with_name(f"{name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True, default=str), encoding="utf-8")
        os.replace(tmp, path)

    def _accounts(self) -> dict[str, Account]:
        return {k: Account.model_validate(v) for k, v in self._read("accounts.json").items()}

    def _save(self, accounts: dict[str, Account]) -> None:
        self._write("accounts.json", {k: v.model_dump(mode="json") for k, v in accounts.items()})

    # -- accounts ------------------------------------------------------------------------

    def active(self) -> bool:
        """True once any account exists: sign-in is then required everywhere."""
        with self._lock:
            return bool(self._read("accounts.json"))

    def all(self) -> list[Account]:
        """Every account, by user name."""
        with self._lock:
            return sorted(self._accounts().values(), key=lambda a: a.username)

    def get(self, username: str) -> Account | None:
        """One account, or None."""
        with self._lock:
            return self._accounts().get(username.strip().lower())

    def create(
        self,
        username: str,
        display_name: str,
        role: Role,
        password: str,
        *,
        created_by: str = "",
        must_change_password: bool = True,
    ) -> Account:
        """A new account; raises `AccountError` (name taken or invalid, weak password)."""
        name = username.strip().lower()
        if not _USERNAME.match(name):
            msg = (
                "a user name is 2-40 characters: lower-case letters, digits, dots, dashes "
                "or underscores, starting with a letter or digit"
            )
            raise AccountError(msg)
        if role not in ROLES:
            raise AccountError(f"unknown role {role!r}")
        _check_password(password)
        with self._lock:
            accounts = self._accounts()
            if name in accounts:
                raise AccountError(f"the user name {name!r} is taken")
            account = Account(
                username=name,
                display_name=display_name.strip() or name,
                role=role,
                password_hash=hash_password(password),
                must_change_password=must_change_password,
                created_at=_now(),
                created_by=created_by,
            )
            accounts[name] = account
            self._save(accounts)
            return account

    def verify(self, username: str, password: str) -> Account | None:
        """The account when the password is right and the account enabled, else None."""
        account = self.get(username)
        if account is None:
            verify_password(password, _dummy_hash())  # same time whether or not it exists
            return None
        if not verify_password(password, account.password_hash) or account.disabled:
            return None
        return account

    def update(self, username: str, **changes: object) -> Account:
        """Change fields of an account; raises `AccountError` when it does not exist."""
        with self._lock:
            accounts = self._accounts()
            name = username.strip().lower()
            if name not in accounts:
                raise AccountError(f"there is no account {name!r}")
            updated = accounts[name].model_copy(update=changes)
            accounts[name] = Account.model_validate(updated.model_dump())
            self._save(accounts)
            return accounts[name]

    def set_password(self, username: str, password: str, *, must_change: bool) -> Account:
        """A new password; every session of the account ends."""
        _check_password(password)
        account = self.get(username)
        if account is None:
            raise AccountError(f"there is no account {username!r}")
        return self.update(
            account.username,
            password_hash=hash_password(password),
            must_change_password=must_change,
            password_changed_at=_now(),
            session_epoch=account.session_epoch + 1,
        )

    def admins_left(self, excluding: str) -> int:
        """Enabled admins other than `excluding` (the last admin cannot be removed)."""
        return sum(
            1
            for a in self.all()
            if a.role == "admin" and not a.disabled and a.username != excluding.lower()
        )

    # -- personal access tokens ----------------------------------------------------------

    def create_token(self, username: str, label: str, role: Role | None = None) -> str:
        """A new token for programs; returns it (shown once). Its role is at most the account's."""
        account = self.get(username)
        if account is None or account.disabled:
            raise AccountError(f"there is no enabled account {username!r}")
        wanted = role or account.role
        if not role_at_least(account.role, wanted):
            msg = f"a {account.role} cannot create a token with the {wanted} role"
            raise AccountError(msg)
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        record = ApiToken(
            token_id="tok-" + secrets.token_hex(6),
            username=account.username,
            label=label.strip() or "token",
            role=wanted,
            token_hash=_token_hash(token),
            created_at=_now(),
        )
        with self._lock:
            tokens = self._read("tokens.json")
            tokens[record.token_id] = record.model_dump(mode="json")
            self._write("tokens.json", tokens)
        return token

    def tokens(self, username: str | None = None) -> list[ApiToken]:
        """Tokens (of one account), newest first."""
        with self._lock:
            found = [ApiToken.model_validate(v) for v in self._read("tokens.json").values()]
        if username is not None:
            found = [t for t in found if t.username == username.lower()]
        return sorted(found, key=lambda t: t.created_at, reverse=True)

    def revoke_token(self, token_id: str) -> None:
        """Stop a token from working."""
        with self._lock:
            tokens = self._read("tokens.json")
            if token_id not in tokens:
                raise AccountError(f"there is no token {token_id!r}")
            tokens[token_id] = {**tokens[token_id], "revoked": True}
            self._write("tokens.json", tokens)

    def account_for_token(self, token: str) -> tuple[Account, Role] | None:
        """The account and role a token acts as, or None (unknown, revoked, account disabled)."""
        if not token.startswith(TOKEN_PREFIX):
            return None
        wanted = _token_hash(token)
        for record in self.tokens():
            if record.revoked or not secrets.compare_digest(record.token_hash, wanted):
                continue
            account = self.get(record.username)
            if account is None or account.disabled:
                return None
            role = record.role if role_at_least(account.role, record.role) else account.role
            return account, role
        return None
