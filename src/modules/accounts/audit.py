"""Who did what: an append-only record of sign-ins and downloads.

One JSON line per event in `<app data>/audit/user_actions.jsonl`. The file sits next to
the accounts, so on Railway it lives on the persistent `/data` volume and survives a
redeploy. It is written whatever the log level is: this is a record, not a diagnostic.

Events:

* `login_succeeded`, `login_failed` (`reason`: `wrong_password` or `throttled`), `logout`;
* `download`: any response sent as an attachment (reports, workbooks, archives,
  deliverables), with the path, the file name and the status;
* `locked_no_accounts`: a request refused because a server listening beyond this machine
  found no accounts (written once per lock-out, not per request).

Each line has `at` (UTC), `event`, `user` (the signed-in account, or the name tried for a
failed sign-in, or null without accounts), `ip` (the address the server saw; behind a
proxy that is the proxy) and, when the request carried one, `forwarded_for` as sent
(not verified). Passwords, cookies and tokens are never written.

Writing is best-effort: a full or read-only disk is logged once and never stops a request.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.core.logger import get_logger

__all__ = ["ACTIONS_FILE", "ActionLog"]

ACTIONS_FILE = "user_actions.jsonl"
_MAX_FIELD = 500

_log = get_logger("accounts.audit")


def _short(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_FIELD:
        return value[:_MAX_FIELD] + "..."
    return value


class ActionLog:
    """Appends user actions to `<root>/user_actions.jsonl`."""

    def __init__(self, root: Path) -> None:
        """Bind to a folder (created on the first write)."""
        self.path = root / ACTIONS_FILE
        self._lock = threading.Lock()
        self._warned = False

    def record(
        self,
        event: str,
        *,
        user: str | None,
        ip: str | None = None,
        forwarded_for: str | None = None,
        **details: Any,
    ) -> None:
        """Append one event; never raises."""
        line: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "event": event,
            "user": user,
            "ip": ip,
        }
        if forwarded_for:
            line["forwarded_for"] = _short(forwarded_for)
        line.update({k: _short(v) for k, v in details.items() if v is not None})
        text = json.dumps(line, ensure_ascii=False, default=str) + "\n"
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(text)
        except OSError as exc:
            if not self._warned:
                self._warned = True
                _log.warning(
                    "user_action_log_unavailable", extra={"path": str(self.path), "error": str(exc)}
                )

    def read(self) -> list[dict[str, Any]]:
        """Every event, oldest first (for tests and the CLI)."""
        if not self.path.is_file():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
