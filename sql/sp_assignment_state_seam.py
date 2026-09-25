"""SEQUENCE SEAM for mkt_control.sp_build_contact_weekly_assignment (MAIN 2026-09-25 17:45, point 1).

The assignment SP keeps its welcome entry gate and rule 8a; only its INPUT changes: next_email_type
(+ offer_rung, carried for the record) comes from mkt_control.contact_sequence_state instead of
customer_lifecycle. One planner: shadow plan = assignment + state.

Usage (ops shell, bq CLI):
  python3 sp_assignment_state_seam.py check   # fetch live body, patch, bq --dry_run the CREATE; print diff counts
  python3 sp_assignment_state_seam.py deploy  # ONLY on MAIN's word; refuses without SEAM_DEPLOY_APPROVED=MAIN-<date>

The patch is an exact string replacement of ONE block; if the live body does not contain the block
verbatim (someone changed the SP), it refuses rather than guessing.
"""
import os
import subprocess
import sys
import json

SP = "jaunais-za-aizv04022026.mkt_control.sp_build_contact_weekly_assignment"
P = "jaunais-za-aizv04022026"

OLD = f"""  CREATE OR REPLACE TABLE `{P}.mkt_control._assign_cl` AS
  SELECT master_key, LOWER(TRIM(email)) AS email, lifecycle_stage, next_email_type,
         first_order, last_order, days_since_last, language, segment
  FROM `{P}.business_marts.customer_lifecycle`
  WHERE lifecycle_stage != 'blocked'"""

NEW = f"""  -- SEQUENCE SEAM (MAIN 2026-09-25 17:45). The STEP lives only in the send engine's state
  -- (mkt_control.contact_sequence_state, single writer Sutisanas dzinejs); customer_lifecycle's
  -- next_email_type/*_step are no longer read. A letter is planned this week only if the engine
  -- says it is due by the end of this ISO week; otherwise the person falls to the ladder below
  -- exactly as a NULL next_email_type always did (akcija). Welcome gate and rule 8a are unchanged.
  -- An empty or stale state (> 36 h) stops the build: loud beats a silent week of akcija.
  IF (SELECT COUNT(*) FROM `{P}.mkt_control.contact_sequence_state`) = 0
     OR (SELECT TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(updated_at), HOUR)
         FROM `{P}.mkt_control.contact_sequence_state`) > 36 THEN
    RAISE USING MESSAGE = 'SEQUENCE_STATE_EMPTY_OR_STALE: mkt_control.contact_sequence_state is empty or older than 36 h';
  END IF;

  CREATE OR REPLACE TABLE `{P}.mkt_control._assign_cl` AS
  SELECT l.master_key, LOWER(TRIM(l.email)) AS email, l.lifecycle_stage,
         IF(s.next_due_on <= DATE_ADD(DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY)), INTERVAL 6 DAY),
            s.next_email_type, NULL) AS next_email_type,
         s.next_offer_rung AS offer_rung,
         l.first_order, l.last_order, l.days_since_last, l.language, l.segment
  FROM `{P}.business_marts.customer_lifecycle` l
  LEFT JOIN `{P}.mkt_control.contact_sequence_state` s ON s.master_key = l.master_key
  WHERE l.lifecycle_stage != 'blocked'"""

# the WHERE continues with "AND master_key IN (...)" - qualify it for the join
OLD_TAIL = f"""    AND master_key IN (SELECT master_key FROM `{P}.business_marts.tiktik_buyer_master` WHERE is_tiktik_buyer);"""
NEW_TAIL = f"""    AND l.master_key IN (SELECT master_key FROM `{P}.business_marts.tiktik_buyer_master` WHERE is_tiktik_buyer);"""

DIFF_SQL = f"""
WITH cur AS (SELECT master_key, email_type FROM `{P}.business_marts.contact_weekly_assignment`
             WHERE layer='commercial' AND week_start = DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY))),
st AS (SELECT master_key,
              IF(next_due_on <= DATE_ADD(DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY)), INTERVAL 6 DAY),
                 next_email_type, NULL) AS eng FROM `{P}.mkt_control.contact_sequence_state`)
SELECT cur.email_type AS today, IFNULL(st.eng, '(none -> ladder)') AS engine_input, COUNT(*) AS people
FROM cur LEFT JOIN st USING (master_key) GROUP BY 1, 2 ORDER BY 3 DESC
"""


def sh(args, stdin=None):
    r = subprocess.run(args, input=stdin, capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"FAILED {' '.join(args[:3])}: {r.stderr[:2000]}")
    return r.stdout


def patched():
    body = json.loads(sh(["bq", "show", "--format=json", "--routine", SP.replace(f"{P}.", "", 1)]))["definitionBody"]
    if body.count(OLD) != 1 or body.count(OLD_TAIL) != 1:
        sys.exit("REFUSED: the live SP no longer contains the expected block verbatim - someone changed it")
    return body.replace(OLD, NEW).replace(OLD_TAIL, NEW_TAIL)


def main(mode):
    body = patched()
    ddl = f"CREATE OR REPLACE PROCEDURE `{SP}`()\n{body}"
    if mode == "check":
        print(sh(["bq", "query", "--nouse_legacy_sql", "--dry_run"], ddl).strip() or "DRY_RUN_OK")
        print(sh(["bq", "query", "--nouse_legacy_sql", "--format=pretty"], DIFF_SQL))
    elif mode == "deploy":
        if not os.environ.get("SEAM_DEPLOY_APPROVED", "").startswith("MAIN-"):
            sys.exit("REFUSED: deploy needs SEAM_DEPLOY_APPROVED=MAIN-<date> (MAIN's word)")
        print(sh(["bq", "query", "--nouse_legacy_sql"], ddl))
    else:
        sys.exit("usage: check | deploy")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "check")
