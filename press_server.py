"""Contract B: the ONE authenticated endpoint the relay calls to press the day. docs/BOUNDARY.md.

ONE ENDPOINT, NOT TWO. A separate "preview the verdict" route would be a second road to the same
row, and the second road is always the one that is open when it should not be. `POST /press` is the
only route that decides anything. `GET /` answers a fixed string so Cloud Run's health probe has
something to talk to; it carries no batch, no verdict and no state, so it is not a road to anything.

SAME IMAGE, SAME CODE, ONE press.py. This runs from the identical container as the job, with a
different entry point, because a second implementation of the press checks would be a second set of
rules - and the entire value of the checks is that there is exactly one. This module never decides
whether the day may go; it gathers, calls press_live.verdict(), and repeats the answer.

THE VERDICT IS RETURNED IN BOTH DIRECTIONS, WITH HTTP 200 IN BOTH. A refusal is an answer, not a
transport failure. If a refusal came back as 4xx the relay would have to distinguish "the checks
said no" from "the endpoint is broken" by reading a body it was told was an error - and those two
must never be confused, because one means show Raivis a reason and the other means record a refusal
and do not send. Non-2xx here therefore means only: this request never reached a verdict.

PRESS_ID IS REQUIRED, AND A RETRY REPLAYS. The relay keeps one idempotency key stable across
retries. If a verdict already exists under that key it is returned VERBATIM and nothing is written -
not re-judged, because a retry means "I did not hear you", not "judge it again", and the world moves
between two calls. Requiring the key is deliberate: a relay that forgets it gets a loud 400 with a
row behind it rather than a second judgement nobody notices. NOTE FOR MAIN: press_id was not part of
the seam contract issued on 2026-09-09; it is required from 2026-09-10 onward.

THREE REFUSALS SIT IN FRONT OF THE CHECKS, DELIBERATELY OUTSIDE THEM. Unknown batch_id, a build_id
that disagrees with the frozen batch, a send_date that disagrees with it. "Is this request about a
real, current day" is a different question from "may the day go", and press.py must stay the single
place the press rules live. They are recorded like every other refusal.

CREDITS ARE READ LIVE, AND AN UNREADABLE CREDIT COUNT REFUSES. Passing zero would put a number in
press_verdict that looks like a measurement and was never measured, which is worse than a blank -
it would make NOT_ENOUGH_CREDITS fail for a reason that is not true.

A MISSING CREDENTIAL AND A WRONG ONE ARE TWO STATES AND ANSWER DIFFERENTLY. 503 SECRET_UNAVAILABLE
means this endpoint cannot read its own secret and can authenticate nobody; 401 with a recorded
BadPressSecret means somebody tried with the wrong one. Collapsing them would have hidden the second
behind the first, which is exactly what happened on 2026-09-10 before the grant landed.

WHAT IS RECORDED, AND WHAT IS DELIBERATELY NOT. Every request that reaches a verdict writes a
campaign_run_report row, and so does every refusal in front of it - a refusal that leaves no row is
afterwards indistinguishable from a button nobody pressed. The ONE exception is a request with NO
secret header at all: this service is publicly reachable (the relay is PHP on a shared host and
holds no Google identity), so unauthenticated scanners will find it, and recording them would turn
the table into noise until the signal drowned. A header that is PRESENT and WRONG is recorded,
because that is somebody trying.
"""
import datetime as dt
import hmac
import json
import logging
import os
import uuid

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bq
import campaign as C
import config as CFG
import gsecret
import press_live as PL

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("press-server")

PORT = int(os.environ.get("PORT", "8080"))
REPORT = f"{CFG.PROJECT}.{CFG.CONTROL}.campaign_run_report"
SECRET_HEADER = "X-Press-Secret"
SECRET_ID = "press_endpoint_secret"
MAX_BODY = 64 * 1024
REQUIRED = ("press_id", "batch_id", "build_id", "counts", "pressed_by", "pressed_at")


def _record(*, status, refusals="", note="", batch_id=None, press_id=None, run_id=None):
    """Write the outcome of one press call. Best effort about the WRITE, never about the DECISION.

    If the row cannot be written the answer still goes back - the verdict itself was already
    recorded by press_live.verdict(), which raises if IT cannot write, so the audit trail that
    actually gates the approval is never the one that is best effort.
    """
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    row = {"run_id": run_id or f"press-{uuid.uuid4().hex[:12]}",
           "started_at": now, "finished_at": now,
           "mode": "press-endpoint", "status": status,
           "refusals": refusals[:900], "note": note[:900],
           "batch_id": batch_id, "press_id": press_id}
    try:
        errs = bq.client().insert_rows_json(REPORT, [row])
        if errs:
            log.error("REPORT_INSERT_FAILED %s", errs[:2])
    except Exception as e:  # noqa: BLE001
        log.error("REPORT_INSERT_RAISED %r", e)


