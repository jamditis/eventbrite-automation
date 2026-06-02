import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from digest.airtable_client import AirtableClient, EventRow, EventRowSchemaError

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "airtable_event_row.json").read_text()
)


class _MockTable:
    def __init__(self, records):
        self.records = records
        self.update_calls = []

    def all(self, formula=None, **kw):
        if formula and "Enabled" in formula:
            return [r for r in self.records if r["fields"].get("Enabled")]
        return self.records

    def update(self, record_id, fields):
        self.update_calls.append({"id": record_id, "fields": fields})
        for r in self.records:
            if r["id"] == record_id:
                r["fields"].update(fields)
                return r
        raise KeyError(record_id)


@pytest.fixture
def mock_pyairtable(monkeypatch):
    """Fresh deep-copied fixture per test so speaker-emails mutation can't leak."""
    table = _MockTable([copy.deepcopy(FIXTURE)])

    class FakeApi:
        def __init__(self, *a, **kw):
            pass

        def table(self, base_id, table_name):
            return table

    monkeypatch.setattr("digest.airtable_client.Api", FakeApi)
    return table


def test_list_active_includes_disabled_with_pending_initial_briefing(mock_pyairtable):
    """A disabled row with a pending initial briefing must surface to the cron
    so the staff-requested briefing still fires. The original list_enabled()
    skips it, leaving the request silently abandoned."""

    class _FormulaTrackingTable:
        def __init__(self):
            self.last_formula = None
            self.records = mock_pyairtable.records

        def all(self, formula=None, **kw):
            self.last_formula = formula
            return self.records

        def update(self, *a, **kw):
            return mock_pyairtable.update(*a, **kw)

    tracker = _FormulaTrackingTable()
    mock_pyairtable.records[0]["fields"]["Enabled"] = False
    mock_pyairtable.records[0]["fields"]["Initial briefing requested at"] = "2026-05-08T11:00:00+00:00"
    mock_pyairtable.records[0]["fields"]["Initial briefing sent at"] = None

    import digest.airtable_client as ac

    class _FakeApi:
        def __init__(self, *a, **kw):
            pass

        def table(self, base_id, table_name):
            return tracker

    original_api = ac.Api
    ac.Api = _FakeApi
    try:
        client = ac.AirtableClient(pat="pat", base_id="base", table_name="Events")
        rows = client.list_active()
        assert tracker.last_formula is not None
        assert "Enabled" in tracker.last_formula
        assert "Initial briefing requested at" in tracker.last_formula
        assert "Initial briefing sent at" in tracker.last_formula
        assert len(rows) == 1
        assert rows[0].enabled is False
        assert rows[0].initial_briefing_requested_at == "2026-05-08T11:00:00+00:00"
    finally:
        ac.Api = original_api


def test_list_enabled_returns_event_rows(mock_pyairtable):
    client = AirtableClient(pat="pat", base_id="base", table_name="Events")
    rows = client.list_enabled()
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, EventRow)
    assert row.slug == "ai-newsroom-march-2026"
    assert row.eventbrite_event_id == "EVT-12345"
    assert row.enabled is True
    assert row.speaker_emails == ["panelist1@example.com", "panelist2@example.com"]
    assert row.days_out_to_start == 7
    assert row.send_time_et == "07:00"


def test_sheet_url_read_from_field(mock_pyairtable):
    """The per-event 'Attendee sheet URL' drives the briefing's 'view full
    sheet' button. cron reads it from Airtable rather than generating a sheet
    in the hot path, so the URL must round-trip onto the row."""
    mock_pyairtable.records[0]["fields"]["Attendee sheet URL"] = (
        "https://docs.google.com/spreadsheets/d/ABC/edit"
    )
    client = AirtableClient(pat="pat", base_id="base", table_name="Events")
    row = client.list_enabled()[0]
    assert row.sheet_url == "https://docs.google.com/spreadsheets/d/ABC/edit"


