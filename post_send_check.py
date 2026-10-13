"""R9 (MAIN 3 13.10, Sūtīšanas dzinējs 9): the automatic AFTER-SEND check of a send day (D46: exactly one letter per
contact per Tuesday, nobody empty).

Runs as job tiktik-post-send-check, started by Cloud Scheduler every 15 min on Tuesday 10:00-14:45 Riga. Each start:
  1. reads the day's SENT Brevo campaigns (sentDate on the day, Riga); TEST campaigns (name starts with TEST or
     "[TESTS") and the two test addresses are left out;
  2. WAIT (exit, nothing written) while a campaign of the day is still queued / in process, or the last one was sent
     less than DELAY_MIN (60) minutes ago, or a check row for the day already exists. The last start of the window
     (FINAL_AFTER, 14:30 Riga) runs whatever the state, so a day never ends without a row;
  3. recipients of every campaign come from BREVO (POST /emailCampaigns/{id}/exportRecipients -> CSV), not from
     send_log (send_log is cross-checked and its difference reported);
  4. CHECK 1 doubles: addresses that are recipients of 2+ campaigns of the day -> must be 0;
     CHECK 2 nobody empty: eligible LV = list 3 members, not blacklisted, not in exclusion lists 4 / 46 / 75 / 96
     (the 236 basis) and created before the first campaign of the day; every one of them must be a recipient of at
     least one campaign -> the ones who are not are counted and listed;
  5. writes ONE row into mkt_control.post_send_check + one detail row per double / empty address into
     mkt_control.post_send_check_detail, and sends ONE WhatsApp line to Raivis (+37126711822 from +37122811277,
     TimelinesAI). Nothing is ever sent to a client.

The Brevo key (tiktik-campaign-sa) and the TimelinesAI token (ops-cloudshell-runner) belong to two service accounts,
so the WhatsApp line is sent by a second start of the same code, `--notify=1`, under the account that holds the
token: it sends the line of every check row of the day whose whatsapp_uid and whatsapp_error are still empty and
stamps them (job tiktik-post-send-notify, same schedule +5 min). When one account holds both, the check sends it
itself (WA_INLINE=1).

`python post_send_check.py --date=2026-10-13 [--force=1] [--no-whatsapp=1]` runs it by hand (force = no WAIT).
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid
import zoneinfo

P = "jaunais-za-aizv04022026"
M = f"{P}.mkt_control"
T_CHECK, T_DETAIL, T_LOG = f"{M}.post_send_check", f"{M}.post_send_check_detail", f"{M}.send_log"
RIGA = zoneinfo.ZoneInfo("Europe/Riga")
DELAY_MIN = 60
FINAL_AFTER = dt.time(14, 30)
LIST_ALL, EXCLUSIONS = 3, (4, 46, 75, 96)
TEST_ADDRESSES = frozenset({"alenda.jurmala@gmail.com", "raivis@alenda.lv"})
WA_TO, WA_FROM = "+37126711822", "+37122811277"
DETAIL_SAMPLE = 5

DDL = [
    f"""CREATE TABLE IF NOT EXISTS `{T_CHECK}` (
  check_date DATE, run_id STRING, checked_at TIMESTAMP, ok BOOL, campaigns_json STRING, recipients_total INT64,
  recipients_distinct INT64, doubles INT64, eligible_lv INT64, empty_lv INT64, send_log_distinct INT64,
  brevo_not_in_send_log INT64, send_log_not_in_brevo INT64, whatsapp_text STRING, whatsapp_uid STRING,
  whatsapp_error STRING, note STRING)""",
    f"""CREATE TABLE IF NOT EXISTS `{T_DETAIL}` (
  check_date DATE, run_id STRING, kind STRING, email STRING, campaigns STRING, created_at TIMESTAMP)""",
]


def _s(v):
    return "NULL" if v is None else "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _ts(v):
    if not v:
        return None
    return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def is_test(c) -> bool:
    n = (c.get("name") or "").strip().upper()
    return n.startswith("TEST") or n.startswith("[TEST")


def day_bounds(d: dt.date):
    a = dt.datetime.combine(d, dt.time(0, 0), RIGA).astimezone(dt.timezone.utc)
    return a, a + dt.timedelta(days=1)


def day_campaigns(brevo, d: dt.date, now=None) -> list:
    """Every campaign of the day that is sent or still going (queued / inProcess), TEST ones left out."""
    a, b = day_bounds(d)
    end = min(b, (now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(seconds=5))   # Brevo: endDate <= now
    out = []
    for status in ("sent", "queued", "inProcess"):          # Brevo API spelling (in_process -> 400, measured 13.10)
        off = 0
        while True:
            q = (f"/emailCampaigns?status={status}&limit=100&offset={off}&excludeHtmlContent=true"
                 + (f"&startDate={a:%Y-%m-%dT%H:%M:%S.000Z}&endDate={end:%Y-%m-%dT%H:%M:%S.000Z}" if status == "sent" else ""))
            page = (brevo("GET", q, None) or {}).get("campaigns") or []
            for c in page:
                when = _ts(c.get("sentDate") or c.get("scheduledAt"))
                if when is None or not a <= when < b:
                    continue
                if not is_test(c):
                    out.append({"id": int(c["id"]), "name": c.get("name"), "status": c.get("status") or status,
                                "sent_at": c.get("sentDate"), "lists": (c.get("recipients") or {}).get("lists") or [],
                                "exclusion_lists": (c.get("recipients") or {}).get("exclusionLists") or []})
            if len(page) < 100:
                break
            off += 100
    return sorted(out, key=lambda c: (c["sent_at"] or "", c["id"]))


def decide(campaigns, now, existing_row, force=False) -> str | None:
    """None = run now; else the WAIT reason."""
    if force:
        return None
    if existing_row:
        return "ALREADY_CHECKED"
    final = now.astimezone(RIGA).time() >= FINAL_AFTER
    if final:
        return None
    if not campaigns:
        return "NO_CAMPAIGN_YET"
    if any(c["status"] != "sent" for c in campaigns):
        return "CAMPAIGN_STILL_GOING " + ",".join(str(c["id"]) for c in campaigns if c["status"] != "sent")
    last = max(_ts(c["sent_at"]) for c in campaigns)
    if now - last < dt.timedelta(minutes=DELAY_MIN):
        return f"LAST_SENT_LESS_THAN_{DELAY_MIN}_MIN ({last:%H:%M}Z)"
    return None


def export_recipients(brevo, fetch, cid, sleep=time.sleep, wait_s=600) -> set:
    """Brevo's own recipient list of a sent campaign (exportRecipients -> process -> CSV)."""
    pid = (brevo("POST", f"/emailCampaigns/{int(cid)}/exportRecipients", {"recipientsType": "all"}) or {})["processId"]
    t0 = time.time()
    while True:
        p = brevo("GET", f"/processes/{int(pid)}", None) or {}
        if p.get("status") == "completed" and p.get("export_url"):
            break
        if time.time() - t0 > wait_s:
            raise RuntimeError(f"export of campaign {cid} not ready after {wait_s} s (process {pid} {p.get('status')})")
        sleep(5)
    text = fetch(p["export_url"])
    rd = csv.reader(io.StringIO(text), delimiter=";" if text.split("\n", 1)[0].count(";") else ",")
    head = [h.strip().strip('"').lower() for h in next(rd, [])]
    col = next((i for i, h in enumerate(head) if h in ("email", "email_id", "e-mail", "email address")), 0)
    return {r[col].strip().lower() for r in rd if len(r) > col and "@" in r[col]}


