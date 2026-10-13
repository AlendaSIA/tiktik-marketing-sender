#!/usr/bin/env python3
"""D1 trace (Sūtīšanas dzinējs 4): for the contacts returned by tools/d1_trace_*.sql (bq --format=json on stdin)
print why this letter, why today, and what comes next and when - by running the SAME sequence.advance() forward
day by day AS IF every due letter were sent and the customer never bought again (stage per customer_lifecycle's own
thresholds; prices assumed present). Pure: reads stdin, prints text, writes nothing."""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cadence_sim as C  # noqa: E402
import sequence as S  # noqa: E402

D = lambda v: None if v in (None, "") else dt.date.fromisoformat(str(v)[:10])  # noqa: E731
I = lambda v: None if v in (None, "") else int(v)  # noqa: E731


def project(r, today, days=230, limit=5):
    st = S.State(r["master_key"], r.get("track"), D(r.get("track_entered_on")), I(r.get("step")) or 0, I(r.get("rung")),
                 D(r.get("rung_set_on")), r.get("rung_month"), D(r.get("ladder_cleared_on")), r.get("last_email_type"),
                 D(r.get("last_sent_on")), I(r.get("rung_cap")), D(r.get("rung_cap_until")), D(r.get("reorder_worked_at")))
    lo, fo, thr = D(r.get("last_order")), D(r.get("first_order")), I(r.get("entry_threshold_days"))
    nr, on, ship = r.get("pp_nr"), D(r.get("pp_on")), D(r.get("pp_ship"))
    pp = xs = None
    out = []
    for i in range(days):
        day = today + dt.timedelta(days=i)
        stage = C.stage_on(day, r["lifecycle_stage"], lo, thr, fo)
        d = S.advance(st, S.Facts(stage, lo, fo, False, C.FAR, thr, nr, on, ship, pp, xs), day)
        if d.next_email_type and d.next_due_on == day and d.hold_reason is None:
            out.append((day, d.next_email_type, S.INTERFACE_V2[d.next_email_type], d.offer_rung, d.offer_valid_until
                        or d.xsell_valid_until, stage))
            st = S.record_sent(d.state, d.next_email_type, day, d.offer_rung)
            pp = day if d.next_email_type == S.PP1 else pp
            xs = day if d.next_email_type == S.XSELL else xs
            if len(out) >= limit:
                break
        else:
            st = d.state
    return out


def main():
    import sequence_job_light as L
    rows = json.load(sys.stdin)
    today = dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else dt.date.today()
    last = None
    for r in rows:
        key = (r["email_type"], r.get("hold_reason") or "")
        if key != last:
            print(f"\n== {r['email_type']} (template {r['template_id']})" + (f" - held {key[1]}" if key[1] else ""))
            last = key
        r["pp_nr"], r["pp_on"], r["pp_ship"] = L.pp_facts(
            {"order_nr": r.get("order_nr"), "order_on": D(r.get("order_on")), "ship_on": D(r.get("ship_on"))},
            D(r.get("last_order")))
        d = lambda v: "-" if not v else dt.date.fromisoformat(str(v)[:10]).strftime("%d.%m.%y")  # noqa: E731
        head = (f"{r['em']} | stage {r['lifecycle_stage']}, {r.get('orders')} orders, last {d(r.get('last_order'))}, "
                f"thr {r.get('entry_threshold_days')} d"
                + (f", last letter {r['last_email_type']} {d(r.get('last_sent_on'))}" if r.get("last_email_type") else "")
                + (f", shop order {r['pp_nr']} of {d(r['pp_on'])} handed over {d(r['pp_ship'])}" if r.get("pp_nr") else ""))
        state = "WOULD SEND" if r["would_send"] == "true" else f"HELD {r.get('hold_reason')}"
        gate = f", pre-send gate today: {r['presend_gates']}" if r.get("presend_gates") else ""
        extra = "".join(f", {k} {r[k]}" for k in ("offer_valid_until", "xsell_valid_until", "trigger_order_nr") if r.get(k))
        print(f"- {head}\n    today: {state} {r['email_type']} on {d(r.get('planned_send_date'))} rung {r['offer_rung']}{extra}{gate}"
              f"; akcija W41: {'yes' if r.get('akcija_in') == 'true' else 'no (' + str(r.get('akcija_reason')) + ')'}")
        print(f"    why: {r['reason']}")
        if r["would_send"] == "true" or r.get("hold_reason") == "no_offer_valid_until":
            nxt = project(r, today)
            print("    as-if-sent, no purchase: " + " -> ".join(
                f"{x[0].strftime('%d.%m.%y')} {x[1]}[{x[2]}]" + (f" r{x[3]}" if x[3] else "")
                + (f" until {x[4].strftime('%d.%m')}" if x[4] else "") for x in nxt))


if __name__ == "__main__":
    main()
