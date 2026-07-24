"""
Eventbrite automation main orchestrator.

Processes Airtable event submissions and creates draft Eventbrite listings.

Usage:
    python main.py                     # Process all unprocessed records
    python main.py --record-id rec123  # Process a specific record
    python main.py --test              # Test connections only
    python main.py --dry-run           # Show what would be processed
"""

import argparse
import logging
import sys

from airtable_client import AirtableClient, EventRecord
from config import validate_config
from eventbrite_client import EventbriteClient
from image_generator import ImageGenerator

logger = logging.getLogger("eventbrite.main")


def process_event(
    event: EventRecord,
    airtable: AirtableClient,
    eventbrite: EventbriteClient,
    image_gen: ImageGenerator,
    dry_run: bool = False,
) -> bool:
    """
    Process a single event record through the full pipeline.

    Args:
        event: EventRecord to process
        airtable: AirtableClient instance
        eventbrite: EventbriteClient instance
        image_gen: ImageGenerator instance
        dry_run: If True, don't actually create anything

    Returns:
        True if successful, False otherwise
    """
    print(f"\n{'='*60}")
    print(f"Processing: {event.title}")
    print(f"Type: {event.event_type} | Pricing: {event.pricing}")
    print(f"Start: {event.start_datetime}")
    print(f"{'='*60}")

    # Show validation warnings
    warnings = event.validation_warnings
    if warnings:
        print("\n[!] DATA QUALITY WARNINGS:")
        for w in warnings:
            print(f"    - {w}")

    if dry_run:
        print("[DRY RUN] Would process this event")
        return True

    image_path = None
    try:
        # Step 1: Generate featured image
        print("\nStep 1: Generating featured image...")
        generated = image_gen.generate_event_image(event)
        image_path = generated.path
        if generated.used_fallback:
            print(f"    [!] AI generation failed ({generated.error}); using default CCM banner")

        # Step 2: Upload image to Eventbrite
        print("\nStep 2: Uploading image to Eventbrite...")
        logo_id = eventbrite.upload_image(image_path)

        # Step 3: Create draft event
        print("\nStep 3: Creating draft event...")
        eb_event = eventbrite.create_draft_event(event, logo_id=logo_id)

        # Step 4: Update Airtable record (store the event ID too, so image
        # regeneration works later without URL-scraping)
        print("\nStep 4: Updating Airtable record...")
        airtable.mark_as_processed(event.record_id, eb_event.url, eb_event.event_id)
        if generated.used_fallback:
            airtable.update_log(
                event.record_id,
                f"AI image generation failed ({generated.error}); used default CCM banner. "
                "Set status to 'Regenerate image' to retry.",
            )
        for warning in eb_event.warnings:
            print(f"    [!] {warning}")
            airtable.update_log(event.record_id, f"Warning: {warning}")

        print("\nSUCCESS: Draft event created")
        print(f"URL: {eb_event.url}")
        print("(Review and publish in Eventbrite dashboard)")

        return True

    except Exception as e:
        logger.exception("Failed to process event %s", event.record_id)
        print(f"\nERROR: Failed to process event: {e}")
        try:
            airtable.update_log(event.record_id, f"Processing error: {e}")
        except Exception:
            logger.exception("Could not write failure to Airtable log for %s", event.record_id)
        return False
    finally:
        # Always clean up the temp image, including on failure
        if image_path is not None and image_path.exists():
            image_path.unlink()


def test_connections() -> bool:
    """Test all API connections."""
    print("Testing API connections...\n")

    results = []

    # Test Airtable
    print("1. Testing Airtable connection...")
    try:
        airtable = AirtableClient()
        records = airtable.get_unprocessed_records()
        print(f"   OK - Found {len(records)} unprocessed records")
        results.append(True)
    except Exception as e:
        print(f"   FAILED - {e}")
        results.append(False)

    # Test Eventbrite
    print("\n2. Testing Eventbrite connection...")
    try:
        eventbrite = EventbriteClient()
        org_id = eventbrite.organization_id
        print(f"   OK - Organization ID: {org_id}")
        results.append(True)
    except Exception as e:
        print(f"   FAILED - {e}")
        results.append(False)

    # Test Gemini (image generation)
    print("\n3. Testing Gemini connection...")
    try:
        ImageGenerator()
        # Just verify client initializes
        print("   OK - Gemini client initialized")
        results.append(True)
    except Exception as e:
        print(f"   FAILED - {e}")
        results.append(False)

    print("\n" + "="*40)
    if all(results):
        print("All connections successful!")
        return True
    else:
        print("Some connections failed. Check credentials.")
        return False


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Create Eventbrite drafts from Airtable event submissions"
    )
    parser.add_argument(
        "--record-id",
        help="Process a specific Airtable record ID",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test API connections only",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be processed without making changes",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Validate configuration
    try:
        validate_config()
    except ValueError as e:
        print(f"Configuration error: {e}")
        sys.exit(1)

    # Test mode
    if args.test:
        success = test_connections()
        sys.exit(0 if success else 1)

    # Initialize clients
    airtable = AirtableClient()
    eventbrite = EventbriteClient()
    image_gen = ImageGenerator()

    # Get records to process
    if args.record_id:
        # Process specific record
        record = airtable.get_record_by_id(args.record_id)
        if record is None:
            print(f"Record not found: {args.record_id}")
            sys.exit(1)
        records = [record]
    else:
        # Process all unprocessed records
        records = airtable.get_unprocessed_records()

    if not records:
        print("No unprocessed records found.")
        sys.exit(0)

    print(f"Found {len(records)} record(s) to process")

    # Process each record
    success_count = 0
    fail_count = 0

    for record in records:
        if process_event(record, airtable, eventbrite, image_gen, args.dry_run):
            success_count += 1
        else:
            fail_count += 1

    # Summary
    print("\n" + "="*60)
    print("PROCESSING COMPLETE")
    print(f"Successful: {success_count}")
    print(f"Failed: {fail_count}")
    print("="*60)

    sys.exit(0 if fail_count == 0 else 1)


if __name__ == "__main__":
    main()
