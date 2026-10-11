"""FLOW GATE (MAIN 11.10, Raivis D1-D7). LIVE IN THE PLAN since MAIN 11.10 11:33 F4 (sending stays DRY): the 08:05
planner calls build_core() and holds by gate (sequence.flow_gate_hold); the 08:55 job refreshes the Brevo lists (F3).
Who the engine may write to, by flow:

  G1 MANUAL_P11      a PD org/person of the master has an OPEN deal in pipeline 11 "B2B pardosana" -> no commercial,
                     no educational letter (order letters untouched - they are not in the engine's plan)
  G2 B2B_EDU_ONLY    B2B (classification_v2 flow='B2B' or PD org field 309 = 716) WITH >= 1 paid shop order -> edu only
  G3 B2B_NO_SHOP     B2B WITHOUT a paid shop order -> nothing
  G4 MANUAL_TO_SHOP  a manual master paid a shop order after the open p11 deal's add_time -> shop flow from the next
                     plan; an event row; an internal mail to the PROOF_RECIPIENT ONLY when FLOW_GATE_NOTIFY=on
  G5 Brevo lists     NO_PROMO (manual + all B2B) and NO_ANY (manual + B2B without shop): exclusion lists for
                     HAND-SENT campaigns; contacts only, nothing is sent

Commands (one per run):
  --build [--run=<send_plan run_id>]   BigQuery: flow_gate_master, flow_gate_plan_shadow, flow_gate_events, list members
  --lists                              Brevo: create (once) and sync the two lists to flow_gate_list_members
  --notify-test                        ONE internal test mail to PROOF_RECIPIENT (MAIN 11.10: build + prove)
  --notify                             mails for un-notified G4 events - refuses unless FLOW_GATE_NOTIFY=on
  --akcija-excl [--date=] [--build-only]  Tuesday hand-campaign exclusion list (a)+(b)+(c) for the next Tuesday
Nothing here opens L1 or touches send_plan, tracks, caps or a customer's state. Module level imports nothing that can
reach Brevo (the planner imports this module; Brevo is reached only through _prod() in the command-line path).
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

P = "jaunais-za-aizv04022026"
M = f"{P}.mkt_control"
T_MASTER, T_PLAN, T_EVENTS = f"{M}.flow_gate_master", f"{M}.flow_gate_plan_shadow", f"{M}.flow_gate_events"
T_MEMBERS, T_LISTS = f"{M}.flow_gate_list_members", f"{M}.flow_gate_lists"
DEFAULT_RUN = "tiktik-marketing-sender-6tpf6"
LIST_FOLDER_ID = 1
LIST_NAMES = {"NO_PROMO": "FLOW GATE NO_PROMO - manuālie + visi B2B (izslēgt no akcijas)",
              "NO_ANY": "FLOW GATE NO_ANY - manuālie + B2B bez veikala (izslēgt no izglītojošām)"}
SQL_DIR = pathlib.Path(__file__).with_name("sql")
DDL_EVENTS = (f"CREATE TABLE IF NOT EXISTS `{T_EVENTS}` (event STRING, master_key STRING, order_id STRING, "
              f"order_at TIMESTAMP, p11_deals STRING, manual_since TIMESTAMP, detected_at TIMESTAMP, "
              f"notified_at TIMESTAMP, notified_to STRING)")
DDL_LISTS = f"CREATE TABLE IF NOT EXISTS `{T_LISTS}` (list_name STRING, list_id INT64, created_at TIMESTAMP)"
SENDER_ID = 2
# MAIN 11.10 12:55 TUESDAY CONTENT 2: ONE more Brevo exclusion list for the hand-sent Tuesday akcija, refilled every
# send morning: (a) INFO slot that Tuesday (D11, bought in the last 28 days), (b) a reorder_1 of the engine that day,
# (c) cold B2B leads in Brevo list 3 (Q4 / Raivis D1). Contacts only; nothing is sent.
EXCL_KEY = "AKCIJA_EXCL"
EXCL_NAME = "AKCIJA IZSLĒGT - info logs + reorder_1 dienā + aukstie B2B (otrdienas roku kampaņai)"
T_EXCL = f"{M}.akcija_exclusion_members"
# RAIVIS D19/D20 (11.10 12:59 / 13:01): a contact who NEVER bought (new Paytraq account, old account, paid shop order,
# engine person with orders) and never READ (no click, no real open - Apple MPP auto-opens do not count) is out of
# everything the engine builds and out of the Tuesday campaign: exclusion part (d). Readers among never-buyers stay.
T_QUIET = f"{M}.quiet_never_buyer"
QUIET_SQL = f"""
CREATE OR REPLACE TABLE `{T_QUIET}` AS
WITH r AS (
  SELECT LOWER(TRIM(email)) AS email, LOGICAL_OR(IFNULL(clicked_links_count, 0) > 0) AS clicked,
         LOGICAL_OR(IFNULL(total_opens, 0) > IFNULL(apple_mpp_opens, 0)) AS real_open,
         LOGICAL_OR(IFNULL(apple_mpp_opens, 0) > 0) AS mpp, COUNT(DISTINCT campaign_id) AS letters
  FROM `{M}.brevo_campaign_recipients_hist` GROUP BY 1),
