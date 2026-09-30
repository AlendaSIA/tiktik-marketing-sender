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

import pd_pending
import pd_record
import pd_target
import pd_writeback as W
import json
import sequence as S

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sequence_job")

P = "jaunais-za-aizv04022026"
T_STATE = f"{P}.mkt_control.contact_sequence_state"
T_SLOG = f"{P}.mkt_control.contact_sequence_log"
T_PLAN = f"{P}.mkt_control.shadow_send_plan"
T_PDW = f"{P}.mkt_control.shadow_pd_writes"
T_RUN = f"{P}.mkt_control.shadow_run_report"
T_OVERRIDE = f"{P}.mkt_control.pd_target_override"
T_PEND = f"{P}.mkt_control.pd_write_pending"
RUN_ID = os.environ.get("CLOUD_RUN_EXECUTION") or f"local-{uuid.uuid4().hex[:12]}"
HORIZON_DAYS = int(os.environ.get("HORIZON_DAYS", "7"))
LADDER_POLICY = "ladder-policy-v1 (Raivis 2026-09-28, contract 326480dce080) + " + S.CADENCE
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
  SELECT master_key, LOWER(TRIM(email)) AS email, lifecycle_stage, last_order, first_order, entry_threshold_days,
         full_name, client_name, client_id,
         ROW_NUMBER() OVER (PARTITION BY master_key ORDER BY last_order DESC, email) AS rn
  FROM `{P}.business_marts.customer_lifecycle` WHERE master_key IS NOT NULL),
sup AS (
  SELECT DISTINCT l.master_key FROM `{P}.business_marts.customer_lifecycle` l
  JOIN `{P}.business_marts.email_suppression_all` s ON LOWER(TRIM(s.email)) = LOWER(TRIM(l.email)))
SELECT lc.master_key, lc.email AS send_email, lc.lifecycle_stage,
       lc.last_order, lc.first_order, lc.entry_threshold_days, (sup.master_key IS NOT NULL) AS suppressed,
       lc.full_name, lc.client_name, pc.reg_number
FROM lc LEFT JOIN sup USING (master_key)
LEFT JOIN `{P}.paytraq_core.clients` pc ON CAST(pc.client_id AS STRING) = CAST(lc.client_id AS STRING)
WHERE lc.rn = 1
"""

# GATE 232/233 source (see sequence.PRICE_GATED_TYPES). TODAY: the PAP transport, contract v2.8.1 A4
# (rows per customer x SKU: price_r1..r3, shop_gross, valid_until), read-only. A rung counts only
# when its price is STRICTLY more than 5 % below the shop (P2, A4 "0.05 STRICT") - the same test the
# writer applies before it may write a REF price, i.e. before OFFER_VALID_UNTIL can be non-empty.
# LATER: when Nakts sinhronizācija writes OFFER_VALID_UNTIL, the gate reads that value instead
# (per contact, "DD.MM.YYYY", non-empty = pass). Its BQ location is not fixed yet - no OFFER_* column
# exists in business_marts.marketing_brevo_payload (measured 2026-09-28); MAIN names it, then only
# this constant changes.
RUNG_PRICE_SOURCE = os.environ.get("RUNG_PRICE_SOURCE", f"{P}.business_marts.pap_block_current_v281")
RUNG_PRICE_SQL = f"""
SELECT master_key,
       MAX(IF(price_r1 < 0.95 * shop_gross, valid_until, NULL)) AS vu_r1,
       MAX(IF(price_r2 < 0.95 * shop_gross, valid_until, NULL)) AS vu_r2,
       MAX(IF(price_r3 < 0.95 * shop_gross, valid_until, NULL)) AS vu_r3
FROM `{RUNG_PRICE_SOURCE}`
WHERE master_key IS NOT NULL AND shop_gross > 0
  AND built_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 26 HOUR)   -- v2.8.2 A7: stale = no price
