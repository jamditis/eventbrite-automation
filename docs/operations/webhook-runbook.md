# Webhook draft creator — operations runbook

The webhook draft creator turns Airtable form submissions into Eventbrite
draft listings with AI-generated banners. This runbook covers where to look
when something goes wrong and how to fix the common failure modes.

Companion: [digest-runbook.md](digest-runbook.md) covers the attendee digest
subsystem, which shares this repo but is otherwise independent.

## System summary

- **Host:** houseofjawn (Raspberry Pi), service `eventbrite-automation.service`
- **Public URL:** `https://eventbrite.amditis.tech` via Cloudflare Tunnel
- **Server:** gunicorn, 1 worker + 8 threads (single process — the
  processing-status map and duplicate-processing guard are in-memory, so all
  requests must share one process)
- **Config:** `.env` at the repo root (webhook only; `.env.digest` belongs to
  the digest). Secrets come from the pass store first, `.env` second.
- **Logs:** `/var/log/eventbrite-automation/webhook.log` and
  `webhook-error.log`, rotated by `/etc/logrotate.d/eventbrite-automation`
  (weekly, or at 20MB). All log lines carry timestamps, levels, and logger
  names (`eventbrite.webhook`, `eventbrite.client`, `eventbrite.airtable`,
  `eventbrite.images`).
- **Deploy:** `git pull` in the checkout, then
  `sudo systemctl restart eventbrite-automation`.

## How failures surface

The system is built so failures are visible in three places, in order of
how staff usually notice them:

1. **Airtable Status column** — a failed record's Status is set to
   **"Needs review"** (`ERROR_STATUS` in `config.py`). Staff scanning the
   base see the failure without reading any server logs.
2. **Airtable "Logs" field** — every failure, fallback-banner substitution,
   and partial-success warning (missing ticket class, venue, or Overview)
   appends a timestamped entry to the record's Logs field.
3. **Server logs** — full stack traces in
   `/var/log/eventbrite-automation/webhook-error.log`.

A record whose Status is "Needs review" is still in the unprocessed set, so
fixing the underlying issue and re-triggering (set Status back to "Todo" or
run `process_all`) retries it.

## Reliability behavior worth knowing

- **Timeouts everywhere.** Every Eventbrite/Airtable/Gemini call is bounded
  (`HTTP_TIMEOUT`/`UPLOAD_TIMEOUT`/`GEMINI_TIMEOUT_MS` in `config.py`). A
  hung upstream fails the record — visibly — instead of wedging a thread.
- **Retries.** GETs to Eventbrite retry on 429/5xx with backoff. POSTs are
  deliberately NOT retried on status errors (a timed-out create might have
  succeeded server-side; a blind retry would make a duplicate draft).
  pyairtable retries Airtable 429s internally.
- **Duplicate-draft guard.** If a record already has an `Eventbrite event ID`
  but an unprocessed Status (the "draft created but Airtable write failed"
  case), reprocessing repairs the Status instead of creating a second draft.
- **Duplicate-fire guard.** Concurrent webhook fires for the same record are
  rejected (`already_processing`) — only one pipeline runs per record at a
  time.
- **Fallback banner is flagged.** When Gemini fails, the default CCM banner
  is used AND a note lands in the record's Logs field, so a generic banner
  never ships silently. Set Status to "Regenerate image" to retry.
- **Bounded worker pool.** Background processing runs on a 4-thread pool;
  a request flood queues instead of exhausting the Pi's memory.

## Triage checklist

```bash
# 1. Is the service up?
sudo systemctl status eventbrite-automation

# 2. Is it reachable end-to-end?
curl https://eventbrite.amditis.tech/            # health + in-flight count
curl http://localhost:5000/                      # bypasses the tunnel

# 3. What happened recently?
tail -100 /var/log/eventbrite-automation/webhook-error.log
tail -100 /var/log/eventbrite-automation/webhook.log

# 4. What does Airtable say?
# Check the record's Status and Logs fields — most failures are explained there.

# 5. Re-trigger one record (sync mode shows the result immediately)
curl -X POST https://eventbrite.amditis.tech/webhook/airtable \
  -H "Content-Type: application/json" \
  -d '{"record_id": "recXXX", "sync": true}'
```

## Common incidents

### Record stuck in "Todo" / nothing happened
- Check the automation ran in Airtable (Automations → run history).
- Check the tunnel: `curl https://eventbrite.amditis.tech/`. If the local
  port answers but the public URL doesn't, restart the Cloudflare tunnel.
- If the service restarted mid-processing the in-memory status was lost;
  the record is safe to re-trigger — the duplicate-draft guard prevents a
  second draft if one was already created.

### Status is "Needs review"
The pipeline failed. Read the record's Logs field for the reason, fix it
(bad credentials, past event date, etc.), and set Status back to "Todo".

### Gemini 401 UNAUTHENTICATED
The API key was revoked (Google scans for exposed keys). Generate a new key,
update pass / `.env`, restart the service. Affected records shipped with the
default banner — set them to "Regenerate image".

### Eventbrite "Start and end dates must be in the future"
The record's proposed date is in the past (common with test records). Fix
the date in Airtable and re-trigger.

### Wrong organizer or 403 on create
Check `EVENTBRITE_ORGANIZER_ID` (5988913981, CCM profile — goes in the event
body) and `EVENTBRITE_ORGANIZATION_ID` (66857244479 — the create-event URL).
The token must have access to the CCM organization; see issue #32.

### Disk full / logs huge
Rotation is handled by logrotate; if it wasn't installed run:
`sudo cp deploy/logrotate-eventbrite-automation /etc/logrotate.d/eventbrite-automation`

## Enabling webhook authentication

Auth is opt-in and currently off (the Airtable script predates it).
To enable:

1. Generate a secret and store it (pass key `claude/eventbrite/webhook-secret`
   or `WEBHOOK_SECRET` in `.env`).
2. Update the Airtable automation script body to
   `JSON.stringify({record_id: recordId, secret: 'THE_SECRET'})`.
3. Set `WEBHOOK_REQUIRE_AUTH=true` in `.env` and restart the service.
4. Verify: a request without the secret should now get a 401.

Do step 2 before step 3, or every automation request will be rejected.

## Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /` | Health check; includes count of in-flight background jobs |
| `POST /webhook/airtable` | Process a record (`{"record_id": ...}`, optional `"sync": true`) or all (`{"action": "process_all"}`) |
| `POST /webhook/regenerate-image` | Regenerate the banner for an existing event |
| `GET /webhook/status/<record_id>` | Background status: `processing` / `regenerating` / `completed` / `failed`; 404 + `unknown` if not tracked (e.g. after a restart) |
| `POST /webhook/test` | Echo test |

The status endpoint returns status + timestamps only (no result payload).
Status is in-memory: it survives neither restarts nor is it shared across
processes (which is why the service runs exactly one gunicorn worker).
