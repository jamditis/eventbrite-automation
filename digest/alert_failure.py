"""Email alert fired by systemd when digest-cron fails.

Run by deploy/digest-failure-alert.service via OnFailure= on the cron unit,
so a non-zero exit (config failure, Airtable outage, or any per-event
failure) produces an email to staff instead of sitting silently in a failed
systemd state that nobody looks at.

Run as: `python -m digest.alert_failure`

Deliberately minimal and self-contained: it reads ONLY the SMTP + recipient
env vars directly rather than going through load_config() — the cron's most
likely run-level failure is a missing/invalid credential, and load_config()
raising on (say) a blank EVENTBRITE_PRIVATE_TOKEN would kill the alert for
exactly the failure it exists to report. It also never touches the email
ledger, Airtable, or Eventbrite: the alert path must not depend on the
things whose failure it reports.

Recipients default to BCC_ALWAYS (the standing staff list); override with
ALERT_RECIPIENTS in .env.digest.
"""
from __future__ import annotations

import logging
import os
import smtplib
import socket
import subprocess
import sys
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

# systemd provides the env via EnvironmentFile=; the dotenv load covers
# manual runs from the repo root. Same file + defaults as digest/config.py.
load_dotenv(Path(__file__).resolve().parent.parent / ".env.digest")

logger = logging.getLogger("digest.alert")

DEFAULT_BCC = "jamditis@gmail.com,etiennec@montclair.edu,advinculaa@montclair.edu"


def _split_addresses(raw: str) -> list[str]:
    return [a.strip() for a in raw.split(",") if a.strip()]


def _recent_journal(unit: str = "digest-cron.service", lines: int = 50) -> str:
    """Last journald lines for the failed unit, so the alert email is
    actionable without SSHing in first. Best-effort — journalctl may be
    unavailable (dev machines) or need permissions."""
    try:
        out = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return out.stdout.strip() or "(journalctl returned no output)"
    except Exception as e:
        return f"(could not read journal: {type(e).__name__}: {e})"


def send_failure_alert() -> int:
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    from_name = os.environ.get("SMTP_FROM_NAME", "Center for Cooperative Media")
    from_email = os.environ.get("SMTP_FROM_EMAIL", "") or smtp_user
    try:
        smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    except ValueError:
        smtp_port = 465

    if not smtp_user or not smtp_password:
        logger.error("SMTP_USER/SMTP_PASSWORD missing; cannot send failure alert")
        return 1

    recipients = _split_addresses(os.environ.get("ALERT_RECIPIENTS", "")) or _split_addresses(
        os.environ.get("BCC_ALWAYS", DEFAULT_BCC)
    )
    if not recipients:
        logger.error("no alert recipients configured (ALERT_RECIPIENTS/BCC_ALWAYS empty)")
        return 1

    host = socket.gethostname()
    now = datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M %Z")

    msg = EmailMessage()
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = f"[ALERT] digest-cron failed on {host}"
    msg.set_content(
        f"The attendee digest cron failed on {host} at {now}.\n"
        "\n"
        "Today's digests may not have gone out. Check the runbook:\n"
        "docs/operations/digest-runbook.md\n"
        "\n"
        "Quick triage:\n"
        f"  ssh {host}\n"
        "  systemctl status digest-cron.service\n"
        "  journalctl -u digest-cron.service -n 200 --no-pager\n"
        "\n"
        "Recent journal output:\n"
        "----------------------------------------\n"
        f"{_recent_journal()}\n"
    )

    with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=60) as smtp:
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(msg)

    logger.info("failure alert sent to %s", ", ".join(recipients))
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    sys.exit(send_failure_alert())
