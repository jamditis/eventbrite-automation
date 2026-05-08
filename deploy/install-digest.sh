#!/usr/bin/env bash
# Install digest-cron systemd unit + timer + logrotate config on houseofjawn.
# Idempotent: re-running picks up unit-file changes via daemon-reload.
#
# DO NOT run before:
#   - .env populated (run `cp deploy/env.example .env` and fill in)
#   - Airtable EventDigests base exists with at least one enabled row OR
#     one event row with `Initial briefing requested at` set
#   - Phase 0 CF Pages migration is verified (only matters for the admin UI;
#     the cron itself runs without it).
#
# Coexists with the existing webhook deploy (eventbrite-automation.service);
# this script only touches the digest-cron units.
set -euo pipefail

REPO=/home/jamditis/projects/eventbrite-automation

if [ ! -f "$REPO/.env" ]; then
    echo "FATAL: $REPO/.env missing. Copy from deploy/env.example and fill in first."
    exit 1
fi

if [ ! -f "$REPO/venv/bin/python" ]; then
    echo "FATAL: venv missing. Run: python3 -m venv venv && venv/bin/pip install -r requirements.txt"
    exit 1
fi

# Verify the cron can actually import the package as systemd will invoke it.
# digest/ is at the repo root, so running from $REPO with no PYTHONPATH works.
# This catches package-layout misconfig before install rather than at first tick.
if ! ( cd "$REPO" && "$REPO/venv/bin/python" -m digest.cron --help >/dev/null 2>&1 ); then
    echo "FATAL: 'python -m digest.cron --help' fails. Check the venv + package layout."
    exit 1
fi

# Soft-check the email_ledger module so operators see this BEFORE first send,
# not as a runtime warning during a live tick.
LEDGER_PATH="${DIGEST_LEDGER_PATH:-$HOME/projects/houseofjawn-bot/scheduler}"
if [ ! -f "$LEDGER_PATH/email_ledger.py" ]; then
    echo "WARNING: email_ledger module missing at $LEDGER_PATH. Cron will run"
    echo "         but the cross-session dup safety net is DISABLED. Primary"
    echo "         dup defense (Airtable last_digest_sent_at) still works."
    echo "         To fix: install houseofjawn-bot, or set DIGEST_LEDGER_PATH"
    echo "         in .env to point to a directory containing email_ledger.py."
fi

sudo install -m 644 "$REPO/deploy/digest-cron.service" /etc/systemd/system/
sudo install -m 644 "$REPO/deploy/digest-cron.timer" /etc/systemd/system/

# The cron logs to journald only (no StandardOutput= directive in the unit).
# `journalctl -u digest-cron.service` is the canonical log surface; journald
# handles its own rotation. If a prior install of this script created
# /var/log/digest-cron.log + a logrotate rule, leave them in place — they
# do no harm. The lines below were intentionally removed in the fold-in.

sudo systemctl daemon-reload
sudo systemctl enable --now digest-cron.timer

echo "Installed. Status:"
systemctl status digest-cron.timer --no-pager
echo
echo "Next firings:"
systemctl list-timers digest-cron.timer --no-pager