def test_sheet_url_defaults_empty_when_absent(mock_pyairtable):
    """An event with no generated sheet yet has no field — sheet_url is empty,
    and cron maps that to None so the button is simply omitted."""
    mock_pyairtable.records[0]["fields"].pop("Attendee sheet URL", None)
    client = AirtableClient(pat="pat", base_id="base", table_name="Events")
    row = client.list_enabled()[0]
    assert row.sheet_url == ""


def test_speaker_emails_parses_comma_or_newline_separated(mock_pyairtable):
    mock_pyairtable.records[0]["fields"]["Speaker emails"] = "a@x.com\nb@x.com\n c@x.com "
    client = AirtableClient(pat="pat", base_id="base", table_name="Events")
    row = client.list_enabled()[0]
    assert row.speaker_emails == ["a@x.com", "b@x.com", "c@x.com"]


def test_speaker_emails_handles_empty_string(mock_pyairtable):
    mock_pyairtable.records[0]["fields"]["Speaker emails"] = ""
    client = AirtableClient(pat="pat", base_id="base", table_name="Events")
    row = client.list_enabled()[0]
    assert row.speaker_emails == []


def test_update_after_send_writes_system_fields(mock_pyairtable):
    client = AirtableClient(pat="pat", base_id="base", table_name="Events")
    rows = client.list_enabled()
    client.update_after_send(
        rows[0],
        sent_at=datetime(2026, 5, 8, 11, 0, tzinfo=UTC),
        attendee_cursor="2026-05-07T20:00:00Z",
        attendee_count=47,
    )
    assert len(mock_pyairtable.update_calls) == 1
    fields = mock_pyairtable.update_calls[0]["fields"]
    assert fields["Last digest sent at"] == "2026-05-08T11:00:00+00:00"
    assert fields["Last attendee cursor"] == "2026-05-07T20:00:00Z"
    assert fields["Last digest attendee count"] == 47
    assert fields["Last error"] == ""


def test_record_error_writes_to_last_error_field(mock_pyairtable):
    client = AirtableClient(pat="pat", base_id="base", table_name="Events")
    rows = client.list_enabled()
    client.record_error(rows[0], "EB API timeout")
    assert mock_pyairtable.update_calls[-1]["fields"] == {"Last error": "EB API timeout"}


def test_record_error_truncates_long_messages(mock_pyairtable):
    client = AirtableClient(pat="pat", base_id="base", table_name="Events")
    rows = client.list_enabled()
    long_msg = "x" * 2000
    client.record_error(rows[0], long_msg)
    written = mock_pyairtable.update_calls[-1]["fields"]["Last error"]
    assert len(written) == 1000


def test_update_after_initial_send_writes_all_six_fields_in_one_call(mock_pyairtable):
    """The initial-briefing path must be atomic: cursor + count + sent_at +
    initial_briefing_sent_at + clear-requested + clear-error in ONE Airtable
    call so a partial failure can't leave the row looking like a pending
    initial briefing while SMTP already went out.
    """
    client = AirtableClient(pat="pat", base_id="base", table_name="Events")
    rows = client.list_enabled()
    client.update_after_initial_send(
        rows[0],
        sent_at=datetime(2026, 5, 8, 11, 0, tzinfo=UTC),
        attendee_cursor="2026-05-07T20:00:00Z",
        attendee_count=12,
    )
    assert len(mock_pyairtable.update_calls) == 1
    fields = mock_pyairtable.update_calls[0]["fields"]
    assert fields["Last digest sent at"] == "2026-05-08T11:00:00+00:00"
    assert fields["Last attendee cursor"] == "2026-05-07T20:00:00Z"
    assert fields["Last digest attendee count"] == 12
    assert fields["Initial briefing sent at"] == "2026-05-08T11:00:00+00:00"
    assert fields["Initial briefing requested at"] is None
    assert fields["Last error"] == ""


