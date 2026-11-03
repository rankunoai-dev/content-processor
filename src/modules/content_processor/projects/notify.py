"""Emails about the shared scrape queue: "your turn" and "your scrape finished".

With an SMTP server configured (`CP_SMTP_HOST`, `CP_SMTP_FROM`, and usually
`CP_SMTP_USER` / `CP_SMTP_PASSWORD`), messages are sent. Without one, each message
is written as an `.eml` file to `<app data>/outbox/`, so nothing is lost and the
app says where it went. A failed send never stops the queue.

The address a person gives is kept only in the run's own folder (`notify.json`);
what the app shows is a partly hidden form (`g***@rankuno.com`).
"""

from __future__ import annotations

import re
import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

from src.core.logger import get_logger

__all__ = ["Mailer", "Notifier", "check_email", "hide_email", "smtp_mailer"]

_logger = get_logger("content_processor.projects.notify")
_EMAIL = re.compile(r"^[^@\s<>,;\"']{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")

# (message) -> None; raises on failure.
Mailer = Callable[[EmailMessage], None]


def check_email(text: str) -> str:
    """The address trimmed, or `ValueError` with a plain reason."""
    value = text.strip()
    if len(value) > 254 or not _EMAIL.match(value):
        raise ValueError(f"'{value[:60]}' is not an email address.")
    return value


def hide_email(address: str) -> str:
    """'gaurav.doshi@rankuno.com' -> 'g***@rankuno.com'."""
    name, _, domain = address.partition("@")
    return f"{name[:1]}***@{domain}" if domain else ""


def smtp_mailer(
    host: str, port: int, *, user: str | None, password: str | None, starttls: bool
) -> Mailer:
    """A mailer that sends through one SMTP server (TLS verified)."""

    def send(message: EmailMessage) -> None:
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as smtp:
                if user and password:
                    smtp.login(user, password)
                smtp.send_message(message)
            return
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            if starttls:
                smtp.starttls(context=context)
            if user and password:
                smtp.login(user, password)
            smtp.send_message(message)

    return send


@dataclass
class Notifier:
    """Writes and delivers queue emails."""

    outbox: Path
    sender: str = "content-scraper@localhost"
    base_url: str = "http://127.0.0.1:8780"
    mailer: Mailer | None = None

    def send(self, to: str, subject: str, body: str, *, tag: str) -> str:
        """Send (or write to the outbox); returns a plain note of what happened."""
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        if self.mailer is not None:
            try:
                self.mailer(message)
            except (OSError, smtplib.SMTPException) as exc:
                _logger.warning("queue_email_failed", extra={"tag": tag, "error": str(exc)[:200]})
                path = self._write(message, tag)
                return f"the email could not be sent ({type(exc).__name__}); saved as {path.name}"
            return f"emailed {hide_email(to)}"
        path = self._write(message, tag)
        return f"email is not set up on this server; the message was saved as {path.name}"

    def _write(self, message: EmailMessage, tag: str) -> Path:
        self.outbox.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        path = self.outbox / f"{stamp}_{re.sub(r'[^A-Za-z0-9_-]+', '-', tag)[:80]}.eml"
        path.write_bytes(bytes(message))
        return path

    def link(self, project_id: str, site_id: str) -> str:
        """The app page of a website's scrapes."""
        return f"{self.base_url.rstrip('/')}/projects/{project_id}?site={site_id}&view=scrape"
