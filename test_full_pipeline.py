"""
Full pipeline test - creates a real Eventbrite draft with generated image.

The draft can be deleted from your Eventbrite dashboard after testing.
"""

from datetime import datetime, timedelta

from airtable_client import EventRecord
from config import TEMP_DIR
from eventbrite_client import EventbriteClient
from image_generator import ImageGenerator


def run_test():
    """Run a full pipeline test with synthetic data."""

    # Create a test event record
    test_event = EventRecord(
        record_id="test_pipeline_001",
        title="Test Event - Delete Me",
        brief_description="This is a test event created by the automation pipeline.",
        full_description="This is a full pipeline test to verify that image generation, upload, and Eventbrite draft creation all work correctly. You can safely delete this draft from your Eventbrite dashboard.",
        start_datetime=datetime.now() + timedelta(days=30),
        end_datetime=datetime.now() + timedelta(days=30, hours=2),
        event_type="Virtual",
        pricing="Free",
        speaker_info="Test Speaker, Test Organization",
    )

    print("=" * 60)
    print("FULL PIPELINE TEST")
    print("=" * 60)
    print(f"\nTest event: {test_event.title}")
    print(f"Start: {test_event.start_datetime}")
    print(f"Type: {test_event.event_type}")
    print(f"Pricing: {test_event.pricing}")

    # Step 1: Generate image
    print("\n" + "-" * 40)
    print("Step 1: Generating image...")
    print("-" * 40)

    image_gen = ImageGenerator()
    image_path = TEMP_DIR / "test_pipeline_image.png"
    image_path = image_gen.generate_event_image(test_event, image_path)
    print(f"Image saved: {image_path}")

    # Step 2: Upload to Eventbrite
    print("\n" + "-" * 40)
    print("Step 2: Uploading image to Eventbrite...")
    print("-" * 40)

    eventbrite = EventbriteClient()
    logo_id = eventbrite.upload_image(image_path)
    print(f"Logo ID: {logo_id}")

    # Step 3: Create draft event
    print("\n" + "-" * 40)
    print("Step 3: Creating Eventbrite draft...")
    print("-" * 40)

    eb_event = eventbrite.create_draft_event(test_event, logo_id=logo_id)

    print("\n" + "=" * 60)
    print("TEST COMPLETE!")
    print("=" * 60)
    print("\nEventbrite draft created:")
    print(f"  URL: {eb_event.url}")
    print(f"  Event ID: {eb_event.event_id}")
    print("\nYou can view and delete this draft from your Eventbrite dashboard.")

    return eb_event


if __name__ == "__main__":
    run_test()
