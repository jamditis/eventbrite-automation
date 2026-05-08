from unittest.mock import MagicMock

import pytest

from digest.send_engine import SendEngine, SendResult


@pytest.fixture
def smtp_mock(monkeypatch):
    """Stub smtplib.SMTP_SSL; capture init args, login, and send_message calls."""
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **kw):
            sent.append({"init_args": a, "init_kw": kw})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, pw):
            sent[-1]["login"] = (user, pw)

        def send_message(self, msg):
            sent[-1]["msg"] = msg

    monkeypatch.setattr("digest.send_engine.smtplib.SMTP_SSL", FakeSMTP)
    return sent


@pytest.fixture
def ledger_mock():
    m = MagicMock()
    m.check_duplicate.return_value = None
    return m


def _engine(ledger_mock, **overrides):
    base = dict(
        smtp_host="smtp.gmail.com",
        smtp_port=465,
        smtp_user="njnewscommons@gmail.com",
        smtp_password="app-pw",
        from_name="Center for Cooperative Media",
        from_email="njnewscommons@gmail.com",
        bcc_always=("joe@example.com", "cassandra@example.com"),
        ledger=ledger_mock,
    )
    base.update(overrides)
    return SendEngine(**base)


def test_send_calls_smtp_with_correct_envelope(smtp_mock, ledger_mock):
    engine = _engine(ledger_mock)
    result = engine.send(
        to=["panelist@example.com"],
        reply_to="host@example.com",
        subject="Test",
        html_body="<p>hi</p>",
        text_body="hi",
        slug="test-slug",
        session_type="cron",
    )
    assert isinstance(result, SendResult)
    assert result.sent is True
    msg = smtp_mock[0]["msg"]
    assert msg["To"] == "panelist@example.com"
    assert msg["Reply-To"] == "host@example.com"
    assert msg["Subject"] == "Test"
    assert msg["Bcc"] == "joe@example.com, cassandra@example.com"
    assert msg["From"] == "Center for Cooperative Media <njnewscommons@gmail.com>"


def test_send_logs_in_with_smtp_credentials(smtp_mock, ledger_mock):
    engine = _engine(ledger_mock)
    engine.send(
        to=["a@x.com"],
        reply_to="b@x.com",
        subject="x",
        html_body="x",
        text_body="x",
        slug="x",
    )
    assert smtp_mock[0]["login"] == ("njnewscommons@gmail.com", "app-pw")
    assert smtp_mock[0]["init_args"] == ("smtp.gmail.com", 465)


def test_send_attaches_html_alternative(smtp_mock, ledger_mock):
    engine = _engine(ledger_mock)
    engine.send(
        to=["a@x.com"],
        reply_to="b@x.com",
        subject="x",
        html_body="<p>HTML body</p>",
        text_body="Plain body",
        slug="x",
    )
    msg = smtp_mock[0]["msg"]
    assert msg.is_multipart()
    parts = list(msg.iter_parts())
    types = [p.get_content_type() for p in parts]
    assert "text/plain" in types
    assert "text/html" in types


def test_send_includes_list_unsubscribe_header(smtp_mock, ledger_mock):
    engine = _engine(ledger_mock)
    engine.send(
        to=["a@x.com"],
        reply_to="b@x.com",
        subject="x",
        html_body="x",
        text_body="x",
        slug="ai-newsroom-march-2026",
    )
    lu = smtp_mock[0]["msg"]["List-Unsubscribe"]
    assert "mailto:njnewscommons@gmail.com" in lu
    assert "ai-newsroom-march-2026" in lu


def test_send_omits_bcc_header_when_no_bcc_configured(smtp_mock, ledger_mock):
    engine = _engine(ledger_mock, bcc_always=())
    engine.send(
        to=["a@x.com"],
        reply_to="b@x.com",
        subject="x",
        html_body="x",
        text_body="x",
        slug="x",
    )
    assert smtp_mock[0]["msg"]["Bcc"] is None


def test_send_aborts_when_ledger_says_duplicate(smtp_mock, ledger_mock):
    ledger_mock.check_duplicate.return_value = {"sent_at": "2026-05-08T05:00", "id": 9}
    engine = _engine(ledger_mock)
    result = engine.send(
        to=["a@x.com"],
        reply_to="b@x.com",
        subject="x",
        html_body="x",
        text_body="x",
        slug="x",
    )
    assert result.sent is False
    assert result.reason == "duplicate"
    assert smtp_mock == []
    ledger_mock.log_send.assert_not_called()


def test_send_dup_check_keys_off_reply_to_not_to_list(smtp_mock, ledger_mock):
    """Ledger uses Reply-To (lead host) so multi-speaker To: lists don't bypass dup."""
    engine = _engine(ledger_mock)
    engine.send(
        to=["speaker1@x.com", "speaker2@x.com"],
        reply_to="lead-host@x.com",
        subject="Subject",
        html_body="x",
        text_body="x",
        slug="x",
    )
    args = ledger_mock.check_duplicate.call_args
    assert args.kwargs.get("recipient") == "lead-host@x.com" or args.args[0] == "lead-host@x.com"


def test_send_logs_to_ledger_after_successful_send(smtp_mock, ledger_mock):
    engine = _engine(ledger_mock)
    engine.send(
        to=["a@x.com"],
        reply_to="b@x.com",
        subject="x",
        html_body="x",
        text_body="x",
        slug="my-slug",
        session_type="cron",
    )
    ledger_mock.log_send.assert_called_once()
    kw = ledger_mock.log_send.call_args.kwargs
    assert kw["recipient"] == "b@x.com"
    assert kw["subject"] == "x"
    assert "my-slug" in kw["context"]
    assert kw["session_type"] == "cron"
