"""D11 POST-PURCHASE INFO TRACK (Raivis 11.10 11:18, MAIN 11:20 / 11:35 task (c)) - SHADOW ONLY. Sends nothing.

Raivis' rule:
  - days since the contact's last paid purchase <= 28 -> the TUESDAY slot carries an INFO letter INSTEAD of the weekly
    akcija; the Thursday educational letter stays as it is; akcija resumes after day 28 (week 5)
  - info letter SEEN = CLICKED in that letter (Brevo click data in BigQuery); an open does NOT count
  - a clicked info letter is never sent again; an unclicked one at most INFO_MAX_SENDS (2) times in total
  - no unseen info letter for that contact -> the slot stays EMPTY (fine by Raivis)
  - a NEW info letter added later (catalogue audience 'all') goes to every contact who has not clicked it, same rule,
    whatever the purchase date
Letters come from ONE catalogue table (mkt_control.info_track_catalog: letter_key, ord, template_id NULL until
"Vēstuļu šabloni 7b" delivers the ids). Week k of the window (k = 1..4) prefers the letter with ord = k; when that
one is clicked or used up, the next eligible letter in catalogue order; otherwise EMPTY.

Click sources (both in BigQuery, both read-only):
  engine campaign sends: mkt_control.send_log (email_type = letter_key, campaign_id) x
                         mkt_control.brevo_campaign_recipients_hist.clicked_links_count > 0
  transactional sends:   business_marts.brevo_events_raw event = 'clicks' AND template_id = the catalogue template id

Commands:  --shadow [--weeks=4]   build the catalogue (once) and mkt_control.info_track_shadow for the next Tuesdays
"""
from __future__ import annotations

import datetime as dt
import json
import sys

P = "jaunais-za-aizv04022026"
M = f"{P}.mkt_control"
T_CAT, T_SHADOW = f"{M}.info_track_catalog", f"{M}.info_track_shadow"
WINDOW_DAYS = 28
INFO_MAX_SENDS = 2
# Raivis' agreed order 11:20 (wk1 cabinet/documents/DDL = rework of 229 beside the existing 244 feedback; wk2 free
# hygiene plan; wk3 recommend us to a colleague; wk4 tiktik.lv advantages + regular purchases). Keys are the
# engine's working names; template ids stay NULL until MAIN passes them.
SEED = [("info_kabinets_dokumenti", 1), ("info_higienas_plans", 2), ("info_iesaki_kolegim", 3),
        ("info_tiktik_prieksrocibas", 4)]
DDL_CAT = (f"CREATE TABLE IF NOT EXISTS `{T_CAT}` (letter_key STRING NOT NULL, ord INT64 NOT NULL, template_id INT64, "
           f"audience STRING, active BOOL, added_at TIMESTAMP, note STRING)")


