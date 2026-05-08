"""Cron entry point + decision logic for the attendee digest.

Run as: `python -m digest.cron [--dry-run]`

Decision sequence per event row, per tick:
  1. Disabled? skip.
  2. Initial briefing pending (requested but not sent)? send + return.
  3. Outside the configured days-out window? skip.
  4. Before today's configured send-time (in ET)? skip.
  5. Already sent today? skip.
  6. No initial briefing yet? skip — daily digests gate on the staff-
     authorized initial briefing so we never auto-fire on a fresh event.
  7. Send daily digest. If silent-when-empty (no new attendees), bail
     before SMTP without recording state changes.

The lock file (XDG state dir) blocks double-execution if a tick runs
long enough that the next cron tick fires before this one returns.
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
    event_start = datetime.fromisoformat(row.event_start_et.replace("Z", "+00:00"))
    if event_start <= now:
        return False
    days_until = (event_start - now).total_seconds() / 86400
    return days_until <= row.days_out_to_start


def _is_past_send_time_today(send_time_et: str, now: datetime) -> bool:
    h, m = parse_send_time_et(send_time_et)
    now_et = now.astimezone(ET)
    threshold = now_et.replace(hour=h, minute=m, second=0, microsecond=0)
    return now_et >= threshold


def _already_sent_today(last_sent_at: str | None, now: datetime) -> bool:
    if not last_sent_at:
        return False
    last = datetime.fromisoformat(last_sent_at.replace("Z", "+00:00"))
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


def _format_event_when(start_local: str, _timezone: str) -> str:
    """`2026-05-15T13:00:00` + tz string -> `Friday, May 15, 2026 at 1:00 PM ET`."""
    dt = datetime.fromisoformat(start_local)
    return dt.strftime("%A, %B %-d, %Y at %-I:%M %p ET")


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
) -> None:
    logger.info("processing %s (initial=%s)", row.slug, is_initial)
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
        logo_url=None,
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
        session_type="cron",
    )

    if result.sent:
        max_cursor = max((p.created_at for p in profiles), default=cursor or "")
        airtable.update_after_send(
            row, sent_at=now, attendee_cursor=max_cursor, attendee_count=len(profiles)
        )
        if is_initial:
            airtable.mark_initial_briefing_sent(row, at=now)
            airtable.clear_initial_briefing_request(row)


def main(dry_run: bool = False) -> None:
    cfg = load_config()
    lock = _acquire_lock()
    try:
        airtable = AirtableClient(cfg.airtable_pat, cfg.airtable_base_id, cfg.airtable_table_name)
        eb = EventbriteClient(cfg.eventbrite_token)
        crm = CrmLookup(cfg.dashboard_api_base, cfg.dashboard_api_key)
        llm = LLMRunner(cfg.gemini_bin, cfg.codex_bin, cfg.codex_model)

        sys.path.insert(
            0, str(Path.home() / "projects" / "houseofjawn-bot" / "scheduler")
        )
        from email_ledger import check_duplicate, log_send  # type: ignore

        class _LedgerWrapper:
            def check_duplicate(self, recipient, subject, hours=6):
                return check_duplicate(recipient, subject, hours=hours)

            def log_send(self, **kw):
                return log_send(**kw)

        sender = SendEngine(
            smtp_host=cfg.smtp_host,
            smtp_port=cfg.smtp_port,
            smtp_user=cfg.smtp_user,
            smtp_password=cfg.smtp_password,
            from_name=cfg.smtp_from_name,
            from_email=cfg.smtp_from_email,
            bcc_always=cfg.bcc_always,
            ledger=_LedgerWrapper(),
        )
        renderer = EmailRenderer()

        now = datetime.now(UTC)
        rows = airtable.list_enabled()
        logger.info("tick: %d enabled rows, now=%s", len(rows), now.isoformat())

        for row in rows:
            try:
                if has_pending_initial_briefing(row):
                    _run_briefing(
                        row, eb, crm, llm, renderer, sender, airtable, now,
                        is_initial=True, dry_run=dry_run,
                    )
                elif should_send_today(row, now):
                    _run_briefing(
                        row, eb, crm, llm, renderer, sender, airtable, now,
                        is_initial=False, dry_run=dry_run,
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