def _batch(batch_id: str):
    from google.cloud.bigquery import ScalarQueryParameter as P
    rows = bq.query(
        f"SELECT batch_id, send_date, assignment_build_id, campaign_count, audience_total "
        f"FROM `{CFG.PROJECT}.{CFG.CONTROL}.day_batch` WHERE batch_id = @b LIMIT 1",
        [P("b", "STRING", batch_id)])
    return dict(rows[0]) if rows else None


def handle_press(body: dict) -> tuple:  # noqa: PLR0911
    """The whole decision, as a function, so it can be exercised without a socket.

    Returns (http_status, answer_dict). Everything it refuses, it names.
    """
    missing = [k for k in REQUIRED if body.get(k) in (None, "")]
    if missing:
        r = {"error": "INCOMPLETE_PRESS",
             "detail": f"the press must carry {', '.join(REQUIRED)}; missing: "
                       f"{', '.join(missing)}. The counts are required AS THEY WERE IN THE MAIL, "
                       f"because afterwards nobody can say what was approved from what happens to "
                       f"be true now; press_id is required because a retry must be recognisable "
                       f"as the same press rather than judged twice."}
        _record(status="refused", refusals="IncompletePress:" + ",".join(missing),
                batch_id=body.get("batch_id"), press_id=body.get("press_id"))
        return 400, r

    press_id = str(body["press_id"])

    # THE REPLAY, and it comes before everything else on purpose. A retry is answered from what was
    # stored, so nothing downstream can write a second row, and the answer cannot drift because the
    # world moved between the two calls.
    prior = PL.verdict_by_press_id(press_id)
    if prior:
        try:
            checks = json.loads(prior["checks_json"] or "[]")
        except Exception:  # noqa: BLE001
            checks = []
        approved = PL.approval_by_press_id(press_id)
        answer = {"verdict_id": prior["verdict_id"], "send_date": str(prior["send_date"]),
                  "batch_id": str(body["batch_id"]), "press_id": press_id,
                  "may_press": bool(prior["may_press"]) and bool(approved),
                  "verdict_may_press": bool(prior["may_press"]),
                  "approval_recorded": bool(approved), "checks": checks,
                  "refusal_text_lv": prior["refusal_text_lv"] or "", "replayed": True}
        _record(status="ok" if answer["may_press"] else "refused",
                refusals="" if answer["may_press"] else "PressReplayedRefusal",
                batch_id=str(body["batch_id"]), press_id=press_id,
                note=f"replayed verdict {prior['verdict_id']} first judged at "
                     f"{prior['checked_at']}; nothing re-judged and nothing written")
        log.info("PRESS_REPLAY press_id=%s verdict=%s", press_id, prior["verdict_id"])
        return 200, answer

    batch_id = str(body["batch_id"])
    batch = _batch(batch_id)
    if not batch:
        r = {"error": "UNKNOWN_BATCH",
             "detail": f"batch {batch_id} does not exist on this side. The mail must name the "
                       f"batch it was written from, and only a batch this side froze can be "
                       f"pressed."}
        _record(status="refused", refusals=f"UnknownBatch:{batch_id}", batch_id=batch_id,
                press_id=press_id)
        return 409, r

    send_date = str(batch["send_date"])
    if str(body["build_id"]) != str(batch["assignment_build_id"]):
        r = {"error": "BUILD_ID_DISAGREES_WITH_BATCH",
             "detail": f"the press carries build {body['build_id']}, the frozen batch "
                       f"{batch_id} carries {batch['assignment_build_id']}. The day was "
                       f"recomputed after the mail: void the token and send the recomputed-day "
                       f"mail rather than pressing this one.",
             "send_date": send_date}
        _record(status="refused", refusals="BuildIdDisagreesWithBatch", batch_id=batch_id,
                press_id=press_id,
                note=f"press build_id={body['build_id']} batch={batch['assignment_build_id']}")
        return 409, r

    claimed = body.get("send_date")
    if claimed and str(claimed) != send_date:
        r = {"error": "SEND_DATE_DISAGREES_WITH_BATCH",
             "detail": f"the press names {claimed}, batch {batch_id} is for {send_date}."}
        _record(status="refused", refusals="SendDateDisagreesWithBatch", batch_id=batch_id,
                press_id=press_id)
        return 409, r

    try:
        credits = C.credit_headroom()
    except Exception as e:  # noqa: BLE001
        r = {"error": "CREDITS_UNREADABLE",
             "detail": f"Brevo's credit headroom could not be read at the press, so the credit "
                       f"check cannot be answered: {e!r}. Refusing rather than judging the day "
                       f"against a zero nobody measured.",
             "send_date": send_date}
        _record(status="refused", refusals="CreditsUnreadable", batch_id=batch_id,
                press_id=press_id, note=repr(e)[:400])
        return 503, r

    v = PL.verdict(send_date, credits, checked_by=str(body["pressed_by"]), press_id=press_id,
                   batch_id=batch_id)
    approval_recorded = False
    approval_error = None
    if v["may_press"]:
        try:
            PL.record_approval(send_date, str(body["pressed_by"]), batch_id,
                               body.get("counts") or {}, board_token=body.get("board_token"),
                               press_id=press_id, verdict_id=v["verdict_id"])
            approval_recorded = True
        except PL.PressRefused as e:
            # record_approval refuses on its own terms even after a passing verdict - a race, a
            # revoked row. Its refusal is the answer, and it is reported as one rather than as a
            # crash, because the relay has to show a human something.
            approval_error = str(e)
    answer = {
        "verdict_id": v["verdict_id"], "send_date": send_date, "batch_id": batch_id,
        "press_id": press_id,
        "may_press": v["may_press"] and approval_recorded,
        "verdict_may_press": v["may_press"],
        "approval_recorded": approval_recorded,
        "checks": v["checks"],
        "refusal_text_lv": v["refusal_text_lv"] or (approval_error or ""),
        "credits_available": credits,
        "replayed": False,
    }
    failed = ",".join(c["id"] for c in v["checks"] if not c["passed"])
    _record(status="ok" if answer["may_press"] else "refused",
            refusals=("PressRefused:" + failed) if failed else (
                "ApprovalNotRecorded" if not approval_recorded else ""),
            batch_id=batch_id, press_id=press_id,
            note=f"verdict {v['verdict_id']} may_press={v['may_press']} "
                 f"approval_recorded={approval_recorded} credits={credits}")
    return 200, answer


