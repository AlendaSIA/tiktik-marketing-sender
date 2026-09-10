"""Contract A: the campaign side PUSHES the frozen day batch to the relay. See docs/BOUNDARY.md.

THE RELAY HAS NO BIGQUERY ACCESS, and that one fact chooses the direction. Nothing pulls. The
campaign job POSTs the frozen batch to `send-ingest.php` with a shared secret, and the relay stores
what arrives byte for byte and renders the board and the digest from what it stored. It derives
nothing, not even a sum.

WHAT IS SENT IS WHAT WAS FROZEN. The payload is assembled from the rows `batch.build()` has just
written and from nothing else - no second query, no recomputed total. Every number the relay prints
is a number this side computed; otherwise the mail and the warehouse can disagree while both look
right, and nobody finds out until a customer does. The SHAPE of it is the seam contract MAIN issued
on 2026-09-09 in one wording to both sides; a field is not added here because it seemed useful.

A FAILED PUSH IS A REFUSAL, LOUD, IN THE NIGHT REPORT - and that is why nothing here raises for the
ordinary failures. An unconfigured relay, a timeout, a 500: each RETURNS a named status, and
campaign_job writes it into mkt_control.campaign_run_report next to the batch it belongs to. A
raise would be caught by that job's `except Exception` and recorded as "error", which says nothing
about whether the day was handed over. The distinction has to survive into the report because the
two cases need different mails: "nothing arrived" is an alarm, "there was nothing to send" is a
quiet day.

AND IT IS NEVER A SILENT SKIP. There is no branch here that returns success because the relay is
not configured yet. `refused_unconfigured` is a status the report shows and the caller turns into a
refusal, precisely so that the day the relay IS configured, nobody has to remember to remove a
temporary allowance that stopped being temporary.

IT SAYS WHO IT IS. The first real push, 2026-09-10, answered 403 - and not from the relay.
Cloudflare error 1010, `browser_signature_banned`, before PHP saw the request at all, because
urllib introduces itself as `Python-urllib/3.12` and that signature is blocked. check_links has
carried a name since it was written and this did not, so the fix is a missing name rather than a
wall to climb. The name is OURS - a client that dresses as a browser to get past a rule is lying to
a site we own, and the next person reading that access log could not tell the night run from a
scraper. If an honest name is still refused, that is an allowlist decision for the owner of
plani.tiktik.lv, and it is asked for rather than worked around.

THE SECRET IS READ BY VERSION NUMBER, NOT BY `latest`, and this is not caution for its own sake.
The relay's copy of `relay_secret` is being rotated after it leaked on their side; the new version
exists here BEFORE the relay accepts it, and `latest` means "the newest enabled version". Following
latest through a rotation therefore sends a value the other side rejects, and a 401 caused by a
rotation looks exactly like a 401 caused by a broken push - the wrong thing to spend an evening on.
So RELAY_SECRET_VERSION must be present and must be DIGITS: the literal "latest" is refused, which
makes the mistake structurally unavailable rather than merely discouraged. Pinned to 5 on
2026-09-10, which the relay accepts both during the rotation and after it.

IDEMPOTENCE IS THE RELAY'S, AND IT IS KEYED ON build_id. The same build_id pushed twice is the same
day stored twice over one row. A DIFFERENT build_id after a mail has gone out means the day was
recomputed: the relay voids the old token and sends a short "day recomputed" mail. This side's only
duty is to report the build_id honestly, which is why it is copied out of the frozen head and never
regenerated here.
"""
import datetime as dt
import json
import logging
import os
import re
import urllib.error
import urllib.request

import gsecret

log = logging.getLogger("push")

# The header carrying the shared secret. Named rather than invented at the call site so the relay
# side has one string to match and a grep finds every place it is used.
SECRET_HEADER = "X-Relay-Secret"
SECRET_ID = "relay_secret"
SECRET_VERSION_ENV = "RELAY_SECRET_VERSION"
URL_ENV = "RELAY_INGEST_URL"
TIMEOUT_S = float(os.environ.get("PUSH_TIMEOUT_S", "20"))
# Our own name, in the same shape check_links has used since it was written. Never a browser's.
USER_AGENT = "tiktik-campaign-push/1.0"

_VERSION_NUMBER = re.compile(r"^[0-9]+$")

