# Stella M/W/F attendee digest implementation plan

> **For agentic workers:** Required sub-skill: use
> `superjawn:subagent-driven-development` or `superjawn:executing-plans` to
> implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for
> tracking.

**Goal:** Add backward-compatible per-event weekday scheduling, stage the July
30 healthcare-costs webinar for Joe-only testing, and leave Stella delivery
unarmed until Joe approves the test results.

**Architecture:** The daily 7 a.m. systemd timer remains unchanged. Airtable
rows may provide `Send weekdays`; the row model validates and normalizes it,
and the cron decision layer evaluates it in `America/New_York` before either
an initial briefing or follow-up digest. Blank configuration preserves the
existing daily behavior.

**Tech stack:** Python 3.13, `datetime` and `zoneinfo`, pyairtable, pytest,
Ruff, Airtable, Eventbrite API, systemd, SMTP, and the email ledger.

---

### Task 1: Parse per-event weekdays

**Files:**

- Modify: `tests/digest/fixtures/airtable_event_row.json`
- Modify: `tests/digest/test_airtable_client.py`
- Modify: `digest/airtable_client.py`

- [x] **Step 1: Add failing row-parsing tests**

Add `Send weekdays` to the fixture:

```json
"Send weekdays": "Mon,Wed,Fri",
```

Extend `test_list_enabled_returns_event_rows`:

```python
assert row.send_weekdays == frozenset({0, 2, 4})
```

Add these tests:

```python
def test_send_weekdays_blank_preserves_daily_default(mock_pyairtable):
    mock_pyairtable.records[0]["fields"]["Send weekdays"] = ""
    client = AirtableClient(pat="pat", base_id="base", table_name="Events")
    assert client.list_enabled()[0].send_weekdays is None


def test_send_weekdays_normalizes_case_and_whitespace(mock_pyairtable):
    mock_pyairtable.records[0]["fields"]["Send weekdays"] = " monday, WED ,fri "
    client = AirtableClient(pat="pat", base_id="base", table_name="Events")
    assert client.list_enabled()[0].send_weekdays == frozenset({0, 2, 4})


def test_send_weekdays_rejects_unknown_token(mock_pyairtable):
    mock_pyairtable.records[0]["fields"]["Send weekdays"] = "Mon,Funday"
    client = AirtableClient(pat="pat", base_id="base", table_name="Events")
    with pytest.raises(EventRowSchemaError, match=r"recABC123.*Send weekdays.*Funday"):
        client.list_enabled()
```

- [x] **Step 2: Run the tests and prove they fail**

Run:

```bash
venv/bin/python -m pytest \
  tests/digest/test_airtable_client.py::test_list_enabled_returns_event_rows \
  tests/digest/test_airtable_client.py::test_send_weekdays_blank_preserves_daily_default \
  tests/digest/test_airtable_client.py::test_send_weekdays_normalizes_case_and_whitespace \
  tests/digest/test_airtable_client.py::test_send_weekdays_rejects_unknown_token -v
```

Expected: failures because `EventRow.send_weekdays` and the parser do not exist.

- [x] **Step 3: Implement weekday parsing**

Add the field constant and parser in `digest/airtable_client.py`:

```python
class FIELD:
    # Existing fields remain unchanged.
    SEND_WEEKDAYS = "Send weekdays"


_WEEKDAYS = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


def _parse_send_weekdays(
    raw: str, *, record_id: str
) -> frozenset[int] | None:
    if not raw or not raw.strip():
        return None
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    invalid = [token for token in tokens if token.lower() not in _WEEKDAYS]
    if invalid:
        raise EventRowSchemaError(
            f"record {record_id}: field {FIELD.SEND_WEEKDAYS!r} "
            f"contains unknown weekday {invalid[0]!r}"
        )
    return frozenset(_WEEKDAYS[token.lower()] for token in tokens)
```

Add `send_weekdays: frozenset[int] | None` to `EventRow` immediately before
`raw_fields`, then populate it in `from_airtable`:

```python
send_weekdays=_parse_send_weekdays(
    f.get(FIELD.SEND_WEEKDAYS, "") or "",
    record_id=record_id,
),
```

