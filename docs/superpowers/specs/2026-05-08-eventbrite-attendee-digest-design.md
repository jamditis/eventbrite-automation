# Eventbrite attendee digest — design spec

**Status:** Approved (brainstorming phase). Awaiting implementation plan.
**Date:** 2026-05-08
**Stakeholders:** Joe Amditis, Cassandra Etienne (associate director of programming and membership, CCM)
**Predecessors:** None — new system. Reuses pieces of `eventbrite-automation`, `houseofjawn-dashboard`, and `ccm-pages`.

## Problem statement

CCM speakers, hosts, and instructors today get inconsistent context about who's registered for their events. Some events have detailed pre-event briefings hand-built by programs staff; some have nothing. Speakers walk into webinars with limited information about the audience and can't tailor their delivery.

We need an automation that emails an event's speakers/hosts a daily digest of registrations, with a one-line professional blurb per attendee, registration form answers for new attendees, and a running tally. Configurable per event from a password-gated web UI. Silent on days when no new attendees registered.

## Decisions captured during brainstorming

| # | Decision | Source |
|---|---|---|
| 1 | Hybrid digest format: full one-liner + form Q&A for new attendees, condensed name+org list for existing | Cassandra |
| 2 | Per-event opt-in only; no category default | Cassandra |
| 3 | Profile blurb depth: one-liner per attendee (name, org, title, one sentence) | Cassandra |
| 4 | One group email per event, all speakers in `To:` | Cassandra |
| 5 | Schedule (days-out + send-time) configurable per event | Cassandra |
| 6 | Privacy posture: LLM enrichment **only** for known CCM contacts (CRM-matched). Unknown attendees get form data only. | Cassandra |
| 7 | First send is manual via "Send initial briefing" button. Daily digests don't fire before initial briefing. | Cassandra |
| 8 | Email envelope: `From: njnewscommons`. `Reply-To:` lead host (per event). `Bcc:` Joe + Cassandra always. | Cassandra |
| 9 | Architecture: migrate `pages.centerforcooperativemedia.org` from GitHub Pages to Cloudflare Pages. Admin path gated by CF Access SSO at the same domain. | Joe |
| 10 | Config storage: Airtable, base `EventDigests`, table `Events` | Joe |
| 11 | LLM mechanism: gemini CLI -p (OAuth, no API key) primary; codex CLI -p fallback; deterministic template if both fail. Never direct API calls. | Joe |
| 12 | v1 scope: multi-event config UI from day one (not phased) | Joe |

## Architecture

### High-level diagram

```
                                 ┌─────────────────────────────────────────┐
                                 │  pages.centerforcooperativemedia.org    │
                                 │       (Cloudflare Pages, post-migration)│
                                 │                                         │
                                 │  /events/[slug]/        ── public ──    │
                                 │  /events/[slug]/admin   ── CF Access ── │
                                 │  /api/airtable/*        ── Functions ── │
                                 └────────┬─────────────────────────┬──────┘
                                          │ (CF Access JWT)         │ (Pages Functions
                                          │                          │  proxies w/ Airtable PAT)
                                          ▼                          ▼
                                  ┌───────────────┐         ┌───────────────────┐
                                  │  CCM staff:   │         │  Airtable base:   │
                                  │  Cassandra,   │         │  EventDigests     │
                                  │  Joe, etc.    │         │  (config + state) │
                                  └───────────────┘         └─────────┬─────────┘
                                                                      │
                                                                      │ (PAT, Pi reads + writes)
                                                                      ▼
        ┌──────────────────────────────────────────────────────────────────────┐
        │  houseofjawn — eventbrite-attendee-digest service                    │
        │                                                                       │
        │  cron every 30 min ─→ for each enabled event row:                    │
        │     1. is now within window + past send-time + not yet sent today?   │
        │     2. fetch /events/{id}/attendees/  (EB API, paginated)            │
        │     3. diff against last-seen cursor in Airtable                     │
        │     4. for each new attendee: form answers + CRM lookup +            │
        │           if-known-CCM gemini CLI -p OAuth enrichment                │
        │     5. render HTML email (hybrid digest)                             │
        │     6. ledger.check_duplicate → SMTP send → ledger.log_send          │
        │     7. update Airtable: last_sent_at, last_attendee_cursor           │
        │     8. silent if no new attendees                                     │
        └──────────────────────────────────────────────────────────────────────┘
```

