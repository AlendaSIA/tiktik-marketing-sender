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

    The grain invariant is READ FROM ITS VIEW rather than restated here. Until 2026-09-08
    this function held its own copy of the duplicate condition, which meant the definition
    that decided the run was not the definition anyone queried when asking whether the
    assignment was clean - and the two could drift apart without a word. The view also
    catches NULL_MASTER_KEY, which the old inline count could not see at all.
    """
    query(f"CALL {C.SP_ASSIGNMENT}()")

    violations = query(f"""
        SELECT violation, CAST(week_start AS STRING) AS week_start,
               IFNULL(master_key, '<null>') AS master_key,
               rows_for_person_week, IFNULL(email_types, '') AS email_types
        FROM {C.T_GRAIN_GUARD}
        ORDER BY rows_for_person_week DESC
        LIMIT 5
    """)
    if violations:
        total = int(scalar(f"SELECT COUNT(*) FROM {C.T_GRAIN_GUARD}"))
        sample = "; ".join(
            f"{v['violation']} {v['week_start']}/{v['master_key']}"
            f" rows={v['rows_for_person_week']} types={v['email_types']}"
            for v in violations)
        raise RuntimeError(
            f"assignment invariant: {total} row(s) in {C.T_GRAIN_GUARD}. Sample: {sample}")

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
# Campaign audience snapshot - the dispatch FACT the serviced marker is derived from
# --------------------------------------------------------------------------- #
def write_audience_snapshot(snapshot_id: str, send_date: str, rows: list,
                            written_by: str = "tiktik-marketing-sender"):
    """Freeze who this campaign is for, BEFORE dispatch, at dispatch_state='planned'.

    The whole point of the table: the serviced marker and the ladder steps read the
    dispatch fact recorded here, never list membership. A campaign that fails to send
    therefore marks nobody as served, which is the failure this replaced - a person losing
    their rung to a letter that never arrived.

    Written before, completed after. If the run dies in between, the row stays 'planned'
    and mkt_control.snapshot_stale_planned reports it. It is NOT closed automatically:
    see stale_planned().
    """
    if not rows:
        return 0
    payload = [{
        "snapshot_id": snapshot_id,
        "built_at": r["built_at"],
        "week_start": r["week_start"],
        "send_date": send_date,
        "utm_campaign": r.get("utm_campaign"),
        "email_type": r["email_type"],
        "track": r["track"],
        "brevo_list_id": r.get("brevo_list_id"),
        "brevo_campaign_id": r.get("brevo_campaign_id"),
        "brevo_message_id": r.get("brevo_message_id"),
        "master_key": r["master_key"],
        "email": r["email"],
        "template_id": r.get("template_id"),
        "dispatch_state": r.get("dispatch_state", "planned"),
        "dispatched_at": r.get("dispatched_at"),
        "dispatch_error": r.get("dispatch_error"),
        "written_by": written_by,
    } for r in rows]
    errors = client().insert_rows_json(C.T_AUDIENCE_SNAPSHOT.strip("`"), payload)
    if errors:
        raise RuntimeError(f"campaign_audience_snapshot insert failed: {errors[:3]}")
    return len(payload)


PLANNED_SNAPSHOT_SQL = f"""
-- Re-derive THIS WEEK's planned audience, in one transaction, so a reader never sees the
-- table half-emptied. The job runs every night against the same week, so an append-only
-- write would multiply a person by seven before the week is out; DELETE-then-INSERT is what
-- makes the nightly run idempotent.
--
-- The DELETE is narrow on purpose. It removes only rows that are still 'planned', carry no
-- Brevo id in either direction, and were written by THIS writer. A row a campaign has already
-- claimed or dispatched is therefore untouchable here, and the dispatch fact - which is the
-- whole reason the table exists - can never be deleted by a planning run.
--
-- FOR WHOEVER BUILDS THE CAMPAIGN LAYER (plan Phase 4): claim your rows by stamping
-- brevo_campaign_id (or moving them off 'planned') in the same statement that hands them to a
-- campaign. Between reading a planned row and stamping it, this DELETE may remove it.
BEGIN TRANSACTION;

DELETE FROM {C.T_AUDIENCE_SNAPSHOT}
WHERE week_start = DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY))
  AND dispatch_state = 'planned'
  AND brevo_campaign_id IS NULL
  AND brevo_message_id IS NULL
  AND written_by = @written_by;

