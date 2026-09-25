"""Daily shadow plan (Sūtīšanas dzinējs, D1 + D3). SHADOW ONLY - this job cannot send.

1. rebuilds mkt_control.contact_sequence_state (single writer) with sequence.advance()
2. writes one mkt_control.shadow_send_plan row per person: "would send letter X (template id) on
   date D because ...", diffed against the previous plan_date
3. writes, for every would-send row due within HORIZON_DAYS, the exact Pipedrive record the live
   path would write (pd_record.render + pd_record.write(shadow=True)) to mkt_control.shadow_pd_writes

It imports no Brevo module, no Pipedrive client and no send function (tests pin that).
History: MAIN reviewed brevo_campaign_class 2026-09-25 17:45 (reviewed_by='MAIN 2026-09-25'). Only
send_log rows whose campaign is classified counts_for_sequence=TRUE AND reviewed advance a person's
state (today: campaign 221 = winback_1 at rung 1, 22.09). Everything else in send_log is frequency
history only. A row is applied once: only sends after the person's state.last_sent_on.
"""
import datetime as dt
import logging
import os
import uuid

from google.cloud import bigquery

import pd_record
import sequence as S

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sequence_job")

P = "jaunais-za-aizv04022026"
T_STATE = f"{P}.mkt_control.contact_sequence_state"
T_SLOG = f"{P}.mkt_control.contact_sequence_log"
T_PLAN = f"{P}.mkt_control.shadow_send_plan"
T_PDW = f"{P}.mkt_control.shadow_pd_writes"
RUN_ID = os.environ.get("CLOUD_RUN_EXECUTION") or f"local-{uuid.uuid4().hex[:12]}"
HORIZON_DAYS = int(os.environ.get("HORIZON_DAYS", "7"))
LADDER_POLICY = "defaults-UNCONFIRMED"
HISTORY_SQL = f"""
SELECT l.master_key, l.email_type, l.track, l.rung, DATE(l.sent_at, 'Europe/Riga') AS sent_on,
       l.campaign_id, l.source
FROM `{P}.mkt_control.send_log` l
LEFT JOIN `{P}.mkt_control.brevo_campaign_class` c ON c.campaign_id = l.campaign_id
WHERE l.master_key IS NOT NULL
  AND (l.source = 'engine_live'
       OR (l.source = 'brevo_history' AND c.counts_for_sequence AND c.reviewed_by IS NOT NULL))
ORDER BY l.master_key, l.sent_at
"""

INPUT_SQL = f"""
WITH lc AS (
  SELECT master_key, LOWER(TRIM(email)) AS email, lifecycle_stage, last_order, first_order,
         person_id,
         ROW_NUMBER() OVER (PARTITION BY master_key ORDER BY last_order DESC, email) AS rn
  FROM `{P}.business_marts.customer_lifecycle` WHERE master_key IS NOT NULL),
sup AS (
  SELECT DISTINCT l.master_key FROM `{P}.business_marts.customer_lifecycle` l
  JOIN `{P}.business_marts.email_suppression_all` s ON LOWER(TRIM(s.email)) = LOWER(TRIM(l.email)))
SELECT lc.master_key, lc.email AS send_email, lc.lifecycle_stage,
       lc.last_order, lc.first_order, lc.person_id, (sup.master_key IS NOT NULL) AS suppressed
FROM lc LEFT JOIN sup USING (master_key)
WHERE lc.rn = 1
"""


def _d(v):
    return None if v is None else (v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v)[:10]))


