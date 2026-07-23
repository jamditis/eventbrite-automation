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
        sheet_url="",
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
        send_weekdays=None,
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


class _StatefulAirtable:
    """An Airtable stand-in that actually THREADS cursor/sent-at state across
    ticks, instead of a bare MagicMock whose writes vanish. Lets a multi-tick
    test PROVE what a reconcile leaves behind (cursor preserved) rather than
    hand-feeding the next tick's row. MagicMock wrappers keep assert_called_*
    usable while the side effects mutate the tracked state.
    """

    def __init__(self, row):
        self.cursor = row.last_attendee_cursor
        self.count = row.last_digest_attendee_count
        self.last_sent_at = row.last_digest_sent_at
        self.update_after_send = MagicMock(side_effect=self._update_after_send)
        self.update_after_initial_send = MagicMock(side_effect=self._update_after_send)
        self.reconcile_after_send = MagicMock(side_effect=self._reconcile)
        self.reconcile_after_initial_send = MagicMock(side_effect=self._reconcile)
        self.record_error = MagicMock()

    def _update_after_send(self, row, *, sent_at, attendee_cursor, attendee_count):
        self.cursor, self.count, self.last_sent_at = attendee_cursor, attendee_count, sent_at

    def _reconcile(self, row, *, sent_at):
        self.last_sent_at = sent_at  # cursor + count deliberately left untouched

    def sent_at_iso(self):
        return self.last_sent_at.isoformat() if hasattr(self.last_sent_at, "isoformat") \
            else self.last_sent_at


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


