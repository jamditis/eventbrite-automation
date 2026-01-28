# Eventbrite automation

This project automates the creation of Eventbrite draft listings from Airtable form submissions, with AI-generated featured images.

---

## Deployment status (2026-01-28)

**Fully deployed and operational on Raspberry Pi (houseofjawn)**

| Component | Status | Details |
|-----------|--------|---------|
| Webhook endpoint | ✅ | `https://eventbrite.amditis.tech/webhook/airtable` |
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
Pi: Gemini generates banner image
        ↓
Pi: Upload image to Eventbrite
        ↓
Pi: Create draft event with description
        ↓
Pi: Update Airtable status → "Eventbrite draft created"
```

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

# Manual trigger (all unprocessed)
curl -X POST https://eventbrite.amditis.tech/webhook/airtable \
  -H "Content-Type: application/json" \
  -d '{"action": "process_all"}'
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

## Important technical details

### Eventbrite organizer profile

Events are created under the **Center for Cooperative Media** organizer profile (ID: 5988913981), not the Rutgers/RIIPL one (ID: 9325601432). This is configured in `config.py` as `EVENTBRITE_ORGANIZER_ID`.

### Markdown to HTML conversion

The `eventbrite_client.py` converts markdown formatting to HTML for Eventbrite:
- `**bold**` → `<strong>bold</strong>`
- `*italic*` or `_italic_` → `<em>italic</em>`
- `[text](url)` → `<a href="url">text</a>`
- Bullet lists → `<ul><li>...</li></ul>`

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

## Common issues

**Gemini 401 UNAUTHENTICATED:** API key was revoked (Google scans for exposed keys). Generate new key and base64 encode before sharing.

**Eventbrite past date error:** "Start and end dates must be in the future" - test records have old dates.

**Wrong organizer showing:** Check `EVENTBRITE_ORGANIZER_ID` in config.py is set to 5988913981.

**Markdown not converting:** Ensure `_markdown_to_html()` is being called in `_build_description_html()`.

## Manual steps for virtual events

Virtual events require manual addition of Zoom link:
1. Go to Eventbrite dashboard → Edit event → Online event page
2. Click "Add Zoom" or "Link another provider"
3. Add: https://us06web.zoom.us/j/85076176419

## Project documentation

See `PROJECT_LOG.md` for detailed session history, decisions made, and problems solved.
