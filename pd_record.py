"""The Pipedrive record a lifecycle send produces - ONE code path for shadow and live.

Raivis 2026-09-25 16:12 (MAIN addendum 1): in shadow mode NOTHING is written to Pipedrive and
nothing to Brevo contact history. For every shadow send the exact write that WOULD be made is
stored in BigQuery (mkt_control.shadow_pd_writes), one row per would-be write. The real write
path is this same code with shadow=False, so what Raivis reviews is exactly what will be
written. Proven by tests/test_send_engine.py: the record renders byte-for-byte identically in
both modes, and shadow mode makes 0 calls to the Pipedrive writer.

Activity type: a CONFIG value, deliberately UNRESOLVED (MAIN 2026-09-25 17:45, point 4). The
account has no active `email` type (read live 2026-09-25) and whatsapp_chat must NOT be used.
Env PD_ACTIVITY_TYPE_KEY / PD_ACTIVITY_TYPE_ID; unset = None. Shadow rows carry None; the live
path refuses a record without a type. Chosen by KEY, never by label (labels are renamed).
"""
from __future__ import annotations

import datetime as dt
import json

import os

PD_ACTIVITY_TYPE_KEY = os.environ.get("PD_ACTIVITY_TYPE_KEY") or None
PD_ACTIVITY_TYPE_ID = int(os.environ["PD_ACTIVITY_TYPE_ID"]) if os.environ.get("PD_ACTIVITY_TYPE_ID") else None
RECORD_VERSION = "pd-record-v1"

# customer-facing wording never says winback/lost; the PD subject is internal, but it is shown to
# Raivis in Pipedrive, so it carries the letter name AND the neutral theme.
LETTER_LABEL = {
    "welcome_1": "Sveiciena vēstule", "reorder_1": "Atgādinājums 1", "reorder_2": "Atgādinājums 2",
    "reorder_3": "Atgādinājums 3", "winback_1": "Tava cena 1", "winback_2": "Tava cena 2",
    "winback_3": "Tava cena 3", "lost_quarterly": "Izdevīgi", "active_xsell": "Komplekts",
}


def render(*, person_id, org_id, master_key, email, email_type, template_id, send_date,
           offer_rung, reason, campaign_ref) -> dict:
    """The one record. Deterministic: same inputs -> same bytes. No clock, no randomness."""
    if isinstance(send_date, dt.date):
        send_date = send_date.isoformat()
    label = LETTER_LABEL.get(email_type, email_type)
    subject = f"[AI] tiktik.lv e-pasts: {label} ({email_type}, veidne {template_id})"
    note = "\n".join([
        f"Nosūtīts: {email} · {send_date}",
        f"Vēstule: {email_type} · veidne {template_id} · kampaņa {campaign_ref}",
        f"Cenu pakāpe (OFFER_RUNG): {offer_rung}",
        f"Kāpēc: {reason}",
        f"master_key: {master_key}",
    ])
    return {
        "record_version": RECORD_VERSION,
        "object": "activity",
        "target_person_id": person_id,
        "target_org_id": org_id,
        "subject": subject,
        "type_key": PD_ACTIVITY_TYPE_KEY,
        "type_id": PD_ACTIVITY_TYPE_ID,
        "due_date": send_date,
        "done": True,
        "note": note,
    }


def canonical(record: dict) -> bytes:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def write(record: dict, *, shadow: bool, pd_writer, shadow_sink) -> dict:
    """shadow=True  -> the record goes to shadow_sink (BigQuery row), pd_writer is NEVER called.
       shadow=False -> pd_writer(record) makes the real Pipedrive write.
    Both receive the identical record object; nothing is re-rendered per mode."""
    body = canonical(record)
    if shadow:
        shadow_sink({"record_json": body.decode(), **{k: record[k] for k in (
            "object", "target_person_id", "target_org_id", "subject", "type_key", "type_id",
            "due_date", "done", "note", "record_version")}})
        return {"mode": "shadow", "bytes": len(body)}
    if record["target_person_id"] is None:
        raise ValueError("live PD write without a person_id is refused")
    if not record["type_key"]:
        raise ValueError("live PD write without an activity type is refused (PD_ACTIVITY_TYPE_KEY unresolved)")
    return {"mode": "live", "result": pd_writer(record), "bytes": len(body)}
