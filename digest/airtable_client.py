"""Airtable I/O — list enabled events and update system fields after send.

Field name strings live in `FIELD` so a future Airtable schema rename is a
single-spot edit rather than a grep across modules.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pyairtable import Api


class FIELD:
    SLUG = "Event slug"
    TITLE = "Event title"
    EB_EVENT_ID = "Eventbrite event ID"
    ENABLED = "Enabled"
    SPEAKER_EMAILS = "Speaker emails"
    LEAD_HOST_EMAIL = "Lead host email"
    DAYS_OUT = "Days out to start"
    SEND_TIME_ET = "Send time (ET)"
    SEND_WEEKDAYS = "Send weekdays"
    QUESTION_IDS = "Registration question IDs to include"
    SHEET_URL = "Attendee sheet URL"
    EVENT_START_ET = "Event start (ET)"
    LAST_DIGEST_SENT_AT = "Last digest sent at"
    LAST_ATTENDEE_CURSOR = "Last attendee cursor"
    LAST_DIGEST_COUNT = "Last digest attendee count"
    INITIAL_BRIEFING_SENT_AT = "Initial briefing sent at"
    INITIAL_BRIEFING_REQUESTED_AT = "Initial briefing requested at"
    LAST_ERROR = "Last error"


_LAST_ERROR_MAX = 1000


class EventRowSchemaError(ValueError):
    """Raised when an Airtable row has a value that can't be coerced.

    Carries the record_id so an operator looking at the dashboard knows
    exactly which row to fix.
    """


def _parse_int(value, default: int, *, record_id: str, field_name: str) -> int:
    """Coerce a possibly-stringly-typed Airtable number, with a meaningful error."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise EventRowSchemaError(
            f"record {record_id}: field {field_name!r} not coercible to int "
            f"(got {value!r})"
        ) from e


def _parse_emails(raw: str) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"[,\n]", raw)
    return [p.strip() for p in parts if p.strip()]


def _parse_question_ids(raw: str) -> list[str]:
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


_WEEKDAYS = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


def _parse_send_weekdays(raw: str, *, record_id: str) -> frozenset[int] | None:
    if not raw or not raw.strip():
        return None
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    invalid = [token for token in tokens if token.lower() not in _WEEKDAYS]
    if invalid:
        raise EventRowSchemaError(
            f"record {record_id}: field {FIELD.SEND_WEEKDAYS!r} "
            f"contains unknown weekday {invalid[0]!r}"
        )
    return frozenset(_WEEKDAYS[token.lower()] for token in tokens)


@dataclass
class EventRow:
    record_id: str
    slug: str
    title: str
    eventbrite_event_id: str
    enabled: bool
    speaker_emails: list[str]
    lead_host_email: str
    sheet_url: str
    days_out_to_start: int
    send_time_et: str
    question_ids_to_include: list[str]
    event_start_et: str | None
    last_digest_sent_at: str | None
    last_attendee_cursor: str | None
    last_digest_attendee_count: int
    initial_briefing_sent_at: str | None
    initial_briefing_requested_at: str | None
    last_error: str
    send_weekdays: frozenset[int] | None = None
    raw_fields: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_airtable(cls, record: dict) -> EventRow:
        f = record.get("fields") or {}
        record_id = record["id"]
        return cls(
            record_id=record_id,
            slug=f.get(FIELD.SLUG, "") or "",
            title=f.get(FIELD.TITLE, "") or "",
            eventbrite_event_id=f.get(FIELD.EB_EVENT_ID, "") or "",
            enabled=bool(f.get(FIELD.ENABLED, False)),
            speaker_emails=_parse_emails(f.get(FIELD.SPEAKER_EMAILS, "") or ""),
            lead_host_email=f.get(FIELD.LEAD_HOST_EMAIL, "") or "",
            sheet_url=f.get(FIELD.SHEET_URL, "") or "",
            days_out_to_start=_parse_int(
                f.get(FIELD.DAYS_OUT), 7, record_id=record_id, field_name=FIELD.DAYS_OUT
            ),
            send_time_et=f.get(FIELD.SEND_TIME_ET, "07:00") or "07:00",
            send_weekdays=_parse_send_weekdays(
                f.get(FIELD.SEND_WEEKDAYS, "") or "",
                record_id=record_id,
            ),
            question_ids_to_include=_parse_question_ids(f.get(FIELD.QUESTION_IDS, "") or ""),
            event_start_et=f.get(FIELD.EVENT_START_ET),
            last_digest_sent_at=f.get(FIELD.LAST_DIGEST_SENT_AT),
            last_attendee_cursor=f.get(FIELD.LAST_ATTENDEE_CURSOR),
            last_digest_attendee_count=_parse_int(
                f.get(FIELD.LAST_DIGEST_COUNT),
                0,
                record_id=record_id,
                field_name=FIELD.LAST_DIGEST_COUNT,
            ),
            initial_briefing_sent_at=f.get(FIELD.INITIAL_BRIEFING_SENT_AT),
            initial_briefing_requested_at=f.get(FIELD.INITIAL_BRIEFING_REQUESTED_AT),
            last_error=f.get(FIELD.LAST_ERROR, "") or "",
            raw_fields=dict(f),
        )


