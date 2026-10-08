"""BUYER OVERRIDE + EARLY ASSIGNMENT (Raivis 2026-10-08 13:29, MAIN 14:00; Sūtīšanas dzinējs 7). NOT DEPLOYED.

Raivis: a client who bought today must not get a reactivation letter tomorrow; overnight the client moves to the
correct list. customer_lifecycle.last_order comes from Paytraq documents 1-2 days late, so the weekly assignment
(07:31 today) still puts a fresh buyer into reorder / winback / lost, and the PAP night prices them.

Measured 2026-10-08 (Sūtīšanas dzinējs 7) on 25.09-08.10: 58 people paid a Mozello order on day D and were in a
reorder / winback / lost plan on D+1 (25 would_send rows). Since the planner hold ORDER_AFTER_LAST_PURCHASE went live
(plan of 05.10) every such letter was held but ONE: an order with no P6 deal (lost_quarterly, order 04.10). The hold
does not move the person between LISTS (assignment, PAP audience, Brevo lists) - this module does.

~04:55 Europe/Riga, AFTER mozello-orders ingest (~04:21) and customer-identity-rebuild (04:50), BEFORE the PAP night:
  1. freshness: mozello_orders fetched today AND customer_identity built today - else NOTHING is built (loud);
  2. mkt_control.buyer_override rows of today = masters with a PAID Mozello order newer than last_order;
  3. CALL the assignment procedure config.SP_ASSIGNMENT (patched: today's override rows read as stage 'active');
  4. one mkt_control.assignment_build_log row (build_on, built_at, mode, override_rows, ok, detail).
07:30 (bq.build_assignment with ASSIGN_MODE=recheck): no rebuild; refuses unless today's log row is ok.

Usage: python3 assign_early.py early | dry     (dry = steps 1-2 counted, nothing written)
"""
from __future__ import annotations

import json
import sys

import config as C

P = "jaunais-za-aizv04022026"
T_OVR = f"{P}.mkt_control.buyer_override"
T_LOG = f"{P}.mkt_control.assignment_build_log"
SP = C.SP_ASSIGNMENT                             # the one name of the assignment procedure
T_GRAIN = f"`{P}.mkt_control.assignment_grain_violation`"   # = config.T_GRAIN_GUARD

FRESH_SQL = f"""
SELECT
  (SELECT DATE(MAX(source_fetched_at), 'Europe/Riga') FROM `{P}.business_marts.mozello_orders`
   WHERE source_mode = 'daily') = CURRENT_DATE('Europe/Riga') AS orders_today,
  (SELECT DATE(MAX(built_at), 'Europe/Riga') FROM `{P}.business_marts.customer_identity`)
   = CURRENT_DATE('Europe/Riga') AS identity_today"""

# who: a master with a PAID Mozello order on a date AFTER customer_lifecycle.last_order (strictly: an order booked the
# same day is already in last_order). 30 days back is enough: Paytraq lags 1-2 days.
OVR_SELECT = f"""
WITH idn AS (SELECT DISTINCT email_norm, master_key FROM `{P}.business_marts.customer_identity`
             WHERE master_key IS NOT NULL AND email_norm IS NOT NULL),
lc AS (SELECT master_key, MAX(last_order) AS last_order FROM `{P}.business_marts.customer_lifecycle`
       WHERE master_key IS NOT NULL GROUP BY 1),
o AS (SELECT i.master_key, mo.created_date, mo.order_id, mo.created_at
      FROM `{P}.business_marts.mozello_orders` mo JOIN idn i ON i.email_norm = LOWER(TRIM(mo.email))
      WHERE mo.payment_status = 'paid'
        AND mo.created_date >= DATE_SUB(CURRENT_DATE('Europe/Riga'), INTERVAL 30 DAY))
SELECT CURRENT_DATE('Europe/Riga') AS built_on, CURRENT_TIMESTAMP() AS built_at, o.master_key,
       MAX(o.created_date) AS shop_order_on,
       ARRAY_AGG(o.order_id ORDER BY o.created_at DESC LIMIT 1)[OFFSET(0)] AS order_id,
       ANY_VALUE(lc.last_order) AS last_order, 'mozello_paid' AS source
FROM o LEFT JOIN lc USING (master_key)
GROUP BY o.master_key
HAVING MAX(o.created_date) > IFNULL(ANY_VALUE(lc.last_order), DATE '1900-01-01')"""

OVR_DDL = f"""CREATE TABLE IF NOT EXISTS `{T_OVR}` (built_on DATE, built_at TIMESTAMP, master_key STRING,
  shop_order_on DATE, order_id STRING, last_order DATE, source STRING)"""
LOG_DDL = f"""CREATE TABLE IF NOT EXISTS `{T_LOG}` (build_on DATE, built_at TIMESTAMP, mode STRING,
  override_rows INT64, ok BOOL, detail STRING)"""


def log_sql(mode, n, ok, detail):
    return (f"INSERT INTO `{T_LOG}` VALUES (CURRENT_DATE('Europe/Riga'), CURRENT_TIMESTAMP(), '{mode}', {int(n)}, "
            f"{'TRUE' if ok else 'FALSE'}, {json.dumps(json.dumps(detail, ensure_ascii=False))})")


def run(q, mode="early") -> int:
    """q(sql) -> list of dict rows. Returns 0 = built, 2 = refused (stale source), 3 = grain violation."""
    f = (q(FRESH_SQL) or [{}])[0]
    fresh = bool(f.get("orders_today")) and bool(f.get("identity_today"))
    n = int((q(f"SELECT COUNT(*) AS n FROM ({OVR_SELECT})") or [{"n": 0}])[0]["n"])
    if mode == "dry":
        print("ASSIGN_EARLY_DRY " + json.dumps({"fresh": f, "override_rows": n}, default=str))
        return 0 if fresh else 2
    q(OVR_DDL)
    q(LOG_DDL)
    if not fresh:
        q(log_sql(mode, 0, False, {"refused": "SOURCE_NOT_OF_TODAY", "fresh": f}))
        print("ASSIGN_EARLY_REFUSED " + json.dumps(f, default=str))
        return 2
    q(f"DELETE FROM `{T_OVR}` WHERE built_on = CURRENT_DATE('Europe/Riga')")
    q(f"INSERT INTO `{T_OVR}` {OVR_SELECT}")
    q(f"CALL {SP}()")
    bad = q(f"SELECT COUNT(*) AS n FROM {T_GRAIN}")
    viol = int((bad or [{"n": 0}])[0]["n"])
    q(log_sql(mode, n, viol == 0, {"grain_violations": viol}))
    print("ASSIGN_EARLY " + json.dumps({"override_rows": n, "grain_violations": viol}))
    return 0 if viol == 0 else 3


RECHECK_SQL = f"""SELECT COUNTIF(ok AND mode = 'early') AS ok FROM `{T_LOG}`
WHERE build_on = CURRENT_DATE('Europe/Riga')"""


def recheck_ok(q) -> bool:
    """07:30: today's early build exists and was ok. False = the caller refuses (no silent rebuild)."""
    try:
        return int((q(RECHECK_SQL) or [{"ok": 0}])[0]["ok"] or 0) > 0
    except Exception:  # noqa: BLE001 - no table yet = no early build
        return False


def _client_q():
    from google.cloud import bigquery
    c = bigquery.Client(project=P)
    return lambda sql: [dict(r) for r in c.query(sql).result()]


if __name__ == "__main__":
    sys.exit(run(_client_q(), (sys.argv[1:] or ["early"])[0]))
