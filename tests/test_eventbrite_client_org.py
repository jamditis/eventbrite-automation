"""Regression tests for organization selection in the webhook draft creator.

See issue #32: the token's `GET /users/me/organizations/` now returns a
blank-named org first that the token cannot create events under (403). Picking
`organizations[0]` breaks `create_draft_event` for every real submission.
"""

import pytest

import eventbrite_client as ebc
from eventbrite_client import EventbriteClient

BLANK_ORG = "213027297304"
CCM_ORG = "66857244479"

ORGS_BLANK_FIRST = {
    "organizations": [
        {"id": BLANK_ORG, "name": ""},
        {"id": CCM_ORG, "name": "Center for Cooperative Media"},
    ]
}


class _MockResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _stubbed_client(monkeypatch, payload):
    def fake_get(url, headers=None, timeout=None, **kwargs):
        return _MockResponse(200, payload)

    client = EventbriteClient()
    monkeypatch.setattr(client.session, "get", fake_get)
    return client


def test_selects_ccm_org_not_blank_first(monkeypatch):
    """The CCM org is chosen even when a blank-named org is listed first."""
    client = _stubbed_client(monkeypatch, ORGS_BLANK_FIRST)
    assert client.organization_id == CCM_ORG


def test_raises_when_configured_org_missing(monkeypatch):
    """A pinned org the token cannot see fails fast instead of 403ing at create."""
    monkeypatch.setattr(ebc, "EVENTBRITE_ORGANIZATION_ID", "999999")
    client = _stubbed_client(monkeypatch, {"organizations": [{"id": BLANK_ORG, "name": ""}]})
    with pytest.raises(ValueError):
        _ = client.organization_id


def test_falls_back_to_brand_name_when_unpinned(monkeypatch):
    """With no pinned org, the CCM brand name still wins over list order."""
    monkeypatch.setattr(ebc, "EVENTBRITE_ORGANIZATION_ID", "")
    client = _stubbed_client(monkeypatch, ORGS_BLANK_FIRST)
    assert client.organization_id == CCM_ORG