### Logical components

1. **Eventbrite attendee fetcher** — extends `eventbrite_client.py` (currently zero attendee logic). Calls `GET /events/{id}/attendees/`, walks `pagination.continuation`, returns attendees including `answers[]` and `cancelled` flag.
2. **Profile builder** — for each attendee: pulls form answers, CRM lookup via dashboard, conditional gemini CLI enrichment (CRM-matched only), output validation, fallback chain.
3. **Email renderer + send engine** — Jinja2 HTML template + auto-generated plain-text alternative. SMTP via njnewscommons. `email_ledger` for duplicate guard + send record.
4. **Cron scheduler** — single houseofjawn cron entry, every 30 minutes. Reads Airtable, evaluates per-event window + send-time + already-sent-today + initial-briefing-sent.
5. **Admin UI + Pages Functions proxy** — static HTML form at `/events/[slug]/admin`, behind CF Access. Pages Functions at `/api/airtable/*` hold the Airtable PAT. Public viewer at `/events/[slug]/` shows minimal status.

## Phase 0: Cloudflare Pages migration (prerequisite)

Migrating `pages.centerforcooperativemedia.org` from GitHub Pages to Cloudflare Pages is required because Cloudflare Access cannot enforce on a GitHub Pages origin (CF requires traffic to traverse Cloudflare; GH Pages requires DNS-only mode, breaking that). Phased gate language; no calendar units.

- **Phase 0a — Set up CF Pages + verify on temp `*.pages.dev` URL.** Acceptance: all existing paths render correctly on the preview URL.
- **Phase 0b — DNS cutover.** Acceptance: prod URL serves from CF Pages, TLS valid, no broken routes on the walkthrough.
- **Phase 0c — Soak.** Acceptance: clean CF Pages deploy logs, all existing live paths return 200, at least one CCM staff member other than Joe confirms no regression in their workflow, no inbound bug reports.
- **Phase 1+ — Digest project work begins** when Phase 0c acceptance passes.

Existing live paths to verify: `/`, `/njnewswire/`, `/ecm/` (and `/ecm/awards.html`, `/ecm/speakers.html`, `/ecm/schedule.html`, `/ecm/sponsors.html`), `/jobs/`, `/demoday/`, `/fellowships/`, `/foodaccess/`, `/programs/`, `/njcic/`, `/weekender/`, `/internal-tools/`, `/edit/`, `/reports/`, `/tools/` (submodule).

Submodule handling: CF Pages build settings include `git submodule update --init --recursive`. Rollback path: flip CNAME back to `jamditis.github.io`. Old GH Pages deployment kept until Phase 0c acceptance passes.

Treat as separate PR from any digest code. Reviewable on its own merits by anyone whose work depends on the existing site.

## Data model — Airtable schema

**Base:** `EventDigests`. **Table:** `Events`. One row per event opted into the automation.

### Staff-edited fields

| Field | Type | Notes |
|---|---|---|
| `Event slug` | Single line text (primary) | URL-safe identifier. Required, unique. Drives `/events/[slug]/`. |
| `Event title` | Single line text | Display label. Pulled from EB on first enable, editable. |
| `Eventbrite event ID` | Single line text | Numeric ID from EB URL. Required. |
| `Enabled` | Checkbox | Kill switch. Cron skips when unchecked. Default off. |
| `Speaker emails` | Long text | Comma-separated. Plain text — most speakers are external, no Airtable seats. |
| `Lead host email` | Email | `Reply-To` target. |
| `Days out to start` | Number | Default 7. Daily digests begin this many days before event start. |
| `Send time (ET)` | Single line text, `HH:MM` | Default `07:00`. |
| `Registration question IDs to include` | Long text | Comma-separated EB question IDs. Empty = include all. |

### System fields (cron writes, staff reads, UI doesn't let edit)

| Field | Type | Notes |
|---|---|---|
| `Event start (ET)` | Date + time | Pulled from EB on enable, refreshed each cron tick. Drives window math. |
| `Last digest sent at` | Date + time | Empty = never sent. Used for "already sent today?" check. |
| `Last attendee cursor` | Single line text | ISO timestamp of most-recently-`created` attendee included in any digest. Diff baseline. |
| `Last digest attendee count` | Number | Total registered as of last successful run. |
| `Initial briefing sent at` | Date + time | Set when staff clicks "Send initial briefing now." Empty = button hasn't fired. |
| `Last error` | Long text | Populated on failure, cleared on next success. Surfaces as admin UI banner. |

