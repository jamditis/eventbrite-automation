"""Tests for the webhook server: routing, validation, duplicate guards, and
failure visibility.

These pin the reliability contract added in the hardening pass:
  - failures set the record's Status to ERROR_STATUS and write the Logs field
  - a record that already has an Eventbrite event ID is repaired, not
    re-created (no duplicate drafts)
  - concurrent fires for the same record are rejected
  - temp images are cleaned up even when the pipeline throws
"""

from types import SimpleNamespace

import pytest

import webhook_server
from airtable_client import AirtableClient
from config import ERROR_STATUS, PROCESSED_STATUS


@pytest.fixture
def client():
    webhook_server.app.config["TESTING"] = True
    with webhook_server.app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def clean_state():
    def _clear():
        with webhook_server._status_lock:
            webhook_server.processing_status.clear()
        with webhook_server._in_flight_lock:
            webhook_server._in_flight.clear()
        webhook_server._created_events.clear()

    _clear()
    yield
    _clear()


class FakeAirtable:
    """Records every state-changing call so tests can assert on them."""

    def __init__(self, record=None):
        self.record = record
        self.status_updates = []
        self.log_entries = []
        self.marked = []
        self.mark_result = True

    def get_record_by_id(self, record_id):
        return self.record

    def update_status(self, record_id, status):
        self.status_updates.append((record_id, status))
        return True

    def update_log(self, record_id, message):
        self.log_entries.append((record_id, message))
        return True

    def mark_as_processed(self, record_id, url, event_id=None):
        self.marked.append((record_id, url, event_id))
        return self.mark_result

    def add_image_attachment(self, record_id, url, filename=None):
        return True

    def update_event_id(self, record_id, event_id):
        return True


