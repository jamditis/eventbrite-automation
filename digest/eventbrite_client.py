"""Eventbrite v3 API client. Scoped to attendee + event reads for the digest."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

import requests

API_BASE = "https://www.eventbriteapi.com/v3"


@dataclass(frozen=True)
class EventbriteAttendee:
    id: str
    created: str
    status: str
    cancelled: bool
    refunded: bool
    first_name: str
    last_name: str
    email: str
    name: str
    answers: list[dict] = field(default_factory=list)

    @classmethod
    def from_api(cls, payload: dict) -> EventbriteAttendee:
        profile = payload.get("profile") or {}
        first = profile.get("first_name", "") or ""
        last = profile.get("last_name", "") or ""
        name = (profile.get("name") or "").strip() or f"{first} {last}".strip()
        return cls(
            id=str(payload["id"]),
            created=payload.get("created", ""),
            status=payload.get("status", ""),
            cancelled=bool(payload.get("cancelled", False)),
            refunded=bool(payload.get("refunded", False)),
            first_name=first,
            last_name=last,
            email=profile.get("email", "") or "",
            name=name,
            answers=list(payload.get("answers") or []),
        )


@dataclass(frozen=True)
class EventbriteEvent:
    id: str
    title: str
    start_local: str
    timezone: str

    @classmethod
    def from_api(cls, payload: dict) -> EventbriteEvent:
        return cls(
            id=str(payload["id"]),
            title=(payload.get("name") or {}).get("text", "") or "",
            start_local=(payload.get("start") or {}).get("local", "") or "",
            timezone=(payload.get("start") or {}).get("timezone", "America/New_York"),
        )


class EventbriteClient:
    def __init__(self, token: str, *, timeout: float = 30.0) -> None:
        self._token = token
        self._timeout = timeout

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def fetch_attendees(self, event_id: str) -> Iterator[EventbriteAttendee]:
        """Walk every page of the EB attendees endpoint; yields cancelled too."""
        url = f"{API_BASE}/events/{event_id}/attendees/"
        params: dict = {}
        while True:
            resp = requests.get(
                url, headers=self._headers(), params=params, timeout=self._timeout
            )
            resp.raise_for_status()
            data = resp.json()
            for raw in data.get("attendees") or []:
                yield EventbriteAttendee.from_api(raw)
            pag = data.get("pagination") or {}
            if not pag.get("has_more_items"):
                return
            continuation = pag.get("continuation")
            if not continuation:
                return
            params = {"continuation": continuation}

    def fetch_event(self, event_id: str) -> EventbriteEvent:
        url = f"{API_BASE}/events/{event_id}/"
        resp = requests.get(url, headers=self._headers(), timeout=self._timeout)
        resp.raise_for_status()
        return EventbriteEvent.from_api(resp.json())
