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
import re
import uuid

from google.cloud import bigquery

import pd_pending
import pd_record
import pd_target
import pd_writeback as W
import json
import presend as G
import selfcheck as SC
import sequence as S

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sequence_job")

P = "jaunais-za-aizv04022026"
# "Build into temp, then swap" (Raivis' standing order): SHADOW_TABLE_SUFFIX=_tmp4 makes a whole run write to
# copies of the eight tables this job writes (clones made beforehand) - nothing the daily run owns is touched.
SFX = os.environ.get("SHADOW_TABLE_SUFFIX", "")
T_STATE = f"{P}.mkt_control.contact_sequence_state{SFX}"
T_SLOG = f"{P}.mkt_control.contact_sequence_log{SFX}"
T_PLAN = f"{P}.mkt_control.shadow_send_plan{SFX}"
T_PDW = f"{P}.mkt_control.shadow_pd_writes{SFX}"
T_RUN = f"{P}.mkt_control.shadow_run_report{SFX}"
T_OVERRIDE = f"{P}.mkt_control.pd_target_override"
T_PEND = f"{P}.mkt_control.pd_write_pending{SFX}"
T_AKCIJA = f"{P}.mkt_control.shadow_akcija_audience{SFX}"
T_CHECK = f"{P}.mkt_control.shadow_selfcheck{SFX}"
T_OFFERED = f"{P}.mkt_control.xsell_offered"
def _bundle_id():
    """The commit the running bundle was built from: file BUNDLE next to the code (written at bundle build)."""
    try:
        return open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "BUNDLE")).read().strip()
    except OSError:
        return "unknown"


BUNDLE = os.environ.get("ENGINE_BUNDLE") or _bundle_id()
# INTERFACE email_type v2: template per letter. The map row (Vēstuļu šabloni's table) wins where it has an id;
# where it has none the engine config speaks, so the shadow plan is complete before the map is filled.
# ENGINE_TEMPLATES_JSON='{"winback_1_e2": 251}' replaces a provisional id the day MAIN names the real one.
TEMPLATES = {**S.INTERFACE_V2, **json.loads(os.environ.get("ENGINE_TEMPLATES_JSON") or "{}")}
# v2.8.2 A7: a price table older than 26 h = no price that day. Env only for a labelled WHAT-IF run into temp
# tables (e.g. the day after a failed night chain); the scheduled job never sets it.
PRICE_MAX_AGE_H = int(os.environ.get("PRICE_MAX_AGE_H", "26"))
RUN_ID = os.environ.get("CLOUD_RUN_EXECUTION") or f"local-{uuid.uuid4().hex[:12]}"
HORIZON_DAYS = int(os.environ.get("HORIZON_DAYS", "7"))
LADDER_POLICY = ("ladder-policy-v1 (Raivis 2026-09-28, contract 326480dce080) + " + S.CADENCE +
                 " + post-purchase-v1.2 + gates (MAIN 2026-10-05 16:10, contract 682ad015f0ab)")
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
  AND built_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {PRICE_MAX_AGE_H} HOUR)   -- v2.8.2 A7: stale = no price
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


# G15.2 (MAIN 2026-10-01 16:55, contract sha 73e8f700b2e2): the daily planner only REPORTS G15, from the LATEST
# AVAILABLE goods run (any plan_date - at 08:05 today's run does not exist yet); it never changes would_send or a hold.
# The hard gate (G15.1: latest run of the SEND date) is send_path L7. Writer = Nakts sinhronizācija.
# G15 source (Sūtīšanas dzinējs 5, 2026-10-06): TODAY's mkt_control.letter_fields row of the letter (rung +
# g15_zero_priced), read through presend.row_goods. The old "latest run" of the goods table had no plan_date
# filter and read YESTERDAY's rows at 08:05 - a fallback WO2 forbids. That table is not read any more.


# G-EN (Raivis 2026-09-30 17:47): EN contacts get no LV engine letter until EN exists. EN = Brevo list 46
# (business_marts.brevo_contacts_snapshot, nightly copy of Brevo list membership) OR LANGUAGE = 'en'
# (business_marts.marketing_brevo_payload, the view the nightly sync writes to the Brevo LANGUAGE attribute), on
# ANY address of the person (customer_identity). Measured 01.10: 624 addresses (list 46 609, LANGUAGE en 622).
EN_LIST_ID = 46
EN_ADDR_SQL = f"""
SELECT LOWER(TRIM(email)) AS e FROM `{P}.business_marts.brevo_contacts_snapshot` WHERE {EN_LIST_ID} IN UNNEST(list_ids)
UNION DISTINCT
SELECT LOWER(TRIM(email)) FROM `{P}.business_marts.marketing_brevo_payload` WHERE LOWER(TRIM(LANGUAGE)) = 'en'
"""
EN_SQL = f"""SELECT DISTINCT i.master_key FROM `{P}.business_marts.customer_identity` i
JOIN ({EN_ADDR_SQL}) en ON en.e = i.email_norm WHERE i.master_key IS NOT NULL"""
EN_SOURCE_SQL = f"""SELECT (SELECT COUNT(*) FROM ({EN_ADDR_SQL})) AS en_addresses,
  (SELECT TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), TIMESTAMP_MILLIS(last_modified_time), MINUTE) / 60.0
   FROM `{P}.business_marts.__TABLES__` WHERE table_id = 'brevo_contacts_snapshot') AS list_snapshot_age_h"""


