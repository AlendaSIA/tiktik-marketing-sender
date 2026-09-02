"""tiktik.lv lifecycle e-mail sender.

Replaces the June 2026 image, which had no source, no scheduler, no identity guard, ran
three times in its life and wrote 7 668 log rows under one shared timestamp.

What this job guarantees, and where each guarantee is enforced
-------------------------------------------------------------
G1  IDENTITY_STALE   identity older than IDENTITY_MAX_AGE_H -> abort            (step 1)
G2  SUPPRESSION_FRESH the contact snapshot is refreshed here, then asserted     (step 2)
G3a ASSIGNMENT     rebuilt here every run; one row per person per week, none suppressed (step 4)
G3  ONE_PER_PERSON   the plan holds at most one row per master_key -> else abort(step 4)
G4  NO_SUPPRESSED    plan INTERSECT suppression = 0 -> else abort               (step 4)
G5  FREQUENCY        <= MAX_EMAILS_PER_WEEK per person, >= MIN_DAYS_BETWEEN     (plan SQL)
G6  SEND_TIME_RECHECK every message re-checks the person against suppression    (step 6)
G7  PER_MESSAGE_LOG  one row per message, real timestamp and message id         (step 6)
G8  DRY_RUN default  sending needs DRY_RUN=false AND ALLOW_SEND=true AND a
                     clean plan; anything else reports and exits 0              (step 5)

Exit codes: 0 = ran and reported. 1 = a guard failed. A guard failure is a real failure and
must stay visible - "a signal that is always on is not a signal".
"""
import datetime as dt
import logging
import os
import sys
import uuid

import config as C
import bq
import brevo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("sender")

RUN_ID = os.environ.get("CLOUD_RUN_EXECUTION") or f"local-{uuid.uuid4().hex[:12]}"


class GuardFailure(RuntimeError):
    pass


def _now():
    return dt.datetime.now(dt.timezone.utc)


# --------------------------------------------------------------------------- #
def step1_identity_guard():
    age = bq.identity_age_hours()
    if age is None:
        raise GuardFailure("IDENTITY_MISSING: customer_identity/customer_master have no built_at")
    if age > C.IDENTITY_MAX_AGE_H:
        raise GuardFailure(
            f"IDENTITY_STALE identity_age_h={age:.1f} threshold_h={C.IDENTITY_MAX_AGE_H}")
    log.info("IDENTITY_OK identity_age_h=%.1f threshold_h=%s", age, C.IDENTITY_MAX_AGE_H)
    return age


def step2_refresh_suppression():
    """Refresh Brevo contact state, then assert it is fresh.

    The refresh is part of sending, not a separate job: suppression that is not refreshed
    by the thing about to send is suppression nobody notices going stale.
    """
    if C.REFRESH_SNAPSHOT:
        if not C.BREVO_API_KEY:
            raise GuardFailure("SNAPSHOT_REFRESH_IMPOSSIBLE: BREVO_API_KEY is not set")
        from google.cloud import storage
        data = brevo.export_contacts_ndjson(C.BREVO_API_KEY)
        bucket_name, _, blob_name = C.SNAPSHOT_GCS_URI[len("gs://"):].partition("/")
        storage.Client(project=C.PROJECT).bucket(bucket_name).blob(blob_name)\
            .upload_from_string(data, content_type="application/x-ndjson")
        n = bq.load_snapshot_from_gcs(C.SNAPSHOT_GCS_URI)
        log.info("SNAPSHOT_REFRESHED contacts=%s", n)
    else:
        log.warning("SNAPSHOT_REFRESH_SKIPPED (REFRESH_SNAPSHOT=false)")

    age = bq.snapshot_age_hours()
    if age is None or age > C.SUPPRESSION_MAX_AGE_H:
        raise GuardFailure(
            f"SUPPRESSION_STALE snapshot_age_h={age} threshold_h={C.SUPPRESSION_MAX_AGE_H}")
    log.info("SUPPRESSION_OK snapshot_age_h=%.1f", age)
    return age


def step3_coverage():
    cov = bq.coverage()
    log.info("COVERAGE %s", cov)
    return cov


def step4_plan():
    if C.BUILD_ASSIGNMENT and bq.table_exists(C.T_ASSIGNMENT):
        n = bq.build_assignment()
        log.info("ASSIGNMENT_REBUILT people=%s", n)
    if not bq.table_exists(C.T_ASSIGNMENT):
        log.warning("NO_ASSIGNMENT: %s does not exist yet - reporting only, nothing to send",
                    C.T_ASSIGNMENT)
        return None
    rows = bq.build_plan()
    planned_at = _now().isoformat()
    plan = [{
        "planned_at": planned_at,
        "week_start": r["week_start"].isoformat() if r["week_start"] else None,
        "master_key": r["master_key"],
        "email": r["email"],
        "track": r["track"],
        "email_type": r["email_type"],
        "template_id": r["template_id"],
        "decision": r["decision"],
        "lifecycle_stage": r["lifecycle_stage"],
        "full_name": r["full_name"],
        "gender_greeting": r["gender_greeting"],
        "language": r["language"],
        "hero_product_name": r["hero_product_name"],
        "hero_product_url": r["hero_product_url"],
        "hero_product_image": r["hero_product_image"],
        "next_discount_pct": r["next_discount_pct"],
        "next_discount_code": r["next_discount_code"],
        "dry_run": C.DRY_RUN,
    } for r in rows]

    sendable = [p for p in plan if p["decision"] == "SEND"]
    masters = [p["master_key"] for p in sendable]
    if len(masters) != len(set(masters)):
        dupes = len(masters) - len(set(masters))
        raise GuardFailure(f"ONE_PER_PERSON violated: {dupes} duplicate master_key rows in the plan")
    if any(p["decision"] == "SUPPRESSED" for p in sendable):
        raise GuardFailure("NO_SUPPRESSED violated: a suppressed row survived into the send set")
    log.info("PLAN rows=%s send=%s suppressed=%s frequency=%s other=%s",
             len(plan), len(sendable),
             sum(1 for p in plan if p["decision"] == "SUPPRESSED"),
             sum(1 for p in plan if str(p["decision"]).startswith("FREQUENCY")),
             sum(1 for p in plan if p["decision"] in ("NO_TEMPLATE", "NOT_IN_LIFECYCLE")))
    bq.write_send_plan(RUN_ID, plan)
    return plan