ev AS (SELECT DISTINCT LOWER(TRIM(email)) AS email FROM `{P}.business_marts.brevo_events_raw` WHERE event = 'clicks'),
buy AS (
  SELECT DISTINCT LOWER(TRIM(e)) AS email FROM `{P}.legacy_paytraq.client_map`,
         UNNEST(REGEXP_EXTRACT_ALL(IFNULL(emails, ''), r'[^,; ]+@[^,; ]+')) e WHERE docs > 0
  UNION DISTINCT SELECT DISTINCT LOWER(TRIM(c.email)) FROM `{P}.paytraq_core.clients` c
         JOIN `{P}.paytraq_core.sales_documents_list` d ON d.client_id = c.client_id
         WHERE d.document_type = 'sale' AND d.document_status NOT IN ('voided', 'draft') AND c.email LIKE '%@%'
  UNION DISTINCT SELECT DISTINCT LOWER(TRIM(email)) FROM `{P}.business_marts.mozello_orders`
         WHERE payment_status = 'paid' AND email LIKE '%@%'
  UNION DISTINCT SELECT DISTINCT i.email_norm FROM `{P}.business_marts.customer_identity` i
         JOIN `{P}.business_marts.customer_master` m USING (master_key) WHERE m.orders > 0 AND i.email_norm LIKE '%@%'),
pop AS (SELECT email FROM r UNION DISTINCT
        SELECT DISTINCT LOWER(TRIM(email)) FROM `{M}.edu_brevo_list` WHERE list_id = 3)
SELECT pop.email, IF(IFNULL(r.mpp, FALSE), 'mpp_only', 'nothing') AS why, IFNULL(r.letters, 0) AS letters,
       CURRENT_TIMESTAMP() AS built_at
