"""Snapshot tests: render the digest HTML against canonical fixtures and
diff against committed snapshots in tests/snapshots/.

Refreshing snapshots: `UPDATE_SNAPSHOTS=1 pytest tests/test_email_snapshots.py`.
Reviewing snapshots: open `tests/snapshots/*.html` in a browser. Any change
to the rendered output will diff in CI/code review against the committed
snapshot, so subtle layout regressions surface as PR diffs rather than
silently shipping to speakers.
"""
from __future__ import annotations

import os
from pathlib import Path

from digest.email_renderer import EmailRenderer, RenderContext
from digest.profile_builder import AttendeeProfile

SNAP_DIR = Path(__file__).parent / "snapshots"


def _fixture_new_profiles() -> list[AttendeeProfile]:
    return [
        AttendeeProfile(
            eb_attendee_id="100001",
            name="Sarah Smith",
            email="sarah@northjersey.example",
            org="North Jersey Journal",
            role="Editor",
            blurb=(
                "Sarah Smith — Editor at North Jersey Journal. Covers municipal "
                "government and elections."
            ),
            form_qa=[
                {
                    "question": "What do you hope to learn?",
                    "answer": (
                        "How to use AI for budget document review without "
                        "compromising source verification."
                    ),
                }
            ],
            is_known_ccm_contact=True,
            crm_contact_id=42,
            created_at="2026-05-07T10:00:00Z",
        ),
        AttendeeProfile(
            eb_attendee_id="100002",
            name="Marcus Chen",
            email="marcus@example.com",
            org="Independent",
            role="Journalist",
            blurb=(
                "Marcus Chen — Independent journalist, formerly NJ Spotlight. "
                "Focus on housing policy."
            ),
            form_qa=[
                {
                    "question": "What do you hope to learn?",
                    "answer": "Practical workflows for transcript review.",
                }
            ],
            is_known_ccm_contact=False,
            crm_contact_id=None,
            created_at="2026-05-07T11:00:00Z",
        ),
    ]


def _existing_profiles(n: int = 5) -> list[AttendeeProfile]:
    return [
        AttendeeProfile(
            eb_attendee_id=str(100100 + i),
            name=f"Existing Person {i}",
            email=f"e{i}@example.com",
            org=f"Org {i}",
            role="Member",
            blurb=f"Existing Person {i} — Org {i}.",
            form_qa=[],
            is_known_ccm_contact=False,
            crm_contact_id=None,
            created_at=f"2026-05-{(i % 5) + 1:02d}T10:00:00Z",
        )
        for i in range(n)
    ]


def _ctx_daily() -> RenderContext:
    return RenderContext(
        event_title="AI in the newsroom: a training for editors",
        event_when="Friday, May 15, 2026 at 1:00 PM ET",
        event_location="Zoom",
        total_count=7,
        new_attendees=_fixture_new_profiles(),
        existing_attendees=_existing_profiles(5),
        subject="AI in the newsroom — 2 new registrations (7 total)",
        logo_url=None,
        sheet_url="https://docs.google.com/spreadsheets/d/EXAMPLE/edit",
    )


def _ctx_initial() -> RenderContext:
    new = _fixture_new_profiles() + [
        AttendeeProfile(
            eb_attendee_id=str(100200 + i),
            name=f"Initial Person {i}",
            email=f"i{i}@example.com",
            org="Org",
            role="Member",
            blurb=f"Initial Person {i} — Org.",
            form_qa=[],
            is_known_ccm_contact=False,
            crm_contact_id=None,
            created_at="2026-05-01T10:00:00Z",
        )
        for i in range(3)
    ]
    return RenderContext(
        event_title="AI in the newsroom: a training for editors",
        event_when="Friday, May 15, 2026 at 1:00 PM ET",
        event_location="Zoom",
        total_count=len(new),
        new_attendees=new,
        existing_attendees=[],
        subject="AI in the newsroom — initial attendee briefing (5 total)",
        logo_url=None,
        sheet_url="https://docs.google.com/spreadsheets/d/EXAMPLE/edit",
        is_initial=True,
    )


def _check_snapshot(name: str, content: str) -> None:
    path = SNAP_DIR / name
    if os.environ.get("UPDATE_SNAPSHOTS") or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return
    expected = path.read_text()
    assert content == expected, (
        f"Snapshot {name} differs from committed copy. "
        "Re-run with UPDATE_SNAPSHOTS=1 if the change is intentional."
    )


def test_daily_digest_snapshot():
    r = EmailRenderer()
    html = r.render(_ctx_daily())
    _check_snapshot("digest-daily.html", html)


def test_initial_briefing_snapshot():
    r = EmailRenderer()
    html = r.render(_ctx_initial())
    _check_snapshot("digest-initial.html", html)