# ---------------------------------------------------------------------------------------------------------------
# Sūtīšanas dzinējs 4 (MAIN 2026-10-05 16:10): inputs of the complete LV plan. All read-only.

# B2B / LEAD guard. TWO sources, either one is enough:
#   (1) legacy_paytraq.b2b_shop_flow_classification_v2 (Data & analytics 2, rule v2): flow B2B | LEAD, keyed
#       cid:<Paytraq client id> (+ its e-mail);
#   (2) Pipedrive ORGANISATION field 309 "MKT Plūsma" (key 0366…0115) = option 716 "B2B", read from the nightly
#       mirror channel_raw.pipedrive_orgs (so a mark set in Pipedrive today is seen tomorrow), reaching the person
#       through the organisation's persons and through the organisation's Paytraq client id.
F309_KEY = "036687330a0d889920e7166c94392ca6238c0115"
F309_B2B = "716"
FLOW_SQL = f"""
WITH idn AS (SELECT DISTINCT client_id, email_norm, master_key FROM `{P}.business_marts.customer_identity`
             WHERE master_key IS NOT NULL),
cls AS (SELECT REGEXP_EXTRACT(customer_key, r'^cid:(.+)$') AS cid, LOWER(TRIM(email)) AS email, flow
        FROM `{P}.legacy_paytraq.b2b_shop_flow_classification_v2` WHERE flow IN ('B2B', 'LEAD')),
o309 AS (SELECT id AS org_id, paytraq_client_id FROM `{P}.channel_raw.pipedrive_orgs`
         WHERE REGEXP_EXTRACT(raw_json, r'"{F309_KEY}":\\s*"?(\\d+)') = '{F309_B2B}'),
a AS (
  SELECT idn.master_key, cls.flow, 'classification_v2' AS src FROM cls JOIN idn ON idn.client_id = cls.cid
  UNION ALL
  SELECT idn.master_key, cls.flow, 'classification_v2' FROM cls JOIN idn ON idn.email_norm = cls.email
  WHERE IFNULL(cls.email, '') != ''
  UNION ALL
  SELECT idn.master_key, 'B2B', 'pd_field_309' FROM o309
  JOIN `{P}.channel_raw.pipedrive_persons` p ON p.org_id = o309.org_id,
       UNNEST(SPLIT(IFNULL(p.email_all, ''), ',')) e
  JOIN idn ON idn.email_norm = LOWER(TRIM(e))
  UNION ALL
  SELECT idn.master_key, 'B2B', 'pd_field_309' FROM o309 JOIN idn ON idn.client_id = o309.paytraq_client_id)
SELECT master_key, IF(LOGICAL_OR(flow = 'B2B'), 'B2B', 'LEAD') AS flow, STRING_AGG(DISTINCT src ORDER BY src) AS src
FROM a GROUP BY 1
"""
FLOW_SRC_SQL = f"""SELECT
  (SELECT MAX(built_at) FROM `{P}.legacy_paytraq.b2b_shop_flow_classification_v2`) AS classification_built_at,
  (SELECT COUNTIF(flow = 'B2B') FROM `{P}.legacy_paytraq.b2b_shop_flow_classification_v2`) AS classification_b2b,
  (SELECT COUNTIF(flow = 'LEAD') FROM `{P}.legacy_paytraq.b2b_shop_flow_classification_v2`) AS classification_lead,
  (SELECT MAX(ingested_at) FROM `{P}.channel_raw.pipedrive_orgs`) AS pd_orgs_ingested_at,
  (SELECT COUNT(*) FROM `{P}.channel_raw.pipedrive_orgs`
   WHERE REGEXP_EXTRACT(raw_json, r'"{F309_KEY}":\\s*"?(\\d+)') = '{F309_B2B}') AS pd_orgs_309_b2b"""