### Out of schema (deliberately)

- No secrets. Airtable PAT, SMTP password, EB token live elsewhere (`pass`, CF Pages env vars).
- No per-attendee rows. EB API + cursor handle "who's new"; duplicating to Airtable creates sync problems.
- No CRM data. Lookup happens at render time; no caching to Airtable.
- No per-event template overrides in v1.

## Profile builder pipeline

For each attendee returned by EB:

```
EB attendee object
       │
       │ {email, first_name, last_name, answers[], status, cancelled, ...}
       ▼
┌──────────────────────────┐
│ Step 1: Status filter    │  ─── Skip if cancelled=true OR status="Not Attending"
└──────────────┬───────────┘
               │
               ▼
┌──────────────────────────┐
│ Step 2: CRM lookup       │  GET /api/contacts/?search={email}
│ (dashboard API)          │  via X-API-Key, exact email match only
└──────────────┬───────────┘
               │
       ┌───────┴───────┐
       │               │
   match found     no match
       │               │
       ▼               ▼
┌─────────────┐   ┌──────────────────┐
│ Step 3a:    │   │ Step 3b:         │
│ gemini -p   │   │ Form-only blurb  │
│ enrichment  │   │ (deterministic)  │
└──────┬──────┘   └────────┬─────────┘
       │                   │
       └─────────┬─────────┘
                 ▼
       ┌──────────────────┐
       │ AttendeeProfile  │
       │ data class       │
       └──────────────────┘
```

### Step 1 — Status filter

Skip attendees where `cancelled=true` or `status` not in `{Attending, Checked In}`. One-line check before any other work.

### Step 2 — CRM lookup

`GET /api/contacts/?search={email}` via dashboard X-API-Key (`pass show claude/tokens/dashboard-api`). The `search` param matches across email/name/org/role with LIKE; treat as a match only when attendee email **exactly equals** one of `email`, `alt_email`, or `work_email` on a returned contact. Substring matches don't count.

### Step 3a — gemini CLI enrichment (CRM-matched only)

Subprocess call to `gemini -p` with prompt:

```
You are writing one-line professional briefings for the host of a
Center for Cooperative Media event. Output ONE sentence, no preamble,
no quotes, no markdown. Use ONLY the inputs provided. Do not invent facts.
If the inputs are thin, return a sentence using only what's there.

Attendee:
  Name: {name}
  Org/title: {org}, {role}
  CRM notes: {notes}
  Past CCM interactions: {recent_interactions_summary}

Their registration form answers:
{form_answers}

Output:
```

Output validated: single sentence, no markdown, ≤ 200 chars, strip known polite prefixes ("Sure, ", "Here's ", "Here is "). Validation failure or non-zero exit triggers fallback chain.

### Step 3b — Deterministic form-only blurb

```
{name} — {org or "(no org provided)"}{", " + role if role}.{first form answer that looks like a self-description, if any}
```

No LLM call. Used for everyone not in CRM, and as the final fallback for CRM-matched attendees when both LLM CLIs fail.

### Fallback chain for LLM call

1. `gemini -p "..."` (default, OAuth, free tier)
2. If gemini fails or output validation fails: `codex -p "..."` (chatgpt OAuth, no API key)
3. If codex also fails: deterministic template (Step 3b), log warning to `Last error`

Never falls back to direct API calls.

### Output shape

```python
@dataclass
class AttendeeProfile:
    eb_attendee_id: str
    name: str
    email: str
    org: str | None
    role: str | None
    blurb: str             # one-liner from 3a or 3b
    form_qa: list[QA]      # [{question: "What do you hope to learn?", answer: "..."}]
    is_known_ccm_contact: bool
    crm_contact_id: int | None
    created_at: datetime   # from EB; renderer uses for new vs existing partition
```

The renderer doesn't know whether a blurb came from gemini or from the deterministic template.

## Email format

### Subject lines (sentence case, no emojis)

- Daily digest: `{Event title} — {N} new registrations ({total} total)`
- Initial briefing: `{Event title} — initial attendee briefing ({total} total)`

### Send envelope

