-- contact_weekly_assignment: the SENDING grain.
-- Exactly one row per master_key per week. Called by the sender at the start of every run
-- (bq.build_assignment), so the refresh has an owner.
--
-- ⚠ TWO WRITERS. bq.build_assignment() runs `CALL sp_build_contact_weekly_assignment()`; it
-- does NOT re-create the procedure from this file. So this file and the live BigQuery
-- routine are two independent copies of the same object, and a change to either alone
-- drifts. Change both in the same pass, always. Last synchronised 2026-09-04.
--
-- Staged in three steps on purpose: customer_lifecycle sits on a deep view stack, and
-- joining it twice inside one statement is what pushed info_email_queue over the BigQuery
-- planner limit on 2026-09-02 ("too many subqueries or query is too complex").
--
-- Rules (Raivis, 2026-09-02):
--   1. max 2 e-mails per person per week, >= 2 days apart   -> enforced by the sender (G5)
--   2. one track owns the person per week; an individual reason REPLACES the akcija
--   3. no individual reason -> next educational step; track exhausted -> akcija
--   4. individual sends go when due; the akcija goes on a fixed weekday
-- Priority: order recovery > upsell-on-order > welcome (buyer) > sign-up welcome >
--           reorder > winback > lost wave > educational > akcija
--
-- Three slots are deliberately NOT active rather than faked:
--   * order recovery - success_attribution carries no e-mail and cannot be matched to
--     checkout_attempts reliably; that match is its own build.
--   * upsell-on-order - DISABLED 2026-09-02 (Raivis via Marketing). Not a window problem:
--     it promises adding an item to an open order, which Fulfilment has not confirmed is
--     possible or at what cost, and orders ship in 0-2 business days. Re-enable the
--     commented branch below once Fulfilment answers.
--   * sign-up welcome - non-buyers are not in customer_lifecycle at all, so they have no
--     master_key and cannot appear here. Its own queue is
--     business_marts.lead_welcome_queue (built 2026-09-04, track 'signup_welcome'); it is
--     a SEPARATE series with a separate goal - the lead's first order - and carries
--     lead_source so a second generator does not merge into the hygiene-plan series.
--
-- TEMPLATE MAPPING (changed 2026-09-04). template_id is resolved from
-- mkt_control.email_template_map, joined on the email_type decided below. That view already
-- unions the manual lifecycle map with business_marts.info_email_catalog, so reading it is
-- reading the single source; reading the catalog directly here was the second source, and it
-- is what let 598 people be planned against templates Brevo had switched off. The old
-- hardcoded NULL for welcome/reorder/winback/lost is gone: it was never a check, and it
-- would have kept reorder_1 (107) and winback_1 (163) unsendable for good. Whether a mapped
-- template may actually be used is NOT decided here - the sender's plan asks track_enabled
-- and brevo_template_status.