def list_members(brevo, lid, with_meta=False):
    out, off = {}, 0
    while True:
        r = brevo("GET", f"/contacts/lists/{int(lid)}/contacts?limit=500&offset={off}", None) or {}
        page = r.get("contacts") or []
        for c in page:
            out[(c.get("email") or "").strip().lower()] = {"bl": bool(c.get("emailBlacklisted")),
                                                           "created": c.get("createdAt")}
        if len(page) < 500:
            return out
        off += 500


def eligible_lv(brevo, before) -> set:
    l3 = list_members(brevo, LIST_ALL)
    ex = set()
    for lid in EXCLUSIONS:
        ex |= set(list_members(brevo, lid))
    return {e for e, m in l3.items() if e and not m["bl"] and e not in ex
            and (before is None or not m["created"] or _ts(m["created"]) < before)} - TEST_ADDRESSES


def evaluate(campaigns, recipients: dict, eligible: set, send_log: set) -> dict:
    """recipients {campaign_id: set(emails)} -> the two checks."""
    per = {}
    for cid, em in recipients.items():
        for e in em - TEST_ADDRESSES:
            per.setdefault(e, []).append(cid)
    doubles = {e: sorted(c) for e, c in per.items() if len(c) > 1}
    got = set(per)
    empty = sorted(eligible - got)
    return {"recipients_total": sum(len(v - TEST_ADDRESSES) for v in recipients.values()),
            "recipients_distinct": len(got), "doubles": doubles, "eligible_lv": len(eligible), "empty": empty,
            "send_log_distinct": len(send_log), "brevo_not_in_send_log": len(got - send_log),
            "send_log_not_in_brevo": len(send_log - got),
            "ok": not doubles and not empty and bool(campaigns)}


