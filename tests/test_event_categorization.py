"""Regression tests: new drafts are created already categorized.

`create_draft_event` never set an Eventbrite format or category, so every draft
landed uncategorized and had to be sorted by hand. These tests pin the defaults
into the create payload and confirm a blanked config value is skipped.
"""

from datetime import datetime

import eventbrite_client as ebc
from eventbrite_client import EventbriteClient
from airtable_client import EventRecord
from config import EVENT_DEFAULTS

CCM_ORG = "66857244479"


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _virtual_event():
    return EventRecord(
        record_id="rec123",
        title="Test webinar",
        brief_description="A short valid summary.",
        full_description="Full body of the event.",
        start_datetime=datetime(2026, 7, 30, 14, 0),
        end_datetime=datetime(2026, 7, 30, 15, 0),
        event_type="Virtual",
        pricing="Free",
    )


def _client_with_captured_posts(monkeypatch):
    """A client whose org lookup is stubbed and whose POSTs are captured."""
    posts = []

    def fake_post(url, headers=None, json=None):
        posts.append((url, json))
        return _Resp(200, {"id": "999", "url": "https://eventbrite.com/e/999"})

    monkeypatch.setattr(ebc.requests, "post", fake_post)
    client = EventbriteClient()
    client._organization_id = CCM_ORG  # skip the org-resolution GET
    return client, posts


def _create_payload(posts):
    """The event body from the create-event POST (ends with /events/)."""
    for url, body in posts:
        if url.endswith("/events/"):
            return body["event"]
    raise AssertionError("no create-event POST was made")


def test_create_payload_sets_format_and_category(monkeypatch):
    """The create call carries the default format_id and category_id."""
    client, posts = _client_with_captured_posts(monkeypatch)
    client.create_draft_event(_virtual_event(), logo_id=None)

    event = _create_payload(posts)
    assert event["format_id"] == EVENT_DEFAULTS["format_id"]
    assert event["category_id"] == EVENT_DEFAULTS["category_id"]


def test_blank_default_is_skipped(monkeypatch):
    """A blanked config value is omitted rather than sent as an empty string."""
    monkeypatch.setitem(EVENT_DEFAULTS, "category_id", "")
    client, posts = _client_with_captured_posts(monkeypatch)
    client.create_draft_event(_virtual_event(), logo_id=None)

    event = _create_payload(posts)
    assert event["format_id"] == EVENT_DEFAULTS["format_id"]
    assert "category_id" not in event
