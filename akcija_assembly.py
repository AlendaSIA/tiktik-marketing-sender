"""Weekly akcija (template 236) PER CONTACT - selector, block picker, assembly. SHADOW ONLY.

Contract: _CONTRACT-v2.8--price-fields.md, sections WEEKLY AKCIJA PER CONTACT v1 ... v1.5 (file sha256[:12]
7ae3f6db1a9f). Sūtīšanas dzinējs 6, MAIN's block of 2026-10-07.

What it does, once a day AFTER the 08:55 send-time run (WA8 v1.4: at send time, never at plan time):
  week      = the Tuesday on or before the letter date; week_id = ISO year-week of that Tuesday; the week ends on
              the Monday after (WA7a v1.3). The maker is the row of mkt_control.akcija_week (owner MAIN).
  audience  = today's rows of mkt_control.shadow_akcija_audience (latest run) with in_audience.
  own block = the contact's rows of mkt_control.akcija_own_goods of the SAME plan_date (never yesterday's): maker
              week = that maker by rnk_maker, MIX week = every maker by rnk; at most 12 (WA7b).
  blocks    = mkt_control.akcija_week_blocks rows of the maker (owner MAIN; no rows = DEFAULT auto_cat:1..4, top 4),
              filled from the week's goods on sale, ordered by buyers (WA7d v1.5: COUNT(DISTINCT master_key) per
              handle in business_marts.customer_bought_products), after the drop of WA6 (a good SHOWN in the own
              block is not shown below; a block is filled from the next goods; a block with 0 goods is not shown).
  result    = mkt_control.akcija_assembled, grain (plan_date, email), params = uzruna, h1, lidz, main_url, own[],
              blocks[] (WA8 v1.4).

NOTHING is delivered: no Brevo call, no send_log row, no sequence state, no Pipedrive write. Delivery (WA8a) is not
decided and nothing here builds it. The only writes are today's partition of akcija_assembled and today's
akcija_asm_* rows of mkt_control.shadow_selfcheck.
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
import os
import re
import subprocess
import sys
import tempfile

P = "jaunais-za-aizv04022026"
SFX = os.environ.get("SHADOW_TABLE_SUFFIX", "")
T_OUT = f"{P}.mkt_control.akcija_assembled{SFX}"
T_CHECK = f"{P}.mkt_control.shadow_selfcheck{SFX}"
T_WEEK = f"{P}.mkt_control.akcija_week"
T_BLOCKS = f"{P}.mkt_control.akcija_week_blocks"
T_OWN = f"{P}.mkt_control.akcija_own_goods"
T_OWNLOG = f"{P}.mkt_control.akcija_own_goods_log"
T_AUD = f"{P}.mkt_control.shadow_akcija_audience"
T_LF = f"{P}.mkt_control.letter_fields"

MAIN_URL = "https://www.tiktik.lv/"
MIX = "MIX"
OWN_CAP = 12                                                     # WA7b
DEFAULT_RULES = [{"block_no": k, "title": None, "selector": f"auto_cat:{k}", "top_n": 4, "max_per_kind": None,
                  "max_per_category": None} for k in (1, 2, 3, 4)]        # WA7d v1.4
MONTH_DATIVE = ["janvārim", "februārim", "martam", "aprīlim", "maijam", "jūnijam", "jūlijam", "augustam",
                "septembrim", "oktobrim", "novembrim", "decembrim"]
CABINET = re.compile(r"kabinets\.php|plani\.tiktik\.lv", re.I)   # WA1: every link leads to the shop
PARAMS_LIMIT = 100_000                                           # Brevo: params 100 KB per message version


class SelectorError(ValueError):
    pass


# ---------------------------------------------------------------------------------------------- week and texts
def week_of(letter_date: dt.date):
    """-> (tuesday, week_id, ending monday). Tuesday on or before the date (WA7a v1.3, WA8 v1.4)."""
    tue = letter_date - dt.timedelta(days=(letter_date.weekday() - 1) % 7)
    iso = tue.isocalendar()
    return tue, f"{iso[0]}-W{iso[1]:02d}", tue + dt.timedelta(days=6)


def lidz(end: dt.date) -> str:
    return f"pirmdienai, {end.day}. {MONTH_DATIVE[end.month - 1]}"            # WA7c


def h1(maker: str) -> str:
    return "MIX izpārdošana" if maker == MIX else f"{maker} produktu nedēļa"  # WA7c


def price(v) -> str:
    """Display string, gross: 3.5 -> '3,50 €'."""
    d = decimal.Decimal(str(v)).quantize(decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_UP)
    return f"{d:.2f}".replace(".", ",") + " €"


def uzruna(greeting) -> str:
    """The name inside the writer's GREETING ('Sveika, Kristīne!' -> 'Kristīne'); 'Sveiki!' or empty -> ''."""
    m = re.match(r"^[^,]+,\s*(.+?)\s*!?\s*$", greeting or "")
    return m.group(1) if m else ""