def week_no(slot_day: dt.date, last_purchase: dt.date | None):
    """1..4 when slot_day is within WINDOW_DAYS after the purchase (purchase day itself = day 0), else None."""
    if last_purchase is None:
        return None
    d = (slot_day - last_purchase).days
    if d < 0 or d > WINDOW_DAYS:
        return None
    return min(4, d // 7 + 1)


def pick(week, catalog, clicked, sends):
    """One contact, one Tuesday. catalog = rows in ord order (letter_key, ord, audience, active);
    clicked = set of letter_keys; sends = {letter_key: n}. -> (slot, letter_key or None, reason).
    slot: INFO | EMPTY | AKCIJA."""
    ok = [c for c in catalog if c.get("active", True) and c["letter_key"] not in clicked
          and sends.get(c["letter_key"], 0) < INFO_MAX_SENDS]
    if week is not None:
        pref = [c for c in ok if c["ord"] == week]
        if pref:
            return "INFO", pref[0]["letter_key"], f"WEEK_{week}"
        if ok:
            return "INFO", ok[0]["letter_key"], f"WEEK_{week}_NEXT_ELIGIBLE"
        return "EMPTY", None, "NOTHING_UNSEEN"
    new = [c for c in ok if (c.get("audience") or "post_purchase") == "all"]
    if new:
        return "INFO", new[0]["letter_key"], "NEW_LETTER_ALL"
    return "AKCIJA", None, "OUTSIDE_WINDOW"


def tuesdays(today: dt.date, n: int):
    first = today + dt.timedelta(days=(1 - today.weekday()) % 7)
    return [first + dt.timedelta(days=7 * i) for i in range(n)]


def simulate(people, catalog, clicked, sends, days):
    """people = [{master_key, email, last_purchase}] (the akcija population). Sends accumulate across the simulated
    Tuesdays (a letter sent in week 1 counts in week 2); clicks are only the measured ones. -> rows."""
    sends = {k: dict(v) for k, v in sends.items()}
    out = []
    for day in days:
        for p in people:
            mk = p["master_key"]
            w = week_no(day, p.get("last_purchase"))
            slot, key, why = pick(w, catalog, clicked.get(mk, set()), sends.get(mk, {}))
            if slot == "INFO":
                s = sends.setdefault(mk, {})
                s[key] = s.get(key, 0) + 1
            out.append({"slot_date": day.isoformat(), "master_key": mk, "email": p.get("email"),
                        "last_purchase": p["last_purchase"].isoformat() if p.get("last_purchase") else None,
                        "week_no": w, "slot": slot, "letter_key": key, "reason": why})
    return out


PEOPLE_SQL = f"""
WITH run AS (SELECT run_id FROM `{M}.shadow_run_report` WHERE plan_date = CURRENT_DATE()
             ORDER BY finished_at DESC LIMIT 1),
a AS (SELECT master_key, LOWER(TRIM(email)) AS email FROM `{M}.shadow_akcija_audience`
      WHERE plan_date = CURRENT_DATE() AND run_id = (SELECT run_id FROM run)
        AND (excluded_reason IS NULL OR excluded_reason = 'PERSONAL_LETTER_THIS_WEEK')),
lc AS (SELECT master_key, MAX(last_order) AS lo FROM `{P}.business_marts.customer_lifecycle` GROUP BY 1),
idn AS (SELECT DISTINCT email_norm, master_key FROM `{P}.business_marts.customer_identity`
        WHERE master_key IS NOT NULL AND email_norm IS NOT NULL),
mz AS (SELECT i.master_key, MAX(mo.created_date) AS paid_on FROM `{P}.business_marts.mozello_orders` mo
       JOIN idn i ON i.email_norm = LOWER(TRIM(mo.email)) WHERE mo.payment_status = 'paid' GROUP BY 1)
SELECT a.master_key, a.email,
       CASE WHEN lc.lo IS NULL THEN mz.paid_on WHEN mz.paid_on IS NULL THEN DATE(lc.lo)
            ELSE GREATEST(DATE(lc.lo), mz.paid_on) END AS last_purchase
FROM a LEFT JOIN lc USING (master_key) LEFT JOIN mz USING (master_key)"""


def clicks_sql(catalog) -> str:
    keys = ", ".join("'" + c["letter_key"] + "'" for c in catalog) or "''"
    tids = ", ".join(str(int(c["template_id"])) for c in catalog if c.get("template_id")) or "-1"
    tmap = " ".join(f"WHEN {int(c['template_id'])} THEN '{c['letter_key']}'" for c in catalog if c.get("template_id"))
    tcase = f"CASE e.template_id {tmap} END" if tmap else "CAST(NULL AS STRING)"
    return f"""
SELECT l.master_key, l.email_type AS letter_key, COUNT(DISTINCT l.campaign_id) AS sends,
       LOGICAL_OR(IFNULL(r.clicked_links_count, 0) > 0) AS clicked
FROM `{M}.send_log` l
LEFT JOIN `{M}.brevo_campaign_recipients_hist` r
  ON r.campaign_id = l.campaign_id AND LOWER(TRIM(r.email)) = LOWER(TRIM(l.email))
WHERE l.email_type IN ({keys}) AND l.master_key IS NOT NULL
GROUP BY 1, 2
UNION ALL
SELECT i.master_key, {tcase} AS letter_key, COUNT(DISTINCT IF(e.event = 'delivered', e.message_id, NULL)) AS sends,
       LOGICAL_OR(e.event = 'clicks') AS clicked
FROM `{P}.business_marts.brevo_events_raw` e
JOIN (SELECT DISTINCT email_norm, master_key FROM `{P}.business_marts.customer_identity`
      WHERE master_key IS NOT NULL) i ON i.email_norm = LOWER(TRIM(e.email))
WHERE e.event IN ('clicks', 'delivered') AND e.template_id IN ({tids})
GROUP BY 1, 2"""


def shadow(q, insert, today=None, weeks=4) -> dict:
    today = today or dt.date.today()
    q(DDL_CAT)
    if not q(f"SELECT 1 AS x FROM `{T_CAT}` LIMIT 1"):
        insert(T_CAT, [{"letter_key": k, "ord": o, "template_id": None, "audience": "post_purchase", "active": True,
                        "added_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "note": "Raivis 11.10 11:20 order; template id from Vestulu sabloni 7b"} for k, o in SEED])
    catalog = sorted(q(f"SELECT letter_key, ord, template_id, audience, active FROM `{T_CAT}`"), key=lambda c: c["ord"])
    clicked, sends = {}, {}
    for r in q(clicks_sql(catalog)):
        if not r.get("letter_key"):
            continue
        if r.get("clicked"):
            clicked.setdefault(r["master_key"], set()).add(r["letter_key"])
        if r.get("sends"):
            d = sends.setdefault(r["master_key"], {})
            d[r["letter_key"]] = d.get(r["letter_key"], 0) + int(r["sends"])
    people = q(PEOPLE_SQL)
    for p in people:
        lp = p.get("last_purchase")
        p["last_purchase"] = dt.date.fromisoformat(str(lp)[:10]) if lp else None
    days = tuesdays(today, weeks)
    rows = simulate(people, catalog, clicked, sends, days)
    q(f"CREATE TABLE IF NOT EXISTS `{T_SHADOW}` (plan_date DATE, slot_date DATE, master_key STRING, email STRING, "
      f"last_purchase DATE, week_no INT64, slot STRING, letter_key STRING, reason STRING, built_at TIMESTAMP)")
    q(f"DELETE FROM `{T_SHADOW}` WHERE plan_date = '{today.isoformat()}'")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    insert(T_SHADOW, [{"plan_date": today.isoformat(), "built_at": now, **r} for r in rows])
    per = {}
    for r in rows:
        k = (r["slot_date"], r["slot"] + (":" + r["letter_key"] if r["letter_key"] else ""))
        per[k] = per.get(k, 0) + 1
    out = {}
    for (d, s), n in sorted(per.items()):
        out.setdefault(d, {})[s] = n
    return {"people": len(people), "no_purchase_date": sum(p["last_purchase"] is None for p in people),
            "catalog": [c["letter_key"] for c in catalog], "clicked_people": len(clicked), "per_tuesday": out}


def _prod():
    from google.cloud import bigquery
    c = bigquery.Client(project=P)

    def q(s):
        return [dict(r) for r in c.query(s).result()]

    def insert(table, rows):
        for i in range(0, len(rows), 5000):
            c.load_table_from_json(rows[i:i + 5000], table, job_config=bigquery.LoadJobConfig(
                write_disposition="WRITE_APPEND")).result()
    return q, insert


def main(argv) -> int:
    args = dict((a.lstrip("-").split("=", 1) + [""])[:2] for a in argv)
    if "shadow" not in args:
        print(__doc__)
        return 2
    q, insert = _prod()
    print("INFO_TRACK " + json.dumps(shadow(q, insert, weeks=int(args.get("weeks") or 4)), ensure_ascii=False,
                                     default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
