"""Structured audit logging.

Every agent action, tool invocation and guardrail decision is emitted as a
single-line JSON record so that runs are machine-auditable after the fact. This
is the *only* sanctioned output channel — `print()` is banned by the linter.

Two sinks are configured:

* stderr, for the human operator watching the run;
* an append-only JSONL file (`AUDIT_LOG_PATH`), for the durable audit trail.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.core.config import get_settings

__all__ = ["JsonFormatter", "current_trace_id", "get_logger", "setup_logging", "trace_context"]

_TRACE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)

# Attributes present on every LogRecord; anything else the caller attached via
# `extra=` is treated as structured payload.
_RESERVED_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "stacklevel",
        "thread",
        "threadName",
        "taskName",
    }
)

_configured = False


def current_trace_id() -> str | None:
    """Return the trace id bound to the current execution context, if any."""
    return _TRACE_ID.get()


@contextmanager
def trace_context(trace_id: str | None = None) -> Generator[str]:
    """Bind a trace id to everything logged inside the block.

    Args:
        trace_id: Explicit id to bind. A short random id is generated if omitted.

    Yields:
        The bound trace id, so callers can attach it to their results.
    """
    resolved = trace_id or uuid.uuid4().hex[:16]
    token = _TRACE_ID.set(resolved)
    try:
        yield resolved
    finally:
        _TRACE_ID.reset(token)


class JsonFormatter(logging.Formatter):
    """Render a `LogRecord` as one line of JSON."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialise the record, folding in any `extra=` fields and the trace id."""
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        trace_id = getattr(record, "trace_id", None) or current_trace_id()
        if trace_id:
            payload["trace_id"] = trace_id

        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # `default=str` keeps the logger from ever being the thing that crashes
        # a run because someone logged a Path or a datetime.
        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging(*, force: bool = False) -> None:
    """Configure the root logger. Idempotent unless `force` is set.

    Args:
        force: Re-configure even if logging was already set up (tests use this).
    """
    global _configured  # noqa: PLW0603 — module-level idempotency flag
    if _configured and not force:
        return

    settings = get_settings()
    root = logging.getLogger("rankuno")
    root.setLevel(settings.log_level)
    root.handlers.clear()
    root.propagate = False

    formatter: logging.Formatter = (
        JsonFormatter()
        if settings.log_format == "json"
        else logging.Formatter("%(asctime)s %(levelname)-8s %(name)s | %(message)s")
    )

    console = logging.StreamHandler(stream=sys.stderr)
    console.setFormatter(formatter)
    root.addHandler(console)

    # The durable audit trail is best-effort: a read-only or missing filesystem
    # must degrade to console-only rather than take the whole run down.
    audit_path: Path = settings.audit_log_path
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(audit_path, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)
    except OSError as exc:  # pragma: no cover - environment dependent
        root.warning("audit_log_unavailable", extra={"path": str(audit_path), "error": str(exc)})

    _configured = True


def get_logger(name: str) -> logging.LoggerAdapter[logging.Logger]:
    """Return a namespaced logger, configuring logging on first use.

    Args:
        name: Dotted module or tool name, e.g. `"core.guardrails"`.

    Returns:
        A logger under the `rankuno.` namespace.
    """
    setup_logging()
    return logging.LoggerAdapter(logging.getLogger(f"rankuno.{name}"), {})