def main():
    bq = bigquery.Client(project=P)
    today = dt.date.today()
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    facts = list(bq.query(INPUT_SQL).result())
    prev = {r["master_key"]: r for r in bq.query(f"SELECT * FROM `{T_STATE}`").result()}
    tmap = {r["email_type"]: r["template_id"] for r in bq.query(
        f"SELECT email_type, ANY_VALUE(template_id) AS template_id FROM `{P}.mkt_control.email_template_map` "
        f"GROUP BY 1").result()}
    history = {}
    for r in bq.query(HISTORY_SQL).result():
        history.setdefault(r["master_key"], []).append(r)
    last_plan = {r["master_key"]: r for r in bq.query(f"""
        SELECT master_key, email_type, planned_send_date, would_send, offer_rung FROM `{T_PLAN}`
        WHERE plan_date = (SELECT MAX(plan_date) FROM `{T_PLAN}` WHERE plan_date < CURRENT_DATE())""").result()}

    states, log_rows, plan_rows, pd_rows = [], [], [], []
    seen = set()
    for f in facts:
        mk = f["master_key"]; seen.add(mk)
        p = prev.get(mk)
        st = S.State(mk) if p is None else S.State(
            mk, p["track"], _d(p["track_entered_on"]), p["step"] or 0, p["rung"], _d(p["rung_set_on"]),
            p["rung_month"], _d(p["ladder_cleared_on"]), p["last_email_type"], _d(p["last_sent_on"]))
        st, src = S.apply_history(st, history.get(mk, []))
        d = S.advance(st, S.Facts(f["lifecycle_stage"], _d(f["last_order"]), _d(f["first_order"]),
                                  bool(f["suppressed"])), today)
        tid = tmap.get(d.next_email_type) if d.next_email_type else None
        hold = d.hold_reason or (None if not d.next_email_type or tid else "NO_TEMPLATE_IN_MAP")
        s = d.state
        states.append({
            "master_key": mk, "send_email": f["send_email"], "lifecycle_stage": f["lifecycle_stage"],
            "track": s.track, "track_entered_on": s.track_entered_on and s.track_entered_on.isoformat(),
            "step": s.step, "rung": s.rung, "rung_set_on": s.rung_set_on and s.rung_set_on.isoformat(),
            "rung_month": s.rung_month, "ladder_cleared_on": s.ladder_cleared_on and s.ladder_cleared_on.isoformat(),
            "last_email_type": s.last_email_type, "last_sent_on": s.last_sent_on and s.last_sent_on.isoformat(),
            "last_send_source": src or (p and p["last_send_source"]), "next_email_type": d.next_email_type, "next_template_id": tid,
            "next_due_on": d.next_due_on and d.next_due_on.isoformat(), "next_offer_rung": d.offer_rung,
            "next_offer_valid_until": d.offer_valid_until and d.offer_valid_until.isoformat(),
            "next_reason": d.reason, "hold_reason": hold,
            "last_order_on": _d(f["last_order"]) and _d(f["last_order"]).isoformat(),
            "suppressed": bool(f["suppressed"]), "ladder_policy": LADDER_POLICY, "run_id": RUN_ID,
            "updated_at": now})
        for field, before, after in d.changes:
            log_rows.append({"run_id": RUN_ID, "changed_at": now, "master_key": mk, "field": field,
                             "before_value": None if before is None else str(before),
                             "after_value": None if after is None else str(after),
                             "reason": d.reason, "shadow": True})
        would = hold is None and d.next_email_type is not None
        lp = last_plan.get(mk)
        key = (d.next_email_type, d.next_due_on and d.next_due_on.isoformat(), would, d.offer_rung)
        prev_key = lp and (lp["email_type"], lp["planned_send_date"] and str(lp["planned_send_date"]),
                           lp["would_send"], lp["offer_rung"])
        plan_rows.append({
            "plan_date": today.isoformat(), "run_id": RUN_ID, "master_key": mk, "email": f["send_email"],
            "track": s.track, "step": s.step + 1 if d.next_email_type else s.step,
            "email_type": d.next_email_type, "template_id": tid,
            "interface_template_id": S.INTERFACE_V1.get(d.next_email_type),
            "offer_rung": d.offer_rung, "planned_send_date": d.next_due_on and d.next_due_on.isoformat(),
            "offer_valid_until": d.offer_valid_until and d.offer_valid_until.isoformat(),
            "would_send": would, "hold_reason": hold, "reason": d.reason,
            "diff_vs_prev": "new" if lp is None else ("same" if key == prev_key else "changed"),
            "planned_at": now})
        if would and d.next_due_on and (d.next_due_on - today).days < HORIZON_DAYS:
            rec = pd_record.render(person_id=f["person_id"], org_id=None, master_key=mk,
                                   email=f["send_email"], email_type=d.next_email_type, template_id=tid,
                                   send_date=d.next_due_on, offer_rung=d.offer_rung, reason=d.reason,
                                   campaign_ref="(shadow)")
            pd_record.write(rec, shadow=True, pd_writer=_no_pd_writer, shadow_sink=lambda row, mk=mk, et=d.next_email_type:
                            pd_rows.append({"plan_date": today.isoformat(), "run_id": RUN_ID,
                                            "master_key": mk, "email_type": et, "planned_at": now, **row}))
    for mk, lp in last_plan.items():
        if mk not in seen:
            plan_rows.append({"plan_date": today.isoformat(), "run_id": RUN_ID, "master_key": mk,
                              "email": None, "track": None, "step": None, "email_type": None,
                              "template_id": None, "interface_template_id": None, "offer_rung": 0,
                              "planned_send_date": None, "would_send": False, "hold_reason": "DROPPED",
                              "reason": "not in customer_lifecycle today", "diff_vs_prev": "changed",
                              "planned_at": now})

    # idempotent per day: today's shadow rows are replaced, state is replaced whole (single writer)
    for t in (T_PLAN, T_PDW):
        bq.query(f"DELETE FROM `{t}` WHERE plan_date = CURRENT_DATE()").result()
    J = bigquery.LoadJobConfig
    bq.load_table_from_json(states, T_STATE, job_config=J(write_disposition="WRITE_TRUNCATE")).result()
    for rows, t in ((log_rows, T_SLOG), (plan_rows, T_PLAN), (pd_rows, T_PDW)):
        if rows:
            bq.load_table_from_json(rows, t, job_config=J(write_disposition="WRITE_APPEND")).result()
    disagree = S.template_disagreements(tmap)
    log.info("SHADOW_DONE run=%s people=%s plan_rows=%s would_send=%s pd_would_writes=%s state_changes=%s "
             "interface_v1_vs_map_disagreements=%s", RUN_ID, len(states), len(plan_rows),
             sum(r["would_send"] for r in plan_rows), len(pd_rows), len(log_rows), disagree)


def _no_pd_writer(record):
    raise RuntimeError("shadow job reached the Pipedrive writer - refused")


if __name__ == "__main__":
    main()