CREATE OR REPLACE PROCEDURE `jaunais-za-aizv04022026.mkt_control.sp_build_contact_weekly_assignment`()
BEGIN

  -- Launch stamp, WRITE-ONCE (Raivis, 2026-09-04). The welcome gate is "first order on or
  -- after the moment the track went live", so that moment has to survive a rollback. H3
  -- rollback is enabled=FALSE and re-enabling is expected; if the stamp were rewritten on
  -- re-enable, everyone whose first order fell inside the disabled window would sit before
  -- the new date and never receive welcome - silently, with no error anywhere. Hence
  -- first_enabled_at is set only while it is NULL, and enabled_at may move freely.
  UPDATE `jaunais-za-aizv04022026.mkt_control.track_enabled`
  SET first_enabled_at = COALESCE(enabled_at, CURRENT_TIMESTAMP())
  WHERE enabled AND first_enabled_at IS NULL;

  CREATE OR REPLACE TABLE `jaunais-za-aizv04022026.mkt_control._assign_cl` AS
  SELECT master_key, LOWER(TRIM(email)) AS email, lifecycle_stage, next_email_type,
         first_order, last_order, days_since_last, language, segment
  FROM `jaunais-za-aizv04022026.business_marts.customer_lifecycle`
  WHERE lifecycle_stage != 'blocked';

  -- Only the queue is needed here now. The template id used to be joined in from
  -- info_email_catalog at this point; it is resolved once, from email_template_map, at the
  -- end of this procedure instead.
  CREATE OR REPLACE TABLE `jaunais-za-aizv04022026.mkt_control._assign_edu` AS
  SELECT LOWER(TRIM(q.email)) AS email, q.info_track, q.next_info_code
  FROM `jaunais-za-aizv04022026.business_marts.info_email_queue` q
  WHERE q.next_info_code IS NOT NULL;

  CREATE OR REPLACE TABLE `jaunais-za-aizv04022026.mkt_control._assign_addr` AS
  WITH sup AS (SELECT DISTINCT email FROM `jaunais-za-aizv04022026.business_marts.email_suppression_all`),
  eng AS (
    SELECT LOWER(TRIM(email)) AS email,
           COUNTIF(event IN ('opened','click','uniqueOpened','clicks')) AS engaged,
           MAX(ts) AS last_event_ts
    FROM `jaunais-za-aizv04022026.business_marts.brevo_events_raw`
    WHERE email IS NOT NULL GROUP BY 1
  ),
  cand AS (
    SELECT c.*, IFNULL(e.engaged, 0) AS engaged, e.last_event_ts,
           (c.email = LOWER(TRIM(m.freshest_email))) AS is_freshest_email
    FROM `jaunais-za-aizv04022026.mkt_control._assign_cl` c
    LEFT JOIN eng e ON e.email = c.email
    LEFT JOIN `jaunais-za-aizv04022026.business_marts.customer_master` m ON m.master_key = c.master_key
    WHERE c.email NOT IN (SELECT email FROM sup)
  )
  -- Address pick (Raivis 2026-09-02): the engaged address wins; if none is engaged, the freshest.
  SELECT *,
    CASE WHEN engaged > 0 THEN 'engaged'
         WHEN is_freshest_email THEN 'freshest_email'
         ELSE 'only_or_deterministic' END AS address_reason
  FROM cand
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY master_key
    ORDER BY (engaged > 0) DESC, last_event_ts DESC NULLS LAST, is_freshest_email DESC, email) = 1;

  CREATE OR REPLACE TABLE `jaunais-za-aizv04022026.business_marts.contact_weekly_assignment`
  PARTITION BY week_start AS
  WITH base AS (
    SELECT a.*, e.info_track, e.next_info_code,
      -- WELCOME GATE (Raivis, 2026-09-04). Entry is the FIRST ORDER, on or after the moment
      -- the welcome_buyer track first went live. Deliberately NOT orders = 1: a new buyer who
      -- orders twice in their first fortnight is still a new buyer.
      -- The backlog is not sent at all - not the 1 331 rows welcome_queue showed, not the 310
      -- of them that were genuine first-time buyers. The track therefore starts EMPTY and
      -- fills from the next order: measured 27-44 new buyers a week over the nine weeks to
      -- 31.08., averaging 33. One empty week is the correct state, not a fault.
      -- No "has never received marketing" condition, and that is deliberate: with a separate
      -- lead welcome (signup_welcome) a converted lead HAS received marketing, and excluding
      -- them here would silence buyer-welcome for exactly the people the lead flow produced.
      (a.next_email_type LIKE 'welcome%'
       AND a.first_order IS NOT NULL
       AND (SELECT first_enabled_at FROM `jaunais-za-aizv04022026.mkt_control.track_enabled`
            WHERE track = 'welcome_buyer') IS NOT NULL
       AND a.first_order >= DATE(
             (SELECT first_enabled_at FROM `jaunais-za-aizv04022026.mkt_control.track_enabled`
              WHERE track = 'welcome_buyer'), 'Europe/Riga')) AS welcome_ok
    FROM `jaunais-za-aizv04022026.mkt_control._assign_addr` a
    LEFT JOIN `jaunais-za-aizv04022026.mkt_control._assign_edu` e ON e.email = a.email
  ),
  assigned AS (
    SELECT
      DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY)) AS week_start,
      b.master_key,
      b.email,
      -- upsell_on_order is DISABLED. To re-enable, restore this branch at the top of both CASEs:
      --   WHEN b.last_order IS NOT NULL
      --    AND DATE_DIFF(CURRENT_DATE(), b.last_order, DAY) <= 7  THEN 'upsell_on_order' / 'upsell_1'
      CASE
        WHEN b.welcome_ok                                             THEN 'welcome_buyer'
        WHEN b.next_email_type LIKE 'reorder%'                        THEN 'reorder'
        WHEN b.next_email_type LIKE 'winback%'                        THEN 'winback'
        WHEN b.next_email_type = 'lost_quarterly'                     THEN 'lost_wave'
        WHEN b.next_info_code IS NOT NULL                             THEN 'educational'
        ELSE 'akcija'
      END AS track,
      CASE
        WHEN b.welcome_ok                                             THEN b.next_email_type
        WHEN b.next_email_type LIKE 'reorder%'
          OR b.next_email_type LIKE 'winback%'
          OR b.next_email_type = 'lost_quarterly'                     THEN b.next_email_type
        WHEN b.next_info_code IS NOT NULL                             THEN b.next_info_code
        ELSE 'akcija_weekly'
      END AS email_type,
      CONCAT(b.address_reason, ' | stage=', b.lifecycle_stage,
             ' | next=', IFNULL(b.next_email_type, 'none'),
             ' | edu=', IFNULL(b.info_track, 'none'),
             ' | welcome_gate=', IF(b.next_email_type LIKE 'welcome%',
                                    IF(b.welcome_ok, 'pass', 'pre_launch'), 'n/a')) AS chosen_because
    FROM base b
  )
  -- ONE mapping, resolved once, from the view that already is the single source. No dedupe
  -- here on purpose: a duplicate email_type in the map would multiply rows, and
  -- build_assignment()'s person-week invariant raises on exactly that. Loud beats averaged.
  SELECT
    a.week_start,
    a.master_key,
    a.email,
    a.track,
    a.email_type,
    m.template_id,
    a.chosen_because,
    CURRENT_TIMESTAMP() AS built_at
  FROM assigned a
  LEFT JOIN `jaunais-za-aizv04022026.mkt_control.email_template_map` m
    ON m.email_type = a.email_type;

  DROP TABLE IF EXISTS `jaunais-za-aizv04022026.mkt_control._assign_cl`;
  DROP TABLE IF EXISTS `jaunais-za-aizv04022026.mkt_control._assign_edu`;
  DROP TABLE IF EXISTS `jaunais-za-aizv04022026.mkt_control._assign_addr`;
END
