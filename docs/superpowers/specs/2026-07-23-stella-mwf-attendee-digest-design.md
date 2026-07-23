# Stella M/W/F attendee digest design

Date: 2026-07-23

Status: Approved

## Problem

Stella Mach needs registration summaries for the Center for Cooperative Media webinar "Using public data to report on rising healthcare costs in New Jersey." The webinar is Thursday, July 30, 2026, at 2 p.m. Eastern. Emails should arrive at `smach@branchfour.org` at 7 a.m. Eastern on Monday, Wednesday, and Friday before the event.

The production digest service currently supports a per-event start window and send time, but every enabled row is eligible on every calendar day. Changing the global systemd timer to Monday, Wednesday, and Friday would also change future events that still need daily delivery.

## Approved behavior

- Add an optional Airtable field named `Send weekdays`.
- Store canonical comma-separated weekday abbreviations, such as `Mon,Wed,Fri`.
- A blank field preserves the existing every-calendar-day behavior.
- A configured field applies to initial briefings and follow-up digests.
- Invalid weekday values fail that row with a useful schema error. They never fall back to a broader schedule.
- The global `digest-cron.timer` remains daily at 7 a.m. Eastern.
- Follow-up digests remain silent when no one new has registered.
- The event-start gate continues to stop delivery after the webinar begins.
- Test emails may go only to `jamditis@gmail.com`.
- No email may go to Stella until Joe reviews and approves the test results.

For this webinar, the expected opportunities are:

- Friday, July 24: initial briefing with all current registrants.
- Monday, July 27: follow-up only when new registrations exist.
- Wednesday, July 29: follow-up only when new registrations exist.
- Friday, July 31: no send because the July 30 event is over.

## Alternatives considered

### Change the global timer

Run `digest-cron.timer` only on Monday, Wednesday, and Friday. This requires little code, but it changes every digest event and removes the current daily default.

### Add one-off scheduled jobs

Create three date-specific systemd timers or cron entries for this webinar. This avoids a schema change, but duplicates scheduling logic, requires manual cleanup, and is harder to verify through the existing Airtable state.

### Add per-event weekday configuration

Keep the global timer daily and make weekday eligibility part of the row decision. This is the approved approach because it is reusable, preserves existing event behavior, and keeps schedule state visible in Airtable.

## Architecture

The existing service boundaries remain:

1. systemd starts `digest.cron` daily at 7 a.m. Eastern.
2. `AirtableClient` reads enabled rows and pending initial briefings.
3. `EventRow` parses `Send weekdays` into weekday numbers used by Python's timezone-aware `datetime`.
4. The cron checks the Eastern calendar weekday before either the initial or follow-up path.
5. Eligible rows continue through the existing event window, duplicate-send, Eventbrite, rendering, SMTP, ledger, and Airtable state paths.

The weekday check belongs in the decision layer. Eventbrite fetching and email rendering do not need schedule knowledge.

## Data model

Add this field to the `Events` table in base `app8ok1uOYxcfYffv`:

| Field | Type | Meaning |
| --- | --- | --- |
| `Send weekdays` | Single line text | Optional comma-separated `Mon` through `Sun`. Blank means every day. |

Add this event row:

| Field | Value |
| --- | --- |
| Event slug | `healthcare-costs-nj-2026-07-30` |
| Event title | `Using public data to report on rising healthcare costs in New Jersey` |
| Eventbrite event ID | `1994018922274` |
| Enabled | No during testing |
| Speaker emails | `jamditis@gmail.com` during testing; `smach@branchfour.org` after Joe approves |
| Lead host email | `jamditis@gmail.com` during testing; `info@centerforcooperativemedia.org` for live delivery |
| Days out to start | `7` |
| Send time (ET) | `07:00` |
| Send weekdays | `Mon,Wed,Fri` |
| Registration question IDs to include | `322741553` |
| Event start (ET) | `2026-07-30T18:00:00.000Z` |
| Initial briefing requested at | Set only for an approved test or live send |

The attendee sheet URL remains blank because no event-specific sheet was supplied. Standing Cc and Bcc recipients remain controlled by `.env.digest`.

Using Joe as both the test recipient and test Reply-To keeps the email-ledger
key separate from live delivery. The ledger keys on Reply-To plus
`event-slug:kind`, so the test cannot suppress the later live initial briefing
after Reply-To changes to `info@centerforcooperativemedia.org`.

Manual test sends must start the cron with empty `CC_ALWAYS` and `BCC_ALWAYS`
environment overrides. This prevents the production standing copies from
receiving a test intended only for Joe. The live systemd environment remains
unchanged.

## Error handling

- Empty `Send weekdays` accepts every weekday for backward compatibility.
- Whitespace and case are normalized.
- An unknown token raises `EventRowSchemaError` with the Airtable record ID and field name.
- A pending initial briefing on a non-selected weekday remains pending for the next selected weekday.
- Existing recipient, event-window, same-day, email-ledger, SMTP, and Airtable write protections remain unchanged.

## Testing

Write tests before implementation for:

- blank weekday configuration preserving daily eligibility;
- parsing `Mon,Wed,Fri`, including whitespace and case normalization;
- rejecting invalid tokens with the record ID in the error;
- Eastern weekday evaluation when UTC is on a different calendar day;
- initial briefings waiting for an eligible weekday;
- follow-up digests sending on Monday, Wednesday, and Friday and skipping other days;
- event-end suppression after July 30;
- parsing the new field from an Airtable fixture.

Run the full digest suite, Ruff checks, a production-data dry run with SMTP disabled by `--dry-run`, and a live systemd service invocation that proves the deployed code reads the new Airtable row without sending early.

## Rollout and verification

1. Deploy the tested branch to the production checkout on houseofjawn.
2. Add `Send weekdays` to the Airtable table.
3. Add the webinar row with `jamditis@gmail.com` as the test recipient and
   test Reply-To, disabled, and without the initial request timestamp.
4. Run `digest.cron --dry-run` and inspect the rendered recipient, event, attendee count, question answer, and weekday decision.
5. Run the manual test with `CC_ALWAYS=` and `BCC_ALWAYS=` so only
   `jamditis@gmail.com` receives it. Verify the process result, Airtable state,
   and email-ledger record.
6. Show Joe the rendered email and test evidence. Wait for explicit approval
   before changing the recipient to Stella.
7. After approval, reset `Initial briefing sent at`, `Last digest sent at`,
   `Last attendee cursor`, and `Last digest attendee count`; change Speaker
   emails to `smach@branchfour.org`; change Lead host email to
   `info@centerforcooperativemedia.org`; set `Enabled`; and arm the initial
   briefing.
8. Confirm the timer is active and the next eligible trigger.
9. After the first live run, verify the systemd exit status, Airtable sent
   fields, and email-ledger record.

## Research notes

- Eventbrite's official attendee documentation confirms the event-attendee endpoint is paginated and returns attendee registration answers: <https://www.eventbrite.com/platform/docs/attendees>.
- Eventbrite's API basics document says collection endpoints paginate results. The existing client already follows continuation tokens: <https://www.eventbrite.com/platform/docs/api-basics>.
- Python's `datetime.weekday()` uses Monday as `0` through Sunday as `6`, which provides a small internal representation for the configured days: <https://docs.python.org/3/library/datetime.html#datetime.datetime.weekday>.
- The production Eventbrite API returned two active attendees and registration question ID `322741553` for event `1994018922274` on 2026-07-23.
- The current digest suite passed all 156 tests before this change. The production systemd timer was active and its 2026-07-23 run exited successfully.