# POST-PURCHASE: the shop order of the contact's latest purchase = the latest deal of the delivery pipeline (P6,
# pipeline_id 6; deal title = the order number the customer knows, e.g. M-860325-34895). Handed to the courier =
# the parcel was staged (business_marts.parcel_watch.staged_at, keyed by order number or deal id) - the moment the
# deal enters stage 62; picked up = stage 68 won (won_time); last resort = the stage-change time of a deal that
# is already in a courier stage. Lost / deleted deals are not orders.
PP_SQL = f"""
WITH idn AS (SELECT DISTINCT email_norm, master_key FROM `{P}.business_marts.customer_identity`
             WHERE master_key IS NOT NULL AND email_norm IS NOT NULL),
d AS (SELECT id, title, person_id, DATE(add_time, 'Europe/Riga') AS order_on, stage_id, status, won_time,
             stage_change_time
      FROM `{P}.channel_raw.pipedrive_deals`
      WHERE pipeline_id = 6 AND NOT IFNULL(is_deleted, FALSE) AND status != 'lost'
        AND add_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 180 DAY)),
pw AS (SELECT order_ref, MIN(staged_at) AS staged, ANY_VALUE(LOWER(TRIM(email))) AS email
       FROM `{P}.business_marts.parcel_watch` WHERE order_ref IS NOT NULL GROUP BY 1),
x AS (SELECT d.id, d.title, d.order_on, d.person_id, COALESCE(p1.email, p2.email) AS parcel_email,
        COALESCE(DATE(p1.staged, 'Europe/Riga'), DATE(p2.staged, 'Europe/Riga'),
                 IF(d.stage_id = 68 AND d.status = 'won', DATE(d.won_time, 'Europe/Riga'), NULL),
                 IF(d.stage_id IN (62, 63, 64, 65, 66), DATE(d.stage_change_time, 'Europe/Riga'), NULL)) AS ship_on,
        CASE WHEN p1.staged IS NOT NULL OR p2.staged IS NOT NULL THEN 'parcel_staged'
             WHEN d.stage_id = 68 AND d.status = 'won' THEN 'picked_up'
             WHEN d.stage_id IN (62, 63, 64, 65, 66) THEN 'stage_change' END AS ship_src
      FROM d LEFT JOIN pw p1 ON p1.order_ref = d.title LEFT JOIN pw p2 ON p2.order_ref = CAST(d.id AS STRING)),
em AS (SELECT x.id, LOWER(TRIM(e)) AS email FROM x
       JOIN `{P}.channel_raw.pipedrive_persons` p ON p.id = x.person_id,
            UNNEST(SPLIT(IFNULL(p.email_all, ''), ',')) e WHERE TRIM(e) NOT IN ('', 'nan')
       UNION DISTINCT SELECT id, parcel_email FROM x WHERE parcel_email IS NOT NULL),
m AS (SELECT DISTINCT em.id, idn.master_key FROM em JOIN idn ON idn.email_norm = em.email)
SELECT m.master_key,
       ARRAY_AGG(STRUCT(x.title AS order_nr, x.order_on, x.ship_on, x.ship_src)
                 ORDER BY x.order_on DESC, x.id DESC LIMIT 1)[OFFSET(0)] AS o
FROM m JOIN x USING (id) GROUP BY 1
"""
PP_ORDER_MATCH_DAYS = 7      # the P6 deal belongs to THIS purchase when it is not older than last_order - 7 d
ORDER_NR_OK = r"[A-Z0-9 _./-]{1,40}"     # FS8: what anketa_url() accepts as an order number (upper-cased)

# LQ/XS PRICE SOURCE v1 (PS1-PS6): lost_quarterly and the 235 intro prices come ONLY from this view. A row counts
# when it is a real saving (lost: P2, > 5 % below the shop; intro: below the shop).
T_LQXS = f"{P}.business_marts.pap_lqxs_current_v281"
LQXS_SQL = f"""
SELECT master_key,
  MAX(IF(email_type = 'lost_quarterly' AND price_role IN ('lq1_floor', 'lq2_minus13')
         AND price < 0.95 * shop_gross, valid_until, NULL)) AS vu_lost,
  MAX(IF(email_type = 'lost_quarterly' AND price_role = 'lq6_capped_r1'
         AND price < 0.95 * shop_gross, valid_until, NULL)) AS vu_lost_capped,
  ARRAY_AGG(DISTINCT IF(email_type = 'active_xsell' AND price_role = 'xs_intro' AND price < shop_gross,
                        handle, NULL) IGNORE NULLS) AS xs_handles,
  MAX(IF(email_type = 'active_xsell' AND price_role = 'xs_intro', valid_until, NULL)) AS vu_xs
FROM `{T_LQXS}` WHERE master_key IS NOT NULL AND shop_gross > 0 GROUP BY 1
"""
LQXS_BUILT_SQL = f"SELECT MAX(built_at) AS built_at FROM `{T_LQXS}`"

# R goods: the letter's R1..R4 = business_marts.attrs_slots kind 'R' (R-CAB C1: the ONE source the Brevo writer and
# the cabinet both read). The cabinet does not show a product that is hidden or out of stock (C3) - so "what the
# cabinet shows" = the same rows that are sellable with stock in business_marts.shop_sellable_product.
R_SQL = f"""
SELECT LOWER(TRIM(a.email)) AS email, ARRAY_AGG(a.handle ORDER BY a.slot) AS r,
       ARRAY_AGG(IF(s.handle IS NOT NULL, a.handle, NULL) IGNORE NULLS ORDER BY a.slot) AS r_cab
FROM `{P}.business_marts.attrs_slots` a
LEFT JOIN (SELECT handle FROM `{P}.business_marts.shop_sellable_product` GROUP BY 1 HAVING MAX(stock_qty) > 0) s
  USING (handle)
WHERE a.kind = 'R' AND a.handle IS NOT NULL GROUP BY 1
"""
OFFERED_SQL = f"SELECT master_key, ARRAY_AGG(DISTINCT handle) AS handles FROM `{T_OFFERED}` GROUP BY 1"
APPROVAL_SQL = f"SELECT template_id, email_type FROM `{P}.mkt_control.template_approval` WHERE approved"