def whatsapp_line(d, campaigns, ev) -> str:
    ok = "✅" if ev["ok"] else "⚠️"
    camps = " · ".join(f"{c['id']}={len(ev['_rcpt'].get(c['id'], ()))}" for c in campaigns) if campaigns else "nav kampaņu"
    line = (f"{ok} Pēc-sūtīšanas pārbaude {d:%d.%m}: kampaņas {camps}; saņēma {ev['recipients_distinct']}; "
            f"2+ vēstules: {len(ev['doubles'])}; bez vēstules: {len(ev['empty'])} no {ev['eligible_lv']} LV")
    if ev["empty"]:
        line += " (piem. " + ", ".join(ev["empty"][:3]) + ")"
    if ev["doubles"]:
        line += " (2+: " + ", ".join(list(ev["doubles"])[:3]) + ")"
    return line


def run(q, brevo, fetch, wa_send, d: dt.date, now=None, force=False, whatsapp=True, sleep=time.sleep,
        no_notify=False) -> dict:
    """no_notify: a hand run whose line must NOT go out later either (whatsapp_error = NOT_SENT ...)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    run_id = f"psc-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    for ddl in DDL:
        q(ddl)
    existing = q(f"SELECT run_id FROM `{T_CHECK}` WHERE check_date = DATE '{d}' LIMIT 1")
    campaigns = day_campaigns(brevo, d, now)
    wait = decide(campaigns, now, existing, force)
    if wait:
        return {"run_id": run_id, "date": str(d), "wait": wait, "campaigns": [c["id"] for c in campaigns]}
    rcpt = {c["id"]: export_recipients(brevo, fetch, c["id"], sleep=sleep) for c in campaigns if c["status"] == "sent"}
    first = min((_ts(c["sent_at"]) for c in campaigns if c["sent_at"]), default=None)
    elig = eligible_lv(brevo, first)
    a, b = day_bounds(d)
    slog = {r["e"] for r in q(f"SELECT DISTINCT LOWER(TRIM(email)) AS e FROM `{T_LOG}` WHERE sent_at >= TIMESTAMP "
                              f"'{a.isoformat()}' AND sent_at < TIMESTAMP '{b.isoformat()}' AND source != 'proof' "
                              f"AND NOT STARTS_WITH(master_key, 'TEST:')")} - TEST_ADDRESSES
    ev = evaluate(campaigns, rcpt, elig, slog)
    ev["_rcpt"] = rcpt
    text = whatsapp_line(d, campaigns, ev)
    uid = err = None
    if whatsapp:
        try:
            uid = wa_send(text)
        except Exception as e:  # noqa: BLE001 - the row is written either way
            err = f"{type(e).__name__}: {str(e)[:300]}"
    elif no_notify:
        err = "NOT_SENT (hand run with --no-whatsapp=1)"
    cj = json.dumps([{**c, "recipients": len(rcpt.get(c["id"], ()))} for c in campaigns], default=str)
    q(f"INSERT INTO `{T_CHECK}` VALUES (DATE '{d}', {_s(run_id)}, CURRENT_TIMESTAMP(), {str(ev['ok']).upper()}, "
      f"{_s(cj)}, {ev['recipients_total']}, {ev['recipients_distinct']}, {len(ev['doubles'])}, {ev['eligible_lv']}, "
      f"{len(ev['empty'])}, {ev['send_log_distinct']}, {ev['brevo_not_in_send_log']}, {ev['send_log_not_in_brevo']}, "
      f"{_s(text)}, {_s(uid)}, {_s(err)}, {_s('forced' if force else None)})")
    det = [("DOUBLE", e, ",".join(map(str, c))) for e, c in ev["doubles"].items()] + [("EMPTY", e, None) for e in ev["empty"]]
    for i in range(0, len(det), 500):
        q(f"INSERT INTO `{T_DETAIL}` VALUES " + ",".join(
            f"(DATE '{d}', {_s(run_id)}, {_s(k)}, {_s(e)}, {_s(c)}, CURRENT_TIMESTAMP())" for k, e, c in det[i:i + 500]))
    return {"run_id": run_id, "date": str(d), "ok": ev["ok"], "campaigns": json.loads(cj),
            "recipients_total": ev["recipients_total"], "recipients_distinct": ev["recipients_distinct"],
            "doubles": len(ev["doubles"]), "doubles_sample": dict(list(ev["doubles"].items())[:DETAIL_SAMPLE]),
            "eligible_lv": ev["eligible_lv"], "empty": len(ev["empty"]), "empty_sample": ev["empty"][:DETAIL_SAMPLE],
            "send_log_distinct": ev["send_log_distinct"], "brevo_not_in_send_log": ev["brevo_not_in_send_log"],
            "send_log_not_in_brevo": ev["send_log_not_in_brevo"], "whatsapp": text, "whatsapp_uid": uid,
            "whatsapp_error": err}


def notify(q, wa_send, d: dt.date) -> dict:
    """Send the WhatsApp line of every check row of the day not sent yet; stamp uid / error on the row."""
    for ddl in DDL:
        q(ddl)
    rows = q(f"SELECT run_id, whatsapp_text FROM `{T_CHECK}` WHERE check_date = DATE '{d}' AND whatsapp_uid IS NULL "
             f"AND whatsapp_error IS NULL AND whatsapp_text IS NOT NULL ORDER BY checked_at")
    out = []
    for r in rows:
        uid = err = None
        try:
            uid = wa_send(r["whatsapp_text"])
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {str(e)[:300]}"
        q(f"UPDATE `{T_CHECK}` SET whatsapp_uid = {_s(uid)}, whatsapp_error = {_s(err)} WHERE run_id = {_s(r['run_id'])}")
        out.append({"run_id": r["run_id"], "uid": uid, "error": err})
    return {"date": str(d), "sent": out}


# ------------------------------------------------------------------------------------------------ production wiring
def _wa_send(text):
    import gsecret
    tok = gsecret.read("TIMELINESAI_API_TOKEN")
    req = urllib.request.Request("https://app.timelines.ai/integrations/api/messages", method="POST",
                                 data=json.dumps({"phone": WA_TO, "whatsapp_account_phone": WA_FROM,
                                                  "text": text[:1990]}).encode(),
                                 headers={"Authorization": f"Bearer {tok}", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return (json.loads(r.read()).get("data") or {}).get("message_uid")


def _fetch(url):
    """Brevo's export file. A bare urllib request is answered 403 (measured 13.10) - browser UA first, then with the
    api-key header. A zipped export is unpacked."""
    import urllib.error
    import zipfile
    import campaign as CAMP
    last = None
    for hdr in ({"User-Agent": "Mozilla/5.0 (tiktik post_send_check)"},
                {"User-Agent": "Mozilla/5.0 (tiktik post_send_check)", "api-key": CAMP.api_key()}):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=120) as r:
                raw = r.read()
            if raw[:2] == b"PK":
                z = zipfile.ZipFile(io.BytesIO(raw))
                raw = z.read(z.namelist()[0])
            return raw.decode("utf-8-sig", "replace")
        except urllib.error.HTTPError as e:
            last = e
            print(f"EXPORT_FETCH {e.code} host={urllib.parse.urlsplit(url).netloc} headers={sorted(hdr)}", flush=True)
    raise last


def main(argv) -> int:
    from google.cloud import bigquery
    args = dict(a.lstrip("-").split("=", 1) for a in argv if "=" in a)
    d = dt.date.fromisoformat(args.get("date") or dt.datetime.now(RIGA).date().isoformat())
    c = bigquery.Client(project=P)
    q = lambda sql: [dict(r) for r in c.query(sql).result()]  # noqa: E731
    if args.get("notify") == "1":
        print("POST_SEND_NOTIFY " + json.dumps(notify(q, _wa_send, d), ensure_ascii=False, default=str))
        return 0
    import campaign as CAMP

    def brevo(method, path, payload):
        if method != "GET" and not (method == "POST" and path.endswith("/exportRecipients")):
            raise RuntimeError(f"post_send_check is read-only in Brevo ({method} {path} refused)")
        return CAMP._call(method, path, payload)
    inline = os.environ.get("WA_INLINE") == "1" and args.get("no-whatsapp") != "1"
    res = run(q, brevo, _fetch, _wa_send, d, force=args.get("force") == "1", whatsapp=inline,
              no_notify=args.get("no-whatsapp") == "1")
    print("POST_SEND_CHECK " + json.dumps(res, ensure_ascii=False, default=str)[:20000])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
