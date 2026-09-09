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

        found = C.template_discount_attributes(TEST_TEMPLATE_ID)
        r["template_discount_attrs"] = ",".join(found)
        if found:
            refusals.append(f"TemplateUsesDiscount:{','.join(found)}")

        if mode == "draft-test":
            if r["computed_audience"] == 0:
                refusals.append("EmptyAudience")
            if not refusals:
                cid = C.create_draft(
                    name=f"[TEST {started:%Y-%m-%d}] campaign layer draft, list {TEST_LIST_ID}",
                    subject="Tests — kampanu slanis (melnraksts, netiek sutits)",
                    list_id=TEST_LIST_ID, template_id=TEST_TEMPLATE_ID,
                    utm_campaign=os.environ.get("TEST_UTM", "2026-w37-tests"))
                r["draft_campaign_id"] = cid
                links = C.check_links(cid, as_contact=TEST_CONTACT)
                r["links_ok"] = len(links["ok"])
                r["links_failed"] = len(links["failed"])
                r["links_unresolved"] = len(links["dynamic_unresolved"])
                if links["failed"]:
                    refusals.append("LinksNot200:" + "; ".join(
                        f"{u} -> {s}" for u, s in links["failed"][:5]))
                if links["dynamic_unresolved"]:
                    refusals.append("PlaceholdersUnresolved:" +
                                    "; ".join(links["dynamic_unresolved"][:5]))
                log.info("DRAFT id=%s links ok=%s failed=%s unresolved=%s",
                         cid, r["links_ok"], r["links_failed"], r["links_unresolved"])

        r["refusals"] = " | ".join(refusals)
        r["status"] = "refused" if refusals else "ok"
        r["note"] = ("This layer creates drafts only; send_now() raises unconditionally. "
                     "Nothing here can reach a customer.")
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