def subject(uz: str, top_short, maker: str) -> str:
    """WA5 / WA7c. No '!'. Without a name the leading '<UZRUNA>, ' is left out and the first letter is raised."""
    body = (f"{top_short} un citas {maker} preces šonedēļ par īpaši labām cenām" if top_short
            else f"{maker} preces šonedēļ par īpaši labām cenām")
    s = f"{uz}, {body}" if uz else body[:1].upper() + body[1:]
    return s.replace("!", "").strip()


# --------------------------------------------------------------------------------------------------- selector
_PART = re.compile(r"^(?P<body>.*?)(?:#(?P<n>\d+))?$", re.S)


def parse_selector(text: str) -> list:
    """'<part>;<part>...', each '<source>[#n]': auto_cat:<k> | cat:<uid,uid> | handles:<h,h> | name~<regex> | rest.
    -> [{'src', 'arg', 'n'}]. Anything else raises SelectorError (a rule that cannot be read stops the run)."""
    out = []
    for raw in (text or "").split(";"):
        raw = raw.strip()
        if not raw:
            continue
        m = _PART.match(raw)
        body, n = m.group("body").strip(), (int(m.group("n")) if m.group("n") else None)
        if n is not None and n < 1:
            raise SelectorError(f"#n must be 1 or more: {raw!r}")
        if body == "rest":
            out.append({"src": "rest", "arg": None, "n": n})
        elif body.startswith("auto_cat:"):
            k = body[9:].strip()
            if not k.isdigit() or int(k) < 1:
                raise SelectorError(f"auto_cat needs a number from 1: {raw!r}")
            out.append({"src": "auto_cat", "arg": int(k), "n": n})
        elif body.startswith("cat:") or body.startswith("handles:"):
            src, _, arg = body.partition(":")
            vals = [v.strip() for v in arg.split(",") if v.strip()]
            if not vals:
                raise SelectorError(f"{src} needs at least one value: {raw!r}")
            out.append({"src": src, "arg": vals, "n": n})
        elif body.startswith("name~"):
            try:
                out.append({"src": "name", "arg": re.compile(body[5:], re.I), "n": n})
            except re.error as e:
                raise SelectorError(f"bad regex in {raw!r}: {e}") from None
            if not body[5:]:
                raise SelectorError(f"name~ needs a regex: {raw!r}")
        else:
            raise SelectorError(f"unknown selector part: {raw!r}")
    if not out:
        raise SelectorError("empty selector")
    return out


def parse_rules(rows) -> list:
    """Rows of akcija_week_blocks for one maker -> rules ordered by block_no, selectors parsed."""
    rules = []
    for r in sorted(rows, key=lambda r: int(r["block_no"])):
        top = int(r["top_n"]) if r.get("top_n") not in (None, "") else None
        if not top or top < 1:
            raise SelectorError(f"block {r['block_no']}: top_n must be 1 or more")
        cap = lambda k: int(r[k]) if r.get(k) not in (None, "") else None  # noqa: E731
        rules.append({"block_no": int(r["block_no"]), "title": (r.get("title") or "").strip() or None,
                      "parts": parse_selector(r.get("selector")), "top_n": top,
                      "max_per_kind": cap("max_per_kind"), "max_per_category": cap("max_per_category")})
    if len({r["block_no"] for r in rules}) != len(rules):
        raise SelectorError("block_no repeats")
    return rules


def _pool(part, goods, cat_order, earlier: set) -> list:
    """Candidate goods of one part, in the order they are offered. goods = the week's goods, buyers descending."""
    if part["src"] == "handles":
        by = {g["handle"]: g for g in goods}
        return [by[h] for h in part["arg"] if h in by]                       # fixed products, in the order written
    if part["src"] == "auto_cat":
        cat = cat_order[part["arg"] - 1] if part["arg"] <= len(cat_order) else None
        return [g for g in goods if cat is not None and g["category"] == cat]
    if part["src"] == "cat":
        return [g for g in goods if g["category"] in part["arg"]]
    if part["src"] == "name":
        return [g for g in goods if part["arg"].search(g["name"] or "")]
    return [g for g in goods if g["handle"] not in earlier]                  # rest


