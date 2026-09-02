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
PLAN_SQL = f"""
WITH assignment AS (
  SELECT master_key, email, track, email_type, template_id, week_start
  FROM {C.T_ASSIGNMENT}
  WHERE week_start = DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY))
),
suppression AS (SELECT DISTINCT email FROM {C.T_SUPPRESSION}),
-- Frequency is a PERSON property (identity rule b), so history is counted per master.
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
         h.last_sent_date
  FROM assignment a
  LEFT JOIN {C.T_LIFECYCLE} c ON c.master_key = a.master_key AND LOWER(TRIM(c.email)) = a.email
  LEFT JOIN history h ON h.master_key = a.master_key
)
SELECT *,
  CASE
    WHEN email IN (SELECT email FROM suppression)                       THEN 'SUPPRESSED'
    WHEN sent_this_week >= @max_per_week                                THEN 'FREQUENCY_WEEK'
    WHEN last_sent_date IS NOT NULL
     AND DATE_DIFF(CURRENT_DATE(), last_sent_date, DAY) < @min_days     THEN 'FREQUENCY_GAP'
    WHEN template_id IS NULL                                            THEN 'NO_TEMPLATE'
    WHEN full_name IS NULL                                              THEN 'NOT_IN_LIFECYCLE'
    ELSE 'SEND'
  END AS decision
FROM joined
"""


def build_plan():
    from google.cloud.bigquery import ScalarQueryParameter as P
    return query(PLAN_SQL, [
        P("max_per_week", "INT64", C.MAX_EMAILS_PER_WEEK),
        P("min_days", "INT64", C.MIN_DAYS_BETWEEN),
    ])


# --------------------------------------------------------------------------- #
# Send-time re-check: never trust a plan row that was computed minutes ago
# --------------------------------------------------------------------------- #
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
  ) AS blocklisted
"""


def recheck(email: str, master_key: str):
    from google.cloud.bigquery import ScalarQueryParameter as P
    row = query(RECHECK_SQL, [
        P("email", "STRING", email),
        P("master_key", "STRING", master_key),
    ])[0]
    return bool(row["addr_suppressed"] or row["master_suppressed"] or row["blocklisted"])


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
WITH l3 AS (SELECT email FROM {C.T_SNAPSHOT} WHERE 3 IN UNNEST(list_ids)),
cl AS (SELECT DISTINCT LOWER(TRIM(email)) AS email, master_key FROM {C.T_LIFECYCLE}),
sup AS (SELECT DISTINCT email FROM {C.T_SUPPRESSION}),
sendable AS (SELECT * FROM cl WHERE email NOT IN (SELECT email FROM sup))
SELECT
  (SELECT COUNT(*) FROM l3) AS list3_total,
  (SELECT COUNTIF(email IN (SELECT email FROM cl)) FROM l3) AS list3_in_engine,
  (SELECT COUNTIF(email NOT IN (SELECT email FROM cl)
              AND email NOT IN (SELECT email FROM sup)) FROM l3) AS orphans_mailable,
  (SELECT COUNT(*) FROM sendable) AS sendable_rows,
  (SELECT COUNT(DISTINCT master_key) FROM sendable) AS sendable_people,
  (SELECT COUNT(*) - COUNT(DISTINCT master_key) FROM sendable) AS duplicate_sends
"""


def coverage():
    r = query(COVERAGE_SQL)[0]
    return {k: (int(v) if v is not None else None) for k, v in r.items()}
