"""LIVE negative test of the send-time placeholder block (MAIN 2026-09-25, F7). Read-only.

MAIN, verbatim: "BUILD: a hard block in the send path — any '⟦' in subject, preheader or HTML =
REFUSED, before any customer send. Prove it with a negative test."

tests/test_send_guard.py proves the block offline, on letters written for the test. This proves it
on the LIVE Brevo templates: for every id it reads GET /smtp/templates/{id}, builds the content a
campaign made from that template would carry (its subject, previewText "", its htmlContent) and
hands exactly that to campaign.send_now() - the real function, in its real order - through
content_reader, with an approval lookup that finds nothing and campaign id 0.

What counts as proof, per template:
  with a ⟦     -> REFUSED_PLACEHOLDER, and the approval was never even looked up;
  without one  -> PASSED_CONTENT_GUARD_THEN_REFUSED_BY_APPROVAL_GATE ("Klusēšana nav piekrišana"),
                  i.e. the guard lets a clean letter through to the gate that was there before.
Anything else - an unreadable template, a different refusal, no refusal - is a failure, and so is a
run in which no template carried a ⟦ at all: a negative test that refused nothing proved nothing.

READ-ONLY BY CONSTRUCTION. campaign._call is wrapped before the first request: every call is
recorded, and any method other than GET raises NonGetRefused before a request exists. send_now()
itself reads nothing here (the content is injected) and cannot send in any case - without an
approval it refuses at the gate, and past the gate it refuses unconditionally.

Run where the Brevo key is readable (the campaign layer's service account):
  python send_guard_proof.py [--templates 229,232,233,236]
Prints ONE JSON report. Exit 0 = the block held on every template; 1 = it did not, or proved nothing.
"""
import argparse
import datetime as dt
import hashlib
import json
import re
import sys

import campaign as C

DEFAULT_TEMPLATES = (229, 232, 233, 236)
REFUSED_PLACEHOLDER = "REFUSED_PLACEHOLDER"
PASSED_THEN_GATE = "PASSED_CONTENT_GUARD_THEN_REFUSED_BY_APPROVAL_GATE"
# press.send_gate's words when there is no approval row - the refusal a clean letter must reach.
GATE_WORDS = "Klusēšana nav piekrišana"
PARTS = ("subject", "preheader", "html")


class NonGetRefused(RuntimeError):
    """A write was attempted from the read-only proof. Raised before any request is built."""


def guard_get_only(calls: list):
    """Wrap campaign._call: record every call, refuse anything but GET. Returns the original."""
    real = C._call

    def get_only(method, path, payload=None, timeout=30):
        calls.append({"method": str(method), "path": path})
        if str(method).upper() != "GET":
            raise NonGetRefused(
                f"send_guard_proof is read-only: {method} {path} refused before the network")
        return real(method, path, payload, timeout)
    C._call = get_only
    return real


def parse_ids(text: str):
    """'229,232' or '229+232' or '229 232' - '+' because a comma splits gcloud's --args list."""
    parts = [p for p in re.split(r"[,+\s]+", text or "") if p]
    if not parts or not all(p.isdigit() for p in parts):
        raise argparse.ArgumentTypeError(f"template ids expected, got {text!r}")
    return [int(p) for p in parts]


def prove(template_id: int) -> dict:
    """Read one live template, hand its content to send_now(), and say what happened."""
    entry = {"id": template_id}
    try:
        t = C.template(template_id)
        content = {"subject": t.get("subject") or "", "previewText": "",
                   "htmlContent": t.get("htmlContent") or ""}
    except Exception as e:  # noqa: BLE001 - reported, and it fails the run
        entry.update(outcome="TEMPLATE_UNREADABLE", message=repr(e)[:600], as_expected=False)
        return entry
    hits = C.placeholder_hits(content["subject"], content["previewText"], content["htmlContent"])
    entry.update(
        name=t.get("name"), is_active=t.get("isActive"), subject=content["subject"],
        html_sha256=hashlib.sha256(content["htmlContent"].encode("utf-8")).hexdigest(),
        placeholder_hits={"count_per_part": {p: sum(1 for h in hits if h["part"] == p)
                                             for p in PARTS},
                          "hits": hits})
    asked = []

    def no_approval(batch_id, build_id):
        asked.append([batch_id, build_id])
        return None
    try:
        C.send_now(0, "2099-01-01", "proof", "proof", approval_lookup=no_approval,
                   content_reader=lambda campaign_id: content)
        outcome, message = "NOT_REFUSED", "send_now returned: nothing stopped this letter"
    except C.PlaceholderLeft as e:
        outcome, message = REFUSED_PLACEHOLDER, str(e)
        if e.hits != hits:
            outcome = "REFUSED_PLACEHOLDER_WITH_OTHER_HITS"
    except C.SendRefused as e:
        outcome = PASSED_THEN_GATE if asked and GATE_WORDS in str(e) else "REFUSED_OTHERWISE"
        message = str(e)
    except Exception as e:  # noqa: BLE001 - reported, and it fails the run
        outcome, message = "ERROR", repr(e)
    entry.update(outcome=outcome, approval_lookup_called=bool(asked), message=message)
    entry["as_expected"] = ((outcome == REFUSED_PLACEHOLDER and not asked) if hits
                            else outcome == PASSED_THEN_GATE)
    return entry


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--templates", type=parse_ids,
                    default=list(DEFAULT_TEMPLATES),
                    help="Brevo template ids, separated by ',', '+' or spaces "
                         f"(default {','.join(map(str, DEFAULT_TEMPLATES))})")
    a = ap.parse_args(argv)
    calls = []
    real = guard_get_only(calls)
    try:
        results = [prove(tid) for tid in a.templates]
    finally:
        C._call = real
    non_get = [c for c in calls if c["method"].upper() != "GET"]
    negatives = [r for r in results if (r.get("placeholder_hits") or {}).get("hits")]
    ok = bool(results) and all(r["as_expected"] for r in results) and not non_get \
        and bool(negatives)
    report = {
        "proof": "MAIN 2026-09-25 F7 - send-time placeholder block, live negative test",
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "read_only_guard": (
            "campaign._call was wrapped before the first request: any method other than GET raises "
            "NonGetRefused before a request is built. send_now() ran with campaign_id=0, "
            "send_date=2099-01-01, the content injected and an approval lookup that finds nothing, "
            "so it read nothing and could not send."),
        "brevo_calls": calls,
        "non_get_attempts": non_get,
        "templates": results,
        "summary": {
            "templates": len(results),
            "with_placeholders": len(negatives),
            "refused_placeholder": sum(1 for r in results if r["outcome"] == REFUSED_PLACEHOLDER),
            "passed_then_approval_gate": sum(1 for r in results if r["outcome"] == PASSED_THEN_GATE),
            "not_as_expected": [r["id"] for r in results if not r["as_expected"]],
        },
        "ok": ok,
    }
    if not negatives:
        report["why_not_ok"] = "no template carried a ⟦, so the negative test refused nothing"
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
