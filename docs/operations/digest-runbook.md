# eventbrite-attendee-digest runbook

Operational reference for the cron service. Live testing prerequisites + first-deploy checklist live in the README quick-start; this doc is for ongoing operations and incident response.

## What this does

Sends one daily digest email per opted-in event to the event's speakers/hosts/instructors, summarizing who has registered. Silent on days with no new registrants. Initial briefing fires once per event when staff explicitly request it from the admin UI.

## Where it runs

| Surface | Location |
| --- | --- |
| Service | `digest-cron.timer` on **houseofjawn** (systemd, every 30 min at `:00` and `:30`) |
| Logs | `/var/log/digest-cron.log` (rotated weekly, 8 weeks retained) |
| Lock | `~/.local/state/digest-cron.lock` (XDG state dir; flock-based, auto-released on crash) |
| State | Airtable `EventDigests` base — `Last digest sent at`, `Last attendee cursor`, `Last error` per event row |
| Dup-send safety net | `~/.claude/workstation/sent-emails.db` (email_ledger from houseofjawn-bot/scheduler) |

## Common operations

### Disable temporarily (whole service)

```bash
sudo systemctl stop digest-cron.timer
sudo systemctl disable digest-cron.timer
```

### Disable a single event without stopping cron

In Airtable, uncheck `Enabled` on the event row. Cron skips on the next tick.

### Manually trigger one tick

```bash
cd /home/jamditis/projects/eventbrite-attendee-digest
.venv/bin/python -m digest.cron --log-level=DEBUG
```

### Dry run (renders + logs, no SMTP send, no state write)

```bash
.venv/bin/python -m digest.cron --dry-run --log-level=DEBUG
```

### Check what the timer thinks it's doing

```bash
systemctl list-timers digest-cron.timer --no-pager
journalctl -u digest-cron.service --since "1 hour ago"
```

## Common failures + fixes

### `ConfigError: missing required env vars`

`.env` isn't being loaded by systemd. Check `EnvironmentFile=` path in `/etc/systemd/system/digest-cron.service` matches the repo path. Re-run `deploy/install.sh` to refresh.

### `CRM transport error for ... ConnectionError: ...`

Dashboard at `pi.amditis.tech` is down or unreachable. The cron continues — affected attendee gets a form-only blurb instead of LLM-enriched. Check:

```bash
curl -I https://pi.amditis.tech/api/contacts/  # 200 expected (with a 401 body)
sudo systemctl status houseofjawn-dashboard
```

### `EB API 401 Unauthorized`

Eventbrite token rotated or revoked. Re-fetch from `pass`, update `.env`, restart timer:

```bash
echo "EVENTBRITE_PRIVATE_TOKEN=$(pass show claude/api/eventbrite/eventbrite-token)" >> .env  # then dedupe by hand
sudo systemctl restart digest-cron.timer
```

### `EventbritePaginationError: ... has_more_items=true with no continuation token`

EB returned an inconsistent paginated response. Likely transient. Re-running the next tick usually clears it. If persistent, file an EB support ticket — `Last error` on the event row will name the affected event.

### `ledger says duplicate for ...`

Email ledger has a recent send for the same recipient/subject. Inspect:

```bash
sqlite3 ~/.claude/workstation/sent-emails.db \
  "SELECT id, recipient, subject, sent_at, context FROM sends \
   WHERE recipient='lead@example.com' ORDER BY sent_at DESC LIMIT 5;"
```

If the dup is real (you don't want to re-send), no action needed — the cron will retry next tick and correctly skip.
If the dup is wrong (a previous send was actually a draft or test), `DELETE FROM sends WHERE id=...` and re-trigger manually.

### Speaker emails not arriving

1. Check ledger above to confirm a send was attempted.
2. Check Bcc to `jamditis@gmail.com` — if Joe got it, the issue is downstream (recipient's spam filter, Gmail throttling).
3. Check `Last error` field in Airtable for that event row.
4. Check `journalctl -u digest-cron.service` for the affected tick.

### Cron exits immediately with `previous tick still running; exiting`

A prior tick is holding the flock. Either it's genuinely still running (long EB fetch + LLM calls) or it crashed without clean handle close. Check:

```bash
ps -ef | grep -F 'digest.cron'
ls -la ~/.local/state/digest-cron.lock
```

If no `digest.cron` process exists, the lock is stale — `rm ~/.local/state/digest-cron.lock` and re-run.

## Rollback procedure

If a digest goes out wrong (hallucinated profiles, wrong recipients, broken formatting):

1. **Stop the timer immediately:**

   ```bash
   sudo systemctl stop digest-cron.timer
   ```

2. **Investigate via logs + Airtable `Last error`:**

   ```bash
   journalctl -u digest-cron.service --since "today" | tail -200
   ```

3. **Notify affected speakers** — Joe or Cassandra sends a correction email to the affected event's speakers list (Reply-To recipient in the bad email).

4. **Fix the bug, ship a patch.** Until merged + deployed, leave timer stopped.

5. **Re-enable the timer:**

   ```bash
   sudo systemctl start digest-cron.timer
   sudo systemctl enable digest-cron.timer  # (already enabled, but harmless)
   ```

## Token rotation

| Credential | Where stored | How to rotate |
| --- | --- | --- |
| Eventbrite token | `pass show claude/api/eventbrite/eventbrite-token` | Generate new token in EB account, update pass, redeploy `.env` |
| Airtable PAT | Airtable account settings → tokens | Generate new PAT scoped to `EventDigests` base, update `.env` |
| Dashboard API key | `pass show claude/tokens/dashboard-api` | Rotate via dashboard admin, update pass + `.env` |
| SMTP app password | `pass show gmail-app-password` | Generate new app password in Google account, update pass + `.env` |

After any rotation: `sudo systemctl restart digest-cron.timer`.
