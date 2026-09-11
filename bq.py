"""BigQuery access. Every statement the sender runs lives here, so the data contract is
readable in one file.
"""
import datetime as dt
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

    # LAYER INVARIANT (2026-09-11). Every other read of this table filters
    # layer IN ('commercial','educational') explicitly, so a row with any other layer - NULL, a
    # typo, a third layer nobody decided - would be dropped by all of them without a word. This
    # is the one read that must NOT filter: it counts exactly those rows and fails the run on
    # them. A third layer also breaks the two-letters-a-week guarantee, which is Raivis' call.
    unknown = int(scalar(ASSIGNMENT_UNKNOWN_LAYER_SQL) or 0)
    if unknown:
        raise RuntimeError(
            f"assignment invariant: {unknown} row(s) whose layer is not one of {C.LAYERS}. "
            f"Every reader filters those two layers explicitly and would silently skip them.")

    leaked = scalar(ASSIGNMENT_LEAKED_SQL)
    if leaked:
        raise RuntimeError(f"assignment invariant: {leaked} suppressed addresses in the assignment")
    # ROWS, not people. Since the layer grain one person can hold two rows, so this number is
    # reported as rows_written and never as a head count - people are COUNT(DISTINCT master_key).
    return int(scalar(ASSIGNMENT_ROWS_SQL))


ASSIGNMENT_UNKNOWN_LAYER_SQL = f"""
    SELECT COUNTIF(layer IS NULL OR layer NOT IN {C.ALL_LAYERS_SQL})
    FROM {C.T_ASSIGNMENT}
"""

ASSIGNMENT_LEAKED_SQL = f"""
    SELECT COUNTIF(a.email IN (SELECT email FROM {C.T_SUPPRESSION}))
    FROM {C.T_ASSIGNMENT} a
    WHERE a.layer IN {C.ALL_LAYERS_SQL}
"""

ASSIGNMENT_ROWS_SQL = f"""
    SELECT COUNT(*) FROM {C.T_ASSIGNMENT}
    WHERE layer IN {C.ALL_LAYERS_SQL}
"""


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
-- BOTH layers, named explicitly, and the layer travels with every row (2026-09-11). This plan is
-- the writer of the planned snapshot for every sending day - Tuesday commercial and Thursday
-- educational alike - so reading only one layer would silently leave the other day with no
-- batch. One person may therefore appear twice, once per layer; the ONE_PER_PERSON guard in
-- main.step4_plan is per (master_key, layer) for exactly that reason.
WITH assignment AS (
  SELECT master_key, email, layer, track, email_type, template_id, week_start, send_date
  FROM {C.T_ASSIGNMENT}
  WHERE week_start = DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY))
    AND layer IN {C.ALL_LAYERS_SQL}
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
         -- next_discount_pct / next_discount_code are NOT read (MAIN, 2026-09-11): discount codes
         -- were cancelled by decision and both columns removed from customer_lifecycle on purpose.
         -- Reading them failed every nightly run from 10.09. No default stands in for them.
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
    # layer is REQUIRED on every new row (MAIN, 2026-09-11: "NULL jaunas rindas nav atlauts").
    # Refused here, before the insert, so a caller that forgets it fails loudly instead of
    # writing a row no layer-filtered reader will ever see.
    bad = [r.get("master_key") for r in rows if r.get("layer") not in C.LAYERS]
    if bad:
        raise RuntimeError(
            f"campaign_audience_snapshot: {len(bad)} row(s) without a valid layer "
            f"(must be one of {C.LAYERS}); first master_key={bad[0]!r}. Nothing written.")
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
        "layer": r["layer"],
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


T_DAY_BATCH = f"`{C.PROJECT}.{C.CONTROL}.day_batch`"

# The days that are FROZEN: any send_date with a day_batch row. `IS NOT NULL` is not decoration - a
# single NULL in a NOT IN list makes the predicate NULL for every row, and the planner would then
# delete and insert nothing at all, silently.
FROZEN_DAYS_SQL = f"SELECT DISTINCT CAST(send_date AS STRING) AS d FROM {T_DAY_BATCH} WHERE send_date IS NOT NULL"