GROUP BY master_key
"""


# PD TARGET by send address (COMMAND 4, P-A..P-E; see pd_target.py). customer_lifecycle.person_id is
# NOT read any more. Snapshot age = hours since the last pipedrive_persons ingest.
PERSONS_SQL = f"""
SELECT id, org_id, name, LOWER(TRIM(email_primary)) AS email_primary, update_time,
       ARRAY(SELECT DISTINCT LOWER(TRIM(e)) FROM UNNEST(SPLIT(email_all, ',')) e
             WHERE TRIM(e) NOT IN ('', 'nan')) AS emails
FROM `{P}.channel_raw.pipedrive_persons`
"""
PERSONS_AGE_SQL = f"""SELECT TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(ingested_at), MINUTE) / 60.0 AS age_h,
  MAX(ingested_at) AS ingested_at FROM `{P}.channel_raw.pipedrive_persons`"""
ORG_ADDR_SQL = f"""
SELECT LOWER(x) AS email, ARRAY_AGG(DISTINCT id) AS org_ids
FROM `{P}.channel_raw.pipedrive_orgs`,
     UNNEST(REGEXP_EXTRACT_ALL(raw_json, r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{{2,}}')) x
GROUP BY 1
"""
MASTER_ADDR_SQL = f"""SELECT master_key, ARRAY_AGG(DISTINCT email_norm) AS emails
FROM `{P}.business_marts.customer_identity` WHERE master_key IS NOT NULL AND email_norm IS NOT NULL GROUP BY 1"""
OVERRIDE_SQL = f"SELECT LOWER(TRIM(email)) AS email, person_id, org_id FROM `{T_OVERRIDE}`"
# A7 measured, not inferred (COMMAND 4 point 4): the price table's age goes into the log + run report.
RUNG_BUILT_SQL = f"SELECT MAX(built_at) AS built_at FROM `{RUNG_PRICE_SOURCE}`"


# LADDER POLICY v1 L6/L8 need every PAID order date per contact (Paytraq paid sales -> master_key).
ORDERS_SQL = f"""
SELECT i.master_key, ARRAY_AGG(DISTINCT DATE(s.document_date) IGNORE NULLS) AS orders
FROM `{P}.paytraq_core.sales_documents_list` s
JOIN `{P}.business_marts.customer_identity` i ON CAST(i.client_id AS STRING) = CAST(s.client_id AS STRING)
WHERE s.document_type = 'sale' AND s.document_status = 'paid' AND i.master_key IS NOT NULL
GROUP BY 1
"""


# v2.9.3 / v2.9.4 write-back inputs (read-only)
ORGS_SQL = f"SELECT id, name, reg_number FROM `{P}.channel_raw.pipedrive_orgs`"
ATTRS_SQL = f"""
SELECT LOWER(TRIM(email)) AS email, plan_date,
  [STRUCT(audit_p1_sku AS sku, P1_PRICE AS price, P1_REF_PRICE AS ref), STRUCT(audit_p2_sku, P2_PRICE, P2_REF_PRICE),
   STRUCT(audit_p3_sku, P3_PRICE, P3_REF_PRICE), STRUCT(audit_p4_sku, P4_PRICE, P4_REF_PRICE),
   STRUCT(audit_p5_sku, P5_PRICE, P5_REF_PRICE), STRUCT(audit_p6_sku, P6_PRICE, P6_REF_PRICE),
   STRUCT(audit_p7_sku, P7_PRICE, P7_REF_PRICE), STRUCT(audit_p8_sku, P8_PRICE, P8_REF_PRICE)] AS slots
FROM `{P}.mkt_control.shadow_brevo_price_attrs`
WHERE plan_date <= CURRENT_DATE('Europe/Riga')
QUALIFY ROW_NUMBER() OVER (PARTITION BY LOWER(TRIM(email)) ORDER BY plan_date DESC) = 1
"""


def writeback_target(t, w):
    """Target as the write-back will act on it: held by the write-back guard, or 'create_person'
    for a contact v2.9.4 creates (C5 / org-only), else unchanged."""
    if w["hold_reason"]:
        return pd_target.Target("held", t.cls, hold_reason=w["hold_reason"])
    if w["person_ref"] == "new:person":
        return pd_target.Target("create_person", t.cls, None, t.org_id if isinstance(t.org_id, int) else None, ())
    if t.kind == "held":
        return pd_target.Target("create_person", t.cls)
    return t


def plan_template(d, tmap):
    """(template id, hold). The map row for the EXACT email_type or nothing - never a fallback. CADENCE v1 K7:
    E2 texts are not written yet, so an E2 without its own row is held E2_TEMPLATE_PENDING (template NULL)."""
    if not d.next_email_type:
        return None, d.hold_reason
    tid = tmap.get(d.next_email_type)
    if d.hold_reason:
        return tid, d.hold_reason
    if tid is None:
        return None, S.HOLD_E2_TEMPLATE if d.next_email_type in S.E2_TYPES else "NO_TEMPLATE_IN_MAP"
    return tid, None


def persons_index(rows) -> dict:
    idx = {}
    for r in rows:
        p = pd_target.Person(r["id"], r["org_id"], r["name"], r["email_primary"], r["update_time"])
        for e in r["emails"] or []:
            idx.setdefault(e, []).append(p)
    return idx


def due_token(planned, plan_date):
    """diff_vs_prev key for the date: every date on or before its own plan_date is the one value
    "due" (a due letter that shadow did not send is re-stated as due today - not a change)."""
    if planned is None:
        return None
    return "due" if planned <= plan_date else planned.isoformat()


def rung_price_map(row) -> dict:
    """{rung: valid_until} for the rungs that carry a showable price (None/absent = no price)."""
    return {r: _d(row[f"vu_r{r}"]) for r in (1, 2, 3) if row[f"vu_r{r}"] is not None}


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
        SELECT master_key, plan_date, email_type, planned_send_date, would_send, offer_rung FROM `{T_PLAN}`
        WHERE plan_date = (SELECT MAX(plan_date) FROM `{T_PLAN}` WHERE plan_date < CURRENT_DATE())""").result()}
    prices = {r["master_key"]: rung_price_map(r) for r in bq.query(RUNG_PRICE_SQL).result()}
    orders = {r["master_key"]: [_d(x) for x in r["orders"]] for r in bq.query(ORDERS_SQL).result()}
    rung_built_at = next(iter(bq.query(RUNG_BUILT_SQL).result()))["built_at"]
    person_rows = list(bq.query(PERSONS_SQL).result())
    by_address = persons_index(person_rows)
    org_idx = W.build_org_index([dict(r) for r in bq.query(ORGS_SQL).result()],
                                [{"org_id": r["org_id"], "emails": r["emails"]} for r in person_rows])
    attrs = {r["email"]: [dict(x) for x in r["slots"]] for r in bq.query(ATTRS_SQL).result()}
    age = next(iter(bq.query(PERSONS_AGE_SQL).result()))
    persons_age_h, persons_ingested_at = age["age_h"], age["ingested_at"]
    org_by_address = {r["email"]: set(r["org_ids"]) for r in bq.query(ORG_ADDR_SQL).result()}
    master_addr = {r["master_key"]: list(r["emails"]) for r in bq.query(MASTER_ADDR_SQL).result()}
    overrides = {r["email"]: (r["person_id"], r["org_id"]) for r in bq.query(OVERRIDE_SQL).result()}

    states, log_rows, plan_rows, pd_rows, held_today = [], [], [], [], []

    def resolve_now(email, mk):
        return pd_target.resolve(email, by_address=by_address, master_other_addresses=master_addr.get(mk, ()),
                                 org_by_address=org_by_address, overrides=overrides, persons_age_h=persons_age_h)
    facts_by_mk = {f["master_key"]: f for f in facts}

    def resolve_full(email, mk):
        """Retry target for the pending queue = pd_target + the v2.9.3/v2.9.4 write-back plan: a C5 contact is
        CREATED (resolves), an ambiguous org guard stays held (pd_org_ambiguous)."""
        t = resolve_now(email, mk)
        f = facts_by_mk.get(mk)
        if f is None:
            return t
        w = W.plan(t, email=email, person_name=f["full_name"], org_name=f["client_name"],
                   reg_nr=f["reg_number"], org_idx=org_idx)
        return writeback_target(t, w)

    seen = set()
    for f in facts:
        mk = f["master_key"]; seen.add(mk)
        p = prev.get(mk)
        st = S.State(mk) if p is None else S.State(
            mk, p["track"], _d(p["track_entered_on"]), p["step"] or 0, p["rung"], _d(p["rung_set_on"]),
            p["rung_month"], _d(p["ladder_cleared_on"]), p["last_email_type"], _d(p["last_sent_on"]),
            p.get("rung_cap"), _d(p.get("rung_cap_until")), _d(p.get("reorder_worked_at")))
        st, src = S.apply_history(st, history.get(mk, []))
        # L6 + L8 recomputed from the full counted history + paid orders; never loosened by a recompute
        cap, cap_until, worked = S.ladder_marks(
            [{"email_type": h["email_type"], "rung": h["rung"], "sent_on": _d(h["sent_on"])} for h in history.get(mk, [])],
            orders.get(mk, []))
        if cap_until and (st.rung_cap_until is None or cap_until > st.rung_cap_until):
            st.rung_cap, st.rung_cap_until = cap, cap_until
        if worked and (st.reorder_worked_at is None or worked > st.reorder_worked_at):
            st.reorder_worked_at = worked
        d = S.advance(st, S.Facts(f["lifecycle_stage"], _d(f["last_order"]), _d(f["first_order"]),
                                  bool(f["suppressed"]), prices.get(mk), f["entry_threshold_days"]), today)
        tid, hold = plan_template(d, tmap)
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
            "rung_cap": s.rung_cap, "rung_cap_until": s.rung_cap_until and s.rung_cap_until.isoformat(),
            "reorder_worked_at": s.reorder_worked_at and s.reorder_worked_at.isoformat(),
            "updated_at": now})
        for field, before, after in d.changes:
            log_rows.append({"run_id": RUN_ID, "changed_at": now, "master_key": mk, "field": field,
                             "before_value": None if before is None else str(before),
                             "after_value": None if after is None else str(after),
                             "reason": d.reason, "shadow": True})
        would = hold is None and d.next_email_type is not None
        lp = last_plan.get(mk)
        key = (d.next_email_type, due_token(d.next_due_on, today), would, d.offer_rung)
        prev_key = lp and (lp["email_type"], due_token(_d(lp["planned_send_date"]), _d(lp["plan_date"])),
                           lp["would_send"], lp["offer_rung"])
        plan_rows.append({
            "plan_date": today.isoformat(), "run_id": RUN_ID, "master_key": mk, "email": f["send_email"],
            "track": s.track, "step": s.step + 1 if d.next_email_type else s.step,
            "email_type": d.next_email_type, "template_id": tid,
            "interface_template_id": S.INTERFACE_V1.get(d.next_email_type),
            "offer_rung": d.offer_rung, "planned_send_date": d.next_due_on and d.next_due_on.isoformat(),
            # v2.8.2 A6: filled only for a letter that would go AND carries a rung price
            "offer_valid_until": (would and d.offer_valid_until and d.offer_valid_until.isoformat()) or None,
            "would_send": would, "hold_reason": hold, "reason": d.reason,
            "diff_vs_prev": "new" if lp is None else ("same" if key == prev_key else "changed"),
            "planned_at": now})
        if would and d.next_due_on and (d.next_due_on - today).days < HORIZON_DAYS:
            tg = resolve_now(f["send_email"], mk)
            wb = W.plan(tg, email=f["send_email"], person_name=f["full_name"], org_name=f["client_name"],
                        reg_nr=f["reg_number"], org_idx=org_idx)
            hold_pd = wb["hold_reason"]
            if hold_pd:   # COMMAND 5 (2): the e-mail is NOT held; the PD write waits in the queue
                held_today.append({"master_key": mk, "email": f["send_email"], "email_type": d.next_email_type,
                                   "hold_reason": hold_pd, "pd_class": tg.cls, "template_id": tid,
                                   "send_date": d.next_due_on.isoformat(), "offer_rung": d.offer_rung,
                                   "reason": d.reason})
            tail, lines = W.offer_summary(attrs.get(f["send_email"], []))
            pid = wb["person_ref"] if isinstance(wb["person_ref"], int) else tg.person_id
            oid = wb["org_ref"] if isinstance(wb["org_ref"], int) else tg.org_id
            rec = pd_record.render(person_id=pid, org_id=oid, master_key=mk,
                                   email=f["send_email"], email_type=d.next_email_type, template_id=tid,
                                   send_date=d.next_due_on, offer_rung=d.offer_rung, reason=d.reason,
                                   campaign_ref="(shadow)", participants=tg.participants,
                                   offer_valid_until=d.offer_valid_until, offer_tail=None if not lines else tail,
                                   product_lines=lines)
            base = {"plan_date": today.isoformat(), "run_id": RUN_ID, "master_key": mk,
                    "email_type": d.next_email_type, "planned_at": now,
                    "target_kind": writeback_target(tg, wb).kind,
                    "pd_class": tg.cls, "pd_hold_reason": hold_pd,
                    "person_ref": None if wb["person_ref"] is None else str(wb["person_ref"]),
                    "org_ref": None if wb["org_ref"] is None else str(wb["org_ref"])}
            pd_record.write(rec, shadow=True, pd_writer=_no_pd_writer,
                            shadow_sink=lambda row, base=base: pd_rows.append({**base, **row}))
            if not hold_pd:
                for c in wb["creates"]:
                    pd_rows.append({**base, "object": c["object"], "record_version": pd_record.RECORD_VERSION,
                                    "match_how": c.get("match_how"), "create_name": c.get("name"),
                                    "record_json": json.dumps(c, ensure_ascii=False, sort_keys=True, default=str)})
                for fw in W.field_writes(wb["person_ref"], email_type=d.next_email_type, send_date=d.next_due_on,
                                         offer_rung=d.offer_rung, offer_valid_until=d.offer_valid_until):
                    pd_rows.append({**base, "object": fw["object"], "record_version": pd_record.RECORD_VERSION,
                                    "field_key": fw["field_key"], "field_value": fw["field_value"],
                                    "record_json": json.dumps(fw, ensure_ascii=False, sort_keys=True)})
    for mk, lp in last_plan.items():
        if mk not in seen:
            plan_rows.append({"plan_date": today.isoformat(), "run_id": RUN_ID, "master_key": mk,
                              "email": None, "track": None, "step": None, "email_type": None,
                              "template_id": None, "interface_template_id": None, "offer_rung": 0,
                              "planned_send_date": None, "would_send": False, "hold_reason": "DROPPED",
                              "reason": "not in customer_lifecycle today", "diff_vs_prev": "changed",
                              "planned_at": now})

    # v2.9.4 reconciliation: what the write-back would create across ALL sendable contacts (counts only)
    pop = {}
    for f in facts:
        if f["suppressed"] or f["lifecycle_stage"] in (None, "blocked") or not f["send_email"]:
            continue
        pop["sendable"] = pop.get("sendable", 0) + 1
        t = resolve_now(f["send_email"], f["master_key"])
        w = W.plan(t, email=f["send_email"], person_name=f["full_name"], org_name=f["client_name"],
                   reg_nr=f["reg_number"], org_idx=org_idx)
        if w["hold_reason"]:
            k = "held_" + w["hold_reason"]
            pop[k] = pop.get(k, 0) + 1
        for c in w["creates"]:
            pop[c["object"]] = pop.get(c["object"], 0) + 1

    def render_pending(r, t):
        rec = pd_record.render(person_id=t.person_id, org_id=t.org_id, master_key=r["master_key"],
                               email=r["email"], email_type=r["email_type"], template_id=r.get("template_id"),
                               send_date=r.get("send_date"), offer_rung=r.get("offer_rung"),
                               reason=r.get("reason"), campaign_ref="(shadow)", participants=t.participants)
        return pd_record.canonical(rec).decode()
    pending_rows = [dict(r) for r in bq.query(f"SELECT * FROM `{T_PEND}`").result()]
    for r in pending_rows:
        for c in ("first_held_at", "last_tried_at", "resolved_at"):
            if r.get(c) is not None and not isinstance(r[c], str):
                r[c] = r[c].isoformat()
        if r.get("send_date") is not None and not isinstance(r["send_date"], str):
            r["send_date"] = r["send_date"].isoformat()
    pending_rows, pstats = pd_pending.step(pending_rows, held_today, resolve_full, render_pending,
                                           dt.datetime.now(dt.timezone.utc))

    # idempotent per day: today's shadow rows are replaced, state is replaced whole (single writer)
    for t in (T_PLAN, T_PDW):
        bq.query(f"DELETE FROM `{t}` WHERE plan_date = CURRENT_DATE()").result()
    J = bigquery.LoadJobConfig
    bq.load_table_from_json(states, T_STATE, job_config=J(write_disposition="WRITE_TRUNCATE")).result()
    if pending_rows:
        bq.load_table_from_json(pending_rows, T_PEND, job_config=J(write_disposition="WRITE_TRUNCATE")).result()
    for rows, t in ((log_rows, T_SLOG), (plan_rows, T_PLAN), (pd_rows, T_PDW)):
        if rows:
            bq.load_table_from_json(rows, t, job_config=J(write_disposition="WRITE_APPEND")).result()
    disagree = S.template_disagreements(tmap)
    kinds = {}
    acts = [r for r in pd_rows if r["object"] == "activity"]
    for r in acts:
        k = r["pd_hold_reason"] or r["target_kind"]
        kinds[k] = kinds.get(k, 0) + 1
    built_age_h = None if rung_built_at is None else round(
        (dt.datetime.now(dt.timezone.utc) - rung_built_at).total_seconds() / 3600, 1)
    bq.load_table_from_json([{
        "run_id": RUN_ID, "plan_date": today.isoformat(), "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "people": len(states), "would_send": sum(r["would_send"] for r in plan_rows), "pd_would_writes": len(acts),
        "pd_targets": [{"kind": k, "n": v} for k, v in sorted(kinds.items())],
        "rung_price_source": RUNG_PRICE_SOURCE,
        "rung_price_built_at": rung_built_at and rung_built_at.isoformat(), "rung_price_age_h": built_age_h,
        "rung_price_stale": built_age_h is None or built_age_h > 26,
        "pd_persons_ingested_at": persons_ingested_at and persons_ingested_at.isoformat(),
        "pd_persons_age_h": persons_age_h,
        "pending_open": pstats["pending_open"], "pending_new": pstats["pending_new"],
        "pending_resolved": pstats["pending_resolved"], "pending_oldest_h": pstats["pending_oldest_h"],
        "pending_by_reason": [{"reason": k, "n": v} for k, v in pstats["pending_by_reason"].items()],
        "writeback_population": [{"kind": k, "n": v} for k, v in sorted(pop.items())]}], T_RUN, job_config=J(write_disposition="WRITE_APPEND")).result()
    log.info("SHADOW_DONE run=%s people=%s plan_rows=%s would_send=%s pd_would_writes=%s pd_targets=%s "
             "rung_price_built_at=%s rung_price_age_h=%s pd_persons_age_h=%s pd_pending=%s writeback_population=%s state_changes=%s "
             "interface_v1_vs_map_disagreements=%s", RUN_ID, len(states), len(plan_rows),
             sum(r["would_send"] for r in plan_rows), len(acts), kinds, rung_built_at, built_age_h,
             persons_age_h, pstats, pop, len(log_rows), disagree)


def _no_pd_writer(record):
    raise RuntimeError("shadow job reached the Pipedrive writer - refused")


if __name__ == "__main__":
    main()
