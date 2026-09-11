"""Entry point for the campaign layer. Creates drafts; never sends; reports what it refused.

Modes:
  preflight      - can the credential be read, how many credits are there, is the allowlisted list
                   still one person. Touches nothing.
  draft-test     - the end-to-end that can be run before any send exists: one draft to the
                   allowlisted list, its links checked AS a real contact, the numbers written down.
  template-scan  - what every mapped template renders, and what blocks it.
  batch          - freeze the day, then PUSH it to the relay (contract A). The push result is part
                   of this row: a batch that is frozen and never handed over is a report nobody can
                   write, and it must not read as a quiet day.
  repush         - hand over a batch that is ALREADY frozen, by batch_id, without recomputing the
                   day. This is what a retry after a failed push must do - the frozen batch is the
                   record of what was decided, and rebuilding it at retry time would quietly hand
                   over a different day than the one that failed. It is also how idempotence is
                   proved: the same build_id twice, from the same bytes.
  dispatch       - write the dispatch fact for a campaign that has already gone out.
  press-verdict  - run the three press-time checks against the LIVE system and record the verdict.
                   Writes NO approval row: this is how the refusal is proved before the relay
                   exists, not a second road to an approval.
  press-selftest - call the deployed press endpoint AS the relay would, with the real secret read
                   from Secret Manager, so contract B is provable end to end without the value ever
                   passing through a conversation. Send the same PRESS_ID twice to prove the replay.
  secret-check   - which of the named secrets THIS identity can read. Prints the name and readable
                   or not, never the value, never its length, never a hash.

Every run writes a row to mkt_control.campaign_run_report, including the runs that refuse. A run
that refused and left no row behind is indistinguishable from a run that never happened, and this
node has met that failure five times.

WHERE PROOF MAY COME FROM, since 2026-09-10: this job, under its own service account. The zero-day
batch of 09.09. was built under `ops-cloudshell-runner` and recorded `ops-verify-2026-09-09` in
day_batch.built_by, so it proved the code and not the road. Anything that works only from the ops
shell is not proven - it is bypassed.
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
import gsecret
import press_live as PL
import push as PUSH
import utm as UTM

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

    Read from BigQuery when this identity may run queries, and from APPROVED_ATTRIBUTES otherwise.
    A guard that cannot read its own allowlist must refuse, never wave things through.
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


def _frozen(batch_id: str) -> dict:
    """Read a batch back out of the warehouse in the shape push.payload() expects.

    Deliberately a READ and not a rebuild. `batch.build()` would produce a new batch_id, a new
    built_at and possibly different numbers - which is a different day wearing the same name.
    """
    from google.cloud.bigquery import ScalarQueryParameter as P
    head = bq.query(
        f"SELECT batch_id, send_date, built_at, built_by, assignment_build_id, campaign_count, "
        f"audience_total, dedup_overlap, credit_headroom, presentable, blocking_reasons, note "
        f"FROM `{PROJECT}.mkt_control.day_batch` WHERE batch_id = @b "
        f"ORDER BY built_at DESC LIMIT 1", [P("b", "STRING", batch_id)])
    if not head:
        raise RuntimeError(
            f"batch {batch_id} does not exist, so there is nothing to hand over. A repush of a "
            f"batch nobody froze would be an invented day.")
    camps = bq.query(
        f"SELECT batch_id, send_date, email_type, track, utm_campaign, template_id, "
        f"template_active, template_approved, audience, criteria, blocking "
        f"FROM `{PROJECT}.mkt_control.day_batch_campaign` WHERE batch_id = @b ORDER BY email_type",
        [P("b", "STRING", batch_id)])
    return {"head": dict(head[0]), "campaigns": [dict(c) for c in camps]}


def _record_push(r: dict, refusals: list, pushed: dict):
    r["push_status"] = pushed["status"]
    r["push_http"] = pushed["http_status"]
    r["push_detail"] = pushed["detail"][:900]
    log.info("PUSH status=%s http=%s sha256=%s detail=%s",
             pushed["status"], pushed["http_status"], pushed.get("sha256"), pushed["detail"])
    if pushed["status"] != PUSH.SENT:
        refusals.append(f"PushNotDelivered:{pushed['status']}:{pushed['detail'][:300]}")


def main() -> int:  # noqa: PLR0912, PLR0915
    mode = os.environ.get("MODE", "preflight")
    started = dt.datetime.now(dt.timezone.utc)
    r = {"started_at": started.isoformat(), "mode": mode, "credential_ok": False,
         "list_id": TEST_LIST_ID, "template_id": TEST_TEMPLATE_ID, "refusals": ""}
    refusals = []
    try:
        if mode == "secret-check":
            # Deliberately BEFORE the Brevo credential: the whole point is to answer "what can this
            # identity read", and failing on an unrelated secret would answer a different question.
            ids = [s.strip() for s in os.environ.get(
                "SECRET_IDS", "relay_secret,press_endpoint_secret").split(",") if s.strip()]
            seen = []
            for sid in ids:
                try:
                    gsecret.read(sid)
                    seen.append(f"{sid}=READABLE")
                except gsecret.SecretUnavailable:
                    seen.append(f"{sid}=UNREADABLE")
                    refusals.append(f"SecretUnreadable:{sid}")
            r["note"] = "; ".join(seen)
            r["refusals"] = " | ".join(refusals)
            r["status"] = "refused" if refusals else "ok"
            log.info("SECRET_CHECK %s", r["note"])
            r["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            _write(r)
            return 0

        if mode == "repush":
            # No Brevo credential is needed to hand over a day that was already decided, and asking
            # for one would make a retry depend on a system that has nothing to do with it.
            batch_id = os.environ["PUSH_BATCH_ID"]
            frozen = _frozen(batch_id)
            r["batch_id"] = batch_id
            pushed = PUSH.push_batch(frozen)
            _record_push(r, refusals, pushed)
            r["note"] = (f"repush of frozen batch {batch_id} build "
                         f"{frozen['head']['assignment_build_id']}: {pushed['bytes']} bytes, "
                         f"sha256 {pushed['sha256']}, {pushed['campaigns']} campaign(s). Nothing "
                         f"was recomputed; these are the rows the freeze wrote.")
            r["refusals"] = " | ".join(refusals)
            r["status"] = "refused" if refusals else "ok"
            r["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            _write(r)
            return 0

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
                params = C.template_params(tid)
                block = []
                if params:
                    block.append("USES_PARAMS_ALWAYS_EMPTY_IN_CAMPAIGN")
                if not t.get("isActive"):
                    block.append("INACTIVE_IN_BREVO")
                if [a for a in attrs if a not in approved]:
                    block.append("ATTRIBUTE_NOT_APPROVED")
                rows.append({
                    "checked_at": started.isoformat(), "run_id": RUN_ID, "template_id": tid,
                    "template_name": (t.get("name") or "")[:200],
                    "is_active": bool(t.get("isActive")),
                    "attributes": ",".join(attrs), "attribute_count": len(attrs),
                    "unapproved": ",".join(a for a in attrs if a not in approved),
                    "discount_shaped_pairs": ",".join(C.discount_shaped_pairs(attrs)),
                    "params_referenced": ",".join(params),
                    "blocking": ",".join(block),
                })
            if rows:
                errs = bigquery.Client(project=PROJECT).insert_rows_json(
                    f"{PROJECT}.mkt_control.template_attribute_usage", rows)
                if errs:
                    raise RuntimeError(f"template_attribute_usage insert failed: {errs[:2]}")
            r["note"] = (f"scanned {len(rows)} template(s); "
                         f"{sum(1 for x in rows if x['unapproved'])} render something outside the "
                         f"approved set of {len(approved)}; "
                         f"{sum(1 for x in rows if x['params_referenced'])} reference params, which "
                         f"are always empty in a campaign")
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
            r["batch_id"] = head["batch_id"]
            r["note"] = (f"batch {head['batch_id']} for {send_date}: "
                         f"{head['campaign_count']} campaign(s), audience "
                         f"{head['audience_total']}, presentable={head['presentable']}")
            if head["blocking_reasons"]:
                refusals.append("BatchBlocking:" + head["blocking_reasons"][:500])

            # CONTRACT A, and it belongs INSIDE the night run rather than beside it. Freezing a
            # batch nobody receives is a report that cannot be written, and the failure has to be
            # visible in the same row as the batch it concerns.
            pushed = PUSH.push_batch(out)
            _record_push(r, refusals, pushed)
            r["note"] += f" | push {pushed['bytes']} bytes sha256 {pushed['sha256']}"

        if mode == "press-verdict":
            # The three press checks, against the live system, recorded. NO approval row is written
            # here - record_approval is reached only through the endpoint, on the relay's call.
            # PRESS_BATCH_ID is required: a verdict judges the batch a human was shown, never
            # "the newest batch for the date" (MAIN, 2026-09-11).
            send_date = os.environ["SEND_DATE"]
            r["batch_id"] = os.environ.get("PRESS_BATCH_ID", "") or None
            v = PL.verdict(send_date, r["brevo_credits"],
                           checked_by=os.environ.get("PRESSED_BY", f"campaign-job/{RUN_ID}"),
                           batch_id=r["batch_id"])
            failed = [c["id"] for c in v["checks"] if not c["passed"]]
            r["note"] = (f"verdict {v['verdict_id']} for {send_date}: may_press={v['may_press']}; "
                         f"failed checks: {', '.join(failed) or 'none'}")
            log.info("PRESS_VERDICT_MODE %s", r["note"])
            log.info("PRESS_REFUSAL_TEXT_LV %s", v["refusal_text_lv"] or "-")
            if failed:
                refusals.append("PressVerdictRefused:" + ",".join(failed))

        if mode == "press-selftest":
            # Contract B, exercised as the relay will exercise it. The secret is read from Secret
            # Manager by this service account and never printed, so the path is proved without the
            # value being seen. This is the same single endpoint, not a second one.
            body = {"press_id": os.environ.get("PRESS_ID", ""),
                    "batch_id": os.environ.get("PRESS_BATCH_ID", ""),
                    "build_id": os.environ.get("PRESS_BUILD_ID", ""),
                    "send_date": os.environ.get("SEND_DATE", ""),
                    "counts": json.loads(os.environ.get("PRESS_COUNTS", "{}")),
                    "pressed_by": os.environ.get("PRESSED_BY", "selftest"),
                    "pressed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
            res = PUSH.press_selftest(body=body)
            r["batch_id"] = body["batch_id"] or None
            r["press_id"] = body["press_id"] or None
            r["note"] = f"press endpoint {res['status']} http={res['http_status']}"
            log.info("PRESS_SELFTEST %s http=%s answer=%s",
                     res["status"], res["http_status"], (res["answer"] or "")[:1800])
            if res["status"] != PUSH.SENT:
                refusals.append(f"PressSelftest:{res['status']}:{res['detail'][:300]}")
            else:
                try:
                    ans = json.loads(res["answer"] or "{}")
                    if not ans.get("may_press"):
                        refusals.append("PressSelftestRefused:" + ",".join(
                            c["id"] for c in ans.get("checks", []) if not c.get("passed")))
                except Exception:  # noqa: BLE001
                    refusals.append("PressSelftestUnparseableAnswer")

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
                # UTM SEAM v1: the week comes from utm.py for the day the draft is for, never from
                # the template. The draft is built from the template's HTML after replacement.
                week = UTM.iso_week(dt.date.fromisoformat(
                    os.environ.get("SEND_DATE") or dt.date.today().isoformat()))
                email_type = os.environ.get("DRAFT_EMAIL_TYPE", "").strip()
                if not email_type:
                    raise C.UtmSeamRefused(
                        "DRAFT_EMAIL_TYPE is required: it is the internal_label of every "
                        "utm_dictionary row this draft writes, and a decode row without one "
                        "decodes nothing.")
                d = C.create_draft(
                    approved_attributes=approved,
                    name=f"[TEST {started:%Y-%m-%d}] {email_type} {week} tpl {TEST_TEMPLATE_ID}, "
                         f"list {TEST_LIST_ID}",
                    list_id=TEST_LIST_ID, template_id=TEST_TEMPLATE_ID, week=week)
                cid = d["id"]
                r["draft_campaign_id"] = cid
                r["note"] = (f"draft {cid} from template {TEST_TEMPLATE_ID} "
                             f"(active={d['template_active']}) week {week}, "
                             f"{len(d['pairs'])} utm pair(s): "
                             + ", ".join(f"{c}/{ct or '-'}" for c, ct in d["pairs"]))
                links = C.check_links(cid, as_contact=TEST_CONTACT, expect_utm=f"{week}-")
                r["links_ok"] = len(links["ok"])
                r["links_failed"] = len(links["failed"])
                r["links_unresolved"] = len(links["dynamic_unresolved"])
                r["links_system"] = len(links["brevo_system"])
                if links["failed"]:
                    refusals.append("LinksNot200:" + "; ".join(
                        f"{u} -> {s}" for u, s in links["failed"][:5]))
                if links.get("missing_utm"):
                    refusals.append("LinksWithoutOurUtm:" + "; ".join(links["missing_utm"][:5]))
                if links["dynamic_unresolved"]:
                    refusals.append("PlaceholdersUnresolved:" +
                                    "; ".join(links["dynamic_unresolved"][:5]))
                # A DIFFERENT FACT, AND THEREFORE A DIFFERENT WORD. Zero checkable links is not an
                # unresolved placeholder: it is a letter with nothing to click. Brevo's own tags do
                # not count towards it, which is exactly why they had to stop being counted as
                # unresolved placeholders - otherwise one refusal hid the other permanently.
                if links["checked"] == 0:
                    refusals.append(
                        f"no_real_links: the rendered campaign has no checkable http link at all "
                        f"({len(links['brevo_system'])} Brevo system tag(s), "
                        f"{len(links['dynamic_unresolved'])} unresolved). A letter with nothing to "
                        f"click cannot be attributed and has no reason to arrive.")
                log.info("DRAFT id=%s links ok=%s failed=%s unresolved=%s system=%s checked=%s",
                         cid, r["links_ok"], r["links_failed"], r["links_unresolved"],
                         r["links_system"], links["checked"])
                log.info("DRAFT_LINKS_OK %s", [f"{u} -> {st}" for u, st in links["ok"]])
                # utm_dictionary: written ONLY here (and by the planner's whole-campaign row), MERGE,
                # one row per pair - and only for a draft that passed, so a refused letter leaves
                # no decode row for a campaign that will not exist.
                if not refusals:
                    n = bq.merge_utm_rows([(c, ct, email_type) for c, ct in d["pairs"]],
                                          added_by="tiktik-campaign-layer",
                                          notes=f"per-link decode row from template "
                                                f"{TEST_TEMPLATE_ID}, UTM seam v1")
                    r["note"] += f" | utm_dictionary rows present for these pairs: {n}"
                if os.environ.get("DELETE_DRAFT_AFTER", "").lower() == "true":
                    gone = C.delete_draft(cid)
                    r["note"] += f" | draft {cid} deleted, GET answers 404: {gone}"
                    if not gone:
                        refusals.append(f"DraftNotDeleted:{cid}")

        r["refusals"] = " | ".join(refusals)
        r["status"] = "refused" if refusals else "ok"
        # APPEND, never overwrite. The first version assigned here and clobbered the note each mode
        # had just written, so the template scan reported the boilerplate instead of its own count.
        r["note"] = ((r.get("note") + " | ") if r.get("note") else "") + (
            "This layer creates drafts only; send_now() raises unconditionally. "
            "Nothing here can reach a customer.")
    except (C.TemplateInactive, C.TemplateUsesParams, C.TemplateUsesUnapprovedAttribute,
            C.TemplateUsesDiscount, C.ListNotAllowed, C.EmptyAudience, C.UtmSeamRefused) as e:
        # A structural refusal is a REPORTED outcome, not a crash: it is the guard doing its job,
        # and it must land in the report with its reason rather than as a stack trace.
        r.update(status="refused", refusals=f"{type(e).__name__}: {str(e)[:600]}")
    except PL.PressRefused as e:
        # Same shape, and deliberately NOT inside the tuple above: this one is the press refusing,
        # which is a different thing from a template refusing, and a reader of the report should be
        # able to tell them apart without opening the text.
        r.update(status="refused", refusals=f"PressRefused: {str(e)[:600]}")
    except C.CredentialUnavailable as e:
        r.update(status="credential_unavailable", error=str(e)[:900])
    except Exception as e:  # noqa: BLE001
        r.update(status="error", error=repr(e)[:900])
        log.exception("CAMPAIGN_JOB_ERROR")
    finally:
        if "finished_at" not in r:
            r["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            _write(r)
    return 0 if r.get("status") in ("ok", "refused") else 1


if __name__ == "__main__":
    sys.exit(main())