def frozen_send_dates() -> set:
    return {r["d"] for r in query(FROZEN_DAYS_SQL)}


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
  AND written_by = @written_by
  -- THE FROZEN DAY (MAIN, 2026-09-11): a send_date that already has a day_batch is never touched
  -- again. What goes out is what Raivis approved; the nightly rebuild never changes a frozen day.
  AND send_date NOT IN (SELECT send_date FROM {T_DAY_BATCH} WHERE send_date IS NOT NULL);

-- layer is written on every row and comes from the assignment row itself, which the WHERE below
-- restricts to the two named layers - so it can never be NULL here (MAIN, 2026-09-11).
INSERT INTO {C.T_AUDIENCE_SNAPSHOT}
  (snapshot_id, built_at, week_start, send_date, utm_campaign, email_type, track,
   brevo_list_id, brevo_campaign_id, brevo_message_id, master_key, email, template_id,
   chosen_because, dispatch_state, dispatched_at, dispatch_error, written_by, layer)
SELECT
  @snapshot_id, CURRENT_TIMESTAMP(), a.week_start, a.send_date,
  p.utm_campaign, a.email_type, a.track,
  NULL, NULL, NULL, a.master_key, a.email, a.template_id,
  a.chosen_because, 'planned', NULL, NULL, @written_by, a.layer
FROM {C.T_ASSIGNMENT} a
-- The three arrays are ONE list split in three, paired by position: WITH OFFSET is what makes
-- that pairing explicit rather than hoped for. A struct array would read better and is the
-- one place this driver's parameter support is worth not relying on.
-- The join key is (master_key, layer), never master_key alone: since the layer grain one
-- person can hold two assignment rows, and a master_key-only join would pair each plan row
-- with both of them.
JOIN (
  SELECT mk AS master_key, ly AS layer, uc AS utm_campaign
  FROM UNNEST(@master_keys) AS mk WITH OFFSET o
  JOIN UNNEST(@layers) AS ly WITH OFFSET o1 ON o = o1
  JOIN UNNEST(@utm_campaigns) AS uc WITH OFFSET o2 ON o = o2
) p ON p.master_key = a.master_key AND p.layer = a.layer
WHERE a.week_start = DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY))
  AND a.layer IN {C.ALL_LAYERS_SQL}
  AND a.send_date NOT IN (SELECT send_date FROM {T_DAY_BATCH} WHERE send_date IS NOT NULL);