# WRITER OUTPUT v1 (contract 7ca13f671703, WO1 / WO2): the ONLY per-letter field source is mkt_control.letter_fields,
# TODAY's plan_date only. No row for today = the letter is held (presend gate NO_LETTER_FIELDS_ROW); there is no
# fallback to an older plan_date and no other table. The scheduled plan (08:05) runs BEFORE the writer (08:40), so in
# the 08:05 plan every would_send row carries that gate; send_path L8 evaluates the same gate again at send time.
LF_SQL = f"""
SELECT * FROM `{P}.mkt_control.letter_fields`
WHERE plan_date = CURRENT_DATE('Europe/Riga')
QUALIFY ROW_NUMBER() OVER (PARTITION BY email ORDER BY built_at DESC) = 1
"""


def lf_slots(row) -> list:
    """P1..P8 of a letter_fields row as [{name, price, ref}] - the texts the letter carries."""
    return [{"name": row[f"P{i}_NAME"], "price": row[f"P{i}_PRICE"], "ref": row[f"P{i}_REF_PRICE"]}
            for i in range(1, 9)] if row else []


def letter_row(rows: dict, email, email_type):
    """Today's letter_fields row of this address FOR THIS LETTER, or None (= held). A row written for another
    letter of the same contact is not this letter's field set."""
    r = rows.get((email or "").strip().lower())
    return r if r and r["email_type"] == email_type else None


SUPPRESSED_CHECK_SQL = f"""
SELECT COUNT(DISTINCT IF(LOWER(TRIM(s.email)) = LOWER(TRIM(p.email)), p.master_key, NULL)) AS send_address,
       COUNT(DISTINCT p.master_key) AS any_address
FROM `{T_PLAN}` p
JOIN `{P}.business_marts.customer_identity` i ON i.master_key = p.master_key
JOIN `{P}.business_marts.email_suppression_all` s
  ON LOWER(TRIM(s.email)) IN (i.email_norm, LOWER(TRIM(p.email)))
WHERE p.plan_date = CURRENT_DATE() AND p.run_id = @run AND p.would_send
"""
PREV_COUNTS_SQL = f"""
SELECT email_type, would_send, hold_reason, COUNT(*) AS n FROM `{T_PLAN}`
WHERE plan_date = (SELECT MAX(plan_date) FROM `{T_PLAN}` WHERE plan_date < CURRENT_DATE()) GROUP BY 1, 2, 3"""


_ORDER_NR = re.compile(r"^(M-\d+-\d+|PAP-\d+-\d+|ALE \d+|PAS/\d+/\d+|PR/\d+/\d+)")


def order_nr_of(title):
    """The order number as the customer knows it, from a P6 deal title. Measured 2026-10-05: once the Paytraq
    document exists the title becomes '<order> _ <document>' ('M-860325-35060 _ ALE 2605715'), and notes are
    appended ('ALE 2605233 COD', '... (testa)'). The FIRST part is the customer's number; never the whole title."""
    t = (title or "").strip()
    m = _ORDER_NR.match(t)
    if m:
        return m.group(1)
    return t.split(" _ ")[0].strip() or None


def pp_facts(o, last_order):
    """(order_nr, order_on, ship_on) of the P6 order when it belongs to THIS purchase, else (None, None, None)."""
    if not o or o.get("order_on") is None:
        return None, None, None
    order_on = _d(o["order_on"])
    if last_order is not None and order_on < last_order - dt.timedelta(days=PP_ORDER_MATCH_DAYS):
        return None, None, None
    return order_nr_of(o.get("order_nr")), order_on, _d(o.get("ship_on"))


def order_nr_usable(nr) -> bool:
    return bool(nr) and re.fullmatch(ORDER_NR_OK, nr.upper()) is not None


def last_sent(history_rows, email_type):
    ds = [_d(h["sent_on"]) for h in history_rows if h["email_type"] == email_type]
    return max(ds) if ds else None


def akcija_week(today):
    """The next weekly akcija: Tuesday on/after today, its ISO week label and the Monday..Sunday it owns."""
    tue = today + dt.timedelta(days=(1 - today.weekday()) % 7)
    iso = tue.isocalendar()
    return tue, f"{iso[0]}-W{iso[1]:02d}", tue - dt.timedelta(days=1), tue + dt.timedelta(days=5)


