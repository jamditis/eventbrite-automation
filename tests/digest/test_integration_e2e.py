"""End-to-end integration tests: drive `_run_briefing` with mocked external
services and assert the right things happen at the seams (right SMTP envelope,
right Airtable state writes, no send when silent-when-empty).

Mocks (everything outside this repo):
  - EventbriteClient: returns canned attendee + event payloads
  - CrmLookup: returns None (nobody known)
  - LLMRunner: returns None (no LLM enrichment, pure deterministic blurb)
  - SendEngine: captures send() calls
  - AirtableClient: captures state-update calls
"""
from datetime import UTC, datetime
from unittest.mock import MagicMock

from digest.airtable_client import EventRow
from digest.cron import _run_briefing
from digest.email_renderer import EmailRenderer
from digest.eventbrite_client import EventbriteAttendee, EventbriteEvent
from digest.send_engine import SendResult


def _row(**overrides) -> EventRow:
    base = dict(
        record_id="rec1",
        slug="test-event",
        title="Test event",
        eventbrite_event_id="EVT-1",
        enabled=True,
        speaker_emails=["panelist@example.com"],
        lead_host_email="host@example.com",
        days_out_to_start=7,
        send_time_et="07:00",
        question_ids_to_include=[],
        event_start_et="2026-05-15T13:00:00.000Z",
        last_digest_sent_at=None,
        last_attendee_cursor=None,
        last_digest_attendee_count=0,
        initial_briefing_sent_at=None,
        initial_briefing_requested_at=None,
        last_error="",
    )
    base.update(overrides)
    return EventRow(**base)


def _attendee(id_, email, name, created):
    first, _, last = name.partition(" ")
    return EventbriteAttendee(
        id=id_,
        created=created,
        status="Attending",
        cancelled=False,
        refunded=False,
        first_name=first,
        last_name=last,
        email=email,
        name=name,
        answers=[],
    )


def test_e2e_initial_briefing_sends_to_speakers_with_all_attendees():
    eb = MagicMock()
    eb.fetch_attendees.return_value = [
        _attendee("100001", "sarah@example.com", "Sarah Smith", "2026-05-01T10:00:00Z"),
        _attendee("100002", "marcus@example.com", "Marcus Chen", "2026-05-02T10:00:00Z"),
    ]
    eb.fetch_event.return_value = EventbriteEvent(
        id="EVT-1",
        title="Test event",
        start_local="2026-05-15T13:00:00",
        timezone="America/New_York",
    )

    crm = MagicMock()
    crm.find_by_email.return_value = None

    llm = MagicMock()
    llm.run_blurb.return_value = None

    sender = MagicMock()
    sender.send.return_value = SendResult(sent=True)

    airtable = MagicMock()
    renderer = EmailRenderer()
    now = datetime(2026, 5, 8, 12, 0, tzinfo=UTC)

    _run_briefing(
        _row(),
        eb, crm, llm, renderer, sender, airtable, now,
        is_initial=True, dry_run=False,
    )

    sender.send.assert_called_once()
    kw = sender.send.call_args.kwargs
    assert kw["to"] == ["panelist@example.com"]
    assert kw["reply_to"] == "host@example.com"
    assert "Test event" in kw["subject"]
    assert "initial attendee briefing" in kw["subject"]
    assert "Sarah Smith" in kw["html_body"]
    assert "Marcus Chen" in kw["html_body"]
    assert kw["slug"] == "test-event"

    airtable.update_after_initial_send.assert_called_once()
    airtable.update_after_send.assert_not_called()
    airtable.mark_initial_briefing_sent.assert_not_called()
    airtable.clear_initial_briefing_request.assert_not_called()


def test_e2e_daily_digest_silent_when_no_new_attendees():
    eb = MagicMock()
    eb.fetch_attendees.return_value = [
        _attendee("100001", "sarah@example.com", "Sarah Smith", "2026-05-01T10:00:00Z"),
    ]
    eb.fetch_event.return_value = EventbriteEvent(
        id="EVT-1", title="x",
        start_local="2026-05-15T13:00:00", timezone="America/New_York",
    )

    crm = MagicMock()
    crm.find_by_email.return_value = None
    llm = MagicMock()
    llm.run_blurb.return_value = None
    sender = MagicMock()
    airtable = MagicMock()
    renderer = EmailRenderer()
    now = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)

    row = _row(
        last_attendee_cursor="2026-05-10T00:00:00Z",
        initial_briefing_sent_at="2026-05-08T11:00:00+00:00",
    )

    _run_briefing(
        row, eb, crm, llm, renderer, sender, airtable, now,
        is_initial=False, dry_run=False,
    )

    sender.send.assert_not_called()
    airtable.update_after_send.assert_not_called()


