"""Entry point for the campaign layer. Creates drafts; never sends; reports what it refused.

Modes:
  preflight  - can the credential be read, how many credits are there, is the allowlisted list
               still one person. Touches nothing.
  draft-test - the end-to-end that can be run before any send exists: one draft to the allowlisted
               list, its links checked AS a real contact, the numbers written down.

Every run writes a row to mkt_control.campaign_run_report, including the runs that refuse. A run
that refused and left no row behind is indistinguishable from a run that never happened, and this
node has met that failure five times.
"""
import datetime as dt
import json
import logging
import os
import sys
import uuid

from google.cloud import bigquery

import batch as B
import bq
import campaign as C

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("campaign-job")

RUN_ID = os.environ.get("CLOUD_RUN_EXECUTION") or f"local-{uuid.uuid4().hex[:12]}"
PROJECT = os.environ.get("BQ_PROJECT", "jaunais-za-aizv04022026")
REPORT = f"{PROJECT}.mkt_control.campaign_run_report"
TEST_LIST_ID = int(os.environ.get("TEST_LIST_ID", "62"))
TEST_TEMPLATE_ID = int(os.environ.get("TEST_TEMPLATE_ID", "20"))
TEST_CONTACT = os.environ.get("TEST_CONTACT", "alenda.jurmala@gmail.com")


def _write(report: dict):
    report["run_id"] = RUN_ID
    errors = bigquery.Client(project=PROJECT).insert_rows_json(REPORT, [report])
    if errors:
        log.error("REPORT_INSERT_FAILED %s", errors[:2])
    else:
        log.info("REPORT_WRITTEN %s", json.dumps(report, default=str))


def _approved_attributes():
    """The approved attribute set. FAIL-CLOSED: unreadable or empty means nothing is approved.

    Read from BigQuery when this identity may run queries, and from APPROVED_ATTRIBUTES otherwise -
    the campaign layer's service account holds dataset WRITER but not bigquery.jobs.create as of
    2026-09-09. A guard that cannot read its own allowlist must refuse, never wave things through.
    """
    env = os.environ.get("APPROVED_ATTRIBUTES", "").strip()
    if env:
        return {a.strip() for a in env.split(",") if a.strip()}
    try:
        rows = bq.query(
            f"SELECT attribute FROM `{PROJECT}.mkt_control.template_attribute_allowlist` "
            f"WHERE approved")
        return {r["attribute"] for r in rows}
    except Exception as e:  # noqa: BLE001
        log.warning("ALLOWLIST_UNREADABLE %r - refusing everything, the safe direction", e)
        return set()


def _params(send_date, email_type):
    from google.cloud.bigquery import ScalarQueryParameter as P
    return [P("d", "DATE", send_date), P("t", "STRING", email_type)]


