"""The live half of the press: gather, judge, record. press.py stays pure; this touches the world.

THE SPLIT IS THE POINT. press.evaluate() decides, and it holds no BigQuery client, no Brevo client
and no clock - the only reason all four refusals were provable on the day they were written, with no
credential and no warehouse. Everything that has to LOOK something up lives here instead, so that
property survives whatever the relay ends up calling. PressRefused is defined here rather than in
press.py for the same reason: press.py imports nothing.

WHAT THE RELAY CALLS. verdict() is the callable their action endpoint must use before recording a
press; it must never re-implement the four checks, and that is only meaningfully true because they
exist as one function anyone can call. record_approval() then refuses to write the approval row
unless a PASSING verdict for that send_date already exists. The order is structural, not procedural:
there is no path to an approval row that skips the checks.

THE TRANSPORT IS NO LONGER OPEN: it is press_server.py, one authenticated endpoint, from the same
image. See docs/BOUNDARY.md.

PRESS_ID, ADDED 2026-09-10. The relay sends an idempotency key with the press and keeps it stable
across retries. It is stored on the verdict AND on the approval row, so a retried press replays a
judgement instead of producing a second one, and "was this day approved by this press" is a fact in
a column rather than something reconstructed from timestamps. The dedup happens at the endpoint,
which is the only place that can answer a retry without re-judging - but the key lives here, because
the rows live here.
"""
import datetime as dt
import json
import logging
import uuid

import bq
import config as C
import press

log = logging.getLogger("press-live")

T_VERDICT = f"{C.PROJECT}.{C.CONTROL}.press_verdict"
T_APPROVAL = f"{C.PROJECT}.{C.CONTROL}.send_approval"


class PressRefused(RuntimeError):
    """A press was attempted without a passing verdict. Outside every per-item handler."""


def live_inputs(send_date: str, credits_available: int):
    """Read the four things the checks judge, AT THE MOMENT OF THE PRESS.

    Not from the batch, deliberately. The batch is what the mail was written from; these are what is
    true now, and the entire purpose of the re-check is that those two can differ.
    """
    from google.cloud.bigquery import ScalarQueryParameter as P
    batch = bq.query(
        f"SELECT assignment_build_id, audience_total FROM `{C.PROJECT}.{C.CONTROL}.day_batch` "
        f"WHERE send_date = @d ORDER BY built_at DESC LIMIT 1",
        [P("d", "DATE", send_date)])
    overlap = int(bq.scalar(
        f"SELECT COUNT(*) FROM {C.T_DAY_OVERLAP} WHERE send_date = @d",
        [P("d", "DATE", send_date)]) or 0)
    return {
        "build_id_in_mail": batch[0]["assignment_build_id"] if batch else None,
        "credits_needed": int(batch[0]["audience_total"]) if batch else 0,
        "live_build_id": bq.assignment_build_id(),
        "overlap_people": overlap,
        "credits_available": int(credits_available),
    }


def approval_row(send_date: str):
    from google.cloud.bigquery import ScalarQueryParameter as P
    rows = bq.query(
        f"SELECT send_date, approved_by, approved_at, revoked_at, revoked_reason "
        f"FROM `{T_APPROVAL}` WHERE send_date = @d ORDER BY approved_at DESC LIMIT 1",
        [P("d", "DATE", send_date)])
    return dict(rows[0]) if rows else None


def verdict_by_press_id(press_id: str):
    """The verdict already stored for this press, if there is one. Replay, never re-judge.

    A retry is the relay saying "I did not hear you", not "judge it again". Re-judging would answer a
    different question - the world moved between the two calls - and could hand back a pass for a
    press that was refused a second earlier, or the reverse. So the stored answer is the answer.
    """
    from google.cloud.bigquery import ScalarQueryParameter as P
    rows = bq.query(
        f"SELECT verdict_id, send_date, may_press, checks_json, refusal_text_lv, checked_at "
        f"FROM `{T_VERDICT}` WHERE press_id = @p ORDER BY checked_at ASC LIMIT 1",
        [P("p", "STRING", press_id)])
    return dict(rows[0]) if rows else None


