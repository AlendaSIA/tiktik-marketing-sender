-- Dispatch fact, the ladder, and the two Phase 3 objects. Applied 2026-09-09.
--
-- WHY THIS FILE EXISTS AT ALL, and it is not the reason the work order gave.
-- The ladder seed was specified as "seed reorder and winback steps from email_send_log".
-- Checked against the live warehouse first: customer_lifecycle is a VIEW and it ALREADY
-- derives welcome_step / reorder_step / winback_step from email_send_log, joined on
-- LOWER(TRIM(email)) and filtered to send_status='sent'. The seed is built. What is empty is
-- its SOURCE: email_send_log has never held a single reorder_* or winback_* row, its last row
-- of any kind is 2026-06-09, and Brevo has sent 60 campaigns since - five of them
-- reactivation- or replenishment-shaped. brevo_events_raw carries transactional mail only, so
-- no campaign send after 09.06 is recorded anywhere in BigQuery.
--
-- So a seed would have written step 0 for everyone, which is what the steps already are, and
-- would have made a dead log look like a decision. The fix is not to seed the ladder from a
-- log nobody writes; it is to WRITE THE LOG FROM THE DISPATCH FACT, which is what this file
-- does. One change closes three things at once: the ladder advances by itself, G7 (one log
-- row per letter) becomes true for the first time, and G5 stops being vacuous because these
-- rows carry master_key - all 22 585 legacy rows carry NULL.

-- ---------------------------------------------------------------------------
-- The invariant that ties the dispatch fact to the log. Defined ONCE, here, and read by the
-- job rather than restated in it - the same rule assignment_grain_violation follows.
-- Empty is the only acceptable state after a dispatch.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW `jaunais-za-aizv04022026.mkt_control.dispatch_log_mismatch`
OPTIONS(description="Disagreements between campaign_audience_snapshot (the dispatch fact) and email_send_log (the per-letter record the ladder is derived from). Three kinds: a snapshot row marked sent with no log row, a sender-written log row with no snapshot row, and a duplicated log row for one campaign and address. Scoped to the sender's own log rows via run_id IS NOT NULL - every one of the 22 585 legacy rows carries NULL there, so history cannot raise a false violation.") AS
WITH sent AS (
  SELECT send_date, brevo_campaign_id, master_key, email, email_type
  FROM `jaunais-za-aizv04022026.mkt_control.campaign_audience_snapshot`
  WHERE dispatch_state = 'sent'
),
logged AS (
  SELECT DATE(sent_at) AS sent_date, campaign_id, master_key,
         LOWER(TRIM(email)) AS email, email_type
  FROM `jaunais-za-aizv04022026.business_marts.email_send_log`
  WHERE run_id IS NOT NULL
),
dupes AS (
  SELECT ANY_VALUE(sent_date) AS sent_date, campaign_id, ANY_VALUE(master_key) AS master_key,
         email, ANY_VALUE(email_type) AS email_type
  FROM logged
  GROUP BY campaign_id, email
  HAVING COUNT(*) > 1
)
SELECT 'SENT_WITHOUT_LOG' AS violation, s.send_date, s.brevo_campaign_id AS campaign_id,
       s.email_type, s.master_key, s.email
FROM sent s
LEFT JOIN logged l ON l.campaign_id = s.brevo_campaign_id AND l.email = s.email
WHERE l.email IS NULL
UNION ALL
SELECT 'LOG_WITHOUT_SNAPSHOT', l.sent_date, l.campaign_id, l.email_type, l.master_key, l.email
FROM logged l
LEFT JOIN sent s ON s.brevo_campaign_id = l.campaign_id AND s.email = l.email
WHERE s.email IS NULL
UNION ALL
SELECT 'LOG_ROW_DUPLICATED', d.sent_date, d.campaign_id, d.email_type, d.master_key, d.email
FROM dupes d;