def build_pools(rules, goods, cat_order) -> list:
    """Per rule, per part: the candidate lists. Same for every contact, so built once.
    'rest' = the week's goods that are in no candidate list of an EARLIER block (not: 'not shown to this contact')."""
    pools, earlier = [], set()
    for r in rules:
        mine = [_pool(p, goods, cat_order, earlier) for p in r["parts"]]
        pools.append(mine)
        earlier |= {g["handle"] for part in mine for g in part}
    return pools


def pick_blocks(rules, pools, shown_own: set, cat_names: dict) -> list:
    """WA6 v1.4 for one contact -> [{'block_no', 'title', 'want', 'items': [good]}], empty blocks left out.
    A good is skipped when it is shown in the own-goods block or in an earlier block; caps count inside the block."""
    shown, out = set(shown_own), []
    for r, parts in zip(rules, pools):
        items, kinds, cats = [], {}, {}
        for p, cand in zip(r["parts"], parts):
            taken = 0
            for g in cand:
                if len(items) >= r["top_n"] or (p["n"] is not None and taken >= p["n"]):
                    break
                if g["handle"] in shown:
                    continue
                if r["max_per_kind"] is not None and kinds.get(g["kind"], 0) >= r["max_per_kind"]:
                    continue
                if r["max_per_category"] is not None and cats.get(g["category"], 0) >= r["max_per_category"]:
                    continue
                items.append(g)
                shown.add(g["handle"])
                taken += 1
                kinds[g["kind"]] = kinds.get(g["kind"], 0) + 1
                cats[g["category"]] = cats.get(g["category"], 0) + 1
        if items:
            title = r["title"] or cat_names.get(items[0]["category"]) or ""
            out.append({"block_no": r["block_no"], "title": title, "want": r["top_n"], "items": items})
    return out


# --------------------------------------------------------------------------------------------------- assembly
def _item(name, img, url, sale, std) -> dict:
    return {"name": name, "img": img, "url": url, "sale": price(sale), "std": price(std)}


def own_block(rows, maker: str) -> list:
    """The contact's own goods of the week, at most 12 (WA4 v1.2, WA7b)."""
    if maker == MIX:
        mine = sorted(rows, key=lambda r: int(r["rnk"]))
    else:
        mine = sorted((r for r in rows if r["maker"] == maker), key=lambda r: int(r["rnk_maker"]))
    return mine[:OWN_CAP]


def assemble_one(aud, own_rows, greeting, *, maker, week_id, end, rules, pools, cat_names, plan_date) -> dict:
    own = own_block(own_rows, maker)
    blocks = pick_blocks(rules, pools, {r["handle"] for r in own}, cat_names)
    uz = uzruna(greeting)
    subj = subject(uz, own[0]["name_short"] if own else None, maker)
    params = {"uzruna": uz, "h1": h1(maker), "lidz": lidz(end), "main_url": MAIN_URL,
              "own": [_item(r["name"], r["img"], r["url"], r["sale_gross"], r["std_gross"]) for r in own],
              "blocks": [{"title": b["title"], "items": [_item(g["name"], g["img"], g["url"], g["sale"], g["std"])
                                                         for g in b["items"]]} for b in blocks]}
    n_goods = sum(len(b["items"]) for b in blocks)
    reasons = []
    if not own and not blocks:
        reasons.append("NOTHING_TO_SHOW")
    if not subj:
        reasons.append("SUBJECT_EMPTY")
    if len(json.dumps(params, ensure_ascii=False).encode()) > PARAMS_LIMIT:
        reasons.append("PARAMS_TOO_BIG")
    notes = ([] if own else ["NO_OWN_GOODS"]) + ([] if uz else ["NO_UZRUNA"])
    return {"plan_date": plan_date.isoformat(), "email": aud["email"], "mk": aud["master_key"], "week_id": week_id,
            "maker": maker, "subject": subj, "params": params, "n_own": len(own), "n_blocks": len(blocks),
            "n_block_goods": n_goods, "status": "REFUSED" if reasons else "OK",
            "reason": ";".join(reasons + notes) or None,
            "_short": sum(len(b["items"]) < b["want"] for b in blocks), "_missing": len(rules) - len(blocks)}


def _row(name, level, ok, value, detail=None) -> dict:
    return {"check_name": name, "level": level, "ok": bool(ok), "value": str(value),
            "detail": detail if detail is None or isinstance(detail, str)
            else json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str)}


