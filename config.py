"""
Configuration constants for Eventbrite automation.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Project paths
PROJECT_ROOT = Path(__file__).parent
TEMPLATES_DIR = PROJECT_ROOT / "templates"
TEMP_DIR = PROJECT_ROOT / "temp"
TEMP_DIR.mkdir(exist_ok=True)

# Airtable configuration
AIRTABLE_PAT = os.getenv("AIRTABLE_PAT")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_ID = os.getenv("AIRTABLE_TABLE_ID")

# Gemini configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-3-pro-image-preview"

# Eventbrite configuration
EVENTBRITE_PRIVATE_TOKEN = os.getenv("EVENTBRITE_PRIVATE_TOKEN")
EVENTBRITE_API_BASE = "https://www.eventbriteapi.com/v3"
# CCM organizer profile ID (not the Rutgers/RIIPL one)
EVENTBRITE_ORGANIZER_ID = "5988913981"

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
}

# Status field values that indicate a record needs processing
UNPROCESSED_STATUSES = ["", "Todo", "In progress", "Needs review"]

# Status value to set after Eventbrite draft is created
PROCESSED_STATUS = "Eventbrite draft created"

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
