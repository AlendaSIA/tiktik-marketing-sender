"""Daily self-check of the shadow plan (Sūtīšanas dzinējs 4, MAIN command 2026-10-05 16:10 item 5). Pure.

Written with every plan to mkt_control.shadow_selfcheck, one row per check:
  level 'hard'  - the plan broke one of its own rules. Any hard failure -> ONE dash row that day.
  level 'warn'  - an input is stale or a seam is still open. Recorded, never a dash row (known seams would
                  otherwise post a row every day while everything is DRY).
  level 'info'  - the counts: per email type, per blocking reason, and the difference to yesterday with reasons.
"""
from __future__ import annotations

import json

import sequence as S

MAX_AGE_H = {"rung_price": 26, "lqxs_price": 26, "pd_persons": 30, "en_list_snapshot": 30,
             "flow_classification": 24 * 8, "pd_orgs_mirror": 30}


def count_by(rows, keys) -> dict:
    out = {}
    for r in rows:
        k = " | ".join(str(r.get(x)) for x in keys)
        out[k] = out.get(k, 0) + 1
    return out


def _row(name, level, ok, value, detail=None):
    return {"check_name": name, "level": level, "ok": bool(ok), "value": str(value),
            "detail": detail if detail is None or isinstance(detail, str)
            else json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str)}


