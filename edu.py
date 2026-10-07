"""EDUCATIONAL LETTER through the engine: ONE letter, ONE date, ONE audience rule, ONE Brevo campaign. LV only.

MAIN's block of 2026-10-07 13:10 (Raivis' decisions 12:59). This is its own weekly slot: it reads the sales engine's
morning tables and never writes one of them - no plan row, no letter_fields row, no send_log row, no sequence state.

FAIL CLOSED. A send for (send_date, letter_code) happens only when ALL of this is true at that moment:
  the letter row exists, carries the approved content hash and is ARMED            mkt_control.edu_letter
  a GO record of that date exists, written by the check, not older than 60 minutes  mkt_control.edu_gate
  NO STOP record exists for that date and letter (a STOP wins over any GO, for good) mkt_control.edu_gate
  nothing was started for it before (one send per letter and date, no retry)         mkt_control.edu_log
  the frozen audience of that GO is there, with the count the GO recorded            mkt_control.edu_audience
  the source campaign in Brevo still hashes to the approved content, has no ⟦ and has an unsubscribe link
No GO (check failed, hung, never ran) = nothing is sent and the log says so.

TWO IDENTITIES, because no identity holds both rights (as with the daily sample):
  ops account   (reads every table; no Brevo key)   prepare | check | stop | status      - the ops shell
  campaign SA   (the Brevo key; mkt_control only)    verify | send                        - Cloud Run job tiktik-edu-send
The check starts the job in verify mode to compare the approved hash with what Brevo holds NOW.

WHAT A SEND DOES: makes a Brevo list holding exactly the frozen audience, creates the engine's OWN campaign from the
source campaign's subject, preview text and HTML (the source draft is never changed and never sent), reads it back,
re-reads STOP, calls sendNow once, and writes one mkt_control.edu_sent row per person.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

P = "jaunais-za-aizv04022026"
M = f"{P}.mkt_control"
T_LETTER, T_SEGMENT, T_AUD, T_GATE = f"{M}.edu_letter", f"{M}.edu_segment", f"{M}.edu_audience", f"{M}.edu_gate"
T_BREVO, T_SENT, T_LOG = f"{M}.edu_brevo", f"{M}.edu_sent", f"{M}.edu_log"
SEND_JOB, REGION = "tiktik-edu-send", "europe-west1"
SALES_JOB = "tiktik-marketing-sender"
TEST_RECIPIENT = "raivis@alenda.lv"            # the only address a test_only letter can reach - a constant
RULE = "lv_glove_buyers"
SUPPRESSION_LIST, EN_LIST, EE_LIST = 4, 46, 75  # Brevo lists: tiktik_suppression, EN_foreign_LANG_en, EE_klienti_tel372
EXCLUDE_LISTS = (SUPPRESSION_LIST, EN_LIST, EE_LIST)
TEST_EXCLUDE_LISTS = (EN_LIST, EE_LIST)         # the test address itself sits in list 4 (measured 2026-10-07)
PASS_REASONS = (None, "PERSONAL_LETTER_THIS_WEEK")   # own weekly slot: a personal sales letter does not exclude
SEGMENT_MAX_AGE_H, GO_MAX_AGE_MIN, ADD_CHUNK, ADD_FAIL_PCT, DEFAULT_TOLERANCE_PCT = 26, 60, 150, 2.0, 10
DASH = "Company-Alenda-SIA/shared-platforms/_agent-sessions.md"
AGENT = "tiktik.lv › Marketing · Sūtīšanas dzinējs (izglītojošā vēstule)"

DDL = f"""
CREATE TABLE IF NOT EXISTS `{T_LETTER}` (send_date DATE, letter_code STRING, source_campaign_id INT64,
  approved_sha256 STRING, approved_by STRING, approved_at TIMESTAMP, audience_rule STRING, expected_audience INT64,
  tolerance_pct INT64, test_only BOOL, armed BOOL, armed_by STRING, armed_at TIMESTAMP, note STRING);
