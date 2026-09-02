-- Applied 2026-09-02. Kept here so the schema the sender depends on is reviewable
-- next to the code that writes it.

-- G7: per-message logging needs a person key, a run key and the provider's message id.
ALTER TABLE `jaunais-za-aizv04022026.business_marts.email_send_log`
  ADD COLUMN IF NOT EXISTS master_key       STRING,
  ADD COLUMN IF NOT EXISTS run_id           STRING,
  ADD COLUMN IF NOT EXISTS brevo_message_id STRING,
  ADD COLUMN IF NOT EXISTS assignment_week  DATE;

-- The dry run's output. One row per planned recipient, with why it would or would not go.
CREATE TABLE IF NOT EXISTS `jaunais-za-aizv04022026.mkt_control.send_plan` (
  run_id STRING, planned_at TIMESTAMP, week_start DATE,
  master_key STRING, email STRING, track STRING, email_type STRING,
  template_id INT64, decision STRING, lifecycle_stage STRING, dry_run BOOL
) PARTITION BY DATE(planned_at);

-- One row per run: the guarantee instrument. If this table is empty, the job did not run.
CREATE TABLE IF NOT EXISTS `jaunais-za-aizv04022026.mkt_control.sender_run_report` (
  run_id STRING, started_at TIMESTAMP, finished_at TIMESTAMP,
  dry_run BOOL, allow_send BOOL, status STRING, error STRING,
  identity_age_h FLOAT64, snapshot_age_h FLOAT64,
  list3_total INT64, list3_in_engine INT64, orphans_mailable INT64,
  sendable_rows INT64, sendable_people INT64, duplicate_sends INT64,
  plan_rows INT64, plan_send INT64,
  sent INT64, skipped_at_send_time INT64, failed INT64
) PARTITION BY DATE(started_at);
