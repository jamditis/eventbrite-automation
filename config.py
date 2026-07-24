"""
Configuration constants for Eventbrite automation.
"""

import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Project paths
PROJECT_ROOT = Path(__file__).parent
TEMPLATES_DIR = PROJECT_ROOT / "templates"
TEMP_DIR = PROJECT_ROOT / "temp"
TEMP_DIR.mkdir(exist_ok=True)


def _pass(key: str) -> str | None:
    """Read a secret from the pass store. Returns None if not found."""
    try:
        return subprocess.check_output(
            ["/home/jamditis/.claude/pass-get", key],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


# Airtable configuration — secret from pass, non-secret IDs from env
AIRTABLE_PAT = _pass("claude/eventbrite/airtable-pat") or os.getenv("AIRTABLE_PAT")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_ID = os.getenv("AIRTABLE_TABLE_ID")

# Gemini configuration
GEMINI_API_KEY = _pass("claude/eventbrite/gemini-api-key") or os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-3-pro-image-preview"

# Eventbrite configuration
EVENTBRITE_PRIVATE_TOKEN = _pass("claude/eventbrite/eventbrite-token") or os.getenv("EVENTBRITE_PRIVATE_TOKEN")
EVENTBRITE_API_BASE = "https://www.eventbriteapi.com/v3"
# CCM organizer profile ID (not the Rutgers/RIIPL one)
EVENTBRITE_ORGANIZER_ID = "5988913981"
# CCM organization ID — used in the create-event URL. The token's organization
# list now returns a blank-named org first that the token cannot create under
# (403), so we pin CCM rather than relying on list order. See issue #32.
EVENTBRITE_ORGANIZATION_ID = "66857244479"

# CCM brand settings
CCM_BRAND = {
    "name": "Center for Cooperative Media",
    "domain": "centerforcooperativemedia.org",
    "logo_color": "https://centerforcooperativemedia.org/wp-content/uploads/2025/07/ccm-logo-iso-2025.png",
    "logo_white": "https://centerforcooperativemedia.org/wp-content/uploads/2025/07/ccm-logo-iso-white-2025.png",
    "accent_color": "#38E6CF",  # Teal accent
    "fallback_bg": "#F5F7F8",   # Light gray fallback
}

# Image generation settings (Eventbrite 2:1 format)
IMAGE_SETTINGS = {
    "width": 2048,
    "height": 1024,
    "aspect_ratio": "2:1",
    "safe_margins": {"top": 64, "right": 64, "bottom": 64, "left": 64},
}

# Template layout settings
TEMPLATE_LAYOUT = {
    "logo": {
        "x": 106,
        "y": 96,
        "width": 220,
        "height": 220,
    },
    "title": {
        "x": 120,
        "y": 500,
        "width": 1040,
        "height": 420,
        "font_size": 120,
        "line_height": 110,
        "max_lines": 3,
    },
    "main_visual": {
        "x": 1010,
        "y": 110,
        "width": 940,
        "height": 820,
    },
    "left_zone_ratio": 0.52,
    "right_zone_ratio": 0.48,
}

# Default event settings
EVENT_DEFAULTS = {
    "timezone": "America/New_York",
    "currency": "USD",
    "default_duration_hours": 2,
    "default_ticket_quantity": 100,
    "default_ticket_name": "General Admission",
    # Categorize new drafts so they don't land uncategorized in Eventbrite.
    # These IDs fit most CCM events (data and policy briefings for journalists);
    # override per event in the Eventbrite editor when a different bucket fits.
    # Blank either value to skip setting it. IDs are from the Eventbrite
    # taxonomy: format 2 = "Seminar or Talk", category 112 = "Government & Politics".
    "format_id": "2",
    "category_id": "112",
}

# Airtable field mapping
# Maps internal names to actual Airtable field names
AIRTABLE_FIELDS = {
    "title": "Title of event",
    "brief_description": "Please provide a brief description of the event to be used for creating the Eventbrite listing (max 140 characters)",
    "full_description": "Please provide a full description of the event, including the goal of the event, the topics that will be discussed, and any expected takeaways for attendees.",
    "start_datetime": "Proposed start date/time of event",
    "end_datetime": "Proposed end date/time of event",
    "event_type": "Will the event be held in-person or produced virtually?",
    "pricing": "Free or paid event?",
    "speaker_info": "Please provide the names and contact information for invited speakers.",
    "location": "Where will the event be held?",
    "status": "Status",
    "visibility": "Visibility",
    "requester": "Who is requesting the event?",
    # Image generation customization fields
    "art_style": "Art style",  # e.g., "minimalist", "watercolor", "bold geometric"
    "image_prompt": "Image prompt",  # Additional prompt guidance for Gemini
    "primary_color": "Primary color",  # Hex code or color name
    "secondary_color": "Secondary color",  # Hex code or color name
    # Eventbrite tracking
    "eventbrite_event_id": "Eventbrite event ID",  # For updating existing events
    "eventbrite_url": "Eventbrite URL",  # URL of the created event
    # Image archive
    "generated_images": "Generated images",  # Attachment field for image archive
    # Automation logging
    "automation_log": "Logs",  # Long text field for error messages and status updates
}

# Status field values that indicate a record needs processing
UNPROCESSED_STATUSES = ["", "Todo", "In progress", "Needs review"]

# Status value to set after Eventbrite draft is created
PROCESSED_STATUS = "Eventbrite draft created"

# Status value that triggers image regeneration
REGENERATE_STATUS = "Regenerate image"

# Status value set when processing fails, so failures are visible in the
# Airtable Status column (not just the Logs field). "Needs review" is already
# an unprocessed status, so a failed record is picked up again by the next
# process_all run once the underlying issue is fixed.
ERROR_STATUS = os.getenv("ERROR_STATUS", "Needs review")

# Outbound HTTP timeouts (connect, read) in seconds. Every Eventbrite/Airtable
# call must be bounded — an unbounded call can hang a webhook worker thread
# indefinitely and silently stall processing.
HTTP_TIMEOUT = (10, 60)
# S3 image upload moves ~1-2 MB from a Raspberry Pi; give the read side longer.
UPLOAD_TIMEOUT = (10, 180)
# Gemini image generation regularly takes 30-60s; bound it so a stalled call
# fails over to the default banner instead of hanging forever. Milliseconds,
# per google-genai HttpOptions.
GEMINI_TIMEOUT_MS = 180_000

# Opt-in webhook authentication. When true AND a webhook secret is configured,
# POST endpoints require the secret via the X-Webhook-Secret header or a
# "secret" field in the JSON body. Off by default because the deployed
# Airtable automation script does not send a secret yet — enable only after
# updating the script (see docs/operations/webhook-runbook.md).
WEBHOOK_REQUIRE_AUTH = os.getenv("WEBHOOK_REQUIRE_AUTH", "").lower() in ("1", "true", "yes")

# Image prompt template for generating the main visual element
VISUAL_PROMPT_TEMPLATE = """Create a clean, modern illustration or graphic for an event about: "{title}"

Theme context: {brief_description}
Event type: {event_type}

Style requirements:
- Clean, modern, confident aesthetic
- Low visual density with strong use of negative space
- Near-flat design with subtle shadows only if needed
- Professional and polished
- Abstract or symbolic representation of the topic
- NO text, words, letters, or numbers in the image
- NO human faces or photorealistic elements
- Use shapes, icons, or abstract representations

Color guidance:
- Use a cohesive color palette (2-4 colors max)
- Colors should evoke themes of journalism, media, collaboration, community
- Consider teal (#38E6CF) as an accent color
- Background should be clean - solid color or subtle gradient

The image should work as a standalone visual element on the right side of an event banner, with text appearing separately on the left.
"""

# Fallback image settings (solid branded background)
FALLBACK_IMAGE = {
    "background_color": "#000000",
    "accent_stripe_color": "#CA3553",
}


def validate_config():
    """Validate that all required configuration is present."""
    required_vars = [
        ("AIRTABLE_PAT", AIRTABLE_PAT),
        ("AIRTABLE_BASE_ID", AIRTABLE_BASE_ID),
        ("AIRTABLE_TABLE_ID", AIRTABLE_TABLE_ID),
        ("GEMINI_API_KEY", GEMINI_API_KEY),
        ("EVENTBRITE_PRIVATE_TOKEN", EVENTBRITE_PRIVATE_TOKEN),
    ]

    missing = [name for name, value in required_vars if not value]

    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    return True