- [x] **Step 4: Re-run the parsing tests**

Run the command from Step 2.

Expected: four tests pass.

- [x] **Step 5: Commit the model change**

```bash
git add digest/airtable_client.py \
  tests/digest/test_airtable_client.py \
  tests/digest/fixtures/airtable_event_row.json
git commit -m "feat: make digest weekdays event-specific"
```

### Task 2: Gate both briefing paths by Eastern weekday

**Files:**

- Modify: `tests/digest/test_cron_decisions.py`
- Modify: `tests/digest/test_integration_e2e.py`
- Modify: `digest/cron.py`

- [ ] **Step 1: Update test row builders**

Add the field to the base dictionaries in `_row` and `_event_row`:

```python
send_weekdays=None,
```

- [ ] **Step 2: Add failing scheduling tests**

Import `is_scheduled_weekday` and `should_send_initial`, then add:

```python
def test_blank_weekdays_preserve_daily_eligibility():
    row = _row(send_weekdays=None)
    thursday = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    assert is_scheduled_weekday(row, thursday) is True


def test_mwf_row_skips_thursday():
    row = _row(
        send_weekdays=frozenset({0, 2, 4}),
        event_start_et="2026-07-30T18:00:00+00:00",
    )
    thursday = datetime(2026, 7, 23, 18, 0, tzinfo=UTC)
    assert should_send_today(row, thursday) is False


def test_mwf_row_sends_friday():
    row = _row(
        send_weekdays=frozenset({0, 2, 4}),
        event_start_et="2026-07-30T18:00:00+00:00",
    )
    friday_at_7_et = datetime(2026, 7, 24, 11, 0, tzinfo=UTC)
    assert should_send_today(row, friday_at_7_et) is True


def test_weekday_uses_eastern_calendar_date():
    row = _row(send_weekdays=frozenset({0}))
    monday_10_30_pm_et = datetime(2026, 7, 28, 2, 30, tzinfo=UTC)
    assert is_scheduled_weekday(row, monday_10_30_pm_et) is True


def test_pending_initial_waits_for_selected_weekday():
    row = _row(
        send_weekdays=frozenset({0, 2, 4}),
        initial_briefing_sent_at=None,
        initial_briefing_requested_at="2026-07-23T14:00:00+00:00",
    )
    thursday = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    friday = datetime(2026, 7, 24, 11, 0, tzinfo=UTC)
    assert should_send_initial(row, thursday) is False
    assert should_send_initial(row, friday) is True
```

- [ ] **Step 3: Run the new decision tests and prove they fail**

```bash
venv/bin/python -m pytest tests/digest/test_cron_decisions.py -v
```

Expected: import or assertion failures because the weekday helpers do not
exist and `should_send_today` does not check weekdays.

- [ ] **Step 4: Implement the decision helpers**

In `digest/cron.py`, add:

```python
def is_scheduled_weekday(row: EventRow, now: datetime) -> bool:
    return (
        row.send_weekdays is None
        or now.astimezone(ET).weekday() in row.send_weekdays
    )


def should_send_initial(row: EventRow, now: datetime) -> bool:
    return has_pending_initial_briefing(row) and is_scheduled_weekday(row, now)
```

Add this check to `should_send_today` after the enabled check:

```python
if not is_scheduled_weekday(row, now):
    return False
```

Change the main loop:

```python
if should_send_initial(row, now):
    _run_briefing(
        row,
        eb,
        crm,
        llm,
        renderer,
        sender,
        airtable,
        now,
        is_initial=True,
        dry_run=dry_run,
        logo_url=cfg.logo_url,
    )
elif row.enabled and should_send_today(row, now):
    _run_briefing(
        row,
        eb,
        crm,
        llm,
        renderer,
        sender,
        airtable,
        now,
        is_initial=False,
        dry_run=dry_run,
        logo_url=cfg.logo_url,
    )
```

- [ ] **Step 5: Run decision and integration tests**

