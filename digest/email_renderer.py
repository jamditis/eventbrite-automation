"""HTML + plain-text email rendering with autoescape XSS guard.

Jinja2 autoescape is on for HTML output — attendee names, form answers,
and CRM-fetched org/role strings are all attacker-influenceable in
principle (someone registers with a `<script>` tag in their name). The
HTML template never uses `{% raw %}` or `|safe`, so every interpolated
value is HTML-escaped before reaching the recipient's webmail client.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

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
    subject: str
    logo_url: str | None
    # Link to a full attendee Google Sheet. None -> the "view full sheet"
    # button is omitted entirely (the {% if sheet_url %} guard), so an event
    # without a generated sheet still renders a valid briefing.
    sheet_url: str | None = None
    # True for the one-time initial briefing (everyone is new, so per-row "new"
    # badges and "N new since the last update" copy are suppressed as noise).
    # Daily digests pass False and flag genuinely-new rows. Threaded explicitly
    # rather than inferred from whether existing_attendees is empty, which
    # mislabels the first daily after an empty-cursor initial briefing.
    is_initial: bool = False


# Canonical Google Sheets document path: /spreadsheets/d/<id>... (also covers
# the /spreadsheets/d/e/<id>/pubhtml published form). A strict pattern, not a
# bare prefix, so /spreadsheets/../document/... traversal can't slip through.
_SHEETS_DOC_PATH = re.compile(r"^/spreadsheets/d/[A-Za-z0-9_-]")


def _safe_sheet_url(url: str | None) -> str | None:
    """Allowlist the attendee-sheet link before it reaches an href.

    The URL comes from an editable Airtable field, so accept only the canonical
    Google Sheets document shape we generate:
    `https://docs.google.com/spreadsheets/d/<id>...`. This blocks
    javascript:/data: schemes, plain http, off-host phishing links, the
    google.com/url open redirector, non-Sheets Google pages (Docs, Sites),
    path traversal off the Sheets prefix, and embedded control characters that
    `urlparse` would otherwise strip before validating the host. Fail-safe: a
    rejected value simply omits the button.
    """
    if not url:
        return None
    candidate = url.strip()
    # urlparse drops \n\r\t before parsing, so a value like
    # "https://docs.google.com/spreadsheets/d/x\nhttps://evil/" would validate
    # as docs.google.com yet still carry the smuggled URL when echoed verbatim.
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in candidate):
        return None
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    # A '..' segment anywhere lets the browser normalize back off the
    # /spreadsheets/d/ prefix at navigation time (the prefix check only sees the
    # un-normalized path). Reject any dot-segment outright — a real Sheets URL
    # never contains one (ids are [A-Za-z0-9_-], no '.').
    if (
        parsed.scheme == "https"
        and host == "docs.google.com"
        and _SHEETS_DOC_PATH.match(path)
        and ".." not in path.split("/")
    ):
        return candidate
    return None


class EmailRenderer:
    def __init__(self, templates_dir: Path = TEMPLATES_DIR) -> None:
        self._env = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            autoescape=select_autoescape(["html", "j2"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    @staticmethod
    def _table_view(ctx: RenderContext) -> dict:
        """Derive the at-a-glance table vars from the context.

        Columns are the union of registration questions across all attendees,
        in first-seen order — so the table adapts to whatever questions an
        event's form collected (and the digest's question-id filter narrowed
        to) rather than hardcoding any event's specific fields.

        Columns are keyed on the stable Eventbrite question_id (falling back to
        the prompt text when no id is present), so two questions that happen to
        share the same prompt text stay distinct columns instead of collapsing
        and dropping one answer. Each attendee's answers are pre-resolved into a
        {column_key: answer} map here rather than re-scanning form_qa per cell
        in the template.
        """
        all_attendees = list(ctx.new_attendees) + list(ctx.existing_attendees)
        new_ids = {p.eb_attendee_id for p in ctx.new_attendees}

        def _key(qa: dict, label: str) -> str:
            return qa.get("question_id") or label

        columns: list[dict] = []
        seen: set[str] = set()
        for p in all_attendees:
            for qa in p.form_qa:
                if not isinstance(qa, dict):
                    continue
                label = (qa.get("question") or "").strip()
                if not label:
                    continue
                key = _key(qa, label)
                if key not in seen:
                    seen.add(key)
                    columns.append({"key": key, "label": label})

        rows: list[dict] = []
        for p in all_attendees:
            answers: dict[str, str] = {}
            for qa in p.form_qa:
                if not isinstance(qa, dict):
                    continue
                label = (qa.get("question") or "").strip()
                if not label:
                    continue
                key = _key(qa, label)
                # Strip so a whitespace-only answer renders as the em-dash
                # placeholder, not a visually-blank cell. First answer wins per
                # column if an attendee somehow repeats one.
                ans = qa.get("answer")
                answers.setdefault(key, ans.strip() if isinstance(ans, str) else ans)
            rows.append(
                {
                    "profile": p,
                    # Eventbrite can omit a name; never render a blank, unlabeled row.
                    "name": (p.name or "").strip() or "(unnamed)",
                    "is_new": p.eb_attendee_id in new_ids,
                    "answers": answers,
                }
            )

        return {
            "columns": columns,
            "rows": rows,
            "new_count": len(ctx.new_attendees),
        }

    def render(self, ctx: RenderContext) -> str:
        tmpl = self._env.get_template("digest.html.j2")
        ctx_vars = dict(ctx.__dict__)
        # Sanitize before the template ever puts it in an href; the {% if
        # sheet_url %} guard then drops the button + intro clause for bad values.
        ctx_vars["sheet_url"] = _safe_sheet_url(ctx.sheet_url)
        return tmpl.render(**ctx_vars, **self._table_view(ctx))

    def render_or_none(self, ctx: RenderContext) -> str | None:
        """Silent-when-empty: zero new + zero existing attendees -> no email."""
        if not ctx.new_attendees and not ctx.existing_attendees:
            return None
        return self.render(ctx)

    def render_plain_text(self, ctx: RenderContext) -> str:
        """Plain-text alternative built directly from the structured table data.

        Stripping tags out of the HTML table would run headers, names, and
        answers together; building from the same _table_view keeps each
        attendee's answers on their own labeled lines.

        The HTML cells truncate long answers/labels for table layout; this
        plain-text path deliberately does not — there's no width constraint, so
        recipients get the full answer. The shared _table_view means the data
        can't drift; only this presentation choice differs by design.
        """
        tv = self._table_view(ctx)
        people = "person has" if ctx.total_count == 1 else "people have"
        when = ctx.event_when + (f" · {ctx.event_location}" if ctx.event_location else "")
        lines = [ctx.event_title, when, "", f"{ctx.total_count} {people} signed up."]
        safe = _safe_sheet_url(ctx.sheet_url)
        if safe:
            lines.append(f"View full attendee sheet: {safe}")
        lines.append("")
        for row in tv["rows"]:
            suffix = " (new)" if row["is_new"] and not ctx.is_initial else ""
            lines.append(f"{row['name']}{suffix}")
            for col in tv["columns"]:
                lines.append(f"  {col['label']}: {row['answers'].get(col['key']) or '—'}")
            lines.append("")
        return "\n".join(lines).strip()

    @staticmethod
    def format_subject_daily(event_title: str, new_count: int, total: int) -> str:
        return f"{event_title} — {new_count} new registrations ({total} total)"

    @staticmethod
    def format_subject_initial(event_title: str, total: int) -> str:
        return f"{event_title} — initial attendee briefing ({total} total)"