# Statuses. They are strings in the report, so they are defined once and spelled once.
SENT = "sent"
REFUSED_UNCONFIGURED = "refused_unconfigured"
FAILED = "failed"


def _criteria(raw):
    """The frozen row keeps `criteria` as a JSON string because BigQuery has no nested literal here.

    It is parsed back into structure before it goes over the wire - the relay should receive a list
    of {chosen_because, people}, not a string containing one. This is a change of ENCODING and not
    of content: the values are the ones the batch froze, and if the string will not parse it travels
    as-is rather than being replaced by something tidier that says less.
    """
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw or "[]")
    except Exception:  # noqa: BLE001
        return raw


def payload(built: dict) -> dict:
    """Assemble contract A's payload from the frozen batch. Pure: no clock beyond generated_at.

    Kept separate from the POST so the shape can be inspected and asserted without a relay, a
    secret or a network - the same reason press.py holds no clients. The night run can therefore
    prove WHAT it would hand over on a day when the relay does not answer.
    """
    head = built["head"]
    return {
        "batch_id": head["batch_id"],
        "build_id": head["assignment_build_id"],
        "target_send_date": str(head["send_date"]),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "day": {
            "campaign_count": head["campaign_count"],
            "audience_total": head["audience_total"],
            "dedup_overlap": head["dedup_overlap"],
            "brevo_credits": head["credit_headroom"],
            "presentable": head["presentable"],
            "blocking_reasons": head["blocking_reasons"],
            "day_note": head["note"],
            "built_at": head["built_at"],
            "built_by": head["built_by"],
        },
        "campaigns": [
            {
                "variant": c["email_type"],
                "track": c["track"],
                "audience": c["audience"],
                "chosen_because": _criteria(c["criteria"]),
                "template_id": c["template_id"],
                "template_active": c["template_active"],
                "template_approved": c["template_approved"],
                "utm_campaign": c["utm_campaign"],
                "blocking": c["blocking"],
            }
            for c in built["campaigns"]
        ],
    }


def push_batch(built: dict, url: str = None, timeout: float = None) -> dict:
    """Hand the frozen batch to the relay. Returns what happened; never raises for a bad relay.

    The return is always the same shape - status, http_status, detail, bytes, campaigns - so the
    report row can be filled without the caller knowing which branch it came from. `http_status`
    is None when no answer was received AT ALL, which is a different fact from a bad status and
    must not be flattened into one.
    """
    body = payload(built)
    raw = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
    out = {"status": FAILED, "http_status": None, "detail": "",
           "bytes": len(raw), "campaigns": len(body["campaigns"]),
           "batch_id": body["batch_id"], "build_id": body["build_id"]}

    target = url or os.environ.get(URL_ENV, "").strip()
    if not target:
        out["status"] = REFUSED_UNCONFIGURED
        out["detail"] = (
            f"{URL_ENV} is not set on this job, so the frozen batch {body['batch_id']} "
            f"({out['campaigns']} campaign(s), {out['bytes']} bytes) has nowhere to go. This is a "
            f"refusal and not a skip: the relay's ingest endpoint is the only way the day reaches "
            f"a human, and a night that quietly hands over nothing looks exactly like a night with "
            f"nothing to hand over.")
        return out

    version = (os.environ.get(SECRET_VERSION_ENV) or "").strip()
    if not _VERSION_NUMBER.match(version):
        out["status"] = REFUSED_UNCONFIGURED
        out["detail"] = (
            f"{SECRET_VERSION_ENV} must be a version NUMBER and is {version!r}. `latest` is "
            f"refused on purpose while {SECRET_ID} is being rotated: latest is the newest ENABLED "
            f"version, which during a rotation is the value the relay has not accepted yet, and "
            f"the 401 that follows is indistinguishable from a broken push. Set the number the "
            f"relay currently accepts.")
        return out
    try:
        secret = gsecret.read(SECRET_ID, env_override="RELAY_SECRET", version=version)
    except gsecret.SecretUnavailable as e:
        out["status"] = REFUSED_UNCONFIGURED
        out["detail"] = (f"the shared secret version {version} is not available, so the push "
                         f"cannot be authenticated: {e}")
        return out

    req = urllib.request.Request(target, data=raw, method="POST")
    req.add_header("content-type", "application/json; charset=utf-8")
    req.add_header("accept", "application/json")
    req.add_header("user-agent", USER_AGENT)
    req.add_header(SECRET_HEADER, secret)
    # The build_id also rides in a header so the relay can decide idempotence before it parses a
    # body it may already hold. The body remains the authority; this is a convenience, not a
    # second source, and the two can never disagree because both are copied from the frozen head.
    req.add_header("X-Relay-Build-Id", str(body["build_id"]))
    # Which VERSION of the shared secret this push was signed with. During a rotation the relay
    # accepts two, and a 401 that names neither is a guessing game for both sides.
    req.add_header("X-Relay-Secret-Version", version)
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT_S) as resp:
            answer = (resp.read() or b"")[:400].decode("utf-8", "replace")
            out["http_status"] = resp.status
            if 200 <= resp.status < 300:
                out["status"] = SENT
                out["detail"] = (f"handed over batch {body['batch_id']} build {body['build_id']} "
                                 f"signed with {SECRET_ID} v{version}: {out['campaigns']} "
                                 f"campaign(s), audience {body['day']['audience_total']}, "
                                 f"{out['bytes']} bytes; relay answered {resp.status} {answer!r}")
            else:
                out["detail"] = (f"relay answered {resp.status}: {answer!r}. The batch was NOT "
                                 f"stored, so no mail can be written from it.")
    except urllib.error.HTTPError as e:
        answer = ""
        try:
            answer = (e.read() or b"")[:400].decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        out["http_status"] = e.code
        out["detail"] = (f"relay answered {e.code} to a push signed with {SECRET_ID} v{version}: "
                         f"{answer!r}")
    except Exception as e:  # noqa: BLE001
        # No answer at all - DNS, TLS, connection refused, timeout. http_status stays None on
        # purpose: "the relay said no" and "the relay was not there" are different things to chase.
        out["detail"] = f"no answer from {target}: {e!r}"
    log.info("PUSH_RESULT status=%s http=%s bytes=%s campaigns=%s secret_version=%s",
             out["status"], out["http_status"], out["bytes"], out["campaigns"], version)
    return out


