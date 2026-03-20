"""
Webhook server for Airtable automation triggers.

Listens for POST requests from Airtable and triggers the event processing pipeline.

Usage:
    python webhook_server.py                    # Run on default port 5000
    python webhook_server.py --port 8080        # Run on custom port

For production on Raspberry Pi:
    gunicorn webhook_server:app -b 0.0.0.0:5000
"""

import argparse
import hmac
import hashlib
import os
import threading
from datetime import datetime
from flask import Flask, request, jsonify

from config import validate_config, REGENERATE_STATUS, PROCESSED_STATUS
from airtable_client import AirtableClient
from eventbrite_client import EventbriteClient
from image_generator import ImageGenerator

app = Flask(__name__)

# Track background processing status
processing_status = {}

# Optional: Set a webhook secret for security
# Generate with: python -c "import secrets; print(secrets.token_hex(32))"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")


def verify_signature(payload: bytes, signature: str) -> bool:
    """Verify webhook signature if secret is configured."""
    if not WEBHOOK_SECRET:
        return True  # No verification if no secret set

    expected = hmac.new(
        WEBHOOK_SECRET.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(f"sha256={expected}", signature)


def process_record(record_id: str) -> dict:
    """Process a single Airtable record through the pipeline."""
    try:
        # Initialize clients
        airtable = AirtableClient()
        eventbrite = EventbriteClient()
        image_gen = ImageGenerator()

        # Get the record
        event = airtable.get_record_by_id(record_id)
        if event is None:
            return {"success": False, "error": f"Record not found: {record_id}"}

        # Check if this is a regenerate request
        if event.status == REGENERATE_STATUS:
            print(f"Regenerate status detected for {record_id}, routing to image regeneration...")
            return regenerate_image_for_record(record_id)

        # Check if already processed
        if event.status == PROCESSED_STATUS:
            return {"success": True, "message": "Already processed", "record_id": record_id}

        # Validate data
        warnings = event.validation_warnings
        if warnings:
            print(f"Validation warnings for {record_id}: {warnings}")

        # Generate image
        print(f"Generating image for: {event.title}")
        image_path = image_gen.generate_event_image(event)

        # Upload to Eventbrite
        print("Uploading image to Eventbrite...")
        logo_id = eventbrite.upload_image(image_path)

        # Create draft event
        print("Creating draft event...")
        eb_event = eventbrite.create_draft_event(event, logo_id=logo_id)

        # Update Airtable with event ID for future updates
        airtable.mark_as_processed(event.record_id, eb_event.url, eb_event.event_id)

        # Save image to Airtable attachment field for archive
        logo_url = eventbrite.get_event_logo_url(eb_event.event_id)
        if logo_url:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            filename = f"{event.title[:30]}_{timestamp}.png"
            airtable.add_image_attachment(event.record_id, logo_url, filename)
        else:
            print("Note: Could not fetch logo URL for attachment archive")

        # Clean up temp image
        if image_path.exists():
            image_path.unlink()

        return {
            "success": True,
            "record_id": record_id,
            "event_title": event.title,
            "eventbrite_url": eb_event.url,
            "is_virtual": event.is_virtual,
        }

    except Exception as e:
        print(f"Error processing record {record_id}: {e}")
        import traceback
        traceback.print_exc()
        try:
            AirtableClient().update_log(record_id, f"Processing error: {e}")
        except Exception:
            pass
        return {"success": False, "error": str(e), "record_id": record_id}


def process_record_async(record_id: str):
    """Background worker to process a record and update status."""
    processing_status[record_id] = {"status": "processing", "started": datetime.now().isoformat()}

    try:
        result = process_record(record_id)
        processing_status[record_id] = {
            "status": "completed" if result.get("success") else "failed",
            "result": result,
            "completed": datetime.now().isoformat(),
        }
        print(f"Background processing completed for {record_id}: {result.get('success')}")
    except Exception as e:
        processing_status[record_id] = {
            "status": "failed",
            "error": str(e),
            "completed": datetime.now().isoformat(),
        }
        print(f"Background processing failed for {record_id}: {e}")


@app.route("/", methods=["GET"])
def health_check():
    """Health check endpoint."""
    return jsonify({
        "status": "ok",
        "service": "eventbrite-automation",
        "timestamp": datetime.now().isoformat(),
    })


@app.route("/webhook/airtable", methods=["POST"])
def airtable_webhook():
    """
    Handle Airtable webhook.

    Responds immediately with 200 and processes in background to avoid timeouts.

    Airtable automation should send a POST with JSON body:
    {
        "record_id": "recXXXXXX"
    }

    Or for processing all unprocessed records:
    {
        "action": "process_all"
    }

    Use sync=true to wait for completion (for manual testing):
    {
        "record_id": "recXXXXXX",
        "sync": true
    }
    """
    # Verify signature if configured
    signature = request.headers.get("X-Webhook-Signature", "")
    if WEBHOOK_SECRET and not verify_signature(request.data, signature):
        return jsonify({"error": "Invalid signature"}), 401

    try:
        data = request.get_json()

        if not data:
            return jsonify({"error": "No JSON data provided"}), 400

        # Log the incoming request
        print(f"\n{'='*60}")
        print(f"Webhook received at {datetime.now().isoformat()}")
        print(f"Data: {data}")
        print(f"{'='*60}")

        # Check if caller wants synchronous processing (for testing)
        sync_mode = data.get("sync", False)

        # Handle different actions
        if data.get("action") == "process_all":
            # Process all unprocessed records (always sync for this action)
            airtable = AirtableClient()
            records = airtable.get_unprocessed_records()

            results = []
            for record in records:
                result = process_record(record.record_id)
                results.append(result)

            return jsonify({
                "success": True,
                "processed": len(results),
                "results": results,
            })

        elif "record_id" in data:
            record_id = data["record_id"]

            if sync_mode:
                # Synchronous processing (for manual testing)
                result = process_record(record_id)
                if result["success"]:
                    return jsonify(result), 200
                else:
                    return jsonify(result), 500
            else:
                # Async processing (default for Airtable webhooks)
                # Respond immediately, process in background
                thread = threading.Thread(
                    target=process_record_async,
                    args=(record_id,),
                    daemon=True
                )
                thread.start()

                return jsonify({
                    "status": "accepted",
                    "message": "Processing started in background",
                    "record_id": record_id,
                    "check_status": f"/webhook/status/{record_id}",
                }), 202  # 202 Accepted

        else:
            return jsonify({
                "error": "Invalid request. Provide 'record_id' or 'action': 'process_all'"
            }), 400

    except Exception as e:
        print(f"Webhook error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/webhook/status/<record_id>", methods=["GET"])
def check_status(record_id: str):
    """Check the processing status of a record."""
    if record_id in processing_status:
        return jsonify(processing_status[record_id])
    else:
        return jsonify({"status": "unknown", "message": "No processing record found"}), 404


def regenerate_image_for_record(record_id: str) -> dict:
    """Regenerate image for an existing Eventbrite event."""
    airtable = AirtableClient()

    try:
        eventbrite = EventbriteClient()
        image_gen = ImageGenerator()

        # Get the record
        event = airtable.get_record_by_id(record_id)
        if event is None:
            msg = f"Record not found: {record_id}"
            print(f"Regenerate failed: {msg}")
            return {"success": False, "error": msg}

        # Resolve event ID — fall back to extracting from URL if field is empty
        event_id = event.eventbrite_event_id
        if not event_id and event.eventbrite_url:
            event_id = AirtableClient.extract_event_id_from_url(event.eventbrite_url)
            if event_id:
                print(f"Extracted event ID {event_id} from URL (was missing from field)")
                airtable.update_event_id(record_id, event_id)
                airtable.update_log(record_id, f"Auto-recovered event ID {event_id} from URL")

        if not event_id:
            msg = "No Eventbrite event ID found and no URL to extract it from. Set status to 'Todo' to create the event first."
            print(f"Regenerate failed for {record_id}: {msg}")
            airtable.update_log(record_id, f"Regeneration failed: {msg}")
            return {"success": False, "error": msg, "record_id": record_id}

        print(f"Regenerating image for: {event.title}")
        print(f"Eventbrite event ID: {event_id}")

        # Generate new image
        image_path = image_gen.generate_event_image(event)

        # Upload to Eventbrite
        print("Uploading new image to Eventbrite...")
        logo_id = eventbrite.upload_image(image_path)

        # Update the existing event with new image
        success = eventbrite.update_event_image(event_id, logo_id)

        # Clean up temp image
        if image_path.exists():
            image_path.unlink()

        if success:
            # Save regenerated image to Airtable attachment field
            logo_url = eventbrite.get_event_logo_url(event_id)
            if logo_url:
                timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                filename = f"{event.title[:30]}_regen_{timestamp}.png"
                airtable.add_image_attachment(event.record_id, logo_url, filename)
            else:
                print("Note: Could not fetch logo URL for attachment archive")

            # Reset status back to "Eventbrite draft created"
            airtable.update_status(record_id, PROCESSED_STATUS)
            print(f"Status reset to '{PROCESSED_STATUS}'")

            airtable.update_log(record_id, "Image regenerated successfully")

            return {
                "success": True,
                "record_id": record_id,
                "event_title": event.title,
                "eventbrite_event_id": event_id,
                "message": "Image regenerated and updated successfully",
            }
        else:
            msg = "Failed to update Eventbrite event with new image"
            print(f"Regenerate failed for {record_id}: {msg}")
            airtable.update_log(record_id, f"Regeneration failed: {msg}")
            return {"success": False, "error": msg, "record_id": record_id}

    except Exception as e:
        print(f"Error regenerating image for {record_id}: {e}")
        import traceback
        traceback.print_exc()
        airtable.update_log(record_id, f"Regeneration error: {e}")
        return {"success": False, "error": str(e), "record_id": record_id}


def regenerate_image_async(record_id: str):
    """Background worker to regenerate image and update status."""
    processing_status[record_id] = {
        "status": "regenerating",
        "started": datetime.now().isoformat(),
    }

    try:
        result = regenerate_image_for_record(record_id)
        processing_status[record_id] = {
            "status": "completed" if result.get("success") else "failed",
            "result": result,
            "completed": datetime.now().isoformat(),
        }
        print(f"Image regeneration completed for {record_id}: {result.get('success')}")
    except Exception as e:
        processing_status[record_id] = {
            "status": "failed",
            "error": str(e),
            "completed": datetime.now().isoformat(),
        }
        print(f"Image regeneration failed for {record_id}: {e}")


@app.route("/webhook/regenerate-image", methods=["POST"])
def regenerate_image_webhook():
    """
    Regenerate the image for an existing Eventbrite event.

    Request body:
    {
        "record_id": "recXXXXXX"
    }

    Optional:
    {
        "record_id": "recXXXXXX",
        "sync": true  // Wait for completion
    }

    The record must already have an Eventbrite event ID stored.
    This generates a new image using current Airtable field values
    (including any art_style, image_prompt, or color customizations)
    and updates the existing Eventbrite event.
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({"error": "No JSON data provided"}), 400

        if "record_id" not in data:
            return jsonify({"error": "record_id is required"}), 400

        record_id = data["record_id"]
        sync_mode = data.get("sync", False)

        print(f"\n{'='*60}")
        print(f"Image regeneration requested at {datetime.now().isoformat()}")
        print(f"Record ID: {record_id}")
        print(f"{'='*60}")

        if sync_mode:
            # Synchronous processing
            result = regenerate_image_for_record(record_id)
            if result["success"]:
                return jsonify(result), 200
            else:
                return jsonify(result), 500
        else:
            # Async processing
            thread = threading.Thread(
                target=regenerate_image_async,
                args=(record_id,),
                daemon=True,
            )
            thread.start()

            return jsonify({
                "status": "accepted",
                "message": "Image regeneration started in background",
                "record_id": record_id,
                "check_status": f"/webhook/status/{record_id}",
            }), 202

    except Exception as e:
        print(f"Regenerate image webhook error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/webhook/test", methods=["POST", "GET"])
def test_webhook():
    """Test endpoint to verify webhook is working."""
    return jsonify({
        "status": "ok",
        "message": "Webhook endpoint is working",
        "timestamp": datetime.now().isoformat(),
        "method": request.method,
    })


def main():
    """Run the webhook server."""
    parser = argparse.ArgumentParser(description="Eventbrite automation webhook server")
    parser.add_argument("--port", type=int, default=5000, help="Port to run on")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--debug", action="store_true", help="Run in debug mode")

    args = parser.parse_args()

    # Validate configuration
    try:
        validate_config()
        print("Configuration validated successfully")
    except ValueError as e:
        print(f"Configuration error: {e}")
        return

    print(f"\nStarting webhook server on {args.host}:{args.port}")
    print(f"Webhook endpoint: http://{args.host}:{args.port}/webhook/airtable")
    print(f"Health check: http://{args.host}:{args.port}/")
    print("\nWaiting for webhooks...\n")

    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