def test_e2e_daily_digest_with_new_attendees_sends_only_new_in_new_section():
    """Cursor-based diff: attendees created after cursor render in the 'new
    registrants' section with full Q&A; attendees on/before cursor render in
    the 'already registered' section as name+org only.
    """
    eb = MagicMock()
    eb.fetch_attendees.return_value = [
        _attendee("100001", "old1@x.com", "Old One", "2026-05-05T10:00:00Z"),
        _attendee("100002", "old2@x.com", "Old Two", "2026-05-06T10:00:00Z"),
        _attendee("100003", "new1@x.com", "New One", "2026-05-12T10:00:00Z"),
        _attendee("100004", "new2@x.com", "New Two", "2026-05-13T10:00:00Z"),
    ]
    eb.fetch_event.return_value = EventbriteEvent(
        id="EVT-1", title="x",
        start_local="2026-05-15T13:00:00", timezone="America/New_York",
    )

    crm = MagicMock()
    crm.find_by_email.return_value = None
    llm = MagicMock()
    llm.run_blurb.return_value = None
    sender = MagicMock()
    sender.send.return_value = SendResult(sent=True)
    airtable = MagicMock()
    renderer = EmailRenderer()
    now = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)

    row = _row(
        last_attendee_cursor="2026-05-10T00:00:00Z",
        initial_briefing_sent_at="2026-05-08T11:00:00+00:00",
    )

    _run_briefing(
        row, eb, crm, llm, renderer, sender, airtable, now,
        is_initial=False, dry_run=False,
    )

    sender.send.assert_called_once()
    body = sender.send.call_args.kwargs["html_body"]

    # Section-bounded assertion: new attendees ONLY appear in the
    # "New registrants" section; old attendees ONLY in "Already registered".
    new_section_start = body.index("New registrants")
    existing_section_start = body.index("Already registered")
    new_section = body[new_section_start:existing_section_start]
    existing_section = body[existing_section_start:]

    assert "New One" in new_section and "New One" not in existing_section
    assert "New Two" in new_section and "New Two" not in existing_section
    assert "Old One" in existing_section and "Old One" not in new_section
    assert "Old Two" in existing_section and "Old Two" not in new_section

    # The cursor advances past the latest attendee
    update_kwargs = airtable.update_after_send.call_args.kwargs
    assert update_kwargs["attendee_cursor"] == "2026-05-13T10:00:00Z"
    assert update_kwargs["attendee_count"] == 4


def test_e2e_dry_run_renders_but_skips_send_and_state_write():
    eb = MagicMock()
    eb.fetch_attendees.return_value = [
        _attendee("100001", "a@x.com", "Alice Anon", "2026-05-01T10:00:00Z"),
    ]
    eb.fetch_event.return_value = EventbriteEvent(
        id="EVT-1", title="x",
        start_local="2026-05-15T13:00:00", timezone="America/New_York",
    )

    crm = MagicMock()
    crm.find_by_email.return_value = None
    llm = MagicMock()
    llm.run_blurb.return_value = None
    sender = MagicMock()
    airtable = MagicMock()
    renderer = EmailRenderer()
    now = datetime(2026, 5, 8, 12, 0, tzinfo=UTC)

    _run_briefing(
        _row(),
        eb, crm, llm, renderer, sender, airtable, now,
        is_initial=True, dry_run=True,
    )

    sender.send.assert_not_called()
    airtable.update_after_send.assert_not_called()
    airtable.mark_initial_briefing_sent.assert_not_called()


