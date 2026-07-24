"""Email alert fired by systemd when digest-cron fails.

Run by deploy/digest-failure-alert.service via OnFailure= on the cron unit,
so a non-zero exit (config failure, Airtable outage, or any per-event
failure) produces an email to staff instead of sitting silently in a failed
systemd state that nobody looks at.

Run as: `python -m digest.alert_failure`

Deliberately minimal and self-contained: it reuses the SMTP credentials from
.env.digest but does NOT touch the email ledger, Airtable, or Eventbrite —
the alert path must not depend on the things whose failure it reports.
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

from .config import load_config

logger = logging.getLogger("digest.alert")


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
    cfg = load_config()

    recipients_raw = os.environ.get("ALERT_RECIPIENTS", "")
    recipients = [a.strip() for a in recipients_raw.split(",") if a.strip()] or list(cfg.bcc_always)
    if not recipients:
        logger.error("no alert recipients configured (ALERT_RECIPIENTS/BCC_ALWAYS empty)")
        return 1

    host = socket.gethostname()
    now = datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M %Z")

    msg = EmailMessage()
    msg["From"] = f"{cfg.smtp_from_name} <{cfg.smtp_from_email}>"
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

    with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=60) as smtp:
        smtp.login(cfg.smtp_user, cfg.smtp_password)
        smtp.send_message(msg)

    logger.info("failure alert sent to %s", ", ".join(recipients))
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    sys.exit(send_failure_alert())
