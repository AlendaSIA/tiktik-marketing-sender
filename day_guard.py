"""Send-day guards that look at the WHOLE day, not one letter type (Raivis 12.10, MAIN 2 19:10).

D47_DOUBLE (Raivis 18:59, permanent, every send day, every track): a contact who would get 2+ letters the same day
    gets NOTHING (no automatic pick of one) and ONE alert mail goes to raivis@alenda.lv with the list (contact,
    letters, why). Raivis solves it with Claude; it is sent by hand and the cause is fixed. Keys: master_key AND the
    e-mail (two master keys on one address are one inbox).
D47_UNEXPLAINED_CHANGE (day-before freeze): the Monday plan (latest plan run of the day before) is what Raivis saw in
    the D43 previews. On the send day the only allowed change is a contact who ORDERED since that plan (paid shop order
    after the Monday plan's planned_at) and therefore changed segment. A contact chosen today whose (type) was not in
    the Monday plan and who did not order since -> held + the same alert mail. A contact in the Monday plan but not
    chosen today is only reported (it falls back to 236 by D46).
D46_CAB_LINK_MISMATCH (Raivis, MAIN 2 19:10): every personal-price letter carries THE CONTACT'S OWN cabinet link. The
    KABINETS_URL attribute is read back FROM BREVO and must contain cabinet_price_current.token of the same master_key
    and e-mail (latest written cab-YYYYMMDD run on or before the send day). No link / another token -> that letter is
    held (the contact falls back to 236).
Pure functions + the SQL they read. send_day wires them in (member refusal + the alert)."""
from __future__ import annotations

import datetime as dt
import re

P = "jaunais-za-aizv04022026"
M = f"{P}.mkt_control"
ALERT_TO = "raivis@alenda.lv"
DOUBLE, UNEXPLAINED, CAB = "D47_DOUBLE", "D47_UNEXPLAINED_CHANGE", "D46_CAB_LINK_MISMATCH"
# letters that print personal prices and link the cabinet (179 / 180 / 232 / 233 + E2 / 235)
PRICED_TYPES = {"reorder_1", "winback_1", "winback_2", "winback_3", "winback_1_e2", "winback_2_e2", "winback_3_e2",
                "active_xsell"}
CAB_ATTR = "KABINETS_URL"


def _e(v) -> str:
    return str(v or "").strip().lower()


def doubles(cands_by_type: dict) -> dict:
    """cands_by_type = {email_type: [candidate rows with master_key, email]} of ONE send day (every type that may go
    that day). -> {master_key: [types]} for every contact that appears in 2+ types, by master_key or by e-mail."""
    by_mk, by_em = {}, {}
    for et, rows in cands_by_type.items():
        for c in rows:
            by_mk.setdefault(c["master_key"], set()).add(et)
            if _e(c.get("email")):
                by_em.setdefault(_e(c.get("email")), set()).add((et, c["master_key"]))
    out = {mk: sorted(ts) for mk, ts in by_mk.items() if len(ts) > 1}
    for em, pairs in by_em.items():
        if len(pairs) > 1:
            for _, mk in pairs:
                out[mk] = sorted(set(out.get(mk, [])) | {t for t, _ in pairs})
    return out


def unexplained(cands_by_type: dict, monday: set, ordered_since: set) -> dict:
    """monday = {(master_key, email_type)} the Monday plan said would go; ordered_since = {master_key, e-mail} with a
    paid order after that plan. -> {master_key: why} for contacts chosen today outside the Monday plan, unexplained."""
    out = {}
    for et, rows in cands_by_type.items():
        for c in rows:
            mk = c["master_key"]
            if (mk, et) in monday or mk in ordered_since or _e(c.get("email")) in ordered_since:
                continue
            was = sorted(t for m, t in monday if m == mk)
            out[mk] = f"{et} not in the Monday plan" + (f" (Monday: {', '.join(was)})" if was else "")
    return out


def token_in(url, token) -> bool:
    return bool(token) and bool(url) and re.search(r"(?<![0-9a-zA-Z])" + re.escape(str(token)) + r"(?![0-9a-zA-Z])",
                                                   str(url)) is not None


def cab_check(rows, tokens: dict, urls: dict) -> dict:
    """rows = chosen personal-price candidates (master_key, email); tokens = {master_key: (token, email)} of the cab
    run; urls = {email: KABINETS_URL in Brevo}. -> {master_key: reason} for no link / foreign link."""
    out = {}
    for c in rows:
        mk, em = c["master_key"], _e(c.get("email"))
        tok, tok_em = tokens.get(mk, (None, None))
        url = urls.get(em)
        if not tok:
            out[mk] = f"{CAB} no cabinet token for {mk}"
        elif tok_em and _e(tok_em) != em:
            out[mk] = f"{CAB} cabinet e-mail {tok_em} != letter e-mail {em}"
        elif not url:
            out[mk] = f"{CAB} no {CAB_ATTR} in Brevo"
        elif not token_in(url, tok):
            out[mk] = f"{CAB} {CAB_ATTR} carries another token"
    return out


