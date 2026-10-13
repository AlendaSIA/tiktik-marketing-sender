"""S5 CARD GATE — R1 Tuesday chain contract (Raivis, MAIN 3 13.10.2026; wording fixed, do not change):

  S5 send-day -> start only after S4 OK; gate checks EVERY rendered card (P and R, FLOW*) == cab-<YYYYMMDD>

What "rendered card" means here: the cards Brevo will render for the contact = the FLOW block fields the ak-writer
pushed (mkt_control.block_fields_today, FLOW_HTML / FLOW2_HTML / FLOWS_HTML / FLOWS2_HTML / FLOWR_HTML), parsed card by
card (name + shown price + struck REF price). A card is checked against today's cabinet book
(mkt_control.cabinet_price_current, run_id cab-<YYYYMMDD>, status 'written'):

  * the card names a good of the contact's letter (today's latest mkt_control.letter_fields_slots row, same name) whose
    SKU has a cabinet row for this contact  -> the shown price must equal the cabinet price (to the cent);
  * the card shows a personal price (struck REF price) but no cabinet row stands behind it -> mismatch;
  * a plain shop-price card with no cabinet row -> not a cabinet card, nothing to compare.

Any mismatch holds THAT contact in every type whose template reads a FLOW field (the contact falls back to 236 by D46);
the block rows must also come from today's FLOW build made after today's OK letter_fields run (else every contact of
those types is held). Pure functions + the SQL texts; send_day wires them.
"""
from __future__ import annotations

import datetime as dt
import html as _html
import re

P = "jaunais-za-aizv04022026"
M = f"{P}.mkt_control"
FLOW_FIELDS = ("FLOW_HTML", "FLOW2_HTML", "FLOWS_HTML", "FLOWS2_HTML", "FLOWR_HTML")
HOLD = "S5_CARD_NE_CAB"
HOLD_FLOW = "S5_FLOW_NOT_AFTER_LF"
_NUM = re.compile(r"(\d{1,3}(?:[ . ]\d{3})*,\d{2}|\d+,\d{2}|\d+\.\d{2})")
_REF = re.compile(r'text-decoration:line-through;font-size:13px;">([^<]*)</span>\s*<span[^>]*>([^<]*)</span>')
_PLAIN = re.compile(r'font-weight:bold;font-size:15px;">([^<]*)</div>')


def money(txt):
    """'21,99 €' / 'no 4,39 €' / '1 234,50 €' -> 21.99 / 4.39 / 1234.5; None if no amount."""
    m = _NUM.findall(txt or "")
    if not m:
        return None
    s = m[-1].replace(" ", "").replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    return round(float(s), 2)


def parse_cards(html_text):
    """-> [{"name", "price", "ref"}] in the order Brevo renders them. One card = one <img alt="name"> followed by its
    price block (struck REF + personal price, or a plain price) before the next card's image."""
    out = []
    if not html_text:
        return out
    parts = html_text.split('alt="')[1:]
    for p in parts:
        name, _, rest = p.partition('"')
        m = _REF.search(rest)
        pm = _PLAIN.search(rest)
        if m and (not pm or m.start() < pm.start()):
            out.append({"name": _html.unescape(name), "price": money(m.group(2)), "ref": money(m.group(1)),
                        "price_txt": m.group(2).strip()})
        elif pm:
            out.append({"name": _html.unescape(name), "price": money(pm.group(1)), "ref": None,
                        "price_txt": pm.group(1).strip()})
        else:
            out.append({"name": _html.unescape(name), "price": None, "ref": None, "price_txt": ""})
    return out


def contact_verdict(block_row, slots, cab, stats=None):
    """block_row: {FLOW_*: html}; slots: [{name, sku, email_type}] of the contact's letter today;
    cab: {(SKU, email_type): price} (a row of another letter type never vouches for this letter's card).
    -> None (all rendered cards == cabinet) or a reason string."""
    by_name = {}
    for s in slots or []:
        if s.get("name") and s.get("sku"):
            by_name.setdefault(s["name"].strip(), set()).add((str(s["sku"]).strip().upper(), s.get("email_type")))
    bad = []
    for f in FLOW_FIELDS:
        for c in parse_cards((block_row or {}).get(f) or ""):
            skus = by_name.get(c["name"].strip(), set())
            want = {cab[k] for k in skus if k in cab}
            if stats is not None:
                stats["cards"] = stats.get("cards", 0) + 1
                stats["vs_cab"] = stats.get("vs_cab", 0) + bool(want)
                stats["personal"] = stats.get("personal", 0) + (c["ref"] is not None)
            if want:
                if c["price"] is None or all(abs(c["price"] - w) > 0.005 for w in want):
                    bad.append(f"{f}:{c['name'][:40]} shown {c['price_txt'] or '-'} cab {min(want):.2f}")
            elif c["ref"] is not None:
                bad.append(f"{f}:{c['name'][:40]} personal price {c['price_txt']} without a cabinet row")
    return (f"{HOLD} {len(bad)}: " + "; ".join(bad[:3])) if bad else None


