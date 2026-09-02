-- contact_weekly_assignment: the SENDING grain.
-- Exactly one row per master_key per week. Called by the sender at the start of every run
-- (bq.build_assignment), so the refresh has an owner.
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
-- Two slots are deliberately NOT implemented rather than faked:
--   * order recovery - success_attribution carries no e-mail and cannot be matched to
--     checkout_attempts reliably; that match is its own build.
--   * sign-up welcome - non-buyers are not in customer_lifecycle at all, so they have no
--     master_key and cannot appear here. That is step 6 (Marketing).

CREATE OR REPLACE PROCEDURE `jaunais-za-aizv04022026.mkt_control.sp_build_contact_weekly_assignment`()
BEGIN

  CREATE OR REPLACE TABLE `jaunais-za-aizv04022026.mkt_control._assign_cl` AS
  SELECT master_key, LOWER(TRIM(email)) AS email, lifecycle_stage, next_email_type,
         last_order, days_since_last, language, segment
  FROM `jaunais-za-aizv04022026.business_marts.customer_lifecycle`
  WHERE lifecycle_stage != 'blocked';

  CREATE OR REPLACE TABLE `jaunais-za-aizv04022026.mkt_control._assign_edu` AS
  SELECT LOWER(TRIM(q.email)) AS email, q.info_track, q.next_info_code,
         c.brevo_template_id AS edu_template_id
  FROM `jaunais-za-aizv04022026.business_marts.info_email_queue` q
  LEFT JOIN `jaunais-za-aizv04022026.business_marts.info_email_catalog` c
    ON c.code = q.next_info_code AND c.active
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
  SELECT
    DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY)) AS week_start,
    a.master_key,
    a.email,
    CASE
      WHEN a.last_order IS NOT NULL
       AND DATE_DIFF(CURRENT_DATE(), a.last_order, DAY) <= 7        THEN 'upsell_on_order'
      WHEN a.next_email_type LIKE 'welcome%'                        THEN 'welcome_buyer'
      WHEN a.next_email_type LIKE 'reorder%'                        THEN 'reorder'
      WHEN a.next_email_type LIKE 'winback%'                        THEN 'winback'
      WHEN a.next_email_type = 'lost_quarterly'                     THEN 'lost_wave'
      WHEN e.next_info_code IS NOT NULL                             THEN 'educational'
      ELSE 'akcija'
    END AS track,
    CASE
      WHEN a.last_order IS NOT NULL
       AND DATE_DIFF(CURRENT_DATE(), a.last_order, DAY) <= 7        THEN 'upsell_1'
      WHEN a.next_email_type LIKE 'welcome%'
        OR a.next_email_type LIKE 'reorder%'
        OR a.next_email_type LIKE 'winback%'
        OR a.next_email_type = 'lost_quarterly'                     THEN a.next_email_type
      WHEN e.next_info_code IS NOT NULL                             THEN e.next_info_code
      ELSE 'akcija_weekly'
    END AS email_type,
    -- Only the educational catalog carries Brevo template ids today. Lifecycle e-mails have
    -- no template mapping anywhere in BigQuery, and the akcija is a LIST CAMPAIGN, not a
    -- per-person send - so NULL for the akcija is correct, not a gap to be filled.
    CASE
      WHEN a.last_order IS NULL OR DATE_DIFF(CURRENT_DATE(), a.last_order, DAY) > 7
       THEN IF(a.next_email_type LIKE 'welcome%' OR a.next_email_type LIKE 'reorder%'
               OR a.next_email_type LIKE 'winback%' OR a.next_email_type = 'lost_quarterly',
               NULL, SAFE_CAST(e.edu_template_id AS INT64))
      ELSE NULL
    END AS template_id,
    CONCAT(a.address_reason, ' | stage=', a.lifecycle_stage,
           ' | next=', IFNULL(a.next_email_type, 'none'),
           ' | edu=', IFNULL(e.info_track, 'none')) AS chosen_because,
    CURRENT_TIMESTAMP() AS built_at
  FROM `jaunais-za-aizv04022026.mkt_control._assign_addr` a
  LEFT JOIN `jaunais-za-aizv04022026.mkt_control._assign_edu` e ON e.email = a.email;

  DROP TABLE IF EXISTS `jaunais-za-aizv04022026.mkt_control._assign_cl`;
  DROP TABLE IF EXISTS `jaunais-za-aizv04022026.mkt_control._assign_edu`;
  DROP TABLE IF EXISTS `jaunais-za-aizv04022026.mkt_control._assign_addr`;
END