CREATE TABLE IF NOT EXISTS `{T_SEGMENT}` (built_at TIMESTAMP, email STRING, language STRING, info_track STRING);
CREATE TABLE IF NOT EXISTS `{T_AUD}` (send_date DATE, letter_code STRING, check_run STRING, email STRING,
  master_key STRING, built_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS `{T_GATE}` (send_date DATE, letter_code STRING, kind STRING, check_run STRING,
  reason STRING, detail STRING, written_at TIMESTAMP, written_by STRING);
CREATE TABLE IF NOT EXISTS `{T_BREVO}` (send_date DATE, letter_code STRING, check_run STRING,
  source_campaign_id INT64, sha256 STRING, status STRING, subject STRING, modified_at STRING, placeholders INT64,
  unsubscribe_links INT64, html_bytes INT64, error STRING, checked_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS `{T_SENT}` (send_date DATE, letter_code STRING, campaign_id INT64,
  source_campaign_id INT64, brevo_list_id INT64, master_key STRING, email STRING, sent_at TIMESTAMP,
  check_run STRING, test_only BOOL);
CREATE TABLE IF NOT EXISTS `{T_LOG}` (send_date DATE, letter_code STRING, at TIMESTAMP, who STRING, event STRING,
  detail STRING);
"""


# ------------------------------------------------------------------------------------------------ pure parts
def content_sha(subject, preview_text, html) -> str:
    """THE approval hash: sha256, lowercase hex, of subject + LF + previewText + LF + htmlContent, each exactly as
    GET /emailCampaigns/{id} returns it (None = empty). No normalisation."""
    return hashlib.sha256("\n".join([subject or "", preview_text or "", html or ""]).encode("utf-8")).hexdigest()


def riga(now: dt.datetime) -> dt.datetime:
    import zoneinfo
    return now.astimezone(zoneinfo.ZoneInfo("Europe/Riga"))


def tolerance_ok(n: int, expected, pct) -> bool:
    if not expected or expected <= 0:
        return False
    return abs(n - expected) * 100 <= (pct if pct is not None else DEFAULT_TOLERANCE_PCT) * expected


def exclusion_lists(test_only: bool) -> list:
    return list(TEST_EXCLUDE_LISTS if test_only else EXCLUDE_LISTS)


def check_reasons(s: dict) -> list:
    """The 09:30 decision, pure. s = the facts the check gathered. -> [] = GO, else every reason for NO-GO."""
    r, L = [], s.get("letter")
    if not L:
        return ["NO_LETTER_ROW"]
    if not L.get("approved_sha256"):
        r.append("NO_APPROVAL_HASH")
    if not L.get("armed"):
        r.append("NOT_ARMED")
    if s.get("stops"):
        r.append("STOPPED")
    if not s.get("plan_run"):
        r.append("NO_PLAN_RUN_TODAY")
    elif s.get("plan_selfcheck_failed") not in (0, "0"):
        r.append(f"PLAN_SELFCHECK_FAILED={s.get('plan_selfcheck_failed')}")
    if s.get("writer_status") != "OK":
        r.append(f"WRITER_NOT_OK={s.get('writer_status')}")
    if not s.get("sendtime_rows"):
        r.append("NO_0855_SELFCHECK_ROWS")
    if s.get("hard_failed"):
        r.append("HARD_SELFCHECK_FAILED=" + ",".join(sorted(s["hard_failed"]))[:200])
    if not s.get("gate_rows"):
        r.append("NO_PERSON_GATE_ROWS_TODAY")
    if s.get("sales_switch_open"):
        r.append("SALES_SWITCH_OPEN=" + str(s["sales_switch_open"])[:120])
    if s.get("tracks_enabled") not in (0, "0"):
        r.append(f"TRACK_ENABLED={s.get('tracks_enabled')}")
    if s.get("segment_age_h") is None or s["segment_age_h"] > SEGMENT_MAX_AGE_H or not s.get("segment_rows"):
        r.append(f"SEGMENT_MISSING_OR_OLD age_h={s.get('segment_age_h')}")
    n = s.get("audience")
    if not n:
        r.append("AUDIENCE_EMPTY")
    elif L.get("test_only"):
        if n != 1 or s.get("audience_emails") != [TEST_RECIPIENT]:
            r.append("TEST_AUDIENCE_IS_NOT_ONLY_THE_TEST_RECIPIENT")
    elif not tolerance_ok(int(n), L.get("expected_audience"), L.get("tolerance_pct")):
        r.append(f"AUDIENCE_OUT_OF_TOLERANCE n={n} expected={L.get('expected_audience')} "
                 f"pct={L.get('tolerance_pct') or DEFAULT_TOLERANCE_PCT}")
    if s.get("audience_bad"):
        r.append(f"AUDIENCE_HOLDS_SUPPRESSED_OR_DUPLICATE={s['audience_bad']}")
    b = s.get("brevo")
    if not b:
        r.append("BREVO_NOT_VERIFIED (the verify job gave no answer)")
    else:
        if b.get("error"):
            r.append("BREVO_READ_ERROR=" + str(b["error"])[:160])
        if b.get("sha256") != (L.get("approved_sha256") or "").strip().lower():
            r.append(f"CONTENT_HASH_MISMATCH brevo={str(b.get('sha256'))[:12]} approved={str(L.get('approved_sha256'))[:12]}")
        if b.get("status") != "draft":
            r.append(f"SOURCE_CAMPAIGN_STATUS={b.get('status')}")
        if int(b.get("placeholders") or 0):
            r.append(f"PLACEHOLDERS_IN_LETTER={b.get('placeholders')}")
        if not int(b.get("unsubscribe_links") or 0):
            r.append("NO_UNSUBSCRIBE_LINK")
    return r


def send_refusals(s: dict) -> list:
    """The send decision, pure, evaluated by the job right before it touches Brevo. [] = may send."""
    r, L = [], s.get("letter")
    if not L:
        return ["NO_LETTER_ROW"]
    if s.get("today") != s.get("send_date"):
        r.append(f"NOT_THE_SEND_DATE today={s.get('today')}")
    if not L.get("armed"):
        r.append("NOT_ARMED")
    if not L.get("approved_sha256"):
        r.append("NO_APPROVAL_HASH")
    if s.get("stops"):
        r.append("STOPPED")
    g = s.get("go")
    if not g:
        r.append("NO_GO_RECORD")
    else:
        if s.get("last_gate_kind") != "GO":
            r.append(f"LATEST_CHECK_IS_{s.get('last_gate_kind')}")
        if g.get("age_min") is None or g["age_min"] > GO_MAX_AGE_MIN or g["age_min"] < 0:
            r.append(f"GO_TOO_OLD age_min={g.get('age_min')}")
        if int(s.get("audience") or 0) != int(g.get("audience") or -1) or not s.get("audience"):
            r.append(f"FROZEN_AUDIENCE_DIFFERS now={s.get('audience')} go={g.get('audience')}")
    if s.get("started"):
        r.append("ALREADY_STARTED (one send per letter and date)")
    if L.get("test_only") and s.get("audience_emails") not in (None, [TEST_RECIPIENT]):
        r.append("TEST_AUDIENCE_IS_NOT_ONLY_THE_TEST_RECIPIENT")
    return r


def content_refusals(letter, camp, placeholders, unsub) -> list:
    r = []
    sha = content_sha(camp.get("subject"), camp.get("previewText"), camp.get("htmlContent"))
    if sha != (letter.get("approved_sha256") or "").strip().lower():
        r.append(f"CONTENT_HASH_MISMATCH brevo={sha[:12]} approved={str(letter.get('approved_sha256'))[:12]}")
    if placeholders:
        r.append(f"PLACEHOLDERS_IN_LETTER={placeholders}")
    if not unsub:
        r.append("NO_UNSUBSCRIBE_LINK")
    return r


def campaign_payload(source, name, list_id, excl) -> dict:
    """The engine's own campaign: the source's subject, preview text and HTML, byte for byte; nothing else of it."""
    p = {"name": name, "subject": source.get("subject"), "sender": {"id": (source.get("sender") or {}).get("id") or 2},
         "replyTo": source.get("replyTo") or "info@tiktik.lv", "htmlContent": source.get("htmlContent"),
         "recipients": {"listIds": [list_id]}, "inlineImageActivation": False}
    if source.get("previewText"):
        p["previewText"] = source["previewText"]
    if excl:
        p["recipients"]["exclusionListIds"] = list(excl)
    return p


def chunks(xs, n):
    return [xs[i:i + n] for i in range(0, len(xs), n)]


# ------------------------------------------------------------------------------------------------- BigQuery
_TOK = {}


def _token() -> str:
    if _TOK.get("exp", 0) < time.time() + 60:
        try:
            req = urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/instance/"
                                         "service-accounts/default/token", headers={"Metadata-Flavor": "Google"})
            j = json.loads(urllib.request.urlopen(req, timeout=5).read())
            _TOK.update(t=j["access_token"], exp=time.time() + int(j.get("expires_in", 300)))
        except Exception:  # noqa: BLE001 - not on a metadata server: the gcloud identity of the shell
            _TOK.update(t=subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip(),
                        exp=time.time() + 600)
    return _TOK["t"]


def _param(name, v):
    if name == "d":                                    # the send date, always: a DATE, never text
        t, val = {"type": "DATE"}, {"value": dt.date.fromisoformat(str(v)).isoformat()}
    elif isinstance(v, bool):
        t, val = {"type": "BOOL"}, {"value": "true" if v else "false"}
    elif isinstance(v, int):
        t, val = {"type": "INT64"}, {"value": str(v)}
    elif isinstance(v, dt.date):
        t, val = {"type": "DATE"}, {"value": v.isoformat()}
    elif isinstance(v, (list, tuple)):
        t, val = {"type": "ARRAY", "arrayType": {"type": "STRING"}}, {"arrayValues": [{"value": str(x)} for x in v]}
    else:
        t, val = {"type": "STRING"}, {"value": None if v is None else str(v)}
    return {"name": name, "parameterType": t, "parameterValue": val}


def query(sql, **params):
    """BigQuery REST jobs.query with NAMED parameters; rows as dicts, scalars as text. Works for both identities."""
    base = f"https://bigquery.googleapis.com/bigquery/v2/projects/{P}/queries"

    def call(url, body=None):
        req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + _token(), "Content-Type": "application/json"})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=120).read())
        except urllib.error.HTTPError as e:
            raise RuntimeError("BigQuery refused: " + e.read().decode()[:600]) from None
    body = {"query": sql, "useLegacySql": False, "timeoutMs": 60000, "maxResults": 20000}
    if params:
        body.update(parameterMode="NAMED", queryParameters=[_param(k, v) for k, v in params.items()])
    r = call(base, body)
    ref = r["jobReference"]
    more = f"{base}/{ref['jobId']}?" + urllib.parse.urlencode({"location": ref.get("location", ""), "timeoutMs": 60000,
                                                              "maxResults": 20000})
    deadline = time.time() + 300
    while not r.get("jobComplete"):
        if time.time() > deadline:
            raise RuntimeError("BigQuery job did not finish in 300 s")
        r = call(more)
    fields, out = (r.get("schema") or {}).get("fields") or [], []
    while True:
        out += [{f["name"]: c["v"] for f, c in zip(fields, row["f"])} for row in r.get("rows") or []]
        if not r.get("pageToken"):
            return out
        r = call(more + "&" + urllib.parse.urlencode({"pageToken": r["pageToken"]}))


def _b(v) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


def _i(v):
    return None if v in (None, "") else int(v)


def log(q, d, code, who, event, detail=None):
    txt = detail if detail is None or isinstance(detail, str) else json.dumps(detail, ensure_ascii=False, default=str)
    print(f"EDU_LOG {event} " + (txt or ""))
    q(f"INSERT INTO `{T_LOG}` (send_date, letter_code, at, who, event, detail) "
      f"VALUES (@d, @c, CURRENT_TIMESTAMP(), @w, @e, @t)", d=d, c=code, w=who, e=event, t=txt)


def letter_row(q, d, code):
    rows = q(f"SELECT * FROM `{T_LETTER}` WHERE send_date = @d AND letter_code = @c", d=d, c=code)
    if len(rows) != 1:
        return None
    L = rows[0]
    for k in ("source_campaign_id", "expected_audience", "tolerance_pct"):
        L[k] = _i(L.get(k))
    for k in ("test_only", "armed"):
        L[k] = _b(L.get(k))
    return L


def stops(q, d, code) -> int:
    return int(q(f"SELECT COUNT(*) AS n FROM `{T_GATE}` WHERE send_date = @d AND letter_code = @c AND kind = 'STOP'",
                 d=d, c=code)[0]["n"])


# ---------------------------------------------------------------------------------------------- ops identity
def setup(q):
    for stmt in [s for s in DDL.split(";") if s.strip()]:
        q(stmt)
    print("EDU_SETUP ok")
    return 0


def prepare(q):
    """The slow part, done ahead of the check: who is an LV glove buyer (own category gloves), from the nightly
    payload view - the same source the hand lists were cut from (info_track via business_marts.info_track_map)."""
    q(f"""CREATE OR REPLACE TABLE `{T_SEGMENT}` AS
SELECT CURRENT_TIMESTAMP() AS built_at, LOWER(TRIM(p.email)) AS email, p.LANGUAGE AS language,
       COALESCE(m.track, 'cits') AS info_track
FROM `{P}.business_marts.marketing_brevo_payload` p
LEFT JOIN `{P}.business_marts.info_track_map` m ON m.category = p.FAVORITE_CATEGORY
WHERE p.email IS NOT NULL""")
    r = q(f"SELECT COUNT(*) AS n, COUNTIF(info_track = 'cimdi') AS gloves, "
          f"COUNTIF(info_track = 'cimdi' AND language = 'lv') AS lv_gloves, COUNT(DISTINCT email) AS emails "
          f"FROM `{T_SEGMENT}`")[0]
    print("EDU_PREPARE " + json.dumps(r))
    return 0 if int(r["lv_gloves"]) > 0 and r["n"] == r["emails"] else 1


AUDIENCE_SQL = f"""
WITH g AS (SELECT DISTINCT email FROM `{T_SEGMENT}` WHERE info_track = 'cimdi' AND language = 'lv'),
a AS (SELECT LOWER(TRIM(email)) AS email, master_key, excluded_reason FROM `{M}.shadow_akcija_audience`
      WHERE plan_date = @d AND run_id = @run),
s AS (SELECT DISTINCT LOWER(TRIM(email)) AS email FROM `{P}.business_marts.email_suppression_all`)
SELECT g.email, a.master_key,
       CASE WHEN a.email IS NULL THEN 'NOT_IN_ENGINE_POPULATION'
            WHEN s.email IS NOT NULL THEN 'SUPPRESSED'
            WHEN a.excluded_reason IS NULL OR a.excluded_reason = 'PERSONAL_LETTER_THIS_WEEK' THEN 'IN'
            ELSE a.excluded_reason END AS gate
FROM g LEFT JOIN a USING (email) LEFT JOIN s USING (email)"""


def build_audience(q, d, code, check_run, plan_run, test_only):
    """Freeze the audience of this check. -> (n, funnel). The real rule is always measured; a test_only letter
    then keeps ONE row, the test recipient - whatever the rule says about him."""
    funnel = {r["gate"]: int(r["n"]) for r in q(
        f"SELECT gate, COUNT(*) AS n FROM ({AUDIENCE_SQL}) GROUP BY 1", d=d, run=plan_run)}
    if test_only:
        q(f"INSERT INTO `{T_AUD}` (send_date, letter_code, check_run, email, master_key, built_at) "
          f"VALUES (@d, @c, @cr, @e, 'test', CURRENT_TIMESTAMP())", d=d, c=code, cr=check_run, e=TEST_RECIPIENT)
    else:
        q(f"INSERT INTO `{T_AUD}` (send_date, letter_code, check_run, email, master_key, built_at) "
          f"SELECT @d, @c, @cr, email, master_key, CURRENT_TIMESTAMP() FROM ({AUDIENCE_SQL}) WHERE gate = 'IN'",
          d=d, c=code, cr=check_run, run=plan_run)
    n = int(q(f"SELECT COUNT(*) AS n FROM `{T_AUD}` WHERE send_date = @d AND letter_code = @c AND check_run = @cr",
              d=d, c=code, cr=check_run)[0]["n"])
    return n, funnel


def sales_switches() -> dict:
    """The SALES sender's switches, read from the live job. They must be CLOSED: this path does not use them and an
    open one is a state nobody ordered. -> {} when closed as expected, else what is open / unreadable."""
    try:
        j = json.loads(subprocess.check_output(["gcloud", "run", "jobs", "describe", SALES_JOB, "--region", REGION,
                                                "--project", P, "--format=json"], timeout=60))
        env = {e["name"]: e.get("value") for e in
               j["spec"]["template"]["spec"]["template"]["spec"]["containers"][0].get("env", [])}
    except Exception as e:  # noqa: BLE001 - unreadable = not known to be closed
        return {"unreadable": f"{type(e).__name__}: {e}"[:120]}
    bad = {}
    if env.get("DRY_RUN") != "true":
        bad["DRY_RUN"] = env.get("DRY_RUN")
    if env.get("GLOBAL_SEND_ENABLED") != "false":
        bad["GLOBAL_SEND_ENABLED"] = env.get("GLOBAL_SEND_ENABLED")
    if env.get("ALLOW_SEND") in ("true", "True", "1"):
        bad["ALLOW_SEND"] = env.get("ALLOW_SEND")
    if env.get("SEND_UNLOCKED_BY"):
        bad["SEND_UNLOCKED_BY"] = env.get("SEND_UNLOCKED_BY")
    return bad


def run_job(mode, d, code, check_run="") -> str:
    out = subprocess.run(["gcloud", "run", "jobs", "execute", SEND_JOB, "--region", REGION, "--project", P, "--async",
                          "--update-env-vars", f"EDU_MODE={mode},EDU_DATE={d},EDU_LETTER={code},EDU_CHECK_RUN={check_run}",
                          "--format=value(metadata.name)"], capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise RuntimeError("job execute failed: " + (out.stderr or out.stdout)[-300:])
    return out.stdout.strip().splitlines()[-1]


def dash_row(status, what):
    try:
        import zoneinfo
        now = dt.datetime.now(zoneinfo.ZoneInfo("Europe/Riga")).strftime("%Y-%m-%d %H:%M")
        row = f"| {now} | {AGENT} | {status} | {what.replace('|', '/')} | nav zināma |\n"
        if not os.path.exists("/root/iso_drive.py"):
            subprocess.run(["gsutil", "-q", "cp", f"gs://{P}-iso-tools/iso_drive.py", "/root/iso_drive.py"],
                           check=True, timeout=60)
        r = subprocess.run([sys.executable, "/root/iso_drive.py", "append", DASH], input=row.encode(),
                           capture_output=True, timeout=90)
        ok = "VERIFIED ok=True" in (r.stdout.decode() + r.stderr.decode())
        print("EDU_DASH " + ("written " if ok else "NOT WRITTEN ") + row.strip())
        return ok
    except Exception as e:  # noqa: BLE001 - the dash row never decides anything
        print(f"EDU_DASH NOT WRITTEN ({type(e).__name__}: {e})")
        return False


def check(q, d, code, wait_s=110, dash=True) -> int:
    """THE CHECK. Writes exactly one GO or NO-GO record for (date, letter) and prints one EDU_CHECK line.
    Exit 0 = GO, 3 = NO-GO, 4 = the check itself broke (also recorded as NO-GO when a record can still be written)."""
    check_run = "chk-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    s = {"send_date": d, "letter_code": code, "check_run": check_run}
    try:
        L = s["letter"] = letter_row(q, d, code)
        s["stops"] = stops(q, d, code)
        execution = None
        if L and L.get("source_campaign_id"):
            execution = run_job("verify", d, code, check_run)               # runs while the tables are read
        pr = q(f"SELECT run_id, selfcheck_failed FROM `{M}.shadow_run_report` WHERE plan_date = @d "
               f"ORDER BY finished_at DESC LIMIT 1", d=d)
        s["plan_run"] = pr[0]["run_id"] if pr else None
        s["plan_selfcheck_failed"] = _i(pr[0]["selfcheck_failed"]) if pr else None
        w = q(f"SELECT status FROM `{M}.letter_fields_log` WHERE plan_date = @d ORDER BY finished_at DESC LIMIT 1", d=d)
        s["writer_status"] = w[0]["status"] if w else None
        sc = q(f"SELECT check_name, level, ok FROM `{M}.shadow_selfcheck` WHERE plan_date = @d", d=d)
        s["sendtime_rows"] = sum(r["check_name"].startswith("sendtime_") for r in sc)
        s["hard_failed"] = sorted({r["check_name"] for r in sc if r["level"] == "hard" and not _b(r["ok"])})
        s["gate_rows"] = int(q(f"SELECT COUNT(*) AS n FROM `{M}.shadow_akcija_audience` WHERE plan_date = @d "
                               f"AND run_id = @r", d=d, r=s["plan_run"] or "")[0]["n"])
        s["sales_switch_open"] = sales_switches()
        s["tracks_enabled"] = int(q(f"SELECT COUNTIF(enabled) AS n FROM `{M}.track_enabled`")[0]["n"])
        seg = q(f"SELECT COUNT(*) AS n, TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(built_at), MINUTE) AS age_min "
                f"FROM `{T_SEGMENT}`")[0]
        s["segment_rows"], s["segment_age_h"] = int(seg["n"]), (None if seg["age_min"] is None
                                                                else round(int(seg["age_min"]) / 60, 1))
        if L and s["plan_run"] and s["segment_rows"]:
            s["audience"], s["funnel"] = build_audience(q, d, code, check_run, s["plan_run"], L["test_only"])
            bad = q(f"SELECT COUNT(*) - COUNT(DISTINCT a.email) AS dup, COUNTIF(s.email IS NOT NULL) AS supp "
                    f"FROM `{T_AUD}` a LEFT JOIN (SELECT DISTINCT LOWER(TRIM(email)) AS email "
                    f"FROM `{P}.business_marts.email_suppression_all`) s USING (email) "
                    f"WHERE a.send_date = @d AND a.letter_code = @c AND a.check_run = @cr", d=d, c=code, cr=check_run)[0]
            dup, supp = int(bad["dup"]), int(bad["supp"])
            s["audience_bad"] = None if not dup and (not supp or L["test_only"]) else {"duplicates": dup, "suppressed": supp}
            if L["test_only"]:
                s["audience_emails"] = [r["email"] for r in q(
                    f"SELECT email FROM `{T_AUD}` WHERE send_date = @d AND letter_code = @c AND check_run = @cr",
                    d=d, c=code, cr=check_run)]
        deadline = time.time() + wait_s
        while execution and time.time() < deadline and not s.get("brevo"):
            b = q(f"SELECT * FROM `{T_BREVO}` WHERE check_run = @cr", cr=check_run)
            if b:
                s["brevo"] = b[0]
            else:
                time.sleep(6)
        s["verify_execution"] = execution
        reasons = check_reasons(s)
        broke = False
    except Exception as e:  # noqa: BLE001 - a check that cannot finish is a NO-GO
        reasons, broke = [f"CHECK_BROKE {type(e).__name__}: {e}"[:300]], True
    kind = "NO-GO" if reasons else "GO"
    detail = {k: v for k, v in s.items() if k not in ("letter",)}
    detail["approved_sha256"] = ((s.get("letter") or {}).get("approved_sha256") or "")[:12]
    q(f"INSERT INTO `{T_GATE}` (send_date, letter_code, kind, check_run, reason, detail, written_at, written_by) "
      f"VALUES (@d, @c, @k, @cr, @r, @t, CURRENT_TIMESTAMP(), 'edu.check')", d=d, c=code, k=kind, cr=check_run,
      r="; ".join(reasons) or None, t=json.dumps(detail, ensure_ascii=False, default=str))
    back = q(f"SELECT kind FROM `{T_GATE}` WHERE check_run = @cr", cr=check_run)
    print("EDU_CHECK " + json.dumps({"date": d, "letter": code, "result": kind, "check_run": check_run,
                                     "audience": s.get("audience"), "funnel": s.get("funnel"), "reasons": reasons,
                                     "record_read_back": [r["kind"] for r in back]}, ensure_ascii=False))
    if dash:
        if kind == "GO":
            dash_row("STRĀDĀ", f"Izglītojošā vēstule {code} {d}: pārbaude OK (GO), auditorija {s.get('audience')}; "
                               f"dzinējs sūtīs")
        else:
            dash_row("KĻŪDA", f"Izglītojošā vēstule {code} {d}: NO-GO — {'; '.join(reasons)[:260]} — dzinējs NESŪTA; "
                              f"jāsūta rezerves kampaņa")
    return 4 if broke else (0 if kind == "GO" else 3)


def stop(q, d, code, by, why) -> int:
    """THE STOP. One record; from that moment no send of (date, letter) is possible, whatever GO exists.
    Exit 0 only when the record was read back."""
    q(f"INSERT INTO `{T_GATE}` (send_date, letter_code, kind, check_run, reason, detail, written_at, written_by) "
      f"VALUES (@d, @c, 'STOP', NULL, @r, NULL, CURRENT_TIMESTAMP(), @b)", d=d, c=code, r=why, b=by)
    n = stops(q, d, code)
    started = q(f"SELECT event, CAST(at AS STRING) AS at FROM `{T_LOG}` WHERE send_date = @d AND letter_code = @c "
                f"AND event IN ('SEND_STARTED', 'SENT') ORDER BY at", d=d, c=code)
    print("EDU_STOP " + json.dumps({"date": d, "letter": code, "stop_records_read_back": n, "engine_send_possible": n == 0,
                                    "send_already_started": started}, ensure_ascii=False))
    return 0 if n >= 1 else 1


def status(q, d, code) -> int:
    out = {"letter": letter_row(q, d, code),
           "gate": q(f"SELECT kind, check_run, reason, CAST(written_at AS STRING) AS at, written_by FROM `{T_GATE}` "
                     f"WHERE send_date = @d AND letter_code = @c ORDER BY written_at", d=d, c=code),
           "sent_rows": int(q(f"SELECT COUNT(*) AS n FROM `{T_SENT}` WHERE send_date = @d AND letter_code = @c",
                              d=d, c=code)[0]["n"]),
           "log": q(f"SELECT CAST(at AS STRING) AS at, who, event, detail FROM `{T_LOG}` WHERE send_date = @d "
                    f"AND letter_code = @c ORDER BY at", d=d, c=code)}
    print("EDU_STATUS " + json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return 0


# ------------------------------------------------------------------------------------------- campaign identity
def _brevo(method, path, payload=None):
    import campaign as C
    try:
        return C._call(method, path, payload)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Brevo {method} {path} -> {e.code}: {e.read().decode()[:300]}") from None


def _facts(camp) -> dict:
    import campaign as C
    html = camp.get("htmlContent") or ""
    return {"sha256": content_sha(camp.get("subject"), camp.get("previewText"), html),
            "placeholders": len(C.placeholder_hits(camp.get("subject"), camp.get("previewText"), html)),
            "unsubscribe_links": C.unsubscribe_links(html), "html_bytes": len(html.encode("utf-8"))}


def verify(q, d, code, check_run) -> int:
    """GET only: what Brevo holds NOW for the source campaign -> one mkt_control.edu_brevo row for this check."""
    L = letter_row(q, d, code)
    row = {"sid": (L or {}).get("source_campaign_id"), "sha": None, "st": None, "subj": None, "mod": None, "ph": None,
           "un": None, "hb": None, "err": None}
    try:
        camp = _brevo("GET", f"/emailCampaigns/{int(row['sid'])}")
        f = _facts(camp)
        row.update(sha=f["sha256"], st=camp.get("status"), subj=camp.get("subject"), mod=camp.get("modifiedAt"),
                   ph=f["placeholders"], un=f["unsubscribe_links"], hb=f["html_bytes"])
    except Exception as e:  # noqa: BLE001 - an unread letter is not an approved one
        row["err"] = f"{type(e).__name__}: {e}"[:300]
    q(f"INSERT INTO `{T_BREVO}` (send_date, letter_code, check_run, source_campaign_id, sha256, status, subject, "
      f"modified_at, placeholders, unsubscribe_links, html_bytes, error, checked_at) "
      f"VALUES (@d, @c, @cr, CAST(@sid AS INT64), @sha, @st, @subj, @mod, CAST(@ph AS INT64), CAST(@un AS INT64), "
      f"CAST(@hb AS INT64), @err, CURRENT_TIMESTAMP())", d=d, c=code, cr=check_run,
      **{k: (None if v is None else str(v)) for k, v in row.items()})
    print("EDU_VERIFY " + json.dumps({"date": d, "letter": code, "check_run": check_run, **row}, ensure_ascii=False))
    return 0 if not row["err"] else 1


def send(q, d, code, now=None) -> int:
    """THE SEND. Exit 0 = sent, 3 = refused (nothing was sent), 5 = failed after sendNow was called."""
    now = now or dt.datetime.now(dt.timezone.utc)
    who = "edu.send"
    L = letter_row(q, d, code)
    gates = q(f"SELECT kind, check_run, detail, TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), written_at, MINUTE) AS age_min "
              f"FROM `{T_GATE}` WHERE send_date = @d AND letter_code = @c AND kind IN ('GO', 'NO-GO') "
              f"ORDER BY written_at DESC LIMIT 1", d=d, c=code)
    s = {"letter": L, "send_date": d, "today": riga(now).date().isoformat(), "stops": stops(q, d, code),
         "last_gate_kind": gates[0]["kind"] if gates else None, "go": None,
         "started": int(q(f"SELECT COUNT(*) AS n FROM `{T_LOG}` WHERE send_date = @d AND letter_code = @c "
                          f"AND event = 'SEND_STARTED'", d=d, c=code)[0]["n"])}
    aud = []
    if gates and gates[0]["kind"] == "GO":
        g = gates[0]
        s["go"] = {"check_run": g["check_run"], "age_min": _i(g["age_min"]),
                   "audience": (json.loads(g["detail"] or "{}")).get("audience")}
        aud = q(f"SELECT email, master_key FROM `{T_AUD}` WHERE send_date = @d AND letter_code = @c AND check_run = @cr "
                f"ORDER BY email", d=d, c=code, cr=g["check_run"])
        s["audience"] = len(aud)
        if L and L["test_only"]:
            s["audience_emails"] = [a["email"] for a in aud]
    refusals = send_refusals(s)
    if refusals:
        log(q, d, code, who, "SEND_REFUSED", {"reasons": refusals, "nothing_sent": True})
        print("EDU_SEND " + json.dumps({"date": d, "letter": code, "sent": False, "reasons": refusals}))
        return 3
    check_run, test_only = s["go"]["check_run"], L["test_only"]
    source = _brevo("GET", f"/emailCampaigns/{L['source_campaign_id']}")
    f = _facts(source)
    refusals = content_refusals(L, source, f["placeholders"], f["unsubscribe_links"])
    if refusals:
        log(q, d, code, who, "SEND_REFUSED", {"reasons": refusals, "nothing_sent": True})
        print("EDU_SEND " + json.dumps({"date": d, "letter": code, "sent": False, "reasons": refusals}))
        return 3
    # one send per letter and date: the marker is written BEFORE Brevo is touched and never removed
    log(q, d, code, who, "SEND_STARTED", {"check_run": check_run, "audience": len(aud), "test_only": test_only})
    if int(q(f"SELECT COUNT(*) AS n FROM `{T_LOG}` WHERE send_date = @d AND letter_code = @c AND event = 'SEND_STARTED'",
             d=d, c=code)[0]["n"]) != 1:
        log(q, d, code, who, "SEND_REFUSED", {"reasons": ["TWO_STARTS_AT_ONCE"], "nothing_sent": True})
        return 3
    campaign_id = list_id = None
    try:
        tag = f"{code} {d}" + (" TESTS" if test_only else "")
        list_id = int(_brevo("POST", "/contacts/lists", {"name": f"ENGINE EDU {tag} {check_run[-6:]}", "folderId": 1})["id"])
        ok, failed = [], []
        for part in chunks([a["email"] for a in aud], ADD_CHUNK):
            try:
                r = _brevo("POST", f"/contacts/lists/{list_id}/contacts/add", {"emails": part}).get("contacts") or {}
                ok += [e.lower() for e in r.get("success") or []]
                failed += [e.lower() if isinstance(e, str) else str(e) for e in r.get("failure") or []]
            except RuntimeError as e:
                failed += part
                log(q, d, code, who, "LIST_ADD_ERROR", str(e)[:300])
        fail_pct = 100.0 * (len(aud) - len(ok)) / len(aud)
        log(q, d, code, who, "LIST_FILLED", {"list_id": list_id, "asked": len(aud), "added": len(ok),
                                             "not_added": len(aud) - len(ok), "examples": failed[:10]})
        if not ok or fail_pct > ADD_FAIL_PCT or (test_only and ok != [TEST_RECIPIENT]):
            raise RuntimeError(f"LIST_NOT_FILLED added={len(ok)} of {len(aud)} ({fail_pct:.1f}% missing, limit {ADD_FAIL_PCT}%)")
        excl = exclusion_lists(test_only)
        created = _brevo("POST", "/emailCampaigns", campaign_payload(source, f"ENGINE · EDU · {tag}", list_id, excl))
        campaign_id = int(created["id"])
        mine = _brevo("GET", f"/emailCampaigns/{campaign_id}")
        rec = mine.get("recipients") or {}
        fm = _facts(mine)
        problems = content_refusals(L, mine, fm["placeholders"], fm["unsubscribe_links"])
        if sorted(rec.get("lists") or []) != [list_id] or sorted(rec.get("exclusionLists") or []) != sorted(excl):
            problems.append(f"RECIPIENTS_READ_BACK_DIFFER lists={rec.get('lists')} excl={rec.get('exclusionLists')}")
        if mine.get("status") != "draft":
            problems.append(f"ENGINE_CAMPAIGN_STATUS={mine.get('status')}")
        if stops(q, d, code):                                              # the last look before the one call
            problems.append("STOPPED")
        if problems:
            raise RuntimeError("; ".join(problems))
    except Exception as e:  # noqa: BLE001 - before sendNow: nothing went out; the engine draft is removed
        removed = None
        if campaign_id:
            try:
                _brevo("DELETE", f"/emailCampaigns/{campaign_id}")
                removed = True
            except Exception:  # noqa: BLE001
                removed = False
        log(q, d, code, who, "SEND_REFUSED", {"reasons": [str(e)[:400]], "nothing_sent": True, "list_id": list_id,
                                              "engine_campaign_id": campaign_id, "engine_draft_removed": removed})
        print("EDU_SEND " + json.dumps({"date": d, "letter": code, "sent": False, "reasons": [str(e)[:400]]}))
        return 3
    _brevo("POST", f"/emailCampaigns/{campaign_id}/sendNow")              # THE ONE SEND CALL
    try:
        log(q, d, code, who, "SENT", {"campaign_id": campaign_id, "list_id": list_id, "people": len(ok),
                                      "exclusion_lists": excl, "source_campaign_id": L["source_campaign_id"]})
        q(f"INSERT INTO `{T_SENT}` (send_date, letter_code, campaign_id, source_campaign_id, brevo_list_id, master_key, "
          f"email, sent_at, check_run, test_only) SELECT @d, @c, @cid, @sid, @lid, master_key, email, "
          f"CURRENT_TIMESTAMP(), @cr, @t FROM `{T_AUD}` WHERE send_date = @d AND letter_code = @c AND check_run = @cr "
          f"AND email IN UNNEST(@ok)", d=d, c=code, cid=campaign_id, sid=L["source_campaign_id"], lid=list_id,
          cr=check_run, t=test_only, ok=ok)
        n = int(q(f"SELECT COUNT(*) AS n FROM `{T_SENT}` WHERE send_date = @d AND letter_code = @c", d=d, c=code)[0]["n"])
        after = _brevo("GET", f"/emailCampaigns/{campaign_id}")
        log(q, d, code, who, "RECORDED", {"edu_sent_rows": n, "brevo_status": after.get("status")})
        print("EDU_SEND " + json.dumps({"date": d, "letter": code, "sent": True, "campaign_id": campaign_id,
                                        "list_id": list_id, "people": len(ok), "edu_sent_rows": n,
                                        "brevo_status": after.get("status")}))
        return 0
    except Exception as e:  # noqa: BLE001 - the letter IS out; only the record is incomplete
        print("EDU_SEND " + json.dumps({"date": d, "letter": code, "sent": True, "campaign_id": campaign_id,
                                        "RECORD_INCOMPLETE": f"{type(e).__name__}: {e}"[:300]}))
        return 5


def main(argv) -> int:
    mode = (argv[0] if argv else os.environ.get("EDU_MODE") or "").strip()
    args = dict(a.lstrip("-").split("=", 1) for a in argv[1:] if "=" in a)
    d = (args.get("date") or os.environ.get("EDU_DATE") or "").strip()
    code = (args.get("letter") or os.environ.get("EDU_LETTER") or "").strip()
    if mode in ("setup", "prepare"):
        return {"setup": setup, "prepare": prepare}[mode](query)
    if mode not in ("check", "stop", "status", "verify", "send") or not code:
        print("usage: edu.py setup | prepare | check|stop|status --date=YYYY-MM-DD --letter=CODE [--by=.. --why=..] ; "
              "job: EDU_MODE=verify|send EDU_DATE EDU_LETTER [EDU_CHECK_RUN]")
        return 2
    dt.date.fromisoformat(d)                                              # a bad date never reaches a query
    if mode == "check":
        return check(query, d, code, dash=args.get("dash", "1") != "0")
    if mode == "stop":
        if not args.get("by") or not args.get("why"):
            print("stop needs --by= and --why="); return 2
        return stop(query, d, code, args["by"], args["why"])
    if mode == "status":
        return status(query, d, code)
    if mode == "verify":
        return verify(query, d, code, os.environ.get("EDU_CHECK_RUN") or args.get("check_run") or "manual")
    return send(query, d, code)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
