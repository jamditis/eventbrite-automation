# Attendee digest runbook

Operational reference for the cron service. First-deploy steps live in the README ("Attendee digest first deploy"); this doc is for ongoing operations and incident response.

## What this does

Sends one daily digest email per opted-in event to the event's speakers/hosts/instructors, summarizing who has registered. Silent on days with no new registrants. Initial briefing fires once per event when staff explicitly request it from the admin UI. Initial briefings fire regardless of `Enabled` state; daily digests fire only when `Enabled = true`.

## Where it runs

| Surface | Location |
| --- | --- |
| Service | `digest-cron.timer` on **houseofjawn** (systemd, once daily at 07:00 ET; `OnCalendar` pins `America/New_York`, so it is not host-timezone-dependent) |
| Logs | `journalctl -u digest-cron.service` (canonical log surface) |
| Lock | `~/.local/state/digest-cron.lock` (XDG state dir; flock-based, auto-released on process exit) |
| State | Airtable `EventDigests` base — see "State fields" below |
| Dup-send safety net | `~/.claude/workstation/sent-emails.db` (email_ledger from `houseofjawn-bot/scheduler/`) |
| Lifetime install path | `/home/jamditis/projects/eventbrite-automation/` (hardcoded in systemd unit; relocating requires re-running `install-digest.sh` after editing the service template) |

### State fields (Airtable `Events` table)

The cron reads these to decide what to do, and writes them after each tick:

| Field | Read or write | Purpose |
| --- | --- | --- |
| `Enabled` | read | Daily-cadence gate. Initial briefings ignore this. |
| `Eventbrite event ID` | read | EB API key for attendee + event fetch. |
| `Speaker emails` | read | Comma- or newline-separated To-list. |
| `Lead host email` | read | Reply-To + ledger key. |
| `Attendee sheet URL` | read | Optional Google Sheet link; renders the "view full attendee sheet" button. Allowlist-sanitized to Google Sheets doc URLs before use. Omit to hide the button. |
| `Days out to start` | read | Window opens this many days before event start. |
| `Send time (ET)` | read | Daily fire-no-earlier-than floor. The cron ticks once a day at 07:00 ET, so a value **later than 07:00 means the event never fires** — keep it `<= 07:00` or move the timer's `OnCalendar`. |
| `Send weekdays` | read | Optional comma-separated `Mon` through `Sun`. Blank means every day. Applies to initial and follow-up emails. |
| `Registration question IDs to include` | read | Optional Q&A filter. |
| `Event start (ET)` | read | Used for window calculation. NOT auto-refreshed from Eventbrite — staff must update if EB event reschedules. If blank, the legacy initial briefing can still send, but follow-up digests do not. |
| `Initial briefing requested at` | read, write (clear after send) | Staff sets this via admin UI to fire the one-shot initial briefing. |
| `Initial briefing sent at` | read, write | Set after initial briefing fires; gates daily digests. |
| `Last digest sent at` | read, write | Per-day skip gate for daily digests. |
| `Last attendee cursor` | read, write | Diff key — attendees with `created_at > cursor` are "new." |
| `Last digest attendee count` | write | Last total registrant count for ops visibility. |
| `Last error` | write | Cleared on successful send; populated with `{Type}: {message}\n{traceback}` on failure. |

Enter a Monday, Wednesday, and Friday schedule as `Mon,Wed,Fri`. The parser
normalizes case and surrounding whitespace. An unknown weekday fails that row
with its record ID in `Last error`; it never falls back to a broader schedule.
A pending initial briefing on an excluded day remains pending until the next
configured weekday inside the event window. A configured event start prevents
it from sending after the event.
Rows that are not eligible on a tick log the skip reason for diagnosis.

### Standing recipients (Cc/Bcc)

Beyond a row's `Speaker emails` (the visible To-list), every send copies a fixed set configured in `.env.digest`:

- **Cc** — `CC_ALWAYS` (default `info@centerforcooperativemedia.org`). The visible org copy; all recipients see it.
- **Bcc** — `BCC_ALWAYS` (default `jamditis@gmail.com,etiennec@montclair.edu,advinculaa@montclair.edu`). Hidden copies; the To and Cc recipients never see these addresses.

To change the standing copies, edit those vars in `.env.digest` and restart the timer (see "Editing `.env.digest`" below). The Bcc to `jamditis@gmail.com` is also the delivery check referenced in "Speaker emails not arriving".

## Common operations

### Disable temporarily (whole service)

```bash
sudo systemctl stop digest-cron.timer
sudo systemctl disable digest-cron.timer
```

### Disable a single event without stopping cron

In Airtable, uncheck `Enabled` on the event row. Cron skips daily-cadence sends on the next tick. Note: pending initial briefings (`Initial briefing requested at` set, `sent at` not) WILL still fire on a disabled row — staff requested the briefing explicitly, so the cron honors it. To cancel a pending briefing, clear the `Initial briefing requested at` field.

### Manually trigger one tick

```bash
cd /home/jamditis/projects/eventbrite-automation
venv/bin/python -m digest.cron --log-level=DEBUG
```

### Dry run (renders + logs, no SMTP send, no state write)

```bash
venv/bin/python -m digest.cron --dry-run --log-level=DEBUG
```

### Check what the timer thinks it's doing

```bash
systemctl list-timers digest-cron.timer --no-pager
journalctl -u digest-cron.service --since "1 hour ago"
journalctl -u digest-cron.service -f   # tail live
```

## Editing `.env.digest`

`.env.digest` is loaded by systemd via `EnvironmentFile=`. systemd's parser has specific rules — get them wrong and credentials silently break:

- One `KEY=value` per line; no shell continuations.
- **No inline comments.** `KEY=value # note` makes the comment part of the value. Comments must be on their own line, starting with `#`.
- Trailing whitespace is preserved as part of the value. Strip it.
- Quoted values: single quotes pass content through literally; double quotes interpret `\n`, `\"`, `\$`. Don't quote unless the value contains a literal `#` (which would otherwise be parsed wrong by some readers — use `KEY='value#with#hash'`).
- File mode should be `600` so credentials aren't world-readable: `chmod 600 .env.digest`.

After editing `.env.digest`:

```bash
sudo systemctl restart digest-cron.timer
journalctl -u digest-cron.service -f   # confirm next tick
```

## Common failures + fixes

### `ConfigError: missing required env vars`

`.env.digest` isn't being loaded by systemd, or a required key is empty. Check:

```bash
sudo systemctl cat digest-cron.service | grep EnvironmentFile
test -s /home/jamditis/projects/eventbrite-automation/.env.digest && echo "ok" || echo "FILE EMPTY OR MISSING"
```

### `ModuleNotFoundError: No module named 'digest'`

The `digest/` package is at the repo root, so the import works only when the working directory is the repo root. The systemd unit pins `WorkingDirectory=/home/jamditis/projects/eventbrite-automation`. If that line was hand-edited away (or the repo was moved without re-running install), Python can't find the package. Reinstall via `bash deploy/install-digest.sh`.

### `CRM transport error for ... ConnectionError: ...`

Dashboard at `pi.amditis.tech` is down or unreachable. The cron continues — affected attendee gets a form-only blurb instead of CRM-enriched. Check:

```bash
curl -I http://localhost:8081/api/contacts/  # 401 expected (auth required)
sudo systemctl status houseofjawn-dashboard
```

### `EB API 401 Unauthorized`

Eventbrite token rotated or revoked. Re-fetch from `pass`, update `.env.digest`, restart timer. (See "Editing .env.digest" above for syntax rules.)

### `EventbritePaginationError: ... has_more_items=true with no continuation token`

EB returned an inconsistent paginated response. Likely transient — re-running the next tick usually clears it. If persistent, file an EB support ticket. The affected event's `Last error` field will name it.