def brevo_attr(brevo, attr=CAB_ATTR, page=1000) -> dict:
    """Every Brevo contact's attr value, read back from Brevo (GET /contacts, paged). -> {email: value}."""
    out, off = {}, 0
    while True:
        r = brevo("GET", f"/contacts?limit={page}&offset={off}", None) or {}
        rows = r.get("contacts") or []
        for c in rows:
            v = (c.get("attributes") or {}).get(attr)
            if v not in (None, ""):
                out[_e(c.get("email"))] = str(v)
        off += page
        if len(rows) < page:
            return out


def cab_tokens_sql(d: dt.date) -> str:
    return f"""
WITH r AS (SELECT run_id FROM `{M}.cabinet_price_current`
           WHERE REGEXP_CONTAINS(run_id, r'^cab-[0-9]{{8}}$') AND run_id <= 'cab-{d:%Y%m%d}'
           GROUP BY run_id ORDER BY run_id DESC LIMIT 1)
SELECT master_key, ANY_VALUE(token) AS token, ANY_VALUE(LOWER(TRIM(email))) AS email, ANY_VALUE(run_id) AS run_id
FROM `{M}.cabinet_price_current` WHERE run_id = (SELECT run_id FROM r) GROUP BY master_key"""


def monday_sql(d: dt.date) -> str:
    """The latest plan run of the day before: {(master_key, email_type)} due by d that would go, + its planned_at."""
    prev = d - dt.timedelta(days=1)
    return f"""
WITH r AS (SELECT run_id, MAX(planned_at) AS p_at FROM `{M}.shadow_send_plan` WHERE plan_date = DATE '{prev}'
           GROUP BY run_id ORDER BY p_at DESC LIMIT 1)
SELECT p.master_key, p.email_type, (SELECT p_at FROM r) AS planned_at, (SELECT run_id FROM r) AS run_id
FROM `{M}.shadow_send_plan` p WHERE p.run_id = (SELECT run_id FROM r) AND p.would_send
  AND p.planned_send_date <= DATE '{d}' AND p.email_type IS NOT NULL"""


def ordered_since_sql(ts: str) -> str:
    return f"""
SELECT DISTINCT LOWER(TRIM(o.email)) AS k FROM `{P}.business_marts.mozello_orders` o
WHERE o.payment_status = 'paid' AND o.created_at > TIMESTAMP '{ts}' AND o.email LIKE '%@%'
UNION DISTINCT
SELECT DISTINCT i.master_key FROM `{P}.business_marts.mozello_orders` o
JOIN `{P}.business_marts.customer_identity` i ON i.email_norm = LOWER(TRIM(o.email))
WHERE o.payment_status = 'paid' AND o.created_at > TIMESTAMP '{ts}'"""


def alert_text(d, double: dict, unexpl: dict, emails: dict) -> tuple:
    lines = [f"Sūtīšanas diena {d}: šiem kontaktiem šodien NEKAS netiek sūtīts (D47). Atrisini ar Claude, tad sūti ar roku.",
             ""]
    for mk, ts in sorted(double.items()):
        lines.append(f"{DOUBLE}  {mk}  {emails.get(mk, '')}  vēstules: {', '.join(ts)}  (2+ vēstules vienā dienā)")
    for mk, why in sorted(unexpl.items()):
        lines.append(f"{UNEXPLAINED}  {mk}  {emails.get(mk, '')}  {why}  (nav pirmdienas plānā, nav pasūtījuma)")
    subj = f"D47 sūtīšanas diena {d}: {len(double)} dubulti, {len(unexpl)} nepaskaidrotas izmaiņas - nekas nav sūtīts"
    return subj, "\n".join(lines)


# ------------------------------------------------------------------------------------------------ D44
# RAIVIS D44 (18:05): "first the shop prices are calculated, then the cabinet, then it appears in Brevo." On a send
# day the commercial letters (and 236) go only when the whole chain ran TODAY and IN ORDER:
#   week-price run (kind weekly, LIVE, finished today, OK, 0 mismatches)
#   < cabinet run written (cab-YYYYMMDD of today)  < letter_fields run (today, OK)  < ak-writer push (today, LIVE OK)
#   236 also: ak_tuesday_log today BUILT with today's week-price run and assembled_built_at > mirror_ingested_at
#   and today's cabinet prices: 0 below MIN, 0 at/above the NEW shop price, 0 deeper than the rung.
# Any fail -> those types are held (non-commercial types unaffected) + KĻŪDA; reason codes D44_*.
COMMERCIAL = {"reorder_1", "active_xsell", "winback_1", "winback_2", "winback_3",
              "winback_1_e2", "winback_2_e2", "winback_3_e2", "akcija_236"}
