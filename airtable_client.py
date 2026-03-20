"""
Airtable API client for event automation.

Handles fetching new event requests and updating records with Eventbrite URLs.
"""

import re
from typing import Optional
from dataclasses import dataclass
from datetime import datetime

from pyairtable import Api

from config import (
    AIRTABLE_PAT,
    AIRTABLE_BASE_ID,
    AIRTABLE_TABLE_ID,
    AIRTABLE_FIELDS,
    UNPROCESSED_STATUSES,
    PROCESSED_STATUS,
)


@dataclass
class EventRecord:
    """Represents an event record from Airtable."""

    record_id: str
    title: str
    brief_description: str
    full_description: str
    start_datetime: Optional[datetime]
    end_datetime: Optional[datetime]
    event_type: str  # "In-person" or "Virtual"
    pricing: str  # "Free" or "Paid"
    speaker_info: Optional[str] = None
    location: Optional[str] = None
    requester: Optional[str] = None
    status: Optional[str] = None
    eventbrite_status: Optional[str] = None
    # Image generation customization
    art_style: Optional[str] = None  # e.g., "minimalist", "watercolor", "bold geometric"
    image_prompt: Optional[str] = None  # Additional prompt guidance
    primary_color: Optional[str] = None  # Hex code or color name
    secondary_color: Optional[str] = None  # Hex code or color name
    # Eventbrite tracking
    eventbrite_event_id: Optional[str] = None  # For updating existing events
    eventbrite_url: Optional[str] = None  # URL of the created event

    @property
    def has_valid_brief_description(self) -> bool:
        """Check if brief_description is valid (not empty, not a URL)."""
        if not self.brief_description:
            return False
        # Check if it looks like a URL
        if self.brief_description.startswith(("http://", "https://", "www.")):
            return False
        return True

    @property
    def has_valid_full_description(self) -> bool:
        """Check if full_description is valid (not empty)."""
        return bool(self.full_description and self.full_description.strip())

    @property
    def validation_warnings(self) -> list[str]:
        """Get list of validation warnings for this record."""
        warnings = []
        if not self.has_valid_brief_description:
            warnings.append("Brief description is missing or invalid (contains URL)")
        if not self.has_valid_full_description:
            warnings.append("Full description is missing")
        if not self.start_datetime:
            warnings.append("Start date/time is missing")
        return warnings

    @property
    def is_virtual(self) -> bool:
        """Check if event is virtual/online."""
        if not self.event_type:
            return False
        return "virtual" in self.event_type.lower()

    @property
    def is_free(self) -> bool:
        """Check if event is free."""
        if not self.pricing:
            return True
        return "free" in self.pricing.lower()

    @property
    def speaker_info_html(self) -> Optional[str]:
        """Format speaker information for HTML display."""
        if not self.speaker_info:
            return None
        # Return as-is since it's already a combined field
        return self.speaker_info.replace("\n", "<br>")


