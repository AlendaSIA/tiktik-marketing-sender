-- Sūtīšanas dzinējs (D1/D2/D3) - tables with ONE writer each. Additive only; nothing existing changes.
-- Owner: tiktik.lv › Marketing · Sūtīšanas dzinējs. 2026-09-25.

CREATE TABLE IF NOT EXISTS `jaunais-za-aizv04022026.mkt_control.contact_sequence_state` (
  master_key STRING NOT NULL, send_email STRING, lifecycle_stage STRING,
  track STRING, track_entered_on DATE, step INT64, rung INT64, rung_set_on DATE, rung_month STRING,
  ladder_cleared_on DATE, last_email_type STRING, last_template_id INT64, last_campaign_id INT64,
  last_sent_on DATE, last_send_source STRING,
  next_email_type STRING, next_template_id INT64, next_due_on DATE, next_offer_rung INT64,
  next_reason STRING, hold_reason STRING, last_order_on DATE, suppressed BOOL,
  ladder_policy STRING, run_id STRING, updated_at TIMESTAMP)
OPTIONS(description="ONE row per master_key: the lifecycle sequence + price-ladder state. SINGLE WRITER = Sūtīšanas dzinējs (sequence_job.py). Step lives ONLY here (MAIN 2026-09-25); customer_lifecycle *_step is ignored. rung NULL|1|2|3 = 10/15/20 % off today's shop price (price from the PAP engine). next_offer_rung = contract v2.8 OFFER_RUNG for the next letter (reorder = 0 always). ladder_policy='defaults-UNCONFIRMED' until Raivis confirms via MAIN.");

CREATE TABLE IF NOT EXISTS `jaunais-za-aizv04022026.mkt_control.contact_sequence_log` (
  run_id STRING, changed_at TIMESTAMP, master_key STRING, field STRING,
  before_value STRING, after_value STRING, reason STRING, shadow BOOL)
PARTITION BY DATE(changed_at)
OPTIONS(description="Append-only: every change to contact_sequence_state, before/after. Writer = Sūtīšanas dzinējs.");

CREATE TABLE IF NOT EXISTS `jaunais-za-aizv04022026.mkt_control.send_log` (
  master_key STRING, email STRING, campaign_id INT64, brevo_list_id INT64, email_type STRING,
  template_id INT64, track STRING, step INT64, rung INT64, sent_at TIMESTAMP, source STRING,
  run_id STRING, brevo_message_id STRING, utm_campaign STRING)
PARTITION BY DATE(sent_at)
OPTIONS(description="Per message, append-only. Successor of the dead business_marts.email_send_log (last row 2026-06-09). source = brevo_history (reconstructed, only after MAIN reviews brevo_campaign_class) | engine_live. SHADOW ROWS NEVER GO HERE - they live in shadow_send_plan. Writer = Sūtīšanas dzinējs.");

CREATE TABLE IF NOT EXISTS `jaunais-za-aizv04022026.mkt_control.brevo_campaign_class` (
  campaign_id INT64, name STRING, sent_date STRING, kind STRING, email_type STRING, track STRING,
  step INT64, rung INT64, counts_for_sequence BOOL, basis STRING, classified_by STRING,
  classified_at TIMESTAMP, reviewed_by STRING)
OPTIONS(description="Historic Brevo campaign -> lifecycle meaning. Filled by Sūtīšanas dzinējs, REVIEWED BY MAIN before any history goes into send_log. kind = broadcast|edu|lifecycle|pap|test|cold.");

CREATE TABLE IF NOT EXISTS `jaunais-za-aizv04022026.mkt_control.shadow_send_plan` (
  plan_date DATE, run_id STRING, master_key STRING, email STRING, track STRING, step INT64,
  email_type STRING, template_id INT64, interface_template_id INT64, offer_rung INT64,
  planned_send_date DATE, would_send BOOL, hold_reason STRING, reason STRING,
  diff_vs_prev STRING, planned_at TIMESTAMP)
PARTITION BY plan_date
OPTIONS(description="D3: per person per day, 'would send letter X (template id) on date D because ...'. NEVER calls a send API. diff_vs_prev = new|changed|same (vs the previous plan_date); a person that dropped out is a row with would_send=false, hold_reason='DROPPED'. Writer = Sūtīšanas dzinējs.");

CREATE TABLE IF NOT EXISTS `jaunais-za-aizv04022026.mkt_control.shadow_pd_writes` (
  plan_date DATE, run_id STRING, master_key STRING, email_type STRING, object STRING,
  target_person_id INT64, target_org_id INT64, subject STRING, type_key STRING, type_id INT64,
  due_date STRING, done BOOL, note STRING, record_version STRING, record_json STRING,
  planned_at TIMESTAMP)
PARTITION BY plan_date
OPTIONS(description="Raivis 2026-09-25 16:12: the exact Pipedrive write each shadow send WOULD make, one row per would-be write. Rendered by pd_record.render - the same code the live path uses (byte-for-byte test in tests/test_send_engine.py). Nothing here was written to Pipedrive.");
