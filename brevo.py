"""Brevo access: contact-state snapshot refresh, and the retired transactional sender.

Why the snapshot lives here and not in a separate job
-----------------------------------------------------
`email_suppression_all` reads Brevo *contact state* (`emailBlacklisted`). Before 2026-09-02
it read only Brevo *events*, so anyone blocklisted manually, by import or by an admin was
invisible: 3 457 blocklisted contacts, of which 2 350 were still mailable by us. The state
has to be refreshed by whoever is about to send, otherwise the suppression list silently
rots and reads as covered - the same failure the fix was for. So the sender refreshes it
at the start of every run and asserts its freshness afterwards.

The transactional path is RETIRED (2026-09-08)
----------------------------------------------
PART C of `_email-A-Z-2026-09.md` replaced per-person transactional sending with campaigns
on 2026-09-07. Until 2026-09-08 that decision was enforced only by DRY_RUN, ALLOW_SEND and
an empty BREVO_API_KEY - configuration, not code. `send_transactional` now refuses at the
call site, unconditionally. See the note on that function.
"""
import csv
import io
import json
import logging
import ssl
import time
import urllib.request

log = logging.getLogger("brevo")
_CTX = ssl.create_default_context()
API = "https://api.brevo.com/v3"


class BrevoError(RuntimeError):
    pass


class RetiredPathError(RuntimeError):
    """A code path retired by decision, which must never execute.

    Deliberately NOT a subclass of BrevoError, and that distinction carries real weight.
    BrevoError means "Brevo refused this message", and a caller is entitled to catch one,
    count it and carry on to the next person. This means "this mechanism no longer exists",
    and catching it per message would convert a single structural refusal into one quiet
    failure row per person - a run that reports 6 153 failures and still exits 0. Callers
    must let it through.
    """


def _request(method, path, key, payload=None, timeout=60):
    url = f"{API}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("api-key", key)
    req.add_header("accept", "application/json")
    if data:
        req.add_header("content-type", "application/json")
    last = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                body = r.read().decode()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            # 4xx other than 429 will not get better by retrying
            if e.code != 429 and 400 <= e.code < 500:
                raise BrevoError(f"{method} {path} -> {e.code} {body}") from e
            last = BrevoError(f"{method} {path} -> {e.code} {body}")
        except Exception as e:  # noqa: BLE001 - network flakiness
            last = e
        time.sleep(2 * (attempt + 1))
    raise BrevoError(f"{method} {path} failed after retries: {last}")


# --------------------------------------------------------------------------- #
# Contact-state snapshot
# --------------------------------------------------------------------------- #
def export_contacts_ndjson(key: str) -> bytes:
    """Full contact export -> NDJSON bytes.

    Uses the export API rather than paging /contacts: one request instead of ~13, and the
    CSV carries `_BLOCKLISTED` and `_listIds`, which is exactly what suppression and the
    coverage join need. `createdSince` is required by the API even for allContacts.
    """
    proc = _request(
        "POST",
        "/contacts/export",
        key,
        {
            "customContactFilter": {
                "actionForContacts": "allContacts",
                "createdSince": "2000-01-01T00:00:00.000Z",
            },
            "exportMandatoryAttributes": True,
            "exportMetadata": ["_listIds"],
            "exportSubscriptionStatus": ["email_marketing"],
            "exportDateInUTC": True,
            "disableNotification": True,
        },
    )
    process_id = proc.get("processId")
    if not process_id:
        raise BrevoError(f"export did not return a processId: {proc}")

    url = None
    for _ in range(60):  # up to ~5 minutes
        info = _request("GET", f"/processes/{process_id}", key)
        if info.get("status") == "completed" and info.get("export_url"):
            url = info["export_url"]
            break
        if info.get("status") in ("failed", "error"):
            raise BrevoError(f"export process {process_id} failed: {info}")
        time.sleep(5)
    if not url:
        raise BrevoError(f"export process {process_id} did not complete in time")

    with urllib.request.urlopen(url, timeout=180, context=_CTX) as r:
        raw = r.read().decode("utf-8", errors="replace")

    out = io.BytesIO()
    n = 0
    for row in csv.DictReader(io.StringIO(raw), delimiter=";"):
        email = (row.get("EMAIL") or "").strip().lower()
        if not email:
            continue
        ids_raw = (row.get("_listIds") or "").strip().strip("[]")
        list_ids = [int(x) for x in ids_raw.split("|") if x.strip().isdigit()] if ids_raw else []
        rec = {
            "email": email,
            "list_ids": list_ids,
            "added_time": _ddmmyyyy(row.get("ADDED_TIME")),
            "modified_time": _ddmmyyyy(row.get("MODIFIED_TIME")),
            "email_subscribed": "email_marketing" in (row.get("_SUBSCRIBED") or ""),
            "email_blocklisted": "email_marketing" in (row.get("_BLOCKLISTED") or ""),
        }
        out.write((json.dumps(rec, ensure_ascii=False) + "\n").encode())
        n += 1
    if n == 0:
        raise BrevoError("contact export produced 0 rows - refusing to overwrite the snapshot")
    log.info("brevo export: %s contacts", n)
    return out.getvalue()


def _ddmmyyyy(value):
    if not value:
        return None
    parts = value.strip().split("-")
    if len(parts) == 3 and len(parts[2]) == 4:
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return None


# --------------------------------------------------------------------------- #
# Transactional send - RETIRED 2026-09-08, kept readable, cannot execute
# --------------------------------------------------------------------------- #
RETIRED_REASON = (
    "send_transactional is RETIRED. PART C of _email-A-Z-2026-09.md (2026-09-07) replaced "
    "per-person transactional sending with campaigns. This function refuses unconditionally "
    "and is not re-enabled by DRY_RUN, ALLOW_SEND or BREVO_API_KEY - the retirement is in the "
    "code because three environment variables are not a guard. If campaigns need a sender, "
    "build it as a campaign; do not revive this."
)


def send_transactional(key, to_email, to_name, template_id, params, tags):
    """RETIRED 2026-09-08. Raises RetiredPathError, always, before touching the network.

    Retired rather than deleted, per the registry rule: the implementation below stays
    readable as history and is unreachable by construction. Do not move the raise down to
    make it conditional - a condition is what this commit removed.

    The original contract, for the record: one call per message on purpose, because the log
    must carry a real per-message timestamp and message id. A batch call that stamped 7 668
    rows with one timestamp is what made the June 2026 history unreadable.
    """
    raise RetiredPathError(f"{RETIRED_REASON} (attempted to={to_email} template={template_id})")

    # --- retired implementation, unreachable; kept as history ---------------- #
    payload = {
        "to": [{"email": to_email, "name": to_name or to_email}],
        "templateId": int(template_id),
        "params": params or {},
        "tags": [t for t in tags if t][:10],
    }
    res = _request("POST", "/smtp/email", key, payload)
    return res.get("messageId")