def main() -> int:
    mode = os.environ.get("MODE", "preflight")
    started = dt.datetime.now(dt.timezone.utc)
    r = {"started_at": started.isoformat(), "mode": mode, "credential_ok": False,
         "list_id": TEST_LIST_ID, "template_id": TEST_TEMPLATE_ID, "refusals": ""}
    refusals = []
    try:
        C.api_key()
        r["credential_ok"] = True
        r["brevo_credits"] = C.credit_headroom()
        r["computed_audience"] = C.effective_audience(TEST_LIST_ID)
        log.info("PREFLIGHT credits=%s audience(list %s)=%s",
                 r["brevo_credits"], TEST_LIST_ID, r["computed_audience"])

        approved = _approved_attributes()
        found = C.template_discount_attributes(TEST_TEMPLATE_ID)
        r["template_discount_attrs"] = ",".join(found)

        if mode == "template-scan":
            ids = [int(x) for x in os.environ.get("TEMPLATE_IDS", "").split(",") if x.strip()]
            rows = []
            for tid in ids:
                t = C.template(tid)
                attrs = C.template_attributes(tid)
                rows.append({
                    "checked_at": started.isoformat(), "run_id": RUN_ID, "template_id": tid,
                    "template_name": (t.get("name") or "")[:200],
                    "is_active": bool(t.get("isActive")),
                    "attributes": ",".join(attrs), "attribute_count": len(attrs),
                    "unapproved": ",".join(a for a in attrs if a not in approved),
                    "discount_shaped_pairs": ",".join(C.discount_shaped_pairs(attrs)),
                })
            if rows:
                errs = bigquery.Client(project=PROJECT).insert_rows_json(
                    f"{PROJECT}.mkt_control.template_attribute_usage", rows)
                if errs:
                    raise RuntimeError(f"template_attribute_usage insert failed: {errs[:2]}")
            r["note"] = (f"scanned {len(rows)} template(s); "
                         f"{sum(1 for x in rows if x['unapproved'])} render something outside the "
                         f"approved set of {len(approved)}")
            log.info("TEMPLATE_SCAN %s", r["note"])
        if found:
            refusals.append(f"TemplateUsesDiscount:{','.join(found)}")

        if mode == "batch":
            send_date = os.environ.get("SEND_DATE") or str(
                dt.date.today() + dt.timedelta(days=1))
            out = B.build(send_date, RUN_ID,
                          template_is_active=C.template_is_active,
                          credits=r["brevo_credits"])
            head = out["head"]
            r["note"] = (f"batch {head['batch_id']} for {send_date}: "
                         f"{head['campaign_count']} campaign(s), audience "
                         f"{head['audience_total']}, presentable={head['presentable']}")
            if head["blocking_reasons"]:
                refusals.append("BatchBlocking:" + head["blocking_reasons"][:500])

        if mode == "dispatch":
            # Writes the dispatch FACT for a campaign that has already gone out. It does not send
            # and cannot: it reads campaignStats and records what Brevo says happened.
            cid = int(os.environ["DISPATCH_CAMPAIGN_ID"])
            send_date = os.environ["SEND_DATE"]
            email_type = os.environ["DISPATCH_EMAIL_TYPE"]
            stats = C.campaign_stats(cid, [TEST_LIST_ID])
            planned = bq.query(
                f"SELECT master_key FROM {bq.C.T_AUDIENCE_SNAPSHOT} "
                f"WHERE send_date = @d AND email_type = @t AND dispatch_state = 'planned'",
                _params(send_date, email_type))
            # A campaign send is all-or-nothing per list: Brevo reports per campaign, never per
            # person, so 'sent' here means the letter WENT OUT, which is true for everyone in the
            # list. Whether it was delivered is a different fact and lives in the log's own
            # columns - conflating the two would advance a rung on a bounce.
            results = [(row["master_key"], "sent", None) for row in planned]
            done = bq.complete_dispatch(RUN_ID, send_date, email_type, cid,
                                        f"dispatch {cid}", results)
            r["draft_campaign_id"] = cid
            r["note"] = (f"campaign {cid}: brevo sent={stats['sent']} "
                         f"delivered={stats['delivered']}; snapshot rows closed="
                         f"{done['updated']}, log rows written={done['logged']}")
            if done["logged"] == 0:
                refusals.append("DispatchWroteNothing")

        if mode == "draft-test":
            if r["computed_audience"] == 0:
                refusals.append("EmptyAudience")
            if not refusals:
                cid = C.create_draft(
                    approved_attributes=approved,
                    name=f"[TEST {started:%Y-%m-%d}] campaign layer draft, list {TEST_LIST_ID}",
                    subject="Tests — kampanu slanis (melnraksts, netiek sutits)",
                    list_id=TEST_LIST_ID, template_id=TEST_TEMPLATE_ID,
                    utm_campaign=os.environ.get("TEST_UTM", "2026-w37-tests"))
                r["draft_campaign_id"] = cid
                links = C.check_links(cid, as_contact=TEST_CONTACT,
                                      expect_utm=os.environ.get("TEST_UTM", "2026-w37-tests"))
                r["links_ok"] = len(links["ok"])
                r["links_failed"] = len(links["failed"])
                r["links_unresolved"] = len(links["dynamic_unresolved"])
                if links["failed"]:
                    refusals.append("LinksNot200:" + "; ".join(
                        f"{u} -> {s}" for u, s in links["failed"][:5]))
                if links.get("missing_utm"):
                    refusals.append("LinksWithoutOurUtm:" + "; ".join(links["missing_utm"][:5]))
                if links["dynamic_unresolved"]:
                    refusals.append("PlaceholdersUnresolved:" +
                                    "; ".join(links["dynamic_unresolved"][:5]))
                log.info("DRAFT id=%s links ok=%s failed=%s unresolved=%s",
                         cid, r["links_ok"], r["links_failed"], r["links_unresolved"])

        r["refusals"] = " | ".join(refusals)
        r["status"] = "refused" if refusals else "ok"
        # APPEND, never overwrite. The first version assigned here and clobbered the note each mode
        # had just written, so the template scan reported the boilerplate instead of its own count -
        # the run said nothing about what it had actually done.
        r["note"] = ((r.get("note") + " | ") if r.get("note") else "") + (
            "This layer creates drafts only; send_now() raises unconditionally. "
            "Nothing here can reach a customer.")
    except (C.TemplateInactive, C.TemplateUsesUnapprovedAttribute, C.ListNotAllowed,
            C.EmptyAudience) as e:
        # A structural refusal is a REPORTED outcome, not a crash: it is the guard doing its job,
        # and it must land in the report with its reason rather than as a stack trace.
        r.update(status="refused", refusals=f"{type(e).__name__}: {str(e)[:600]}")
    except C.CredentialUnavailable as e:
        r.update(status="credential_unavailable", error=str(e)[:900])
    except Exception as e:  # noqa: BLE001
        r.update(status="error", error=repr(e)[:900])
        log.exception("CAMPAIGN_JOB_ERROR")
    finally:
        r["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        _write(r)
    return 0 if r.get("status") in ("ok", "refused") else 1


if __name__ == "__main__":
    sys.exit(main())
