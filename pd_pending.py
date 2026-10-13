"""Pending queue for held Pipedrive records (MAIN 2026-09-28 COMMAND 5, decision 2). Pure.

A held PD record NEVER holds the e-mail: would_send stays true, and the PD write goes here with its
hold reason. Every run retries every OPEN row with the current PD data (merge done / override row
added / person appears); a row that now resolves is closed with its target and the exact record the
live path would write at that moment. Nothing here talks to Pipedrive.

Key: pending_key = master_key|email_type|<send ref>. Live: the send ref is the send_log row (one
message = one record). Shadow: nothing is sent, the same letter is re-planned daily, so the ref is
the constant "shadow" - one open row per person x letter, not one per day.
Table mkt_control.pd_write_pending is rewritten whole by its single writer (the shadow job).
"""
from __future__ import annotations

import datetime as dt

SHADOW_REF = "shadow"


def key(master_key: str, email_type: str, send_ref: str = SHADOW_REF) -> str:
    return f"{master_key}|{email_type}|{send_ref}"


def step(rows: list, held_today: list, resolve, render, now: dt.datetime) -> tuple:
    """rows: current table rows (dicts). held_today: [{master_key, email, email_type, hold_reason,
    pd_class, send_ref?}] from today's plan. resolve(email, master_key) -> pd_target.Target with
    today's data. render(row, target) -> record_json str. Returns (new_rows, stats)."""
    iso = now.isoformat()
    by_key = {r["pending_key"]: dict(r) for r in rows}
    new = 0
    for h in held_today:
        k = key(h["master_key"], h["email_type"], h.get("send_ref") or SHADOW_REF)
        cur = by_key.get(k)
        if cur is None or cur.get("resolved_at"):
            by_key[k] = {"pending_key": k, "master_key": h["master_key"], "email": h["email"],
                         "email_type": h["email_type"], "hold_reason": h["hold_reason"],
                         "pd_class": h["pd_class"], "template_id": h.get("template_id"),
                         "send_date": h.get("send_date"), "offer_rung": h.get("offer_rung"),
                         "reason": h.get("reason"), "first_held_at": iso, "last_tried_at": None,
                         "tries": 0, "resolved_at": None, "resolved_kind": None,
                         "resolved_person_id": None, "resolved_org_id": None, "resolved_record_json": None}
            new += 1
    resolved = 0
    for k, r in by_key.items():
        if r.get("resolved_at"):
            continue
        t = resolve(r["email"], r["master_key"])
        r["tries"] = (r.get("tries") or 0) + 1
        r["last_tried_at"] = iso
        if t.kind == "held":
            r["hold_reason"], r["pd_class"] = t.hold_reason, t.cls
        else:
            r.update(resolved_at=iso, resolved_kind=t.kind, resolved_person_id=t.person_id,
                     resolved_org_id=t.org_id, resolved_record_json=render(r, t))
            resolved += 1
    open_rows = [r for r in by_key.values() if not r.get("resolved_at")]
    by_reason = {}
    for r in open_rows:
        by_reason[r["hold_reason"]] = by_reason.get(r["hold_reason"], 0) + 1
    oldest = min((dt.datetime.fromisoformat(str(r["first_held_at"])) for r in open_rows), default=None)
    if oldest is not None and oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=dt.timezone.utc)
    stats = {"pending_new": new, "pending_resolved": resolved, "pending_open": len(open_rows),
             "pending_by_reason": dict(sorted(by_reason.items())),
             "pending_oldest_h": None if oldest is None else round((now - oldest).total_seconds() / 3600, 1)}
    return list(by_key.values()), stats
