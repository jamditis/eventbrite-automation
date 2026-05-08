"""Profile builder pipeline — privacy-aware blurb construction.

Pipeline:
  1. Status filter (skip cancelled / not-attending)
  2. CRM lookup (exact email match)
  3a. CRM-matched contact -> gemini->codex LLM blurb (with CRM-supplied context)
  3b. Otherwise (or LLM total failure) -> deterministic template blurb
  4. Output AttendeeProfile dataclass

Privacy invariant (load-bearing): the LLM is NEVER invoked for an attendee
who is not already a known CCM contact. Strangers get a deterministic blurb
assembled from their form answers only — no LLM-invented prose about
people we don't have context on.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .crm_lookup import CrmContact, CrmLookup
from .eventbrite_client import EventbriteAttendee
from .llm_subprocess import LLMRunner

ACTIVE_STATUSES = {"Attending", "Checked In"}


@dataclass(frozen=True)
class AttendeeProfile:
    eb_attendee_id: str
    name: str
    email: str
    org: str | None
    role: str | None
    blurb: str
    form_qa: list[dict] = field(default_factory=list)
    is_known_ccm_contact: bool = False
    crm_contact_id: int | None = None
    created_at: str = ""


_BLURB_PROMPT_TEMPLATE = """\
You are writing one-line professional briefings for the host of a Center for Cooperative Media event. Output ONE sentence, no preamble, no quotes, no markdown. Use ONLY the inputs provided. Do not invent facts. If the inputs are thin, return a sentence using only what's there.

Attendee:
  Name: {name}
  Org/title: {org}, {role}
  CRM notes: {notes}
  Past CCM interactions:
{interactions}

Their registration form answers:
{form_answers}

Output:"""


_ORG_KEYWORDS = ("organization", "company", "newsroom", "outlet", "employer")
_ROLE_KEYWORDS = ("title", "role", "position", "job")


def _summarize_interactions(interactions: Iterable[dict], max_items: int = 3) -> str:
    lines = []
    for i in list(interactions)[:max_items]:
        summary = (i.get("summary") or "").strip()
        if summary:
            lines.append(f"- {summary}")
    return "\n".join(lines) if lines else "(none recorded)"


def _format_form_answers(answers: list[dict]) -> str:
    if not answers:
        return "(none)"
    return "\n".join(
        f"- {a.get('question') or 'Question'}: {a.get('answer') or ''}" for a in answers
    )


def _extract_form_org_role(answers: list[dict]) -> tuple[str, str]:
    org = ""
    role = ""
    for a in answers:
        q = (a.get("question") or "").lower()
        ans = (a.get("answer") or "").strip()
        if not ans:
            continue
        if not org and any(kw in q for kw in _ORG_KEYWORDS):
            org = ans
        elif not role and any(kw in q for kw in _ROLE_KEYWORDS):
            role = ans
    return org, role


def _deterministic_blurb(
    attendee: EventbriteAttendee, contact: CrmContact | None
) -> str:
    name = attendee.name or "(unnamed)"
    if contact:
        org = contact.organization or "(no org)"
        role_suffix = f", {contact.role}" if contact.role else ""
        first_sentence = contact.notes.split(".")[0].strip() if contact.notes else ""
        if first_sentence:
            return f"{name} — {org}{role_suffix}. {first_sentence}."
        return f"{name} — {org}{role_suffix}."

    org_from_form, role_from_form = _extract_form_org_role(attendee.answers)
    org = org_from_form or "(no org provided)"
    role_suffix = f", {role_from_form}" if role_from_form else ""
    return f"{name} — {org}{role_suffix}."


class ProfileBuilder:
    def __init__(
        self,
        crm: CrmLookup,
        llm: LLMRunner,
        *,
        question_id_filter: list[str] | None = None,
    ) -> None:
        self._crm = crm
        self._llm = llm
        self._filter = question_id_filter or None

    def build(self, attendee: EventbriteAttendee) -> AttendeeProfile | None:
        if attendee.cancelled or attendee.status not in ACTIVE_STATUSES:
            return None

        contact = self._crm.find_by_email(attendee.email) if attendee.email else None

        if contact is not None:
            prompt = _BLURB_PROMPT_TEMPLATE.format(
                name=attendee.name,
                org=contact.organization or "(unknown)",
                role=contact.role or "(unknown)",
                notes=contact.notes or "(none)",
                interactions=_summarize_interactions(contact.interactions),
                form_answers=_format_form_answers(attendee.answers),
            )
            llm_blurb = self._llm.run_blurb(prompt)
            blurb = llm_blurb if llm_blurb else _deterministic_blurb(attendee, contact)
        else:
            blurb = _deterministic_blurb(attendee, None)

        qa = list(attendee.answers)
        if self._filter:
            qa = [a for a in qa if a.get("question_id") in self._filter]

        return AttendeeProfile(
            eb_attendee_id=attendee.id,
            name=attendee.name,
            email=attendee.email,
            org=(contact.organization if contact else None),
            role=(contact.role if contact else None),
            blurb=blurb,
            form_qa=qa,
            is_known_ccm_contact=contact is not None,
            crm_contact_id=(contact.id if contact else None),
            created_at=attendee.created,
        )