FROM pop LEFT JOIN r USING (email) LEFT JOIN ev USING (email) LEFT JOIN buy USING (email)
WHERE buy.email IS NULL AND ev.email IS NULL AND NOT IFNULL(r.clicked, FALSE) AND NOT IFNULL(r.real_open, FALSE)
  AND REGEXP_CONTAINS(pop.email, r'^[-a-z0-9._%+]+@[-a-z0-9.]+[.][a-z]{{2,}}$')"""


def sql(name):
    return (SQL_DIR / name).read_text(encoding="utf-8")


def recipient(env) -> str:
    import send_day
    return send_day.proof_recipient(env)


def _s(v):
    return "NULL" if v is None else "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


# ------------------------------------------------------------------------------------------------ BigQuery
GATE_SQL = f"SELECT master_key, gate FROM `{T_MASTER}`"


def build_core(q) -> dict:
    """LIVE (MAIN 11.10 F3/F4): the daily planner (08:05) calls this first - gate table, G4 events, list members.
    BigQuery only; nothing here can reach Brevo or Pipedrive. -> {gate: n}."""
    q(sql("flow_gate_master.sql"))
    _events(q)
    q(sql("flow_gate_lists.sql"))
    return {r["gate"]: int(r["n"]) for r in q(f"SELECT gate, COUNT(*) AS n FROM `{T_MASTER}` GROUP BY 1")}


def check_core(counts, gates):
    """Refuse to plan on a gate table that cannot be right: empty, or without a single B2B master."""
    if not gates or not any(counts.get(g) for g in ("B2B_EDU_ONLY", "B2B_NO_SHOP", "B2B_SHOP_UNKNOWN")):
        raise RuntimeError(f"FLOW GATE source empty or without B2B ({counts}) - refusing to plan without the gate")


def build(q, run_id=DEFAULT_RUN) -> dict:
    q(sql("flow_gate_master.sql"))
    q(sql("flow_gate_plan_shadow.sql").replace("@run_id", _s(run_id)))
    _events(q)
    q(sql("flow_gate_lists.sql"))
    gates = {r["gate"]: r["n"] for r in q(f"SELECT gate, COUNT(*) AS n FROM `{T_MASTER}` GROUP BY 1")}
    drops = {r["k"]: r["n"] for r in q(f"SELECT IFNULL(gate_drop, 'KEPT') AS k, COUNT(*) AS n FROM `{T_PLAN}` GROUP BY 1")}
    lists = {r["list_name"]: r["n"] for r in q(f"SELECT list_name, COUNT(*) AS n FROM `{T_MEMBERS}` GROUP BY 1")}
    return {"run_id": run_id, "gates": gates, "plan_rows": drops, "list_members": lists}


def _events(q):
    q(DDL_EVENTS)
    q(f"""INSERT INTO `{T_EVENTS}` (event, master_key, order_id, order_at, p11_deals, manual_since, detected_at)
SELECT 'MANUAL_TO_SHOP', g.master_key, g.shop_after_manual.order_id, g.shop_after_manual.created_at, g.p11_deals,
       g.manual_since, CURRENT_TIMESTAMP()
