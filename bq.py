"""BigQuery access. Every statement the sender runs lives here, so the data contract is
readable in one file.
"""
import logging
from google.cloud import bigquery

import config as C

log = logging.getLogger("bq")
_client = None


def client() -> bigquery.Client:
    global _client
    if _client is None:
        _client = bigquery.Client(project=C.PROJECT, location=C.BQ_LOCATION)
    return _client


def query(sql: str, params=None):
    job_config = bigquery.QueryJobConfig(query_parameters=params or [])
    return list(client().query(sql, job_config=job_config).result())


def scalar(sql: str, params=None):
    rows = query(sql, params)
    return None if not rows else list(rows[0].values())[0]


def table_exists(fq_backticked: str) -> bool:
    plain = fq_backticked.strip("`")
    try:
        client().get_table(plain)
        return True
    except Exception:  # noqa: BLE001 - NotFound and permission both mean "cannot use it"
        return False


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #
def identity_age_hours() -> float:
    return scalar(f"""
        SELECT TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), LEAST(
                 (SELECT MAX(built_at) FROM {C.T_IDENTITY}),
                 (SELECT MAX(built_at) FROM {C.T_MASTER})), MINUTE) / 60
    """)


def snapshot_age_hours() -> float:
    return scalar(f"""
        SELECT TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), TIMESTAMP_MILLIS(last_modified_time), MINUTE) / 60
        FROM `{C.PROJECT}.{C.MARTS}.__TABLES__` WHERE table_id = 'brevo_contacts_snapshot'
    """)


def load_snapshot_from_gcs(uri: str) -> int:
    job = client().load_table_from_uri(
        uri,
        C.SNAPSHOT_TABLE_PLAIN,
        job_config=bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            schema=[
                bigquery.SchemaField("email", "STRING"),
                bigquery.SchemaField("list_ids", "INTEGER", mode="REPEATED"),
                bigquery.SchemaField("added_time", "DATE"),
                bigquery.SchemaField("modified_time", "DATE"),
                bigquery.SchemaField("email_subscribed", "BOOL"),
                bigquery.SchemaField("email_blocklisted", "BOOL"),
            ],
        ),
    )
    job.result()
    return int(scalar(f"SELECT COUNT(*) FROM {C.T_SNAPSHOT}"))


