"""
Eventbrite API client for creating draft events.

Handles all Eventbrite API interactions including image upload and event creation.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import requests

from config import (
    EVENTBRITE_PRIVATE_TOKEN,
    EVENTBRITE_API_BASE,
    EVENTBRITE_ORGANIZER_ID,
    EVENT_DEFAULTS,
)
from airtable_client import EventRecord


@dataclass
class EventbriteEvent:
    """Represents a created Eventbrite event."""

    event_id: str
    url: str
    name: str
    status: str


class EventbriteClient:
    """Client for interacting with Eventbrite API."""

    def __init__(self):
        self.token = EVENTBRITE_PRIVATE_TOKEN
        self.base_url = EVENTBRITE_API_BASE
        self._organization_id: Optional[str] = None

    @property
    def headers(self) -> dict:
        """Get authorization headers for API requests."""
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    @property
    def organization_id(self) -> str:
        """Get the organization ID (cached after first fetch)."""
        if self._organization_id is None:
            self._organization_id = self._fetch_organization_id()
        return self._organization_id

    def _fetch_organization_id(self) -> str:
        """Fetch the organization ID for the authenticated user."""
        response = requests.get(
            f"{self.base_url}/users/me/organizations/",
            headers=self.headers,
        )
        response.raise_for_status()
        data = response.json()

        if not data.get("organizations"):
            raise ValueError("No organizations found for this account")

        # Use the first organization
        org_id = data["organizations"][0]["id"]
        print(f"Using organization ID: {org_id}")
        return org_id

    def upload_image(self, image_path: Path) -> str:
        """
        Upload an image to Eventbrite (3-step process).

        Args:
            image_path: Path to the image file to upload

        Returns:
            The logo_id to use when creating an event
        """
        print(f"Uploading image: {image_path}")

        # Step 1: Get upload token (use auth header only, no Content-Type)
        auth_headers = {"Authorization": f"Bearer {self.token}"}
        upload_token_response = requests.get(
            f"{self.base_url}/media/upload/",
            headers=auth_headers,
            params={"type": "image-event-logo"},
        )
        upload_token_response.raise_for_status()
        upload_data = upload_token_response.json()

        upload_token = upload_data["upload_token"]
        upload_url = upload_data["upload_url"]
        upload_args = upload_data["upload_data"]

        print(f"Got upload token: {upload_token[:20]}...")

        # Step 2: Upload to S3
        with open(image_path, "rb") as f:
            files = {"file": (image_path.name, f, "image/png")}
            s3_response = requests.post(
                upload_url,
                data=upload_args,
                files=files,
            )
            s3_response.raise_for_status()

        print("Uploaded to S3")

        # Step 3: Notify Eventbrite of completion
        notify_response = requests.post(
            f"{self.base_url}/media/upload/",
            headers=self.headers,
            json={
                "upload_token": upload_token,
                "crop_mask": {
                    "top_left": {"x": 0, "y": 0},
                    "width": 1920,
                    "height": 1080,
                },
            },
        )
        notify_response.raise_for_status()
        media_data = notify_response.json()

        logo_id = media_data["id"]
        print(f"Image uploaded successfully. Logo ID: {logo_id}")
        return logo_id

    def create_draft_event(
        self,
        event: EventRecord,
        logo_id: Optional[str] = None,
    ) -> EventbriteEvent:
        """
        Create a draft event on Eventbrite.

        Args:
            event: EventRecord with event details
            logo_id: Optional logo ID from uploaded image

        Returns:
            EventbriteEvent with the created event details
        """
        # Calculate start and end times (as local Eastern time)
        start_local = self._format_datetime_local(event.start_datetime)

        # Use end_datetime from record if available, otherwise default to +2 hours
        if event.end_datetime:
            end_local = self._format_datetime_local(event.end_datetime)
        elif event.start_datetime:
            end_local = self._format_datetime_local(
                event.start_datetime + timedelta(hours=EVENT_DEFAULTS["default_duration_hours"])
            )
        else:
            end_local = self._format_datetime_local(None)

        # Build event payload
        # Only use brief_description if it's valid (not empty, not a URL)
        if event.has_valid_brief_description:
            summary_text = event.brief_description[:140]
        else:
            summary_text = ""
            print("  Warning: No valid summary available (field is empty or contains URL)")

        if summary_text:
            print(f"  Summary (140 char): {summary_text}")

        # Note: Eventbrite API does not allow both summary AND description in the same request
        # We use summary for the 140-char tagline, and structured_content for the Overview section

        event_payload = {
            "event": {
                "name": {"html": event.title},
                "summary": summary_text if summary_text else None,
                "start": {
                    "timezone": EVENT_DEFAULTS["timezone"],
                    "utc": start_local + "Z",  # Eventbrite requires UTC format
                },
                "end": {
                    "timezone": EVENT_DEFAULTS["timezone"],
                    "utc": end_local + "Z",  # Eventbrite requires UTC format
                },
                "currency": EVENT_DEFAULTS["currency"],
                "online_event": event.is_virtual,
                "listed": False,  # Keep as draft
                "organizer_id": EVENTBRITE_ORGANIZER_ID,  # Use CCM profile, not Rutgers
            }
        }

        # Add venue for in-person events
        if not event.is_virtual and event.location:
            print(f"  Location: {event.location}")
            venue_id = self._create_or_get_venue(event.location)
            if venue_id:
                event_payload["event"]["venue_id"] = venue_id

        # For virtual events, set online event URL
        if event.is_virtual:
            # Standard CCM Zoom link for all virtual events
            event_payload["event"]["online_event"] = True

        # Add logo if provided
        if logo_id:
            event_payload["event"]["logo_id"] = logo_id

        print(f"Creating draft event: {event.title}")

        response = requests.post(
            f"{self.base_url}/organizations/{self.organization_id}/events/",
            headers=self.headers,
            json=event_payload,
        )

        if response.status_code != 200:
            print(f"Eventbrite API error: {response.status_code}")
            print(f"Response: {response.text}")
            response.raise_for_status()

        data = response.json()

        event_id = data["id"]
        event_url = data["url"]

        print(f"Draft event created: {event_url}")

        # Add description
        self._add_description(event_id, event)

        # Create ticket class
        self._create_ticket_class(event_id, event.is_free)

        # Note for virtual events: Zoom link must be added manually
        if event.is_virtual:
            print("\n  [!] MANUAL STEP REQUIRED:")
            print("      Add Zoom link to Online Event Page in Eventbrite dashboard")
            print("      Zoom: https://us06web.zoom.us/j/85076176419")

        return EventbriteEvent(
            event_id=event_id,
            url=event_url,
            name=event.title,
            status="draft",
        )

    def _add_description(self, event_id: str, event: EventRecord) -> None:
        """Add structured content for the Overview section.

        Note: Main description is now added during event creation to avoid
        the SUMMARY_DESCRIPTION_CONFLICT error from Eventbrite API.
        """
        description_html = self._build_description_html(event)

        if not description_html:
            print("  No description content to add")
            return

        # Add structured_content for the Overview section
        self._add_structured_content(event_id, description_html)

    def _add_structured_content(self, event_id: str, description_html: str) -> None:
        """Add structured content to populate the Overview section."""
        # Try different approaches for structured content
        payload = {
            "modules": [
                {
                    "type": "text",
                    "data": {
                        "body": {
                            "text": description_html,
                            "type": "html"
                        }
                    }
                }
            ],
            "purpose": "listing"
        }

        response = requests.post(
            f"{self.base_url}/events/{event_id}/structured_content/",
            headers=self.headers,
            json=payload,
        )

        if response.status_code in [200, 201]:
            print("  Added structured content (Overview)")
        else:
            # Try alternative: access_type instead of purpose
            payload2 = {
                "modules": [
                    {
                        "type": "text",
                        "data": {
                            "body": {
                                "text": description_html,
                                "type": "html"
                            }
                        }
                    }
                ],
                "access_type": "public",
                "purpose": "listing"
            }

            response2 = requests.post(
                f"{self.base_url}/events/{event_id}/structured_content/1/",
                headers=self.headers,
                json=payload2,
            )

            if response2.status_code in [200, 201]:
                print("  Added structured content (Overview)")
            else:
                print(f"  Note: Could not add structured content (Overview section may need manual entry)")

    def _markdown_to_html(self, text: str) -> str:
        """Convert markdown formatting to HTML for Eventbrite.

        Handles:
        - **bold** → <strong>bold</strong>
        - *italic* → <em>italic</em>
        - [link](url) → <a href="url">link</a>
        """
        import re

        # Bold: **text** → <strong>text</strong>
        text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)

        # Italic: *text* or _text_ → <em>text</em>
        # Handle both asterisk and underscore style italics
        text = re.sub(r'(?<!\*)\*([^\*\n]+?)\*(?!\*)', r'<em>\1</em>', text)
        text = re.sub(r'(?<!_)_([^_\n]+?)_(?!_)', r'<em>\1</em>', text)

        # Links: [text](url) → <a href="url">text</a>
        text = re.sub(r'\[([^\]]+)\]\(([^\)]+)\)', r'<a href="\2">\1</a>', text)

        return text

    def _to_eastern(self, dt: Optional[datetime]) -> Optional[datetime]:
        """Convert a datetime to Eastern time.

        Airtable stores datetimes in UTC, but we display them in Eastern time.
        """
        if dt is None:
            return None

        eastern = ZoneInfo("America/New_York")

        # If datetime is timezone-aware (has tzinfo), convert it
        if dt.tzinfo is not None:
            return dt.astimezone(eastern)

        # If naive, assume it's already Eastern (shouldn't happen with Airtable)
        return dt.replace(tzinfo=eastern)

    def _strip_internal_notes(self, text: str) -> str:
        """Remove internal planning notes that shouldn't appear in public listings.

        Strips lines like:
        - Target audience: [internal] ...
        - The goal: [internal] ...
        - Any line containing [internal]
        """
        import re

        if not text:
            return text

        lines = text.split("\n")
        cleaned_lines = []

        for line in lines:
            # Skip lines containing [internal] marker
            if "[internal]" in line.lower():
                continue
            # Skip common internal planning prefixes (case-insensitive)
            line_lower = line.lower().strip()
            if any(line_lower.startswith(prefix) for prefix in [
                "target audience:",
                "the goal:",
                "internal note:",
                "internal:",
                "note to self:",
                "[internal",
            ]):
                continue
            cleaned_lines.append(line)

        # Remove excess blank lines that may result from stripping
        result = "\n".join(cleaned_lines)
        # Collapse multiple newlines into double newlines (paragraph breaks)
        result = re.sub(r'\n{3,}', '\n\n', result)
        return result.strip()

    def _build_description_html(self, event: EventRecord) -> str:
        """Build formatted description HTML using CCM template style."""
        parts = []

        # About this event section
        if event.full_description:
            # Strip internal notes first, then clean up and convert markdown to HTML
            desc_text = self._strip_internal_notes(event.full_description)
            desc_text = self._markdown_to_html(desc_text.strip())
            # Convert newlines to HTML paragraphs
            paragraphs = desc_text.split("\n\n")
            for p in paragraphs:
                p = p.strip()
                if p:
                    # Handle bullet points
                    if p.startswith("- ") or p.lstrip().startswith("- "):
                        lines = p.split("\n")
                        parts.append("<ul>")
                        for line in lines:
                            line = line.strip().lstrip("- ")
                            if line:
                                parts.append(f"<li>{line}</li>")
                        parts.append("</ul>")
                    else:
                        # Regular paragraph - convert single newlines to <br>
                        p = p.replace("\n", "<br>")
                        parts.append(f"<p>{p}</p>")

        # Event details section
        details = []
        if event.start_datetime:
            # Convert UTC to Eastern time for display
            eastern_dt = self._to_eastern(event.start_datetime)
            date_str = eastern_dt.strftime("%A, %B %d, %Y")
            time_str = eastern_dt.strftime("%I:%M %p").lstrip("0")
            details.append(f"📅 Date: {date_str}")
            details.append(f"🕒 Time: {time_str} ET")

        if event.event_type:
            if event.is_virtual:
                details.append("📍 Location: Online (link provided after registration)")
            else:
                location = event.location or "In-person"
                details.append(f"📍 Location: {location}")

        if details:
            parts.append("<hr>")
            # Use HTML entities for emojis to avoid encoding issues
            details_html = "<br>".join(details)
            parts.append(f"<p>{details_html}</p>")

        # Speaker info section
        if event.speaker_info:
            parts.append("<hr>")
            parts.append("<h3>About our speakers</h3>")
            # Strip internal notes, then convert markdown and newlines to proper HTML
            speaker_text = self._strip_internal_notes(event.speaker_info)
            speaker_html = self._markdown_to_html(speaker_text)
            speaker_html = speaker_html.replace("\n\n", "</p><p>").replace("\n", "<br>")
            parts.append(f"<p>{speaker_html}</p>")

        # Footer
        parts.append("<hr>")
        parts.append("<p>Questions? Contact <a href='mailto:info@centerforcooperativemedia.org'>info@centerforcooperativemedia.org</a></p>")

        return "\n".join(parts)

    def _create_ticket_class(self, event_id: str, is_free: bool) -> None:
        """Create a ticket class for the event."""
        payload = {
            "ticket_class": {
                "name": EVENT_DEFAULTS["default_ticket_name"],
                "free": is_free,
                "quantity_total": EVENT_DEFAULTS["default_ticket_quantity"],
            }
        }

        # Add price for paid events
        if not is_free:
            payload["ticket_class"]["cost"] = "USD,2500"  # $25.00

        response = requests.post(
            f"{self.base_url}/events/{event_id}/ticket_classes/",
            headers=self.headers,
            json=payload,
        )

        if response.status_code == 200:
            ticket_type = "free" if is_free else "paid"
            print(f"Created {ticket_type} ticket class")
        else:
            print(f"Warning: Could not create ticket class: {response.text}")

    def _create_or_get_venue(self, location_text: str) -> Optional[str]:
        """Create a venue for the event or return None if creation fails."""
        # Parse location - try to extract components
        # Format expected: "Venue Name, City, State" or just "Venue Name"
        parts = [p.strip() for p in location_text.split(",")]

        venue_name = parts[0] if parts else location_text
        city = parts[1] if len(parts) > 1 else "New Jersey"
        region = parts[2] if len(parts) > 2 else "NJ"

        payload = {
            "venue": {
                "name": venue_name,
                "address": {
                    "city": city,
                    "region": region,
                    "country": "US",
                },
            }
        }

        try:
            response = requests.post(
                f"{self.base_url}/organizations/{self.organization_id}/venues/",
                headers=self.headers,
                json=payload,
            )

            if response.status_code in [200, 201]:
                venue_id = response.json().get("id")
                print(f"  Created venue: {venue_id}")
                return venue_id
            else:
                print(f"  Note: Could not create venue (will show as TBD)")
                return None
        except Exception as e:
            print(f"  Note: Venue creation failed: {e}")
            return None

    def _format_datetime_local(self, dt: Optional[datetime]) -> str:
        """Format datetime for Eventbrite API as local time (without Z suffix).

        Airtable stores times in UTC, but the user enters them thinking in Eastern time.
        We need to treat the stored time as if it were Eastern time.
        """
        if dt is None:
            # Default to tomorrow at 6 PM
            dt = datetime.now().replace(hour=18, minute=0, second=0, microsecond=0)
            dt += timedelta(days=1)

        # Remove timezone info and format as local time (Eventbrite will apply the timezone we specify)
        dt_naive = dt.replace(tzinfo=None)
        return dt_naive.strftime("%Y-%m-%dT%H:%M:%S")


def test_connection():
    """Test the Eventbrite connection."""
    print("Testing Eventbrite connection...")
    client = EventbriteClient()

    # Test organization fetch
    org_id = client.organization_id
    print(f"Organization ID: {org_id}")

    return True


if __name__ == "__main__":
    test_connection()
