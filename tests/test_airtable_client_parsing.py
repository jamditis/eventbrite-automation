"""Tests for Airtable record parsing and log-field maintenance."""

from types import SimpleNamespace

from airtable_client import AirtableClient
from config import AIRTABLE_FIELDS


def _client_with_table(monkeypatch, table):
    client = AirtableClient.__new__(AirtableClient)  # skip __init__ (no network)
    client.table = table
    return client


def test_parse_record_minimal_fields():
    client = AirtableClient.__new__(AirtableClient)
    record = {
        "id": "rec1234567890",
        "fields": {
            AIRTABLE_FIELDS["title"]: "My event",
            AIRTABLE_FIELDS["start_datetime"]: "2026-08-01T18:00:00.000Z",
            AIRTABLE_FIELDS["status"]: "Todo",
        },
    }
    parsed = client._parse_record(record)
    assert parsed.record_id == "rec1234567890"
    assert parsed.title == "My event"
    assert parsed.status == "Todo"
    assert parsed.start_datetime is not None
    assert parsed.start_datetime.year == 2026
    # unset fields fall back to safe defaults
    assert parsed.event_type == "In-person"
    assert parsed.pricing == "Free"
    assert parsed.eventbrite_event_id is None


def test_parse_record_bad_datetime_does_not_crash():
    client = AirtableClient.__new__(AirtableClient)
    record = {
        "id": "rec1234567890",
        "fields": {
            AIRTABLE_FIELDS["title"]: "Bad date",
            AIRTABLE_FIELDS["start_datetime"]: "not-a-date",
        },
    }
    parsed = client._parse_record(record)
    assert parsed.start_datetime is None


def test_update_log_truncates_to_50_lines(monkeypatch):
    existing = "\n".join(f"[old] line {i}" for i in range(60))
    updates = []

    table = SimpleNamespace(
        get=lambda record_id: {"fields": {AIRTABLE_FIELDS["automation_log"]: existing}},
        update=lambda record_id, fields: updates.append(fields),
    )
    client = _client_with_table(monkeypatch, table)

    assert client.update_log("rec1234567890", "new entry") is True
    log_value = updates[0][AIRTABLE_FIELDS["automation_log"]]
    lines = log_value.split("\n")
    assert len(lines) == 50
    assert "new entry" in lines[-1]


def test_validation_warnings_flag_url_briefs():
    client = AirtableClient.__new__(AirtableClient)
    record = {
        "id": "rec1234567890",
        "fields": {
            AIRTABLE_FIELDS["title"]: "URL brief",
            AIRTABLE_FIELDS["brief_description"]: "https://example.com",
        },
    }
    parsed = client._parse_record(record)
    assert not parsed.has_valid_brief_description
    assert any("Brief description" in w for w in parsed.validation_warnings)
