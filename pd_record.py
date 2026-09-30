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

# Contract v2.9.3 (28.09): the type EXISTS - id 32, key_string _e_pasts_automatisks ("✉️ E-pasts (automātisks)").
# This fills lock L6's config. The other five send locks are untouched. Env may still override.
PD_ACTIVITY_TYPE_KEY = os.environ.get("PD_ACTIVITY_TYPE_KEY") or "_e_pasts_automatisks"
PD_ACTIVITY_TYPE_ID = int(os.environ["PD_ACTIVITY_TYPE_ID"]) if os.environ.get("PD_ACTIVITY_TYPE_ID") else 32
RECORD_VERSION = "pd-record-v3"   # v3 (28.09): type 32, offer summary in subject, products/prices/OVU in note

# customer-facing wording never says winback/lost; the PD subject is internal, but it is shown to
# Raivis in Pipedrive, so it carries the letter name AND the neutral theme.
LETTER_LABEL = {
    "welcome_1": "Sveiciena vēstule", "reorder_1": "Atgādinājums 1", "reorder_2": "Atgādinājums 2",
    "reorder_3": "Atgādinājums 3", "winback_1": "Tava cena 1", "winback_2": "Tava cena 2",
    "winback_3": "Tava cena 3", "lost_quarterly": "Izdevīgi", "active_xsell": "Komplekts",
    # CADENCE v1 K3: the second letter of a price episode (same price, same deadline)
    "winback_1_e2": "Tava cena 1 · 2. vēstule", "winback_2_e2": "Tava cena 2 · 2. vēstule",
    "winback_3_e2": "Tava cena 3 · 2. vēstule",
}


def render(*, person_id, org_id, master_key, email, email_type, template_id, send_date,
           offer_rung, reason, campaign_ref, participants=(), offer_valid_until=None,
           offer_tail=None, product_lines=()) -> dict:
    """The one record. Deterministic: same inputs -> same bytes. No clock, no randomness."""
    if isinstance(send_date, dt.date):
        send_date = send_date.isoformat()
    label = LETTER_LABEL.get(email_type, email_type)
    if isinstance(offer_valid_until, dt.date):
        offer_valid_until = offer_valid_until.isoformat()
    tail = offer_tail or (f"pakāpe {offer_rung}, līdz {offer_valid_until}" if offer_rung else "bez atlaides")
    subject = f"[AI] tiktik.lv e-pasts: {label} ({email_type}, veidne {template_id}) · {tail}"
    note = "\n".join([
        f"Nosūtīts: {email} · {send_date}",
        f"Vēstule: {email_type} · veidne {template_id} · kampaņa {campaign_ref}",
        f"Cenu pakāpe (OFFER_RUNG): {offer_rung} · spēkā līdz: {offer_valid_until or '-'}",
        *([f"Produkti: {'; '.join(product_lines)}"] if product_lines else []),
        f"Kāpēc: {reason}",
        f"master_key: {master_key}",
    ])
    return {
        "record_version": RECORD_VERSION,
        "object": "activity",
        "target_person_id": person_id,
        "target_org_id": org_id,
        "participant_person_ids": [int(x) for x in (participants or ())],
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
            "object", "target_person_id", "target_org_id", "participant_person_ids", "subject", "type_key", "type_id",
            "due_date", "done", "note", "record_version")}})
        return {"mode": "shadow", "bytes": len(body)}
    if record["target_person_id"] is None and record["target_org_id"] is None:
        raise ValueError("live PD write without a person or an organisation is refused")
    if not record["type_key"]:
        raise ValueError("live PD write without an activity type is refused (PD_ACTIVITY_TYPE_KEY unresolved)")
    return {"mode": "live", "result": pd_writer(record), "bytes": len(body)}
