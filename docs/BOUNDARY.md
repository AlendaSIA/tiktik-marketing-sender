# The Phase 5 boundary - what the campaign layer owns and what the relay owns

Written 2026-09-09 by the sender's builder, for the `tiktik-relay` / `blk-ar-collection` owner.
The SEAM CONTRACT below (section "The seam, chosen") is **agreed and fixed** - it was issued to the
relay side on 2026-09-09 in this same form. The rest of this document describes the campaign side's
half of it. Nothing has been built on the relay's side of the line yet.

## The line

| | Owner |
|---|---|
| Computing and FREEZING the day batch | campaign layer |
| Pushing the frozen batch to the relay | campaign layer |
| Rendering the board and the digest e-mail | relay |
| Token signing, the press UI, the action endpoint | relay |
| The four press-time checks | campaign layer - **called**, never re-implemented |
| Writing the approval row | campaign layer, on the relay's call |
| Dispatching campaigns after approval | campaign layer |

## The relay has NO BigQuery access. This is the fact the seam is built around.

An earlier draft of this document assumed the relay could read `mkt_control.day_batch_read` itself.
**It cannot** - no BigQuery client, no GCP credentials, and none are planned. Any design in which the
relay pulls the batch is impossible, not merely inconvenient. `day_batch_read` remains the campaign
side's own object: it is what the campaign job reads in order to build the payload it pushes.

Per campaign it carries: `email_type` - `track` - `utm_campaign` - `template_id` - `template_active` -
`template_approved` - `audience` - `criteria` (the distinct `chosen_because` values with counts, the
honest answer to "why these people", never re-derived) - `campaign_blocking`.

Per day: `batch_id` - `assignment_build_id` - `campaign_count` - `audience_total` - `dedup_overlap` -
`credit_headroom` - `presentable` - `blocking_reasons` - `day_note`.

**A day with zero campaigns still has a row.** The mail goes out anyway: zero planned campaigns is a
finding, and a missing report must never look like a quiet week. `presentable` is false and
`day_note` says why.

---

# The seam, chosen

A boundary document that names three possible transports and chooses none is not a boundary. Two
directions, two mechanisms, both fixed.

## A) PUSH: campaign -> relay

After the night build the campaign job **POSTs the frozen batch to the relay's `send-ingest.php`**
with a shared secret.

Payload, at the top level: `batch_id`, `build_id`, `target_send_date`, `generated_at`.
Per campaign: variant, audience count, `chosen_because`, template id, template approval state, UTM slug.
Per day: the dedup figure, and the Brevo credits.

The relay **stores it byte for byte** and renders from what it stored. It derives nothing - not even
sums. Every number in the mail is a number the campaign side computed and the relay repeated.

**A FAILED PUSH IS AN ERROR IN THE NIGHT REPORT, not a silent skip.** It lands in
`mkt_control.campaign_run_report` as a refusal with a reason, exactly like a structural refusal, and
it is loud. "Nothing arrived" and "there was nothing to send" are two different mails and must never
look alike.

A repeated push with the same `build_id` is **idempotent** - the relay stores it again over the same
row and nothing else happens. A *different* `build_id` after a mail has already gone out means the
day was recomputed: the relay voids the old token and sends a short "day recomputed" mail with a new
button.

## B) PRESS: relay -> campaign

**ONE authenticated HTTP endpoint.** The relay POSTs to it; the campaign side answers.

`tiktik-marketing-sender` is a Cloud Run **JOB** and has no endpoint. The recommendation is a Cloud
Run **SERVICE built from the SAME image and the SAME code**, with a different entry point, so that
there stays **one `press.py`, not two**. A second implementation of the checks is a second set of
rules, and the whole point of the checks is that there is exactly one. If a better form presents
itself, it may be written instead - with the justification written next to it.

The relay POSTs: `batch_id`, `build_id`, the counts **AS THEY WERE IN THE MAIL** (not as they are at
the moment of the press - otherwise nobody can later say what he actually approved), the presser, and
the time.

The campaign side runs `press_live.verdict(...)` and, if it passes, `press_live.record_approval(...)`,
and **returns the verdict in both cases**, with the failed checks as text that can be shown to a human
in Latvian (`refusal_text_lv` - every failed check, not only the first).

**THE APPROVAL ROW IS WRITTEN ONLY BY THE CAMPAIGN SIDE. The relay is forbidden to write it.**
`record_approval()` refuses unless a passing verdict for that day already exists, so there is no path
to an approval row that skips the checks - structural, not procedural.

**ONE endpoint, not two.** A separate "preview the verdict" endpoint would be a second road to the
same row, and the second road is always the one that is open when it should not be.

**An unreachable endpoint = the relay REFUSES and records the refusal.** Timeout, 500, empty answer:
the day does not go. No silent retry, no optimistic pass. A refusal that is not recorded is
afterwards indistinguishable from a button nobody pressed.

## Secrets - form and name only

The values never pass through a conversation. If a value appears in a chat it is burned. Raivis places
them himself.

| What | Where it lives | Form |
|---|---|---|
| `relay_secret` | Secret Manager, project `jaunais-za-aizv04022026`, secret name `relay_secret`; on the relay side, the server's own config file outside the web root | one long random string, shared by both sides, sent by the campaign side in a header on the push |
| relay push URL | env var `RELAY_INGEST_URL` on `tiktik-campaign-layer` | `https://<relay host>/send-ingest.php` |
| press endpoint credential | Secret Manager, secret name `press_endpoint_secret`; on the relay side, the same config file | one long random string, sent by the relay in a header on the press call |
| press endpoint URL | the relay's config file | the Cloud Run **service** URL |

## What is true today

`send_now()` raises unconditionally. `template_approval` is empty. 14 of the 19 mapped templates
reference Liquid `params`, which are always empty in a campaign, and are blocked for that reason.
Template 35 is active in Brevo as of 2026-09-09 18:00 UTC and is the one template intended for the
first end-to-end. Nothing in this repository can reach a customer.