def test_reconcile_after_send_sets_only_sent_at_not_cursor(mock_pyairtable):
    """#20: a duplicate-reconcile must NOT advance the attendee cursor or
    count. The email it reconciles left SMTP on a PRIOR tick against an OLDER
    cursor; writing this tick's cursor would mark gap attendees (registered
    between the real send and now) as covered though they never appeared in
    the email. Set only last_digest_sent_at + clear last_error, so the next
    genuine tick re-evaluates new attendees against the unchanged cursor.
    """
    client = AirtableClient(pat="pat", base_id="base", table_name="Events")
    rows = client.list_enabled()
    client.reconcile_after_send(rows[0], sent_at=datetime(2026, 5, 8, 11, 0, tzinfo=UTC))
    assert len(mock_pyairtable.update_calls) == 1
    fields = mock_pyairtable.update_calls[0]["fields"]
    assert fields["Last digest sent at"] == "2026-05-08T11:00:00+00:00"
    assert fields["Last error"] == ""
    # The cursor + count fields must NOT be written — that's the whole point.
    assert "Last attendee cursor" not in fields
    assert "Last digest attendee count" not in fields


def test_reconcile_after_initial_send_marks_sent_without_advancing_cursor(mock_pyairtable):
    """#20 on the initial-briefing path: the reconcile sets
    initial-briefing-sent-at (+ clears the request) so the briefing can't
    re-fire, and sets last_digest_sent_at, but leaves the attendee
    cursor/count untouched so the first genuine daily digest still covers
    attendees who registered after the already-sent initial briefing.
    """
    client = AirtableClient(pat="pat", base_id="base", table_name="Events")
    rows = client.list_enabled()
    client.reconcile_after_initial_send(rows[0], sent_at=datetime(2026, 5, 8, 11, 0, tzinfo=UTC))
    assert len(mock_pyairtable.update_calls) == 1
    fields = mock_pyairtable.update_calls[0]["fields"]
    assert fields["Last digest sent at"] == "2026-05-08T11:00:00+00:00"
    assert fields["Initial briefing sent at"] == "2026-05-08T11:00:00+00:00"
    assert fields["Initial briefing requested at"] is None
    assert fields["Last error"] == ""
    assert "Last attendee cursor" not in fields
    assert "Last digest attendee count" not in fields


def test_mark_initial_briefing_sent(mock_pyairtable):
    client = AirtableClient(pat="pat", base_id="base", table_name="Events")
    rows = client.list_enabled()
    client.mark_initial_briefing_sent(
        rows[0], at=datetime(2026, 5, 8, 11, 0, tzinfo=UTC)
    )
    fields = mock_pyairtable.update_calls[-1]["fields"]
    assert fields["Initial briefing sent at"] == "2026-05-08T11:00:00+00:00"


def test_from_airtable_raises_schema_error_on_bad_int(mock_pyairtable):
    """Non-coercible 'Days out to start' must surface the record_id, not crash
    on a generic ValueError that hides which row to fix.
    """
    mock_pyairtable.records[0]["fields"]["Days out to start"] = "seven"
    client = AirtableClient(pat="pat", base_id="base", table_name="Events")
    with pytest.raises(EventRowSchemaError, match=r"recABC123.*Days out to start"):
        client.list_enabled()


def test_from_airtable_handles_none_for_optional_int(mock_pyairtable):
    """None / empty string falls back to the default rather than raising."""
    mock_pyairtable.records[0]["fields"]["Last digest attendee count"] = None
    client = AirtableClient(pat="pat", base_id="base", table_name="Events")
    row = client.list_enabled()[0]
    assert row.last_digest_attendee_count == 0


def test_clear_initial_briefing_request(mock_pyairtable):
    client = AirtableClient(pat="pat", base_id="base", table_name="Events")
    rows = client.list_enabled()
    client.clear_initial_briefing_request(rows[0])
    fields = mock_pyairtable.update_calls[-1]["fields"]
    assert fields["Initial briefing requested at"] is None
