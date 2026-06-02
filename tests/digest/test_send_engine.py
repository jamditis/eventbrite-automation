import smtplib
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from digest.send_engine import SendEngine, SendResult


class _FakeSMTP(smtplib.SMTP):
    """Subclass of the real smtplib.SMTP so send_message runs real envelope-
    compute + Bcc-strip logic. We skip the network connect by overriding
    __init__ and capture both the pre-send EmailMessage and the post-strip
    sendmail() arguments so tests can verify both layers of the contract.
    """

    instances: ClassVar[list["_FakeSMTP"]] = []

    def __init__(self, host="", port=0, *args, **kwargs):
        self._host = host
        self._port = port
        self.timeout = 60
        self.esmtp_features = {}
        self.command_encoding = "ascii"
        self.source_address = None
        self.local_hostname = "test"
        self.does_esmtp = False
        self.captured: dict = {
            "init_args": (host, port),
            "sendmail_calls": [],
            "send_message_msgs": [],
        }
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo_or_helo_if_needed(self):
        return None

    def login(self, user, pw):
        self.captured["login"] = (user, pw)

    def send_message(self, msg, from_addr=None, to_addrs=None, *a, **kw):
        self.captured["send_message_msgs"].append(msg)
        return super().send_message(msg, from_addr, to_addrs, *a, **kw)

    def sendmail(self, from_addr, to_addrs, msg, mail_options=(), rcpt_options=()):
        msg_str = msg if isinstance(msg, str) else msg.decode("utf-8", errors="replace")
        self.captured["sendmail_calls"].append(
            {"from_addr": from_addr, "to_addrs": list(to_addrs), "msg_str": msg_str}
        )
        return {}


