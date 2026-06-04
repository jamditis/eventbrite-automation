# Eventbrite automation

## Bug-fixing workflow

When a bug is reported, don't immediately attempt to fix it. Instead:

1. **Write a failing test first** that reproduces the bug
2. **Launch subagents** to work on fixing the bug
3. **Verify the fix** by running the test — a passing test proves the bug is fixed

## Workflow orchestration

- **Plan first** — Enter plan mode for non-trivial tasks (3+ steps). If things go sideways, stop and re-plan.
- **Use subagents** — Offload research, exploration, and parallel analysis. One task per subagent.
- **Self-improvement** — After corrections, document lessons. Write rules that prevent the same mistake.
- **Verify before done** — Never mark complete without proof. Run tests, check logs, demonstrate correctness.
- **Demand elegance** — For non-trivial changes, ask "is there a more elegant way?" Skip for simple fixes.
- **Autonomous fixing** — When given a bug: just fix it. Zero hand-holding required.

---

This repo hosts two independent subsystems that share the Eventbrite + Airtable plumbing:

1. **Webhook draft creator** (original) — turns Airtable form submissions into Eventbrite draft listings with AI-generated banners. Runs as `eventbrite-automation.service`, reads `.env`. Documented in the rest of this file.
2. **Attendee digest** (added 2026-05) — a daily cron that emails event speakers a briefing of who has registered. Runs as `digest-cron.timer`, reads `.env.digest`, lives in the `digest/` package. See the section directly below.

---

## Attendee digest

A once-daily cron that sends each opted-in event's speakers a registration briefing: a one-time initial briefing when staff request it, then daily updates that fire only on days with new signups, going silent and auto-stopping after the event.

