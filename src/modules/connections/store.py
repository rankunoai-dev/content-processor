"""Platform connections: one saved login per platform account, chosen by projects.

A connection is a named account of a platform (for example "Semrush, agency login"). Many
projects can use the same connection, so its credential is entered once, not per project.

**Secrets never enter the app's data.** A connection stores only the *name* of the
environment variable that holds its key (for example `SEMRUSH_AGENCY`). The value lives
where secrets already live: `.env` on a workstation, the host's variables on Railway. The
app shows whether each variable is set, never its value. The name must be upper-case
letters, digits and underscores, so a name cannot point at an arbitrary setting.

**Projects choose.** A project maps each platform to one connection (`ProjectRecord.connections`).
A project with no connection for a platform keeps using the installation's own key, as
before. A project whose chosen connection has no key set is refused with the reason, never
quietly given another account's key.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import Field

from src.core.config import REPO_ROOT
from src.core.schemas import StrictModel

__all__ = [
    "PLATFORMS",
    "Connection",
    "ConnectionStore",
    "Platform",
    "SavedConnectionError",
    "secret_value",
]

Platform = Literal["semrush", "ahrefs", "gsc", "ga4"]
PLATFORMS: Final[tuple[Platform, ...]] = ("semrush", "ahrefs", "gsc", "ga4")
SECRET_ENV: Final = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
MAX_CONNECTIONS: Final = 200


class SavedConnectionError(ValueError):
    """A connection could not be saved, found or used (the message says why, for people)."""


def read_env_file(path: Path) -> dict[str, str]:
    """`KEY=value` lines of a `.env` file (comments and blank lines skipped; quotes removed)."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def secret_value(name: str, *, environ: dict[str, str] | None = None) -> str | None:
    """The value of the variable `name`: the process environment first, then `.env`."""
    if not SECRET_ENV.match(name):
        return None
    found = (environ if environ is not None else os.environ).get(name)
    if found is None and environ is None:
        found = read_env_file(REPO_ROOT / ".env").get(name)
    return found or None


class Connection(StrictModel):
    """One saved platform account. It holds the name of its key's variable, never the key."""

    connection_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,39}$")
    platform: Platform
    label: str = Field(min_length=1, max_length=80)
    secret_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")
    property_ref: str = Field(
        default="",
        max_length=200,
        description="GA4 property id or Search Console site; empty when the platform needs none",
    )
    created_at: datetime
    created_by: str = Field(min_length=1, max_length=120)


class ConnectionStore:
    """The saved connections, in one JSON file (`<root>/connections.json`)."""

    def __init__(
        self,
        root: Path,
        *,
        secret_lookup: Callable[[str], str | None] = secret_value,
    ) -> None:
        """Bind to a folder; `secret_lookup` resolves a variable name (tests pass their own)."""
        self._path = Path(root) / "connections.json"
        self._lookup = secret_lookup
        self._lock = threading.RLock()

    def _load(self) -> list[Connection]:
        if not self._path.exists():
            return []
        data = json.loads(self._path.read_text(encoding="utf-8"))
        return [Connection.model_validate(item) for item in data]

    def _save(self, items: list[Connection]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([c.model_dump(mode="json") for c in items], indent=2), encoding="utf-8"
        )
        os.replace(tmp, self._path)

    def all(self) -> list[Connection]:
        """Every connection, by platform then label."""
        with self._lock:
            return sorted(self._load(), key=lambda c: (c.platform, c.label.casefold()))

    def get(self, connection_id: str) -> Connection:
        """The connection with this id (SavedConnectionError when there is none)."""
        with self._lock:
            for item in self._load():
                if item.connection_id == connection_id:
                    return item
        raise SavedConnectionError(f"there is no connection {connection_id!r}")

    def add(
        self,
        *,
        platform: str,
        label: str,
        secret_env: str,
        created_by: str,
        property_ref: str = "",
    ) -> Connection:
        """Save a connection. Refused when a field is malformed or the same key is saved twice."""
        if platform not in PLATFORMS:
            raise SavedConnectionError(f"platform must be one of {', '.join(PLATFORMS)}")
        if not SECRET_ENV.match(secret_env):
            raise SavedConnectionError(
                "the key's variable name must be upper-case letters, digits and underscores, "
                "for example SEMRUSH_AGENCY"
            )
        with self._lock:
            items = self._load()
            if len(items) >= MAX_CONNECTIONS:
                raise SavedConnectionError(
                    "there are too many connections; remove unused ones first"
                )
            if any(c.platform == platform and c.secret_env == secret_env for c in items):
                raise SavedConnectionError(
                    f"{secret_env} is already saved as a {platform} connection"
                )
            base = _slug(label or platform)
            taken = {c.connection_id for c in items}
            cid, n = base, 2
            while cid in taken:
                cid, n = f"{base}-{n}", n + 1
            item = Connection(
                connection_id=cid,
                platform=platform,
                label=label.strip(),
                secret_env=secret_env,
                property_ref=property_ref.strip(),
                created_at=datetime.now(UTC),
                created_by=created_by,
            )
            items.append(item)
            self._save(items)
            return item

    def remove(self, connection_id: str, *, in_use_by: list[str]) -> None:
        """Delete a connection. Refused while a project still uses it."""
        if in_use_by:
            names = ", ".join(in_use_by)
            raise SavedConnectionError(
                f"it is still used by {names}; choose another connection in those projects first"
            )
        with self._lock:
            items = self._load()
            kept = [c for c in items if c.connection_id != connection_id]
            if len(kept) == len(items):
                raise SavedConnectionError(f"there is no connection {connection_id!r}")
            self._save(kept)

    def key_set(self, connection: Connection) -> bool:
        """True when the connection's variable holds a value (the value is never returned)."""
        return self._lookup(connection.secret_env) is not None

    def secret(self, connection: Connection) -> str:
        """The key itself, for a pull. SavedConnectionError when the variable is not set."""
        value = self._lookup(connection.secret_env)
        if value is None:
            raise SavedConnectionError(
                f"the {connection.platform} connection {connection.label!r} has no key: set "
                f"{connection.secret_env} in the environment (.env or the host's variables)"
            )
        return value


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
    return (cleaned[:40].strip("-")) or "connection"


def bound_key(
    store: ConnectionStore, project_connections: dict[str, str], platform: str
) -> str | None:
    """The key of the connection a project chose for `platform`; None when it chose none.

    A chosen connection that cannot give a key raises (never a silent fallback to another key).
    """
    cid = project_connections.get(platform)
    if not cid:
        return None
    return store.secret(store.get(cid))