INSERT INTO {C.T_AUDIENCE_SNAPSHOT}
  (snapshot_id, built_at, week_start, send_date, utm_campaign, email_type, track,
   brevo_list_id, brevo_campaign_id, brevo_message_id, master_key, email, template_id,
   chosen_because, dispatch_state, dispatched_at, dispatch_error, written_by)
SELECT
  @snapshot_id, CURRENT_TIMESTAMP(), a.week_start, a.send_date,
  p.utm_campaign, a.email_type, a.track,
  NULL, NULL, NULL, a.master_key, a.email, a.template_id,
  a.chosen_because, 'planned', NULL, NULL, @written_by
FROM {C.T_ASSIGNMENT} a
-- The two arrays are ONE list split in two, paired by position: WITH OFFSET is what makes
-- that pairing explicit rather than hoped for. A struct array would read better and is the
-- one place this driver's parameter support is worth not relying on.
JOIN (
  SELECT mk AS master_key, uc AS utm_campaign
  FROM UNNEST(@master_keys) AS mk WITH OFFSET o
  JOIN UNNEST(@utm_campaigns) AS uc WITH OFFSET o2 ON o = o2
) p ON p.master_key = a.master_key
WHERE a.week_start = DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY));

COMMIT TRANSACTION;
"""


def write_planned_snapshot(snapshot_id: str, master_keys: list, utm_campaigns: list,
                           written_by: str = "tiktik-marketing-sender") -> int:
    """Freeze who is slated for which letter on which day, at dispatch_state='planned'.

    WHICH ROWS. Exactly the people the plan decided SEND for. Not the whole assignment: a
    TRACK_OFF row is not going out, and recording it as 'planned' would leave a row that
    nothing can ever dispatch or reconcile. mkt_control.snapshot_stale_planned would then
    report every row in the table forever, and a signal that is always on is no signal - the
    same failure this node has already met on `status: partial`.

    WHY IT RUNS IN A DRY RUN TOO. DRY_RUN governs SENDING, not planning. The day-ahead
    approval e-mail (PART E) reads exactly these rows for tomorrow's send_date and prints
    chosen_because as the criteria, so if the planning write waited for a live send there
    would be nothing to approve and therefore no path to a live send.

    WHERE THE COLUMNS COME FROM, and it is two sources on purpose. The DECISION comes from
    the plan (one definition of who may go, in PLAN_SQL); the DAY and the CRITERIA come from
    contact_weekly_assignment, which is their single writer. Re-deriving either here would
    create the second definition this node keeps removing.

    Returns the number of rows actually in the table for this snapshot_id - read back, not
    assumed. A master_key in the plan with no assignment row would otherwise vanish silently.
    """
    if len(master_keys) != len(utm_campaigns):
        raise RuntimeError(
            f"master_keys ({len(master_keys)}) and utm_campaigns ({len(utm_campaigns)}) are "
            f"one list split in two and must stay the same length; they are paired by "
            f"position in the SQL below.")
    if not master_keys:
        return 0
    from google.cloud.bigquery import ArrayQueryParameter as A
    from google.cloud.bigquery import ScalarQueryParameter as P
    query(PLANNED_SNAPSHOT_SQL, [
        P("snapshot_id", "STRING", snapshot_id),
        P("written_by", "STRING", written_by),
        A("master_keys", "STRING", master_keys),
        A("utm_campaigns", "STRING", utm_campaigns),
    ])
    written = int(scalar(
        f"SELECT COUNT(*) FROM {C.T_AUDIENCE_SNAPSHOT} WHERE snapshot_id = @snapshot_id",
        [P("snapshot_id", "STRING", snapshot_id)]) or 0)
    if written != len(master_keys):
        raise RuntimeError(
            f"snapshot write disagrees with the plan: {len(master_keys)} people decided SEND, "
            f"{written} rows written for snapshot_id={snapshot_id}. A plan row with no "
            f"assignment row for this week is a contradiction between two objects that are "
            f"built from each other, not a rounding difference.")
    return written


UTM_DICTIONARY_SQL = f"""
-- Per-slug DELETE then INSERT, and the DELETE is NULL-safe on utm_content because the
-- whole-campaign row IS the row where utm_content is NULL. The declared primary key on
-- (utm_campaign, utm_content) is informational in BigQuery and enforces nothing, so a
-- retried run or a rebuilt slug would otherwise duplicate silently - and a carelessly
-- written NOT EXISTS never matches a NULL, which is how the duplicate would survive review.
--
-- Scoped to utm_content IS NULL on purpose. The per-link rows ('hero', 'pap-1', ...) belong
-- to the campaign layer, which knows the template; deleting them here would make this a
-- second writer of somebody else's rows rather than the single writer of its own.
BEGIN TRANSACTION;

