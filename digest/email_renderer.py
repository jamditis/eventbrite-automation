"""HTML + plain-text email rendering with autoescape XSS guard.

Jinja2 autoescape is on for HTML output — attendee names, form answers,
and CRM-fetched org/role strings are all attacker-influenceable in
principle (someone registers with a `<script>` tag in their name). The
HTML template never uses `{% raw %}` or `|safe`, so every interpolated
value is HTML-escaped before reaching the recipient's webmail client.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .profile_builder import AttendeeProfile

TEMPLATES_DIR = Path(__file__).parent / "templates"


@dataclass(frozen=True)
class RenderContext:
    event_title: str
    event_when: str
    event_location: str
    total_count: int
    new_attendees: list[AttendeeProfile]
    existing_attendees: list[AttendeeProfile]
    admin_url: str
    subject: str
    logo_url: str | None


def _strip_tags(s: str) -> str:
    s = re.sub(r"<style.*?</style>", "", s, flags=re.S | re.I)
    s = re.sub(r"<script.*?</script>", "", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


class EmailRenderer:
    def __init__(self, templates_dir: Path = TEMPLATES_DIR) -> None:
        self._env = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            autoescape=select_autoescape(["html", "j2"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render(self, ctx: RenderContext) -> str:
        tmpl = self._env.get_template("digest.html.j2")
        return tmpl.render(**ctx.__dict__)

    def render_or_none(self, ctx: RenderContext) -> str | None:
        """Silent-when-empty: zero new + zero existing attendees -> no email."""
        if not ctx.new_attendees and not ctx.existing_attendees:
            return None
        return self.render(ctx)

    def render_plain_text(self, ctx: RenderContext) -> str:
        return _strip_tags(self.render(ctx))

    @staticmethod
    def format_subject_daily(event_title: str, new_count: int, total: int) -> str:
        return f"{event_title} — {new_count} new registrations ({total} total)"

    @staticmethod
    def format_subject_initial(event_title: str, total: int) -> str:
        return f"{event_title} — initial attendee briefing ({total} total)"