-- ---------------------------------------------------------------------------
-- PHASE 3 ITEM 15 - the membership plan blk-brevo-contacts-snapshot consumes.
-- It is a VIEW over the planned rows and not a second computation of who belongs where. The
-- snapshot job materialises Brevo lists from this; it must never re-derive membership, or we
-- hold two answers to "who was in that campaign" - the defect class that produced two pricing
-- engines and two template mappings.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW `jaunais-za-aizv04022026.mkt_control.variant_list_plan`
OPTIONS(description="One row per person per variant per sending day, from the planned rows of campaign_audience_snapshot. THE membership plan for the per-variant Brevo lists. Read it; do not re-derive it. chosen_because travels with the row so the approval report can print why this person is here.") AS
SELECT
  send_date, email_type, track, utm_campaign, template_id, brevo_list_id,
  master_key, email, chosen_because, week_start, snapshot_id
FROM `jaunais-za-aizv04022026.mkt_control.campaign_audience_snapshot`
WHERE dispatch_state = 'planned';

CREATE OR REPLACE VIEW `jaunais-za-aizv04022026.mkt_control.variant_list_plan_summary`
OPTIONS(description="The header the list builder reads first: one row per variant per sending day with its audience size. distinct_people and members must be equal - if they are not, one person is in the list twice and day_list_overlap says who.") AS
SELECT
  send_date, email_type, track, utm_campaign, template_id,
  COUNT(*) AS members,
  COUNT(DISTINCT master_key) AS distinct_people,
  MIN(snapshot_id) AS first_snapshot_id,
  MAX(snapshot_id) AS last_snapshot_id
FROM `jaunais-za-aizv04022026.mkt_control.variant_list_plan`
GROUP BY 1,2,3,4,5;

-- ---------------------------------------------------------------------------
-- PHASE 3 ITEM 17 - the union-dedup assertion across all of a day's lists.
-- One person in two of the day's lists stops the day. Run at materialisation AND again at the
-- press against the live lists: the mail is written from one moment and pressed at another,
-- and the whole point is that the second moment is checked rather than assumed.
--
-- Under the current design the assignment's (week_start, master_key) key makes this
-- impossible by construction - which is exactly why it is worth asserting. It fires when
-- something ELSE has written planned rows: a stale build, a campaign layer adding people, a
-- second writer nobody declared. A guard that only proves what the key already guarantees is
-- cheap; the day it fires it is the only thing standing between a customer and two letters.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW `jaunais-za-aizv04022026.mkt_control.day_list_overlap`
OPTIONS(description="People planned into more than one variant, or into one variant twice, on a single send_date. Empty is the only acceptable state; a non-empty result stops that sending day.") AS
SELECT
  send_date,
  master_key,
  COUNT(DISTINCT email_type) AS variants,
  COUNT(*) AS rows_for_person_day,
  STRING_AGG(DISTINCT email_type ORDER BY email_type) AS email_types,
  ANY_VALUE(email) AS email
FROM `jaunais-za-aizv04022026.mkt_control.campaign_audience_snapshot`
WHERE dispatch_state = 'planned'
GROUP BY send_date, master_key
HAVING COUNT(DISTINCT email_type) > 1 OR COUNT(*) > 1;

-- ---------------------------------------------------------------------------
-- The assignment build log. Append-only, and it exists because of a defect Data & analytics
-- found today: contact_weekly_assignment was replaced TWICE on 2026-09-09 - 07:31 scheduled
-- and 12:40 from a dry run - and built_at is overwritten by every rebuild, so the only trace
-- of the earlier build disappears at the next one.
--
-- build_id is a CONTENT hash, not a timestamp, and that is the whole design. Raivis' approval
-- e-mail records the build_id it was written from and the press compares it against the live
-- one. A rebuild that produces the same audience keeps the same id, so a harmless rerun does
-- not invalidate an approved batch; a rebuild that moves one person to another variant
-- changes it and the press refuses. Comparing timestamps would refuse on every single run and
-- would therefore be switched off within a week - a signal that is always on is not a signal.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `jaunais-za-aizv04022026.mkt_control.assignment_build_log` (
  built_at        TIMESTAMP,
  run_id          STRING,
  assignment_week_hash STRING,  -- renamed from build_id 2026-09-11 (MAIN 18:50): build_id is only the day identity
  rows_written    INT64,
  distinct_people INT64,
  same_as_previous BOOL
) PARTITION BY DATE(built_at)
OPTIONS(description="One row per rebuild of business_marts.contact_weekly_assignment. The assignment table itself is CREATE OR REPLACEd every run and keeps no history, so this is the only place a rebuild leaves a trace. same_as_previous=TRUE means the rebuild changed nobody's variant or sending day.");
