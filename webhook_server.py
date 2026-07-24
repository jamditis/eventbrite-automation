"""
Webhook server for Airtable automation triggers.

Listens for POST requests from Airtable and triggers the event processing pipeline.

Usage:
    python webhook_server.py                    # Run on default port 5000
    python webhook_server.py --port 8080        # Run on custom port

For production on Raspberry Pi (single worker + threads so the in-memory
processing-status map is shared by every request):
    gunicorn webhook_server:app -b 0.0.0.0:5000 --worker-class gthread --workers 1 --threads 8
"""

import argparse
import hmac
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Flask, jsonify, request

from airtable_client import AirtableClient
from config import (
    ERROR_STATUS,
    PROCESSED_STATUS,
    REGENERATE_STATUS,
    WEBHOOK_REQUIRE_AUTH,
    _pass,
    validate_config,
)
from eventbrite_client import EventbriteClient
from image_generator import ImageGenerator


def _setup_logging():
    """Configure root logging once, whether run via CLI or gunicorn."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=os.getenv("LOG_LEVEL", "INFO"),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )


_setup_logging()
logger = logging.getLogger("eventbrite.webhook")

app = Flask(__name__)

# Track background processing status. Guarded by _status_lock; bounded so a
# long-lived process doesn't grow it forever.
processing_status = {}
_status_lock = threading.Lock()
MAX_STATUS_ENTRIES = 200

# Records currently being processed (either path). Prevents two concurrent
# webhook fires for the same record from creating duplicate Eventbrite drafts.
_in_flight = set()
_in_flight_lock = threading.Lock()

# Bounded pool for background processing — an unbounded thread-per-request
# model lets a request flood exhaust the Pi's memory.
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pipeline")


@app.after_request
def add_security_headers(response):
    """Add security headers to all responses."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "0"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cache-Control"] = "no-store"
    return response

# Webhook secret. Enforcement is opt-in via WEBHOOK_REQUIRE_AUTH because the
# deployed Airtable automation script doesn't send a secret yet. Once the
# script includes {"secret": "..."} in its JSON body (or an X-Webhook-Secret
# header), set WEBHOOK_REQUIRE_AUTH=true in .env to require it.
WEBHOOK_SECRET = _pass("claude/eventbrite/webhook-secret") or os.getenv("WEBHOOK_SECRET", "")

# Compiled regex for Airtable record ID validation
RECORD_ID_RE = re.compile(r'rec[a-zA-Z0-9]{10,20}$')


def _valid_record_id(record_id: str) -> bool:
    """Validate that record_id matches Airtable's format."""
    return isinstance(record_id, str) and RECORD_ID_RE.fullmatch(record_id) is not None


def verify_token(token: str) -> bool:
    """Verify the static webhook token. Returns False if secret is not configured."""
    if not WEBHOOK_SECRET:
        return False  # No secret configured = reject all requests
    return hmac.compare_digest(WEBHOOK_SECRET, token)


def _auth_error(data: dict):
    """Enforce webhook auth when enabled. Returns an error response or None.

    The secret may arrive as an X-Webhook-Secret header or a "secret" field in
    the JSON body (the Airtable script can send body fields but historically
    not headers).
    """
    if not (WEBHOOK_REQUIRE_AUTH and WEBHOOK_SECRET):
        return None
    supplied = request.headers.get("X-Webhook-Secret") or (data or {}).get("secret") or ""
    if verify_token(supplied):
        return None
    logger.warning("Rejected webhook request with missing/invalid secret from %s",
                   request.remote_addr)
    return jsonify({"error": "unauthorized"}), 401


def _set_status(record_id: str, entry: dict):
    """Record processing status, evicting oldest entries past the cap."""
    with _status_lock:
        processing_status[record_id] = entry
        while len(processing_status) > MAX_STATUS_ENTRIES:
            # dicts preserve insertion order; drop the oldest
            processing_status.pop(next(iter(processing_status)))


def _try_claim(record_id: str) -> bool:
    """Claim a record for processing. False if it's already in flight."""
    with _in_flight_lock:
        if record_id in _in_flight:
            return False
        _in_flight.add(record_id)
        return True


def _release(record_id: str):
    with _in_flight_lock:
        _in_flight.discard(record_id)


def _record_failure(airtable, record_id: str, message: str):
    """Make a failure visible in Airtable: Logs entry + Status change.

    Setting Status to ERROR_STATUS ("Needs review") means staff see failures
    in the Status column instead of records silently sitting in "Todo".
    """
    try:
        airtable.update_log(record_id, message)
        airtable.update_status(record_id, ERROR_STATUS)
    except Exception:
        logger.exception("Could not record failure in Airtable for %s", record_id)


