import json
from pathlib import Path

import pytest

from digest.eventbrite_client import (
    EventbriteAttendee,
    EventbriteClient,
    EventbritePaginationError,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "eb_attendees_sample.json").read_text()
)


class _MockResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _StubSession:
    """Stands in for the client's retrying session; records every call."""

    def __init__(self, fake_get):
        self.get = fake_get


def _stub_session(monkeypatch, fake_get):
    monkeypatch.setattr(
        "digest.eventbrite_client._build_session", lambda: _StubSession(fake_get)
    )


@pytest.fixture
def mock_responses(monkeypatch):
    """Stub the session's get to walk the paginated EB fixture; record every call."""
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append({"url": url, "headers": dict(headers or {}), "params": dict(params or {})})
        if (params or {}).get("continuation"):
            return _MockResponse(200, FIXTURE["page_two"])
        return _MockResponse(200, FIXTURE["page_one"])

    _stub_session(monkeypatch, fake_get)
    return calls


def test_fetch_attendees_walks_pagination(mock_responses):
    client = EventbriteClient(token="tok123")
    attendees = list(client.fetch_attendees(event_id="EVT-1"))
    assert len(attendees) == 3
    assert all(isinstance(a, EventbriteAttendee) for a in attendees)
    assert {a.id for a in attendees} == {"100001", "100002", "100003"}


def test_fetch_attendees_includes_cancelled(mock_responses):
    client = EventbriteClient(token="tok123")
    attendees = list(client.fetch_attendees(event_id="EVT-1"))
    cancelled = [a for a in attendees if a.cancelled]
    assert len(cancelled) == 1
    assert cancelled[0].id == "100002"


def test_fetch_attendees_uses_bearer_auth(mock_responses):
    client = EventbriteClient(token="tok123")
    list(client.fetch_attendees(event_id="EVT-1"))
    assert mock_responses[0]["url"].endswith("/events/EVT-1/attendees/")
    assert mock_responses[0]["headers"].get("Authorization") == "Bearer tok123"


def test_fetch_attendees_passes_continuation_on_page_two(mock_responses):
    client = EventbriteClient(token="tok123")
    list(client.fetch_attendees(event_id="EVT-1"))
    assert len(mock_responses) == 2
    assert "continuation" not in mock_responses[0]["params"]
    assert mock_responses[1]["params"]["continuation"] == "eyJwYWdlIjoyfQ=="


def test_attendee_dataclass_exposes_form_answers(mock_responses):
    client = EventbriteClient(token="tok123")
    attendees = list(client.fetch_attendees(event_id="EVT-1"))
    sarah = next(a for a in attendees if a.id == "100001")
    assert len(sarah.answers) == 1
    assert sarah.answers[0]["question_id"] == "q_1"
    assert "AI workflows" in sarah.answers[0]["answer"]


def test_fetch_attendees_raises_on_truncated_pagination(monkeypatch):
    """has_more_items=true with no continuation token must raise, not silently
    truncate. Silent truncation would mean the digest ships missing the most
    recent registrants — a category of failure that's invisible until a speaker
    notices someone obviously missing.
    """
    truncated = {
        "pagination": {
            "page_number": 1,
            "has_more_items": True,
            "continuation": None,
        },
        "attendees": [],
    }

    def fake_get(url, headers=None, params=None, timeout=None):
        return _MockResponse(200, truncated)

    _stub_session(monkeypatch, fake_get)
    client = EventbriteClient(token="tok123")
    with pytest.raises(EventbritePaginationError, match="has_more_items"):
        list(client.fetch_attendees(event_id="EVT-1"))


def test_attendee_email_is_lowercased_at_construction(monkeypatch):
    """Producer-side normalization: downstream consumers (CRM lookup, BCC dedup,
    blurb hashing) get canonical lowercase without remembering to .lower().
    """
    payload = {
        "pagination": {"has_more_items": False, "continuation": None},
        "attendees": [
            {
                "id": "X1",
                "created": "2026-05-01T00:00:00Z",
                "status": "Attending",
                "cancelled": False,
                "refunded": False,
                "profile": {
                    "first_name": "Mixed",
                    "last_name": "Case",
                    "email": "Mixed.Case@Example.COM",
                    "name": "Mixed Case",
                },
                "answers": [],
            }
        ],
    }

    def fake_get(url, headers=None, params=None, timeout=None):
        return _MockResponse(200, payload)

    _stub_session(monkeypatch, fake_get)
    client = EventbriteClient(token="tok123")
    attendee = next(client.fetch_attendees(event_id="EVT-1"))
    assert attendee.email == "mixed.case@example.com"


def test_fetch_event_returns_event_metadata(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None):
        return _MockResponse(
            200,
            {
                "id": "EVT-1",
                "name": {"text": "Test Training"},
                "start": {"local": "2026-05-15T13:00:00", "timezone": "America/New_York"},
            },
        )

    _stub_session(monkeypatch, fake_get)
    client = EventbriteClient(token="tok123")
    event = client.fetch_event("EVT-1")
    assert event.id == "EVT-1"
    assert event.title == "Test Training"
    assert event.start_local == "2026-05-15T13:00:00"
    assert event.timezone == "America/New_York"
