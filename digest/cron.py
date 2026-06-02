"""Cron entry point + decision logic for the attendee digest.

Run as: `python -m digest.cron [--dry-run]`

Decision sequence per event row, per tick:
  1. Initial briefing pending (requested but not sent)? send + return.
     This fires even when the row is NOT enabled — a staff-requested
     briefing on a not-yet-enabled draft must not silently never-fire
     (see AirtableClient.list_active, which selects these rows regardless
     of Enabled).
  2. Not enabled? skip the daily-digest path.
  3. Outside the configured days-out window? skip.
  4. Before today's configured send-time (in ET)? skip.
  5. Already sent today? skip.
  6. No initial briefing yet? skip — daily digests gate on the staff-
     authorized initial briefing so we never auto-fire on a fresh event.
  7. Send daily digest. If silent-when-empty (no new attendees), bail
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
import traceback
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
ADMIN_URL_BASE = "https://pages.centerforcooperativemedia.org/events"

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


def should_send_today(row: EventRow, now: datetime) -> bool:
    if not row.enabled:
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
) -> None:
    logger.info("processing %s (initial=%s)", row.slug, is_initial)

    # Recipient-validity gate: a row missing speaker_emails or lead_host_email
    # must not reach SMTP. Without this guard, an empty `to` list still
    # produces a deliverable message via the always-BCC addresses, and the
    # success path would then mark Airtable as sent — speakers receive
    # nothing while ops sees green.
    if not row.speaker_emails:
        msg = f"speaker_emails empty for row {row.record_id}; refusing to send digest"
        logger.error(msg)
        if not dry_run:
            airtable.record_error(row, msg)
        return
    if not row.lead_host_email:
        msg = f"lead_host_email empty for row {row.record_id}; refusing to send digest"
        logger.error(msg)
        if not dry_run:
            airtable.record_error(row, msg)
        return

    builder = ProfileBuilder(crm, llm, question_id_filter=row.question_ids_to_include or None)

    attendees = list(eb.fetch_attendees(row.eventbrite_event_id))
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
        return

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
        admin_url=f"{ADMIN_URL_BASE}/{row.slug}/admin",
        subject=subject,
        logo_url=logo_url or None,
    )
    html_body = renderer.render(ctx)
    text_body = renderer.render_plain_text(ctx)

    if dry_run:
        logger.info("dry-run: would send %r to %s", subject, row.speaker_emails)
        return

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
    # means the email DID go out on a prior tick, just that the Airtable
    # state write didn't follow. Reconcile Airtable now using the same
    # write path as a successful send. Without this branch, stale Airtable
    # state survives until the 20h ledger window ages out, after which the
    # cron would re-send with the old cursor.
    if result.sent or result.reason == "duplicate":
        if not result.sent:
            logger.warning(
                "ledger duplicate for %s; reconciling Airtable state", row.slug
            )
        max_cursor = max((p.created_at for p in profiles), default=cursor or "")
        if is_initial:
            airtable.update_after_initial_send(
                row, sent_at=now, attendee_cursor=max_cursor, attendee_count=len(profiles)
            )
        else:
            airtable.update_after_send(
                row, sent_at=now, attendee_cursor=max_cursor, attendee_count=len(profiles)
            )


def main(dry_run: bool = False) -> None:
    cfg = load_config()
    lock = _acquire_lock()
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
            ledger=ledger,
        )
        renderer = EmailRenderer()

        now = datetime.now(UTC)
        rows = airtable.list_active()
        logger.info("tick: %d active rows, now=%s", len(rows), now.isoformat())

        for row in rows:
            try:
                if has_pending_initial_briefing(row):
                    _run_briefing(
                        row, eb, crm, llm, renderer, sender, airtable, now,
                        is_initial=True, dry_run=dry_run, logo_url=cfg.logo_url,
                    )
                elif row.enabled and should_send_today(row, now):
                    _run_briefing(
                        row, eb, crm, llm, renderer, sender, airtable, now,
                        is_initial=False, dry_run=dry_run, logo_url=cfg.logo_url,
                    )
            except Exception as e:
                logger.exception("event %s failed", row.slug)
                if not dry_run:
                    airtable.record_error(
                        row, f"{type(e).__name__}: {e}\n{traceback.format_exc()[:1500]}"
                    )
    finally:
        lock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    main(dry_run=args.dry_run)
