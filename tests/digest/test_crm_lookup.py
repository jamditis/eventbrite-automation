import json
from pathlib import Path

import pytest

from digest.crm_lookup import CrmContact, CrmLookup

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "crm_contact_sample.json").read_text()
)


class _MockResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def mock_get(monkeypatch):
    state = {"payload": FIXTURE["no_match"], "calls": []}

    def fake_get(url, headers=None, params=None, timeout=None):
        state["calls"].append({"url": url, "headers": dict(headers or {}), "params": dict(params or {})})
        return _MockResp(200, state["payload"])

    monkeypatch.setattr("digest.crm_lookup.requests.get", fake_get)
    return state


def test_lookup_returns_none_when_no_match(mock_get):
    mock_get["payload"] = FIXTURE["no_match"]
    crm = CrmLookup(api_base="http://x/api", api_key="k")
    assert crm.find_by_email("nobody@example.com") is None


def test_lookup_returns_contact_on_exact_email_match(mock_get):
    mock_get["payload"] = FIXTURE["exact_match"]
    crm = CrmLookup(api_base="http://x/api", api_key="k")
    contact = crm.find_by_email("sarah@northjersey.example")
    assert isinstance(contact, CrmContact)
    assert contact.id == 42
    assert contact.organization == "North Jersey Journal"
    assert contact.role == "Editor"


def test_lookup_rejects_substring_match(mock_get):
    mock_get["payload"] = FIXTURE["substring_only"]
    crm = CrmLookup(api_base="http://x/api", api_key="k")
    assert crm.find_by_email("sarah@northjersey.example") is None


def test_lookup_handles_alt_email(mock_get):
    mock_get["payload"] = {
        "items": [
            {
                "id": 50,
                "name": "Joe X",
                "email": "joe@primary.com",
                "alt_email": "joe@alt.com",
                "work_email": None,
                "organization": "Org",
                "role": "Role",
                "notes": "",
                "interactions": [],
            }
        ],
        "total": 1,
    }
    crm = CrmLookup(api_base="http://x/api", api_key="k")
    assert crm.find_by_email("joe@alt.com") is not None


def test_lookup_returns_none_on_network_error(monkeypatch):
    def fake_get(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr("digest.crm_lookup.requests.get", fake_get)
    crm = CrmLookup(api_base="http://x/api", api_key="k")
    assert crm.find_by_email("any@example.com") is None


def test_lookup_sends_api_key_header(mock_get):
    mock_get["payload"] = FIXTURE["no_match"]
    crm = CrmLookup(api_base="http://x/api", api_key="secret-k")
    crm.find_by_email("nobody@example.com")
    assert mock_get["calls"][0]["headers"].get("X-API-Key") == "secret-k"
    assert mock_get["calls"][0]["url"].endswith("/api/contacts/")


def test_lookup_short_circuits_on_empty_email(mock_get):
    crm = CrmLookup(api_base="http://x/api", api_key="k")
    assert crm.find_by_email("") is None
    assert mock_get["calls"] == []