def press_selftest(url: str = None, body: dict = None, timeout: float = None) -> dict:
    """Call the press endpoint AS the relay would, to prove contract B end to end.

    This is a client standing in for the relay, not a second road to the approval row: it POSTs to
    the same single endpoint, with the same header and the same secret, and it can do nothing the
    relay could not do. Its whole reason to exist is that the secret must never pass through a
    conversation - a job running as this service account can read it from Secret Manager and use
    it, so the full path is provable without the value ever being seen or typed.
    """
    target = url or os.environ.get("PRESS_URL", "").strip()
    out = {"status": FAILED, "http_status": None, "detail": "", "answer": None}
    if not target:
        out["status"] = REFUSED_UNCONFIGURED
        out["detail"] = "PRESS_URL is not set, so there is nothing to call."
        return out
    try:
        secret = gsecret.read("press_endpoint_secret", env_override="PRESS_ENDPOINT_SECRET")
    except gsecret.SecretUnavailable as e:
        out["status"] = REFUSED_UNCONFIGURED
        out["detail"] = str(e)
        return out
    raw = json.dumps(body or {}, ensure_ascii=False, default=str).encode("utf-8")
    req = urllib.request.Request(target, data=raw, method="POST")
    req.add_header("content-type", "application/json; charset=utf-8")
    req.add_header("user-agent", USER_AGENT)
    req.add_header("X-Press-Secret", secret)
    try:
        with urllib.request.urlopen(req, timeout=timeout or 60) as resp:
            out["http_status"] = resp.status
            out["answer"] = (resp.read() or b"")[:2000].decode("utf-8", "replace")
            out["status"] = SENT if 200 <= resp.status < 300 else FAILED
            out["detail"] = f"press endpoint answered {resp.status}"
    except urllib.error.HTTPError as e:
        out["http_status"] = e.code
        try:
            out["answer"] = (e.read() or b"")[:2000].decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        out["detail"] = f"press endpoint answered {e.code}"
    except Exception as e:  # noqa: BLE001
        out["detail"] = f"no answer from {target}: {e!r}"
    return out