class Handler(BaseHTTPRequestHandler):
    server_version = "tiktik-campaign-press/1.0"

    def log_message(self, fmt, *args):  # noqa: A003
        log.info("HTTP %s", fmt % args)

    def _send(self, status: int, obj: dict):
        raw = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0] == "/":
            self._send(200, {"service": "tiktik campaign layer - press endpoint",
                            "press": "POST /press",
                            "note": "liveness only; this route decides nothing and holds no state"})
            return
        self._send(404, {"error": "NO_SUCH_ROUTE", "detail": "the only route is POST /press"})

    def do_POST(self):  # noqa: N802
        if self.path.split("?")[0] != "/press":
            self._send(404, {"error": "NO_SUCH_ROUTE", "detail": "the only route is POST /press"})
            return
        offered = self.headers.get(SECRET_HEADER)
        if not offered:
            # Not recorded, on purpose - see the module docstring. A scanner is not a press.
            self._send(401, {"error": "UNAUTHENTICATED",
                             "detail": f"{SECRET_HEADER} is required"})
            return
        try:
            expected = gsecret.read(SECRET_ID, env_override="PRESS_ENDPOINT_SECRET")
        except gsecret.SecretUnavailable as e:
            _record(status="refused", refusals="PressSecretUnavailable", note=str(e))
            self._send(503, {"error": "SECRET_UNAVAILABLE",
                             "detail": "this endpoint cannot read its own credential, so it "
                                       "cannot authenticate anyone. Refusing."})
            return
        if not hmac.compare_digest(offered, expected):
            _record(status="refused", refusals="BadPressSecret",
                    note=f"a press arrived with a wrong {SECRET_HEADER}")
            self._send(401, {"error": "UNAUTHENTICATED", "detail": "wrong secret"})
            return
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send(400, {"error": "BAD_BODY",
                             "detail": f"expected a JSON body of 1..{MAX_BODY} bytes"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("the press body must be a JSON object")
        except Exception as e:  # noqa: BLE001
            self._send(400, {"error": "BAD_JSON", "detail": repr(e)[:300]})
            return
        try:
            status, answer = handle_press(body)
        except Exception as e:  # noqa: BLE001
            # An unexpected failure must still leave a record and must NEVER read as permission.
            log.exception("PRESS_ENDPOINT_ERROR")
            _record(status="error", refusals="PressEndpointError", note=repr(e)[:600],
                    batch_id=body.get("batch_id"), press_id=body.get("press_id"))
            self._send(500, {"error": "PRESS_ENDPOINT_ERROR", "detail": repr(e)[:300],
                             "may_press": False})
            return
        self._send(status, answer)


def main():
    log.info("PRESS_ENDPOINT_LISTENING port=%s project=%s", PORT, CFG.PROJECT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