def process_record(record_id: str) -> dict:
    """Process a single Airtable record through the pipeline."""
    if not _try_claim(record_id):
        logger.warning("Record %s is already being processed; skipping duplicate request", record_id)
        return {"success": False, "error": "already_processing", "record_id": record_id}
    try:
        return _process_record_locked(record_id)
    finally:
        _release(record_id)


def _process_record_locked(record_id: str) -> dict:
    airtable = None
    image_path = None
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
            logger.info("Regenerate status detected for %s, routing to image regeneration", record_id)
            return _regenerate_image_locked(record_id)

        # Check if already processed
        if event.status == PROCESSED_STATUS:
            return {"success": True, "message": "Already processed", "record_id": record_id}

        # Duplicate-draft guard: if a previous run created the Eventbrite event
        # but failed before updating Status, don't create a second draft —
        # repair the Airtable state instead.
        if event.eventbrite_event_id:
            logger.warning(
                "Record %s already has Eventbrite event %s but status %r; "
                "repairing status instead of creating a duplicate draft",
                record_id, event.eventbrite_event_id, event.status,
            )
            airtable.mark_as_processed(record_id, event.eventbrite_url or "", event.eventbrite_event_id)
            airtable.update_log(
                record_id,
                f"Skipped duplicate draft creation — event {event.eventbrite_event_id} already exists; status repaired",
            )
            return {
                "success": True,
                "message": "Draft already exists; status repaired",
                "record_id": record_id,
                "eventbrite_url": event.eventbrite_url,
            }

        # Validate data
        warnings = event.validation_warnings
        if warnings:
            logger.warning("Validation warnings for %s: %s", record_id, warnings)

        # Generate image
        logger.info("Generating image for: %s", event.title)
        generated = image_gen.generate_event_image(event)
        image_path = generated.path

        # Upload to Eventbrite
        logger.info("Uploading image to Eventbrite...")
        logo_id = eventbrite.upload_image(image_path)

        # Create draft event
        logger.info("Creating draft event...")
        eb_event = eventbrite.create_draft_event(event, logo_id=logo_id)

        # Update Airtable with event ID for future updates. If this write
        # fails, the draft exists but Airtable doesn't know — log loudly, and
        # the duplicate-draft guard above prevents a re-run from creating a
        # second draft once the event ID write eventually lands.
        marked = airtable.mark_as_processed(event.record_id, eb_event.url, eb_event.event_id)
        if not marked:
            logger.critical(
                "Draft %s created (%s) but Airtable status update FAILED for %s — "
                "fix manually or re-trigger once Airtable recovers",
                eb_event.event_id, eb_event.url, record_id,
            )
            airtable.update_log(
                record_id,
                f"WARNING: draft created ({eb_event.url}) but status update failed — set Status manually",
            )

        # Surface non-fatal issues to staff in the record's Logs field
        if generated.used_fallback:
            airtable.update_log(
                record_id,
                f"AI image generation failed ({generated.error}); used default CCM banner. "
                "Set status to 'Regenerate image' to retry.",
            )
        for warning in eb_event.warnings:
            airtable.update_log(record_id, f"Warning: {warning}")

        # Save image to Airtable attachment field for archive
        logo_url = eventbrite.get_event_logo_url(eb_event.event_id)
        if logo_url:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            filename = f"{event.title[:30]}_{timestamp}.png"
            airtable.add_image_attachment(event.record_id, logo_url, filename)
        else:
            logger.info("Could not fetch logo URL for attachment archive")

        logger.info("Successfully processed %s: %s", record_id, eb_event.url)
        return {
            "success": True,
            "record_id": record_id,
            "event_title": event.title,
            "eventbrite_url": eb_event.url,
            "is_virtual": event.is_virtual,
            "airtable_updated": marked,
            "warnings": eb_event.warnings,
        }

    except Exception as e:
        logger.exception("Error processing record %s", record_id)
        if airtable is not None:
            _record_failure(airtable, record_id, f"Processing error: {e}")
        else:
            try:
                _record_failure(AirtableClient(), record_id, f"Processing error: {e}")
            except Exception:
                logger.exception("Could not report failure to Airtable for %s", record_id)
        return {"success": False, "error": "processing_failed", "record_id": record_id}
    finally:
        # Always clean up the temp image, including on failure
        if image_path is not None:
            try:
                if image_path.exists():
                    image_path.unlink()
            except OSError:
                logger.warning("Could not remove temp image %s", image_path)


