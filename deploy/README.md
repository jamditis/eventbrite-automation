# Raspberry Pi deployment guide

This guide walks you through deploying the Eventbrite automation webhook server to a Raspberry Pi.

## Quick start

```bash
# 1. SSH to your Pi
ssh pi@raspberrypi.local

# 2. Clone the repository
cd /home/pi
git clone https://github.com/jamditis/eventbrite-automation.git
cd eventbrite-automation

# 3. Run the setup script
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
Airtable Automation (triggers on new record)
     |
     v
POST webhook to external URL
     |
     v
ngrok/Cloudflare tunnel
     |
     v
Raspberry Pi (Flask server on port 5000)
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
| `setup.sh` | Automated setup script for Pi |
| `eventbrite-automation.service` | systemd service for the webhook server |
| `ngrok.service` | systemd service for ngrok tunnel |

## Environment variables

Create a `.env` file in the project root with:

```bash
# Required
AIRTABLE_PAT=pat_xxxxxxxxxxxxxxxxxx
AIRTABLE_BASE_ID=appXXXXXXXXXXXXXX
AIRTABLE_TABLE_ID=tblXXXXXXXXXXXXXX
GEMINI_API_KEY=AIza_xxxxxxxxxxxxxxxxxxxxxxxx
EVENTBRITE_PRIVATE_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Optional (for security)
WEBHOOK_SECRET=your_random_secret_string
```

Generate a webhook secret with:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

## External access options

The webhook needs to be accessible from the internet so Airtable can send POST requests to it.

### Option A: ngrok (easiest)

**Install ngrok:**
```bash
# Download and install
curl -s https://ngrok-agent.s3.amazonaws.com/ngrok.asc | \
  sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | \
  sudo tee /etc/apt/sources.list.d/ngrok.list
sudo apt update && sudo apt install ngrok
```

**Authenticate (one-time):**
1. Create free account at https://ngrok.com
2. Get your authtoken from the dashboard
3. Run: `ngrok config add-authtoken YOUR_TOKEN`

**Start tunnel manually:**
```bash
ngrok http 5000
```
Copy the HTTPS URL (e.g., `https://abc123.ngrok-free.app`)

**Or run as a service:**
```bash
sudo cp deploy/ngrok.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ngrok
sudo systemctl start ngrok
```

Check ngrok URL:
```bash
curl http://localhost:4040/api/tunnels | jq '.tunnels[0].public_url'
```

**Note:** Free ngrok URLs change when restarted. Consider ngrok paid plan for static URLs.

### Option B: Cloudflare tunnel (more stable)

For a permanent URL with a custom domain:

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

## Airtable automation setup

1. Go to your Airtable base
2. Click "Automations" tab
3. Create new automation
4. **Trigger:** "When a record matches conditions"
   - Table: Event requests
   - Condition: Status is empty OR Status is "Todo"
5. **Action:** "Send a webhook"
   - URL: `https://YOUR_NGROK_URL/webhook/airtable`
   - Method: POST
   - Body:
   ```json
   {
     "record_id": "{RECORD_ID()}"
   }
   ```

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

# View logs
tail -f /var/log/eventbrite-automation/webhook.log
tail -f /var/log/eventbrite-automation/webhook-error.log
journalctl -u eventbrite-automation -f
```

## Testing

**Health check:**
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
1. Check ngrok/tunnel is running
2. Verify URL in Airtable automation matches external URL
3. Check Pi firewall: `sudo ufw status`

### API errors
1. Check .env has all required keys
2. Verify API keys haven't expired
3. Check error log: `cat /var/log/eventbrite-automation/webhook-error.log`

### Image generation fails
1. Check GEMINI_API_KEY is valid
2. Verify internet connectivity
3. The system will fall back to a simple branded image if Gemini fails

## Updating

```bash
cd /home/pi/eventbrite-automation
git pull
sudo systemctl restart eventbrite-automation
```

## Security notes

- Never commit `.env` to git
- Use WEBHOOK_SECRET to verify requests are from Airtable
- Consider IP whitelisting if your Pi has a static IP
- Keep the Pi's OS and packages updated
