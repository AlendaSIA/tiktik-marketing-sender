# The Phase 5 boundary - what the campaign layer owns and what the relay owns

Written 2026-09-09 by the sender's builder, for the `tiktik-relay` / `blk-ar-collection` owner.
**Proposed, not agreed.** Nothing has been built on the relay's side of this line.

## The line

| | Owner |
|---|---|
| Computing and FREEZING the day batch | campaign layer |
| Rendering the board and the digest e-mail | relay |
| Token signing, the press UI, the action endpoint | relay |
| The four press-time checks | campaign layer - **called**, never re-implemented |
| Writing the approval row | campaign layer, on the relay's call |
| Dispatching campaigns after approval | campaign layer |

## What the relay READS - one object, not a query

`mkt_control.day_batch_read`, filtered to a `send_date`. One row per campaign, already joined to the
day-level figures. Read it; do not recompute any part of it.

Per campaign: `email_type` - `track` - `utm_campaign` - `template_id` - `template_active` -
`template_approved` - `audience` - `criteria` (the distinct `chosen_because` values with counts, the
honest answer to "why these people", never re-derived) - `campaign_blocking`.

Per day: `batch_id` - `assignment_build_id` - `campaign_count` - `audience_total` - `dedup_overlap` -
`credit_headroom` - `presentable` - `blocking_reasons` - `day_note`.

**A day with zero campaigns still has a row.** The mail goes out anyway: zero planned campaigns is a
finding, and a missing report must never look like a quiet week. `presentable` is false and
`day_note` says why.

## What the relay CALLS - never re-implements

1. `press_live.verdict(send_date, credits_available, checked_by)` -> `{may_press, checks,
   refusal_text_lv}`. Runs all four checks against the LIVE state and records the judgement,
   refusals included. `refusal_text_lv` is what the board shows: every failed check, in Latvian, in
   Raivis' words, not only the first one.
2. `press_live.record_approval(send_date, approved_by, batch_id, counts_at_press)` -> refuses unless
   a passing verdict for that day already exists. **There is no path to an approval row that skips
   the checks**, and that is structural rather than procedural.

`counts_at_press` must be the numbers **as the e-mail showed them**, not as they are at the press -
otherwise nobody can later say what he actually approved.

## Transport is yours to choose, and it is not chosen here

The same code serves all three; pick one and we build the adapter:

- an HTTP endpoint on Cloud Run, called by your action endpoint;
- a Cloud Run job execution, if a press can tolerate a few seconds;
- a request row written by you and the verdict read back, if you would rather not call out at all.

## What is true today

`send_now()` raises unconditionally. `template_approval` is empty. Every mapped template is inactive
in Brevo, and 14 of the 19 reference Liquid `params`, which are always empty in a campaign. Nothing
can reach a customer through any of this.
