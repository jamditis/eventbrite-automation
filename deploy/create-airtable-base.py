#!/usr/bin/env python3
"""Create the EventDigests Airtable base + Events table for the digest cron.

One-shot, idempotent setup script. Implements Task 3 of the attendee-digest
plan (docs/superpowers/plans/2026-05-08-eventbrite-attendee-digest.md) via the
Airtable metadata API instead of hand-building the schema in the web UI.

The Events table schema mirrors the FIELD constants in digest/airtable_client.py
exactly — if a field name drifts there, it must drift here too, or the cron
silently reads empty values.

Idempotent: if a base with the target name already exists for the token, the
script prints its ID and ensures the stub row, rather than creating a second
base. Safe to re-run.

Usage:
    python deploy/create-airtable-base.py --workspace wspXXXXXXXXXXXXXX

The token comes from `pass show claude/api/airtable` (the njnewscommons
account, which has schema.bases:write). Override with --pat-path if needed.
The workspace ID is the `wsp...` segment in the Airtable workspace URL.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = "https://api.airtable.com/v0"

# dateTime fields store ISO timestamps written by digest code via .isoformat().
# Display timezone is cosmetic for staff; America/New_York matches the "(ET)"
# labels on the fields the cron writes.
_DT_OPTS = {
    "dateFormat": {"name": "iso", "format": "YYYY-MM-DD"},
    "timeFormat": {"name": "24hour", "format": "HH:mm"},
    "timeZone": "America/New_York",
}

# Field order matters: the first field becomes the table's primary field.
# Names must match digest/airtable_client.py FIELD exactly.
EVENTS_FIELDS = [
    {"name": "Event slug", "type": "singleLineText",
     "description": "URL-safe identifier. Required, unique. Primary field."},
    {"name": "Event title", "type": "singleLineText",
     "description": "Display label. Pulled from Eventbrite on first enable."},
    {"name": "Eventbrite event ID", "type": "singleLineText",
     "description": "Numeric ID from the Eventbrite event URL. Required."},
    {"name": "Enabled", "type": "checkbox",
     "options": {"icon": "check", "color": "greenBright"},
     "description": "Kill switch. Cron skips the row when unchecked."},
    {"name": "Speaker emails", "type": "multilineText",
     "description": "Comma- or newline-separated. Goes in the email To: line."},
    {"name": "Lead host email", "type": "email",
     "description": "Reply-To target on the digest email."},
    {"name": "Days out to start", "type": "number", "options": {"precision": 0},
     "description": "Daily digests begin this many days before the event. Default 7."},
    {"name": "Send time (ET)", "type": "singleLineText",
     "description": "HH:MM Eastern. The daily send floor. Default 07:00."},
    {"name": "Registration question IDs to include", "type": "multilineText",
     "description": "Comma-separated Eventbrite question IDs. Empty = include all."},
    {"name": "Event start (ET)", "type": "dateTime", "options": _DT_OPTS,
     "description": "Pulled from Eventbrite, refreshed each tick. Drives window math."},
    {"name": "Last digest sent at", "type": "dateTime", "options": _DT_OPTS,
     "description": "Empty = never sent. Drives the already-sent-today check."},
    {"name": "Last attendee cursor", "type": "singleLineText",
     "description": "ISO timestamp of the newest attendee included in any digest."},
    {"name": "Last digest attendee count", "type": "number", "options": {"precision": 0},
     "description": "Total registered as of the last successful run."},
    {"name": "Initial briefing sent at", "type": "dateTime", "options": _DT_OPTS,
     "description": "Set when the initial briefing fires. Empty = daily digests gated off."},
    {"name": "Initial briefing requested at", "type": "dateTime", "options": _DT_OPTS,
     "description": "Polling signal: admin UI sets this to trigger an immediate run."},
    {"name": "Last error", "type": "multilineText",
     "description": "Populated on failure, cleared on next success. Surfaces in admin UI."},
]

TABLE_DESCRIPTION = (
    "One row per event opted into the attendee-digest automation. "
    "Staff edit the recipient/schedule fields; the cron writes the system fields."
)

STUB_ROW = {
    "Event slug": "test-event-2026-05-08",
    "Event title": "Test event for digest pipeline",
    "Enabled": False,
}


def _pass_get(path: str) -> str:
    """Read a single-line secret from pass."""
    out = subprocess.run(
        ["pass", "show", path], capture_output=True, text=True, check=True
    )
    return out.stdout.splitlines()[0].strip()


def _api(method: str, url: str, token: str, body: dict | None = None) -> dict:
    """Call the Airtable API and return the parsed JSON, raising on HTTP error."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SystemExit(
            f"Airtable API {method} {url} failed: {e.code} {detail}"
        ) from e


