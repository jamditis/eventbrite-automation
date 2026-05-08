#!/usr/bin/env bash
# Install digest-cron systemd unit + timer + logrotate config on houseofjawn.
# Idempotent: re-running picks up unit-file changes via daemon-reload.
#
# DO NOT run before:
#   - .env populated (run `cp deploy/env.example .env` and fill in)
#   - Airtable EventDigests base exists with at least one enabled row
#   - Phase 0 CF Pages migration is verified (only matters for the admin UI;
#     the cron itself runs without it).
set -euo pipefail

REPO=/home/jamditis/projects/eventbrite-attendee-digest

if [ ! -f "$REPO/.env" ]; then
    echo "FATAL: $REPO/.env missing. Copy from deploy/env.example and fill in first."
    exit 1
fi

if [ ! -f "$REPO/.venv/bin/python" ]; then
    echo "FATAL: venv missing. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

sudo install -m 644 "$REPO/deploy/digest-cron.service" /etc/systemd/system/
sudo install -m 644 "$REPO/deploy/digest-cron.timer" /etc/systemd/system/
sudo install -m 644 "$REPO/deploy/digest-cron.logrotate" /etc/logrotate.d/digest-cron

sudo touch /var/log/digest-cron.log
sudo chown jamditis:jamditis /var/log/digest-cron.log

sudo systemctl daemon-reload
sudo systemctl enable --now digest-cron.timer

echo "Installed. Status:"
systemctl status digest-cron.timer --no-pager
echo
echo "Next firings:"
systemctl list-timers digest-cron.timer --no-pager