FROM `{T_MASTER}` g
WHERE g.gate = 'MANUAL_TO_SHOP'
  AND NOT EXISTS (SELECT 1 FROM `{T_EVENTS}` e WHERE e.master_key = g.master_key
                  AND e.order_id = g.shop_after_manual.order_id)""")


# ------------------------------------------------------------------------------------------------ Brevo
def list_ids(q, brevo, names=None) -> dict:
    q(DDL_LISTS)
    have = {r["list_name"]: int(r["list_id"]) for r in q(f"SELECT list_name, list_id FROM `{T_LISTS}`")}
    names = LIST_NAMES if names is None else names
    for key, name in names.items():
        if key not in have:
            lid = int(brevo("POST", "/contacts/lists", {"name": name, "folderId": LIST_FOLDER_ID})["id"])
            q(f"INSERT INTO `{T_LISTS}` VALUES ({_s(key)}, {lid}, CURRENT_TIMESTAMP())")
            have[key] = lid
    return {k: v for k, v in have.items() if k in names}


def list_members(brevo, lid) -> set:
    out, off = set(), 0
    while True:
        r = brevo("GET", f"/contacts/lists/{lid}/contacts?limit=500&offset={off}", None) or {}
        page = [(c.get("email") or "").strip().lower() for c in r.get("contacts") or []]
        out |= set(page)
        off += 500
        if len(page) < 500:
            return out


def sync_lists(q, brevo, chunk=150) -> dict:
    ids, out = list_ids(q, brevo), {}
    for key, lid in ids.items():
        want = {r["email"] for r in q(f"SELECT email FROM `{T_MEMBERS}` WHERE list_name = {_s(key)}")}
        out[key] = sync_one(brevo, lid, want, chunk)
    return out


def sync_one(brevo, lid, want, chunk=150) -> dict:
    """Make Brevo list lid hold exactly `want` (of the addresses that are Brevo contacts)."""
    have = list_members(brevo, lid)
    add, drop, not_in_brevo, invalid = sorted(want - have), sorted(have - want), 0, 0
    for i in range(0, len(add), chunk):
        part = add[i:i + chunk]
        try:
            r = brevo("POST", f"/contacts/lists/{lid}/contacts/add", {"emails": part}) or {}
            not_in_brevo += len(((r.get("contacts") or {}).get("failure")) or [])
        except Exception:  # noqa: BLE001 - Brevo refuses the whole chunk for one malformed address: go one by one
            for e in part:
                try:
                    r = brevo("POST", f"/contacts/lists/{lid}/contacts/add", {"emails": [e]}) or {}
                    not_in_brevo += len(((r.get("contacts") or {}).get("failure")) or [])
                except Exception:  # noqa: BLE001
                    invalid += 1
    for i in range(0, len(drop), chunk):
        brevo("POST", f"/contacts/lists/{lid}/contacts/remove", {"emails": drop[i:i + chunk]})
    now = list_members(brevo, lid)
    return {"list_id": lid, "wanted": len(want), "added": len(add) - not_in_brevo - invalid,
            "removed": len(drop), "not_a_brevo_contact": not_in_brevo, "refused_by_brevo": invalid,
            "members_now": len(now)}


def excl_sql(send_date: str) -> str:
    """Members of the Tuesday exclusion list for send_date. (b) = the latest plan run's reorder_1 would-send rows due
    on or before the date (before 08:05 that is the day before's plan - a superset is the safe side)."""
    import edu
    return f"""
WITH it AS (SELECT MAX(plan_date) AS pd FROM `{M}.info_track_shadow` WHERE slot_date = DATE '{send_date}'),
a AS (SELECT DISTINCT LOWER(TRIM(email)) AS email, 'a_info_slot' AS why FROM `{M}.info_track_shadow`
      WHERE slot_date = DATE '{send_date}' AND plan_date = (SELECT pd FROM it) AND slot = 'INFO'),
run AS (SELECT run_id FROM `{M}.shadow_send_plan` WHERE plan_date <= DATE '{send_date}'
        GROUP BY run_id, plan_date ORDER BY plan_date DESC, MAX(planned_at) DESC LIMIT 1),
b AS (SELECT DISTINCT LOWER(TRIM(email)) AS email, 'b_reorder_1' AS why FROM `{M}.shadow_send_plan`
      WHERE run_id = (SELECT run_id FROM run) AND email_type = 'reorder_1' AND would_send
        AND planned_send_date <= DATE '{send_date}'),
l3 AS (SELECT DISTINCT LOWER(TRIM(email)) AS email FROM `{edu.T_BLIST}` WHERE list_id = 3),
c AS (SELECT DISTINCT l.email, 'c_cold_b2b_lead' AS why FROM `{edu.T_LEAD}` l JOIN l3 USING (email) WHERE l.is_lead),
d AS (SELECT DISTINCT q.email, 'd_quiet_never_buyer' AS why FROM `{T_QUIET}` q JOIN l3 USING (email)),
x AS (SELECT * FROM a UNION ALL SELECT * FROM b UNION ALL SELECT * FROM c UNION ALL SELECT * FROM d)
SELECT DATE '{send_date}' AS send_date, email, STRING_AGG(DISTINCT why ORDER BY why) AS why, CURRENT_TIMESTAMP() AS built_at
FROM x WHERE REGEXP_CONTAINS(email, r'^[-a-z0-9._%+]+@[-a-z0-9.]+[.][a-z]{{2,}}$') GROUP BY email"""


def build_excl(q, send_date: str, refresh_leads=True) -> dict:
    """BigQuery only. The cold-lead table is rebuilt with edu's own text first (it is otherwise only rebuilt by hand)."""
    import edu
    if refresh_leads:
        q(edu.LEADS_SQL)
        q(QUIET_SQL)
    q(f"CREATE TABLE IF NOT EXISTS `{T_EXCL}` (send_date DATE, email STRING, why STRING, built_at TIMESTAMP)")
    q(f"DELETE FROM `{T_EXCL}` WHERE send_date = DATE '{send_date}'")
    q(f"INSERT INTO `{T_EXCL}` (send_date, email, why, built_at) " + excl_sql(send_date))
    split = {r["k"]: int(r["n"]) for r in q(
        f"SELECT k, COUNT(*) AS n FROM `{T_EXCL}`, UNNEST(SPLIT(why, ',')) k WHERE send_date = DATE '{send_date}' "
        f"GROUP BY k")}
    total = q(f"SELECT COUNT(*) AS n FROM `{T_EXCL}` WHERE send_date = DATE '{send_date}'")[0]["n"]
    return {"send_date": send_date, "members": int(total), "split": split}


def sync_excl(q, brevo, send_date: str) -> dict:
    lid = list_ids(q, brevo, {EXCL_KEY: EXCL_NAME})[EXCL_KEY]
    want = {r["email"] for r in q(f"SELECT email FROM `{T_EXCL}` WHERE send_date = DATE '{send_date}'")}
    return sync_one(brevo, lid, want)


def mail_text(client, order_nr) -> str:
    return f"{client} no manuālās plūsmas nopirka veikalā (pasūtījums {order_nr}), pārlikts uz Veikals"


def send_mail(brevo, to, subject, text):
    return brevo("POST", "/smtp/email", {"sender": {"id": SENDER_ID}, "to": [{"email": to}], "subject": subject,
                                         "textContent": text, "tags": ["flow-gate"]})


def notify_test(brevo, env) -> dict:
    to = recipient(env)
    text = mail_text("TESTS (Klients SIA)", "M-000000-00000")
    r = send_mail(brevo, to, "TEST · " + text, "TESTS - šis ir plūsmas vārtu (G4) paziņojuma paraugs. " + text)
    return {"to": to, "messageId": (r or {}).get("messageId"), "text": text}


def notify(q, brevo, env) -> dict:
    if (env.get("FLOW_GATE_NOTIFY") or "").strip().lower() != "on":
        return {"refused": "FLOW_GATE_NOTIFY is not 'on' - the G4 trigger stays off until MAIN's word", "sent": 0}
    to, sent = recipient(env), 0
    rows = q(f"""SELECT e.master_key, e.order_id, ANY_VALUE(c.freshest_name) AS client
FROM `{T_EVENTS}` e LEFT JOIN `{P}.business_marts.customer_master` c USING (master_key)
WHERE e.notified_at IS NULL GROUP BY 1, 2""")
    for r in rows:
        text = mail_text(r.get("client") or r["master_key"], r["order_id"])
        send_mail(brevo, to, text, text)
        q(f"UPDATE `{T_EVENTS}` SET notified_at = CURRENT_TIMESTAMP(), notified_to = {_s(to)} "
          f"WHERE master_key = {_s(r['master_key'])} AND order_id = {_s(r['order_id'])}")
        sent += 1
    return {"to": to, "sent": sent}


# ------------------------------------------------------------------------------------------------ wiring
def _prod():
    from google.cloud import bigquery
    import campaign as CAMP
    c = bigquery.Client(project=P)
    return (lambda s: [dict(r) for r in c.query(s).result()]), CAMP._call


def main(argv) -> int:
    args = dict((a.lstrip("-").split("=", 1) + [""])[:2] for a in argv)
    q, brevo = _prod()
    if "build" in args:
        res = build(q, args.get("run") or DEFAULT_RUN)
    elif "lists" in args:
        res = sync_lists(q, brevo)
    elif "akcija-excl" in args:
        import info_track
        iq, ins = info_track._prod()
        d = args.get("date") or ""
        day = __import__("datetime").date.fromisoformat(d) if d else None
        it = info_track.shadow(iq, ins, weeks=4)
        res = {"info_track": {k: v for k, v in it.items() if k != "catalog"},
               "build": build_excl(q, (day or __import__("datetime").date.fromisoformat(min(it["per_tuesday"]))).isoformat())}
        res["brevo"] = sync_excl(q, brevo, res["build"]["send_date"]) if "build-only" not in args else None
    elif "notify-test" in args:
        res = notify_test(brevo, os.environ)
    elif "notify" in args:
        res = notify(q, brevo, os.environ)
    else:
        print(__doc__)
        return 2
    print("FLOW_GATE " + json.dumps(res, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
