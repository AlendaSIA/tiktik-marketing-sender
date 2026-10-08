"""NIGHTLY SEND_LOG IMPORT (MAIN 2026-10-08 17:10 + 17:45, Sūtīšanas dzinējs 7). BUILT, NOT DEPLOYED.

Why: mkt_control.send_log was a one-time reconstruction (job brevo-history-snapshot, 25.09). The planner reads it as
the contact history (sequence_job.HISTORY_SQL), so a campaign that never reaches it is a letter the planner cannot see.

Job: `python sendlog_sync.py` on the tiktik-edu-send image (same secrets), ~04:30 Europe/Riga, after 04:00 and before
the 08:05 plan. Its own entry point: edu.py stays a path that never writes send_log (tests/test_edu.py); this module
only borrows edu.history and edu._brevo (GET).
  1. edu.history(): every SENT Brevo campaign the recipients history does not hold yet -> brevo_campaign_recipients_hist
     (recipient exports; nothing in the Brevo account changes) + one edu_hist_state row.
  2. every campaign in that history that send_log does not hold yet: ONE GET /emailCampaigns/{id} (never the listing
     here), then
       - a brevo_campaign_class row if none exists: kind 'unclassified', counts_for_sequence FALSE, reviewed_by NULL
         (the planner holds reactivation letters for its recipients until MAIN reviews it - CAMPAIGN_UNREVIEWED);
       - its recipients into send_log: source 'brevo_history', run_id 'sendlog-YYYYMMDD', master_key via
         customer_identity - the same columns as the backfill of 2026-10-08 (run_id backfill-sd7-20261008);
       - rows vs Brevo's sent count; more than max(10, 1 %) apart = not ok.
  3. ONE mkt_control.send_log_sync_state row (synced_at, run_id, ok, ...). The planner's freshness gate reads only
     ok rows: no ok row dated yesterday or today = SEND_LOG_STALE (sequence.send_log_stale).
It sends nothing to anyone. Rollback of a night: DELETE send_log / brevo_campaign_class / send_log_sync_state rows of
that run_id (class rows by classified_by = 'sendlog <run_id>').
"""
import datetime as dt
import json
import zoneinfo

P = "jaunais-za-aizv04022026"
M = f"{P}.mkt_control"
T_LOG, T_CLASS = f"{M}.send_log", f"{M}.brevo_campaign_class"
T_HIST, T_STATE = f"{M}.brevo_campaign_recipients_hist", f"{M}.send_log_sync_state"
T_IDN = f"{P}.business_marts.customer_identity"
SOURCE = "brevo_history"
TOL_ABS, TOL_PCT = 10, 0.01

STATE_DDL = f"""CREATE TABLE IF NOT EXISTS `{T_STATE}` (synced_at TIMESTAMP, run_id STRING, ok BOOL,
  hist_rc INT64, new_campaigns INT64, added_rows INT64, newest_campaign_id INT64, detail STRING)"""

NEW_SQL = f"""SELECT DISTINCT h.campaign_id AS c FROM `{T_HIST}` h
WHERE h.campaign_id IS NOT NULL
  AND h.campaign_id NOT IN (SELECT DISTINCT campaign_id FROM `{T_LOG}` WHERE campaign_id IS NOT NULL)
ORDER BY c"""

CLASS_SQL = f"""INSERT INTO `{T_CLASS}` (campaign_id, name, sent_date, kind, email_type, track, step, rung,
  counts_for_sequence, basis, classified_by, classified_at, reviewed_by)
SELECT CAST(@c AS INT64), @name, @sent, 'unclassified', NULL, NULL, NULL, NULL, FALSE,
  'nightly send_log import: not reviewed yet; the planner holds reactivation letters for its recipients',
  @by, CURRENT_TIMESTAMP(), NULL
FROM UNNEST([1]) WHERE CAST(@c AS INT64) NOT IN (SELECT campaign_id FROM `{T_CLASS}` WHERE campaign_id IS NOT NULL)"""

ROWS_SQL = f"""INSERT INTO `{T_LOG}` (master_key, email, campaign_id, brevo_list_id, email_type, template_id, track,
  step, rung, sent_at, source, run_id, brevo_message_id, utm_campaign)
WITH idn AS (SELECT email_norm, MIN(master_key) AS master_key FROM `{T_IDN}`
             WHERE master_key IS NOT NULL AND email_norm IS NOT NULL GROUP BY 1),
r AS (SELECT DISTINCT LOWER(TRIM(email)) AS email FROM `{T_HIST}`
      WHERE campaign_id = CAST(@c AS INT64) AND email IS NOT NULL)
SELECT idn.master_key, r.email, CAST(@c AS INT64), SAFE_CAST(@lst AS INT64), NULL, NULL, NULL, NULL, NULL,
  TIMESTAMP(@sent), '{SOURCE}', @run, NULL, NULL
FROM r LEFT JOIN idn ON idn.email_norm = r.email
WHERE CAST(@c AS INT64) NOT IN (SELECT DISTINCT campaign_id FROM `{T_LOG}` WHERE campaign_id IS NOT NULL)"""

