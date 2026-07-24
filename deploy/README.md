# Raspberry Pi deployment guide

This guide covers deploying both subsystems to a Raspberry Pi (production host: houseofjawn):

1. **Webhook draft creator** — `eventbrite-automation.service` (this guide)
2. **Attendee digest** — `digest-cron.timer` + `digest-cron.service`; install with `install-digest.sh`, operate via [`docs/operations/digest-runbook.md`](../docs/operations/digest-runbook.md)

## Quick start (webhook server)

```bash
# 1. SSH to the Pi
ssh <user>@<pi-hostname>

# 2. Clone the repository
git clone https://github.com/jamditis/eventbrite-automation.git
cd eventbrite-automation

# 3. Run the setup script (creates venv, installs deps, installs the
#    systemd unit rewritten for your user + checkout path, installs logrotate)
chmod +x deploy/setup.sh
./deploy/setup.sh

# 4. Create your .env file
nano .env

# 5. Start the service
sudo systemctl enable eventbrite-automation
sudo systemctl start eventbrite-automation

# 6. Set up external access (see below)
```

## Architecture

```
Airtable Form
     |
     v
Airtable Automation (Script action, triggers on Status = "Todo")
     |
     v
POST webhook to external URL
     |
     v
Cloudflare Tunnel
     |
     v
Raspberry Pi (gunicorn on port 5000)
     |
     +--> Generates image with Gemini AI
     |
     +--> Creates draft on Eventbrite
     |
     v
Updates Airtable record status
```

## Files in this folder

| File | Purpose |
|------|---------|
| `setup.sh` | Automated setup script for the webhook server |
| `eventbrite-automation.service` | systemd unit for the webhook server (template — setup.sh rewrites user/paths) |
| `logrotate-eventbrite-automation` | logrotate config for the webhook log files (installed by setup.sh) |
| `install-digest.sh` | Automated setup for the digest cron units |
| `digest-cron.service` / `digest-cron.timer` | systemd units for the daily attendee digest |
| `digest-failure-alert.service` | OnFailure unit that emails staff when a digest tick fails |
| `env.example` | Template for `.env.digest` (digest subsystem) |
| `.env.example` | Template for `.env` (webhook subsystem) |
| `create-airtable-base.py` | One-time script that created the EventDigests Airtable base |
| `ngrok.service` | Legacy ngrok tunnel unit — not used in production (Cloudflare Tunnel is) |

## Environment variables (webhook)

Create a `.env` file in the project root with:

```bash
# Required
AIRTABLE_PAT=pat_xxxxxxxxxxxxxxxxxx
AIRTABLE_BASE_ID=appXXXXXXXXXXXXXX
AIRTABLE_TABLE_ID=tblXXXXXXXXXXXXXX
GEMINI_API_KEY=AIza_xxxxxxxxxxxxxxxxxxxxxxxx
EVENTBRITE_PRIVATE_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Optional webhook authentication — see "Security notes" below before enabling
WEBHOOK_SECRET=your_random_secret_string
# WEBHOOK_REQUIRE_AUTH=true
```

On houseofjawn, secrets are read from the pass store first (`config.py:_pass`)
and fall back to `.env` values.

Generate a webhook secret with:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

## External access

The webhook needs to be reachable from the internet so Airtable can POST to it.

### Production: Cloudflare Tunnel

The live deployment serves `https://eventbrite.amditis.tech` through the
existing `houseofjawn` Cloudflare tunnel. For a fresh setup:

```bash
# Install cloudflared
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb -o cloudflared.deb
sudo dpkg -i cloudflared.deb

# Login (opens browser)
cloudflared tunnel login

# Create tunnel
cloudflared tunnel create eventbrite-automation

# Route traffic (requires domain in Cloudflare)
cloudflared tunnel route dns eventbrite-automation events.yourdomain.com

# Run tunnel
cloudflared tunnel run eventbrite-automation
```

Unlike ngrok, the URL is permanent — the Airtable automation never needs
re-pointing after a restart.

### Legacy: ngrok

`ngrok.service` remains in this folder from the original deployment but is
**not used in production**: free ngrok URLs change on every restart, which
silently breaks the Airtable → webhook path until someone updates the
automation URL. If you must use it for a quick test: `ngrok http 5000`.

## Airtable automation setup

The automation uses a **Script action** (not the native "Send a webhook"
action) so the request body and error handling stay under our control.

1. Go to your Airtable base → "Automations" tab
2. Create new automation
3. **Trigger:** "When a record matches conditions" → Status is "Todo"
4. **Action:** "Run a script" with input variable `recordId` mapped to
   "Record ID" from the trigger, and this script:

```javascript
let recordId = input.config().recordId;

await fetch('https://eventbrite.amditis.tech/webhook/airtable', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({record_id: recordId})
});

output.set('status', 'sent');
```

If you enable `WEBHOOK_REQUIRE_AUTH`, add the secret to the body:
`body: JSON.stringify({record_id: recordId, secret: 'YOUR_SECRET'})`.

## Managing the service

```bash
# Start/stop/restart
sudo systemctl start eventbrite-automation
sudo systemctl stop eventbrite-automation
sudo systemctl restart eventbrite-automation

# Check status
sudo systemctl status eventbrite-automation

# Enable/disable auto-start
sudo systemctl enable eventbrite-automation
sudo systemctl disable eventbrite-automation

# View logs (rotated weekly / at 20MB by logrotate)
tail -f /var/log/eventbrite-automation/webhook.log
tail -f /var/log/eventbrite-automation/webhook-error.log
journalctl -u eventbrite-automation -f
```

## Testing

**Health check (also reports in-flight background jobs):**
```bash
curl http://localhost:5000/
```

**Process all unprocessed records:**
```bash
curl -X POST http://localhost:5000/webhook/airtable \
  -H "Content-Type: application/json" \
  -d '{"action": "process_all"}'
```

**Process specific record:**
```bash
curl -X POST http://localhost:5000/webhook/airtable \
  -H "Content-Type: application/json" \
  -d '{"record_id": "recXXXXXXXXXXXXXX"}'
```

## Troubleshooting

See [`docs/operations/webhook-runbook.md`](../docs/operations/webhook-runbook.md)
for the full incident-response guide. Quick checks:

### Service won't start
```bash
# Check for errors
journalctl -u eventbrite-automation --no-pager -n 50

# Common issues:
# - Missing .env file
# - Python dependencies not installed
# - Wrong paths in service file
```

### Webhook not receiving requests
1. Check the Cloudflare tunnel is running
2. Verify URL in Airtable automation matches the external URL
3. Check Pi firewall: `sudo ufw status`

### API errors
1. Check .env has all required keys
2. Verify API keys haven't expired
3. Check error log: `cat /var/log/eventbrite-automation/webhook-error.log`

### Image generation fails
1. Check GEMINI_API_KEY is valid
2. Verify internet connectivity
3. The system falls back to `templates/default-banner.png` if Gemini fails,
   and writes a note to the record's Logs field so staff can regenerate

## Updating

```bash
cd <checkout-directory>
git pull
sudo systemctl restart eventbrite-automation
```

## Security notes

- Never commit `.env` to git
- **Webhook auth is off by default.** `WEBHOOK_SECRET` alone does nothing —
  requests are only verified when `WEBHOOK_REQUIRE_AUTH=true` is also set.
  Before enabling it, update the Airtable script to send the secret in the
  JSON body (see above), or every automation request will get a 401.
- Consider IP whitelisting if your Pi has a static IP
- Keep the Pi's OS and packages updated