# --------------------------------------------------------------------------- #
# Weekly assignment - built here, by us, at the start of every run
# --------------------------------------------------------------------------- #
def build_assignment() -> int:
    """Rebuild contact_weekly_assignment, then assert its two invariants.

    One row per person per week, and no suppressed address in it. Both are checked here
    rather than trusted, because a silent violation is a duplicate or an unwanted send.
    """
    query(f"CALL {C.SP_ASSIGNMENT}()")
    dupes = scalar(f"""
        SELECT COUNT(*) - COUNT(DISTINCT CONCAT(CAST(week_start AS STRING), '|', master_key))
        FROM {C.T_ASSIGNMENT}
    """)
    if dupes:
        raise RuntimeError(f"assignment invariant: {dupes} duplicate person-week rows")
    leaked = scalar(f"""
        SELECT COUNTIF(a.email IN (SELECT email FROM {C.T_SUPPRESSION}))
        FROM {C.T_ASSIGNMENT} a
    """)
    if leaked:
        raise RuntimeError(f"assignment invariant: {leaked} suppressed addresses in the assignment")
    return int(scalar(f"SELECT COUNT(*) FROM {C.T_ASSIGNMENT}"))


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #
# The verdict vocabulary and its ORDER are copied deliberately from
# mkt_control.track_send_readiness. Two surfaces that answer "may this go out" must not
# invent two vocabularies: on 2026-09-04 the readiness view said TEMPLATE_INACTIVE_IN_BREVO
# about the same 598 people this plan called SEND, and nothing in either surface made the
# contradiction visible. Comparing decision against that view per email_type is now the
# acceptance test for any change in here.
PLAN_SQL = f"""
WITH assignment AS (
  SELECT master_key, email, track, email_type, template_id, week_start
  FROM {C.T_ASSIGNMENT}
  WHERE week_start = DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY))
),
suppression AS (SELECT DISTINCT email FROM {C.T_SUPPRESSION}),
-- Raivis' per-track switch. Read it or his decisions are decoration.
tracks AS (SELECT track, LOGICAL_AND(enabled) AS track_enabled FROM {C.T_TRACK_ENABLED} GROUP BY track),
-- The ONE mapping. Grouped by key so a duplicate row cannot silently multiply the plan;
-- LOGICAL_AND is the conservative direction - any row saying not-sendable wins.
tmap AS (SELECT email_type, LOGICAL_AND(sendable) AS map_sendable FROM {C.T_TEMPLATE_MAP} GROUP BY email_type),
-- Brevo's own answer, mirrored by blk-brevo-contacts-snapshot. MAX(age) = the oldest check
-- decides staleness, again the conservative direction.
tstatus AS (
  SELECT template_id,
         LOGICAL_AND(is_active) AS brevo_active,
         MAX(TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), checked_at, HOUR)) AS brevo_status_age_h
  FROM {C.T_TEMPLATE_STATUS} GROUP BY template_id
),
-- Frequency is a PERSON property (identity rule b), so history is counted per master.
-- KNOWN LIMIT, measured 2026-09-04: email_send_log holds 22 585 rows, 14 560 of them 'sent',
-- and NOT ONE carries a master_key. This CTE therefore returns nothing for everybody, and
-- FREQUENCY_* can never fire. G5 is vacuous until the sender's own log rows exist - it is
-- proven by the first real send, not by any dry run.
history AS (
  SELECT l.master_key,
         COUNTIF(DATE(l.sent_at) >= DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY))) AS sent_this_week,
         MAX(DATE(l.sent_at)) AS last_sent_date
  FROM {C.T_SEND_LOG} l
  WHERE l.master_key IS NOT NULL AND l.send_status = 'sent'
  GROUP BY 1
),
joined AS (
  SELECT a.*,
         c.full_name, c.gender_greeting, c.language, c.segment, c.lifecycle_stage,
         c.hero_product_name, c.hero_product_url, c.hero_product_image, c.hero_product_price,
         c.next_discount_pct, c.next_discount_code,
         COALESCE(h.sent_this_week, 0) AS sent_this_week,
         h.last_sent_date,
         tr.track_enabled,
         tm.map_sendable,
         ts.brevo_active,
         ts.brevo_status_age_h,
         (ts.template_id IS NOT NULL) AS has_status_row
  FROM assignment a
  LEFT JOIN {C.T_LIFECYCLE} c ON c.master_key = a.master_key AND LOWER(TRIM(c.email)) = a.email
  LEFT JOIN history h ON h.master_key = a.master_key
  LEFT JOIN tracks tr ON tr.track = a.track
  LEFT JOIN tmap tm ON tm.email_type = a.email_type
  LEFT JOIN tstatus ts ON ts.template_id = a.template_id
)
SELECT *,
  CASE
    WHEN email IN (SELECT email FROM suppression)                       THEN 'SUPPRESSED'
    WHEN sent_this_week >= @max_per_week                                THEN 'FREQUENCY_WEEK'
    WHEN last_sent_date IS NOT NULL
     AND DATE_DIFF(CURRENT_DATE(), last_sent_date, DAY) < @min_days     THEN 'FREQUENCY_GAP'
    WHEN track_enabled IS NOT TRUE                                      THEN 'TRACK_OFF'
    WHEN template_id IS NULL                                            THEN 'NO_TEMPLATE'
    WHEN map_sendable IS NOT TRUE                                       THEN 'TEMPLATE_NOT_SENDABLE'
    WHEN NOT has_status_row                                             THEN 'TEMPLATE_STATUS_UNKNOWN'
    WHEN brevo_status_age_h IS NULL
      OR brevo_status_age_h > @status_max_age_h                         THEN 'TEMPLATE_STATUS_STALE'
    WHEN brevo_active IS NOT TRUE                                       THEN 'TEMPLATE_INACTIVE_IN_BREVO'
    WHEN full_name IS NULL                                              THEN 'NOT_IN_LIFECYCLE'
    ELSE 'SEND'
  END AS decision,
  -- Same ladder with the track switch removed, so that switching a track on never reveals a
  -- template problem for the first time. Diagnostic only: nothing sends on this column.
  CASE
    WHEN email IN (SELECT email FROM suppression)                       THEN 'SUPPRESSED'
    WHEN sent_this_week >= @max_per_week                                THEN 'FREQUENCY_WEEK'
    WHEN last_sent_date IS NOT NULL
     AND DATE_DIFF(CURRENT_DATE(), last_sent_date, DAY) < @min_days     THEN 'FREQUENCY_GAP'
    WHEN template_id IS NULL                                            THEN 'NO_TEMPLATE'
    WHEN map_sendable IS NOT TRUE                                       THEN 'TEMPLATE_NOT_SENDABLE'
    WHEN NOT has_status_row                                             THEN 'TEMPLATE_STATUS_UNKNOWN'
    WHEN brevo_status_age_h IS NULL
      OR brevo_status_age_h > @status_max_age_h                         THEN 'TEMPLATE_STATUS_STALE'
    WHEN brevo_active IS NOT TRUE                                       THEN 'TEMPLATE_INACTIVE_IN_BREVO'
    WHEN full_name IS NULL                                              THEN 'NOT_IN_LIFECYCLE'
    ELSE 'READY'
  END AS decision_if_enabled
FROM joined
"""