COMMIT TRANSACTION;
"""


def write_planned_snapshot(snapshot_id: str, master_keys: list, utm_campaigns: list,
                           layers: list = None, send_dates: list = None,
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
    if send_dates is None or len(send_dates) != len(master_keys):
        raise RuntimeError(
            "send_dates is required, one per plan row: a frozen day is skipped by send_date, and a "
            "row whose day is unknown cannot be told apart from one that must not be touched.")
    # FROZEN DAYS ARE DROPPED HERE AS WELL AS IN THE SQL, so the read-back count below compares like
    # with like: rows for a frozen send_date are neither deleted nor inserted, by design.
    frozen = frozen_send_dates()
    keep = [i for i, d in enumerate(send_dates) if str(d)[:10] not in frozen]
    skipped = len(master_keys) - len(keep)
    if skipped:
        log.info("FROZEN_DAYS_SKIPPED rows=%s frozen_dates=%s", skipped, sorted(frozen))
    master_keys = [master_keys[i] for i in keep]
    utm_campaigns = [utm_campaigns[i] for i in keep]
    layers = [layers[i] for i in keep] if layers is not None else None
    if layers is None or not (len(master_keys) == len(utm_campaigns) == len(layers)):
        raise RuntimeError(
            f"master_keys ({len(master_keys)}), layers ({'missing' if layers is None else len(layers)}) "
            f"and utm_campaigns ({len(utm_campaigns)}) are one list split in three and must stay "
            f"the same length; they are paired by position in the SQL below. layers is "
            f"required: a snapshot row without its layer is not allowed (MAIN, 2026-09-11).")
    if not master_keys:
        return 0
    bad = [ly for ly in layers if ly not in C.LAYERS]
    if bad:
        raise RuntimeError(
            f"write_planned_snapshot: {len(bad)} layer value(s) outside {C.LAYERS}, first "
            f"{bad[0]!r}. Nothing written.")
    from google.cloud.bigquery import ArrayQueryParameter as A
    from google.cloud.bigquery import ScalarQueryParameter as P
    query(PLANNED_SNAPSHOT_SQL, [
        P("snapshot_id", "STRING", snapshot_id),
        P("written_by", "STRING", written_by),
        A("master_keys", "STRING", master_keys),
        A("layers", "STRING", layers),
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


# UTM SEAM v1 (MAIN, 2026-09-11): "mkt_control.utm_dictionary rindas raksta TIKAI kampaņu slānis, ar
# MERGE, vienu rindu katram (utm_campaign, utm_content) pārim. Veidņu būvētājs tur neraksta."
#
# ONE statement for both writers in this repo - the planner's whole-campaign rows (utm_content NULL)
# and the draft's per-link rows - so there is one form of write, not two. NULL-safe on utm_content
# (the whole-campaign row IS the NULL one; a plain `=` never matches NULL, which is how a duplicate
# survives review). Array parameters cannot carry NULL, so NULL travels as '' and is turned back
# with NULLIF. WHEN NOT MATCHED only: an existing pair is never rewritten, never deleted - the
# declared primary key is informational in BigQuery, so the MERGE is what keeps it one row.
UTM_MERGE_SQL = f"""
MERGE {C.T_UTM_DICTIONARY} t
USING (
  SELECT DISTINCT c AS utm_campaign, NULLIF(ct, '') AS utm_content, l AS internal_label
  FROM UNNEST(@campaigns) AS c WITH OFFSET o
  JOIN UNNEST(@contents) AS ct WITH OFFSET o1 ON o = o1
  JOIN UNNEST(@labels) AS l WITH OFFSET o2 ON o = o2
) s
ON t.utm_campaign = s.utm_campaign AND IFNULL(t.utm_content, '') = IFNULL(s.utm_content, '')
WHEN NOT MATCHED THEN
  INSERT (utm_campaign, utm_content, channel, internal_label, internal_campaign, added_at, added_by,
          notes)
  VALUES (s.utm_campaign, s.utm_content, 'email', s.internal_label, NULL, CURRENT_TIMESTAMP(),
          @added_by, @notes)
