"""BUYER OVERRIDE patch for mkt_control.sp_build_contact_weekly_assignment (MAIN 2026-10-08 14:00). NOT DEPLOYED.

ONE exact-text replacement: the SP's _assign_cl reads customer_lifecycle; it now reads customer_lifecycle with
TODAY's mkt_control.buyer_override rows applied - such a master reads as stage 'active' (-> rhythm_next, out of the
reorder / winback / lost audiences, the PAP price audience and the variant lists), last_order = the paid shop order
date, days_since_last recomputed. Only rows with built_on = today are read: a failed 04:55 step leaves the SP exactly
as today (no stale override is ever applied). Everything else in the body is untouched.

Usage (ops shell, bq CLI):
  python3 sp_buyer_override.py check    # fetch the live body, patch, print sha of old/new, dry-run the CREATE
  python3 sp_buyer_override.py deploy   # ONLY on MAIN's word: needs OVERRIDE_DEPLOY_APPROVED=MAIN-<YYYY-MM-DD>;
                                        # first copies the live body to ..._bak_<YYYYMMDD> (= the rollback)
Rollback: CREATE OR REPLACE PROCEDURE <SP>() BEGIN <body of _bak_YYYYMMDD> END  (printed by deploy).
"""
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys

P = "jaunais-za-aizv04022026"
SP = f"{P}.mkt_control.sp_build_contact_weekly_assignment"

OLD = f"""  FROM `{P}.business_marts.customer_lifecycle`
  WHERE lifecycle_stage != 'blocked'"""

NEW = f"""  -- BUYER OVERRIDE (Raivis 2026-10-08 13:29, MAIN 14:00): a master with a PAID Mozello order newer than its Paytraq
  -- last_order (mkt_control.buyer_override, built ~04:55 from the nightly order ingest) reads as stage 'active' -
  -- a fresh buyer never stays in reorder / winback / lost. Only TODAY's rows; none = the body as before.
  FROM (SELECT l.* REPLACE (
                 IF(o.master_key IS NOT NULL AND l.lifecycle_stage != 'blocked', 'active', l.lifecycle_stage) AS lifecycle_stage,
                 IF(o.master_key IS NOT NULL, o.shop_order_on, l.last_order) AS last_order,
                 IF(o.master_key IS NOT NULL, DATE_DIFF(CURRENT_DATE('Europe/Riga'), o.shop_order_on, DAY),
                    l.days_since_last) AS days_since_last)
        FROM `{P}.business_marts.customer_lifecycle` l
        LEFT JOIN (SELECT master_key, MAX(shop_order_on) AS shop_order_on FROM `{P}.mkt_control.buyer_override`
                   WHERE built_on = CURRENT_DATE('Europe/Riga') GROUP BY 1) o
          ON o.master_key = l.master_key)
  WHERE lifecycle_stage != 'blocked'"""


def bq(sql, *flags):
    r = subprocess.run(["bq", "--project_id", P, "query", "--nouse_legacy_sql", "--format=json", *flags],
                       input=sql, capture_output=True, text=True, timeout=160)
    if r.returncode:
        raise SystemExit("bq failed: " + (r.stderr or r.stdout)[:800])
    return r.stdout


def live_body():
    out = bq("SELECT routine_definition AS b FROM mkt_control.INFORMATION_SCHEMA.ROUTINES "
             "WHERE routine_name = 'sp_build_contact_weekly_assignment'")
    return json.loads(out[out.index("["):])[0]["b"]


def patched(body):
    if body.count(OLD) != 1:
        raise SystemExit(f"REFUSED: the live body holds the replaced block {body.count(OLD)} times (need exactly 1) "
                         "- someone changed the SP; not guessing")
    return body.replace(OLD, NEW)


def sha(t):
    return hashlib.sha256(t.encode("utf-8")).hexdigest()[:12]


def main(mode):
    body = live_body()
    new = patched(body)
    print(json.dumps({"old_sha": sha(body), "new_sha": sha(new), "old_len": len(body), "new_len": len(new)}))
    create = f"CREATE OR REPLACE PROCEDURE `{SP}`()\n{new}"
    if mode == "check":
        bq(create, "--dry_run")
        print("DRY_RUN_OK (nothing changed)")
        return 0
    if mode == "deploy":
        want = f"MAIN-{dt.date.today().isoformat()}"
        if os.environ.get("OVERRIDE_DEPLOY_APPROVED") != want:
            raise SystemExit(f"REFUSED: OVERRIDE_DEPLOY_APPROVED must be {want} (MAIN's word of today)")
        bak = f"{SP}_bak_{dt.date.today().strftime('%Y%m%d')}"
        bq(f"CREATE OR REPLACE PROCEDURE `{bak}`()\n{body}")
        bq(create)
        print(f"DEPLOYED. Rollback: CREATE OR REPLACE PROCEDURE `{SP}`() <body of `{bak}`>  (old sha {sha(body)})")
        return 0
    raise SystemExit("usage: check | deploy")


if __name__ == "__main__":
    sys.exit(main((sys.argv[1:] or ["check"])[0]))