def build_plan():
    from google.cloud.bigquery import ScalarQueryParameter as P
    return query(PLAN_SQL, [
        P("max_per_week", "INT64", C.MAX_EMAILS_PER_WEEK),
        P("min_days", "INT64", C.MIN_DAYS_BETWEEN),
        P("status_max_age_h", "FLOAT64", C.TEMPLATE_STATUS_MAX_AGE_H),
    ])


# --------------------------------------------------------------------------- #
# Send-time re-check: never trust a plan row that was computed minutes ago
# --------------------------------------------------------------------------- #
# Consent was always re-checked here. The template was not - and a template deactivated
# between plan and send is exactly the case this guard exists for, so it is checked against
# the same mirror and the same freshness threshold the plan used.
RECHECK_SQL = f"""
SELECT
  EXISTS(SELECT 1 FROM {C.T_SUPPRESSION} s WHERE s.email = @email) AS addr_suppressed,
  EXISTS(
    SELECT 1
    FROM {C.T_MASTER} m
    CROSS JOIN UNNEST(m.deliverable_emails) AS x
    JOIN {C.T_SUPPRESSION} s ON s.email = LOWER(TRIM(x))
    WHERE m.master_key = @master_key
  ) AS master_suppressed,
  EXISTS(
    SELECT 1 FROM {C.T_SNAPSHOT} b WHERE b.email = @email AND b.email_blocklisted
  ) AS blocklisted,
  (SELECT LOGICAL_AND(s.is_active) FROM {C.T_TEMPLATE_STATUS} s
    WHERE s.template_id = @template_id) AS tpl_active,
  (SELECT MAX(TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), s.checked_at, HOUR))
     FROM {C.T_TEMPLATE_STATUS} s WHERE s.template_id = @template_id) AS tpl_status_age_h
"""


def recheck(email: str, master_key: str, template_id):
    """Return a REASON string when this message must not go out, else None.

    A reason rather than a bool: 'skipped' with two different causes in one counter is a
    number nobody can act on.
    """
    from google.cloud.bigquery import ScalarQueryParameter as P
    row = query(RECHECK_SQL, [
        P("email", "STRING", email),
        P("master_key", "STRING", master_key),
        P("template_id", "INT64", template_id),
    ])[0]
    if row["addr_suppressed"] or row["master_suppressed"] or row["blocklisted"]:
        return "SUPPRESSED_AT_SEND_TIME"
    if row["tpl_active"] is None:
        return "TEMPLATE_STATUS_UNKNOWN"
    if row["tpl_status_age_h"] is None or row["tpl_status_age_h"] > C.TEMPLATE_STATUS_MAX_AGE_H:
        return "TEMPLATE_STATUS_STALE"
    if not row["tpl_active"]:
        return "TEMPLATE_INACTIVE_IN_BREVO"
    return None


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def write_send_plan(run_id: str, rows: list):
    if not rows:
        return 0
    payload = [{
        "run_id": run_id,
        "planned_at": r["planned_at"],
        "week_start": r["week_start"],
        "master_key": r["master_key"],
        "email": r["email"],
        "track": r["track"],
        "email_type": r["email_type"],
        "template_id": r["template_id"],
        "decision": r["decision"],
        "decision_if_enabled": r["decision_if_enabled"],
        "lifecycle_stage": r["lifecycle_stage"],
        "dry_run": r["dry_run"],
    } for r in rows]
    errors = client().insert_rows_json(C.T_SEND_PLAN.strip("`"), payload)
    if errors:
        raise RuntimeError(f"send_plan insert failed: {errors[:3]}")
    return len(payload)