"""


def merge_utm_rows(rows: list, added_by: str, notes: str) -> int:
    """MERGE (utm_campaign, utm_content|None, internal_label) rows; return rows now present.

    Refuses a pair that arrives with two different labels - the source would then hold two rows for
    one key, and on NOT MATCHED both would be inserted. "Present afterwards" is READ back, not
    assumed, and must equal the number of distinct pairs.
    """
    if not rows:
        return 0
    labels = {}
    for c, ct, l in rows:
        key = (c, ct or None)
        if labels.setdefault(key, l) != l:
            raise RuntimeError(f"utm pair {key} arrives with two labels: {labels[key]!r} and {l!r}")
    from google.cloud.bigquery import ArrayQueryParameter as A
    from google.cloud.bigquery import ScalarQueryParameter as P
    keys = sorted(labels, key=lambda k: (k[0], k[1] or ""))
    query(UTM_MERGE_SQL, [
        A("campaigns", "STRING", [k[0] for k in keys]),
        A("contents", "STRING", [k[1] or "" for k in keys]),
        A("labels", "STRING", [labels[k] for k in keys]),
        P("added_by", "STRING", added_by),
        P("notes", "STRING", notes),
    ])
    present = int(scalar(
        f"SELECT COUNT(*) FROM {C.T_UTM_DICTIONARY} t, UNNEST(@campaigns) AS c WITH OFFSET o "
        f"JOIN UNNEST(@contents) AS ct WITH OFFSET o1 ON o = o1 "
        f"WHERE t.utm_campaign = c AND IFNULL(t.utm_content, '') = ct",
        [A("campaigns", "STRING", [k[0] for k in keys]),
         A("contents", "STRING", [k[1] or "" for k in keys])]) or 0)
    if present != len(keys):
        raise RuntimeError(
            f"utm_dictionary disagrees with itself after MERGE: {len(keys)} distinct pairs, "
            f"{present} rows present for them. More means a duplicate already existed; fewer means "
            f"the write did not land.")
    return present


def write_utm_dictionary(slug_label_pairs: list,
                         added_by: str = "tiktik-marketing-sender") -> int:
    """The planner's whole-campaign decode row per emitted slug (utm_content NULL), via the MERGE.

    internal_label is the email_type and never the track: from the track the winback rungs
    collapse into one label. Per-link rows come from the draft (campaign.create_draft pairs).
    """
    return merge_utm_rows(
        [(s, None, l) for s, l in slug_label_pairs], added_by=added_by,
        notes="Whole-campaign decode row, emitted at planning time. Per-link rows are written by "
              "the campaign layer when the draft is built.")


def assignment_week_hash() -> str:
    """A content hash of the WHOLE WEEK's assignment table - a REBUILD TRACE, nothing else.

    Since 2026-09-11 (MAIN) this is NOT the build_id of a day any more. It is written only to
    mkt_control.assignment_build_log (and sender_run_report.assignment_build_id, a column whose
    name predates the change) so a rebuild leaves a trace. The day identity that the batch, the
    relay and the press carry is day_build_id(send_date) below.
    """
    return str(scalar(ASSIGNMENT_WEEK_HASH_SQL) or "")


def day_build_id(send_date) -> str:
    """THE build_id: the identity of ONE send_date's planned audience (MAIN, 2026-09-11).

    MD5 over `master_key|email_type|send_date` of that day's planned rows in
    campaign_audience_snapshot, in a total order. Frozen into day_batch when the batch is built;
    the press compares it with the same computation now (AUDIENCE_CHANGED). Because the planner
    never touches a frozen day, the two differ only if something ELSE wrote that day - which is
    exactly when the press should refuse. An empty day hashes the empty string (a fixed value),
    never NULL: the relay requires the field.
    """
    from google.cloud.bigquery import ScalarQueryParameter as P
    return str(scalar(DAY_BUILD_ID_SQL, [P("d", "DATE", str(send_date)[:10])]))


DAY_BUILD_ID_SQL = f"""
        SELECT TO_HEX(MD5(IFNULL(STRING_AGG(
                 FORMAT('%s|%s|%t', master_key, email_type, send_date)
                 ORDER BY master_key, email_type), '')))
        FROM {C.T_AUDIENCE_SNAPSHOT}
        WHERE send_date = @d AND dispatch_state = 'planned'
"""


# (Since the frozen-day decision of 2026-09-11 this hash is a rebuild TRACE only - see
# assignment_week_hash(). The notes below explain its order and remain true.)
# BOTH layers, named, and a TOTAL order (2026-09-11). Until this change the aggregate was ordered
# by master_key alone. With one row per person that was a total order; since the layer grain
# (2026-09-10) a person holds up to two rows, the two tie on master_key, and BigQuery does not
# promise which comes first - so the hash of an unchanged table was not guaranteed to be stable,
# and the press compares exactly this value (AUDIENCE_CHANGED). The tie-break is layer DESC:
# measured live on 2026-09-11 against the 04:31 UTC build, the old expression returned
# 53a763ec0a8fa09400507f936c16bb4f five times out of five, and ORDER BY master_key, layer DESC is
# the order that reproduces it, so no build_id already stored in assignment_build_log, day_batch
# or press_verdict changes its meaning. The row text itself is unchanged: layer is not added to
# FORMAT because email_type already differs between the two layers (0 shared values measured).
ASSIGNMENT_WEEK_HASH_SQL = f"""
        SELECT TO_HEX(MD5(STRING_AGG(
                 FORMAT('%s|%s|%t', master_key, email_type, send_date)
                 ORDER BY master_key, layer DESC)))
        FROM {C.T_ASSIGNMENT}
        WHERE week_start = DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY))
          AND layer IN {C.ALL_LAYERS_SQL}
