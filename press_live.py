"""The live half of the press: gather, judge, record. press.py stays pure; this touches the world.

THE SPLIT IS THE POINT. press.evaluate() decides, and it holds no BigQuery client, no Brevo client
and no clock - the only reason every refusal was provable on the day they were written, with no
credential and no warehouse. Everything that has to LOOK something up lives here instead, so that
property survives whatever the relay ends up calling. PressRefused is defined here rather than in
press.py for the same reason: press.py imports nothing.

WHAT THE RELAY CALLS. verdict() is the callable their action endpoint must use before recording a
press; it must never re-implement the three press checks, and that is only meaningfully true because they
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


BATCH_BY_ID_SQL = (f"SELECT batch_id, send_date, assignment_build_id, audience_total "
                   f"FROM `{C.PROJECT}.{C.CONTROL}.day_batch` WHERE batch_id = @b LIMIT 1")


def live_inputs(send_date: str, credits_available: int, batch_id: str):
    """Read what the three checks judge, AT THE MOMENT OF THE PRESS, for the PRESSED batch.

    The mail side (build_id_in_mail, credits_needed) comes from the batch the human was SHOWN,
    looked up by its batch_id (MAIN, 2026-09-11). Until then it was the NEWEST day_batch for the
    date, so a batch rebuilt after the mail went out would have been judged in place of the one he
    read - an approval of a day he never saw. The live side (live_build_id, overlap) is what is true
    now, and the entire purpose of the re-check is that those two can differ.

    A batch_id that does not exist, or belongs to another date, gives build_id_in_mail=None, which
    fails AUDIENCE_CHANGED: fail-closed, never a silent fallback to some other batch.
    """
    from google.cloud.bigquery import ScalarQueryParameter as P
    if not batch_id:
        raise PressRefused("a press must name the batch it was shown; no batch_id, no verdict.")
    batch = bq.query(BATCH_BY_ID_SQL, [P("b", "STRING", batch_id)])
    if batch and str(batch[0]["send_date"]) != str(send_date):
        batch = []
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


def approval_for(batch_id: str, build_id: str):
    """The approval row for exactly this batch AND this build - what the SEND-TIME gate needs.

    Injected into campaign.send_now() as its approval_lookup. press.send_gate() re-checks both keys
    on the row it gets back, so a lookup that drifted could still not let the wrong day through.
    """
    from google.cloud.bigquery import ScalarQueryParameter as P
    rows = bq.query(
        f"SELECT send_date, batch_id, assignment_build_id, approved_by, approved_at, revoked_at, "
        f"revoked_reason FROM `{T_APPROVAL}` WHERE batch_id = @b AND assignment_build_id = @i "
        f"ORDER BY approved_at DESC LIMIT 1",
        [P("b", "STRING", batch_id), P("i", "STRING", build_id)])
    return dict(rows[0]) if rows else None


def verdict_by_press_id(press_id: str):
    """The verdict already stored for this press, if there is one. Replay, never re-judge.

    A retry is the relay saying "I did not hear you", not "judge it again". Re-judging would answer a
    different question - the world moved between the two calls - and could hand back a pass for a
    press that was refused a second earlier, or the reverse. So the stored answer is the answer.
    """
    from google.cloud.bigquery import ScalarQueryParameter as P
    rows = bq.query(
        f"SELECT verdict_id, send_date, may_press, checks_json, refusal_text_lv, checked_at, batch_id "
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
            press_id: str = None, batch_id: str = None) -> dict:
    """Judge the press and RECORD the judgement, refusal included.

    The refusal is written, not only returned. The press is the one moment where a wrong answer
    either mails thousands of people or silently mails nobody, so a refusal that leaves no row is
    indistinguishable afterwards from a press that never happened.
    """
    v = live_inputs(send_date, credits_available, batch_id)
    checks = press.evaluate(send_date=send_date, **v)
    may = press.may_press(checks)
    row = {
        "verdict_id": f"{send_date}-{uuid.uuid4().hex[:8]}", "send_date": send_date,
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(), "checked_by": checked_by,
        "may_press": may, "checks_json": json.dumps(checks, ensure_ascii=False),
        "refusal_text_lv": press.refusal_text(checks), "press_id": press_id,
        "batch_id": batch_id, **v,
    }
    errs = bq.client().insert_rows_json(T_VERDICT, [row])
    if errs:
        raise RuntimeError(f"press_verdict insert failed, refusing the press: {errs[:2]}")
    log.info("PRESS_VERDICT %s may_press=%s press_id=%s", row["verdict_id"], may, press_id)
    return {"verdict_id": row["verdict_id"], "may_press": may, "checks": checks,
            "refusal_text_lv": row["refusal_text_lv"]}


def record_approval(send_date: str, approved_by: str, batch_id: str, counts_at_press: dict,
                    board_token: str = None, press_id: str = None,
                    verdict_id: str = None) -> str:  # noqa: PLR0913
    """Write the approval row. Refuses unless THIS press's verdict exists and passed.

    The verdict is looked up by its verdict_id, and must be for this batch_id - not "the newest
    verdict for the date", which a second press on another batch could have written a second
    earlier. counts_at_press is stored as it was SHOWN in the e-mail, not as it is now - otherwise
    nobody can later say what Raivis approved, only what happened to be true afterwards.
    """
    from google.cloud.bigquery import ScalarQueryParameter as P
    if not verdict_id:
        raise PressRefused("record_approval needs the verdict_id of the press it records.")
    rows = bq.query(
        f"SELECT verdict_id, may_press, live_build_id, batch_id FROM `{T_VERDICT}` "
        f"WHERE verdict_id = @v LIMIT 1",
        [P("v", "STRING", verdict_id)])
    if not rows or not rows[0]["may_press"] or rows[0].get("batch_id") != batch_id:
        raise PressRefused(
            f"no passing verdict {verdict_id} for batch {batch_id}. The checks run BEFORE the "
            f"approval row is written, and there is no path to an approval row that skips them.")
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
        "batch_id": batch_id,
    }
    errs = bq.client().insert_rows_json(T_APPROVAL, [row])
    if errs:
        raise RuntimeError(f"send_approval insert failed: {errs[:2]}")
    log.info("APPROVAL_RECORDED %s by=%s verdict=%s press_id=%s",
             send_date, approved_by, rows[0]["verdict_id"], press_id)
    return rows[0]["verdict_id"]