def run(plan_rows, akcija_rows, *, prev_counts, flows, en_masters, suppressed_send_address, suppressed_any_address,
        today, ages_h, no_letter_fields, map_disagreements) -> list:
    ws = [r for r in plan_rows if r["would_send"]]
    wd = [r for r in plan_rows if r.get("would_deliver")]
    out = []
    # ---- hard: the plan's own rules
    out.append(_row("suppressed_among_would_send", "hard", suppressed_send_address == 0 and suppressed_any_address == 0,
                    suppressed_any_address, {"send_address": suppressed_send_address,
                                             "any_address_of_the_person": suppressed_any_address}))
    bad = [r["master_key"] for r in ws if flows.get(r["master_key"]) in S.FLOW_HOLD]
    out.append(_row("b2b_lead_among_would_send", "hard", not bad, len(bad), bad[:20]))
    bad = [r["master_key"] for r in ws if r["master_key"] in en_masters]
    out.append(_row("en_among_would_send", "hard", not bad, len(bad), bad[:20]))
    bad = count_by([r for r in plan_rows if r.get("email_type") in S.NEVER_PLANNED], ("email_type",))
    out.append(_row("retired_letters_planned", "hard", not bad, sum(bad.values()), bad))
    bad = [r["master_key"] for r in ws if r.get("template_id") is None or r.get("planned_send_date") is None]
    out.append(_row("would_send_without_template_or_date", "hard", not bad, len(bad), bad[:20]))
    bad = count_by([r for r in ws if r.get("email_type") not in S.INTERFACE_V2], ("email_type",))
    out.append(_row("email_type_outside_interface_v2", "hard", not bad, sum(bad.values()), bad))
    bad = [r["master_key"] for r in wd
           if S.is_price_letter(r["email_type"], r["offer_rung"]) and not r.get("offer_valid_until")]
    out.append(_row("price_letter_deliverable_without_offer_valid_until", "hard", not bad, len(bad), bad[:20]))
    bad = [r["master_key"] for r in wd if r["email_type"] == S.PP1 and not r.get("trigger_order_nr")]
    out.append(_row("244_deliverable_without_order_nr", "hard", not bad, len(bad), bad[:20]))
    bad = [r["master_key"] for r in wd if r["email_type"] == S.XSELL and not r.get("xsell_valid_until")]
    out.append(_row("235_deliverable_without_xsell_valid_until", "hard", not bad, len(bad), bad[:20]))
    # DW1 (contract 9d7c6584cc16): every planned priced letter carries its date - held rows included
    bad = count_by([r for r in plan_rows if r.get("planned_send_date") and (
        (r.get("email_type") in S.LADDER_TYPES and not r.get("offer_valid_until"))
        or (r.get("email_type") == S.XSELL and not r.get("xsell_valid_until")))], ("email_type",))
    out.append(_row("planned_priced_row_without_date", "hard", not bad, sum(bad.values()), bad))
    seen, dup = set(), 0
    for r in plan_rows:
        dup += r["master_key"] in seen
        seen.add(r["master_key"])
    out.append(_row("one_plan_row_per_person", "hard", dup == 0, dup))
    both = [a["master_key"] for a in akcija_rows if a["in_audience"] and a.get("personal_email_type")]
    plan_by = {r["master_key"]: r for r in plan_rows}
    for a in akcija_rows:                     # in the akcija audience AND a deliverable sales letter the same week
        p = plan_by.get(a["master_key"])
        if a["in_audience"] and p and p.get("would_deliver") and p["email_type"] in S.SALES_TYPES \
                and p.get("planned_send_date") and _same_week(p["planned_send_date"], a["send_date"]):
            both.append(a["master_key"])
    out.append(_row("personal_letter_and_akcija_same_week", "hard", not both, len(both), both[:20]))
    # ---- warn: inputs and seams
    for k, limit in MAX_AGE_H.items():
        v = ages_h.get(k)
        out.append(_row(f"fresh_{k}", "warn", v is not None and v <= limit, v, f"limit {limit} h"))
    v = ages_h.get("goods_run_days")
    out.append(_row("goods_run_not_older_than_1_day", "warn", v is not None and v <= 1, v,
                    "writer run of mkt_control.shadow_rung_goods_slots (G15); the hard gate is at send time"))
    out.append(_row("letter_fields_row_today_for_every_would_send", "warn", not no_letter_fields,
                    sum(no_letter_fields.values()), no_letter_fields))   # always open at 08:05: the writer runs 08:40
    out.append(_row("template_map_agrees_with_interface_v2", "warn", not map_disagreements, len(map_disagreements),
                    [{"email_type": e, "engine": t, "map": m} for e, t, m in map_disagreements]))
    # ---- info: the counts and the difference to yesterday
    out.append(_row("count_would_send_by_type", "info", True, len(ws), count_by(ws, ("email_type",))))
    out.append(_row("count_would_deliver_by_type", "info", True, len(wd), count_by(wd, ("email_type",))))
    out.append(_row("count_blocked_by_hold", "info", True, len(plan_rows) - len(ws),
                    count_by([r for r in plan_rows if not r["would_send"]], ("email_type", "hold_reason"))))
    out.append(_row("count_blocked_by_gate", "info", True, len(ws) - len(wd),
                    count_by([r for r in ws if not r.get("would_deliver")], ("email_type", "presend_gate"))))
    out.append(_row("count_akcija_audience", "info", True, sum(a["in_audience"] for a in akcija_rows),
                    count_by([a for a in akcija_rows if not a["in_audience"]], ("excluded_reason",))))
    out.append(_row("diff_vs_yesterday", "info", True, *diff(plan_rows, prev_counts)))
    out.append(_row("diff_per_person", "info", True, sum(r.get("diff_vs_prev") != "same" for r in plan_rows),
                    count_by(plan_rows, ("diff_vs_prev",))))
    return out


def _same_week(a: str, b: str) -> bool:
    import datetime as dt
    x, y = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
    return x.isocalendar()[:2] == y.isocalendar()[:2]


def diff(plan_rows, prev_counts) -> tuple:
    """(number of groups that changed, [{group, yesterday, today, delta}]) - group = type | would_send | hold.
    The group names ARE the reasons: a move shows as -n in one hold and +n in another."""
    key = ("email_type", "would_send", "hold_reason")
    now = count_by(plan_rows, key)
    prev = {" | ".join(str(r.get(x)) for x in key): r["n"] for r in prev_counts}
    rows = [{"group": g, "yesterday": prev.get(g, 0), "today": now.get(g, 0),
             "delta": now.get(g, 0) - prev.get(g, 0)} for g in sorted(set(now) | set(prev))
            if now.get(g, 0) != prev.get(g, 0)]
    return len(rows), sorted(rows, key=lambda r: -abs(r["delta"]))[:60]
