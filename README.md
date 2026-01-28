# Eventbrite automation for CCM events

This system automatically creates Eventbrite draft listings when someone submits an event request through our Airtable form. It saves hours of manual work by:

- Generating a custom promotional image for each event using AI
- Creating a draft Eventbrite listing with all the event details
- Setting up tickets and event descriptions automatically

## How it works (the simple version)

```
Someone fills out    →    System automatically    →    Draft appears in
the Airtable form         creates everything           Eventbrite dashboard
```

**Step by step:**

1. **Event request submitted**: Someone fills out the CCM event request form in Airtable
2. **Automatic trigger**: Our Raspberry Pi server detects the new submission
3. **Image generated**: AI creates a professional banner image with the event title
4. **Eventbrite draft created**: The system creates a complete draft listing including:
   - Event title and description
   - Date, time, and location
   - Featured image
   - Ticket setup (free or paid)
5. **Ready for review**: The draft appears in our Eventbrite dashboard for final review before publishing

## What you need to know

### For event requesters

Just fill out the Airtable form as usual. The automation handles the rest. Your event will appear as a draft in Eventbrite within a few minutes.

**Important:** Make sure to fill out:
- Event title
- Brief description (140 characters - this shows at the top of the Eventbrite page)
- Full description (the detailed event info)
- Date and time
- Whether it's in-person or virtual

### For the events team

All events are created as **drafts** - nothing is published automatically. Before publishing:

1. Review the event details for accuracy
2. Check that the AI-generated image looks good
3. For virtual events: Add the Zoom link manually (go to Online Event Page → Add Zoom)
4. Publish when ready

### What the automation handles

| Task | Automated? | Notes |
|------|-----------|-------|
| Create Eventbrite listing | Yes | Created as draft |
| Generate featured image | Yes | AI-generated based on title |
| Add event description | Yes | Pulled from Airtable form |
| Set up tickets | Yes | Free or paid based on form |
| Create venue (in-person) | Yes | Location from form |
| Add Zoom link (virtual) | **No** | Must be added manually |
| Publish event | **No** | Requires human review |

## The status field

The automation uses the "Status" field in Airtable to track progress:

| Status | What it means |
|--------|---------------|
| Blank / Todo / In progress / Needs review | Ready to be processed |
| Eventbrite draft created | Automation completed - check Eventbrite |

## Troubleshooting

**Event didn't appear in Eventbrite?**
- Check that the Airtable record has a future date (can't create events in the past)
- Make sure all required fields are filled out
- Look for errors in the Status field

**Image looks wrong?**
- The AI does its best, but sometimes needs a redo
- You can upload a different image manually in Eventbrite

**Wrong information in the listing?**
- Edit directly in Eventbrite dashboard
- Or update Airtable and reprocess (change Status back to "Todo")

## Questions?

Contact Joe Amditis at amditisj@montclair.edu or info@centerforcooperativemedia.org
