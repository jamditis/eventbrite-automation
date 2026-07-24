"""Reliability contract for draft creation (PR #38 review follow-ups):

- Enrichment failures (description, ticket class) degrade to warnings —
  they must never lose the created draft's ID.
- An ambiguous create (timeout after POST) reconciles against existing
  drafts instead of failing, so a retry can't duplicate the event.
"""

from datetime import datetime

import pytest
import requests

from airtable_client import EventRecord
from eventbrite_client import EventbriteClient

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


def _event():
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


def _client(monkeypatch, fake_post, fake_get=None):
    client = EventbriteClient()
    client._organization_id = CCM_ORG
    monkeypatch.setattr(client.session, "post", fake_post)
    if fake_get is not None:
        monkeypatch.setattr(client.session, "get", fake_get)
    return client


def test_enrichment_exception_becomes_warning(monkeypatch):
    """A timeout while adding the description/ticket class must not raise —
    the draft exists, and losing its ID would cause a duplicate on retry."""

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        if url.endswith(f"/organizations/{CCM_ORG}/events/"):
            return _Resp(200, {"id": "999", "url": "https://eventbrite.com/e/999"})
        raise requests.ConnectionError("enrichment endpoint down")

    client = _client(monkeypatch, fake_post)
    result = client.create_draft_event(_event(), logo_id=None)

    assert result.event_id == "999"
    assert any("Overview description" in w for w in result.warnings)
    assert any("ticket class" in w for w in result.warnings)


def test_ambiguous_create_adopts_existing_draft(monkeypatch):
    """Create POST times out, but the draft was created server-side — the
    reconciliation lookup finds it by exact title and adopts it."""

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        if url.endswith(f"/organizations/{CCM_ORG}/events/"):
            raise requests.Timeout("create timed out")
        return _Resp(200, {})

    def fake_get(url, headers=None, params=None, timeout=None, **kwargs):
        assert params["status"] == "draft"
        return _Resp(200, {"events": [
            {"id": "777", "url": "https://eventbrite.com/e/777",
             "name": {"text": "Test webinar"}},
        ]})

    client = _client(monkeypatch, fake_post, fake_get)
    result = client.create_draft_event(_event(), logo_id=None)

    assert result.event_id == "777"
    assert any("adopted" in w for w in result.warnings)


def test_ambiguous_create_reraises_when_no_draft_found(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        if url.endswith(f"/organizations/{CCM_ORG}/events/"):
            raise requests.Timeout("create timed out")
        return _Resp(200, {})

    def fake_get(url, headers=None, params=None, timeout=None, **kwargs):
        return _Resp(200, {"events": []})

    client = _client(monkeypatch, fake_post, fake_get)
    with pytest.raises(requests.Timeout):
        client.create_draft_event(_event(), logo_id=None)