"""


# People, not rows: one person can hold a commercial AND an educational row.
ASSIGNMENT_PEOPLE_SQL = f"""
        SELECT COUNT(DISTINCT master_key) FROM {C.T_ASSIGNMENT}
        WHERE week_start = DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY))
          AND layer IN {C.ALL_LAYERS_SQL}"""


def log_assignment_build(run_id: str, build_id: str, rows: int) -> bool:
    """Append the rebuild to the build log and answer whether it changed anything.

    contact_weekly_assignment is CREATE OR REPLACEd every run and built_at is overwritten with
    it, so without this row a rebuild leaves no trace at all after the next one. Data &
    analytics found the table rebuilt twice on 2026-09-09 - the scheduled 07:31 run and a
    12:40 dry run - and neither is visible in the table today.
    """
    previous = scalar(
        f"SELECT build_id FROM {C.T_BUILD_LOG} ORDER BY built_at DESC LIMIT 1")
    same = (previous == build_id)
    people = int(scalar(ASSIGNMENT_PEOPLE_SQL) or 0)
    errors = client().insert_rows_json(C.T_BUILD_LOG.strip("`"), [{
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "run_id": run_id, "build_id": build_id,
        "rows_written": rows, "distinct_people": people,
        "same_as_previous": same,
    }])
    if errors:
        raise RuntimeError(f"assignment_build_log insert failed: {errors[:3]}")
    return same


COMPLETE_DISPATCH_SQL = f"""
-- The dispatch FACT, written in one transaction with the per-letter log rows derived from it.
-- One transaction because the two must never disagree: a snapshot row that says 'sent' with no
-- log row behind it advances nobody's rung and cannot be measured, and a log row with no
-- snapshot row is a letter nobody can explain.
--
-- The log rows are what make the ladder move. customer_lifecycle derives welcome_step /
-- reorder_step / winback_step from email_send_log; these rows are therefore the ONLY mechanism
-- by which anyone ever climbs a rung, and they carry master_key, which no legacy row does.
BEGIN TRANSACTION;

UPDATE {C.T_AUDIENCE_SNAPSHOT} s
SET dispatch_state = r.state,
    dispatched_at = CURRENT_TIMESTAMP(),
    dispatch_error = r.err,
    brevo_campaign_id = @brevo_campaign_id
FROM (
  SELECT mk AS master_key, st AS state, er AS err
  FROM UNNEST(@master_keys) AS mk WITH OFFSET o
  JOIN UNNEST(@states) AS st WITH OFFSET o2 ON o = o2
  JOIN UNNEST(@errors) AS er WITH OFFSET o3 ON o = o3
) r
WHERE s.master_key = r.master_key
  AND s.send_date = @send_date
  AND s.email_type = @email_type
  AND s.dispatch_state = 'planned';

-- Idempotent on a retry: this campaign's own log rows are removed before they are rewritten.
-- Scoped to run_id AND campaign_id so it can never touch the 22 585 legacy rows, every one of
-- which carries a NULL run_id.
DELETE FROM {C.T_SEND_LOG}
WHERE run_id = @run_id AND campaign_id = @brevo_campaign_id;

INSERT INTO {C.T_SEND_LOG}
  (email, campaign_id, campaign_name, utm_campaign, brevo_list_id, send_status, channel,
   sent_at, email_type, language, master_key, run_id, brevo_message_id, assignment_week)
