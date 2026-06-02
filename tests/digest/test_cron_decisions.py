from datetime import UTC, datetime

from digest.airtable_client import EventRow
from digest.cron import (
    _format_event_when,
    _now_in_window,
    has_pending_initial_briefing,
    parse_send_time_et,
    should_send_today,
)


def _row(**overrides):
    base = dict(
        record_id="rec1",
        slug="s",
        title="T",
        eventbrite_event_id="EVT",
        enabled=True,
        speaker_emails=["a@x.com"],
        lead_host_email="h@x.com",
        sheet_url="",
        days_out_to_start=7,
        send_time_et="07:00",
        question_ids_to_include=[],
        event_start_et="2026-05-15T13:00:00.000Z",
        last_digest_sent_at=None,
        last_attendee_cursor=None,
        last_digest_attendee_count=0,
        initial_briefing_sent_at="2026-05-08T11:00:00+00:00",
        initial_briefing_requested_at=None,
        last_error="",
    )
    base.update(overrides)
    return EventRow(**base)


def test_skips_when_disabled():
    r = _row(enabled=False)
    assert should_send_today(r, datetime(2026, 5, 14, 12, 0, tzinfo=UTC)) is False


def test_skips_when_event_already_passed():
    r = _row(event_start_et="2026-05-01T13:00:00.000Z")
    assert should_send_today(r, datetime(2026, 5, 14, 12, 0, tzinfo=UTC)) is False


def test_skips_outside_window():
    r = _row(days_out_to_start=7)
    too_early = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    assert should_send_today(r, too_early) is False


def test_skips_before_send_time():
    r = _row(send_time_et="07:00")
    too_early_today = datetime(2026, 5, 14, 10, 0, tzinfo=UTC)
    assert should_send_today(r, too_early_today) is False


def test_sends_when_window_and_send_time_passed():
    r = _row(send_time_et="07:00", last_digest_sent_at=None)
    now = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    assert should_send_today(r, now) is True


def test_skips_when_already_sent_today():
    r = _row(
        send_time_et="07:00",
        last_digest_sent_at="2026-05-14T11:30:00+00:00",
    )
    now = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    assert should_send_today(r, now) is False


def test_sends_again_next_day():
    r = _row(
        send_time_et="07:00",
        last_digest_sent_at="2026-05-13T11:30:00+00:00",
    )
    now = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    assert should_send_today(r, now) is True


def test_skips_when_no_initial_briefing_yet():
    """Daily digests are gated on initial briefing — no auto-fire before staff
    confirms the first send."""
    r = _row(initial_briefing_sent_at=None)
    now = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    assert should_send_today(r, now) is False


def test_pending_initial_briefing_detected_when_requested_but_not_sent():
    r = _row(
        initial_briefing_sent_at=None,
        initial_briefing_requested_at="2026-05-14T12:30:00+00:00",
    )
    assert has_pending_initial_briefing(r) is True


def test_no_pending_initial_briefing_when_already_sent():
    r = _row(
        initial_briefing_sent_at="2026-05-14T13:00:00+00:00",
        initial_briefing_requested_at="2026-05-14T12:30:00+00:00",
    )
    assert has_pending_initial_briefing(r) is False


def test_no_pending_initial_briefing_when_no_request():
    r = _row(initial_briefing_sent_at=None, initial_briefing_requested_at=None)
    assert has_pending_initial_briefing(r) is False


def test_now_in_window_includes_event_start_window():
    r = _row(days_out_to_start=7, event_start_et="2026-05-15T13:00:00.000Z")
    inside = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    assert _now_in_window(r, inside) is True


def test_now_in_window_excludes_passed_event():
    r = _row(event_start_et="2026-05-01T13:00:00.000Z")
    later = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    assert _now_in_window(r, later) is False


def test_parse_send_time_et():
    assert parse_send_time_et("07:00") == (7, 0)
    assert parse_send_time_et("14:30") == (14, 30)
    assert parse_send_time_et("00:00") == (0, 0)


def test_format_event_when_renders_friendly_string():
    out = _format_event_when("2026-05-15T13:00:00", "America/New_York")
    assert "Friday" in out
    assert "May 15" in out
    assert "2026" in out
    assert "1:00 PM" in out
    assert "ET" in out


def test_format_event_when_uses_event_timezone_not_hardcoded_et():
    """Eventbrite events outside ET must render with the event's actual tz
    abbreviation, not 'ET'.
    """
    out = _format_event_when("2026-05-15T10:00:00", "America/Los_Angeles")
    assert "PT" in out
    assert "ET" not in out


def test_format_event_when_falls_back_to_zone_name_for_unmapped():
    out = _format_event_when("2026-05-15T10:00:00", "Europe/London")
    assert "Europe/London" in out


def test_already_sent_today_normalizes_naive_iso_as_utc():
    """Airtable can hand back naive ISO strings; without tz normalization,
    astimezone() interprets them in the host's local zone and the calendar
    day shifts. This pins UTC as the assumed zone for naive input.
    """
    from digest.cron import _already_sent_today

    naive_today = "2026-05-14T11:30:00"
    now = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    assert _already_sent_today(naive_today, now) is True


def test_event_start_equal_to_now_is_outside_window():
    """Boundary: event_start == now treats the event as already started
    (window closed)."""
    r = _row(event_start_et="2026-05-14T12:00:00+00:00")
    now = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    assert _now_in_window(r, now) is False


def test_send_time_exactly_now_is_sendable():
    """Boundary: now_et == threshold counts as past-send-time."""
    from digest.cron import _is_past_send_time_today

    # 7:00 AM ET = 11:00 AM UTC (during EDT, but the equality logic is
    # zone-aware via astimezone, so this works either way).
    now_et_7am = datetime(2026, 5, 14, 11, 0, tzinfo=UTC)
    assert _is_past_send_time_today("07:00", now_et_7am) is True