def akcija_row(*, mk, email, stage, suppressed, flow, is_en, plan, week):
    """'Personal letter OR akcija, never both' (one sales letter per week). plan = that contact's plan row."""
    tue, label, mon, sun = week
    reason = None
    if suppressed:
        reason = "SUPPRESSED"
    elif stage in (None, "blocked"):
        reason = "BLOCKED_OR_UNKNOWN"
    elif flow in S.FLOW_HOLD:
        reason = S.FLOW_HOLD[flow]
    elif is_en:
        reason = S.HOLD_EN                       # G-EN: no LV letter, the LV akcija included
    elif plan and plan.get("would_deliver") and plan["email_type"] in S.SALES_TYPES and plan["planned_send_date"] \
            and mon.isoformat() <= plan["planned_send_date"] <= sun.isoformat():
        reason = "PERSONAL_LETTER_THIS_WEEK"
    personal = reason == "PERSONAL_LETTER_THIS_WEEK"
    return {"offer_week": label, "send_date": tue.isoformat(), "master_key": mk, "email": email,
            "in_audience": reason is None, "excluded_reason": reason,
            "personal_email_type": plan["email_type"] if personal else None,
            "personal_send_date": plan["planned_send_date"] if personal else None}


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


def plan_template(d, tmap, templates=None):
    """(template id, hold, source). The id for the EXACT email_type or nothing - never another letter's template.
    source: 'map' (mkt_control.email_template_map has an id) | 'config' (INTERFACE v2) | 'config_provisional'
    (9180 / 9232 / 9233 - the pre-send gate TEMPLATE_PROVISIONAL keeps such a letter from being delivered)."""
    templates = S.INTERFACE_V2 if templates is None else templates
    et = d.next_email_type
    if not et:
        return None, d.hold_reason, None
    tid, src = (tmap[et], "map") if tmap.get(et) is not None else (templates.get(et), "config")
    if tid is None:
        return None, d.hold_reason or "NO_TEMPLATE", None
    if tid in S.PROVISIONAL_TEMPLATE_IDS:
        src = "config_provisional"
    return tid, d.hold_reason, src


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


def _int(v):
    """contact_sequence_state.rung_cap is a STRING column (DDL 28.09): "1" must come back as 1, or L6/LQ6 never match."""
    return None if v in (None, "") else int(v)


def _d(v):
    return None if v is None else (v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v)[:10]))


# DW3 / DW4: 06:00 sync -> 08:05 plan -> 08:40 letter writer -> send window 09:00-23:00. The plan is not re-run
# after the writer ran on a send day. This job REFUSES to rebuild today's plan once mkt_control.letter_fields_log
# holds an OK row for today (the writer has already copied the dates), unless ALLOW_REPLAN_AFTER_WRITER=1 is set on
# that one execution - and then the run report says so (replanned_after_writer) and send_path lock L10 refuses every
# send until the writer has run again on the new plan. Temp runs (SHADOW_TABLE_SUFFIX) never touch the real plan.
WRITER_LOG_SQL = f"""SELECT run_id, finished_at, JSON_VALUE(counts, '$.plan_run_id') AS plan_run_id
FROM `{P}.mkt_control.letter_fields_log` WHERE plan_date = CURRENT_DATE() AND status = 'OK'
ORDER BY finished_at DESC LIMIT 1"""


class ReplanRefused(RuntimeError):
    pass


def replan_guard(writer_row, allow: bool, temp: bool) -> bool:
    """-> replanned_after_writer. Raises ReplanRefused when the writer already ran today and no override is set."""
    if writer_row is None or temp:
        return False
    if not allow:
        raise ReplanRefused(
            f"DW4: the letter writer already ran today ({writer_row['run_id']}, plan {writer_row['plan_run_id']}) - "
            "the plan is not rebuilt after it on a send day. Set ALLOW_REPLAN_AFTER_WRITER=1 on this execution only "
            "if the writer will run again before any send.")
    return True