### `ledger says duplicate for ...`

Email ledger has a recent send for the same recipient/subject. Inspect:

```bash
sqlite3 ~/.claude/workstation/sent-emails.db \
  "SELECT id, recipient, subject, sent_at, context FROM sends \
   WHERE recipient='lead@example.com' ORDER BY sent_at DESC LIMIT 5;"
```

If the dup is real (you don't want to re-send), no action needed.
If the dup is a stale test entry blocking a legit send, `DELETE FROM sends WHERE id=...` and re-trigger manually.

### `email_ledger missing at ... falling back to no-op ledger`

The `houseofjawn-bot/scheduler/email_ledger.py` module isn't installed or isn't on `DIGEST_LEDGER_PATH`. The cron runs anyway, but the cross-session dup safety net is OFF. Primary dup defense (Airtable `Last digest sent at`) still works. To fix: clone `houseofjawn-bot` into `~/projects/`, or set `DIGEST_LEDGER_PATH` in `.env.digest`.

### Speaker emails not arriving

1. Check ledger above to confirm a send was attempted.
2. Check Bcc to `jamditis@gmail.com` — if Joe got it, the issue is downstream (recipient's spam filter, Gmail throttling).
3. Check `Last error` field in Airtable for that event row.
4. Check `journalctl -u digest-cron.service` for the affected tick.

### Cron exits immediately with `previous tick still running; exiting`

A prior tick is holding the flock. With flock, a leftover lock FILE is not itself a problem — the lock is on the open file descriptor; if no process holds it, the next run acquires it normally. To check whether a real process is still running:

```bash
ps -ef | grep -F 'digest.cron' | grep -v grep
fuser ~/.local/state/digest-cron.lock 2>/dev/null    # shows the PID holding the FD
```

If a `digest.cron` Python process is genuinely stuck, kill it (`kill <pid>`). Removing the lock file by itself does nothing for an active lock and is unnecessary for releasing a dead one.

## Rollback procedure

If a digest goes out wrong (hallucinated profiles, wrong recipients, broken formatting):

1. **Stop the timer immediately:**

   ```bash
   sudo systemctl stop digest-cron.timer
   ```

2. **Investigate via journald + Airtable `Last error`:**

   ```bash
   journalctl -u digest-cron.service --since "today" | tail -200
   ```

3. **Notify affected speakers** — Joe or Cassandra sends a correction email to the affected event's speakers list (Reply-To recipient in the bad email).

4. **Fix the bug, ship a patch.** Until merged + deployed, leave timer stopped.

5. **Re-enable the timer:**

   ```bash
   sudo systemctl start digest-cron.timer
   sudo systemctl enable digest-cron.timer
   ```

## Token rotation

| Credential | Where stored | How to rotate |
| --- | --- | --- |
| Eventbrite token | `pass show claude/api/eventbrite/eventbrite-token` | Generate new token in EB account, update pass, redeploy `.env.digest`, restart timer |
| Airtable PAT | Airtable account settings → tokens | Generate new PAT scoped to `EventDigests` base, update `.env.digest`, restart timer |
| Dashboard API key | `pass show claude/tokens/dashboard-api` | Rotate via dashboard admin, update pass + `.env.digest`, restart timer |
| SMTP app password | `pass show gmail-app-password` | Generate new app password in Google account, update pass + `.env.digest`, restart timer |

## What's NOT implemented (MVP scope)

These are explicitly out of scope for the first live-test phase. Track in follow-up tickets if any becomes a real-world problem:

- EB API retry/backoff on 5xx or 429 — the cron logs the failure into `Last error` and skips that event for the tick. The next daily tick retries it (~24h later).
- SMTP retry — same: failures log and surface; the next daily tick retries (~24h later).
- Telegram alerts on catastrophic cron failure — not wired. Operators rely on `journalctl` + `Last error` for visibility.
- Auto-refreshing `Event start (ET)` from Eventbrite each tick — staff must update manually if the EB event reschedules.
