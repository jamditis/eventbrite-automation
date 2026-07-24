"""
Eventbrite API client for creating draft events.

Handles all Eventbrite API interactions including image upload and event creation.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from airtable_client import EventRecord
from config import (
    CCM_BRAND,
    EVENT_DEFAULTS,
    EVENTBRITE_API_BASE,
    EVENTBRITE_ORGANIZATION_ID,
    EVENTBRITE_ORGANIZER_ID,
    EVENTBRITE_PRIVATE_TOKEN,
    HTTP_TIMEOUT,
    UPLOAD_TIMEOUT,
)

logger = logging.getLogger("eventbrite.client")


def build_retrying_session() -> requests.Session:
    """A requests session that retries transient failures.

    GET/HEAD retry on 429 and 5xx with exponential backoff. POSTs are NOT
    retried on status/read errors — a create-event POST that timed out may
    have succeeded server-side, and a blind retry would create a duplicate
    draft. Connect errors (no request ever reached the server) are retried
    for all methods.
    """
    retry = Retry(
        total=3,
        connect=3,
        read=2,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


@dataclass
class EventbriteEvent:
    """Represents a created Eventbrite event."""

    event_id: str
    url: str
    name: str
    status: str
    # Non-fatal problems hit while building out the event (missing ticket
    # class, description, venue). The draft exists, but staff should review
    # these before publishing — callers write them to the Airtable log.
    warnings: list[str] = field(default_factory=list)


class EventbriteClient:
    """Client for interacting with Eventbrite API."""

    def __init__(self):
        self.token = EVENTBRITE_PRIVATE_TOKEN
        self.base_url = EVENTBRITE_API_BASE
        self._organization_id: str | None = None
        self.session = build_retrying_session()

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
        """Resolve the CCM organization ID for event creation.

        The token's organization list returns a blank-named org first that the
        token cannot create events under (403), so we do not rely on list order.
        Prefer the pinned CCM organization, fall back to a name match on the CCM
        brand, then the first org. See issue #32.
        """
        response = self.session.get(
            f"{self.base_url}/users/me/organizations/",
            headers=self.headers,
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        orgs = response.json().get("organizations", [])

        if not orgs:
            raise ValueError("No organizations found for this account")

        # 1. Pinned CCM organization, if the token can see it.
        if EVENTBRITE_ORGANIZATION_ID:
            for org in orgs:
                if org.get("id") == EVENTBRITE_ORGANIZATION_ID:
                    logger.info("Using organization ID: %s (%s)", org["id"], org.get("name"))
                    return org["id"]
            raise ValueError(
                f"Configured organization {EVENTBRITE_ORGANIZATION_ID} is not "
                "accessible to this token; check the token's account access."
            )

        # 2. Fall back to the CCM brand name, then the first org.
        for org in orgs:
            if org.get("name") == CCM_BRAND["name"]:
                logger.info("Using organization ID: %s (%s)", org["id"], org.get("name"))
                return org["id"]

        org_id = orgs[0]["id"]
        logger.info("Using organization ID: %s (fallback to first org)", org_id)
        return org_id

    def upload_image(self, image_path: Path) -> str:
        """
        Upload an image to Eventbrite (3-step process).

        Args:
            image_path: Path to the image file to upload

        Returns:
            The logo_id to use when creating an event
        """
        logger.info("Uploading image: %s", image_path)

        # Step 1: Get upload token (use auth header only, no Content-Type)
        auth_headers = {"Authorization": f"Bearer {self.token}"}
        upload_token_response = self.session.get(
            f"{self.base_url}/media/upload/",
            headers=auth_headers,
            params={"type": "image-event-logo"},
            timeout=HTTP_TIMEOUT,
        )
        upload_token_response.raise_for_status()
        upload_data = upload_token_response.json()

        upload_token = upload_data["upload_token"]
        upload_url = upload_data["upload_url"]
        upload_args = upload_data["upload_data"]

        # Step 2: Upload to S3
        with open(image_path, "rb") as f:
            files = {"file": (image_path.name, f, "image/png")}
            s3_response = self.session.post(
                upload_url,
                data=upload_args,
                files=files,
                timeout=UPLOAD_TIMEOUT,
            )
            s3_response.raise_for_status()

        logger.info("Uploaded to S3")

        # Step 3: Notify Eventbrite of completion
        notify_response = self.session.post(
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
            timeout=HTTP_TIMEOUT,
        )
        notify_response.raise_for_status()
        media_data = notify_response.json()

        logo_id = media_data["id"]
        logger.info("Image uploaded successfully. Logo ID: %s", logo_id)
        return logo_id

    def create_draft_event(
        self,
        event: EventRecord,
        logo_id: str | None = None,
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
            logger.warning("No valid summary available (field is empty or contains URL)")

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

        # Categorize the draft so it doesn't land uncategorized (the create API
        # otherwise leaves format and category blank, so every event had to be
        # sorted by hand). Defaults suit most CCM events; edit in the Eventbrite
        # editor when a specific event fits a different bucket.
        if EVENT_DEFAULTS.get("format_id"):
            event_payload["event"]["format_id"] = EVENT_DEFAULTS["format_id"]
        if EVENT_DEFAULTS.get("category_id"):
            event_payload["event"]["category_id"] = EVENT_DEFAULTS["category_id"]

        # Collect non-fatal problems so the caller can surface them in Airtable.
        warnings: list[str] = []

        # Add venue for in-person events
        if not event.is_virtual and event.location:
            logger.info("Location: %s", event.location)
            venue_id = self._create_or_get_venue(event.location)
            if venue_id:
                event_payload["event"]["venue_id"] = venue_id
            else:
                warnings.append(f"Could not create venue for '{event.location}' — location will show as TBD")

        # For virtual events, set online event URL
        if event.is_virtual:
            # Standard CCM Zoom link for all virtual events
            event_payload["event"]["online_event"] = True

        # Add logo if provided
        if logo_id:
            event_payload["event"]["logo_id"] = logo_id

        logger.info("Creating draft event: %s", event.title)

        try:
            response = self.session.post(
                f"{self.base_url}/organizations/{self.organization_id}/events/",
                headers=self.headers,
                json=event_payload,
                timeout=HTTP_TIMEOUT,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            # Ambiguous outcome: the POST may have succeeded server-side before
            # the connection died. Reconcile by looking for a draft with this
            # exact title — if one exists, adopt it instead of failing (a
            # failure here would leave Airtable without the event ID, and a
            # retry would create a duplicate draft).
            logger.warning(
                "Create-event request failed ambiguously (%s); "
                "checking for an existing draft to adopt", e,
            )
            adopted = self.find_draft_by_title(event.title)
            if adopted is None:
                raise
            event_id, event_url = adopted
            logger.warning("Adopted existing draft %s after ambiguous create", event_id)
            warnings.append(
                "Create request timed out but the draft already existed and was "
                "adopted — double-check the listing in Eventbrite"
            )
        else:
            if response.status_code != 200:
                logger.error(
                    "Eventbrite create-event API error %s: %s",
                    response.status_code, response.text,
                )
                response.raise_for_status()

            data = response.json()
            event_id = data["id"]
            event_url = data["url"]

        logger.info("Draft event created: %s", event_url)

        # Enrichment below is best-effort: the draft already exists, so a
        # transport error here must degrade to a warning — raising would lose
        # the event ID and set up a duplicate draft on retry.
        try:
            if not self._add_description(event_id, event):
                warnings.append("Could not add Overview description — add it manually in Eventbrite")
        except Exception as e:
            logger.exception("Adding Overview description failed for event %s", event_id)
            warnings.append(f"Could not add Overview description ({e}) — add it manually in Eventbrite")

        try:
            if not self._create_ticket_class(event_id, event.is_free):
                warnings.append("Could not create ticket class — add one manually in Eventbrite")
        except Exception as e:
            logger.exception("Creating ticket class failed for event %s", event_id)
            warnings.append(f"Could not create ticket class ({e}) — add one manually in Eventbrite")

        # Note for virtual events: Zoom link must be added manually
        if event.is_virtual:
            logger.info(
                "MANUAL STEP REQUIRED: add Zoom link to Online Event Page in "
                "Eventbrite dashboard (https://us06web.zoom.us/j/85076176419)"
            )

        return EventbriteEvent(
            event_id=event_id,
            url=event_url,
            name=event.title,
            status="draft",
            warnings=warnings,
        )

    def find_draft_by_title(self, title: str) -> tuple | None:
        """Find the most recent draft event whose name exactly matches `title`.

        Used to reconcile an ambiguous create (timeout/connection drop after
        the POST): if Eventbrite already created the draft, we adopt it rather
        than failing and duplicating it on retry. Returns (event_id, url) or
        None. Errors return None — reconciliation is best-effort and the
        caller re-raises the original failure.
        """
        try:
            response = self.session.get(
                f"{self.base_url}/organizations/{self.organization_id}/events/",
                headers=self.headers,
                params={"status": "draft", "name_filter": title, "order_by": "created_desc"},
                timeout=HTTP_TIMEOUT,
            )
            response.raise_for_status()
            for candidate in response.json().get("events", []):
                if (candidate.get("name") or {}).get("text") == title:
                    return candidate["id"], candidate.get("url", "")
        except Exception:
            logger.exception("Draft reconciliation lookup failed for %r", title)
        return None

    def _add_description(self, event_id: str, event: EventRecord) -> bool:
        """Add structured content for the Overview section.

        Note: Main description is now added during event creation to avoid
        the SUMMARY_DESCRIPTION_CONFLICT error from Eventbrite API.

        Returns True if the Overview was added (or there was nothing to add).
        """
        description_html = self._build_description_html(event)

        if not description_html:
            logger.info("No description content to add")
            return True

        # Add structured_content for the Overview section
        return self._add_structured_content(event_id, description_html)

    def _add_structured_content(self, event_id: str, description_html: str) -> bool:
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

        response = self.session.post(
            f"{self.base_url}/events/{event_id}/structured_content/",
            headers=self.headers,
            json=payload,
            timeout=HTTP_TIMEOUT,
        )

        if response.status_code in [200, 201]:
            logger.info("Added structured content (Overview)")
            return True

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

        response2 = self.session.post(
            f"{self.base_url}/events/{event_id}/structured_content/1/",
            headers=self.headers,
            json=payload2,
            timeout=HTTP_TIMEOUT,
        )

        if response2.status_code in [200, 201]:
            logger.info("Added structured content (Overview)")
            return True

        logger.warning(
            "Could not add structured content for event %s (tried both endpoints; "
            "last status %s: %s) — Overview section needs manual entry",
            event_id, response2.status_code, response2.text[:500],
        )
        return False

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

    def _to_eastern(self, dt: datetime | None) -> datetime | None:
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

    def _create_ticket_class(self, event_id: str, is_free: bool) -> bool:
        """Create a ticket class for the event. Returns True on success."""
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

        response = self.session.post(
            f"{self.base_url}/events/{event_id}/ticket_classes/",
            headers=self.headers,
            json=payload,
            timeout=HTTP_TIMEOUT,
        )

        if response.status_code == 200:
            logger.info("Created %s ticket class", "free" if is_free else "paid")
            return True

        logger.warning(
            "Could not create ticket class for event %s (status %s): %s",
            event_id, response.status_code, response.text[:500],
        )
        return False

    def _create_or_get_venue(self, location_text: str) -> str | None:
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
            response = self.session.post(
                f"{self.base_url}/organizations/{self.organization_id}/venues/",
                headers=self.headers,
                json=payload,
                timeout=HTTP_TIMEOUT,
            )

            if response.status_code in [200, 201]:
                venue_id = response.json().get("id")
                logger.info("Created venue: %s", venue_id)
                return venue_id
            logger.warning(
                "Could not create venue (status %s): %s — location will show as TBD",
                response.status_code, response.text[:500],
            )
            return None
        except Exception:
            logger.exception("Venue creation failed — location will show as TBD")
            return None

    def get_event_logo_url(self, event_id: str) -> str | None:
        """
        Fetch the public URL of an event's logo image.

        Args:
            event_id: Eventbrite event ID

        Returns:
            Public URL of the logo image, or None if not found
        """
        response = self.session.get(
            f"{self.base_url}/events/{event_id}/",
            headers=self.headers,
            timeout=HTTP_TIMEOUT,
        )

        if response.status_code == 200:
            data = response.json()
            logo = data.get("logo")
            if logo:
                # Try different URL fields - Eventbrite uses "original" for full-res
                return (
                    logo.get("original", {}).get("url")
                    or logo.get("url")
                )
        return None

    def update_event_image(self, event_id: str, logo_id: str) -> bool:
        """
        Update the image/logo on an existing Eventbrite event.

        Args:
            event_id: Eventbrite event ID
            logo_id: New logo ID from uploaded image

        Returns:
            True if update succeeded, False otherwise
        """
        logger.info("Updating image for event %s...", event_id)

        payload = {
            "event": {
                "logo_id": logo_id,
            }
        }

        response = self.session.post(
            f"{self.base_url}/events/{event_id}/",
            headers=self.headers,
            json=payload,
            timeout=HTTP_TIMEOUT,
        )

        if response.status_code == 200:
            logger.info("Image updated successfully for event %s", event_id)
            return True

        logger.error(
            "Failed to update image for event %s (status %s): %s",
            event_id, response.status_code, response.text[:500],
        )
        return False

    def _format_datetime_local(self, dt: datetime | None) -> str:
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
