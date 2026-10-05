"""CADENCE v1 proof tool (Sūtīšanas dzinējs 3, 2026-09-30). Pure: no clock, no client, never sends.

Projects the planner forward day by day for a contact, AS IF every letter the planner makes due (no hold)
were sent that day: sequence.record_sent is applied to a COPY of the state. Stage per day follows
customer_lifecycle's own formula (thr = entry_threshold_days): >= thr reorder_due, > thr+56 winback,
> thr+168 lost; 'new' / 'blocked' stay as they are today. Assumes no purchase in the window, templates
present (E2 texts not written yet - projection only) and, unless given, a rung price at every rung
refreshed nightly (the cadence is tested, not the prices).
"""
import datetime as dt

FAR = {1: dt.date(2099, 1, 1), 2: dt.date(2099, 1, 1), 3: dt.date(2099, 1, 1),
       4: dt.date(2099, 1, 1), "4c": dt.date(2099, 1, 1)}   # 4 / "4c" = lost prices (LQ/XS PRICE SOURCE v1)


def stage_on(day, stage_today, last_order, thr, first_order=None):
    if stage_today in (None, "blocked"):
        return stage_today
    if stage_today == "new" and first_order and (day - first_order).days <= 40:
        return "new"
    if last_order is None or thr is None:
        return stage_today
    n = (day - last_order).days
    if n > thr + 168:
        return "lost"
    if n > thr + 56:
        return "winback"
    if n >= thr:
        return "reorder_due"
    return "active"


def project(S, state, *, stage_today, last_order, first_order, thr, suppressed=False, start, days=98, rungs=FAR):
    """-> [(date, email_type, rung, offer_valid_until)] the planner would send in [start, start+days)."""
    st, out = state, []
    for i in range(days):
        day = start + dt.timedelta(days=i)
        f = S.Facts(stage_on(day, stage_today, last_order, thr, first_order), last_order, first_order,
                    suppressed, rungs, thr)
        d = S.advance(st, f, day)
        if d.next_email_type and d.next_due_on == day and d.hold_reason is None:
            out.append((day, d.next_email_type, d.offer_rung, d.offer_valid_until))
            st = S.record_sent(d.state, d.next_email_type, day, d.offer_rung)
        else:
            st = d.state
    return out


def violations(sends, e2_types=None, e1_of=None):
    """Consecutive-week pairs that are NOT an E1 -> E2 pair of the same rung exactly 7 days apart.
    'Consecutive weeks' = ISO week distance <= 1 (same week included)."""
    bad = []
    for a, b in zip(sends, sends[1:]):
        wk = (b[0] - dt.timedelta(days=b[0].weekday()) - (a[0] - dt.timedelta(days=a[0].weekday()))).days // 7
        if wk > 1:
            continue
        pair = e2_types is not None and b[1] in e2_types and e1_of(b[1]) == a[1] and (b[0] - a[0]).days == 7
        if not pair:
            bad.append((a, b))
    return bad