| Header | Value |
|---|---|
| From | `Center for Cooperative Media <njnewscommons@gmail.com>` |
| To | comma-joined `Speaker emails` from Airtable row |
| Reply-To | `Lead host email` from Airtable row |
| Bcc | `jamditis@gmail.com, etiennec@montclair.edu` |
| List-Unsubscribe | `mailto:njnewscommons@gmail.com?subject=unsubscribe%20{slug}` |

SMTP: `gmail-app-password` from pass (njnewscommons app password). Standard `smtplib.SMTP_SSL("smtp.gmail.com", 465)`.

### Body layout

```
┌──────────────────────────────────────────────────────┐
│  [CCM logo, 120px wide]                              │
│                                                      │
│  AI in the newsroom: a training for editors          │
│  Friday, March 14, 2026 · 1:00 PM ET · Zoom         │
│                                                      │
│  ─────────────────────────────────────────────────   │
│  47 total registered  ·  4 new since yesterday       │
│  ─────────────────────────────────────────────────   │
│                                                      │
│  New registrants                                     │
│                                                      │
│  Sarah Smith — Editor at North Jersey Journal.       │
│    Covers municipal government and elections.        │
│      What do you hope to learn?                      │
│        How to use AI for budget document review      │
│        without compromising source verification.     │
│                                                      │
│  Marcus Chen — Independent journalist, formerly      │
│    NJ Spotlight. Focus on housing policy.            │
│      What do you hope to learn?                      │
│        Practical workflows for transcript review.    │
│                                                      │
│  ─────────────────────────────────────────────────   │
│                                                      │
│  Already registered (43)                             │
│                                                      │
│  Jane Doe — North Jersey Public Radio                │
│  Tom Smith — Asbury Park Press                       │
│  Yuli Delgado — Center for Cooperative Media         │
│  [...40 more, one per line, no blurbs...]           │
│                                                      │
│  ─────────────────────────────────────────────────   │
│                                                      │
│  Manage this digest: [admin link]                    │
│                                                      │
│  Sent by the Center for Cooperative Media            │
└──────────────────────────────────────────────────────┘
```

### Template details

- Mobile-first: outer wrapper `max-width: 600px; margin: 0 auto`, viewport meta tag.
- System font stack only — no web fonts.
- Form Q&A under each new attendee: `<dl>` with `<dt>/<dd>`, indented, smaller font.
- "Already registered" condensed list sorted by `created` ascending (registration order).
- No attendee profile images.
- One CTA at bottom: admin link.
- Plain-text alternative auto-generated from HTML via a `text_from_html()` helper. Not hand-maintained.

### Initial briefing variant

Same template. "New registrants" section contains everyone currently registered; "Already registered" section is suppressed entirely. Subject uses the "initial attendee briefing" form. Fires once, gated by `Initial briefing sent at` field. Resend prompts for explicit confirmation.

## Cron scheduler + send engine

### Cron entry on houseofjawn

```
*/30 * * * * cd /home/jamditis/projects/eventbrite-attendee-digest && \
  /usr/bin/timeout --foreground 600 \
  ./.venv/bin/python -m digest.cron >> /var/log/digest-cron.log 2>&1
```

`timeout --foreground` mandatory per Joe's hard-won lesson. 600s ceiling = 5x worst-case headroom.

### Per-tick decision logic

```python
def cron_tick(now: datetime) -> None:
    rows = airtable.list_enabled_events()
    for row in rows:
        try:
            decide_and_dispatch(row, now)
        except Exception as e:
            airtable.update_row(row.id, {"Last error": format_exc(e)})
            logger.exception(f"failed for event {row.slug}")
            # CRITICAL: continue. One event's failure cannot block others.

def decide_and_dispatch(row, now):
    if row.event_start_et < now:
        return  # event over
    if (row.event_start_et - now).days > row.days_out_to_start:
        return  # outside window
    if not _is_past_send_time_today(row.send_time_et, now):
        return  # not yet today
    if _already_sent_today(row.last_digest_sent_at, now):
        return  # idempotent
    if not row.initial_briefing_sent_at:
        return  # daily digests gated on initial briefing
    run_daily_digest(row, now)
```

### Idempotency rules

