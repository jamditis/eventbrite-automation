"""Reliability contract for the digest cron added in the hardening pass:

- main() returns exit codes instead of letting setup failures escape as bare
  tracebacks (0 clean / 1 run-level failure / 2 per-event failures), so the
  systemd OnFailure alert actually fires.
- The post-send Airtable state write retries with backoff — that write is
  what prevents a duplicate send on the next tick.
- SMTP connections are bounded by a timeout so one hung connection can't
  starve the rest of the tick.
- The failure-alert email builds and sends from config without touching
  Airtable/Eventbrite.
"""

from unittest.mock import MagicMock

import pytest

import digest.alert_failure as alert_failure
import digest.cron as cron
import digest.send_engine as send_engine

# --- main() exit codes ------------------------------------------------------

def test_main_returns_1_on_setup_failure(monkeypatch):
    def boom(dry_run=False):
        raise RuntimeError("airtable unreachable")

    monkeypatch.setattr(cron, "_run_tick", boom)
    assert cron.main() == 1


def test_main_passes_through_tick_exit_code(monkeypatch):
    monkeypatch.setattr(cron, "_run_tick", lambda dry_run=False: 2)
    assert cron.main() == 2
    monkeypatch.setattr(cron, "_run_tick", lambda dry_run=False: 0)
    assert cron.main() == 0


# --- state-write retry ------------------------------------------------------

def test_retry_state_write_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(cron.time, "sleep", lambda s: None)
    calls = []

    def flaky(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) < 3:
            raise ConnectionError("airtable blip")
        return "ok"

    assert cron._retry_state_write(flaky, "row", sent_at="now") == "ok"
    assert len(calls) == 3


def test_retry_state_write_raises_after_exhaustion(monkeypatch):
    monkeypatch.setattr(cron.time, "sleep", lambda s: None)
    attempts = []

    def always_fails(*args, **kwargs):
        attempts.append(1)
        raise ConnectionError("airtable down")

    with pytest.raises(ConnectionError):
        cron._retry_state_write(always_fails, attempts=3)
    assert len(attempts) == 3


# --- SMTP timeout -----------------------------------------------------------

def test_smtp_connection_has_timeout(monkeypatch):
    captured = {}

    class _CapturingSMTP:
        def __init__(self, host, port, timeout=None):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, pw):
            pass

        def send_message(self, msg):
            pass

    monkeypatch.setattr(send_engine.smtplib, "SMTP_SSL", _CapturingSMTP)
    ledger = MagicMock()
    ledger.check_duplicate.return_value = None

    engine = send_engine.SendEngine(
        smtp_host="smtp.test",
        smtp_port=465,
        smtp_user="u",
        smtp_password="p",
        from_name="CCM",
        from_email="from@test",
        bcc_always=(),
        cc_always=(),
        ledger=ledger,
    )
    result = engine.send(
        to=["to@test"],
        reply_to="host@test",
        subject="s",
        html_body="<p>x</p>",
        text_body="x",
        slug="evt",
    )
    assert result.sent is True
    assert captured["timeout"] is not None and captured["timeout"] > 0


# --- failure alert ----------------------------------------------------------

def _alert_env(monkeypatch):
    """Minimal env the alert needs. Deliberately does NOT set the Eventbrite/
    Airtable/dashboard vars — the alert must work without them, since a
    missing credential is exactly the failure it reports."""
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    monkeypatch.setenv("SMTP_FROM_NAME", "CCM")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "from@test")
    monkeypatch.setenv("BCC_ALWAYS", "staff1@test,staff2@test")
    monkeypatch.delenv("ALERT_RECIPIENTS", raising=False)
    for var in ("EVENTBRITE_PRIVATE_TOKEN", "AIRTABLE_PAT", "AIRTABLE_BASE_ID", "DASHBOARD_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_failure_alert_sends_to_standing_recipients(monkeypatch):
    sent = {}

    class _CapturingSMTP:
        def __init__(self, host, port, timeout=None):
            sent["conn"] = (host, port, timeout)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, pw):
            sent["login"] = (user, pw)

        def send_message(self, msg):
            sent["msg"] = msg

    _alert_env(monkeypatch)
    monkeypatch.setattr(alert_failure.smtplib, "SMTP_SSL", _CapturingSMTP)
    monkeypatch.setattr(alert_failure, "_recent_journal", lambda **kw: "journal tail")

    assert alert_failure.send_failure_alert() == 0
    msg = sent["msg"]
    assert "staff1@test" in msg["To"] and "staff2@test" in msg["To"]
    assert "digest-cron failed" in msg["Subject"]
    assert "journal tail" in msg.get_content()
    assert sent["conn"][2] is not None, "alert SMTP must also be bounded"


def test_failure_alert_honors_recipient_override(monkeypatch):
    sent = {}

    class _CapturingSMTP:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, *a):
            pass

        def send_message(self, msg):
            sent["msg"] = msg

    _alert_env(monkeypatch)
    monkeypatch.setattr(alert_failure.smtplib, "SMTP_SSL", _CapturingSMTP)
    monkeypatch.setattr(alert_failure, "_recent_journal", lambda **kw: "")
    monkeypatch.setenv("ALERT_RECIPIENTS", "oncall@test")

    assert alert_failure.send_failure_alert() == 0
    assert sent["msg"]["To"] == "oncall@test"


def test_failure_alert_fails_cleanly_without_smtp_creds(monkeypatch):
    _alert_env(monkeypatch)
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    assert alert_failure.send_failure_alert() == 1


# --- recipient-validation failures fail the tick ----------------------------

def test_briefing_missing_recipients_returns_failure():
    """A row with no speaker emails is a per-event failure — it must count
    toward the tick's failed events so the OnFailure alert fires."""
    row = MagicMock()
    row.record_id = "recX"
    row.slug = "evt"
    row.speaker_emails = []
    row.lead_host_email = "host@test"
    airtable = MagicMock()

    ok = cron._run_briefing(
        row, MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(),
        airtable, None, is_initial=True, dry_run=False,
    )
    assert ok is False
    airtable.record_error.assert_called_once()