def checks(rows, *, audience_n, week_id, maker, rules_source, goods_n, goods_dropped, audience_week) -> list:
    """Self-check rows of one assembly (close handover 2026-10-06, section 3)."""
    sizes = sorted(len(json.dumps(r["params"], ensure_ascii=False).encode()) for r in rows) or [0]
    p95 = sizes[min(len(sizes) - 1, int(len(sizes) * 0.95))]
    twice, cab, bang = [], [], []
    for r in rows:
        urls = [i["url"] for i in r["params"]["own"]] + [i["url"] for b in r["params"]["blocks"] for i in b["items"]]
        if len(urls) != len(set(urls)):
            twice.append(r["email"])
        if any(CABINET.search(u or "") for u in urls + [r["params"]["main_url"]]):
            cab.append(r["email"])
        if "!" in r["subject"]:
            bang.append(r["email"])
    empty_subj = [r["email"] for r in rows if not r["subject"]]
    nothing = sum("NOTHING_TO_SHOW" in (r["reason"] or "") for r in rows)
    return [
        _row("akcija_asm_rows_equal_audience", "hard", len(rows) == audience_n, len(rows), {"audience": audience_n}),
        _row("akcija_asm_subject_empty", "hard", not empty_subj, len(empty_subj), empty_subj[:20]),
        _row("akcija_asm_subject_has_exclamation", "hard", not bang, len(bang), bang[:20]),
        _row("akcija_asm_cabinet_url_found", "hard", not cab, len(cab), cab[:20]),
        _row("akcija_asm_good_shown_twice", "hard", not twice, len(twice), twice[:20]),
        _row("akcija_asm_params_bytes", "hard", sizes[-1] <= PARAMS_LIMIT, sizes[-1],
             {"max": sizes[-1], "p95": p95, "limit": PARAMS_LIMIT}),
        _row("akcija_asm_nothing_to_show", "warn", nothing == 0, nothing),
        _row("akcija_asm_short_blocks", "info", True, sum(r["_short"] > 0 for r in rows),
             {"contacts_with_a_short_block": sum(r["_short"] > 0 for r in rows),
              "contacts_with_a_block_not_shown": sum(r["_missing"] > 0 for r in rows),
              "contacts_with_no_block": sum(r["n_blocks"] == 0 for r in rows)}),
        _row("akcija_asm_own_goods", "info", True, sum(r["n_own"] > 0 for r in rows),
             {"with_own": sum(r["n_own"] > 0 for r in rows), "without_own": sum(r["n_own"] == 0 for r in rows),
              "at_cap_12": sum(r["n_own"] == OWN_CAP for r in rows),
              "no_uzruna": sum("NO_UZRUNA" in (r["reason"] or "") for r in rows)}),
        _row("akcija_asm_week", "info", True, f"{week_id} {maker}",
             {"rules": rules_source, "week_goods": goods_n, "goods_dropped": goods_dropped,
              "audience_offer_week": audience_week, "audience_is_for_this_week": audience_week == week_id}),
    ]


# -------------------------------------------------------------------------------------------------------- I/O
def sql_goods(maker: str) -> str:
    """The week's goods on sale, buyers descending. A good needs to be sellable now, with a shop url and a picture."""
    only = "" if maker == MIX else f"AND m.pg = '{maker}'"
    return f"""
WITH m AS (SELECT handle, ANY_VALUE(promo_group) AS pg FROM `{P}.business_marts.product_maker` GROUP BY 1),
s AS (SELECT DISTINCT handle FROM `{P}.business_marts.shop_sellable_product`),
b AS (SELECT handle, COUNT(DISTINCT master_key) AS buyers FROM `{P}.business_marts.customer_bought_products` GROUP BY 1)
SELECT c.handle, c.name, c.image AS img, c.url, CAST(c.eff_price AS STRING) AS sale, CAST(c.std_price AS STRING) AS std,
       c.category_handle AS category, c.cat_leaf_slug AS kind, IFNULL(b.buyers, 0) AS buyers, m.pg AS maker,
       s.handle IS NOT NULL AS sellable
FROM `{P}.business_marts.product_catalog` c
JOIN m USING (handle) LEFT JOIN s USING (handle) LEFT JOIN b USING (handle)
WHERE c.on_sale AND c.eff_price < c.std_price {only}
ORDER BY buyers DESC, c.handle"""