```bash
venv/bin/python -m pytest \
  tests/digest/test_cron_decisions.py \
  tests/digest/test_integration_e2e.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit the cron change**

```bash
git add digest/cron.py \
  tests/digest/test_cron_decisions.py \
  tests/digest/test_integration_e2e.py
git commit -m "feat: honor event weekdays before digest sends"
```

### Task 3: Keep schema and operations documentation current

**Files:**

- Modify: `deploy/create-airtable-base.py`
- Modify: `docs/operations/digest-runbook.md`
- Modify: `docs/superpowers/specs/2026-05-08-eventbrite-attendee-digest-design.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add the field to new-base creation**

Place this immediately after `Send time (ET)` in `EVENTS_FIELDS`:

```python
{
    "name": "Send weekdays",
    "type": "singleLineText",
    "description": (
        "Optional comma-separated Mon through Sun. "
        "Blank keeps the every-calendar-day default."
    ),
},
```

- [ ] **Step 2: Document the decision path and operator format**

Add `Send weekdays` to the runbook state table:

```markdown
| `Send weekdays` | read | Optional comma-separated `Mon` through `Sun`. Blank means every day. Applies to initial and follow-up emails. |
```

State that invalid values produce a row error, initial requests wait for the
next configured weekday, and M/W/F is entered as `Mon,Wed,Fri`.

Add the same field to the original digest design's Airtable schema table and
the attendee-digest summary in `CLAUDE.md`.

- [ ] **Step 3: Run formatting and spec-symbol checks**

```bash
venv/bin/ruff format digest tests deploy/create-airtable-base.py
venv/bin/ruff check digest tests deploy/create-airtable-base.py
venv/bin/python -m pytest tests/digest/test_spec_symbols.py -v
git diff --check
```

Expected: Ruff and spec-symbol checks pass; `git diff --check` prints nothing.

- [ ] **Step 4: Commit schema and documentation**

```bash
git add deploy/create-airtable-base.py \
  docs/operations/digest-runbook.md \
  docs/superpowers/specs/2026-05-08-eventbrite-attendee-digest-design.md \
  CLAUDE.md
git commit -m "docs: expose weekday cadence to digest operators"
```

### Task 4: Verify and review the code branch

**Files:**

- Review all changes since `origin/master`

- [ ] **Step 1: Run the full local verification**

```bash
venv/bin/ruff format --check digest tests deploy/create-airtable-base.py
venv/bin/ruff check digest tests deploy/create-airtable-base.py
venv/bin/python -m pytest tests/digest/ -q
git diff --check origin/master...HEAD
```

Expected: Ruff passes, more than 156 tests pass, and diff check prints nothing.

- [ ] **Step 2: Run the required local Codex review floor**

```bash
codex -C /home/jamditis/projects/worktrees/eventbrite-mwf-stella \
  exec review --base origin/master -m gpt-5.5 \
  -c model_reasoning_effort="low"
```

Address genuine high- or medium-severity findings and repeat until the floor
converges. Count every pass toward the six-round cap.

- [ ] **Step 3: Run the tiered review**

```bash
codex -C /home/jamditis/projects/worktrees/eventbrite-mwf-stella \
  exec review --base origin/master -m gpt-5.6-sol \
  -c model_reasoning_effort="xhigh"
```

Address genuine findings, rerun the focused tests, and stop after six total
review rounds if convergence has not occurred.

- [ ] **Step 4: Inspect every changed line**

```bash
git diff --stat origin/master...HEAD
git diff origin/master...HEAD
git status --short --branch
```

Expected: only the approved scheduling, tests, schema, plan, and documentation
changes are present; the worktree is clean.

### Task 5: Add the Airtable field and stage a Joe-only row

**Files:**

- External: Airtable base `app8ok1uOYxcfYffv`, table `tblYyzywk9aorXJHw`

- [ ] **Step 1: Re-read the live schema and check for duplicate rows**

Use the Airtable connector or the installed CLI to confirm `Send weekdays` is
absent and no row has event ID `1994018922274` or slug
`healthcare-costs-nj-2026-07-30`.

- [ ] **Step 2: Create the field**

With `AIRTABLE_TOKEN` set from the production `.env.digest`, run:

```bash
airtable-mcp create-field --input - <<'JSON'
{
  "baseId": "app8ok1uOYxcfYffv",
  "tableId": "tblYyzywk9aorXJHw",
  "field": {
    "name": "Send weekdays",
    "type": "singleLineText",
    "description": "Optional comma-separated Mon through Sun. Blank means every day."
  }
}
JSON
```

Expected: one new field whose ID begins with `fld`.

- [ ] **Step 3: Re-read schema and resolve the new field ID**

Use `airtable-mcp list-tables-for-base --baseId app8ok1uOYxcfYffv` and record
the returned ID for `Send weekdays`. Use only returned field IDs in the create
request.

- [ ] **Step 4: Create the disabled Joe-only row**

Create exactly one record with:

```json
{
  "Event slug": "healthcare-costs-nj-2026-07-30",
  "Event title": "Using public data to report on rising healthcare costs in New Jersey",
  "Eventbrite event ID": "1994018922274",
  "Enabled": false,
  "Speaker emails": "jamditis@gmail.com",
  "Lead host email": "jamditis@gmail.com",
  "Days out to start": 7,
  "Send time (ET)": "07:00",
  "Send weekdays": "Mon,Wed,Fri",
  "Registration question IDs to include": "322741553",
  "Event start (ET)": "2026-07-30T18:00:00.000Z"
}
```

Leave every send-state field empty, especially `Initial briefing requested
at`. Re-read the record and prove `Enabled` is false and both initial-briefing
fields are empty.

### Task 6: Deploy without sending and verify production reads

**Files:**

- Deploy to: `/home/jamditis/projects/eventbrite-automation`
- Service: `digest-cron.timer`

- [ ] **Step 1: Confirm the service is idle and the timer is healthy**

```bash
systemctl is-active digest-cron.timer
systemctl is-active digest-cron.service || true
pgrep -af 'python -m digest.cron' || true
```

Expected: timer is `active`, service is `inactive`, and no cron process is
running.

- [ ] **Step 2: Fast-forward the production checkout locally**

Preserve the untracked `AGENTS.md`, switch the production checkout to local
`master`, fast-forward it to `origin/master`, then fast-forward to the tested
feature branch:

```bash
git -C /home/jamditis/projects/eventbrite-automation switch master
git -C /home/jamditis/projects/eventbrite-automation merge --ff-only origin/master
git -C /home/jamditis/projects/eventbrite-automation merge --ff-only feat/mwf-digest-cadence
```

Expected: production `master` points at the verified feature commit and
`AGENTS.md` remains untracked and unchanged.

- [ ] **Step 3: Run production verification with no send path armed**

```bash
cd /home/jamditis/projects/eventbrite-automation
venv/bin/ruff check digest tests deploy/create-airtable-base.py
venv/bin/python -m pytest tests/digest/ -q
venv/bin/python - <<'PY'
from digest.airtable_client import AirtableClient
from digest.config import load_config

cfg = load_config()
rows = AirtableClient(
    cfg.airtable_pat, cfg.airtable_base_id, cfg.airtable_table_name
).list_all()
row = next(
    item for item in rows
    if item.eventbrite_event_id == "1994018922274"
)
print(
    {
        "slug": row.slug,
        "enabled": row.enabled,
        "send_weekdays": sorted(row.send_weekdays or ()),
        "initial_requested": row.initial_briefing_requested_at,
    }
)
PY
venv/bin/python -m digest.cron --dry-run --log-level=DEBUG
```

The direct read must print weekday numbers `[0, 2, 4]`, `enabled: False`, and
an empty initial request. Because the staged row is disabled and has no
initial request, the dry run must not render or send it. Confirm its Airtable
sent-state fields remain empty.

- [ ] **Step 4: Verify the production timer**

```bash
systemctl status digest-cron.timer --no-pager
systemctl list-timers digest-cron.timer --no-pager
```

Expected: daily timer remains active at 7 a.m. Eastern. The code, not systemd,
now applies event weekdays.

### Task 7: Run Joe-only email tests and hold live activation

**Files:**