class AirtableClient:
    def __init__(self, pat: str, base_id: str, table_name: str = "Events") -> None:
        self._api = Api(pat)
        self._base_id = base_id
        self._table_name = table_name

    @property
    def _table(self):
        return self._api.table(self._base_id, self._table_name)

    def list_enabled(self) -> list[EventRow]:
        records = self._table.all(formula="{Enabled} = TRUE()")
        return [EventRow.from_airtable(r) for r in records]

    def list_active(self) -> list[EventRow]:
        """Rows the cron should consider this tick: enabled OR with a pending
        initial briefing. The latter is included regardless of `Enabled` so
        a staff-requested briefing on a not-yet-enabled draft event still
        fires — the alternative (silent never-fire) is the worst kind of
        trap because the request looks active but nothing happens.
        """
        formula = (
            "OR({Enabled} = TRUE(), "
            "AND({Initial briefing requested at}, NOT({Initial briefing sent at})))"
        )
        records = self._table.all(formula=formula)
        return [EventRow.from_airtable(r) for r in records]

    def list_all(self) -> list[EventRow]:
        return [EventRow.from_airtable(r) for r in self._table.all()]

    def update_after_send(
        self,
        row: EventRow,
        *,
        sent_at: datetime,
        attendee_cursor: str,
        attendee_count: int,
    ) -> None:
        self._table.update(
            row.record_id,
            {
                FIELD.LAST_DIGEST_SENT_AT: sent_at.isoformat(),
                FIELD.LAST_ATTENDEE_CURSOR: attendee_cursor,
                FIELD.LAST_DIGEST_COUNT: attendee_count,
                FIELD.LAST_ERROR: "",
            },
        )

    def update_after_initial_send(
        self,
        row: EventRow,
        *,
        sent_at: datetime,
        attendee_cursor: str,
        attendee_count: int,
    ) -> None:
        """Atomic state write for the initial-briefing path.

        Writes all of: last-sent-at, attendee cursor, attendee count,
        initial-briefing-sent-at, clears initial-briefing-requested-at, and
        clears last-error in a single Airtable update. Avoids the partial-
        failure window where SMTP succeeds and the next tick re-fires the
        initial briefing because only the first of three sequential writes
        landed.
        """
        self._table.update(
            row.record_id,
            {
                FIELD.LAST_DIGEST_SENT_AT: sent_at.isoformat(),
                FIELD.LAST_ATTENDEE_CURSOR: attendee_cursor,
                FIELD.LAST_DIGEST_COUNT: attendee_count,
                FIELD.INITIAL_BRIEFING_SENT_AT: sent_at.isoformat(),
                FIELD.INITIAL_BRIEFING_REQUESTED_AT: None,
                FIELD.LAST_ERROR: "",
            },
        )

    def reconcile_after_send(self, row: EventRow, *, sent_at: datetime) -> None:
        """Record a daily digest as sent WITHOUT advancing the attendee cursor.

        Used on a ledger duplicate-reconcile: the ledger proves an email left
        SMTP on a PRIOR tick, but that email reflected an OLDER cursor than
        this tick's Eventbrite fetch. Advancing the cursor to this tick's fetch
        (as `update_after_send` does) would mark attendees who registered in
        the gap between the real send and now as "covered" though they never
        appeared in any digest — silent data loss (#20).

        So write only `last_digest_sent_at` (to suppress an immediate same-day
        re-send) and clear `last_error`. The cursor/count stay put, so the next
        genuine send re-evaluates new attendees against the unchanged cursor
        and picks up the gap attendees (at the cost of re-showing the prior
        email's attendees once — redundancy is preferable to silent loss).
        """
        self._table.update(
            row.record_id,
            {
                FIELD.LAST_DIGEST_SENT_AT: sent_at.isoformat(),
                FIELD.LAST_ERROR: "",
            },
        )

    def reconcile_after_initial_send(self, row: EventRow, *, sent_at: datetime) -> None:
        """Reconcile the initial-briefing path on a ledger duplicate.

        Marks the briefing sent (`initial_briefing_sent_at`) and clears the
        request so it can't re-fire, and sets `last_digest_sent_at` to suppress
        a same-day daily — but, like `reconcile_after_send`, leaves the
        attendee cursor/count untouched (#20). The first genuine daily digest
        then still covers attendees who registered after the already-sent
        briefing rather than skipping them.
        """
        self._table.update(
            row.record_id,
            {
                FIELD.LAST_DIGEST_SENT_AT: sent_at.isoformat(),
                FIELD.INITIAL_BRIEFING_SENT_AT: sent_at.isoformat(),
                FIELD.INITIAL_BRIEFING_REQUESTED_AT: None,
                FIELD.LAST_ERROR: "",
            },
        )

    def record_error(self, row: EventRow, message: str) -> None:
        self._table.update(row.record_id, {FIELD.LAST_ERROR: message[:_LAST_ERROR_MAX]})

    def mark_initial_briefing_sent(self, row: EventRow, at: datetime) -> None:
        self._table.update(row.record_id, {FIELD.INITIAL_BRIEFING_SENT_AT: at.isoformat()})

    def clear_initial_briefing_request(self, row: EventRow) -> None:
        self._table.update(row.record_id, {FIELD.INITIAL_BRIEFING_REQUESTED_AT: None})