def sql_cat_order(maker: str) -> str:
    """Shop categories of the week's usable goods, by distinct buyers of those goods, descending (auto_cat:<k>)."""
    only = "" if maker == MIX else f"AND m.pg = '{maker}'"
    return f"""
WITH m AS (SELECT handle, ANY_VALUE(promo_group) AS pg FROM `{P}.business_marts.product_maker` GROUP BY 1),
s AS (SELECT DISTINCT handle FROM `{P}.business_marts.shop_sellable_product`),
g AS (SELECT c.handle, c.category_handle FROM `{P}.business_marts.product_catalog` c JOIN m USING (handle)
      JOIN s USING (handle)
      WHERE c.on_sale AND c.eff_price < c.std_price AND c.url IS NOT NULL AND c.image IS NOT NULL {only})
SELECT g.category_handle AS category, ANY_VALUE(k.name) AS name, COUNT(DISTINCT g.handle) AS goods,
       COUNT(DISTINCT x.master_key) AS buyers
FROM g LEFT JOIN `{P}.business_marts.customer_bought_products` x USING (handle)
LEFT JOIN `{P}.channel_raw.mozello_categories` k ON k.id = g.category_handle
GROUP BY 1 ORDER BY buyers DESC, category"""


def _b(v) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


def _sql_text(v) -> str:
    return "NULL" if v is None else "'" + str(v).replace("\\", " ").replace("'", " ").replace("\n", " ") + "'"


def record_checks(query, day, run_id, rows):
    d = day.isoformat()
    query(f"DELETE FROM `{T_CHECK}` WHERE plan_date = DATE '{d}' AND check_name LIKE 'akcija\\\\_asm\\\\_%'")
    vals = ", ".join(f"(DATE '{d}', {_sql_text(run_id)}, {_sql_text(c['check_name'])}, {_sql_text(c['level'])}, "
                     f"{'TRUE' if c['ok'] else 'FALSE'}, {_sql_text(c['value'])}, {_sql_text(c['detail'])}, "
                     f"CURRENT_TIMESTAMP())" for c in rows)
    query(f"INSERT INTO `{T_CHECK}` (plan_date, run_id, check_name, level, ok, value, detail, checked_at) "
          f"VALUES {vals}")


def load_partition(day, rows):
    """Replace today's partition of akcija_assembled in ONE load job (atomic; the other days stay)."""
    with tempfile.NamedTemporaryFile("w", suffix=".ndjson", delete=False, encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({**{k: v for k, v in r.items() if not k.startswith("_")},
                                "params": json.dumps(r["params"], ensure_ascii=False)}, ensure_ascii=False) + "\n")
        path = f.name
    table = T_OUT.split(".", 1)[1] + "$" + day.strftime("%Y%m%d")
    out = subprocess.run(["bq", "--project_id", P, "load", "--quiet", "--replace",
                          "--source_format=NEWLINE_DELIMITED_JSON", table, path],
                         capture_output=True, text=True, timeout=300)
    os.unlink(path)
    if out.returncode:
        raise RuntimeError("bq load failed: " + (out.stdout + out.stderr)[:600])