def approval_by_press_id(press_id: str):
    """Did THIS press already write an approval row. A column, not an inference from timestamps."""
    from google.cloud.bigquery import ScalarQueryParameter as P
    rows = bq.query(
        f"SELECT send_date, approved_at, approved_by FROM `{T_APPROVAL}` "
        f"WHERE press_id = @p ORDER BY approved_at ASC LIMIT 1",
        [P("p", "STRING", press_id)])
    return dict(rows[0]) if rows else None


def verdict(send_date: str, credits_available: int, checked_by: str,
            press_id: str = None) -> dict:
    """Judge the press and RECORD the judgement, refusal included.

    The refusal is written, not only returned. The press is the one moment where a wrong answer
    either mails thousands of people or silently mails nobody, so a refusal that leaves no row is
    indistinguishable afterwards from a press that never happened.
    """
    v = live_inputs(send_date, credits_available)
    checks = press.evaluate(send_date=send_date, approval_row=approval_row(send_date), **v)
    may = press.may_press(checks)
    row = {
        "verdict_id": f"{send_date}-{uuid.uuid4().hex[:8]}", "send_date": send_date,
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(), "checked_by": checked_by,
        "may_press": may, "checks_json": json.dumps(checks, ensure_ascii=False),
        "refusal_text_lv": press.refusal_text(checks), "press_id": press_id, **v,
    }
    errs = bq.client().insert_rows_json(T_VERDICT, [row])
    if errs:
        raise RuntimeError(f"press_verdict insert failed, refusing the press: {errs[:2]}")
    log.info("PRESS_VERDICT %s may_press=%s press_id=%s", row["verdict_id"], may, press_id)
    return {"verdict_id": row["verdict_id"], "may_press": may, "checks": checks,
            "refusal_text_lv": row["refusal_text_lv"]}


def record_approval(send_date: str, approved_by: str, batch_id: str, counts_at_press: dict,
                    board_token: str = None, press_id: str = None) -> str:  # noqa: PLR0913
    """Write the approval row. Refuses unless a PASSING verdict for this day already exists.

    counts_at_press is stored as it was SHOWN in the e-mail, not as it is now - otherwise nobody can
    later say what Raivis approved, only what happened to be true afterwards.
    """
    from google.cloud.bigquery import ScalarQueryParameter as P
    rows = bq.query(
        f"SELECT verdict_id, may_press, live_build_id FROM `{T_VERDICT}` "
        f"WHERE send_date = @d ORDER BY checked_at DESC LIMIT 1",
        [P("d", "DATE", send_date)])
    if not rows or not rows[0]["may_press"]:
        raise PressRefused(
            f"no passing verdict for {send_date}. The checks run BEFORE the approval row is "
            f"written, and there is no path to an approval row that skips them.")
    batch = bq.query(
        f"SELECT audience_total, campaign_count, dedup_overlap, credit_headroom "
        f"FROM `{C.PROJECT}.{C.CONTROL}.day_batch` WHERE batch_id = @b LIMIT 1",
        [P("b", "STRING", batch_id)])
    if not batch:
        raise PressRefused(f"batch {batch_id} does not exist; the mail must name its batch.")
    row = {
        "send_date": send_date,
        "approved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "approved_by": approved_by,
        "assignment_build_id": rows[0]["live_build_id"],
        "campaign_count": int(batch[0]["campaign_count"] or 0),
        "audience_total": int(batch[0]["audience_total"] or 0),
        "counts_at_press": json.dumps(counts_at_press, ensure_ascii=False),
        "dedup_ok": int(batch[0]["dedup_overlap"] or 0) == 0,
        "credit_headroom": int(batch[0]["credit_headroom"] or 0),
        "board_token": board_token,
        "press_id": press_id,
    }
    errs = bq.client().insert_rows_json(T_APPROVAL, [row])
    if errs:
        raise RuntimeError(f"send_approval insert failed: {errs[:2]}")
    log.info("APPROVAL_RECORDED %s by=%s verdict=%s press_id=%s",
             send_date, approved_by, rows[0]["verdict_id"], press_id)
    return rows[0]["verdict_id"]