class AirtableClient:
    """Client for interacting with Airtable event request table."""

    def __init__(self):
        self.api = Api(AIRTABLE_PAT)
        self.table = self.api.table(AIRTABLE_BASE_ID, AIRTABLE_TABLE_ID)

    def get_all_records(self) -> list[dict]:
        """Fetch all records without any filtering (for debugging)."""
        return self.table.all()

    def get_table_fields(self) -> list[str]:
        """
        Get all field names from the first record in the table.

        Useful for debugging field name mismatches.
        """
        records = self.table.all(max_records=1)
        if records:
            return list(records[0]["fields"].keys())
        return []

    def get_unprocessed_records(self) -> list[EventRecord]:
        """
        Fetch all records that haven't been processed yet.

        Returns records where Status field is blank, Todo, In progress, or Needs review.
        """
        status_field = AIRTABLE_FIELDS.get("status")

        try:
            if status_field:
                # Build OR formula for all unprocessed statuses
                # Handle blank/empty as a special case
                conditions = []
                for status in UNPROCESSED_STATUSES:
                    if status == "":
                        conditions.append(f"{{{status_field}}} = BLANK()")
                        conditions.append(f"{{{status_field}}} = ''")
                    else:
                        conditions.append(f"{{{status_field}}} = '{status}'")

                formula = f"OR({', '.join(conditions)})"
                records = self.table.all(formula=formula)
            else:
                records = self.table.all()
        except Exception as e:
            print(f"Note: Could not filter by Status field, fetching all records")
            print(f"      (Error: {e})")
            records = self.table.all()

        return [self._parse_record(r) for r in records]

    def get_record_by_id(self, record_id: str) -> Optional[EventRecord]:
        """Fetch a specific record by ID."""
        try:
            record = self.table.get(record_id)
            return self._parse_record(record)
        except Exception as e:
            print(f"Error fetching record {record_id}: {e}")
            return None

    def mark_as_processed(
        self,
        record_id: str,
        eventbrite_url: str,
        eventbrite_event_id: Optional[str] = None,
    ) -> bool:
        """
        Mark a record as processed by updating the Status field.

        Args:
            record_id: Airtable record ID
            eventbrite_url: URL of the created Eventbrite draft event
            eventbrite_event_id: Eventbrite event ID (for later updates)

        Returns:
            True if update succeeded, False otherwise
        """
        status_field = AIRTABLE_FIELDS.get("status")
        url_field = AIRTABLE_FIELDS.get("eventbrite_url")
        event_id_field = AIRTABLE_FIELDS.get("eventbrite_event_id")

        if not status_field:
            print("Warning: No Status field configured, cannot mark as processed")
            return False

        try:
            # Build update payload
            update_data = {status_field: PROCESSED_STATUS}

            # Add URL if field exists
            if url_field:
                update_data[url_field] = eventbrite_url

            # Add event ID if field exists and ID provided
            if event_id_field and eventbrite_event_id:
                update_data[event_id_field] = eventbrite_event_id

            self.table.update(record_id, update_data)
            print(f"Marked record {record_id} as '{PROCESSED_STATUS}'")
            print(f"Eventbrite URL: {eventbrite_url}")
            if eventbrite_event_id:
                print(f"Eventbrite event ID: {eventbrite_event_id}")
            return True
        except Exception as e:
            print(f"Error updating record {record_id}: {e}")
            print(f"Note: Make sure '{PROCESSED_STATUS}' is a valid option in the Status field")
            return False

    def add_image_attachment(
        self,
        record_id: str,
        image_url: str,
        filename: Optional[str] = None,
    ) -> bool:
        """
        Add an image attachment to the Generated images field.

        Appends to existing attachments rather than replacing them,
        so we keep a history of all generated images.

        Args:
            record_id: Airtable record ID
            image_url: Public URL of the image to attach
            filename: Optional filename for the attachment

        Returns:
            True if update succeeded, False otherwise
        """
        attachment_field = AIRTABLE_FIELDS.get("generated_images")

        if not attachment_field:
            print("Warning: No Generated images field configured")
            return False

        try:
            # First, get existing attachments so we can append
            record = self.table.get(record_id)
            existing = record.get("fields", {}).get(attachment_field, [])

            # Build new attachment
            new_attachment = {"url": image_url}
            if filename:
                new_attachment["filename"] = filename

            # Append new attachment to existing ones
            updated_attachments = existing + [new_attachment]

            self.table.update(record_id, {attachment_field: updated_attachments})
            print(f"Added image attachment to record {record_id}")
            return True
        except Exception as e:
            print(f"Error adding attachment to {record_id}: {e}")
            return False

    def update_status(self, record_id: str, new_status: str) -> bool:
        """
        Update just the Status field of a record.

        Args:
            record_id: Airtable record ID
            new_status: New status value to set

        Returns:
            True if update succeeded, False otherwise
        """
        status_field = AIRTABLE_FIELDS.get("status")

        if not status_field:
            print("Warning: No Status field configured")
            return False

        try:
            self.table.update(record_id, {status_field: new_status})
            print(f"Updated record {record_id} status to '{new_status}'")
            return True
        except Exception as e:
            print(f"Error updating status for {record_id}: {e}")
            return False

    def update_log(self, record_id: str, message: str) -> bool:
        """Append a timestamped message to the Automation log field."""
        log_field = AIRTABLE_FIELDS.get("automation_log")
        if not log_field:
            print(f"Warning: No Automation log field configured")
            return False

        try:
            # Get existing log content to append
            record = self.table.get(record_id)
            existing = record.get("fields", {}).get(log_field, "")

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            entry = f"[{timestamp}] {message}"
            updated = f"{existing}\n{entry}".strip()

            # Keep only the last 50 lines to avoid hitting Airtable's char limit
            lines = updated.split("\n")
            if len(lines) > 50:
                updated = "\n".join(lines[-50:])

            self.table.update(record_id, {log_field: updated})
            return True
        except Exception as e:
            print(f"Error updating log for {record_id}: {e}")
            return False

    def update_event_id(self, record_id: str, event_id: str) -> bool:
        """Store the Eventbrite event ID for a record."""
        field = AIRTABLE_FIELDS.get("eventbrite_event_id")
        if not field:
            return False
        try:
            self.table.update(record_id, {field: event_id})
            print(f"Stored event ID {event_id} for record {record_id}")
            return True
        except Exception as e:
            print(f"Error storing event ID for {record_id}: {e}")
            return False

    @staticmethod
    def extract_event_id_from_url(url: str) -> Optional[str]:
        """Extract Eventbrite event ID from a URL like .../tickets-1234567890."""
        if not url:
            return None
        match = re.search(r'(\d{10,})', url)
        return match.group(1) if match else None

    def _parse_record(self, record: dict) -> EventRecord:
        """Parse an Airtable record into an EventRecord dataclass."""
        fields = record["fields"]

        # Parse start datetime if present
        start_datetime = None
        datetime_str = fields.get(AIRTABLE_FIELDS["start_datetime"])
        if datetime_str:
            try:
                start_datetime = datetime.fromisoformat(datetime_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                print(f"Warning: Could not parse start datetime: {datetime_str}")

        # Parse end datetime if present
        end_datetime = None
        end_datetime_str = fields.get(AIRTABLE_FIELDS.get("end_datetime", ""))
        if end_datetime_str:
            try:
                end_datetime = datetime.fromisoformat(end_datetime_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                print(f"Warning: Could not parse end datetime: {end_datetime_str}")

        return EventRecord(
            record_id=record["id"],
            title=fields.get(AIRTABLE_FIELDS["title"], "Untitled Event"),
            brief_description=fields.get(AIRTABLE_FIELDS["brief_description"], ""),
            full_description=fields.get(AIRTABLE_FIELDS["full_description"], ""),
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            event_type=fields.get(AIRTABLE_FIELDS["event_type"], "In-person"),
            pricing=fields.get(AIRTABLE_FIELDS["pricing"], "Free"),
            speaker_info=fields.get(AIRTABLE_FIELDS.get("speaker_info", ""), None),
            location=fields.get(AIRTABLE_FIELDS.get("location", ""), None),
            requester=fields.get(AIRTABLE_FIELDS.get("requester", ""), None),
            status=fields.get(AIRTABLE_FIELDS.get("status", ""), None),
            eventbrite_status=fields.get(AIRTABLE_FIELDS.get("eventbrite_status", ""), None),
            # Image generation customization
            art_style=fields.get(AIRTABLE_FIELDS.get("art_style", ""), None),
            image_prompt=fields.get(AIRTABLE_FIELDS.get("image_prompt", ""), None),
            primary_color=fields.get(AIRTABLE_FIELDS.get("primary_color", ""), None),
            secondary_color=fields.get(AIRTABLE_FIELDS.get("secondary_color", ""), None),
            # Eventbrite tracking
            eventbrite_event_id=fields.get(AIRTABLE_FIELDS.get("eventbrite_event_id", ""), None),
            eventbrite_url=fields.get(AIRTABLE_FIELDS.get("eventbrite_url", ""), None),
        )


def test_connection():
    """Test the Airtable connection."""
    print("Testing Airtable connection...")
    client = AirtableClient()

    # First, show available fields
    print("\nChecking table fields...")
    fields = client.get_table_fields()
    if fields:
        print(f"Available fields in table:")
        for f in fields:
            print(f"  - {f}")
    else:
        print("No records found in table (or table is empty)")

    # Try to fetch records
    print("\nFetching unprocessed records...")
    records = client.get_unprocessed_records()
    print(f"Found {len(records)} unprocessed records")

    for record in records[:3]:  # Show first 3
        print(f"\n  Title: {record.title}")
        print(f"  Type: {record.event_type}")
        print(f"  Pricing: {record.pricing}")
        print(f"  Start: {record.start_datetime}")
        print(f"  Virtual: {record.is_virtual}")
        print(f"  Free: {record.is_free}")

    return len(records) >= 0  # Success if no exception


if __name__ == "__main__":
    test_connection()