COUNT_SQL = f"SELECT COUNT(*) AS n FROM `{T_LOG}` WHERE campaign_id = CAST(@c AS INT64) AND run_id = @run"
FIRST_SEND_SQL = f"""SELECT FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', TIMESTAMP(MIN(send_local), 'Europe/Riga')) AS t
FROM `{T_HIST}` WHERE campaign_id = CAST(@c AS INT64)"""


def run_id_of(now=None) -> str:
    now = now or dt.datetime.now(zoneinfo.ZoneInfo("Europe/Riga"))
    return "sendlog-" + now.strftime("%Y%m%d")


def brevo_sent(camp) -> int | None:
    """Brevo's own sent count of one campaign (GET /emailCampaigns/{id}); None when the answer has none."""
    st = camp.get("statistics") or {}
    g = (st.get("globalStats") or {}).get("sent")
    if g is not None:
        return int(g)
    per = [int(x.get("sent") or 0) for x in (st.get("campaignStats") or [])]
    return sum(per) if per else None


def within(rows: int, sent: int | None) -> bool:
    if sent is None:
        return False
    return abs(rows - sent) <= max(TOL_ABS, TOL_PCT * sent)


def sync(q, brevo_get, run_id) -> dict:
    """q(sql, **params) -> list of dict rows; brevo_get(path) -> dict. Returns the night's summary (ok, per campaign)."""
    new = [int(r["c"]) for r in q(NEW_SQL)]
    per, errors = [], []
    for cid in new:
        try:
            camp = brevo_get(f"/emailCampaigns/{cid}")
            lists = (camp.get("recipients") or {}).get("lists") or []
            sent_at = camp.get("sentDate") or camp.get("scheduledAt")
            if not sent_at:
                sent_at = ((q(FIRST_SEND_SQL, c=str(cid)) or [{}])[0]).get("t")
            if not sent_at:
                raise RuntimeError("no send time (Brevo sentDate / scheduledAt / history send_local)")
            q(CLASS_SQL, c=str(cid), name=str(camp.get("name") or ""), sent=str(sent_at), by=f"sendlog {run_id}")
            q(ROWS_SQL, c=str(cid), lst=str(lists[0]) if lists else "", sent=str(sent_at), run=run_id)
            n = int((q(COUNT_SQL, c=str(cid), run=run_id) or [{"n": 0}])[0]["n"])
            s = brevo_sent(camp)
            per.append({"campaign_id": cid, "rows": n, "brevo_sent": s, "match": within(n, s)})
        except Exception as e:  # noqa: BLE001 - a campaign that cannot be imported makes the night not ok
            errors.append(f"{cid}: {type(e).__name__}: {e}"[:160])
    return {"new": new, "per": per, "errors": errors,
            "ok": not errors and all(p["match"] for p in per)}


def run(q, brevo_get, history_fn, now=None) -> int:
    """The nightly mode. 0 = ok (state row ok=TRUE); 1 = not ok (state row ok=FALSE, the planner treats it as stale)."""
    run_id = run_id_of(now)
    try:
        hist_rc = int(history_fn(q))
    except Exception as e:  # noqa: BLE001
        hist_rc = 9
        print(f"SENDLOG_HISTORY_FAILED {type(e).__name__}: {e}"[:300])
    s = sync(q, brevo_get, run_id)
    ok = hist_rc == 0 and s["ok"]
    q(STATE_DDL)
    q(f"INSERT INTO `{T_STATE}` (synced_at, run_id, ok, hist_rc, new_campaigns, added_rows, newest_campaign_id, detail) "
      f"VALUES (CURRENT_TIMESTAMP(), @run, {'TRUE' if ok else 'FALSE'}, CAST(@h AS INT64), CAST(@nc AS INT64), "
      f"CAST(@ar AS INT64), SAFE_CAST(@mx AS INT64), @dt)", run=run_id, h=str(hist_rc), nc=str(len(s["new"])),
      ar=str(sum(p["rows"] for p in s["per"])), mx=str(max(s["new"])) if s["new"] else "",
      dt=json.dumps({"per": s["per"], "errors": s["errors"]}))
    print("SENDLOG " + json.dumps({"run_id": run_id, "ok": ok, "hist_rc": hist_rc, **s}))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    import edu
    sys.exit(run(edu.query, lambda path: edu._brevo("GET", path), edu.history))