SELECT
  s.email, @brevo_campaign_id, @campaign_name, s.utm_campaign, s.brevo_list_id,
  'sent', 'email',
  -- One campaign is one dispatch, so one timestamp per campaign is the truth here. This is
  -- NOT the June defect, where 7 668 PER-PERSON sends shared a single batch stamp.
  CURRENT_TIMESTAMP(),
  s.email_type, c.language, s.master_key, @run_id, s.brevo_message_id, s.week_start
FROM {C.T_AUDIENCE_SNAPSHOT} s
LEFT JOIN {C.T_LIFECYCLE} c
  ON c.master_key = s.master_key AND LOWER(TRIM(c.email)) = s.email
WHERE s.send_date = @send_date
  AND s.email_type = @email_type
  AND s.brevo_campaign_id = @brevo_campaign_id
  AND s.dispatch_state = 'sent';

COMMIT TRANSACTION;
"""


def complete_dispatch(run_id: str, send_date: str, email_type: str, brevo_campaign_id: int,
                      campaign_name: str, results: list) -> dict:
    """Close a dispatched campaign: planned -> sent/failed, and one log row per letter sent.

    `results` is a list of (master_key, state, error) with state in {'sent','failed'}. The
    caller is whatever actually dispatched; this function never talks to Brevo and never
    decides whether something was sent - it records what it is told and then checks itself.

    The check is the point. After the write it asserts mkt_control.dispatch_log_mismatch is
    empty for this campaign, so "the dispatch was recorded" is a verified statement rather
    than the absence of an exception.
    """
    if not results:
        return {"updated": 0, "logged": 0}
    from google.cloud.bigquery import ArrayQueryParameter as A
    from google.cloud.bigquery import ScalarQueryParameter as P
    params = [
        P("run_id", "STRING", run_id),
        P("send_date", "DATE", send_date),
        P("email_type", "STRING", email_type),
        P("brevo_campaign_id", "INT64", brevo_campaign_id),
        P("campaign_name", "STRING", campaign_name),
        A("master_keys", "STRING", [r[0] for r in results]),
        A("states", "STRING", [r[1] for r in results]),
        A("errors", "STRING", [r[2] for r in results]),
    ]
    query(COMPLETE_DISPATCH_SQL, params)

    updated = int(scalar(
        f"SELECT COUNT(*) FROM {C.T_AUDIENCE_SNAPSHOT} "
        f"WHERE brevo_campaign_id = @brevo_campaign_id AND dispatch_state != 'planned'",
        [P("brevo_campaign_id", "INT64", brevo_campaign_id)]) or 0)
    logged = int(scalar(
        f"SELECT COUNT(*) FROM {C.T_SEND_LOG} "
        f"WHERE run_id = @run_id AND campaign_id = @brevo_campaign_id",
        [P("run_id", "STRING", run_id),
         P("brevo_campaign_id", "INT64", brevo_campaign_id)]) or 0)

    bad = query(
        f"SELECT violation, COUNT(*) AS n FROM {C.T_DISPATCH_MISMATCH} "
        f"WHERE campaign_id = @brevo_campaign_id GROUP BY violation",
        [P("brevo_campaign_id", "INT64", brevo_campaign_id)])
    if bad:
        raise RuntimeError(
            f"dispatch not consistently recorded for campaign {brevo_campaign_id}: "
            + "; ".join(f"{b['violation']}={b['n']}" for b in bad))
    return {"updated": updated, "logged": logged}


def dispatch_log_mismatch() -> int:
    """Rows in the dispatch/log invariant view. Reported every run; empty is the only good state."""
    return int(scalar(f"SELECT COUNT(*) FROM {C.T_DISPATCH_MISMATCH}") or 0)


def day_list_overlap() -> int:
    """People in more than one of a sending day's lists. One is enough to stop the day."""
    return int(scalar(f"SELECT COUNT(*) FROM {C.T_DAY_OVERLAP}") or 0)