def process_record_async(record_id: str):
    """Background worker to process a record and update status."""
    _set_status(record_id, {"status": "processing", "started": datetime.now().isoformat()})

    try:
        result = process_record(record_id)
        _set_status(record_id, {
            "status": "completed" if result.get("success") else "failed",
            "result": result,
            "completed": datetime.now().isoformat(),
        })
        logger.info("Background processing completed for %s: success=%s", record_id, result.get("success"))
    except Exception:
        _set_status(record_id, {
            "status": "failed",
            "completed": datetime.now().isoformat(),
        })
        logger.exception("Background processing failed for %s", record_id)


@app.route("/", methods=["GET"])
def health_check():
    """Health check endpoint."""
    with _in_flight_lock:
        in_flight = len(_in_flight)
    return jsonify({
        "status": "ok",
        "service": "eventbrite-automation",
        "in_flight": in_flight,
        "timestamp": datetime.now().isoformat(),
    })


@app.route("/webhook/airtable", methods=["POST"])
def airtable_webhook():
    """
    Handle Airtable webhook.

    Responds immediately with 202 and processes in background to avoid timeouts.

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
    try:
        data = request.get_json(silent=True)

        if not data:
            return jsonify({"error": "No JSON data provided"}), 400

        auth_error = _auth_error(data)
        if auth_error:
            return auth_error

        logger.info("Webhook received: %s", {k: v for k, v in data.items() if k != "secret"})

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

            if not _valid_record_id(record_id):
                return jsonify({"error": "Invalid record_id format"}), 400

            if sync_mode:
                # Synchronous processing (for manual testing)
                result = process_record(record_id)
                if result["success"]:
                    return jsonify(result), 200
                else:
                    return jsonify(result), 500
            else:
                # Async processing (default for Airtable webhooks).
                # Reject duplicate fires for a record that's already in flight.
                with _status_lock:
                    current = processing_status.get(record_id, {})
                if current.get("status") == "processing":
                    return jsonify({
                        "status": "already_processing",
                        "record_id": record_id,
                        "check_status": f"/webhook/status/{record_id}",
                    }), 202

                _set_status(record_id, {"status": "processing", "started": datetime.now().isoformat()})
                _executor.submit(process_record_async, record_id)

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

    except Exception:
        logger.exception("Webhook error")
        return jsonify({"error": "internal_server_error"}), 500


@app.route("/webhook/status/<record_id>", methods=["GET"])
def check_status(record_id: str):
    """Check the processing status of a record. Returns status only, no result details."""
    with _status_lock:
        entry = processing_status.get(record_id)
    if entry is not None:
        return jsonify({
            "status": entry.get("status"),
            "started": entry.get("started"),
            "completed": entry.get("completed"),
        })
    else:
        return jsonify({"status": "unknown"}), 404


def regenerate_image_for_record(record_id: str) -> dict:
    """Regenerate image for an existing Eventbrite event."""
    if not _try_claim(record_id):
        logger.warning("Record %s is already being processed; skipping duplicate regenerate", record_id)
        return {"success": False, "error": "already_processing", "record_id": record_id}
    try:
        return _regenerate_image_locked(record_id)
    finally:
        _release(record_id)


def _regenerate_image_locked(record_id: str) -> dict:
    airtable = None
    image_path = None
    try:
        airtable = AirtableClient()
        eventbrite = EventbriteClient()
        image_gen = ImageGenerator()

        # Get the record
        event = airtable.get_record_by_id(record_id)
        if event is None:
            msg = f"Record not found: {record_id}"
            logger.error("Regenerate failed: %s", msg)
            airtable.update_log(record_id, f"Regeneration failed: {msg}")
            return {"success": False, "error": msg}

        # Resolve event ID — fall back to extracting from URL if field is empty
        event_id = event.eventbrite_event_id
        if not event_id and event.eventbrite_url:
            event_id = AirtableClient.extract_event_id_from_url(event.eventbrite_url)
            if event_id:
                logger.info("Extracted event ID %s from URL (was missing from field)", event_id)
                airtable.update_event_id(record_id, event_id)
                airtable.update_log(record_id, f"Auto-recovered event ID {event_id} from URL")

        if not event_id:
            msg = "No Eventbrite event ID found and no URL to extract it from. Set status to 'Todo' to create the event first."
            logger.error("Regenerate failed for %s: %s", record_id, msg)
            airtable.update_log(record_id, f"Regeneration failed: {msg}")
            return {"success": False, "error": msg, "record_id": record_id}

        logger.info("Regenerating image for: %s (Eventbrite event %s)", event.title, event_id)

        # Generate new image
        generated = image_gen.generate_event_image(event)
        image_path = generated.path
        if generated.used_fallback:
            airtable.update_log(
                record_id,
                f"AI image generation failed during regeneration ({generated.error}); "
                "used default CCM banner",
            )

        # Upload to Eventbrite
        logger.info("Uploading new image to Eventbrite...")
        logo_id = eventbrite.upload_image(image_path)

        # Update the existing event with new image
        success = eventbrite.update_event_image(event_id, logo_id)

        if success:
            # Save regenerated image to Airtable attachment field
            logo_url = eventbrite.get_event_logo_url(event_id)
            if logo_url:
                timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                filename = f"{event.title[:30]}_regen_{timestamp}.png"
                airtable.add_image_attachment(event.record_id, logo_url, filename)
            else:
                logger.info("Could not fetch logo URL for attachment archive")

            # Reset status back to "Eventbrite draft created"
            airtable.update_status(record_id, PROCESSED_STATUS)
            logger.info("Status reset to '%s'", PROCESSED_STATUS)

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
            logger.error("Regenerate failed for %s: %s", record_id, msg)
            airtable.update_log(record_id, f"Regeneration failed: {msg}")
            return {"success": False, "error": msg, "record_id": record_id}

    except Exception as e:
        logger.exception("Error regenerating image for %s", record_id)
        try:
            (airtable or AirtableClient()).update_log(record_id, f"Regeneration error: {e}")
        except Exception:
            logger.exception("Could not report regeneration failure to Airtable for %s", record_id)
        return {"success": False, "error": "regeneration_failed", "record_id": record_id}
    finally:
        if image_path is not None:
            try:
                if image_path.exists():
                    image_path.unlink()
            except OSError:
                logger.warning("Could not remove temp image %s", image_path)


def regenerate_image_async(record_id: str):
    """Background worker to regenerate image and update status."""
    _set_status(record_id, {
        "status": "regenerating",
        "started": datetime.now().isoformat(),
    })

    try:
        result = regenerate_image_for_record(record_id)
        _set_status(record_id, {
            "status": "completed" if result.get("success") else "failed",
            "result": result,
            "completed": datetime.now().isoformat(),
        })
        logger.info("Image regeneration completed for %s: success=%s", record_id, result.get("success"))
    except Exception:
        _set_status(record_id, {
            "status": "failed",
            "completed": datetime.now().isoformat(),
        })
        logger.exception("Image regeneration failed for %s", record_id)


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
        data = request.get_json(silent=True)

        if not data:
            return jsonify({"error": "No JSON data provided"}), 400

        auth_error = _auth_error(data)
        if auth_error:
            return auth_error

        if "record_id" not in data:
            return jsonify({"error": "record_id is required"}), 400

        record_id = data["record_id"]

        if not _valid_record_id(record_id):
            return jsonify({"error": "Invalid record_id format"}), 400

        sync_mode = data.get("sync", False)

        logger.info("Image regeneration requested for %s", record_id)

        if sync_mode:
            # Synchronous processing
            result = regenerate_image_for_record(record_id)
            if result["success"]:
                return jsonify(result), 200
            else:
                return jsonify(result), 500
        else:
            with _status_lock:
                current = processing_status.get(record_id, {})
            if current.get("status") in ("processing", "regenerating"):
                return jsonify({
                    "status": "already_processing",
                    "record_id": record_id,
                    "check_status": f"/webhook/status/{record_id}",
                }), 202

            _set_status(record_id, {"status": "regenerating", "started": datetime.now().isoformat()})
            _executor.submit(regenerate_image_async, record_id)

            return jsonify({
                "status": "accepted",
                "message": "Image regeneration started in background",
                "record_id": record_id,
                "check_status": f"/webhook/status/{record_id}",
            }), 202

    except Exception:
        logger.exception("Regenerate image webhook error")
        return jsonify({"error": "internal_server_error"}), 500


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
    args = parser.parse_args()

    # Validate configuration
    try:
        validate_config()
        logger.info("Configuration validated successfully")
    except ValueError as e:
        logger.error("Configuration error: %s", e)
        return

    if not WEBHOOK_SECRET:
        logger.warning("No WEBHOOK_SECRET configured. POST requests will be accepted without authentication.")
    elif not WEBHOOK_REQUIRE_AUTH:
        logger.info("WEBHOOK_SECRET configured but WEBHOOK_REQUIRE_AUTH is off; auth not enforced.")

    logger.info("Starting webhook server on %s:%s", args.host, args.port)

    # Never run with debug=True in production — exposes interactive debugger
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