DELETE FROM {C.T_UTM_DICTIONARY}
WHERE utm_content IS NULL
  AND utm_campaign IN UNNEST(@slugs);

INSERT INTO {C.T_UTM_DICTIONARY}
  (utm_campaign, utm_content, channel, internal_label, internal_campaign, added_at, added_by, notes)
SELECT s, NULL, 'email', l, NULL, CURRENT_TIMESTAMP(), @added_by,
       'Whole-campaign decode row, emitted at planning time. internal_campaign stays NULL '
       'until a Brevo campaign exists; the per-link rows are written by the campaign layer.'
FROM UNNEST(@slugs) AS s WITH OFFSET o
JOIN UNNEST(@labels) AS l WITH OFFSET o2 ON o = o2;

COMMIT TRANSACTION;
"""


def write_utm_dictionary(slug_label_pairs: list,
                         added_by: str = "tiktik-marketing-sender") -> int:
    """Write one decode row per emitted slug: utm_campaign -> which variant it really is.

    A slug with no dictionary row is a slug nobody can read afterwards, and the pre-send check
    treats a missing row as a blocker rather than a warning. Writing it at PLANNING time means
    the blocker is satisfied by construction instead of remembered.

    internal_label is the email_type and never the track: from the track, winback_1, winback_2
    and winback_3 collapse into one label and the rungs become unreadable in exactly the
    report that was supposed to tell them apart.

    added_by names THIS writer, per the single-writer rule. The contract's literal
    'campaign-layer' was written when one component was expected to own the whole table; the
    split is now by KEY - whole-campaign row here, per-link rows there - so two names are
    correct and one row still has one writer.
    """
    if not slug_label_pairs:
        return 0
    from google.cloud.bigquery import ArrayQueryParameter as A
    from google.cloud.bigquery import ScalarQueryParameter as P
    slugs = [s for s, _ in slug_label_pairs]
    labels = [l for _, l in slug_label_pairs]
    query(UTM_DICTIONARY_SQL, [
        P("added_by", "STRING", added_by),
        A("slugs", "STRING", slugs),
        A("labels", "STRING", labels),
    ])
    written = int(scalar(
        f"SELECT COUNT(*) FROM {C.T_UTM_DICTIONARY} "
        f"WHERE utm_content IS NULL AND utm_campaign IN UNNEST(@slugs)",
        [A("slugs", "STRING", slugs)]) or 0)
    if written != len(slugs):
        raise RuntimeError(
            f"utm_dictionary write disagrees with itself: {len(slugs)} slugs emitted, "
            f"{written} whole-campaign rows present afterwards.")
    return written


def default_day_rows() -> int:
    """How many of this week's assignment rows got their day from the PLACEHOLDER, not the map.

    Reported on every run next to rows written and grain violations. chosen_because makes the
    reason readable per row, but a trace nobody queries is not a signal; this is the number
    that surfaces without anyone writing SQL. Non-zero means a variant is being sent without
    Marketing having chosen its day - since 2026-09-09 that lands on Thursday rather than the
    commercial day, which makes it survivable, not correct.
    """
    n = scalar(f"""
        SELECT COUNTIF(chosen_because LIKE '%send_day=default_first_sending_day%')
        FROM {C.T_ASSIGNMENT}
        WHERE week_start = DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY))
    """)
    return int(n or 0)


def stale_planned() -> int:
    """How many snapshot rows are still 'planned' after their send_date.

    Reported on every run and never acted on here. Closing such a row on age alone would
    mark a late-dispatched person unserved, and they would receive the same letter again
    next week - the serviced marker's own failure arriving from the other side. Closing
    requires reconciling against Brevo first (see the view's reconcile_route), which
    belongs to whatever dispatches campaigns, not to a counter.
    """
    n = scalar(f"SELECT SUM(planned_rows) FROM {C.T_STALE_PLANNED}")
    return int(n or 0)


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