def test_e2e_briefing_includes_sheet_button_from_row_sheet_url():
    """The Airtable 'Attendee sheet URL' must thread through cron into the
    rendered email's 'view full sheet' button; an empty field omits it. This
    pins the wiring so the button can't silently regress to always-off."""
    eb = MagicMock()
    eb.fetch_attendees.return_value = [
        _attendee("100001", "sarah@example.com", "Sarah Smith", "2026-05-01T10:00:00Z"),
    ]
    eb.fetch_event.return_value = EventbriteEvent(
        id="EVT-1", title="Test event",
        start_local="2026-05-15T13:00:00", timezone="America/New_York",
    )
    crm = MagicMock()
    crm.find_by_email.return_value = None
    llm = MagicMock()
    llm.run_blurb.return_value = None
    renderer = EmailRenderer()
    now = datetime(2026, 5, 8, 12, 0, tzinfo=UTC)

    sender = MagicMock()
    sender.send.return_value = SendResult(sent=True)
    _run_briefing(
        _row(sheet_url="https://docs.google.com/spreadsheets/d/XYZ/edit"),
        eb, crm, llm, renderer, sender, MagicMock(), now,
        is_initial=True, dry_run=False,
    )
    body = sender.send.call_args.kwargs["html_body"]
    assert "https://docs.google.com/spreadsheets/d/XYZ/edit" in body
    assert "View full attendee sheet" in body

    sender2 = MagicMock()
    sender2.send.return_value = SendResult(sent=True)
    _run_briefing(
        _row(sheet_url=""),
        eb, crm, llm, renderer, sender2, MagicMock(), now,
        is_initial=True, dry_run=False,
    )
    assert "View full attendee sheet" not in sender2.send.call_args.kwargs["html_body"]


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

    # New design: one at-a-glance table, every attendee present. New attendees
    # since the cursor are flagged with a "new" badge; existing ones are not.
    rows = body.split("<tr")

    def row_for(name: str) -> str:
        return next(r for r in rows if name in r)

    assert "New One" in body and "New Two" in body
    assert "Old One" in body and "Old Two" in body
    assert "&middot; new" in row_for("New One")
    assert "&middot; new" in row_for("New Two")
    assert "&middot; new" not in row_for("Old One")
    assert "&middot; new" not in row_for("Old Two")

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
    proof the email left SMTP and reconcile last_digest_sent_at so we don't
    re-send the same calendar day. But do NOT advance the attendee cursor:
    the duplicate email reflected a PRIOR, older fetch, so advancing to this
    tick's cursor would silently bury any attendee who registered in the gap
    (#20). The cursor advance is deferred to the next genuine send."""
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
    # Reconcile sent-at only; the cursor must NOT advance to this tick's fetch.
    airtable.update_after_send.assert_not_called()
    airtable.reconcile_after_send.assert_called_once()
    kw = airtable.reconcile_after_send.call_args.kwargs
    assert kw["sent_at"] == now


def test_e2e_ledger_duplicate_reconciles_airtable_state_for_initial():
    """Same reconciliation on the initial-briefing path: mark the briefing
    sent (so it can't re-fire) without advancing the attendee cursor, so the
    first genuine daily digest still covers attendees who registered after
    the already-sent initial briefing (#20)."""
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
    # Reconcile marks the briefing sent without advancing the cursor.
    airtable.reconcile_after_initial_send.assert_called_once()
    assert airtable.reconcile_after_initial_send.call_args.kwargs["sent_at"] == now
    airtable.update_after_initial_send.assert_not_called()
    airtable.update_after_send.assert_not_called()


def test_e2e_reconcile_does_not_lose_gap_attendees_on_next_genuine_tick():
    """#20 end-to-end repro of the silent-data-loss path.

    Sequence:
      * Tick A sent a daily digest (covering attendees up to 2026-05-11) but
        its Airtable write failed, so the row's cursor is still the pre-A value
        (2026-05-10). [modeled by the row's initial state below]
      * A "gap" attendee registers on 2026-05-12.
      * Tick B fires within the 20h ledger window -> SendResult duplicate ->
        reconcile. The bug: reconcile advanced the cursor to 2026-05-12,
        marking the gap attendee covered though tick A's email predates them.
      * Tick C (next genuine send) computes new_profiles against the cursor.

    With the fix, tick B does NOT advance the cursor, so tick C still surfaces
    the gap attendee in the "New registrants" section.
    """
    attendees = [
        _attendee("100001", "sent@x.com", "Already Sent", "2026-05-11T10:00:00Z"),
        _attendee("100002", "gap@x.com", "Gap Person", "2026-05-12T10:00:00Z"),
    ]
    eb = MagicMock()
    eb.fetch_attendees.return_value = attendees
    eb.fetch_event.return_value = EventbriteEvent(
        id="EVT-1", title="x",
        start_local="2026-05-15T13:00:00", timezone="America/New_York",
    )
    crm = MagicMock()
    crm.find_by_email.return_value = None
    llm = MagicMock()
    llm.run_blurb.return_value = None
    renderer = EmailRenderer()

    # Pre-A cursor: tick A's update_after_send never landed, so the cursor is
    # stale at 2026-05-10 — behind both "Already Sent" and the gap attendee.
    # The stateful stand-in threads what each tick actually writes into the
    # next, so the test proves (not assumes) the reconcile preserved state.
    row = _row(
        last_attendee_cursor="2026-05-10T00:00:00Z",
        last_digest_attendee_count=1,
        initial_briefing_sent_at="2026-05-08T11:00:00+00:00",
    )
    airtable = _StatefulAirtable(row)

    # --- Tick B: ledger duplicate -> reconcile, must NOT advance cursor ---
    sender_b = MagicMock()
    sender_b.send.return_value = SendResult(sent=False, reason="duplicate")
    _run_briefing(
        row, eb, crm, llm, renderer, sender_b, airtable,
        datetime(2026, 5, 13, 12, 0, tzinfo=UTC),
        is_initial=False, dry_run=False,
    )
    airtable.update_after_send.assert_not_called()
    airtable.reconcile_after_send.assert_called_once()
    # Load-bearing fact: the reconcile left the cursor exactly where it was.
    # (On the pre-fix code this is 2026-05-12 — the silent skip.)
    assert airtable.cursor == "2026-05-10T00:00:00Z"

    # --- Tick C: next genuine send, reading the REAL post-B state (cursor
    # threaded from tick B, not hand-fed). The gap attendee must appear. ---
    row_c = _row(
        last_attendee_cursor=airtable.cursor,
        last_digest_attendee_count=airtable.count,
        last_digest_sent_at=airtable.sent_at_iso(),
        initial_briefing_sent_at="2026-05-08T11:00:00+00:00",
    )
    sender_c = MagicMock()
    sender_c.send.return_value = SendResult(sent=True)
    _run_briefing(
        row_c, eb, crm, llm, renderer, sender_c, airtable,
        datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        is_initial=False, dry_run=False,
    )
    sender_c.send.assert_called_once()
    body = sender_c.send.call_args.kwargs["html_body"]
    # Gap Person registered after the prior (reconciled) send but before this
    # tick. Against the unchanged cursor (#20), they must still appear in this
    # digest rather than being silently marked covered — the deliberate
    # redundancy that trades re-showing prior attendees once for never
    # dropping a gap attendee.
    assert "Gap Person" in body, "gap attendee silently dropped from digest (#20)"
    # Tick C, a real send, advances the cursor past the gap attendee.
    airtable.update_after_send.assert_called_once()
    assert airtable.cursor == "2026-05-12T10:00:00Z"


def test_e2e_unexpected_non_sent_reason_is_surfaced_not_silently_dropped():
    """A SendResult(sent=False) whose reason is NOT 'duplicate' must not be a
    silent no-op. send_engine only returns 'duplicate' today (all real SMTP
    failures raise and are caught upstream), but a future reason string
    (e.g. 'throttled') should surface via Last error + a log warning instead
    of leaving the row in stale state with no trace and no cursor write."""
    eb = MagicMock()
    eb.fetch_attendees.return_value = [
        _attendee("100001", "a@x.com", "Alice Anon", "2026-05-12T10:00:00Z"),
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
    sender.send.return_value = SendResult(sent=False, reason="throttled")
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

    # Not sent and not a duplicate: write nothing to cursor/sent-at, but DO
    # record the anomaly so an unexpected result isn't swallowed.
    airtable.update_after_send.assert_not_called()
    airtable.reconcile_after_send.assert_not_called()
    airtable.record_error.assert_called_once()
    assert "throttled" in airtable.record_error.call_args.args[1]
