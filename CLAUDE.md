# Eventbrite automation

This project automates the creation of Eventbrite draft listings from Airtable form submissions, with AI-generated featured images.

---

## Handoff note (2026-01-28)

### What's done

- All code pushed to GitHub: https://github.com/jamditis/eventbrite-automation
- Deployment files created in `deploy/` folder (setup.sh, systemd services, README)
- GitHub Pages documentation site created in `docs/` folder
- GitHub Pages URL: https://jamditis.github.io/eventbrite-automation/ (enable in repo settings if not live)

### What's left to do on the Raspberry Pi

**1. Clone and run setup script:**
```bash
cd /home/pi
git clone https://github.com/jamditis/eventbrite-automation.git
cd eventbrite-automation
chmod +x deploy/setup.sh
./deploy/setup.sh
```

**2. Create .env file with credentials (copy from local .env):**
```bash
nano .env
# Paste the contents from your Windows .env file
```

**3. Start the webhook service:**
```bash
sudo systemctl enable eventbrite-automation
sudo systemctl start eventbrite-automation
```

**4. Install and configure ngrok:**
```bash
sudo apt update && sudo apt install ngrok
ngrok config add-authtoken YOUR_NGROK_TOKEN
ngrok http 5000
```

**5. Create Airtable automation:**
- Go to: https://airtable.com/appKaCDow7qGjhcOm
- Automations → Create automation
- Trigger: "When record matches conditions" (Status is empty OR "Todo")
- Action: "Send webhook" POST to `https://YOUR_NGROK_URL/webhook/airtable`
- Body: `{"record_id": "{RECORD_ID()}"}`

**6. Test the flow:**
- Create a test event in Airtable
- Watch Pi logs: `journalctl -u eventbrite-automation -f`
- Verify draft appears in Eventbrite

### Pi network info

- WiFi: 192.168.1.89
- Tailscale: 100.122.208.15

---

## Quick start

```bash
# Activate virtual environment
# Windows: venv\Scripts\activate
# Linux/Pi: source venv/bin/activate

# Process all unprocessed records
python main.py

# Process a specific record
python main.py --record-id recXXX

# Preview what would be processed (no changes)
python main.py --dry-run

# Test API connections
python main.py --test

# Run webhook server (for Raspberry Pi deployment)
python webhook_server.py --port 5000
```

## Architecture

```
Airtable Form → Airtable Record → Webhook → Raspberry Pi → Gemini (image) → Eventbrite Draft
                     ↓                                                              ↓
              Airtable Automation                                          Update Airtable Status
              (sends webhook)
```

## Key files

| File | Purpose |
|------|---------|
| `main.py` | CLI entry point - orchestrates the full pipeline |
| `webhook_server.py` | Flask server for Airtable webhook triggers |
| `config.py` | Configuration constants, field mappings, API keys |
| `airtable_client.py` | Fetches records, filters by status, marks as processed |
| `eventbrite_client.py` | Uploads images, creates drafts, adds descriptions/tickets |
| `image_generator.py` | Generates complete banner images via Gemini AI |
| `deploy/` | Deployment files for Raspberry Pi |

## How it works

1. Fetches unprocessed records from Airtable (Status: blank, Todo, In progress, or Needs review)
2. Generates a featured image using Gemini 3.0 AI with event title and subtitle
3. Uploads the image to Eventbrite via their 3-step S3 upload process
4. Creates a draft event on Eventbrite with description and tickets
5. Updates the Airtable record status to "Eventbrite draft created"

## Important technical details

### Airtable

- Uses pyairtable library
- Filters by Status field using OR formula
- Field mappings are in `config.py` under `AIRTABLE_FIELDS`

### Eventbrite

- Image upload is a 3-step process (get token → upload to S3 → notify completion)
- GET request for upload token must NOT include Content-Type header
- Description is added via POST to `/events/{event_id}/` (not structured_content endpoint)

### Gemini image generation

- Model: `gemini-3-pro-image-preview`
- Returns raw bytes (not base64)
- Generates complete 2048x1024 banner with title + subtitle text
- No CCM branding in image (handled by Eventbrite listing itself)

## Status field values

- **Unprocessed:** blank, "Todo", "In progress", "Needs review"
- **After processing:** "Eventbrite draft created"

## Dependencies

- pyairtable - Airtable API client
- google-genai - Gemini AI for image generation
- requests - HTTP requests for Eventbrite API
- python-dotenv - Environment variable management
- Pillow - Image processing and resizing

## Credentials

All credentials are stored in `.env` file:
- `AIRTABLE_PAT` - Airtable personal access token
- `AIRTABLE_BASE_ID` - Airtable base ID
- `AIRTABLE_TABLE_ID` - Airtable table ID
- `GEMINI_API_KEY` - Google Gemini API key
- `EVENTBRITE_PRIVATE_TOKEN` - Eventbrite private token

## Common issues

**Airtable 403 error:** PAT doesn't have access to the base. Generate new PAT with correct base permissions.

**Gemini image errors:** Falls back to simple branded image with title text centered on dark background.

**Eventbrite upload fails:** Check that auth header is correct and image file exists.

## Webhook server (Raspberry Pi)

The webhook server listens for POST requests from Airtable automations and triggers the pipeline.

**Endpoints:**
- `GET /` - Health check
- `POST /webhook/airtable` - Main webhook endpoint
- `POST /webhook/test` - Test endpoint

**Airtable webhook payload:**
```json
{"record_id": "recXXXXXX"}
```

**Or process all unprocessed:**
```json
{"action": "process_all"}
```

## Manual steps for virtual events

Virtual events require manual addition of Zoom link to Online Event Page:
1. Go to Eventbrite dashboard → Edit event → Online event page
2. Click "Add Zoom" or "Link another provider"
3. Add: https://us06web.zoom.us/j/85076176419

## Project documentation

See `PROJECT_LOG.md` for detailed session history, decisions made, and problems solved.
