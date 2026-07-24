"""Cron entry point + decision logic for the attendee digest.

Run as: `python -m digest.cron [--dry-run]`

Decision sequence per event row, per tick:
  1. Initial briefing pending (requested but not sent) on a configured
     weekday and inside the event window? send + return.
     This fires even when the row is NOT enabled — a staff-requested
     briefing on a not-yet-enabled draft must not silently never-fire
     (see AirtableClient.list_active_records, which selects these rows
     regardless of Enabled).
  2. Not enabled? skip the daily-digest path.
  3. Today is outside the configured weekdays? skip.
  4. Outside the configured days-out window? skip.
  5. Before today's configured send-time (in ET)? skip.
  6. Already sent today? skip.
  7. No initial briefing yet? skip — daily digests gate on the staff-
     authorized initial briefing so we never auto-fire on a fresh event.
  8. Send daily digest. If silent-when-empty (no new attendees), bail
     before SMTP without recording state changes.

Deployment constraints (per CLAUDE.md "services run on houseofjawn only"):
  - flock() in _acquire_lock is per-machine. Cross-machine coordination
    is not a goal — only houseofjawn runs this cron. If that ever
    changes, replace the lock with an Airtable lease field.
  - _format_event_when uses GNU `%-d` / `%-I` strftime flags which work
    on Linux but not Windows. Fine on houseofjawn.
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import sys
import time
import traceback
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .airtable_client import AirtableClient, EventRow
from .config import load_config
from .crm_lookup import CrmLookup
from .email_renderer import EmailRenderer, RenderContext
from .eventbrite_client import EventbriteClient
from .llm_subprocess import LLMRunner
from .profile_builder import ProfileBuilder
from .send_engine import SendEngine

ET = ZoneInfo("America/New_York")
DEFAULT_LOCK_PATH = (
    Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    / "digest-cron.lock"
)

logger = logging.getLogger("digest.cron")


def parse_send_time_et(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def _now_in_window(row: EventRow, now: datetime) -> bool:
    if not row.event_start_et:
        return False
    event_start = _parse_iso_aware(row.event_start_et)
    if event_start <= now:
        return False
    days_until = (event_start - now).total_seconds() / 86400
    return days_until <= row.days_out_to_start


def _is_past_send_time_today(send_time_et: str, now: datetime) -> bool:
    h, m = parse_send_time_et(send_time_et)
    now_et = now.astimezone(ET)
    threshold = now_et.replace(hour=h, minute=m, second=0, microsecond=0)
    return now_et >= threshold


def _parse_iso_aware(s: str) -> datetime:
    """Parse an ISO timestamp; assume UTC if no timezone info is present.

    Airtable can hand back naive ISO strings (no Z, no offset) when staff
    type a datetime in the UI without specifying tz. We normalize on read
    rather than letting astimezone() interpret naive timestamps in the
    machine's local zone, which would shift the calendar day.
    """
    parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _already_sent_today(last_sent_at: str | None, now: datetime) -> bool:
    if not last_sent_at:
        return False
    last = _parse_iso_aware(last_sent_at)
    return last.astimezone(ET).date() == now.astimezone(ET).date()


def has_pending_initial_briefing(row: EventRow) -> bool:
    return bool(row.initial_briefing_requested_at) and not row.initial_briefing_sent_at


def is_scheduled_weekday(row: EventRow, now: datetime) -> bool:
    return (
        row.send_weekdays is None
        or now.astimezone(ET).weekday() in row.send_weekdays
    )


def should_send_initial(row: EventRow, now: datetime) -> bool:
    return (
        has_pending_initial_briefing(row)
        and is_scheduled_weekday(row, now)
        and (not row.event_start_et or _now_in_window(row, now))
    )


def should_send_today(row: EventRow, now: datetime) -> bool:
    if not row.enabled:
        return False
    if not is_scheduled_weekday(row, now):
        return False
    if not _now_in_window(row, now):
        return False
    if not _is_past_send_time_today(row.send_time_et, now):
        return False
    if _already_sent_today(row.last_digest_sent_at, now):
        return False
    if not row.initial_briefing_sent_at:
        return False
    return True


def _skip_reason(row: EventRow, now: datetime) -> str:
    if has_pending_initial_briefing(row):
        if not is_scheduled_weekday(row, now):
            return "initial briefing waiting for configured weekday"
        if row.event_start_et and not _now_in_window(row, now):
            return "initial briefing outside event window"
    if not row.enabled:
        return "event disabled"
    if not is_scheduled_weekday(row, now):
        return "outside configured weekday"
    if not _now_in_window(row, now):
        return "outside event window"
    if not _is_past_send_time_today(row.send_time_et, now):
        return "before configured send time"
    if _already_sent_today(row.last_digest_sent_at, now):
        return "already sent today"
    if not row.initial_briefing_sent_at:
        return "initial briefing not sent"
    return "not eligible"


def _parse_active_records(
    records: list[dict],
) -> Iterator[tuple[str, EventRow | None, Exception | None]]:
    """Parse active records independently so one invalid row cannot stop a tick."""
    for record in records:
        record_id = str(record.get("id") or "<unknown>")
        try:
            yield record_id, EventRow.from_airtable(record), None
        except Exception as error:
            yield record_id, None, error


def _record_error_safely(
    airtable: AirtableClient,
    record_id: str,
    message: str,
) -> None:
    try:
        airtable.record_error_by_id(record_id, message)
    except Exception:
        logger.exception("could not record error for event record %s", record_id)


def _retry_state_write(fn, *args, attempts: int = 3, base_delay: float = 2.0, **kwargs):
    """Retry the post-send Airtable state write with backoff.

    This write is the critical one: the email has already left SMTP, and if
    the state write is lost the next tick re-sends the same content (the
    email-ledger window is shorter than the 24h tick interval, so it can't
    catch this). Retrying here shrinks that window from "any Airtable blip"
    to "Airtable down for the whole retry span" — and if we still fail, we
    raise so the failure is recorded and the run exits non-zero (which fires
    the OnFailure alert).
    """
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception:
            if attempt == attempts:
                logger.critical(
                    "state write %s failed after %d attempts — email was SENT but "
                    "Airtable state is stale; next tick may re-send. Fix the row manually.",
                    getattr(fn, "__name__", fn), attempts,
                )
                raise
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "state write %s failed (attempt %d/%d); retrying in %.0fs",
                getattr(fn, "__name__", fn), attempt, attempts, delay,
                exc_info=True,
            )
            time.sleep(delay)


_TZ_ABBREVS = {
    "America/New_York": "ET",
    "America/Chicago": "CT",
    "America/Denver": "MT",
    "America/Los_Angeles": "PT",
    "America/Phoenix": "MST",
    "America/Anchorage": "AKT",
    "Pacific/Honolulu": "HT",
    "UTC": "UTC",
}


def _format_event_when(start_local: str, tz_name: str) -> str:
    """`2026-05-15T13:00:00` + tz string -> `Friday, May 15, 2026 at 1:00 PM ET`.

    Honors the event's actual timezone — virtual events not in ET render
    with the right local-time string and zone abbreviation. Falls back to
    rendering the bare zone name (e.g., 'Europe/London') if no abbreviation
    is mapped, so consumers always see something recognizable.
    """
    dt = datetime.fromisoformat(start_local)
    abbrev = _TZ_ABBREVS.get(tz_name, tz_name)
    return dt.strftime(f"%A, %B %-d, %Y at %-I:%M %p {abbrev}")


class _NoopLedger:
    """No-op ledger used when email_ledger module is unavailable. Disables
    the cross-session dup safety net but keeps the cron functional. The
    primary dup defense (last_digest_sent_at on the Airtable row) still
    works.
    """

    def check_duplicate(self, recipient, subject, thread_id=None, hours=6):
        return None

    def log_send(self, **kw):
        return 0


def _acquire_lock(path: Path = DEFAULT_LOCK_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.info("previous tick still running; exiting")
        fh.close()
        sys.exit(0)
    return fh


def _run_briefing(
    row: EventRow,
    eb: EventbriteClient,
    crm: CrmLookup,
    llm: LLMRunner,
    renderer: EmailRenderer,
    sender: SendEngine,
    airtable: AirtableClient,
    now: datetime,
    *,
    is_initial: bool,
    dry_run: bool,
    logo_url: str = "",
) -> bool:
    """Run one event's briefing. Returns True on success (including silent
    skips), False on a handled per-event failure — the caller counts False
    toward the tick's failed events so the OnFailure alert fires for it.
    """
    logger.info("processing %s (initial=%s)", row.slug, is_initial)

    # Recipient-validity gate: a row missing speaker_emails or lead_host_email
    # must not reach SMTP. Without this guard, an empty `to` list still
    # produces a deliverable message via the always-BCC addresses, and the
    # success path would then mark Airtable as sent — speakers receive
    # nothing while ops sees green. These are per-event failures: a requested
    # digest did not go out, so they must fail the tick (and alert staff),
    # not just write `Last error`.
    if not row.speaker_emails:
        msg = f"speaker_emails empty for row {row.record_id}; refusing to send digest"
        logger.error(msg)
        if not dry_run:
            airtable.record_error(row, msg)
        return False
    if not row.lead_host_email:
        msg = f"lead_host_email empty for row {row.record_id}; refusing to send digest"
        logger.error(msg)
        if not dry_run:
            airtable.record_error(row, msg)
        return False

    builder = ProfileBuilder(crm, llm, question_id_filter=row.question_ids_to_include or None)

    attendees = list(eb.fetch_attendees(row.eventbrite_event_id))
    # Each profile still carries an LLM-generated `blurb` for CRM-matched
    # attendees, but the current briefing template renders name + Q&A only and
    # never shows the blurb. The pipeline is retained, not surfaced — a recorded
    # decision, not an accident. Whether to resurface or gate it off is tracked
    # in issue #8 (codex blurb invocation); don't treat the blurb as dead by
    # mistake.
    profiles = [p for p in (builder.build(a) for a in attendees) if p is not None]

    cursor = row.last_attendee_cursor
    if is_initial:
        new_profiles = profiles
        existing_profiles: list = []
    elif cursor:
        new_profiles = [p for p in profiles if p.created_at > cursor]
        existing_profiles = [p for p in profiles if p.created_at <= cursor]
    else:
        new_profiles = profiles
        existing_profiles = []

    existing_profiles.sort(key=lambda p: p.created_at)
    new_profiles.sort(key=lambda p: p.created_at, reverse=True)

    if not is_initial and not new_profiles:
        logger.info("silent: no new attendees for %s", row.slug)
        return True

    event_meta = eb.fetch_event(row.eventbrite_event_id)
    when = _format_event_when(event_meta.start_local, event_meta.timezone)

    subject = (
        renderer.format_subject_initial(row.title, len(profiles))
        if is_initial
        else renderer.format_subject_daily(row.title, len(new_profiles), len(profiles))
    )
    ctx = RenderContext(
        event_title=row.title,
        event_when=when,
        event_location="",
        total_count=len(profiles),
        new_attendees=new_profiles,
        existing_attendees=existing_profiles,
        subject=subject,
        logo_url=logo_url or None,
        # Per-event attendee sheet, generated out-of-band and stored on the
        # Airtable row. Empty -> None so the "view full sheet" button is omitted.
        sheet_url=row.sheet_url or None,
        is_initial=is_initial,
    )
    html_body = renderer.render(ctx)
    text_body = renderer.render_plain_text(ctx)

    if dry_run:
        logger.info("dry-run: would send %r to %s", subject, row.speaker_emails)
        return True

    result = sender.send(
        to=row.speaker_emails,
        reply_to=row.lead_host_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        slug=row.slug,
        # Initial briefing and daily digest share a slug and a lead-host
        # reply_to; the kind keeps their ledger dedup keys distinct so a
        # daily digest is never suppressed by a recent initial briefing.
        kind="initial" if is_initial else "daily",
        session_type="cron",
    )

    # The ledger is canonical for "did email leave SMTP" — log_send fires
    # immediately after smtp.send_message succeeds. So a `duplicate` reason
    # means the email DID go out on a PRIOR tick; only the Airtable state
    # write didn't follow.
    if not result.sent and result.reason == "duplicate":
        # Reconcile the sent-at marker so we don't re-send the same calendar
        # day, but do NOT advance the attendee cursor. The duplicate email
        # reflected an older fetch; advancing to THIS tick's cursor would mark
        # any attendee who registered in the gap between that send and now as
        # covered though they never appeared in a digest — silent loss (#20).
        # The next genuine send advances the cursor against the unchanged
        # value and picks up the gap attendees.
        logger.warning(
            "ledger duplicate for %s; reconciling sent-at without advancing cursor",
            row.slug,
        )
        if is_initial:
            _retry_state_write(airtable.reconcile_after_initial_send, row, sent_at=now)
        else:
            _retry_state_write(airtable.reconcile_after_send, row, sent_at=now)
        return True

    # A genuine send (this tick's email reflects this tick's fetch) advances
    # the cursor to the latest attendee so the next tick diffs against it.
    if result.sent:
        max_cursor = max((p.created_at for p in profiles), default=cursor or "")
        if is_initial:
            _retry_state_write(
                airtable.update_after_initial_send,
                row, sent_at=now, attendee_cursor=max_cursor, attendee_count=len(profiles),
            )
        else:
            _retry_state_write(
                airtable.update_after_send,
                row, sent_at=now, attendee_cursor=max_cursor, attendee_count=len(profiles),
            )
        return True
    else:
        # Not sent, and not the duplicate-reconcile handled above. send_engine
        # only returns sent=False with reason="duplicate" today (real SMTP
        # failures raise and are caught by the per-row handler), so this is
        # unreachable now — but a future reason string must not become a silent
        # no-op that leaves the row in stale state with no trace. Surface it.
        msg = f"send returned sent=False reason={result.reason!r} for {row.slug}; no state written"
        logger.warning(msg)
        airtable.record_error(row, msg)
        return False


def main(dry_run: bool = False) -> int:
    """Run one tick. Returns a process exit code:

    0 = clean tick (sends, silent skips, or nothing eligible)
    1 = run-level failure (config, Airtable fetch, or other setup crash)
    2 = one or more events failed while the rest of the tick completed

    Any non-zero exit puts the systemd unit in a failed state and fires the
    OnFailure alert (deploy/digest-failure-alert.service), so problems are
    emailed to staff instead of sitting silently in journald.
    """
    try:
        return _run_tick(dry_run=dry_run)
    except SystemExit:
        raise
    except Exception:
        # Run-level boundary: without this, a config/Airtable/setup failure
        # would be visible only as a traceback in journald.
        logger.exception("digest tick failed before/outside per-event processing")
        return 1


def _run_tick(dry_run: bool = False) -> int:
    cfg = load_config()
    lock = _acquire_lock()
    failed_events: list[str] = []
    try:
        airtable = AirtableClient(cfg.airtable_pat, cfg.airtable_base_id, cfg.airtable_table_name)
        eb = EventbriteClient(cfg.eventbrite_token)
        crm = CrmLookup(cfg.dashboard_api_base, cfg.dashboard_api_key)
        llm = LLMRunner(cfg.gemini_bin, cfg.codex_bin, cfg.codex_model)

        ledger_path = os.environ.get(
            "DIGEST_LEDGER_PATH",
            str(Path.home() / "projects" / "houseofjawn-bot" / "scheduler"),
        )
        sys.path.insert(0, ledger_path)
        try:
            from email_ledger import check_duplicate, log_send  # type: ignore

            class _LedgerWrapper:
                def check_duplicate(self, recipient, subject, thread_id=None, hours=6):
                    return check_duplicate(
                        recipient, subject, thread_id=thread_id, hours=hours
                    )

                def log_send(self, **kw):
                    return log_send(**kw)

            ledger = _LedgerWrapper()
        except ImportError as e:
            logger.warning(
                "email_ledger missing at %s (%s); falling back to no-op ledger. "
                "Duplicate-send protection from this safety net is DISABLED. "
                "Override path with DIGEST_LEDGER_PATH env.",
                ledger_path, e,
            )
            ledger = _NoopLedger()

        sender = SendEngine(
            smtp_host=cfg.smtp_host,
            smtp_port=cfg.smtp_port,
            smtp_user=cfg.smtp_user,
            smtp_password=cfg.smtp_password,
            from_name=cfg.smtp_from_name,
            from_email=cfg.smtp_from_email,
            bcc_always=cfg.bcc_always,
            cc_always=cfg.cc_always,
            ledger=ledger,
        )
        renderer = EmailRenderer()

        now = datetime.now(UTC)
        records = airtable.list_active_records()
        logger.info("tick: %d active rows, now=%s", len(records), now.isoformat())

        for record_id, row, parse_error in _parse_active_records(records):
            if parse_error is not None:
                logger.error("event record %s invalid: %s", record_id, parse_error)
                failed_events.append(record_id)
                if not dry_run:
                    _record_error_safely(
                        airtable,
                        record_id,
                        f"{type(parse_error).__name__}: {parse_error}",
                    )
                continue

            assert row is not None
            try:
                if should_send_initial(row, now):
                    ok = _run_briefing(
                        row, eb, crm, llm, renderer, sender, airtable, now,
                        is_initial=True, dry_run=dry_run, logo_url=cfg.logo_url,
                    )
                    if not ok:
                        failed_events.append(row.slug)
                elif row.enabled and should_send_today(row, now):
                    ok = _run_briefing(
                        row, eb, crm, llm, renderer, sender, airtable, now,
                        is_initial=False, dry_run=dry_run, logo_url=cfg.logo_url,
                    )
                    if not ok:
                        failed_events.append(row.slug)
                else:
                    logger.info("event %s skipped: %s", row.slug, _skip_reason(row, now))
            except Exception as e:
                logger.exception("event %s failed", row.slug)
                failed_events.append(row.slug)
                if not dry_run:
                    _record_error_safely(
                        airtable,
                        row.record_id,
                        f"{type(e).__name__}: {e}\n{traceback.format_exc()[:1500]}",
                    )
    finally:
        lock.close()

    if failed_events:
        logger.error(
            "tick finished with %d failed event(s): %s",
            len(failed_events), ", ".join(failed_events),
        )
        return 2
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sys.exit(main(dry_run=args.dry_run))
