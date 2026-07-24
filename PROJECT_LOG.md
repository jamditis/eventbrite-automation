# Project log: Eventbrite automation

## Project overview

Automates the creation of Eventbrite draft listings from Airtable form submissions. When someone submits an event request via Airtable form, this script:

1. Fetches unprocessed records from Airtable (Status: blank, Todo, In progress, or Needs review)
2. Generates a featured image using Gemini 3.0 AI
3. Uploads the image to Eventbrite
4. Creates a draft event on Eventbrite with description and tickets
5. Updates the Airtable record status to "Eventbrite draft created"

## Architecture

```
Airtable Form → Airtable Record → Python Script → Gemini (image) → Eventbrite Draft
                                                                  ↓
                                                         Update Airtable Status
```

## Session history

### 2026-01-28 - Initial implementation

- Created project structure with all modules
- Implemented Airtable client with status-based filtering
- Implemented Eventbrite client for image upload and draft creation
- Integrated Gemini 3.0 for AI image generation

**Key decisions:**
- Using `gemini-3-pro-image-preview` model for image generation
- Images are complete banners with title + subtitle (no logo overlay)
- CCM branding handled by Eventbrite listing itself
- Status field used to track processed records
- Events created as drafts for human review before publishing

**Problems solved:**
- Airtable PAT needed correct base permissions
- Eventbrite image upload requires auth header without Content-Type for GET
- Gemini returns raw bytes, not base64
- Event description uses POST to /events/{id}/ endpoint (not structured_content)

**Image generation iterations:**
- Started with template-based compositing (rigid layout) - rejected
- Moved to full Gemini generation with logo overlay - logos looked bad
- Final: Gemini generates complete image with just title + subtitle, no branding

### 2026-01-28 - First production run

- Ran pipeline on 6 unprocessed Airtable records
- 1 successful draft created (future-dated event)
- 5 failed due to past dates (Eventbrite requires future dates)

**Successful draft:**
- Title: How journalism collaboratives can raise money from small-dollar donors
- URL: https://www.eventbrite.com/e/how-journalism-collaboratives-can-raise-money-from-small-dollar-donors-tickets-1981869870132
- Airtable record marked as "Eventbrite draft created"

**Note:** Events with past start dates cannot be created as Eventbrite drafts. The 5 failures were historical records from 2022-2025 that were in the "unprocessed" status. These should be manually updated in Airtable to a different status.

### 2026-01-28 - Major improvements

**Data validation added:**
- Validates brief_description (checks for empty, URL values)
- Validates full_description (checks for empty)
- Warns about missing start dates
- Shows validation warnings before processing each record

**Eventbrite API fixes:**
- Fixed Summary/Description conflict - Eventbrite won't allow both in same request
- Now uses `structured_content` endpoint for Overview section
- Summary (140 char) set during event creation
- Description content added via structured_content

**Venue support for in-person events:**
- Creates venue in Eventbrite from location field
- Parses location as "Venue Name, City, State" format

**Virtual event handling:**
- Marks events as online
- Outputs reminder to manually add Zoom link to Online Event Page
- Standard Zoom link: https://us06web.zoom.us/j/85076176419

**Image generation improvements:**
- Updated prompt to enforce sentence case (not Title Case or ALL CAPS)
- Added clear examples of correct/incorrect capitalization
- Preserves acronyms (NJNC, NJ, AI, CCM)

**Description formatting:**
- Uses CCM template style with proper HTML structure
- Includes date/time/location details section
- Includes speaker bios section
- Includes contact footer

**Test events created:**
- 2026 NJNC Excellence in local news awards (in-person at TCNJ)
- How to pitch your stories to national outlets (in-person at MSU)
- NJ local news funding strategies webinar (virtual)

**Known limitations:**
- Eventbrite API doesn't support adding Zoom links to Online Event Page
- Virtual events require manual step to add Zoom via dashboard
- Time display depends on how Airtable stores datetime values

### 2026-01-28 - Webhook server for Raspberry Pi

- Created `webhook_server.py` - Flask server to receive Airtable webhooks
- Created `deploy/eventbrite-automation.service` - systemd service file for Pi
- Updated CLAUDE.md with webhook documentation

**Webhook endpoints:**
- `GET /` - Health check
- `POST /webhook/airtable` - Main endpoint for Airtable automation
- `POST /webhook/test` - Test endpoint

**Deployment approach:**
- Raspberry Pi runs webhook server as systemd service
- Airtable automation sends webhook when new record is created
- ngrok or cloudflare tunnel provides external HTTPS access

## Current status

- ✅ Project structure complete
- ✅ Airtable integration working
- ✅ Gemini image generation working
- ✅ Eventbrite draft creation working
- ✅ Full pipeline tested
- ✅ First production run successful
- ✅ Webhook server created
- ✅ Raspberry Pi deployment (live on houseofjawn since 2026-01-29)

## Key files

| File | Purpose |
|------|---------|
| `main.py` | CLI entry point - orchestrates the full pipeline |
| `webhook_server.py` | Flask server for Airtable webhook triggers |
| `config.py` | Configuration constants, field mappings, API keys |
| `airtable_client.py` | Fetches records, filters by status, marks as processed |
| `eventbrite_client.py` | Uploads images, creates drafts, adds descriptions/tickets |
| `image_generator.py` | Generates complete banner images via Gemini AI |
| `test_full_pipeline.py` | End-to-end test with synthetic data |
| `CLAUDE.md` | Project documentation for Claude Code |
| `deploy/` | Deployment files for Raspberry Pi |

## Configuration

### Airtable fields used
- Title of event
- Brief description (140 char) - used as subtitle in image
- Full description - used in Eventbrite description
- Proposed start/end date/time
- Event type (In-person/Virtual)
- Pricing (Free/Paid)
- Speaker info
- Status - tracks processing state

### Status field values
- **Unprocessed:** blank, "Todo", "In progress", "Needs review"
- **After processing:** "Eventbrite draft created"

## Usage

```bash
# Activate virtual environment
# Windows: venv\Scripts\activate

# Process all unprocessed records
python main.py

# Process a specific record
python main.py --record-id recXXX

# Preview what would be processed (no changes made)
python main.py --dry-run

# Test API connections
python main.py --test
```

## Dependencies

- pyairtable - Airtable API client
- google-genai - Gemini AI for image generation
- requests - HTTP requests for Eventbrite API
- python-dotenv - Environment variable management
- Pillow - Image processing and resizing

## API references

- Airtable API: https://airtable.com/developers/web/api/introduction
- Eventbrite API: https://www.eventbrite.com/platform/api
- Gemini API: https://ai.google.dev/gemini-api/docs

## Future enhancements

- Add webhook endpoint for real-time Airtable triggers
- Implement retry logic for API failures
- Add email/Slack notification when draft is created
- Support for recurring events
- Batch processing with progress bar
- Option to use white CCM logo overlay for dark images
