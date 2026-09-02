# tiktik-marketing-sender

Lifecycle e-mail sender for tiktik.lv. Cloud Run job, `europe-west1`.

This is a **rewrite**, decided by Raivis on 2026-09-02. The predecessor was a Cloud Run job
with **no source repository**, no scheduler and no identity guard. It ran three times in its
life, last on 2026-06-04, and it wrote 7 668 log rows sharing one timestamp with NULL list
ids — which is why five days of send history in June were unreadable and briefly looked
like an unknown second sender. Nothing about that image is recoverable or worth recovering:
the design changed underneath it (individual sends are the default, the weekly akcija is the
residual, the reactivation ladder is prices rather than discount codes).

## The two grains — the rule this job exists to enforce

| | grain | why |
|---|---|---|
| **storage** (`customer_lifecycle`) | one row per **deliverable address** | we send to an address; a row is a CONTACT (identity rule a) |
| **sending** (this job) | one row per **person** (`master_key`) | otherwise the same person gets the same e-mail twice |

Measured on 2026-09-02 before this job existed: 359 people held 734 mailable addresses,
i.e. **375 duplicate sends per wave**, none of them conflicting — the same e-mail twice.
Address choice, when a person has several: **the engaged address wins; if none is engaged,
the freshest.** Cost of that rule, measured: 31 live readers across the whole base, against
122 people who are currently also mailed at a dead address.

## Guarantees

| id | guarantee | enforced in |
|---|---|---|
| G1 | `IDENTITY_STALE` — identity older than `IDENTITY_MAX_AGE_H` aborts the run | `step1_identity_guard` |
| G2 | Brevo contact state is refreshed by this job, then asserted fresh | `step2_refresh_suppression` |
| G3 | at most one row per `master_key` in the send set, or abort | `step4_plan` |
| G4 | send set ∩ suppression = 0, or abort | `step4_plan` |
| G5 | ≤ `MAX_EMAILS_PER_WEEK` per person, ≥ `MIN_DAYS_BETWEEN` days apart | plan SQL, counted per master |
| G6 | every message re-checks the person against suppression immediately before sending | `step6_send` |
| G7 | one log row per message, with its own timestamp and Brevo message id | `step6_send` |
| G8 | dry run is the default; sending needs an explicit switch **and** a clean plan | `step5_gate` |

**G2 is not an optional extra.** `email_suppression_all` reads Brevo *contact state*
(`emailBlacklisted`). Until 2026-09-02 it read only Brevo *events*, so 3 457 blocklisted
contacts existed of which **2 350 were still mailable by us**. A suppression source nobody
refreshes reads as covered and rots — the same failure mode in a fourth place. Whoever is
about to send refreshes it. That is this job.

## Dry run is a mode, not a phase

`DRY_RUN=true` (the default) computes everything — guards, coverage, plan, decisions — and
writes `mkt_control.send_plan` plus one row in `mkt_control.sender_run_report`. Nothing is
sent. Sending additionally requires `DRY_RUN=false`, `ALLOW_SEND=true`, and a plan where
`orphans_mailable = 0` and `duplicate_sends = 0`. Any other state logs the refusal and
exits 0.

## Inputs

| object | role |
|---|---|
| `business_marts.contact_weekly_assignment` | **one row per person per week** — which track owns them and which single address to use. Built separately (step 3). Absent ⇒ this job reports and sends nothing. |
| `business_marts.customer_lifecycle` | personalisation and stage, address grain |
| `business_marts.email_suppression_all` | consent, master-resolved |
| `business_marts.brevo_contacts_snapshot` | Brevo contact state + list membership (refreshed here) |
| `business_marts.email_send_log` | frequency history, counted per `master_key` |

## Outputs

- `business_marts.email_send_log` — one row per message (G7)
- `mkt_control.send_plan` — the full plan with a `decision` per row
- `mkt_control.sender_run_report` — one row per run: guards, coverage, counts

## Environment

| var | default | note |
|---|---|---|
| `DRY_RUN` | `true` | anything but `false` means dry run |
| `ALLOW_SEND` | `false` | must be `true` **as well** to send |
| `SEND_LIMIT` | `0` | 0 = uncapped; use a small number for the first live run |
| `IDENTITY_MAX_AGE_H` | `36` | |
| `SUPPRESSION_MAX_AGE_H` | `26` | daily refresh + margin |
| `MAX_EMAILS_PER_WEEK` | `2` | per person |
| `MIN_DAYS_BETWEEN` | `2` | per person |
| `REFRESH_SNAPSHOT` | `true` | needs `BREVO_API_KEY` |
| `BREVO_API_KEY` | — | Secret Manager `BREVO_API_KEY` |

## Build and deploy

Build only from `cloudbuild.yaml`; deployment stays an explicit command so an env change is
never smuggled in by a build:

```
gcloud builds submit --config cloudbuild.yaml .
gcloud run jobs update tiktik-marketing-sender \
  --region=europe-west1 \
  --image=europe-west1-docker.pkg.dev/$PROJECT/jobs/tiktik-marketing-sender:$SHORT_SHA
```

**No scheduler yet, on purpose.** Scheduling is a separate step and comes only after a dry
run reports `orphans_mailable = 0` and `duplicate_sends = 0`.

## Home in the tree

`Company-Alenda-SIA/shared-platforms/03-data-analytics.md` holds the data contract and the
identity rules; the sender itself belongs to `tiktik.lv › Marketing › Email campaigns`.
Build id `blk-tiktik-marketing-sender`.