def _record(**overrides):
    defaults = dict(
        record_id="rec1234567890",
        title="Test event",
        status="Todo",
        eventbrite_event_id=None,
        eventbrite_url=None,
        validation_warnings=[],
        is_virtual=True,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _wire_pipeline(monkeypatch, airtable, tmp_path, create_raises=None):
    """Install fakes for the three pipeline clients."""
    image_file = tmp_path / "banner.png"
    image_file.write_bytes(b"png")

    fake_gen = SimpleNamespace(
        generate_event_image=lambda event: SimpleNamespace(
            path=image_file, used_fallback=False, error=None
        )
    )

    def fake_create(event, logo_id=None):
        if create_raises:
            raise create_raises
        return SimpleNamespace(
            event_id="999", url="https://eventbrite.com/e/999", warnings=[]
        )

    fake_eb = SimpleNamespace(
        upload_image=lambda path: "logo-1",
        create_draft_event=fake_create,
        get_event_logo_url=lambda event_id: None,
        update_event_image=lambda event_id, logo_id: True,
    )

    class _FakeAirtableFactory:
        # the regenerate path calls this staticmethod on the class itself
        extract_event_id_from_url = staticmethod(AirtableClient.extract_event_id_from_url)

        def __new__(cls):
            return airtable

    monkeypatch.setattr(webhook_server, "AirtableClient", _FakeAirtableFactory)
    monkeypatch.setattr(webhook_server, "EventbriteClient", lambda: fake_eb)
    monkeypatch.setattr(webhook_server, "ImageGenerator", lambda: fake_gen)
    return image_file


# --- Route validation -------------------------------------------------------

def test_missing_json_returns_400(client):
    resp = client.post("/webhook/airtable", data="", content_type="application/json")
    assert resp.status_code == 400


def test_invalid_record_id_returns_400(client):
    resp = client.post("/webhook/airtable", json={"record_id": "not-a-record"})
    assert resp.status_code == 400


def test_unknown_status_returns_404(client):
    resp = client.get("/webhook/status/recUNKNOWN123")
    assert resp.status_code == 404
    assert resp.get_json()["status"] == "unknown"


def test_health_check_reports_in_flight(client):
    resp = client.get("/")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["status"] == "ok"
    assert body["in_flight"] == 0


# --- Async accept + duplicate-fire rejection --------------------------------

def test_async_request_is_accepted(client, monkeypatch):
    submitted = []
    monkeypatch.setattr(webhook_server._executor, "submit", lambda fn, *a: submitted.append(a))
    resp = client.post("/webhook/airtable", json={"record_id": "rec1234567890"})
    assert resp.status_code == 202
    assert submitted == [("rec1234567890",)]
    assert webhook_server.processing_status["rec1234567890"]["status"] == "processing"


def test_duplicate_fire_while_processing_is_not_resubmitted(client, monkeypatch):
    submitted = []
    monkeypatch.setattr(webhook_server._executor, "submit", lambda fn, *a: submitted.append(a))
    client.post("/webhook/airtable", json={"record_id": "rec1234567890"})
    resp = client.post("/webhook/airtable", json={"record_id": "rec1234567890"})
    assert resp.status_code == 202
    assert resp.get_json()["status"] == "already_processing"
    assert len(submitted) == 1


def test_airtable_fire_during_regeneration_is_rejected(client, monkeypatch):
    """A regular processing fire must not clobber an in-flight regeneration."""
    submitted = []
    monkeypatch.setattr(webhook_server._executor, "submit", lambda fn, *a: submitted.append(a))
    webhook_server._set_status("rec1234567890", {"status": "regenerating"})
    resp = client.post("/webhook/airtable", json={"record_id": "rec1234567890"})
    assert resp.status_code == 202
    assert resp.get_json()["status"] == "already_processing"
    assert submitted == []


def test_in_flight_claim_blocks_concurrent_processing(monkeypatch):
    assert webhook_server._try_claim("rec1234567890")
    result = webhook_server.process_record("rec1234567890")
    assert result == {
        "success": False,
        "error": "already_processing",
        "record_id": "rec1234567890",
    }
    webhook_server._release("rec1234567890")


# --- Pipeline behavior ------------------------------------------------------

def test_successful_processing_marks_record(monkeypatch, tmp_path):
    airtable = FakeAirtable(record=_record())
    image_file = _wire_pipeline(monkeypatch, airtable, tmp_path)

    result = webhook_server.process_record("rec1234567890")

    assert result["success"] is True
    assert airtable.marked == [("rec1234567890", "https://eventbrite.com/e/999", "999")]
    assert not image_file.exists(), "temp image should be cleaned up"


def test_failure_sets_error_status_and_log(monkeypatch, tmp_path):
    airtable = FakeAirtable(record=_record())
    image_file = _wire_pipeline(
        monkeypatch, airtable, tmp_path, create_raises=RuntimeError("EB down")
    )

    result = webhook_server.process_record("rec1234567890")

    assert result["success"] is False
    assert (("rec1234567890", ERROR_STATUS)) in airtable.status_updates
    assert any("EB down" in msg for _, msg in airtable.log_entries)
    assert not image_file.exists(), "temp image should be cleaned up on failure too"


def test_existing_event_id_repairs_instead_of_duplicating(monkeypatch, tmp_path):
    """A record with an Eventbrite event ID but unprocessed status must not
    get a second draft — that's the partial-failure scenario where the draft
    was created but the Airtable write failed."""
    airtable = FakeAirtable(
        record=_record(eventbrite_event_id="777", eventbrite_url="https://eventbrite.com/e/777")
    )
    created = []

    def exploding_create(event, logo_id=None):
        created.append(event)
        raise AssertionError("create_draft_event must not be called")

    _wire_pipeline(monkeypatch, airtable, tmp_path)
    result = webhook_server.process_record("rec1234567890")

    assert result["success"] is True
    assert "repaired" in result["message"]
    assert created == []
    assert airtable.marked == [("rec1234567890", "https://eventbrite.com/e/777", "777")]


def test_already_processed_record_is_skipped(monkeypatch, tmp_path):
    airtable = FakeAirtable(record=_record(status=PROCESSED_STATUS))
    _wire_pipeline(monkeypatch, airtable, tmp_path)
    result = webhook_server.process_record("rec1234567890")
    assert result["success"] is True
    assert result["message"] == "Already processed"
    assert airtable.marked == []


def test_fallback_image_is_flagged_in_airtable_log(monkeypatch, tmp_path):
    airtable = FakeAirtable(record=_record())
    image_file = tmp_path / "banner.png"
    image_file.write_bytes(b"png")

    fake_gen = SimpleNamespace(
        generate_event_image=lambda event: SimpleNamespace(
            path=image_file, used_fallback=True, error="ValueError: no image"
        )
    )
    fake_eb = SimpleNamespace(
        upload_image=lambda path: "logo-1",
        create_draft_event=lambda event, logo_id=None: SimpleNamespace(
            event_id="999", url="https://eventbrite.com/e/999", warnings=[]
        ),
        get_event_logo_url=lambda event_id: None,
    )
    monkeypatch.setattr(webhook_server, "AirtableClient", lambda: airtable)
    monkeypatch.setattr(webhook_server, "EventbriteClient", lambda: fake_eb)
    monkeypatch.setattr(webhook_server, "ImageGenerator", lambda: fake_gen)

    result = webhook_server.process_record("rec1234567890")
    assert result["success"] is True
    assert any("AI image generation failed" in msg for _, msg in airtable.log_entries)


def test_airtable_persistence_failure_reports_failure(monkeypatch, tmp_path):
    """Draft created but the Airtable status write failed: the result must be
    a failure (retryable), and the in-process cache must let the retry repair
    the record instead of creating a second draft."""
    webhook_server._created_events.clear()
    airtable = FakeAirtable(record=_record())
    airtable.mark_result = False
    _wire_pipeline(monkeypatch, airtable, tmp_path)

    result = webhook_server.process_record("rec1234567890")

    assert result["success"] is False
    assert result["error"] == "airtable_update_failed"
    assert result["eventbrite_event_id"] == "999"
    # the draft is remembered in-process for the duplicate guard
    assert webhook_server._created_events["rec1234567890"] == (
        "999", "https://eventbrite.com/e/999"
    )

    # Retry with Airtable recovered: repair path, no second create
    airtable2 = FakeAirtable(record=_record())  # Airtable still has no event ID

    def exploding_create(event, logo_id=None):
        raise AssertionError("create_draft_event must not be called on retry")

    _wire_pipeline(monkeypatch, airtable2, tmp_path)
    result2 = webhook_server.process_record("rec1234567890")
    assert result2["success"] is True
    assert "repaired" in result2["message"]
    assert airtable2.marked == [("rec1234567890", "https://eventbrite.com/e/999", "999")]
    webhook_server._created_events.clear()


def test_archive_failure_does_not_downgrade_success(monkeypatch, tmp_path):
    """Once Airtable reflects the draft, a failing best-effort archive step
    (logo fetch) must not flip the record to a failed state."""
    airtable = FakeAirtable(record=_record())
    _wire_pipeline(monkeypatch, airtable, tmp_path)

    def exploding_logo_fetch(event_id):
        raise requests.ConnectionError("logo fetch down")

    import requests
    fake_eb = webhook_server.EventbriteClient()
    # rewire just the logo fetch on the already-faked client
    monkeypatch.setattr(fake_eb, "get_event_logo_url", exploding_logo_fetch, raising=False)
    monkeypatch.setattr(webhook_server, "EventbriteClient", lambda: fake_eb)

    result = webhook_server.process_record("rec1234567890")
    assert result["success"] is True
    assert airtable.status_updates == []  # never set to ERROR_STATUS


# --- Status map bounds ------------------------------------------------------

def test_status_map_is_bounded():
    for i in range(webhook_server.MAX_STATUS_ENTRIES + 50):
        webhook_server._set_status(f"rec{i:013d}", {"status": "completed"})
    assert len(webhook_server.processing_status) == webhook_server.MAX_STATUS_ENTRIES
    # oldest entries evicted first
    assert "rec0000000000000" not in webhook_server.processing_status


# --- Optional auth ----------------------------------------------------------

def test_auth_enforced_when_enabled(client, monkeypatch):
    monkeypatch.setattr(webhook_server, "WEBHOOK_REQUIRE_AUTH", True)
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "s3cret")

    resp = client.post("/webhook/airtable", json={"record_id": "rec1234567890"})
    assert resp.status_code == 401

    monkeypatch.setattr(webhook_server._executor, "submit", lambda fn, *a: None)
    resp = client.post(
        "/webhook/airtable", json={"record_id": "rec1234567890", "secret": "s3cret"}
    )
    assert resp.status_code == 202


