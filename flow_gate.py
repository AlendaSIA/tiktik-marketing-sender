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


# RAIVIS D46 (12.10 18:28, MAIN 2 18:35): every marketing-eligible LV contact gets EXACTLY ONE Tuesday letter - the
# personal letter if it really goes, otherwise 236. Part (b) is therefore EVERY personal letter of the day, read from
# the send day's own per-row outcome (mkt_control.send_outcome), never from the plan: a letter held at send time (L7,
# L8, block_empty, any hold) is not in (b), so that contact gets 236 the same day. No send-day run for the date ->
# no list (fail closed: a 236 on a guessed list would double or drop people).
# forecast=True (DRY / the morning preview only): a type refused ONLY by D1 (track not yet enabled) counts as going.


class NoSendDayRun(RuntimeError):
    pass


def _edu():
    import edu
    return edu


def personal_sql(send_date: str, forecast: bool = False, outcome_table: str | None = None) -> str:
    """Emails that receive a personal letter on send_date. LIVE: every row SENT by any send-day run of that date (a
    letter that went, went - several runs per day add up). FORECAST: the latest run of the date, DRY_WOULD_SEND plus a
    type refused ONLY by D1. One row per email (letters = how many personal types that email got - must be 1)."""
    t = outcome_table or f"{M}.send_outcome"
    if forecast:
        where = (f"run_id = (SELECT run_id FROM `{t}` WHERE send_date = DATE '{send_date}' GROUP BY run_id "
                 f"ORDER BY MAX(created_at) DESC LIMIT 1) AND (outcome IN ('SENT', 'DRY_WOULD_SEND') "
                 f"OR (outcome = 'TYPE_REFUSED' AND STARTS_WITH(IFNULL(reason, ''), 'D1 TRACK')))")
    else:
        where = f"send_date = DATE '{send_date}' AND outcome = 'SENT'"
    return f"""
SELECT LOWER(TRIM(email)) AS email, MIN(email_type) AS email_type, COUNT(DISTINCT email_type) AS letters,
       ANY_VALUE(run_id) AS send_run
FROM `{t}` WHERE {where} AND email LIKE '%@%'
GROUP BY 1"""


# D47 (Raivis 12.10 18:59): 98c / 98d read the LIVE Brevo list 3, snapshot by the send day itself right before the
# build (snapshot_l3), never the edu_brevo_list snapshot of another day.
T_L3LIVE = f"{M}.brevo_list3_live"


def snapshot_l3(q, brevo, send_date: str) -> int:
    """Brevo list 3 members NOW -> mkt_control.brevo_list3_live (send_date rows replaced). GET only."""
    emails = sorted(e for e in list_members(brevo, 3) if e)
    q(f"CREATE TABLE IF NOT EXISTS `{T_L3LIVE}` (send_date DATE, email STRING, fetched_at TIMESTAMP)")
    q(f"DELETE FROM `{T_L3LIVE}` WHERE send_date = DATE '{send_date}'")
    for i in range(0, len(emails), 2000):
        q(f"INSERT INTO `{T_L3LIVE}` (send_date, email, fetched_at) VALUES " + ", ".join(
            f"(DATE '{send_date}', {_s(e)}, CURRENT_TIMESTAMP())" for e in emails[i:i + 2000]))
    return len(emails)


def excl_sql(send_date: str, forecast: bool = False, outcome_table: str | None = None) -> str:
    """Members of the Tuesday exclusion list for send_date. (b) = D46: every personal letter of that send day
    (personal_sql), all types - not only reorder_1 (the 18:20 defect: winback_1 / active_xsell / 244 got 236 too).
    D47 (Raivis 18:59): INFO letters go on THURSDAY, never on a Tuesday - the old part (a) "INFO slot instead of 236"
    is gone (it left those contacts with no letter at all). (c)/(d) use the live list 3 of the send day."""
    return f"""
WITH pl AS ({personal_sql(send_date, forecast, outcome_table)}),
b AS (SELECT email, CONCAT('b_personal_', email_type) AS why FROM pl),
l3 AS (SELECT DISTINCT LOWER(TRIM(email)) AS email FROM `{T_L3LIVE}` WHERE send_date = DATE '{send_date}'),
c AS (SELECT DISTINCT l.email, 'c_cold_b2b_lead' AS why FROM `{_edu().T_LEAD}` l JOIN l3 USING (email) WHERE l.is_lead),
d AS (SELECT DISTINCT q.email, 'd_quiet_never_buyer' AS why FROM `{T_QUIET}` q JOIN l3 USING (email)),
x AS (SELECT * FROM b UNION ALL SELECT * FROM c UNION ALL SELECT * FROM d)
SELECT DATE '{send_date}' AS send_date, email, STRING_AGG(DISTINCT why ORDER BY why) AS why, CURRENT_TIMESTAMP() AS built_at
FROM x WHERE REGEXP_CONTAINS(email, r'^[-a-z0-9._%+]+@[-a-z0-9.]+[.][a-z]{{2,}}$') GROUP BY email"""


def build_excl(q, send_date: str, refresh_leads=True, forecast=False, table=None, outcome_table=None) -> dict:
    """BigQuery only. The cold-lead table is rebuilt with edu's own text first (it is otherwise only rebuilt by hand).
    D46: refuses (NoSendDayRun) when the send day has not run for send_date - the list is built from its outcome."""
    import edu
    ot = outcome_table or f"{M}.send_outcome"
    if not q(f"SELECT 1 AS x FROM `{ot}` WHERE send_date = DATE '{send_date}' LIMIT 1"):
        raise NoSendDayRun(f"D46: no send-day run for {send_date} in {ot} - list 98 is built only from the send "
                           f"day's outcome; no 236 before it")
    T_EXCL = table or globals()["T_EXCL"]
    if not q(f"SELECT 1 AS x FROM `{T_L3LIVE}` WHERE send_date = DATE '{send_date}' LIMIT 1"):
        raise NoSendDayRun(f"D47: no live Brevo list 3 snapshot for {send_date} - run snapshot_l3 first")
    if refresh_leads:
        q(edu.LEADS_SQL)
        q(QUIET_SQL)
    q(f"CREATE TABLE IF NOT EXISTS `{T_EXCL}` (send_date DATE, email STRING, why STRING, built_at TIMESTAMP)")
    q(f"DELETE FROM `{T_EXCL}` WHERE send_date = DATE '{send_date}'")
    q(f"INSERT INTO `{T_EXCL}` (send_date, email, why, built_at) " + excl_sql(send_date, forecast, outcome_table))
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
               "build": build_excl(q, (day or __import__("datetime").date.fromisoformat(min(it["per_tuesday"]))).isoformat(),
                                   forecast="forecast" in args)}
        # a forecast list is never pushed to Brevo: only the send day's real outcome may fill list 98
        res["brevo"] = sync_excl(q, brevo, res["build"]["send_date"]) \
            if "build-only" not in args and "forecast" not in args else None
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