1. **One digest per calendar day per event.** Calendar-date-in-ET, not 24h rolling.
2. **Send-time is a floor, not target.** `>= send_time` triggers; missed precision is fine.
3. **No daily digest before initial briefing.** Even if window + send-time match, nothing fires until human-pressed button has set `Initial briefing sent at`.
4. **Email ledger as second-line guard.** Before SMTP send, `email_ledger.check_duplicate(recipient=lead_host, subject, hours=20)`. If duplicate, abort and log warning.

### Send sequence

```
1. Fetch attendees from EB API (paginated until has_more_items=False)
2. Filter cancelled / Not-Attending
3. Partition: new (created > last_attendee_cursor) vs existing
4. If no new attendees AND not first send ever:
       Log "silent: no new attendees" and return.    # SILENT-WHEN-EMPTY
5. For each NEW attendee: profile builder pipeline
6. Render HTML + plain-text alternative
7. email_ledger.check_duplicate → abort if duplicate
8. SMTP send via njnewscommons
9. email_ledger.log_send(...)
10. airtable.update_row(...)
```

### Concurrency / locking

`fcntl.flock` on `/var/run/digest-cron.lock` at top of `digest.cron`. 5s timeout; if can't acquire, log "previous tick still running" and exit. Belt-and-suspenders alongside ledger guard.

### Logging

- File: `/var/log/digest-cron.log`, weekly rotation
- Per-event errors → Airtable `Last error` field → admin UI banner
- Catastrophic failures (Airtable down, SMTP creds rotated, lock failures) → Telegram alert via `~/.claude/workstation/send-actionable-message.py` on second consecutive whole-cron failure

### State

No state file on the Pi. Everything between runs lives in Airtable. Pi-rebuild safe; portable to officejawn with zero migration.

## Admin UI + Pages Functions proxy

### URL structure

| Path | Auth | Purpose |
|---|---|---|
| `/events/` | Public | Index of enabled events (public-safe fields) |
| `/events/[slug]/` | Public | Per-event public viewer (status, count) |
| `/events/[slug]/admin` | CF Access SSO | Per-event config form |
| `/events/new` | CF Access SSO | EB event picker for opt-in |
| `/api/airtable/events` | CF Access SSO + Function | List + create |
| `/api/airtable/events/[slug]` | CF Access SSO + Function | Get + patch + delete |
| `/api/airtable/events/[slug]/initial-briefing` | CF Access SSO + Function | POST: trigger immediate digest |
| `/api/eventbrite/upcoming` | CF Access SSO + Function | List upcoming CCM-organizer events |

### Cloudflare Access policy

One Access application targeting `pages.centerforcooperativemedia.org/events/*/admin`, `/events/new`, `/api/*`. Policy: `email_domain in {centerforcooperativemedia.org, montclair.edu}`. Pages Functions verify the JWT against CF Access JWKS on every request.

### Admin form fields

Status block (read-only): Enabled toggle, Last digest sent, Last digest count, Initial briefing sent + Resend button.

Recipients: Speaker emails (textarea, comma or newline separated), Lead host email.

Schedule: Days before event to start, Send time (ET).

Form questions: Checkbox list of EB questions for this event, with "Refresh from Eventbrite" button.

Buttons: Save, Disable + remove from automation.

Preview pane: in-page iframe rendering "what would this digest look like right now."

### Discovery flow at `/events/new`

1. Page hits `/api/eventbrite/upcoming` → query EB for organizer 5988913981 events with status=`live`, `start.range_start >= now`.
2. List rendered with title, date, current registrant count, "Configure digest" button.
3. Click creates draft Airtable row prefilled with EB event ID, title, start, auto-slug, `Enabled = false`.
4. Redirects to `/events/[slug]/admin` for fleshing out. Explicit `Enabled` toggle required to activate.

### Pages Functions proxy contract

Each `/api/*` route is a small Pages Function that:

1. Reads CF Access JWT from headers, verifies against JWKS endpoint (cached). 401 on invalid.
2. Reads `AIRTABLE_PAT` and `AIRTABLE_BASE_ID` from CF Pages env vars.
3. Forwards to `https://api.airtable.com/v0/{base_id}/Events`.
4. Strips Airtable internal fields (`createdTime`) before responding.

### Initial briefing trigger

`POST /api/airtable/events/[slug]/initial-briefing` writes to a Redis pub/sub channel that houseofjawn subscribes to (existing `brain-coordinator` infrastructure). Cron has small subscriber that triggers immediate digest run for one event row. Falls back to "scheduled run will pick this up within 30 min" message if Redis is unreachable.