RUNG_DEPTH = {"r1": 0.10, "r2": 0.15, "r3": 0.20, "xs_intro": 0.105}   # xs_intro: 10 % + rounding to x9 (measured 10.42 %)


def d44_facts_sql(d: dt.date) -> str:
    cab = f"cab-{d:%Y%m%d}"
    return f"""
WITH wp AS (SELECT run_id, finished_at FROM `{M}.week_price_run_summary`
            WHERE kind = 'weekly' AND mode = 'LIVE' AND status = 'OK' AND IFNULL(n_mismatch, 0) = 0
              AND DATE(finished_at, 'Europe/Riga') = DATE '{d}' ORDER BY finished_at DESC LIMIT 1),
cab AS (SELECT MAX(written_at) AS t_at,
               COUNTIF(cabinet_price_gross < min_gross - 0.005) AS below_min,
               COUNTIF(cabinet_price_gross >= shop_gross) AS ge_shop,
               COUNTIF(SAFE_DIVIDE(shop_gross - cabinet_price_gross, shop_gross) >
                       CASE price_role WHEN 'r1' THEN {RUNG_DEPTH['r1']} WHEN 'r2' THEN {RUNG_DEPTH['r2']}
                                       WHEN 'r3' THEN {RUNG_DEPTH['r3']} WHEN 'xs_intro' THEN {RUNG_DEPTH['xs_intro']}
                                       ELSE 0 END + 0.0005) AS too_deep
        FROM `{M}.cabinet_price_current` WHERE run_id = '{cab}'),
lf AS (SELECT MAX(finished_at) AS t_at FROM `{M}.letter_fields_log` WHERE plan_date = DATE '{d}' AND status = 'OK'),
ak AS (SELECT MAX(finished_at) AS t_at FROM `{M}.block_push_log`
       WHERE run_date = DATE '{d}' AND mode = 'LIVE' AND status = 'OK' AND IFNULL(errors, 0) = 0),
tl AS (SELECT status, week_price_run_id, mirror_ingested_at, assembled_built_at FROM `{M}.ak_tuesday_log`
       WHERE run_date = DATE '{d}' AND mode = 'LIVE' ORDER BY logged_at DESC LIMIT 1)
SELECT (SELECT run_id FROM wp) AS wp_run, (SELECT finished_at FROM wp) AS wp_at, cab.t_at AS cab_at,
       cab.below_min, cab.ge_shop, cab.too_deep, lf.t_at AS lf_at, ak.t_at AS ak_at,
       (SELECT status FROM tl) AS ak_status, (SELECT week_price_run_id FROM tl) AS ak_wp_run,
       (SELECT mirror_ingested_at FROM tl) AS ak_mirror_at, (SELECT assembled_built_at FROM tl) AS ak_assembled_at
FROM cab, lf, ak"""


def _t(v):
    if v in (None, ""):
        return None
    if isinstance(v, dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)
    s = str(v).replace(" UTC", "").replace("Z", "+00:00")
    x = dt.datetime.fromisoformat(s)
    return x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)


def d44_eval(f: dict) -> dict:
    """f = one row of d44_facts_sql. -> {"commercial": [codes], "akcija_236": [codes]} (empty lists = pass)."""
    wp, cab, lf, ak = _t(f.get("wp_at")), _t(f.get("cab_at")), _t(f.get("lf_at")), _t(f.get("ak_at"))
    c = []
    if not wp:
        c.append("D44_WEEK_PRICE_NOT_OK")
    if not cab:
        c.append("D44_NO_CAB_RUN_TODAY")
    elif wp and cab <= wp:
        c.append("D44_CAB_BEFORE_WEEK_PRICE")
    if not lf:
        c.append("D44_NO_LETTER_FIELDS_TODAY")
    elif cab and lf <= cab:
        c.append("D44_LETTER_FIELDS_BEFORE_CAB")
    if not ak:
        c.append("D44_NO_AK_WRITER_TODAY")
    elif lf and ak <= lf:
        c.append("D44_AK_WRITER_BEFORE_LETTER_FIELDS")
    for k, code in (("below_min", "D44_PRICE_BELOW_MIN"), ("ge_shop", "D44_PRICE_NOT_BELOW_NEW_SHOP"),
                    ("too_deep", "D44_PRICE_DEEPER_THAN_RUNG")):
        if int(f.get(k) or 0):
            c.append(f"{code} {int(f[k])}")
    a = list(c)
    if not (f.get("ak_status") == "BUILT" and f.get("wp_run") and f.get("ak_wp_run") == f.get("wp_run")
            and _t(f.get("ak_assembled_at")) and _t(f.get("ak_mirror_at"))
            and _t(f.get("ak_assembled_at")) > _t(f.get("ak_mirror_at"))):
        a.append("D44_AK_NOT_BUILT")
    return {"commercial": c, "akcija_236": a}
