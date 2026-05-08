"""SMTP send + email_ledger integration.

Two-line dup defense:
  1. Cron orchestrator's own per-event state (last_digest_sent_at on the
     Airtable row) prevents re-sending a digest for the same day.
  2. THIS module's email_ledger check is the cross-session safety net
     (`~/.claude/workstation/sent-emails.db`) — if a wake fires twice or a
     scheduler hiccup duplicates the call, the ledger blocks the second send.

Bcc handling: addresses listed in `bcc_always` are set on the EmailMessage's
Bcc header. smtplib.send_message() reads the Bcc header to compute envelope
recipients, then strips that header before transmission — so the rendered
email body the recipients actually see does NOT contain a "Bcc:" line.
However, the visible "To" header IS present in the transmitted message and
visible to ALL recipients (Bcc included). If per-recipient isolation
matters, the orchestrator must send separate messages.
"""
from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

logger = logging.getLogger(__name__)


class LedgerProtocol(Protocol):
    def check_duplicate(
        self, recipient: str, subject: str, hours: int = 6
    ) -> dict | None: ...

    def log_send(
        self,
        *,
        recipient: str,
        subject: str,
        sender: str = "",
        body_hash: str | None = None,
        thread_id: str | None = None,
        context: str = "",
        session_type: str = "unknown",
        gmail_msg_id: str | None = None,
    ) -> int: ...


@dataclass(frozen=True)
class SendResult:
    sent: bool
    reason: str = ""


class SendEngine:
    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_password: str,
        from_name: str,
        from_email: str,
        bcc_always: tuple[str, ...],
        ledger: LedgerProtocol,
    ) -> None:
        self._host = smtp_host
        self._port = smtp_port
        self._user = smtp_user
        self._password = smtp_password
        self._from = f"{from_name} <{from_email}>"
        self._from_email = from_email
        self._bcc = bcc_always
        self._ledger = ledger

    def send(
        self,
        *,
        to: list[str],
        reply_to: str,
        subject: str,
        html_body: str,
        text_body: str,
        slug: str,
        session_type: str = "cron",
    ) -> SendResult:
        dup = self._ledger.check_duplicate(recipient=reply_to, subject=subject, hours=20)
        if dup:
            logger.warning(
                "ledger says duplicate for reply_to=%s subject=%r; aborting", reply_to, subject
            )
            return SendResult(sent=False, reason="duplicate")

        msg = EmailMessage()
        msg["From"] = self._from
        msg["To"] = ", ".join(to)
        msg["Reply-To"] = reply_to
        msg["Subject"] = subject
        if self._bcc:
            msg["Bcc"] = ", ".join(self._bcc)
        msg["List-Unsubscribe"] = (
            f"<mailto:{self._from_email}?subject=unsubscribe%20{slug}>"
        )
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")

        with smtplib.SMTP_SSL(self._host, self._port) as smtp:
            smtp.login(self._user, self._password)
            smtp.send_message(msg)

        self._ledger.log_send(
            recipient=reply_to,
            subject=subject,
            sender=self._from_email,
            context=f"event-digest:{slug}",
            session_type=session_type,
        )
        return SendResult(sent=True)