def test_e2e_empty_speaker_emails_aborts_send_and_records_error():
    """A row with no speaker_emails would otherwise mail only the always-BCC
    addresses while marking Airtable as sent — a delivery + privacy bug.
    Refuse to send, record `Last error`, leave sent_at unset so a fix to the
    row can recover on the next tick."""
    eb = MagicMock()
    crm = MagicMock()
    llm = MagicMock()
    sender = MagicMock()
    airtable = MagicMock()
    renderer = EmailRenderer()
    now = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    row = _row(speaker_emails=[])

    _run_briefing(
        row, eb, crm, llm, renderer, sender, airtable, now,
        is_initial=True, dry_run=False,
    )

    sender.send.assert_not_called()
    airtable.update_after_send.assert_not_called()
    airtable.update_after_initial_send.assert_not_called()
    airtable.record_error.assert_called_once()
    err_msg = airtable.record_error.call_args.args[1]
    assert "speaker_emails" in err_msg.lower() or "speaker emails" in err_msg.lower()


def test_e2e_empty_lead_host_email_aborts_send_and_records_error():
    """Lead host email is the Reply-To and the ledger key. Empty value
    breaks both — refuse to send and record the error."""
    eb = MagicMock()
    crm = MagicMock()
    llm = MagicMock()
    sender = MagicMock()
    airtable = MagicMock()
    renderer = EmailRenderer()
    now = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    row = _row(lead_host_email="")

    _run_briefing(
        row, eb, crm, llm, renderer, sender, airtable, now,
        is_initial=True, dry_run=False,
    )

    sender.send.assert_not_called()
    airtable.record_error.assert_called_once()
    err_msg = airtable.record_error.call_args.args[1]
    assert "lead_host_email" in err_msg.lower() or "lead host" in err_msg.lower()


def test_e2e_ledger_duplicate_reconciles_airtable_state_for_daily():
    """If SMTP succeeded on a prior tick but the Airtable state write failed,
    the next tick hits a ledger duplicate. Treat the duplicate as authoritative
    proof the email left SMTP and reconcile Airtable — same state writes as a
    successful send. Without this, last_digest_sent_at + cursor stay stale
    until the 20h ledger window ages out, then the cron re-sends with the
    old cursor."""
    eb = MagicMock()
    eb.fetch_attendees.return_value = [
        _attendee("100001", "old@x.com", "Old One", "2026-05-05T10:00:00Z"),
        _attendee("100002", "new@x.com", "New One", "2026-05-12T10:00:00Z"),
    ]
    eb.fetch_event.return_value = EventbriteEvent(
        id="EVT-1", title="x",
        start_local="2026-05-15T13:00:00", timezone="America/New_York",
    )
    crm = MagicMock()
    crm.find_by_email.return_value = None
    llm = MagicMock()
    llm.run_blurb.return_value = None

    sender = MagicMock()
    sender.send.return_value = SendResult(sent=False, reason="duplicate")

    airtable = MagicMock()
    renderer = EmailRenderer()
    now = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    row = _row(
        last_attendee_cursor="2026-05-10T00:00:00Z",
        initial_briefing_sent_at="2026-05-08T11:00:00+00:00",
    )

    _run_briefing(
        row, eb, crm, llm, renderer, sender, airtable, now,
        is_initial=False, dry_run=False,
    )

    sender.send.assert_called_once()
    airtable.update_after_send.assert_called_once()
    kw = airtable.update_after_send.call_args.kwargs
    assert kw["sent_at"] == now
    assert kw["attendee_cursor"] == "2026-05-12T10:00:00Z"
    assert kw["attendee_count"] == 2


def test_e2e_ledger_duplicate_reconciles_airtable_state_for_initial():
    """Same reconciliation logic on the initial-briefing path."""
    eb = MagicMock()
    eb.fetch_attendees.return_value = [
        _attendee("100001", "a@x.com", "Alice", "2026-05-01T10:00:00Z"),
    ]
    eb.fetch_event.return_value = EventbriteEvent(
        id="EVT-1", title="x",
        start_local="2026-05-15T13:00:00", timezone="America/New_York",
    )
    crm = MagicMock()
    crm.find_by_email.return_value = None
    llm = MagicMock()
    llm.run_blurb.return_value = None

    sender = MagicMock()
    sender.send.return_value = SendResult(sent=False, reason="duplicate")

    airtable = MagicMock()
    renderer = EmailRenderer()
    now = datetime(2026, 5, 8, 12, 0, tzinfo=UTC)

    _run_briefing(
        _row(),
        eb, crm, llm, renderer, sender, airtable, now,
        is_initial=True, dry_run=False,
    )

    sender.send.assert_called_once()
    airtable.update_after_initial_send.assert_called_once()
    airtable.update_after_send.assert_not_called()