(Open question for implementation: whether Redis trigger is worth the complexity vs. polling Airtable for an `Initial briefing requested at` field on every cron tick. Decide in writing-plans phase.)

### Public viewer at `/events/[slug]/`

Minimal: event title, date/time, automation on/off, total registrants, "Daily briefings sent through {date}", admin sign-in link. No attendee names, no speaker emails, no internal config.

### Tech choices

- Plain HTML + small vanilla JS, matches rest of `ccm-pages`. No React, no Vue, no build step.
- Tailwind via CDN.
- No SPA router. Each path is a real file. Refresh-safe, deep-linkable.
- No client-side state library. Form values from Airtable on load, patched on save, full reload after success.

## Error handling

| Failure mode | Detected where | Response | User-visible? |
|---|---|---|---|
| EB API 5xx / timeout | `eventbrite_client.fetch_attendees` | Retry 3x exp backoff (1s/4s/16s); fail row, write `Last error`, continue cron | Yes (admin banner) |
| EB rate limit (429) | Same | Sleep until next tick. No retry within tick | No |
| EB auth (401) | Same | Hard fail tick + Telegram alert | Yes |
| CRM lookup network error | `profile_builder._lookup_crm` | Treat as "not in CRM," skip enrichment, continue | No |
| gemini CLI fail / validation fail | `profile_builder._enrich` | Fall back: codex CLI → deterministic template | No |
| SMTP auth failure | `send_engine.dispatch` | Fail row, `Last error`, Telegram alert | Yes |
| SMTP transient | Same | Retry 3x within tick. Defer if still failing | No |
| Email ledger says duplicate | Same | Abort send, log warning, do NOT update `Last digest sent at` | No |
| Airtable write fails after send | `cron.update_row_after_send` | Log, Telegram alert (ledger has record) | Yes |
| Airtable read fails on tick startup | `cron.list_enabled_events` | Hard fail + Telegram on 2nd consecutive | Yes |
| File lock contention | Cron startup | Log + clean exit | No |
| CF Access JWT verification fails | Pages Function | Return 401, browser sees CF Access login | Yes |
| Pages Function can't reach Airtable | Pages Function | Return 502, UI shows red banner with retry button | Yes |

### Cross-cutting principles

- **One event's failure never blocks others** — `for row in rows: try: ... except: continue`.
- **Fail forward, not backward** — degrade output (less info), don't suppress (no email).
- **Errors visible where staff already look** — admin UI banner is primary surface; Telegram is reserved for engineer-attention failures.
- **No silent suppression** — every caught exception either retries, falls back to documented degraded path, or surfaces to a user. No `except: pass`.

## Testing approach

| Layer | Coverage | Where |
|---|---|---|
| Unit | EB client (mocked HTTP), profile builder (mocked CRM + gemini subprocess), email renderer (snapshot), cron decision logic (table-driven) | `pytest` on houseofjawn pre-deploy + GitHub Actions on push |
| Integration (Pi-side) | Test EB event + test Airtable row → real cron tick → SMTP override writes to local `mail/` dir → assert HTML matches | `make test-integration` |
| Integration (Pages Functions) | Mock Airtable, verify proxy + JWT verification + error paths | Wrangler dev server in CI |
| Smoke | One real CCM event opted in, real `gemini -p`, real attendees, send to test inbox until output looks right, then add real speakers | Phase 1 launch |
| Soak | Watch test event for full cycle: initial briefing → ~3 daily digests → event date passes. Collect anomalies. | Phase 1 launch acceptance |

**No mocks of dashboard CRM in integration tests.** Hit real API (test instance or read-only against prod). Mocked CRM responses fine in unit layer only.

**Snapshot tests for HTML email** — render template against fixture set, save to `tests/snapshots/digest-daily.html` and `tests/snapshots/digest-initial.html`. Future template changes diff against these.

## Out of scope for v1

- CJS2026 conferences (separate organizer, separate pipeline already in flight)
- Multi-track events (one event = one digest in v1)
- Per-event email template overrides
- Attendee opt-out / GDPR-style consent flow (privacy posture is "form data + CRM-known-only enrichment")
- Attendee-facing "who else is registered" view
- Calendar / iCal integration
- Slack notifications instead of email
- "Resend yesterday's digest" button (only initial briefing has manual trigger)
- Auto-disable when event ends (cron skips post-event rows; row stays for history)