def run(query, day: dt.date, record: bool, what_if_maker=None) -> dict:
    """what_if_maker: measure another maker on today's data (read-only; refused together with record)."""
    if what_if_maker and record:
        raise ValueError("a what-if maker is never recorded")
    d = day.isoformat()
    tue, week_id, end = week_of(day)
    aud_run = query(f"SELECT run_id, ANY_VALUE(offer_week) AS offer_week FROM `{T_AUD}` WHERE plan_date = DATE '{d}' "
                    f"GROUP BY run_id ORDER BY MAX(planned_at) DESC LIMIT 1")
    run_id = aud_run[0]["run_id"] if aud_run else "none"
    res = {"plan_date": d, "week_id": week_id, "week_start": tue.isoformat(), "week_end": end.isoformat(),
           "plan_run": run_id, "recorded": False}

    def stop(name, level, why, detail=None):
        res.update({"status": "NO_LETTERS", "why": why})
        if record:
            if query(f"SELECT 1 AS x FROM `{T_OUT}` WHERE plan_date = DATE '{d}' LIMIT 1"):
                query(f"DELETE FROM `{T_OUT}` WHERE plan_date = DATE '{d}'")      # never leave an older answer
            record_checks(query, day, run_id, [_row(name, level, level != "hard", why, detail)])
            res["recorded"] = True
        return res

    wk = query(f"SELECT maker FROM `{T_WEEK}` WHERE week_id = '{week_id}'")
    if len(wk) != 1:
        return stop("akcija_asm_no_week_row", "info", f"no akcija_week row for {week_id}: no akcija letters today",
                    {"rows": len(wk)})
    maker = what_if_maker or wk[0]["maker"]
    log = query(f"SELECT status, run_id FROM `{T_OWNLOG}` WHERE plan_date = DATE '{d}' ORDER BY finished_at DESC LIMIT 1")
    if not log or log[0]["status"] != "OK":
        return stop("akcija_asm_own_goods_run_missing", "hard",
                    f"akcija_own_goods of {d} is missing or failed: refused, yesterday's is never used",
                    {"log": log[:1]})
    if not aud_run:
        return stop("akcija_asm_no_audience", "hard", f"no shadow_akcija_audience rows for {d}")
    try:
        brows = query(f"SELECT block_no, title, selector, top_n, max_per_kind, max_per_category FROM `{T_BLOCKS}` "
                      f"WHERE maker = '{maker}' ORDER BY block_no")
        rules = parse_rules(brows or DEFAULT_RULES)
    except SelectorError as e:
        return stop("akcija_asm_block_rules_unreadable", "hard", f"{maker}: {e}")
    all_goods = query(sql_goods(maker))
    goods = [g for g in all_goods if _b(g["sellable"]) and g["url"] and g["img"]]
    dropped = {"not_sellable": sum(not _b(g["sellable"]) for g in all_goods),
               "no_shop_url": [g["handle"] for g in all_goods if _b(g["sellable"]) and not g["url"]],
               "no_picture": [g["handle"] for g in all_goods if _b(g["sellable"]) and g["url"] and not g["img"]]}
    cats = query(sql_cat_order(maker))
    cat_order, cat_names = [c["category"] for c in cats], {c["category"]: c["name"] for c in cats}
    pools = build_pools(rules, goods, cat_order)
    aud = query(f"SELECT LOWER(TRIM(email)) AS email, master_key FROM `{T_AUD}` WHERE plan_date = DATE '{d}' "
                f"AND run_id = '{run_id}' AND in_audience")
    own = {}
    for r in query(f"SELECT LOWER(TRIM(email)) AS email, maker, rnk, rnk_maker, handle, name, name_short, img, url, "
                   f"CAST(sale_gross AS STRING) AS sale_gross, CAST(std_gross AS STRING) AS std_gross "
                   f"FROM `{T_OWN}` WHERE plan_date = DATE '{d}'"):
        own.setdefault(r["email"], []).append(r)
    greet = {r["email"]: r["GREETING"] for r in query(
        f"SELECT LOWER(TRIM(email)) AS email, ANY_VALUE(GREETING HAVING MAX built_at) AS GREETING FROM `{T_LF}` "
        f"WHERE plan_date = DATE '{d}' GROUP BY 1")}
    rows = [assemble_one(a, own.get(a["email"], []), greet.get(a["email"]), maker=maker, week_id=week_id, end=end,
                         rules=rules, pools=pools, cat_names=cat_names, plan_date=day) for a in aud]
    cs = checks(rows, audience_n=len(aud), week_id=week_id, maker=maker,
                rules_source="akcija_week_blocks" if brows else "DEFAULT", goods_n=len(goods), goods_dropped=dropped,
                audience_week=aud_run[0]["offer_week"])
    res.update({"status": "ASSEMBLED", "maker": maker, "rows": len(rows), "ok": sum(r["status"] == "OK" for r in rows),
                "refused": sum(r["status"] != "OK" for r in rows), "week_goods": len(goods), "categories": cats,
                "rules": "akcija_week_blocks" if brows else "DEFAULT", "checks": cs})
    if record:
        load_partition(day, rows)
        record_checks(query, day, run_id, cs)
        res["recorded"] = True
    return res


def main(argv) -> int:
    import argparse
    import zoneinfo
    import send_lookups as L
    ap = argparse.ArgumentParser(description="weekly akcija 236 per contact: assembly in shadow (no delivery)")
    ap.add_argument("--record", action="store_true", help="write akcija_assembled (today) and the akcija_asm_* rows")
    ap.add_argument("--date", help="letter date YYYY-MM-DD (default: today, Europe/Riga)")
    ap.add_argument("--what-if-maker", help="measure this maker instead of the week's (never with --record)")
    a = ap.parse_args(argv)
    day = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(zoneinfo.ZoneInfo("Europe/Riga")).date()
    q = L.cli_query if os.environ.get("BQ_TRANSPORT") == "cli" else L.rest_query
    res = run(q, day, a.record, a.what_if_maker)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