def test_auth_not_enforced_by_default(client, monkeypatch):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(webhook_server._executor, "submit", lambda fn, *a: None)
    resp = client.post("/webhook/airtable", json={"record_id": "rec1234567890"})
    assert resp.status_code == 202


# --- Regeneration guardrails ------------------------------------------------

def test_regenerate_without_event_id_fails_with_log(monkeypatch, tmp_path):
    airtable = FakeAirtable(record=_record(status="Regenerate image"))
    _wire_pipeline(monkeypatch, airtable, tmp_path)

    result = webhook_server.regenerate_image_for_record("rec1234567890")

    assert result["success"] is False
    assert any("Regeneration failed" in msg for _, msg in airtable.log_entries)


def test_regenerate_recovers_event_id_from_url(monkeypatch, tmp_path):
    airtable = FakeAirtable(
        record=_record(
            status="Regenerate image",
            eventbrite_event_id=None,
            eventbrite_url="https://www.eventbrite.com/e/test-tickets-1234567890",
        )
    )
    _wire_pipeline(monkeypatch, airtable, tmp_path)

    result = webhook_server.regenerate_image_for_record("rec1234567890")

    assert result["success"] is True
    assert result["eventbrite_event_id"] == "1234567890"
    assert (("rec1234567890", PROCESSED_STATUS)) in airtable.status_updates
