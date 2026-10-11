"""FLOW GATE (MAIN 11.10, Raivis D1-D7) - SHADOW. Who the engine may write to, by flow:

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
Nothing here opens L1 or touches send_plan, tracks, caps or a customer's state.
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


def sql(name):
    return (SQL_DIR / name).read_text(encoding="utf-8")


def recipient(env) -> str:
    import send_day
    return send_day.proof_recipient(env)


def _s(v):
    return "NULL" if v is None else "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


# ------------------------------------------------------------------------------------------------ BigQuery
def build(q, run_id=DEFAULT_RUN) -> dict:
    q(sql("flow_gate_master.sql"))
    q(sql("flow_gate_plan_shadow.sql").replace("@run_id", _s(run_id)))
    q(DDL_EVENTS)
    q(f"""INSERT INTO `{T_EVENTS}` (event, master_key, order_id, order_at, p11_deals, manual_since, detected_at)
SELECT 'MANUAL_TO_SHOP', g.master_key, g.shop_after_manual.order_id, g.shop_after_manual.created_at, g.p11_deals,
       g.manual_since, CURRENT_TIMESTAMP()
FROM `{T_MASTER}` g
WHERE g.gate = 'MANUAL_TO_SHOP'
  AND NOT EXISTS (SELECT 1 FROM `{T_EVENTS}` e WHERE e.master_key = g.master_key
                  AND e.order_id = g.shop_after_manual.order_id)""")
    q(sql("flow_gate_lists.sql"))
    gates = {r["gate"]: r["n"] for r in q(f"SELECT gate, COUNT(*) AS n FROM `{T_MASTER}` GROUP BY 1")}
    drops = {r["k"]: r["n"] for r in q(f"SELECT IFNULL(gate_drop, 'KEPT') AS k, COUNT(*) AS n FROM `{T_PLAN}` GROUP BY 1")}
    lists = {r["list_name"]: r["n"] for r in q(f"SELECT list_name, COUNT(*) AS n FROM `{T_MEMBERS}` GROUP BY 1")}
    return {"run_id": run_id, "gates": gates, "plan_rows": drops, "list_members": lists}


# ------------------------------------------------------------------------------------------------ Brevo
def list_ids(q, brevo) -> dict:
    q(DDL_LISTS)
    have = {r["list_name"]: int(r["list_id"]) for r in q(f"SELECT list_name, list_id FROM `{T_LISTS}`")}
    for key, name in LIST_NAMES.items():
        if key not in have:
            lid = int(brevo("POST", "/contacts/lists", {"name": name, "folderId": LIST_FOLDER_ID})["id"])
            q(f"INSERT INTO `{T_LISTS}` VALUES ({_s(key)}, {lid}, CURRENT_TIMESTAMP())")
            have[key] = lid
    return have


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
        have = list_members(brevo, lid)
        add, drop, not_in_brevo = sorted(want - have), sorted(have - want), 0
        for i in range(0, len(add), chunk):
            r = brevo("POST", f"/contacts/lists/{lid}/contacts/add", {"emails": add[i:i + chunk]}) or {}
            not_in_brevo += len(((r.get("contacts") or {}).get("failure")) or [])
        for i in range(0, len(drop), chunk):
            brevo("POST", f"/contacts/lists/{lid}/contacts/remove", {"emails": drop[i:i + chunk]})
        now = list_members(brevo, lid)
        out[key] = {"list_id": lid, "wanted": len(want), "added": len(add) - not_in_brevo, "removed": len(drop),
                    "not_a_brevo_contact": not_in_brevo, "members_now": len(now)}
    return out


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
