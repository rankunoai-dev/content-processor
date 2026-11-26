"""Email digest of a check's new alerts.

Off by default. A project sets `digest_level` ("high", or "medium" for high and medium)
and who gets it; after each check (by hand, automatic or after a scheduled re-scrape)
the alerts at that level that were not emailed in the last `RESEND_DAYS` days are sent
in one message. Delivery is the queue emails' (`projects/notify.py`): the configured
SMTP server, else an `.eml` file in `<app data>/outbox/`. A failed send never fails
the check.

- The addresses are kept in `<app data>/monitoring/<project>/digest.json`, apart from
  the watch settings, which show them partly hidden (`g***@rankuno.com`).
- A **confidential** project's digest holds only counts by severity and kind and the
  link to the app: no page titles, addresses or changed text.
- A digest of dummy data (the Semrush or Ahrefs stand-in) says so in its subject.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime, timedelta

from pydantic import Field

from src.core.schemas import StrictModel
from src.modules.content_processor.projects.notify import Notifier, check_email
from src.modules.monitoring.models import Alert, DigestLevel, MonitorCheck

__all__ = ["RESEND_DAYS", "DigestBook", "check_addresses", "compose", "due_alerts", "send_digest"]

RESEND_DAYS = 30
MAX_ADDRESSES = 10
_MAX_ALERTS = 25
_KEEP_SENT = 2000
_LEVELS: dict[DigestLevel, frozenset[str]] = {
    "off": frozenset(),
    "high": frozenset({"high"}),
    "medium": frozenset({"high", "medium"}),
}


class DigestBook(StrictModel):
    """Who gets a project's digest and which alerts were already sent (id -> when)."""

    to: list[str] = Field(default_factory=list, max_length=MAX_ADDRESSES)
    sent: dict[str, datetime] = Field(default_factory=dict)


def check_addresses(addresses: list[str]) -> list[str]:
    """Trimmed, distinct, valid addresses; raises `ValueError` naming the first bad one."""
    out: list[str] = []
    for raw in addresses:
        if not raw.strip():
            continue
        value = check_email(raw)
        if value.casefold() not in {a.casefold() for a in out}:
            out.append(value)
    if len(out) > MAX_ADDRESSES:
        raise ValueError(f"At most {MAX_ADDRESSES} addresses get the digest.")
    return out


def due_alerts(
    check: MonitorCheck, level: DigestLevel, sent: dict[str, datetime], now: datetime
) -> list[Alert]:
    """The check's alerts at `level` not emailed in the last RESEND_DAYS days."""
    wanted = _LEVELS[level]
    since = now - timedelta(days=RESEND_DAYS)
    return [
        a
        for a in check.alerts
        if a.severity in wanted and (a.alert_id not in sent or sent[a.alert_id] < since)
    ]


def _clean(text: str, limit: int) -> str:
    """One line, no control characters (a project name can never break a header)."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", text).strip()[:limit]


def compose(
    project_name: str,
    check: MonitorCheck,
    alerts: list[Alert],
    *,
    confidential: bool,
    link: str,
) -> tuple[str, str]:
    """The digest's subject and plain-text body."""
    dummy = check.signals is not None and check.signals.dummy
    high = sum(1 for a in alerts if a.severity == "high")
    subject = _clean(
        ("[dummy data] " if dummy else "")
        + f"Monitoring: {len(alerts)} new alert(s), {high} high, in {project_name}",
        200,
    )
    when = check.created_at.strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"Monitoring check of {_clean(project_name, 120)} on {when} ({check.trigger}).",
        f"{len(alerts)} new alert(s) at the level this project asked for.",
        "",
    ]
    if confidential:
        kinds = Counter((a.severity, a.kind.replace("_", " ")) for a in alerts)
        lines.append("This project is confidential: open the app for the details.")
        lines += [f"- {sev}: {kind} ({n})" for (sev, kind), n in sorted(kinds.items())]
    else:
        for severity in ("high", "medium"):
            group = [a for a in alerts if a.severity == severity]
            if not group:
                continue
            lines.append(severity.upper())
            for alert in group[:_MAX_ALERTS]:
                lines.append(f"- {_clean(alert.title, 200)}")
                lines.append(f"  {_clean(alert.detail, 400)}")
                lines += [f"  {u}" for u in alert.urls[:3]]
            if len(group) > _MAX_ALERTS:
                lines.append(f"  ... and {len(group) - _MAX_ALERTS} more")
            lines.append("")
    lines += ["", f"Open the Monitoring tab: {link}"]
    return subject, "\n".join(lines) + "\n"


def send_digest(
    check: MonitorCheck,
    level: DigestLevel,
    book: DigestBook,
    notifier: Notifier,
    *,
    project_name: str,
    confidential: bool,
    now: datetime | None = None,
) -> tuple[DigestBook, str]:
    """Send the due alerts to every address; returns the updated book and a note."""
    moment = now or datetime.now(UTC)
    if level == "off" or not book.to:
        return book, ""
    alerts = due_alerts(check, level, book.sent, moment)
    if not alerts:
        return book, ""
    link = f"{notifier.base_url.rstrip('/')}/projects/{check.project_id}?tab=monitoring"
    subject, body = compose(project_name, check, alerts, confidential=confidential, link=link)
    notes = [
        notifier.send(to, subject, body, tag=f"monitoring-{check.project_id}-{n}")
        for n, to in enumerate(book.to)
    ]
    sent = {**book.sent, **{a.alert_id: moment for a in alerts}}
    if len(sent) > _KEEP_SENT:
        sent = dict(sorted(sent.items(), key=lambda kv: kv[1])[-_KEEP_SENT:])
    note = f"{len(alerts)} alert(s): " + "; ".join(dict.fromkeys(notes))
    return book.model_copy(update={"sent": sent}), note[:500]
