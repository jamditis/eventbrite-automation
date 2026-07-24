"""Unit tests for the Eventbrite client's text-formatting helpers —
markdown conversion, internal-note stripping, and event-ID extraction.
These were previously untested despite being documented behavior.
"""

from datetime import UTC, datetime

from airtable_client import AirtableClient, EventRecord
from eventbrite_client import EventbriteClient


def _client():
    return EventbriteClient()


def test_markdown_bold_italic_links():
    c = _client()
    assert c._markdown_to_html("**bold**") == "<strong>bold</strong>"
    assert c._markdown_to_html("*italic*") == "<em>italic</em>"
    assert c._markdown_to_html("_italic_") == "<em>italic</em>"
    assert (
        c._markdown_to_html("[CCM](https://centerforcooperativemedia.org)")
        == '<a href="https://centerforcooperativemedia.org">CCM</a>'
    )


def test_markdown_bold_not_treated_as_italic():
    c = _client()
    out = c._markdown_to_html("**bold** and *italic*")
    assert out == "<strong>bold</strong> and <em>italic</em>"


def test_strip_internal_notes_removes_marked_lines():
    c = _client()
    text = (
        "Public intro line.\n"
        "Target audience: journalists [internal]\n"
        "The goal: drive signups\n"
        "Internal note: don't publish this\n"
        "Closing public line."
    )
    result = c._strip_internal_notes(text)
    assert "Public intro line." in result
    assert "Closing public line." in result
    assert "internal" not in result.lower()
    assert "goal" not in result.lower()


def test_strip_internal_notes_collapses_blank_runs():
    c = _client()
    text = "Line one.\nInternal note: hidden\n\n\n\nLine two."
    result = c._strip_internal_notes(text)
    assert "\n\n\n" not in result


def test_extract_event_id_from_url():
    assert (
        AirtableClient.extract_event_id_from_url(
            "https://www.eventbrite.com/e/my-event-tickets-1234567890"
        )
        == "1234567890"
    )
    assert AirtableClient.extract_event_id_from_url("") is None
    assert AirtableClient.extract_event_id_from_url("https://example.com/no-id") is None


def test_description_times_render_in_eastern():
    """Airtable hands back UTC; the description must show Eastern time."""
    c = _client()
    event = EventRecord(
        record_id="rec1",
        title="TZ test",
        brief_description="Short",
        full_description="Body",
        # 18:00 UTC on July 1 == 2:00 PM EDT
        start_datetime=datetime(2026, 7, 1, 18, 0, tzinfo=UTC),
        end_datetime=None,
        event_type="Virtual",
        pricing="Free",
    )
    html = c._build_description_html(event)
    assert "2:00 PM ET" in html
    assert "Wednesday, July 01, 2026" in html