def write_send_log(rows: list):
    """One row per message. Never a batch stamp: sent_at is the moment of that message."""
    if not rows:
        return 0
    errors = client().insert_rows_json(C.T_SEND_LOG.strip("`"), rows)
    if errors:
        raise RuntimeError(f"email_send_log insert failed: {errors[:3]}")
    return len(rows)


def write_run_report(report: dict):
    errors = client().insert_rows_json(C.T_RUN_REPORT.strip("`"), [report])
    if errors:
        raise RuntimeError(f"sender_run_report insert failed: {errors[:3]}")


# --------------------------------------------------------------------------- #
# Coverage / orphan report - the gate for enabling sending
# --------------------------------------------------------------------------- #
COVERAGE_SQL = f"""
-- Two different things are measured here, and they are NOT the same gate.
--
--   * assignment_*  = the thing we actually send. Marketing was right on 2026-09-02: a gate
--     that measures the pre-assignment path is decoration. duplicate_sends over
--     customer_lifecycle counts people who hold two mailable addresses, which the assignment
--     then resolves to one row. It is a useful health number; it is not a send-blocker.
--   * list3 / orphans_mailable = coverage of the weekly akcija LIST, which this job does not
--     send. Reported always; blocks only if GATE_ON_ORPHANS is on (see main.step5_gate).
--
-- 2026-09-04: sendable_people read 6 144 while assignment_people read 6 143, and the whole of
-- that gap was ONE person with lifecycle_stage='blocked'. sql/assignment.sql excludes blocked
-- in _assign_cl; this query did not. The definition is aligned below rather than the number
-- patched. Note it is aligned for SENDABLE only: cl stays unfiltered for the list3/orphan
-- numbers, because a blocked person IS known to the engine and must not become an "orphan".
WITH l3 AS (SELECT email FROM {C.T_SNAPSHOT} WHERE 3 IN UNNEST(list_ids)),
cl AS (SELECT DISTINCT LOWER(TRIM(email)) AS email, master_key, lifecycle_stage FROM {C.T_LIFECYCLE}),
sup AS (SELECT DISTINCT email FROM {C.T_SUPPRESSION}),
sendable AS (
  SELECT email, master_key FROM cl
  WHERE lifecycle_stage != 'blocked' AND email NOT IN (SELECT email FROM sup)
),
asg AS (
  SELECT master_key, email, week_start FROM {C.T_ASSIGNMENT}
  WHERE week_start = DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY))
)
SELECT
  (SELECT COUNT(*) FROM l3) AS list3_total,
  (SELECT COUNTIF(email IN (SELECT email FROM cl)) FROM l3) AS list3_in_engine,
  (SELECT COUNTIF(email NOT IN (SELECT email FROM cl)
              AND email NOT IN (SELECT email FROM sup)) FROM l3) AS orphans_mailable,
  (SELECT COUNT(*) FROM sendable) AS sendable_rows,
  (SELECT COUNT(DISTINCT master_key) FROM sendable) AS sendable_people,
  (SELECT COUNT(*) - COUNT(DISTINCT master_key) FROM sendable) AS multi_address_people,
  (SELECT COUNT(*) FROM asg) AS assignment_people,
  (SELECT COUNT(*) - COUNT(DISTINCT master_key) FROM asg) AS duplicate_sends,
  (SELECT COUNTIF(email IN (SELECT email FROM sup)) FROM asg) AS assignment_suppressed
"""


def coverage():
    r = query(COVERAGE_SQL)[0]
    return {k: (int(v) if v is not None else None) for k, v in r.items()}
