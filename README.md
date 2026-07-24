# Airtable → Eventbrite Automation Pipeline

A Python webhook server that automates the creation of Eventbrite event listings from Airtable form submissions, with AI-generated promotional images via Google Gemini.

**Live deployment:** This system runs on a Raspberry Pi and processes events for the [Center for Cooperative Media](https://centerforcooperativemedia.org).

## Two subsystems in this repo

1. **Webhook draft creator** (this README) — Airtable form submission → Eventbrite draft listing with an AI banner. Service `eventbrite-automation.service`, config `.env`.
2. **Attendee digest** — a daily cron that emails event speakers a briefing of who has registered for their session. Service `digest-cron.timer`, config `.env.digest`, code in the `digest/` package. Operations guide: [`docs/operations/digest-runbook.md`](docs/operations/digest-runbook.md).

### Attendee digest first deploy

On houseofjawn, from the repo root:

```bash
cp deploy/env.example .env.digest   # then fill in the values
bash deploy/install-digest.sh       # installs and enables digest-cron.timer
```

The cron fires daily at 07:00 ET. Day-to-day operations, the Airtable state fields, and incident response live in the [digest runbook](docs/operations/digest-runbook.md).

## Architecture overview

```
┌─────────────────┐      ┌──────────────────┐      ┌─────────────────────┐
│  Airtable Form  │──────│    Automation    │──────│  Webhook (POST)     │
│  (new record)   │      │  (script trigger)│      │  Returns 202        │
└─────────────────┘      └──────────────────┘      └──────────┬──────────┘
                                                              │
                         ┌────────────────────────────────────┘
                         │  Background thread
                         ▼
              ┌─────────────────────┐
              │  Gemini AI          │
              │  Generate banner    │──────┐
              └─────────────────────┘      │
                                           ▼
              ┌─────────────────────┐    ┌─────────────────────┐
              │  Eventbrite API     │◄───│  Upload image       │
              │  Create draft event │    │  (3-step process)   │
              └──────────┬──────────┘    └─────────────────────┘
                         │
                         ▼
              ┌─────────────────────┐
              │  Airtable API       │
              │  Update status      │
              └─────────────────────┘
```

**Why async?** Airtable automations timeout after ~30 seconds. Image generation + Eventbrite uploads take 30-45 seconds. The webhook returns `202 Accepted` immediately and processes in a background thread.

## Quick start

```bash
# Clone and setup
git clone https://github.com/jamditis/eventbrite-automation.git
cd eventbrite-automation
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure
cp deploy/.env.example .env
# Edit .env with your API keys

# Run locally
python webhook_server.py --port 5000

# Or use gunicorn for production
gunicorn webhook_server:app -b 0.0.0.0:5000
```

## API credentials required

| Service | Where to get it | Env variable |
|---------|-----------------|--------------|
| Airtable | [airtable.com/create/tokens](https://airtable.com/create/tokens) | `AIRTABLE_PAT`, `AIRTABLE_BASE_ID`, `AIRTABLE_TABLE_ID` |
| Gemini | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | `GEMINI_API_KEY` |
| Eventbrite | [eventbrite.com/platform/api-keys](https://www.eventbrite.com/platform/api-keys) | `EVENTBRITE_PRIVATE_TOKEN` |

## Project structure

```
├── webhook_server.py      # Flask app with async processing
├── main.py                # CLI for batch processing
├── airtable_client.py     # Airtable API wrapper + record models
├── eventbrite_client.py   # Eventbrite API + image upload + markdown→HTML
├── image_generator.py     # Gemini image generation + fallback
├── config.py              # Field mappings, constants, validation
├── templates/
│   └── default-banner.png # Fallback image when AI fails
└── deploy/                # systemd services, setup scripts
```

## Key technical details

### Eventbrite image upload (3-step process)

The Eventbrite API requires a specific flow to upload images:

```python
# 1. Get upload instructions (no Content-Type header!)
GET /media/upload?type=image-event-logo
→ Returns: upload_url, upload_data, upload_token

# 2. Upload to S3 with presigned URL
POST {upload_url}
Body: multipart form with upload_data + file
→ Returns: 204 No Content

# 3. Notify Eventbrite
POST /media/upload
Body: {"upload_token": "...", "crop_mask": {...}}
→ Returns: {"id": "logo_id"}

# 4. Use logo_id when creating event
POST /organizations/{org_id}/events
Body: {..., "event.logo_id": "logo_id"}
```

See `eventbrite_client.py:upload_image()` for the full implementation.

### Gemini image generation

Uses `gemini-3-pro-image-preview` model to generate 2048x1024 banners with embedded title text:

```python
# Prompt structure (simplified)
prompt = f"""
Create a 2048x1024 event banner for: "{title}"

Left half: Event title in large white text on dark background
Right half: Abstract illustration related to the topic

The title should be: "{title}"
Subtitle: "{brief_description}"
"""
```

The image includes the event title directly, eliminating need for text overlay. Falls back to `templates/default-banner.png` if generation fails.

### Airtable field mapping

Fields are mapped in `config.py:AIRTABLE_FIELDS` — that mapping is the
source of truth for the exact column names (several are long form-question
sentences, e.g. the brief description column is literally "Please provide a
brief description of the event to be used for creating the Eventbrite
listing (max 140 characters)"). Friendly labels below:

**Required:**
- `Title of event` - Event name
- Brief description - Eventbrite summary (max 140 chars)
- Full description - Detailed description (markdown supported)
- Proposed start date/time - Event start (UTC)
- Event type (in-person or virtual)
- Free or paid?
- `Status` - Processing status

**Optional (image customization):**
- `Art style` - e.g., "minimalist", "watercolor", "bold geometric"
- `Image prompt` - Additional guidance for Gemini
- `Primary color` - Hex code or color name
- `Secondary color` - Accent color

**Auto-populated:**
- `Eventbrite event ID` - For updates/regeneration
- `Eventbrite URL` - Link to created event
- `Generated images` - Attachment field archiving all images
- `Logs` - Timestamped automation log: errors, fallback-banner notices, and
  partial-success warnings land here so staff never need server access

### Internal notes filtering

Lines containing `[internal]` are stripped from descriptions before creating Eventbrite listings:

```
Target audience: [internal] Collaborative managers and leads
```

This lets you keep planning notes in Airtable without them appearing publicly.

### Markdown → HTML conversion

The `_markdown_to_html()` function in `eventbrite_client.py` converts:
- `**bold**` → `<strong>`
- `*italic*` or `_italic_` → `<em>`
- `[text](url)` → `<a href="url">`
- Bullet lists → `<ul><li>...</li></ul>`

### Timezone handling

Airtable stores all datetimes in UTC. The system converts to Eastern time (`America/New_York`) for display in event descriptions.

## Webhook API

### POST /webhook/airtable

Main endpoint for processing records.

```bash
# Async (default) - returns immediately
curl -X POST http://localhost:5000/webhook/airtable \
  -H "Content-Type: application/json" \
  -d '{"record_id": "recXXX"}'
# Returns 202 Accepted

# Sync mode - waits for completion
curl -X POST http://localhost:5000/webhook/airtable \
  -H "Content-Type: application/json" \
  -d '{"record_id": "recXXX", "sync": true}'

# Process all unprocessed records
curl -X POST http://localhost:5000/webhook/airtable \
  -H "Content-Type: application/json" \
  -d '{"action": "process_all"}'
```

### POST /webhook/regenerate-image

Regenerate AI image for an existing event without creating a new listing.

```bash
curl -X POST http://localhost:5000/webhook/regenerate-image \
  -H "Content-Type: application/json" \
  -d '{"record_id": "recXXX"}'
```

Requires `Eventbrite event ID` field to be populated. Uses current values of image customization fields.

### GET /webhook/status/{record_id}

Check processing status for async requests. Returns status and timestamps
only (no result payload); unknown record IDs return HTTP 404 with
`{"status": "unknown"}`. Status is held in memory, so it resets when the
service restarts — check the Airtable record's Status/Logs fields for the
durable outcome.

```json
{"status": "processing", "started": "2026-01-29T13:03:06.555106"}
{"status": "completed", "started": "...", "completed": "2026-01-29T13:03:36.675291"}
{"status": "failed", "started": "...", "completed": "..."}
```

## Adapting for your own use

### 1. Update field mappings

Edit `config.py:AIRTABLE_FIELDS` to match your Airtable schema:

```python
AIRTABLE_FIELDS = {
    "title": "Your Title Field Name",
    "brief_description": "Your Summary Field",
    # ... etc
}
```

### 2. Configure Eventbrite organizer + organization

Set both IDs in `config.py` — they are different things:

```python
EVENTBRITE_ORGANIZER_ID = "your_organizer_profile_id"   # goes in the event body
EVENTBRITE_ORGANIZATION_ID = "your_organization_id"     # goes in the create-event URL
```

Find your organization ID via:
```bash
curl -H "Authorization: Bearer $EVENTBRITE_PRIVATE_TOKEN" \
  https://www.eventbriteapi.com/v3/users/me/organizations/
```

Pinning the organization matters when a token can see multiple orgs — the
first listed org may not be one the token can create events under (issue #32).

### 3. Customize image generation

Modify `config.py:VISUAL_PROMPT_TEMPLATE` for your brand. Key areas to customize:
- Color palette references
- Style guidance
- Topic/industry context

### 4. Update fallback image

Replace `templates/default-banner.png` with your branded fallback (2160x1080 recommended).

### 5. Set up Airtable automation

Create an Airtable automation with a **Script action** (not webhook):

**Trigger:** When record matches conditions → Status equals "Todo"

**Script:**
```javascript
let recordId = input.config().recordId;

await fetch('https://your-webhook-url/webhook/airtable', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({record_id: recordId})
});

output.set('status', 'sent');
```

Add `recordId` as an input variable mapped to "Record ID" from trigger.

## Deployment

See [`deploy/README.md`](deploy/README.md) for full deployment guide including:
- Raspberry Pi setup
- systemd service configuration
- Cloudflare Tunnel for external access
- Log management (logrotate)

Operations and incident response: [`docs/operations/webhook-runbook.md`](docs/operations/webhook-runbook.md).

Quick deployment:
```bash
# On your server
git clone https://github.com/jamditis/eventbrite-automation.git
cd eventbrite-automation
./deploy/setup.sh
nano .env  # Add your API keys
sudo systemctl enable eventbrite-automation
sudo systemctl start eventbrite-automation
```

## CLI usage

For batch processing or debugging:

```bash
# Process all unprocessed records
python main.py

# Process specific record
python main.py --record-id recXXX

# Dry run (no changes)
python main.py --dry-run

# Test API connections
python main.py --test
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Record Status is "Needs review" | The pipeline failed; the reason is in the record's `Logs` field. Fix and set Status back to "Todo" |
| Airtable timeout | Ensure webhook returns 202 async (default behavior) |
| Gemini 401 | API key was revoked (Google scans for exposed keys). Affected records shipped the default banner — regenerate after fixing |
| Eventbrite past date error | Event dates must be in the future |
| Image upload fails | Check the 3-step upload process, especially no Content-Type on step 1 |
| Wrong timezone | Ensure `_to_eastern()` is called before formatting times |
| Processing status lost | Status dict is in-memory; the Airtable record's Status/Logs fields are the durable record |

Full incident-response guide: [`docs/operations/webhook-runbook.md`](docs/operations/webhook-runbook.md).

## Dependencies

See `requirements.txt` for versions. Highlights:

- Python 3.11+
- Flask + gunicorn (webhook server)
- google-genai (Gemini SDK)
- pyairtable
- requests
- python-dotenv
- Pillow (image handling)
- jinja2 + python-dateutil (attendee digest)
- pytest + ruff (tests and lint)

## License

MIT

## Contributing

Issues and PRs welcome at [github.com/jamditis/eventbrite-automation](https://github.com/jamditis/eventbrite-automation).
