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
