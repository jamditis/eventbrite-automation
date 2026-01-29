# Airtable → Eventbrite Automation Pipeline

A Python webhook server that automates the creation of Eventbrite event listings from Airtable form submissions, with AI-generated promotional images via Google Gemini.

**Live deployment:** This system runs on a Raspberry Pi and processes events for the [Center for Cooperative Media](https://centerforcooperativemedia.org).

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

Fields are mapped in `config.py:AIRTABLE_FIELDS`. The system expects these Airtable columns:

**Required:**
- `Title of event` - Event name
- `Brief description (max 140 chars)` - Eventbrite summary
- `Full description` - Detailed description (markdown supported)
- `Proposed start date/time` - Event start (UTC)
- `Event type` - "Virtual" or "In-person"
- `Free or paid?` - Pricing
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

Check processing status for async requests.

```json
{"status": "processing", "started": "2026-01-29T13:03:06.555106"}
{"status": "completed", "result": {...}, "completed": "2026-01-29T13:03:36.675291"}
{"status": "failed", "result": {"error": "..."}, "completed": "..."}
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

### 2. Configure Eventbrite organizer

Set your organizer ID in `config.py`:

```python
EVENTBRITE_ORGANIZER_ID = "your_organizer_id"
```

Find your organizer ID via:
```bash
curl -H "Authorization: Bearer $EVENTBRITE_PRIVATE_TOKEN" \
  https://www.eventbriteapi.com/v3/users/me/organizations/
```

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
- ngrok / Cloudflare tunnel for external access
- Log management

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
| Airtable timeout | Ensure webhook returns 202 async (default behavior) |
| Gemini 401 | API key was revoked (Google scans for exposed keys) |
| Eventbrite past date error | Event dates must be in the future |
| Image upload fails | Check the 3-step upload process, especially no Content-Type on step 1 |
| Wrong timezone | Ensure `_to_eastern()` is called before formatting times |
| Processing status lost | Status dict is in-memory; check logs if service restarted |

## Dependencies

- Python 3.9+
- Flask + gunicorn
- google-generativeai (Gemini SDK)
- pyairtable
- requests
- python-dotenv
- pytz

## License

MIT

## Contributing

Issues and PRs welcome at [github.com/jamditis/eventbrite-automation](https://github.com/jamditis/eventbrite-automation).
