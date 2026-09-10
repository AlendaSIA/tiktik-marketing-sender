"""The day batch: one FROZEN object per sending day, built the afternoon before.

The approval e-mail is written from this row and nothing else. That is the whole point of freezing
it: the mail is written on Monday afternoon and pressed on Tuesday, and if the report were rebuilt
at the press it would silently describe a different day than the one he read.

RULE THAT IS EASY TO LOSE, so it is enforced here rather than remembered: a day with ZERO campaigns
still gets a row. Zero planned campaigns is a finding, not a reason for silence - a missing report
and a quiet week must never look the same. `presentable` is False and `note` says why, and the mail
still goes out.

WHAT IS COPIED AND WHAT IS COMPUTED. The audience counts and the criteria are COPIED from the
planned snapshot rows - `chosen_because` is the honest answer to "why these people" and re-deriving
it would produce a second explanation that can disagree with the first. The template's active state
is READ LIVE from Brevo, not from the 06:30 mirror, because a campaign is built now. The approval
state is read from mkt_control.template_approval, which is empty today, which is why every campaign
currently blocks - and that is correct, not a bug.

WHAT IS DELIBERATELY NOT HERE, decided 2026-09-10. A frequency gate counting letters from senders
outside the engine was built and then reverted. Raivis sends campaigns by hand from the Brevo
interface as a deliberate temporary arrangement, and he stops doing it the moment the automatic
campaigns are switched on - so the two paths never run at once and there is no overlap to catch.
The engine's per-person limit counts what the engine sends, which is the whole population it will
ever send to. The columns freq_* exist on day_batch and stay NULL.
"""
import datetime as dt
import json
import logging
import uuid

import bq
import config as C

log = logging.getLogger("batch")

T_BATCH = f"{C.PROJECT}.{C.CONTROL}.day_batch"
T_BATCH_CAMPAIGN = f"{C.PROJECT}.{C.CONTROL}.day_batch_campaign"


def _campaigns(send_date):
    from google.cloud.bigquery import ScalarQueryParameter as P
    return bq.query(f"""
        SELECT send_date, email_type, track, utm_campaign, template_id,
               members, distinct_people
        FROM `{C.PROJECT}.{C.CONTROL}.variant_list_plan_summary`
        WHERE send_date = @d
        ORDER BY email_type
    """, [P("d", "DATE", send_date)])


def _criteria(send_date):
    """Distinct chosen_because per variant, with counts. Copied, never re-derived."""
    from google.cloud.bigquery import ScalarQueryParameter as P
    rows = bq.query(f"""
        SELECT email_type, chosen_because, COUNT(*) AS people
        FROM `{C.PROJECT}.{C.CONTROL}.variant_list_plan`
        WHERE send_date = @d
        GROUP BY 1, 2
        ORDER BY 1, people DESC
    """, [P("d", "DATE", send_date)])
    out = {}
    for r in rows:
        out.setdefault(r["email_type"], []).append(
            {"chosen_because": r["chosen_because"], "people": int(r["people"])})
    return out


def _approvals():
    rows = bq.query(f"""
        SELECT template_id, email_type, approved
        FROM `{C.PROJECT}.{C.CONTROL}.template_approval`
    """)
    return {(r["template_id"], r["email_type"]): bool(r["approved"]) for r in rows}


def _overlap(send_date):
    from google.cloud.bigquery import ScalarQueryParameter as P
    return int(bq.scalar(
        f"SELECT COUNT(*) FROM {C.T_DAY_OVERLAP} WHERE send_date = @d",
        [P("d", "DATE", send_date)]) or 0)


def build(send_date: str, run_id: str, template_is_active=None, credits: int = 0) -> dict:
    """Freeze the batch for one sending day and return it.

    template_is_active is injected so the batch can be built and inspected without a Brevo
    credential; when it is None the active state is recorded as unknown, which BLOCKS rather than
    passes. Unknown is not the same as fine, and defaults belong on the harmless side.
    """
    batch_id = f"{send_date}-{uuid.uuid4().hex[:8]}"
    built_at = dt.datetime.now(dt.timezone.utc)
    campaigns = _campaigns(send_date)
    criteria = _criteria(send_date)
    approvals = _approvals()
    overlap = _overlap(send_date)
    build_id = bq.assignment_build_id()

    rows, audience_total, blocking_day = [], 0, []
    for c in campaigns:
        tid = c["template_id"]
        blocking = []
        active = None
        if tid is None:
            blocking.append("NO_TEMPLATE")
        elif template_is_active is None:
            blocking.append("TEMPLATE_STATE_UNKNOWN")
        else:
            active = bool(template_is_active(tid))
            if not active:
                blocking.append("TEMPLATE_INACTIVE_IN_BREVO")
        approved = approvals.get((tid, c["email_type"]))
        if not approved:
            # PART E rule 5: listed, and it marks the day blocking. Not silently dropped - a
            # campaign that disappears from the report is a campaign nobody notices is missing.
            blocking.append("NO_APPROVAL_FOR_TEMPLATE_X_AUDIENCE")
        if int(c["members"] or 0) == 0:
            blocking.append("EMPTY_AUDIENCE")
        if int(c["members"] or 0) != int(c["distinct_people"] or 0):
            blocking.append("PERSON_TWICE_IN_ONE_LIST")

        audience_total += int(c["members"] or 0)
        blocking_day.extend(f'{c["email_type"]}:{b}' for b in blocking)
        rows.append({
            "batch_id": batch_id, "send_date": send_date, "email_type": c["email_type"],
            "track": c["track"], "utm_campaign": c["utm_campaign"], "template_id": tid,
            "template_active": active, "template_approved": bool(approved),
            "audience": int(c["members"] or 0),
            "criteria": json.dumps(criteria.get(c["email_type"], []), ensure_ascii=False),
            "blocking": ",".join(blocking),
        })

    if overlap:
        blocking_day.append(f"DAY:PERSON_IN_TWO_LISTS={overlap}")
    if credits < audience_total:
        blocking_day.append(f"DAY:NOT_ENOUGH_CREDITS={credits}<{audience_total}")

    note = ("Nothing is planned for this day. The report still goes out: zero planned campaigns is "
            "a finding, and a missing report must never look like a quiet week."
            if not campaigns else
            "Every campaign listed; blocking reasons are per campaign and for the day as a whole.")

    head = {
        "batch_id": batch_id, "send_date": send_date, "built_at": built_at.isoformat(),
        "built_by": run_id, "assignment_build_id": build_id,
        "campaign_count": len(campaigns), "audience_total": audience_total,
        "dedup_overlap": overlap, "credit_headroom": int(credits),
        "presentable": bool(campaigns) and not blocking_day,
        "blocking_reasons": " | ".join(blocking_day), "note": note,
    }

    client = bq.client()
    errs = client.insert_rows_json(T_BATCH, [head])
    if errs:
        raise RuntimeError(f"day_batch insert failed: {errs[:3]}")
    if rows:
        errs = client.insert_rows_json(T_BATCH_CAMPAIGN, rows)
        if errs:
            raise RuntimeError(f"day_batch_campaign insert failed: {errs[:3]}")
    log.info("BATCH_FROZEN %s campaigns=%s audience=%s presentable=%s blocking=%s",
             batch_id, head["campaign_count"], audience_total, head["presentable"],
             head["blocking_reasons"] or "-")
    return {"head": head, "campaigns": rows}