- External: staged Airtable row
- External: `~/.claude/workstation/sent-emails.db`

- [ ] **Step 1: Prove the envelope is Joe-only**

Before any test send, set `CC_ALWAYS=` and `BCC_ALWAYS=` in the manual process
environment. Re-read the row and prove both `Speaker emails` and `Lead host
email` equal `jamditis@gmail.com`. Do not modify production `.env.digest`.

- [ ] **Step 2: Stop the timer for the controlled test**

```bash
sudo systemctl stop digest-cron.timer
systemctl is-active digest-cron.timer || true
pgrep -af 'python -m digest.cron' || true
```

Expected: timer is `inactive` and no cron process is running. This prevents
the production service from picking up the test request with its standing Cc
and Bcc values.

- [ ] **Step 3: Arm only the Joe test**

Keep `Enabled` false. For the July 23 controlled test, temporarily set `Send
weekdays` to `Thu`, then set `Initial briefing requested at`. Re-read the row
and prove it is disabled, addressed only to Joe, and pending.

- [ ] **Step 4: Run the Joe-only test**

Run:

```bash
cd /home/jamditis/projects/eventbrite-automation
CC_ALWAYS= BCC_ALWAYS= venv/bin/python -m digest.cron --log-level=DEBUG
```

Expected: one email addressed only to `jamditis@gmail.com`; no Cc or Bcc;
process exits zero; Airtable records the initial send and attendee cursor.

- [ ] **Step 5: Restore the M/W/F configuration and timer**

Set `Send weekdays` back to `Mon,Wed,Fri`, leave `Enabled` false, and confirm
`Initial briefing requested at` is empty after the successful test. Then:

```bash
sudo systemctl start digest-cron.timer
systemctl is-active digest-cron.timer
```

Expected: timer is `active`, while the disabled row cannot send follow-ups.

- [ ] **Step 6: Gather test evidence**

Check:

```bash
sqlite3 ~/.claude/workstation/sent-emails.db \
  "SELECT recipient, subject, sent_at, context FROM sends
   WHERE recipient='jamditis@gmail.com'
   ORDER BY sent_at DESC LIMIT 3;"
```

Re-read the Airtable row and report the recipient, subject, active attendee
count, custom-question presence, sent timestamps, and ledger entry.

- [ ] **Step 7: Wait for live-recipient approval**

Do not change `Speaker emails` to Stella, do not enable the row, and do not arm
another initial briefing until Joe explicitly approves the test results.

- [ ] **Step 8: Activate Stella only after approval**

After approval, calculate the activation timestamp:

```bash
activation_timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
```

Resolve the record ID by searching the exact event slug, then atomically update
the row with the known live field IDs:

```bash
event_record_id=$(
  airtable-mcp search-records \
    --baseId app8ok1uOYxcfYffv \
    --table tblYyzywk9aorXJHw \
    --query '"healthcare-costs-nj-2026-07-30"' \
    --fields '["Event slug"]' -q |
  jq -er '.records | select(length == 1) | .[0].id'
)

jq -n \
  --arg record_id "$event_record_id" \
  --arg activation_timestamp "$activation_timestamp" \
  '{
    baseId: "app8ok1uOYxcfYffv",
    tableId: "tblYyzywk9aorXJHw",
    records: [{
      id: $record_id,
      fields: {
        fldzkMe2DLqNeC7fS: "smach@branchfour.org",
        fldDkS9A0dNcHaSUL: "info@centerforcooperativemedia.org",
        fldAWaa6NNnjShOnD: true,
        fldYc71eEisESTY98: $activation_timestamp,
        fldafbwFQvNqEei7H: null,
        fldF9KxnF7kdeZC0q: null,
        fldoBwALLAz8YFgkE: null,
        fldhNCHIMqStzlAqv: null,
        fldQ1SZ14zffaKWar: ""
      }
    }]
  }' |
  airtable-mcp update-records-for-table --input - -q
```

Then re-read the row, confirm the next M/W/F opportunity, and leave the timer
active. Do not manually send the live email unless Joe explicitly asks for an
immediate send.