- **Runs on:** houseofjawn, `digest-cron.timer` (systemd, daily 07:00 ET). A `git pull` on houseofjawn is the deploy — no build step.
- **Code:** `digest/` package at the repo root — `cron.py` (orchestrator), `email_renderer.py`, `send_engine.py`, `airtable_client.py`, `config.py`, `profile_builder.py`, `crm_lookup.py`, `eventbrite_client.py`, `llm_subprocess.py`.
- **Config:** `.env.digest` — NOT `.env` (that belongs to the webhook). Loaded by systemd `EnvironmentFile=` and by `config.py` via python-dotenv.
- **State:** Airtable `EventDigests` base (`app8ok1uOYxcfYffv`), `Events` table — one row per event. Separate from the webhook's base.
- **Send model:** setting `Initial briefing requested at` on a row arms the one-shot initial briefing (fires on the next tick regardless of `Enabled`); checking `Enabled` turns on daily digests. Each clears or advances its own state after sending, so neither repeats.
- **Standing recipients:** every send Bcc's `jamditis@gmail.com`, `etiennec@montclair.edu`, `advinculaa@montclair.edu` and Cc's `info@centerforcooperativemedia.org` (overridable via `BCC_ALWAYS` / `CC_ALWAYS` in `.env.digest`).
- **Email:** SMTP as `njnewscommons@gmail.com`; the cross-session dup safety net is the email ledger at `~/.claude/workstation/sent-emails.db`.
- **Tests:** `venv/bin/python -m pytest tests/digest/` (156 as of 2026-06-04). Includes `test_spec_symbols.py`, which fails the build if the design spec names a `module.method` that no longer exists in `digest/` (the guard against spec drift that issue #27 surfaced).
- **Ops + incident response:** `docs/operations/digest-runbook.md`. Design spec: `docs/superpowers/specs/2026-05-08-eventbrite-attendee-digest-design.md`.

**Live since 2026-06-02:** the first production briefing went to the Pro News Coaches workshop speakers (June 4 event); daily digests enabled.

---

## Deployment status (2026-01-29)

**Webhook draft creator** — fully deployed and operational on Raspberry Pi (houseofjawn)

| Component | Status | Details |
|-----------|--------|---------|
| Webhook endpoint | ✅ | `https://eventbrite.amditis.tech/webhook/airtable` |
| Async processing | ✅ | Returns 202 immediately, processes in background thread |
| Gemini AI images | ✅ | Generates custom banners via `gemini-3-pro-image-preview` |
| Fallback image | ✅ | CCM default banner at `templates/default-banner.png` |
| Eventbrite organizer | ✅ | Center for Cooperative Media (ID: 5988913981) |
| Markdown → HTML | ✅ | Converts `**bold**`, `*italic*`, `[links](url)`, bullet lists |
| Airtable automation | ✅ | Script-based trigger on Status = "Todo" |
| Systemd service | ✅ | `eventbrite-automation.service` with auto-restart |
| Cloudflare Tunnel | ✅ | Public URL via existing `houseofjawn` tunnel |

---

## Quick start

```bash
# Activate virtual environment
source venv/bin/activate

# Process all unprocessed records
python main.py

# Process a specific record
python main.py --record-id recXXX

# Preview what would be processed (no changes)
python main.py --dry-run

# Test API connections
python main.py --test

# Run webhook server locally
python webhook_server.py --port 5000
```

## Architecture

```
Airtable Record (Status: Todo)
        ↓
Airtable Automation (Script action)
        ↓
POST to eventbrite.amditis.tech/webhook/airtable
        ↓
Pi: Returns 202 Accepted immediately (<200ms)
        ↓
Background thread spawned:
  → Gemini generates banner image
  → Upload image to Eventbrite
  → Create draft event with description
  → Update Airtable status → "Eventbrite draft created"
```

### Why async?

Airtable scripts have a ~30 second timeout. The full pipeline (Gemini image generation + Eventbrite uploads) takes 30-45 seconds. The webhook responds immediately with `202 Accepted` and processes in a background thread to avoid timeout errors.

## Key files

| File | Purpose |
|------|---------|
| `main.py` | CLI entry point - orchestrates the full pipeline |
| `webhook_server.py` | Flask/gunicorn server for Airtable webhook triggers |
| `config.py` | Configuration constants, field mappings, organizer ID |
| `airtable_client.py` | Fetches records, filters by status, marks as processed |
| `eventbrite_client.py` | Uploads images, creates drafts, markdown→HTML conversion |
| `image_generator.py` | Generates banners via Gemini AI, fallback to CCM default |
| `templates/default-banner.png` | CCM fallback banner image (2160x1080) |
| `deploy/` | Deployment files for Raspberry Pi |

## Service management

```bash
# Check status
sudo systemctl status eventbrite-automation

# View logs
tail -f /var/log/eventbrite-automation/webhook.log

# Restart service
sudo systemctl restart eventbrite-automation

# Manual trigger (async - returns immediately)
curl -X POST https://eventbrite.amditis.tech/webhook/airtable \
  -H "Content-Type: application/json" \
  -d '{"record_id": "recXXX"}'

# Manual trigger (sync - waits for completion, useful for testing)
curl -X POST https://eventbrite.amditis.tech/webhook/airtable \
  -H "Content-Type: application/json" \
  -d '{"record_id": "recXXX", "sync": true}'

# Check processing status
curl https://eventbrite.amditis.tech/webhook/status/recXXX

# Process all unprocessed records (always sync)
curl -X POST https://eventbrite.amditis.tech/webhook/airtable \
  -H "Content-Type: application/json" \
  -d '{"action": "process_all"}'

# Regenerate image for existing event (async)
curl -X POST https://eventbrite.amditis.tech/webhook/regenerate-image \
  -H "Content-Type: application/json" \
  -d '{"record_id": "recXXX"}'

# Regenerate image (sync - waits for completion)
curl -X POST https://eventbrite.amditis.tech/webhook/regenerate-image \
  -H "Content-Type: application/json" \
  -d '{"record_id": "recXXX", "sync": true}'
```

## Airtable automation setup

The automation uses a **Script action** (not webhook) for better control:

**Trigger:** When record matches conditions → Status equals "Todo"

**Action:** Run a script with this code:
```javascript
let recordId = input.config().recordId;

await fetch('https://eventbrite.amditis.tech/webhook/airtable', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({record_id: recordId})
});

output.set('status', 'sent');
```

**Input variable:** Add `recordId` mapped to "Record ID" from trigger.

## Airtable fields

### Required fields
| Field name | Purpose |
|-----------|---------|
| Title of event | Event title |
| Brief description (max 140 chars) | Eventbrite summary |
| Full description | Event details (internal notes filtered out) |
| Proposed start date/time | Event start |
| Event type | Virtual or In-person |
| Free or paid? | Pricing |
| Status | Processing status |

### Image customization fields (optional)
| Field name | Purpose | Example values |
|-----------|---------|----------------|
| Art style | Style guidance for Gemini | "minimalist", "watercolor", "bold geometric", "retro poster" |
| Image prompt | Additional prompt guidance | "Include imagery of newspapers and digital screens" |
| Primary color | Main color for the design | "#FF5733", "navy blue", "forest green" |
| Secondary color | Accent color | "#38E6CF", "gold", "coral" |

**Form helper text for these fields:**

- **Art style**: Describe the visual style for the event banner image. Leave blank for the default modern/editorial style. Examples: "minimalist", "watercolor", "bold geometric", "retro poster", "illustrated editorial", "abstract shapes", "hand-drawn sketch".

- **Image prompt**: Additional guidance for the AI image generator. Use this to suggest specific imagery, themes, or visual elements that relate to your event. Example: "Include imagery of community newspapers and local landmarks" or "Use visual metaphors related to investigative journalism".

- **Primary color**: Main color for the event banner. Use a hex code (like #2E86AB) or a color name (like "navy blue", "forest green", "coral"). Leave blank to use the default CCM color palette.

- **Secondary color**: Accent color for the event banner. Use a hex code or color name. This color will be used for highlights and secondary elements. Leave blank to use the default palette.

### Eventbrite tracking fields (auto-populated)
| Field name | Purpose |
|-----------|---------|
| Eventbrite event ID | For updating existing events |
| Eventbrite URL | Link to the created event |
| Generated images | Attachment field - archives all generated/regenerated images |

---

## Webhook API

### POST /webhook/airtable

Triggers processing for a record. Returns immediately by default.

**Request:**
```json
{"record_id": "recXXX"}
```

**Response (202 Accepted):**
```json
{
  "status": "accepted",
  "message": "Processing started in background",
  "record_id": "recXXX",
  "check_status": "/webhook/status/recXXX"
}
```

**Options:**
- `"sync": true` - Wait for completion (for testing)
- `"action": "process_all"` - Process all unprocessed records (always sync)

### POST /webhook/regenerate-image

Regenerate the image for an existing Eventbrite event without creating a new listing.

**Requirements:** The record must already have an `Eventbrite event ID` stored (from initial processing).

**Request:**
```json
{"record_id": "recXXX"}
```

**Response (202 Accepted):**
```json
{
  "status": "accepted",
  "message": "Image regeneration started in background",
  "record_id": "recXXX",
  "check_status": "/webhook/status/recXXX"
}
```

**Sync mode (for testing):**
```json
{"record_id": "recXXX", "sync": true}
```

This uses the current values of `Art style`, `Image prompt`, `Primary color`, and `Secondary color` fields from Airtable to generate a new image.

### GET /webhook/status/{record_id}

Check processing status for a record.

**Response (processing):**
```json
{"status": "processing", "started": "2026-01-29T13:03:06.555106"}
```

**Response (completed):**
```json
{
  "status": "completed",
  "result": {"success": true, "eventbrite_url": "https://..."},
  "completed": "2026-01-29T13:03:36.675291"
}
```

**Response (failed):**
```json
{
  "status": "failed",
  "result": {"success": false, "error": "..."},
  "completed": "2026-01-29T13:03:36.675291"
}
```

---

## Important technical details

### Eventbrite organizer profile

Events are created under the **Center for Cooperative Media** organizer profile (ID: 5988913981), not the Rutgers/RIIPL one (ID: 9325601432). This is configured in `config.py` as `EVENTBRITE_ORGANIZER_ID`.

### Internal notes filtering

The system automatically strips internal planning notes from descriptions before creating Eventbrite listings. Lines are removed if they:
- Contain `[internal]` anywhere in the line
- Start with common planning prefixes: `Target audience:`, `The goal:`, `Internal note:`, etc.

This allows Airtable records to contain planning context without it appearing publicly.

### Markdown to HTML conversion

The `eventbrite_client.py` converts markdown formatting to HTML for Eventbrite:
- `**bold**` → `<strong>bold</strong>`
- `*italic*` or `_italic_` → `<em>italic</em>`
- `[text](url)` → `<a href="url">text</a>`
- Bullet lists → `<ul><li>...</li></ul>`

### Timezone handling

Airtable stores all datetimes in UTC. The system converts to Eastern time (America/New_York) for:
- Event description display (via `_to_eastern()` helper)
- Eventbrite API (passes timezone parameter)

If times appear wrong in the overview description, check that `_to_eastern()` is being called before formatting.

### Gemini image generation

- Model: `gemini-3-pro-image-preview`
- Generates complete 2048x1024 banners with title + subtitle
- Falls back to `templates/default-banner.png` if Gemini fails
- API key should be base64 encoded when sharing to avoid Google's automated revocation

### Eventbrite image upload

Three-step process:
1. GET upload token (no Content-Type header)
2. POST image to S3 with token
3. Notify Eventbrite of completion

## Credentials

All credentials in `.env` file (not committed):
- `AIRTABLE_PAT` - Airtable personal access token
- `AIRTABLE_BASE_ID` - appKaCDow7qGjhcOm
- `AIRTABLE_TABLE_ID` - tbliKx6zccSxC2qA4
- `GEMINI_API_KEY` - Google Gemini API key
- `EVENTBRITE_PRIVATE_TOKEN` - Eventbrite private token

## Status field values

- **Unprocessed:** blank, "Todo", "In progress", "Needs review"
- **After processing:** "Eventbrite draft created"
- **Regenerate image:** Set to "Regenerate image" to trigger a new AI image generation. Status automatically resets to "Eventbrite draft created" when complete.

## Common issues

**Airtable REQUEST_TIMEOUT:** Fixed as of 2026-01-29. The webhook now returns `202 Accepted` immediately and processes in a background thread. If you see this error, ensure you're running the latest `webhook_server.py`.

**Gemini 401 UNAUTHENTICATED:** API key was revoked (Google scans for exposed keys). Generate new key and base64 encode before sharing.

**Eventbrite past date error:** "Start and end dates must be in the future" - test records have old dates.

**Wrong organizer showing:** Check `EVENTBRITE_ORGANIZER_ID` in config.py is set to 5988913981.

**Markdown not converting:** Ensure `_markdown_to_html()` is being called in `_build_description_html()`.

**Processing status lost on restart:** The `processing_status` dict is in-memory. If the service restarts mid-processing, status is lost. Check logs for actual results.

## Manual steps for virtual events

Virtual events require manual addition of Zoom link:
1. Go to Eventbrite dashboard → Edit event → Online event page
2. Click "Add Zoom" or "Link another provider"
3. Add: https://us06web.zoom.us/j/85076176419

## Project documentation

See `PROJECT_LOG.md` for detailed session history, decisions made, and problems solved.