def facts_sql(d: dt.date, dry: bool = False) -> str:
    """Today's FLOW build vs today's OK letter_fields run (S4 after S3). dry: the DRY block rows / log (Mon DRY)."""
    mode = "DRY" if dry else "LIVE"
    return f"""
SELECT (SELECT MAX(finished_at) FROM `{M}.letter_fields_log` WHERE plan_date = DATE '{d}' AND status = 'OK'
          AND JSON_VALUE(counts, '$.cab_run_id') = 'cab-{d:%Y%m%d}' AND JSON_VALUE(counts, '$.cab_ok') = 'true') AS lf_at,
       (SELECT AS STRUCT run_id, finished_at FROM `{M}.block_push_log`
         WHERE run_date = DATE '{d}' AND block = 'FLOW_HTML' AND mode = '{mode}' AND status = 'OK' AND IFNULL(errors, 0) = 0
         ORDER BY finished_at DESC LIMIT 1) AS flow,
       (SELECT ANY_VALUE(run_id) FROM `{M}.block_fields_today{'_dry' if dry else ''}`) AS table_run"""


def rows_sql(d: dt.date, emails, dry: bool = False) -> tuple:
    lst = ",".join("'" + e.replace("'", "") + "'" for e in sorted(emails)) or "''"
    cols = ", ".join(FLOW_FIELDS)
    blocks = (f"SELECT LOWER(TRIM(email)) AS em, run_id, {cols} FROM `{M}.block_fields_today{'_dry' if dry else ''}` "
              f"WHERE LOWER(TRIM(email)) IN ({lst})")
    slots = (f"SELECT em, name, sku, email_type FROM `{M}.letter_fields_slots` WHERE plan_date = DATE '{d}' AND em IN ({lst}) "
             f"AND in_letter AND sku IS NOT NULL "
             f"AND run_id = (SELECT MAX(run_id) FROM `{M}.letter_fields_slots` WHERE plan_date = DATE '{d}')")
    cab = (f"SELECT LOWER(TRIM(email)) AS em, UPPER(TRIM(sku)) AS sku, email_type, ANY_VALUE(cabinet_price_gross) AS price "
           f"FROM `{M}.cabinet_price_current` WHERE run_id = 'cab-{d:%Y%m%d}' AND status = 'written' "
           f"AND LOWER(TRIM(email)) IN ({lst}) GROUP BY 1, 2, 3")
    return blocks, slots, cab


def _aware(v):
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)
    x = dt.datetime.fromisoformat(str(v).replace(" UTC", "").replace("Z", "+00:00"))
    return x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)


def flow_order_hold(f: dict):
    """f = one row of facts_sql. -> None or the whole-type hold reason (FLOW build not after today's OK lf run)."""
    lf, flow = _aware(f.get("lf_at")), f.get("flow") or {}
    fat = _aware(flow.get("finished_at")) if flow else None
    if lf is None:
        return f"{HOLD_FLOW} no OK letter_fields run on today's cabinet"
    if fat is None:
        return f"{HOLD_FLOW} no FLOW_HTML build today"
    if fat <= lf:
        return f"{HOLD_FLOW} FLOW build {flow.get('run_id')} ({fat:%H:%M:%S}Z) is not after letter_fields ({lf:%H:%M:%S}Z)"
    if f.get("table_run") and flow.get("run_id") and f["table_run"] != flow["run_id"]:
        return f"{HOLD_FLOW} block_fields_today holds {f['table_run']}, latest OK FLOW build is {flow['run_id']}"
    return None


def evaluate(q, d: dt.date, emails, dry: bool = False, chunk: int = 1500) -> dict:
    """-> {"order_hold": str|None, "holds": {email: reason}, "checked": n, "cards": n}. q(sql) -> rows."""
    f = [dict(r) for r in q(facts_sql(d, dry))]
    f = f[0] if f else {}
    if f.get("flow") is not None and not isinstance(f["flow"], dict):
        f["flow"] = dict(f["flow"])
    out = {"order_hold": flow_order_hold(f), "holds": {}, "checked": 0, "cards": 0, "stats": {},
           "facts": {k: (str(v) if v is not None else None) for k, v in f.items()}}
    ems = sorted({(e or "").strip().lower() for e in emails if e})
    for i in range(0, len(ems), chunk):
        part = ems[i:i + chunk]
        b_sql, s_sql, c_sql = rows_sql(d, part, dry)
        blocks = {r["em"]: dict(r) for r in q(b_sql)}
        slots, cab = {}, {}
        for r in q(s_sql):
            slots.setdefault(r["em"], []).append({"name": r["name"], "sku": r["sku"], "email_type": r["email_type"]})
        for r in q(c_sql):
            cab.setdefault(r["em"], {})[(r["sku"], r["email_type"])] = round(float(r["price"]), 2)
        for e in part:
            row = blocks.get(e)
            if not row:
                continue
            out["checked"] += 1
            why = contact_verdict(row, slots.get(e), cab.get(e, {}), out["stats"])
            if why:
                out["holds"][e] = why
    out["cards"] = out["stats"].get("cards", 0)
    return out