def default_day_rows() -> int:
    """How many of this week's assignment rows got their day from the PLACEHOLDER, not the map.

    Reported on every run next to rows written and grain violations. chosen_because makes the
    reason readable per row, but a trace nobody queries is not a signal; this is the number
    that surfaces without anyone writing SQL. Non-zero means a variant is being sent without
    Marketing having chosen its day - since 2026-09-09 that lands on Thursday rather than the
    commercial day, which makes it survivable, not correct.
    """
    n = scalar(DEFAULT_DAY_ROWS_SQL)
    return int(n or 0)


# ROWS on the placeholder, both layers named: a variant with no send-day row is a finding in
# either layer, and one person's two rows are two variants that each lack (or have) a map row.
DEFAULT_DAY_ROWS_SQL = f"""
        SELECT COUNTIF(chosen_because LIKE '%send_day=default_first_sending_day%')
        FROM {C.T_ASSIGNMENT}
        WHERE week_start = DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY))
          AND layer IN {C.ALL_LAYERS_SQL}
"""


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
-- that gap was ONE person with lifecycle_stage='blocked'. The assignment procedure
-- (mkt_control.sp_build_contact_weekly_assignment, live) excludes blocked in _assign_cl; this
-- query did not. The definition is aligned below rather than the number
-- patched. Note it is aligned for SENDABLE only: cl stays unfiltered for the list3/orphan
-- numbers, because a blocked person IS known to the engine and must not become an "orphan".
WITH l3 AS (SELECT email FROM {C.T_SNAPSHOT} WHERE 3 IN UNNEST(list_ids)),
cl AS (SELECT DISTINCT LOWER(TRIM(email)) AS email, master_key, lifecycle_stage FROM {C.T_LIFECYCLE}),
sup AS (SELECT DISTINCT email FROM {C.T_SUPPRESSION}),
sendable AS (
  SELECT email, master_key FROM cl
  WHERE lifecycle_stage != 'blocked' AND email NOT IN (SELECT email FROM sup)
),
-- Both layers, named (2026-09-11). Two numbers changed meaning with the layer grain and are
-- defined here so they keep the meaning their names promise:
--   assignment_people = COUNT(DISTINCT master_key) - people, not rows. Rows are not people.
--   duplicate_sends   = rows beyond one per (master_key, layer). A person holding one
--                       commercial and one educational row is the designed state, not a
--                       duplicate; before this change it read 3 883 on 2026-09-11 04:31 UTC
--                       and step5_gate would have refused on it every night.
asg AS (
  SELECT master_key, email, layer, week_start FROM {C.T_ASSIGNMENT}
  WHERE week_start = DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY))
    AND layer IN {C.ALL_LAYERS_SQL}
)
SELECT
  (SELECT COUNT(*) FROM l3) AS list3_total,
  (SELECT COUNTIF(email IN (SELECT email FROM cl)) FROM l3) AS list3_in_engine,
  (SELECT COUNTIF(email NOT IN (SELECT email FROM cl)
              AND email NOT IN (SELECT email FROM sup)) FROM l3) AS orphans_mailable,
  (SELECT COUNT(*) FROM sendable) AS sendable_rows,
  (SELECT COUNT(DISTINCT master_key) FROM sendable) AS sendable_people,
  (SELECT COUNT(*) - COUNT(DISTINCT master_key) FROM sendable) AS multi_address_people,
  (SELECT COUNT(DISTINCT master_key) FROM asg) AS assignment_people,
  (SELECT COUNT(*) - COUNT(DISTINCT FORMAT('%s|%s', master_key, layer)) FROM asg) AS duplicate_sends,
  (SELECT COUNTIF(email IN (SELECT email FROM sup)) FROM asg) AS assignment_suppressed
"""


def coverage():
    r = query(COVERAGE_SQL)[0]
    return {k: (int(v) if v is not None else None) for k, v in r.items()}