def main():
    bq = bigquery.Client(project=P)
    replanned = replan_guard(next(iter(bq.query(WRITER_LOG_SQL).result()), None),
                             os.environ.get("ALLOW_REPLAN_AFTER_WRITER") == "1", bool(SFX))
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
    age = next(iter(bq.query(PERSONS_AGE_SQL).result()))
    persons_age_h, persons_ingested_at = age["age_h"], age["ingested_at"]
    org_by_address = {r["email"]: set(r["org_ids"]) for r in bq.query(ORG_ADDR_SQL).result()}
    master_addr = {r["master_key"]: list(r["emails"]) for r in bq.query(MASTER_ADDR_SQL).result()}
    overrides = {r["email"]: (r["person_id"], r["org_id"]) for r in bq.query(OVERRIDE_SQL).result()}
    flows = {r["master_key"]: (r["flow"], r["src"]) for r in bq.query(FLOW_SQL).result()}
    flow_src = dict(next(iter(bq.query(FLOW_SRC_SQL).result())))
    if not flow_src["classification_b2b"]:
        raise RuntimeError("B2B guard source empty - refusing to plan without the B2B / LEAD guard")
    pp_orders = {r["master_key"]: dict(r["o"]) for r in bq.query(PP_SQL).result()}
    lqxs = {r["master_key"]: r for r in bq.query(LQXS_SQL).result()}
    lqxs_built_at = next(iter(bq.query(LQXS_BUILT_SQL).result()))["built_at"]
    r_goods = {r["email"]: (tuple(r["r"]), tuple(r["r_cab"])) for r in bq.query(R_SQL).result()}
    offered = {r["master_key"]: frozenset(r["handles"]) for r in bq.query(OFFERED_SQL).result()}
    approved = {(r["template_id"], r["email_type"]) for r in bq.query(APPROVAL_SQL).result()}
    lf_rows = {r["email"]: dict(r) for r in bq.query(LF_SQL).result()}
    no_lf = {}
    age_h = lambda t: None if t is None else round(  # noqa: E731
        (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 3600, 1)
    rung_stale = age_h(rung_built_at) is None or age_h(rung_built_at) > PRICE_MAX_AGE_H
    lqxs_stale = age_h(lqxs_built_at) is None or age_h(lqxs_built_at) > PRICE_MAX_AGE_H
    week = akcija_week(today)
    lf_run = next((r["run_id"] for r in lf_rows.values()), None)      # None before the 08:40 writer
    goods_run = {"run_id": lf_run, "plan_date": today if lf_run else None, "built_at": None}
    en_masters = {r["master_key"] for r in bq.query(EN_SQL).result()}
    en_src = next(iter(bq.query(EN_SOURCE_SQL).result()))
    if not en_masters or not en_src["en_addresses"]:
        raise RuntimeError("G-EN source empty - refusing to plan LV letters without the EN guard")

    states, log_rows, plan_rows, pd_rows, held_today, akcija_rows = [], [], [], [], [], []
    g15_report, basis = {}, {"xs4": "letter_fields", "anketa": "letter_fields", "fields": "letter_fields today only",
                         "g15": "letter_fields today only", "counts_at": "send time (send_lookups)"}

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
            _int(p.get("rung_cap")), _d(p.get("rung_cap_until")), _d(p.get("reorder_worked_at")))
        st, src = S.apply_history(st, history.get(mk, []))
        # L6 + L8 recomputed from the full counted history + paid orders; never loosened by a recompute
        cap, cap_until, worked = S.ladder_marks(
            [{"email_type": h["email_type"], "rung": h["rung"], "sent_on": _d(h["sent_on"])} for h in history.get(mk, [])],
            orders.get(mk, []))
        if cap_until and (st.rung_cap_until is None or cap_until > st.rung_cap_until):
            st.rung_cap, st.rung_cap_until = cap, cap_until
        if worked and (st.reorder_worked_at is None or worked > st.reorder_worked_at):
            st.reorder_worked_at = worked
        lx = lqxs.get(mk)
        pr = dict(prices.get(mk) or {})
        if lx is not None and not lqxs_stale:                          # PS1: lost prices come only from the LQ/XS view
            if lx["vu_lost"] is not None:
                pr[S.LOST_OFFER_RUNG] = _d(lx["vu_lost"])
            if lx["vu_lost_capped"] is not None:
                pr["4c"] = _d(lx["vu_lost_capped"])
        hist = history.get(mk, [])
        pp_nr, pp_on, pp_ship = pp_facts(pp_orders.get(mk), _d(f["last_order"]))
        d = S.advance(st, S.Facts(f["lifecycle_stage"], _d(f["last_order"]), _d(f["first_order"]),
                                  bool(f["suppressed"]), pr or None, f["entry_threshold_days"],
                                  pp_nr, pp_on, pp_ship, last_sent(hist, S.PP1), last_sent(hist, S.XSELL)), today)
        tid, hold, tsrc = plan_template(d, tmap, TEMPLATES)
        # G15.2 (contract 73e8f700b2e2): the planner only REPORTS G15; the hard gate is send_path L7 (pre-send).
        w0 = letter_row(lf_rows, f["send_email"], d.next_email_type)
        g15 = S.goods_hold(d.next_email_type, d.offer_rung, hold, G.row_goods(w0)) if G.row_goods(w0) else hold
        if g15 in (S.HOLD_NO_PRICED, S.HOLD_NO_SLOT_ROW) and g15 != hold:
            g15_report[g15] = g15_report.get(g15, 0) + 1
        shop_order = pp_orders.get(mk)
        hold = S.recent_order_hold(d.next_email_type, hold, shop_order and _d(shop_order.get("order_on")),
                                   _d(f["last_order"]))                      # K6 / L5 on orders the stage cannot see yet
        flow, flow_from = flows.get(mk, (None, None))
        hold = S.flow_hold(d.next_email_type, hold, flow)                            # B2B / LEAD guard
        hold = S.language_hold(d.next_email_type, hold, mk in en_masters)            # G-EN
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
        # PRE-SEND GATES, evaluated on the newest data that exists now; send_path L8 re-evaluates at send time.
        et = d.next_email_type
        gate_list = []
        if would:
            r, r_cab = r_goods.get(f["send_email"], ((), ()))
            w = letter_row(lf_rows, f["send_email"], et)             # WO1 / WO2: today's row or the letter is held
            if w is None:
                no_lf[et] = no_lf.get(et, 0) + 1
            xvu = d.xsell_valid_until
            # K12 / XS2: the intro price must hold to send + 13
            xs_holds = not (et == S.XSELL and lx is not None and d.next_due_on <= today and (
                lx["vu_xs"] is None or _d(lx["vu_xs"]) < xvu))
            # ONE builder for the planner and the send path (presend.build_ctx); WO3 order match is inside it
            gate_list = G.gates(et, d.offer_rung, G.build_ctx(
                email_type=et, template_id=tid, template_approved=(tid, et) in approved,
                offer_valid_until=d.offer_valid_until, xsell_valid_until=xvu, has_price=d.has_price,
                price_stale=lqxs_stale if et in (S.LOST, S.XSELL) else rung_stale, row=w,
                trigger_order_nr=d.trigger_order_nr, r_handles=r, r_cabinet=r_cab,
                xsell_offered=offered.get(mk, frozenset()), xs_price_holds=xs_holds))
        lp = last_plan.get(mk)
        key = (d.next_email_type, due_token(d.next_due_on, today), would, d.offer_rung)
        prev_key = lp and (lp["email_type"], due_token(_d(lp["planned_send_date"]), _d(lp["plan_date"])),
                           lp["would_send"], lp["offer_rung"])
        plan_rows.append({
            "plan_date": today.isoformat(), "run_id": RUN_ID, "master_key": mk, "email": f["send_email"],
            "track": s.track, "step": s.step + 1 if d.next_email_type else s.step,
            "email_type": d.next_email_type, "template_id": tid,
            "offer_rung": d.offer_rung, "planned_send_date": d.next_due_on and d.next_due_on.isoformat(),
            # DW1 (contract 9d7c6584cc16): EVERY planned priced letter carries its date - held rows too
            "offer_valid_until": d.offer_valid_until and d.offer_valid_until.isoformat(),
            "would_send": would, "hold_reason": hold, "reason": d.reason,
            "lost_capped": d.lost_capped,                                   # LQ6, read by the writer
            "interface_template_id": TEMPLATES.get(et), "template_source": tsrc,
            "flow": flow, "flow_source": flow_from,
            "trigger_order_nr": d.trigger_order_nr,                         # 244: read by the writer (ANKETA_URL / ORDER_NR)
            "xsell_valid_until": d.xsell_valid_until and d.xsell_valid_until.isoformat(),
            "presend_gate": gate_list[0] if gate_list else None,
            "presend_gates": ",".join(gate_list) or None,
            "would_deliver": would and not gate_list,
            "diff_vs_prev": "new" if lp is None else ("same" if key == prev_key else "changed"),
            "planned_at": now})
        akcija_rows.append({"plan_date": today.isoformat(), "run_id": RUN_ID, "planned_at": now, **akcija_row(
            mk=mk, email=f["send_email"], stage=f["lifecycle_stage"], suppressed=bool(f["suppressed"]), flow=flow,
            is_en=mk in en_masters, plan=plan_rows[-1], week=week)})
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
            tail, lines = W.offer_summary(lf_slots(letter_row(lf_rows, f["send_email"], d.next_email_type)))
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
    for t in (T_PLAN, T_PDW, T_AKCIJA):
        bq.query(f"DELETE FROM `{t}` WHERE plan_date = CURRENT_DATE()").result()
    # the marker of a dash row already posted today survives a re-run (never two dash rows a day)
    bq.query(f"DELETE FROM `{T_CHECK}` WHERE plan_date = CURRENT_DATE() AND check_name != '_dash_row_posted'").result()
    J = bigquery.LoadJobConfig
    bq.load_table_from_json(states, T_STATE, job_config=J(write_disposition="WRITE_TRUNCATE")).result()
    if pending_rows:
        bq.load_table_from_json(pending_rows, T_PEND, job_config=J(write_disposition="WRITE_TRUNCATE")).result()
    for rows, t in ((log_rows, T_SLOG), (plan_rows, T_PLAN), (pd_rows, T_PDW), (akcija_rows, T_AKCIJA)):
        if rows:
            bq.load_table_from_json(rows, t, job_config=J(write_disposition="WRITE_APPEND")).result()
    disagree = S.template_disagreements(tmap)
    # DAILY SELF-CHECK (MAIN 2026-10-05 16:10 item 5), written with the plan. Hard failures -> one dash row (posted
    # by the small job tiktik-shadow-selfcheck-dash on the ops service account; this job has no Drive access).
    sup = next(iter(bq.query(SUPPRESSED_CHECK_SQL, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("run", "STRING", RUN_ID)])).result()))
    prev_counts = [dict(r) for r in bq.query(PREV_COUNTS_SQL).result()]
    checks = SC.run(plan_rows, akcija_rows, prev_counts=prev_counts, flows={k: v[0] for k, v in flows.items()},
                    en_masters=en_masters, suppressed_send_address=sup["send_address"],
                    suppressed_any_address=sup["any_address"], today=today,
                    ages_h={"rung_price": age_h(rung_built_at), "lqxs_price": age_h(lqxs_built_at),
                            "pd_persons": persons_age_h, "en_list_snapshot": en_src["list_snapshot_age_h"],
                            "flow_classification": age_h(flow_src["classification_built_at"]),
                            "pd_orgs_mirror": age_h(flow_src["pd_orgs_ingested_at"]),
                            "goods_run_days": None if goods_run["plan_date"] is None
                            else (today - _d(goods_run["plan_date"])).days},
                    no_letter_fields=no_lf, map_disagreements=disagree)
    bq.load_table_from_json([{"plan_date": today.isoformat(), "run_id": RUN_ID, "checked_at": now, **c}
                             for c in checks], T_CHECK, job_config=J(write_disposition="WRITE_APPEND")).result()
    failed = [c["check_name"] for c in checks if c["level"] == "hard" and not c["ok"]]
    cnt = lambda key, rows: json.dumps(SC.count_by(rows, key), ensure_ascii=False, sort_keys=True)  # noqa: E731
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
        "writeback_population": [{"kind": k, "n": v} for k, v in sorted(pop.items())],
        "en_pending": sum(r["hold_reason"] == S.HOLD_EN for r in plan_rows),
        "lost_capped": sum(bool(r.get("lost_capped")) for r in plan_rows),
        "pd_rung_option_missing": sum(1 for r in pd_rows if r.get("object") == "person_field_skipped"),
        # G15.2 report-only: the hard-hold columns g15_no_priced_slots / g15_no_slot_row stay NULL from here on
        "g15_would_be_no_priced": g15_report.get(S.HOLD_NO_PRICED, 0),
        "g15_would_be_no_slot_row": g15_report.get(S.HOLD_NO_SLOT_ROW, 0),
        "g15_goods_run_id": goods_run["run_id"],
        "g15_goods_plan_date": goods_run["plan_date"] and str(goods_run["plan_date"]),
        "en_masters": len(en_masters), "en_addresses": en_src["en_addresses"],
        "en_list_snapshot_age_h": en_src["list_snapshot_age_h"],
        # Sūtīšanas dzinējs 4
        "replanned_after_writer": replanned,
        "bundle": BUNDLE, "would_deliver": sum(bool(r.get("would_deliver")) for r in plan_rows),
        "holds_json": cnt(("email_type", "hold_reason"), [r for r in plan_rows if not r["would_send"]]),
        "gates_json": cnt(("email_type", "presend_gate"), [r for r in plan_rows if r["would_send"]]),
        "gate_basis": json.dumps(basis, sort_keys=True), "writer_fields_missing": json.dumps({"no_letter_fields_row_today": no_lf}, sort_keys=True) if no_lf else None,
        "flow_b2b": sum(v[0] == "B2B" for v in flows.values()), "flow_lead": sum(v[0] == "LEAD" for v in flows.values()),
        "flow_classification_built_at": flow_src["classification_built_at"] and flow_src["classification_built_at"].isoformat(),
        "flow_pd_orgs_ingested_at": flow_src["pd_orgs_ingested_at"] and flow_src["pd_orgs_ingested_at"].isoformat(),
        "flow_pd_orgs_309_b2b": flow_src["pd_orgs_309_b2b"],
        "lqxs_built_at": lqxs_built_at and lqxs_built_at.isoformat(), "lqxs_age_h": age_h(lqxs_built_at),
        "akcija_week": week[1], "akcija_send_date": week[0].isoformat(),
        "akcija_in_audience": sum(r["in_audience"] for r in akcija_rows),
        "akcija_excluded_json": cnt(("excluded_reason",), [r for r in akcija_rows if not r["in_audience"]]),
        "selfcheck_failed": len(failed), "selfcheck_failed_names": ",".join(failed) or None,
        }], T_RUN, job_config=J(write_disposition="WRITE_APPEND")).result()
    log.info("SELFCHECK run=%s failed=%s %s", RUN_ID, len(failed), failed)
    log.info("SHADOW_DONE run=%s people=%s plan_rows=%s would_send=%s pd_would_writes=%s pd_targets=%s "
             "rung_price_built_at=%s rung_price_age_h=%s pd_persons_age_h=%s pd_pending=%s writeback_population=%s state_changes=%s "
             "interface_v1_vs_map_disagreements=%s en_pending=%s en_masters=%s en_list_snapshot_age_h=%s g15_report_only goods_run=%s goods_plan_date=%s would_be_no_priced=%s would_be_no_slot_row=%s", RUN_ID, len(states), len(plan_rows),
             sum(r["would_send"] for r in plan_rows), len(acts), kinds, rung_built_at, built_age_h,
             persons_age_h, pstats, pop, len(log_rows), disagree,
             sum(r["hold_reason"] == S.HOLD_EN for r in plan_rows), len(en_masters), en_src["list_snapshot_age_h"],
             goods_run["run_id"], goods_run["plan_date"], g15_report.get(S.HOLD_NO_PRICED, 0),
             g15_report.get(S.HOLD_NO_SLOT_ROW, 0))


def _no_pd_writer(record):
    raise RuntimeError("shadow job reached the Pipedrive writer - refused")


if __name__ == "__main__":
    main()
