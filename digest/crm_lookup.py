"""Dashboard CRM lookup with exact-email match guard.

The dashboard's contacts search uses LIKE matching, so a query for
sarah@northjersey could substring-match sarah@othersite. We post-filter
on exact email so a wrong-attribution blurb never reaches an email render.
Network errors degrade to None so the digest can still ship form-data
blurbs when the dashboard is briefly unreachable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CrmContact:
    id: int
    name: str
    email: str
    alt_email: str | None
    work_email: str | None
    organization: str
    role: str
    notes: str
    interactions: tuple[dict, ...] = ()

    @classmethod
    def from_api(cls, payload: dict) -> CrmContact:
        return cls(
            id=int(payload["id"]),
            name=payload.get("name") or "",
            email=(payload.get("email") or "").lower(),
            alt_email=((payload.get("alt_email") or "").lower() or None),
            work_email=((payload.get("work_email") or "").lower() or None),
            organization=payload.get("organization") or "",
            role=payload.get("role") or "",
            notes=payload.get("notes") or "",
            interactions=tuple(payload.get("interactions") or []),
        )


class CrmLookup:
    def __init__(self, api_base: str, api_key: str, *, timeout: float = 5.0) -> None:
        self._base = api_base.rstrip("/")
        self._key = api_key
        self._timeout = timeout

    def find_by_email(self, email: str) -> CrmContact | None:
        if not email:
            return None
        email_lower = email.lower()
        try:
            resp = requests.get(
                f"{self._base}/contacts/",
                headers={"X-API-Key": self._key},
                params={"search": email_lower},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            logger.warning("CRM lookup failed for %s: %s", email_lower, e)
            return None

        for item in payload.get("items") or []:
            contact = CrmContact.from_api(item)
            if email_lower in {contact.email, contact.alt_email, contact.work_email}:
                return contact
        return None