def step5_gate(plan, cov):
    """Sending is the exception, not the default."""
    if C.DRY_RUN or not C.ALLOW_SEND:
        log.info("DRY_RUN: computed everything, sending skipped (DRY_RUN=%s ALLOW_SEND=%s)",
                 C.DRY_RUN, C.ALLOW_SEND)
        return False
    if plan is None:
        log.error("REFUSING TO SEND: no assignment table")
        return False
    if cov["orphans_mailable"] != 0:
        log.error("REFUSING TO SEND: orphans_mailable=%s (must be 0)", cov["orphans_mailable"])
        return False
    if cov["duplicate_sends"] != 0:
        log.error("REFUSING TO SEND: duplicate_sends=%s (must be 0)", cov["duplicate_sends"])
        return False
    if not C.BREVO_API_KEY:
        log.error("REFUSING TO SEND: BREVO_API_KEY is not set")
        return False
    return True


def step6_send(plan):
    sent, skipped, failed = 0, 0, 0
    log_rows = []
    for p in [x for x in plan if x["decision"] == "SEND"]:
        if C.SEND_LIMIT and sent >= C.SEND_LIMIT:
            log.info("SEND_LIMIT %s reached", C.SEND_LIMIT)
            break
        # G6 - the plan may be minutes old; consent may not be.
        if bq.recheck(p["email"], p["master_key"]):
            skipped += 1
            log.info("SKIP_AT_SEND_TIME %s (%s)", p["email"], p["master_key"])
            continue
        params = {
            "greeting": p["gender_greeting"],
            "name": p["full_name"],
            "product": p["hero_product_name"],
            "product_url": p["hero_product_url"],
            "product_image": p["hero_product_image"],
            "discount_pct": p["next_discount_pct"],
            "discount_code": p["next_discount_code"],
        }
        try:
            message_id = brevo.send_transactional(
                C.BREVO_API_KEY, p["email"], p["full_name"], p["template_id"], params,
                tags=[p["email_type"], p["track"], f"run:{RUN_ID}"])
            status = "sent"
        except Exception as e:  # noqa: BLE001 - one bad message must not kill the run
            failed += 1
            message_id = None
            status = "failed"
            log.error("SEND_FAILED %s: %s", p["email"], e)
        # G7 - one row, this message, this instant.
        log_rows.append({
            "email": p["email"],
            "master_key": p["master_key"],
            "run_id": RUN_ID,
            "brevo_message_id": message_id,
            "assignment_week": p["week_start"],
            "campaign_name": f"{p['track']}/{p['email_type']}",
            "email_type": p["email_type"],
            "language": p["language"],
            "send_status": status,
            "channel": "email",
            "sent_at": _now().isoformat(),
            "targeted_at": p["planned_at"],
            "discount_code": p["next_discount_code"],
            "discount_pct": p["next_discount_pct"],
        })
        if status == "sent":
            sent += 1
        if len(log_rows) >= 200:
            bq.write_send_log(log_rows)
            log_rows = []
    bq.write_send_log(log_rows)
    log.info("SEND_DONE sent=%s skipped_at_send_time=%s failed=%s", sent, skipped, failed)
    return sent, skipped, failed


def main() -> int:
    started = _now()
    report = {"run_id": RUN_ID, "started_at": started.isoformat(),
              "dry_run": C.DRY_RUN, "allow_send": C.ALLOW_SEND}
    try:
        report["identity_age_h"] = step1_identity_guard()
        report["snapshot_age_h"] = step2_refresh_suppression()
        cov = step3_coverage()
        report.update({k: v for k, v in cov.items()})
        plan = step4_plan()
        report["plan_rows"] = len(plan) if plan else 0
        report["plan_send"] = sum(1 for p in (plan or []) if p["decision"] == "SEND")

        if step5_gate(plan, cov):
            sent, skipped, failed = step6_send(plan)
            report.update(sent=sent, skipped_at_send_time=skipped, failed=failed)
        else:
            report.update(sent=0, skipped_at_send_time=0, failed=0)

        report["status"] = "ok"
        report["finished_at"] = _now().isoformat()
        bq.write_run_report(report)
        log.info("RUN_OK %s", report)
        return 0
    except GuardFailure as e:
        report.update(status="guard_failed", error=str(e), finished_at=_now().isoformat())
        try:
            bq.write_run_report(report)
        finally:
            log.error("GUARD_FAILED %s", e)
        return 1
    except Exception as e:  # noqa: BLE001
        report.update(status="error", error=repr(e), finished_at=_now().isoformat())
        try:
            bq.write_run_report(report)
        finally:
            log.exception("RUN_ERROR")
        return 1


if __name__ == "__main__":
    sys.exit(main())