## Open questions for implementation phase

- Exact CCM logo asset for email header (verify in `~/projects/ccm-pages`)
- Whether `eventbrite-automation` repo absorbs this code or new sibling repo (probably sibling — different cadence, deps, deploy story)
- Canonical Cassandra address for BCC: `etiennec@montclair.edu` vs `etiennec@mail.montclair.edu`
- Initial briefing trigger: brain-coordinator Redis pub/sub vs polling for Airtable field change

## v1 acceptance criteria

- Phase 0 (CF Pages migration) acceptance passed
- One real CCM event opted into automation, completed initial briefing + one daily digest cycle
- Lead host (CCM staff member, not Joe) confirms email was useful and accurate
- No false-positive sends (digests fired when nothing was new) in cycle
- No false-negative misses (digests didn't fire when something was new) in cycle
- Cassandra confirms admin UI is operable without engineering hand-holding
- Code, tests, deploy automation, and operating runbook committed

## Research notes

Sources consulted during the research phase before approach proposal:

- **Eventbrite v3 API** ([developer.eventbrite.com/platform/docs/attendees](https://www.eventbrite.com/platform/docs/attendees), [api-basics](https://www.eventbrite.com/platform/docs/api-basics), [rate-limits](https://www.eventbrite.com/platform/docs/rate-limits), [status glossary](https://www.eventbrite.com/support/articles/en_US/Troubleshooting/glossary-of-order-and-attendee-statuses-in-event-reports)): `GET /events/{id}/attendees/` returns paginated list with `pagination.continuation` token, max 50 per page. Each attendee object includes `answers[]` array (custom question responses) when event is configured for "collect info from each attendee." Cancellations stay visible with `cancelled` boolean + status string. Rate limit 1,000/hr per token (some tokens get 5,000/hr); per-token, not per-organizer. Webhooks exist (`attendee.updated`, `order.placed`) but are overkill for once-daily-digest cadence.
- **Cloudflare Access on GitHub Pages origin** ([app paths](https://developers.cloudflare.com/cloudflare-one/policies/access/app-paths/), [self-hosted private app](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/non-http/self-hosted-private-app/)): Path-scoping a single domain works for CF Pages or CF Tunnel origins. **GH Pages cannot work** because it requires DNS-only mode (grey-cloud), which prevents CF Access enforcement. Workarounds ranked: (1) move admin to CF-tunneled origin, (2) subdomain split, (3) migrate to CF Pages — chose (3) per design decision.
- **Airtable browser writes** ([Airtable Web API getting started](https://support.airtable.com/docs/getting-started-with-airtables-web-api), [Airtable/airtable_api_proxy](https://github.com/Airtable/airtable_api_proxy), [PAT guide 2025](https://tablescripts.com/blog/article32/how-to-get-your-airtable-api-key-a-complete-guide-for-2025)): PATs are now the only auth method (legacy keys deprecated). Should not be exposed in client-side JS. Standard pattern is backend proxy. Rate limit: 5 req/sec per base, 30s lockout on exceed. Scoped PATs: `data.records:read/write`, `schema.bases:read`, restricted to one base.
- **Codebase prior art** (Explore subagent, 2026-05-08): `eventbrite_client.py` has zero attendee-fetch methods today (only event creation). `check-notifications.py:849` has the silent-when-empty pattern (`if not mentions_found: return []`). State file convention: `~/.claude/workstation/notification-state.json`. Dashboard CRM lookup: `GET /api/contacts/?search=<email>` (`backend/routers/contacts.py:603-668`), session-auth (X-API-Key works for cron); contact model has `email`, `alt_email`, `work_email` fields. Email ledger: `email_ledger.check_duplicate` + `log_send` from `houseofjawn-bot/scheduler/email_ledger.py`. DB: `~/.claude/workstation/sent-emails.db`. `/events/` on `ccm-pages` is currently single-page (index.html + data.js); no per-event folders to match.
- **CLI OAuth state on houseofjawn** (inline check, 2026-05-08): codex CLI 0.129.0 installed, `~/.codex/auth.json` confirms `auth_mode=chatgpt` (OAuth, not API key). Claude CLI 2.1.136. Gemini CLI 0.24.5. `OPENAI_API_KEY` unset (good — would override OAuth). All three available as subprocess fallbacks per design.