def _find_base(token: str, name: str) -> str | None:
    """Return the ID of an existing base with this name, or None.

    GET /meta/bases is paginated: a large account returns an `offset` token
    pointing at the next page. Walk every page before concluding the base is
    absent — otherwise a re-run could miss a base on page 2+ and create a
    duplicate, defeating this script's idempotence guarantee.
    """
    url = f"{API_ROOT}/meta/bases"
    while True:
        result = _api("GET", url, token)
        for base in result.get("bases", []):
            if base.get("name") == name:
                return base["id"]
        offset = result.get("offset")
        if not offset:
            return None
        url = f"{API_ROOT}/meta/bases?offset={urllib.parse.quote(offset)}"


def _ensure_stub_row(token: str, base_id: str, table: str) -> None:
    """Add the integration-test stub row if the table has no rows yet."""
    # The table name goes into the URL path; encode it so a name with spaces
    # or reserved characters (e.g. a non-default --table) builds a valid URL.
    table_path = urllib.parse.quote(table, safe="")
    existing = _api(
        "GET", f"{API_ROOT}/{base_id}/{table_path}?maxRecords=1", token
    )
    if existing.get("records"):
        print("  table already has rows — leaving stub row alone")
        return
    _api(
        "POST", f"{API_ROOT}/{base_id}/{table_path}", token,
        {"records": [{"fields": STUB_ROW}], "typecast": True},
    )
    print(f"  added stub row: {STUB_ROW['Event slug']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", required=True,
        help="Airtable workspace ID (the wsp... segment in the workspace URL)",
    )
    parser.add_argument("--name", default="EventDigests", help="Base name")
    parser.add_argument("--table", default="Events", help="Table name")
    parser.add_argument(
        "--pat-path", default="claude/api/airtable",
        help="pass path to the Airtable PAT (needs schema.bases:write)",
    )
    args = parser.parse_args()

    token = _pass_get(args.pat_path)

    existing = _find_base(token, args.name)
    if existing:
        print(f"Base '{args.name}' already exists: {existing}")
        _ensure_stub_row(token, existing, args.table)
        base_id = existing
    else:
        print(f"Creating base '{args.name}' in workspace {args.workspace} ...")
        created = _api(
            "POST", f"{API_ROOT}/meta/bases", token,
            {
                "name": args.name,
                "workspaceId": args.workspace,
                "tables": [{
                    "name": args.table,
                    "description": TABLE_DESCRIPTION,
                    "fields": EVENTS_FIELDS,
                }],
            },
        )
        base_id = created["id"]
        print(f"  created base: {base_id}")
        _ensure_stub_row(token, base_id, args.table)

    print()
    print("Done. Next steps:")
    print(f"  1. Set AIRTABLE_BASE_ID={base_id} in .env.digest")
    print("  2. Create a base-scoped PAT (data.records:read+write, "
          "schema.bases:read) at https://airtable.com/create/tokens")
    print("     restricted to this base, then: pass insert "
          "claude/api/airtable-eventdigests")
    print("  3. Populate eventbrite-automation/.env.digest on houseofjawn "
          "from deploy/env.example")
    return 0


if __name__ == "__main__":
    sys.exit(main())