@pytest.fixture
def smtp_mock(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr("digest.send_engine.smtplib.SMTP_SSL", _FakeSMTP)
    return _FakeSMTP.instances


@pytest.fixture
def ledger_mock():
    m = MagicMock()
    m.check_duplicate.return_value = None
    return m


def _engine(ledger_mock, **overrides):
    base = dict(
        smtp_host="smtp.gmail.com",
        smtp_port=465,
        smtp_user="sender@example.com",
        smtp_password="app-pw",
        from_name="Center for Cooperative Media",
        from_email="sender@example.com",
        bcc_always=("joe@example.com", "cassandra@example.com"),
        cc_always=(),
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
    msg = smtp_mock[0].captured["send_message_msgs"][0]
    assert msg["To"] == "panelist@example.com"
    assert msg["Reply-To"] == "host@example.com"
    assert msg["Subject"] == "Test"
    assert msg["From"] == "Center for Cooperative Media <sender@example.com>"


def test_envelope_to_includes_bcc_addresses_and_msg_strips_bcc_header(
    smtp_mock, ledger_mock
):
    """Verifies the actual SMTP-level Bcc contract: Bcc addresses end up in
    the envelope-to (recipients receive the message) but the transmitted
    message bytes do NOT contain a Bcc: header (recipients can't see the
    Bcc list). Exercises the real smtplib.SMTP.send_message logic, not just
    the pre-send EmailMessage object.
    """
    engine = _engine(ledger_mock)
    engine.send(
        to=["panelist@example.com"],
        reply_to="host@example.com",
        subject="Test",
        html_body="<p>hi</p>",
        text_body="hi",
        slug="x",
    )
    sm = smtp_mock[0].captured["sendmail_calls"][0]
    assert "panelist@example.com" in sm["to_addrs"]
    assert "joe@example.com" in sm["to_addrs"]
    assert "cassandra@example.com" in sm["to_addrs"]
    assert "Bcc:" not in sm["msg_str"]
    assert "joe@example.com" not in sm["msg_str"]
    assert "cassandra@example.com" not in sm["msg_str"]


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
    assert smtp_mock[0].captured["login"] == ("sender@example.com", "app-pw")
    assert smtp_mock[0].captured["init_args"] == ("smtp.gmail.com", 465)


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
    msg = smtp_mock[0].captured["send_message_msgs"][0]
    assert msg.is_multipart()
    parts = list(msg.iter_parts())
    types = [p.get_content_type() for p in parts]
    assert "text/plain" in types
    assert "text/html" in types


def test_send_includes_well_formed_list_unsubscribe_header(smtp_mock, ledger_mock):
    """List-Unsubscribe must be a syntactically valid mailto: URI inside angle
    brackets per RFC 2369; substring presence isn't enough.
    """
    import re

    engine = _engine(ledger_mock)
    engine.send(
        to=["a@x.com"],
        reply_to="b@x.com",
        subject="x",
        html_body="x",
        text_body="x",
        slug="ai-newsroom-march-2026",
    )
    lu = smtp_mock[0].captured["send_message_msgs"][0]["List-Unsubscribe"]
    m = re.fullmatch(r"<mailto:([^?]+)\?subject=([^>]+)>", lu)
    assert m, f"List-Unsubscribe not in valid mailto-URI form: {lu!r}"
    assert m.group(1) == "sender@example.com"
    assert m.group(2) == "unsubscribe%20ai-newsroom-march-2026"


def test_cc_addresses_receive_and_remain_visible(smtp_mock, ledger_mock):
    """Cc is the inverse of Bcc: send_message keeps the Cc header in the
    transmitted bytes (every recipient sees who was copied) while the Cc address
    still lands in the envelope-to (it receives the message). Bcc from the same
    send stays hidden — proving the two coexist correctly in one message.
    """
    engine = _engine(ledger_mock, cc_always=("info@centerforcooperativemedia.org",))
    engine.send(
        to=["panelist@example.com"],
        reply_to="host@example.com",
        subject="Test",
        html_body="<p>hi</p>",
        text_body="hi",
        slug="x",
    )
    msg = smtp_mock[0].captured["send_message_msgs"][0]
    assert msg["Cc"] == "info@centerforcooperativemedia.org"
    sm = smtp_mock[0].captured["sendmail_calls"][0]
    # Cc recipient receives the message (in envelope-to)...
    assert "info@centerforcooperativemedia.org" in sm["to_addrs"]
    # ...and is visible in the transmitted bytes, unlike Bcc.
    assert "info@centerforcooperativemedia.org" in sm["msg_str"]
    # Bcc from the default engine stays hidden in the same message.
    assert "joe@example.com" in sm["to_addrs"]
    assert "joe@example.com" not in sm["msg_str"]


def test_send_omits_cc_header_when_no_cc_configured(smtp_mock, ledger_mock):
    engine = _engine(ledger_mock, cc_always=())
    engine.send(
        to=["a@x.com"],
        reply_to="b@x.com",
        subject="x",
        html_body="x",
        text_body="x",
        slug="x",
    )
    msg = smtp_mock[0].captured["send_message_msgs"][0]
    assert msg["Cc"] is None


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
    msg = smtp_mock[0].captured["send_message_msgs"][0]
    assert msg["Bcc"] is None
    sm = smtp_mock[0].captured["sendmail_calls"][0]
    assert sm["to_addrs"] == ["a@x.com"]


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
        slug="ai-newsroom",
    )
    args = ledger_mock.check_duplicate.call_args
    assert args.kwargs.get("recipient") == "lead-host@x.com" or args.args[0] == "lead-host@x.com"
    # Dedup is keyed on the stable event identity (slug) plus the email kind,
    # not the volatile subject. Default kind is "digest".
    assert args.kwargs.get("thread_id") == "ai-newsroom:digest"


def test_send_dedup_keyed_on_slug_not_subject(smtp_mock, ledger_mock):
    """Two distinct events that share a lead host AND an identical subject must
    not suppress each other: the dedup discriminator is the slug, so different
    slugs produce different ledger keys even with matching reply_to + subject.
    """
    engine = _engine(ledger_mock)
    common = dict(
        to=["a@x.com"],
        reply_to="shared-host@x.com",
        subject="Weekly roundup — 3 new registrations (10 total)",
        html_body="x",
        text_body="x",
    )
    engine.send(**common, slug="event-one")
    engine.send(**common, slug="event-two")
    thread_ids = [
        c.kwargs.get("thread_id") for c in ledger_mock.check_duplicate.call_args_list
    ]
    assert thread_ids == ["event-one:digest", "event-two:digest"]


def test_send_dedup_falls_back_to_subject_when_slug_blank(smtp_mock, ledger_mock):
    """A row with no slug can't key on identity; fall back to subject (thread_id
    None) rather than collapsing every slugless event onto an empty thread_id.
    """
    engine = _engine(ledger_mock)
    engine.send(
        to=["a@x.com"],
        reply_to="b@x.com",
        subject="Subject",
        html_body="x",
        text_body="x",
        slug="",
    )
    assert ledger_mock.check_duplicate.call_args.kwargs.get("thread_id") is None


class _StatefulFakeLedger:
    """In-memory stand-in that mirrors email_ledger's verified matching contract
    (houseofjawn-bot/scheduler/email_ledger.py): when thread_id is provided,
    check_duplicate matches on recipient + thread_id and IGNORES subject;
    otherwise it matches on recipient + subject. Recipient is lowercased on
    both store and query, as the real module does.

    A real-ledger import would couple the digest's CI to a sibling repo that
    isn't checked out there — the same absence the _NoopLedger fallback exists
    for. This faithful model proves the load-bearing claim the per-call mocks
    can't: that send() reads with the SAME key it later writes with, so a
    second send of one event is suppressed while a different event sharing the
    same reply_to and subject still goes out.

    Out of scope, deliberately: the real ledger also filters on a time window
    (`sent_at >= now - hours`). This model ignores `hours` because none of
    these tests advance the clock — don't reuse it for a window-expiry test
    without adding that filter.
    """

    def __init__(self):
        self.rows: list[dict] = []

    def check_duplicate(self, recipient, subject, thread_id=None, hours=6):
        recipient = recipient.lower()
        for row in reversed(self.rows):
            if row["recipient"] != recipient:
                continue
            if thread_id is not None:
                if row["thread_id"] == thread_id:
                    return row
            elif row["subject"] == subject:
                return row
        return None

    def log_send(self, *, recipient, subject, thread_id=None, **kw):
        self.rows.append(
            {"recipient": recipient.lower(), "subject": subject, "thread_id": thread_id}
        )
        return len(self.rows)


def test_send_dedup_round_trip_against_faithful_ledger(smtp_mock):
    """End-to-end: the read key must equal the write key. Same event sent twice
    is blocked; a different event sharing reply_to + subject still sends.
    """
    ledger = _StatefulFakeLedger()
    engine = _engine(ledger)
    shared = dict(
        reply_to="host@x.com",
        subject="Roundup — 3 new (10 total)",
        html_body="x",
        text_body="x",
    )

    first = engine.send(to=["a@x.com"], slug="event-one", **shared)
    second = engine.send(to=["a@x.com"], slug="event-one", **shared)
    other = engine.send(to=["a@x.com"], slug="event-two", **shared)

    assert first.sent is True
    assert second.sent is False and second.reason == "duplicate"
    assert other.sent is True  # same reply_to + subject, different slug -> not suppressed
    # SMTP fired for the two real sends, not the suppressed duplicate.
    assert len(smtp_mock) == 2


def test_initial_briefing_does_not_suppress_daily_digest_same_event(smtp_mock):
    """Regression: initial briefing and daily digest share a slug AND a
    lead-host reply_to. Keying dedup on the bare slug made a daily digest sent
    within the ledger window of its initial briefing look like a duplicate, so
    it never went out (and cron then reconciled Airtable as if it had). The
    kind suffix in the thread key keeps the two email types distinct.
    """
    ledger = _StatefulFakeLedger()
    engine = _engine(ledger)
    shared = dict(
        to=["a@x.com"],
        reply_to="host@x.com",
        subject="Same subject text",  # identical subject is the worst case
        html_body="x",
        text_body="x",
        slug="event-one",
    )

    initial = engine.send(**shared, kind="initial")
    daily = engine.send(**shared, kind="daily")

    assert initial.sent is True
    assert daily.sent is True  # different kind -> different thread key -> not suppressed
    # A genuine same-kind double-fire IS still caught.
    repeat = engine.send(**shared, kind="daily")
    assert repeat.sent is False and repeat.reason == "duplicate"
    assert len(smtp_mock) == 2


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
    assert kw["thread_id"] == "my-slug:digest"
    assert "my-slug" in kw["context"]
    assert kw["session_type"] == "cron"
