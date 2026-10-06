"""Send-time lookups on the real tables (Sūtīšanas dzinējs 5, MAIN order 2026-10-06; contract 1634b7f07054).

What send_path.dispatch() asks at SEND time, answered from BigQuery, read-only:
  plan_run(send_date)        the latest plan run of the date (mkt_control.shadow_send_plan)                     L10
  letter_fields(send_date)   the latest writer run of the date (mkt_control.letter_fields_log)                  L10
  presend_ctx(...)           one presend.Ctx per audience member, built by presend.build_ctx - the SAME builder
                             the 08:05 planner uses - from the data of this moment                              L8
  person_blocks(...)         B2B_FLOW / LEAD_FLOW / EN_PENDING per member                                       L9

MAIN decision 2026-10-06: the gate that counts is the one evaluated at SEND time. The 08:05 plan cannot know what
the 08:40 writer will write. evaluate() runs L10 + L8 + L9 over today's due letters after the writer; checks() turns
the answer into mkt_control.shadow_selfcheck rows (check names sendtime_*), which the 08:55 dash job reads.

Nothing here reaches a customer, Brevo or Pipedrive. The only write is --record: the sendtime_* rows of today.
query(sql) -> rows as dicts. Two transports: a bigquery.Client (Cloud Run) or the bq CLI (ops shell). The CLI
returns every scalar as text, so values are normalised here.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import types
import urllib.error
import urllib.parse
import urllib.request

import presend as G
import selfcheck as SC
import send_path as SP
import sequence as S

P = "jaunais-za-aizv04022026"
T_PLAN = f"{P}.mkt_control.shadow_send_plan"
T_LF = f"{P}.mkt_control.letter_fields"
T_LFLOG = f"{P}.mkt_control.letter_fields_log"
# Temp-then-swap: SHADOW_TABLE_SUFFIX moves the two tables this module WRITES (never the ones it reads to decide).
SFX = os.environ.get("SHADOW_TABLE_SUFFIX", "")
T_CHECK = f"{P}.mkt_control.shadow_selfcheck{SFX}"
T_AKCIJA = f"{P}.mkt_control.shadow_akcija_audience{SFX}"
PERSONAL = "PERSONAL_LETTER_THIS_WEEK"
# Daily shadow sample (CF5, MAIN 2026-10-06): the picks. Mailed by job tiktik-shadow-sample (shadow_sample.py).
T_SAMPLE = f"{P}.mkt_control.shadow_sample{SFX}"
SAMPLE_JOB = "tiktik-shadow-sample"
# Types that are NOT sampled although planned / known, with the reason MAIN gave (2026-10-06). Remove a line only on
# MAIN's word; nothing here puts or activates a template.
SAMPLE_SKIP = {
    S.PP1: "Brevo template 244 still holds text v4; the approved text is v5 and is not put (MAIN 2026-10-06)",
    "akcija_weekly": "template 236 is not approved; the weekly akcija is not an engine letter (MAIN 2026-10-06)",
}
SAMPLE_SKIP_TEMPLATE = {"akcija_weekly": 236}


def _job():
    """sequence_job, for its SQL texts: the planner's sources ARE the send-time sources (one text, two readers).
    In the ops shell google-cloud-bigquery is absent; only the texts are needed there, so the import is stubbed."""
    try:
        import sequence_job as J
    except ImportError:
        for name in ("google", "google.cloud", "google.cloud.bigquery"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["google"].cloud = sys.modules["google.cloud"]
        sys.modules["google.cloud"].bigquery = sys.modules["google.cloud.bigquery"]
        import sequence_job as J
    return J


def _b(v) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


def _i(v):
    return None if v in (None, "") else int(v)


def _date(v):
    if v in (None, ""):
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v)[:10])


def _ts(v):
    if v in (None, ""):
        return None
    if not isinstance(v, dt.datetime):
        try:                                  # the REST API returns a TIMESTAMP as epoch seconds
            return dt.datetime.fromtimestamp(float(v), dt.timezone.utc)
        except (TypeError, ValueError):
            pass
        s = str(v).strip().replace(" UTC", "").replace("Z", "").replace("T", " ")
        try:
            v = dt.datetime.fromisoformat(s)
        except ValueError:
            v = dt.datetime.fromisoformat(s[:19])
    return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)


def _day(v) -> str:
    """ISO date for SQL text; anything that is not a date raises before it can reach a query."""
    return _date(v).isoformat()


class Warehouse:
    """The four send_path lookups on real tables. One instance = one moment: every table is read once."""

    def __init__(self, query):
        self._q, self._c = query, {}

    def _rows(self, key, sql):
        if key not in self._c:
            self._c[key] = [dict(r) for r in self._q(sql)]
        return self._c[key]

    # ---- L10
    def plan_run(self, send_date):
        d = _day(send_date)
        r = self._rows(("run", d), f"SELECT run_id FROM `{T_PLAN}` WHERE plan_date = DATE '{d}' "
                                   f"GROUP BY run_id ORDER BY MAX(planned_at) DESC LIMIT 1")
        return r[0]["run_id"] if r else None

    def letter_fields(self, send_date):
        d = _day(send_date)
        r = self._rows(("lflog", d), f"SELECT run_id, status, JSON_VALUE(counts, '$.plan_run_id') AS plan_run_id "
                                     f"FROM `{T_LFLOG}` WHERE plan_date = DATE '{d}' ORDER BY run_ts DESC LIMIT 1")
        return r[0] if r else None

    # ---- the plan and the writer's rows of the date
    def plan_rows(self, send_date) -> dict:
        d, run = _day(send_date), self.plan_run(send_date)
        if run is None:
            return {}
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", run):
            raise ValueError(f"unexpected plan run id {run!r}")
        rows = self._rows(("plan", d), f"""
SELECT master_key, LOWER(TRIM(email)) AS email, email_type, template_id, offer_rung,
  CAST(planned_send_date AS STRING) AS planned_send_date, CAST(offer_valid_until AS STRING) AS offer_valid_until,
  CAST(xsell_valid_until AS STRING) AS xsell_valid_until, trigger_order_nr, would_send, hold_reason, lost_capped,
  run_id
FROM `{T_PLAN}` WHERE plan_date = DATE '{d}' AND run_id = '{run}'""")
        return {r["master_key"]: r for r in rows}

    def lf_rows(self, send_date) -> dict:
        d = _day(send_date)
        if ("lf_by", d) not in self._c:
            rows = self._rows(("lf", d), f"""
SELECT LOWER(TRIM(email)) AS email, email_type, letter, rung, g15_zero_priced, R1_REF_PRICE, OFFER_VALID_UNTIL,
  XSELL_VALID_UNTIL, ANKETA_URL, ORDER_NR, plan_run_id, run_id
FROM `{T_LF}` WHERE plan_date = DATE '{d}'
QUALIFY ROW_NUMBER() OVER (PARTITION BY email ORDER BY built_at DESC) = 1""")
            for r in rows:
                r["rung"], r["g15_zero_priced"] = _i(r.get("rung")), _b(r.get("g15_zero_priced"))
            self._c[("lf_by", d)] = {r["email"]: r for r in rows}
        return self._c[("lf_by", d)]

    def _shared(self, now):
        if "shared" in self._c:
            return self._c["shared"]
        J = _job()
        rows = lambda sql: [dict(r) for r in self._q(sql)]  # noqa: E731
        one = lambda sql, k: next((r.get(k) for r in rows(sql)), None)  # noqa: E731

        def stale(t):
            return t is None or (now - t).total_seconds() / 3600 > J.PRICE_MAX_AGE_H
        sh = {
            "rung_stale": stale(_ts(one(J.RUNG_BUILT_SQL, "built_at"))),
            "lqxs_stale": stale(_ts(one(J.LQXS_BUILT_SQL, "built_at"))),
            "prices": {r["master_key"]: J.rung_price_map(r) for r in rows(J.RUNG_PRICE_SQL)},
            "lqxs": {r["master_key"]: r for r in rows(J.LQXS_SQL)},
            "r": {r["email"]: (tuple(r.get("r") or ()), tuple(r.get("r_cab") or ())) for r in rows(J.R_SQL)},
            "offered": {r["master_key"]: frozenset(r.get("handles") or ()) for r in rows(J.OFFERED_SQL)},
            "approved": {(_i(r["template_id"]), r["email_type"]) for r in rows(J.APPROVAL_SQL)},
            "flows": {r["master_key"]: r["flow"] for r in rows(J.FLOW_SQL)},
            "en": {r["master_key"] for r in rows(J.EN_SQL)},
        }
        if not sh["flows"] or not sh["en"]:      # as the planner: no guard source = no answer, never "nobody blocked"
            raise RuntimeError("B2B / LEAD or G-EN guard source is empty - refusing to answer the person re-check")
        self._c["shared"] = sh
        return sh

    # ---- L8
    def presend_ctx(self, campaign, send_date, master_keys, now=None) -> dict:
        d = _date(send_date)
        sh, plan, lf = self._shared(now or dt.datetime.now(dt.timezone.utc)), self.plan_rows(d), self.lf_rows(d)
        et, out = campaign.get("email_type"), {}
        for mk in master_keys:
            p = plan.get(mk)
            if not p or p["email_type"] != et or not _b(p["would_send"]) or _date(p["planned_send_date"]) != d:
                continue          # not this letter today: no Ctx, and presend_lock refuses an empty Ctx
            out[mk] = self._ctx(p, d, sh, lf)
        return out

    @staticmethod
    def _ctx(p, d, sh, lf):
        et, rung, mk = p["email_type"], _i(p["offer_rung"]) or 0, p["master_key"]
        ovu, xvu = _date(p["offer_valid_until"]), _date(p["xsell_valid_until"])
        lx = sh["lqxs"].get(mk)
        pr = dict(sh["prices"].get(mk) or {})
        if lx is not None and not sh["lqxs_stale"]:                    # PS1, as the planner
            if lx.get("vu_lost") is not None:
                pr[S.LOST_OFFER_RUNG] = _date(lx["vu_lost"])
            if lx.get("vu_lost_capped") is not None:
                pr["4c"] = _date(lx["vu_lost_capped"])
        due = _date(p.get("planned_send_date")) or d
        has = S.has_rung_price(types.SimpleNamespace(rung_price_valid_until=pr or None), rung, due, ovu, d,
                               capped=_b(p.get("lost_capped"))) if rung else None
        xs_holds = not (et == S.XSELL and lx is not None and xvu is not None and due <= d
                        and (lx.get("vu_xs") is None or _date(lx["vu_xs"]) < xvu))       # K12 / XS2
        row = lf.get(p["email"])
        tid = _i(p["template_id"])
        r, r_cab = sh["r"].get(p["email"], ((), ()))
        return G.build_ctx(
            email_type=et, template_id=tid, template_approved=(tid, et) in sh["approved"], offer_valid_until=ovu,
            xsell_valid_until=xvu, has_price=has,
            price_stale=sh["lqxs_stale"] if et in (S.LOST, S.XSELL) else sh["rung_stale"],
            row=row if row and row["email_type"] == et else None, trigger_order_nr=p.get("trigger_order_nr"),
            r_handles=r, r_cabinet=r_cab, xsell_offered=sh["offered"].get(mk, frozenset()), xs_price_holds=xs_holds)

    # ---- L4 (TC4)
    def template_content(self, now=None) -> list:
        """Per approved, non-provisional template: the approved hash, Brevo's hash from the mirror and its age."""
        J, now = _job(), now or dt.datetime.now(dt.timezone.utc)
        rows = self._rows("tc", f"""
SELECT a.template_id, a.email_type, a.approved, LOWER(TRIM(a.approved_sha256)) AS approved_sha256, a.approved_commit,
  b.html_sha256, b.error, b.checked_at
FROM `{J.T_APPROVAL}` a LEFT JOIN `{J.T_BREVO_HASH}` b ON b.template_id = a.template_id ORDER BY 1, 2""")
        out = []
        for r in rows:
            tid, t = _i(r["template_id"]), _ts(r.get("checked_at"))
            age = None if t is None else round((now - t).total_seconds() / 3600, 2)
            out.append({"template_id": tid, "email_type": r["email_type"], "approved_sha256": r.get("approved_sha256"),
                        "approved_commit": r.get("approved_commit"), "brevo_sha256": r.get("html_sha256"),
                        "mirror_age_h": age, "mirror_error": r.get("error"),
                        "provisional": tid in S.PROVISIONAL_TEMPLATE_IDS,
                        "content_approved": bool(_b(r.get("approved")) and r.get("approved_sha256")
                                                 and r.get("html_sha256") and r["approved_sha256"] == r["html_sha256"]
                                                 and age is not None and age <= J.TEMPLATE_HASH_MAX_AGE_H)})
        return out

    def template_approved(self, template_id, live_hash=None) -> bool:
        """L4. True only when EVERY approval row of this template id carries one and the same approved_sha256 and it
        equals Brevo's hash - read live (live_hash(template_id) -> hex) where the Brevo key is, else from the mirror."""
        rows = [r for r in self.template_content() if r["template_id"] == _i(template_id)]
        want = {r["approved_sha256"] for r in rows}
        if not rows or len(want) != 1 or None in want or "" in want:
            return False
        if live_hash is not None:
            return live_hash(_i(template_id)) == want.pop()
        return all(r["content_approved"] for r in rows)

    # ---- L9
    def person_blocks(self, send_date, master_keys, now=None) -> dict:
        sh, out = self._shared(now or dt.datetime.now(dt.timezone.utc)), {}
        for mk in master_keys:
            flow = sh["flows"].get(mk)
            if flow in S.FLOW_HOLD:
                out[mk] = S.FLOW_HOLD[flow]
            elif mk in sh["en"]:
                out[mk] = S.HOLD_EN
        return out


def warehouse():
    """Production transport (Cloud Run): a bigquery.Client. Read-only queries."""
    from google.cloud import bigquery
    c = bigquery.Client(project=P)
    return Warehouse(lambda sql: c.query(sql).result())


def cli_query(sql):
    """Ops-shell transport: the bq CLI."""
    out = subprocess.run(["bq", "--project_id", P, "query", "--quiet", "--nouse_legacy_sql", "--format=json",
                          "--max_rows=1000000"], input=sql, capture_output=True, text=True, timeout=300)  # SQL on stdin: no argv limit
    if out.returncode:
        raise RuntimeError("bq failed: " + (out.stderr or out.stdout)[:600])
    s = out.stdout.strip()
    return json.loads(s[s.index("["):]) if "[" in s else []


_TOK = {}


def _token() -> str:
    if _TOK.get("exp", 0) < time.time() + 60:
        try:
            req = urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/"
                                         "default/token", headers={"Metadata-Flavor": "Google"})
            j = json.loads(urllib.request.urlopen(req, timeout=5).read())
            _TOK.update(t=j["access_token"], exp=time.time() + int(j.get("expires_in", 300)))
        except Exception:  # noqa: BLE001 - not on GCE metadata: the gcloud identity of the shell
            _TOK.update(t=subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip(),
                        exp=time.time() + 600)
    return _TOK["t"]


def _cell(field, v):
    if v is None:
        return None
    if field.get("mode") == "REPEATED":
        return [_cell({**field, "mode": "NULLABLE"}, x["v"]) for x in v]
    if field["type"] in ("RECORD", "STRUCT"):
        return {f["name"]: _cell(f, c["v"]) for f, c in zip(field["fields"], v["f"])}
    return v


def rest_query(sql):
    """Ops-shell transport without the CLI start-up cost (5 s per bq call): BigQuery REST jobs.query. Scalars come
    back as text, like the CLI. Read or DML; the caller decides what it sends."""
    base = f"https://bigquery.googleapis.com/bigquery/v2/projects/{P}/queries"

    def call(url, body=None):
        req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + _token(),
                                              "Content-Type": "application/json"})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=120).read())
        except urllib.error.HTTPError as e:
            raise RuntimeError("BigQuery refused: " + e.read().decode()[:600]) from None
    r = call(base, {"query": sql, "useLegacySql": False, "timeoutMs": 60000, "maxResults": 20000})
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
        out += [{f["name"]: _cell(f, c["v"]) for f, c in zip(fields, row["f"])} for row in r.get("rows") or []]
        if not r.get("pageToken"):
            return out
        r = call(more + "&" + urllib.parse.urlencode({"pageToken": r["pageToken"]}))


def evaluate(wh, send_date, now) -> dict:
    """L10 + L8 + L9 on the real tables for every letter of the latest plan that is due on send_date.
    The locks are send_path's own functions; one 'campaign' = one (email type, rung, template)."""
    d = _date(send_date)
    l10 = SP.window_lock(send_date=d.isoformat(), now=now, letter_fields=wh.letter_fields, plan_run=wh.plan_run)
    ws = [p for p in wh.plan_rows(d).values() if _b(p["would_send"])]
    due = [p for p in ws if _date(p["planned_send_date"]) == d]
    later = [p for p in ws if _date(p["planned_send_date"]) != d]
    camps = {}
    for p in due:
        camps.setdefault((p["email_type"], _i(p["offer_rung"]) or 0, _i(p["template_id"])), []).append(p)
    rows, per = [], []
    for (et, rung, tid), aud in sorted(camps.items(), key=str):
        c, mks = {"email_type": et, "rung": rung, "template_id": tid}, [a["master_key"] for a in aud]
        ctx, blocks = wh.presend_ctx(c, d, mks, now), wh.person_blocks(d, mks, now)
        l8 = SP.presend_lock(campaign=c, send_date=d, audience=aud, presend_ctx=lambda *_: ctx)
        l9 = SP.person_lock(send_date=d, audience=aud, person_blocks=lambda *_: blocks)
        ok = 0
        for a in aud:
            g, b = G.gates(et, rung, ctx.get(a["master_key"]) or G.Ctx()), blocks.get(a["master_key"])
            ok += not g and not b
            rows.append({"master_key": a["master_key"], "email": a["email"], "email_type": et, "offer_rung": rung,
                         "template_id": tid, "gate": g[0] if g else None, "gates": g, "person_block": b,
                         "deliverable": not g and not b})
        per.append({"email_type": et, "rung": rung, "template_id": tid, "audience": len(aud), "deliverable": ok,
                    "L8": l8, "L9": l9})
    return {"send_date": d.isoformat(), "evaluated_at": now.isoformat(), "plan_run": wh.plan_run(d),
            "writer": wh.letter_fields(d), "L10": l10, "would_send": len(ws), "due_today": len(due),
            "planned_later": SC.count_by(later, ("email_type",)), "campaigns": per, "rows": rows}


def personal_this_week(wh, send_date, now) -> dict:
    """SG7 (contract 15e761dce94e): who has a PERSONAL sales letter this akcija week that is deliverable on the data
    of this moment (after the writer). -> {master_key: (email_type, planned_send_date ISO)}. The week and the letter
    set are the planner's (sequence_job.akcija_week, sequence.SALES_TYPES); the gates are L8 + L9 as at send."""
    d = _date(send_date)
    _tue, _label, mon, sun = _job().akcija_week(d)
    sh, lf = wh._shared(now), wh.lf_rows(d)
    rows = [p for p in wh.plan_rows(d).values() if _b(p["would_send"]) and p["email_type"] in S.SALES_TYPES
            and p.get("planned_send_date") and mon <= _date(p["planned_send_date"]) <= sun]
    blocks = wh.person_blocks(d, [p["master_key"] for p in rows], now)
    out = {}
    for p in rows:
        if blocks.get(p["master_key"]):
            continue
        if not G.gates(p["email_type"], _i(p["offer_rung"]) or 0, wh._ctx(p, d, sh, lf)):
            out[p["master_key"]] = (p["email_type"], _date(p["planned_send_date"]).isoformat())
    return out


def akcija_plan(wh, send_date, personal) -> dict:
    """Today's akcija audience rows after SG7. Only the PERSONAL_LETTER_THIS_WEEK reason moves; every other
    exclusion of the planner (SUPPRESSED, B2B_FLOW, LEAD_FLOW, EN_PENDING, BLOCKED_OR_UNKNOWN) stays as it is."""
    d = _day(send_date)
    rows = wh._rows(("akcija", d), f"SELECT master_key, in_audience, excluded_reason FROM `{T_AKCIJA}` "
                                   f"WHERE plan_date = DATE '{d}'")
    free = [r["master_key"] for r in rows if r.get("excluded_reason") in (None, "", PERSONAL)]
    excl = [(mk,) + personal[mk] for mk in free if mk in personal]
    return {"rows": len(rows), "before_in_audience": sum(_b(r["in_audience"]) for r in rows),
            "before_personal": sum(r.get("excluded_reason") == PERSONAL for r in rows),
            "in_audience": len(free) - len(excl), "personal": len(excl),
            "personal_by_type": SC.count_by([{"t": e[1]} for e in excl], ("t",)), "exclude": excl}


def akcija_record(query, send_date, plan, chunk=5000):
    """Write SG7 into today's rows of mkt_control.shadow_akcija_audience: reset the reason, then set it."""
    d = _day(send_date)
    query(f"UPDATE `{T_AKCIJA}` SET in_audience = TRUE, excluded_reason = NULL, personal_email_type = NULL, "
          f"personal_send_date = NULL WHERE plan_date = DATE '{d}' AND excluded_reason = '{PERSONAL}'")
    ex = plan["exclude"]
    for i in range(0, len(ex), chunk):
        vals = ", ".join(f"STRUCT({_sql_text(mk)} AS mk, {_sql_text(et)} AS et, DATE '{_day(sd)}' AS sd)"
                         for mk, et, sd in ex[i:i + chunk])
        query(f"UPDATE `{T_AKCIJA}` t SET in_audience = FALSE, excluded_reason = '{PERSONAL}', "
              f"personal_email_type = x.et, personal_send_date = x.sd FROM UNNEST([{vals}]) x "
              f"WHERE t.plan_date = DATE '{d}' AND t.master_key = x.mk AND t.in_audience")


def sample_pick(res, send_date) -> list:
    """One deliverable letter per type of today's send-time evaluation, or the reason there is none. The contact
    rotates by day (hash of date + master key), so the same client is not shown every morning."""
    d = _day(send_date)
    out = []
    for c in res["campaigns"]:
        et, base = c["email_type"], {"email_type": c["email_type"], "template_id": c["template_id"],
                                     "master_key": None, "email": None, "skip_reason": None}
        ok = [r for r in res["rows"] if r["email_type"] == et and r["offer_rung"] == c["rung"] and r["deliverable"]]
        if et in SAMPLE_SKIP:
            out.append({**base, "skip_reason": SAMPLE_SKIP[et]})
        elif not ok:
            out.append({**base, "skip_reason": "no deliverable letter of this type today: "
                        + (c["L8"][0][1] if c["L8"] else c["L9"][0][1] if c["L9"] else "none planned")})
        else:
            r = min(ok, key=lambda r: hashlib.sha256((d + r["master_key"]).encode()).hexdigest())
            out.append({**base, "master_key": r["master_key"], "email": r["email"]})
    have = {o["email_type"] for o in out}
    out += [{"email_type": et, "template_id": SAMPLE_SKIP_TEMPLATE.get(et), "master_key": None, "email": None,
             "skip_reason": why} for et, why in SAMPLE_SKIP.items() if et not in have]
    return sorted(out, key=lambda o: o["email_type"])


def sample_record(query, res, picks, force=False) -> bool:
    """Queue today's picks ONCE per day (a re-run of the job mails nothing twice). -> True when rows were written."""
    d, run = _day(res["send_date"]), res["plan_run"] or "none"
    if int(query(f"SELECT COUNT(*) AS n FROM `{T_SAMPLE}` WHERE plan_date = DATE '{d}'")[0]["n"]):
        if not force:
            return False
        query(f"DELETE FROM `{T_SAMPLE}` WHERE plan_date = DATE '{d}'")
    vals = ", ".join(f"(DATE '{d}', CURRENT_TIMESTAMP(), {_sql_text(run)}, {_sql_text(p['email_type'])}, "
                     f"{'NULL' if p['template_id'] is None else int(p['template_id'])}, {_sql_text(p['master_key'])}, "
                     f"{_sql_text(p['email'])}, {_sql_text(p['skip_reason'])})" for p in picks)
    query(f"INSERT INTO `{T_SAMPLE}` (plan_date, picked_at, plan_run_id, email_type, template_id, master_key, email, "
          f"skip_reason) VALUES {vals}")
    return True


def sample_trigger() -> str:
    """Start the mailing half (Cloud Run job on the campaign account). The job reads today's queue itself."""
    r = subprocess.run(["gcloud", "run", "jobs", "execute", SAMPLE_JOB, "--region", "europe-west1", "--project", P,
                        "--async", "--format=value(metadata.name)"], capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError("could not start " + SAMPLE_JOB + ": " + (r.stderr or r.stdout)[-300:])
    return r.stdout.strip().splitlines()[-1]


def refresh_hashes():
    """Run job tiktik-shadow-sample in hash mode and wait: it rewrites mkt_control.brevo_template_content from Brevo.
    -> None when it ran, else why not (short text). A failure is not fatal here: the mirror ages and closes the gate."""
    try:
        r = subprocess.run(["gcloud", "run", "jobs", "execute", SAMPLE_JOB, "--region", "europe-west1", "--project", P,
                            "--update-env-vars", "SAMPLE_MODE=hash", "--wait"], capture_output=True, text=True,
                           timeout=300)
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"[:200]
    return None if r.returncode == 0 else (r.stderr or r.stdout).strip()[-200:]


def checks(res) -> list:
    """The send-time answer as shadow_selfcheck rows. Hard = nothing could be sent today / the plan and the send
    path disagree about a person; info = the counts MAIN reads instead of the 08:05 gate columns."""
    rows = res["rows"]
    l10 = [m for _, m in res["L10"]]
    blocked = [r for r in rows if r["person_block"]]
    ok = [r for r in rows if r["deliverable"]]
    gated = [r for r in rows if r["gate"]]
    return [
        SC._row("sendtime_l10_writer_ok_on_latest_plan", "hard", not l10, len(l10),
                {"closed": l10, "plan_run": res["plan_run"], "writer": res["writer"]}),
        SC._row("sendtime_person_blocked_among_due", "hard", not blocked, len(blocked),
                SC.count_by(blocked, ("email_type", "person_block"))),
        SC._row("sendtime_due_today_by_type", "info", True, len(rows), SC.count_by(rows, ("email_type",))),
        SC._row("sendtime_deliverable_by_type", "info", True, len(ok), SC.count_by(ok, ("email_type",))),
        SC._row("sendtime_blocked_by_gate", "info", True, len(gated), SC.count_by(gated, ("email_type", "gate"))),
        SC._row("sendtime_planned_for_later_by_type", "info", True, sum(res["planned_later"].values()),
                res["planned_later"]),
    ] + _tc_checks(res) + ([SC._row("sendtime_akcija_audience", "info", True, res["akcija"]["in_audience"],
                  {k: v for k, v in res["akcija"].items() if k != "exclude"})] if res.get("akcija") else [])


MIRROR_FRESH_H = 2          # at the send-time evaluation the mirror must have been rewritten just before


def _tc_checks(res) -> list:
    tc = res.get("template_content")
    if tc is None:
        return []
    real = [t for t in tc["rows"] if not t["provisional"]]
    stale = [t["template_id"] for t in real if t["mirror_age_h"] is None or t["mirror_age_h"] > MIRROR_FRESH_H
             or t["mirror_error"]]
    short = lambda h: h and h[:12]  # noqa: E731
    return [
        SC._row("sendtime_template_hash_mirror_fresh", "hard", not stale and not tc["refresh_error"], len(set(stale)),
                {"stale_or_failed_template_ids": sorted(set(stale)), "refresh_error": tc["refresh_error"],
                 "limit_h": MIRROR_FRESH_H}),
        SC._row("sendtime_template_content", "info", True, sum(t["content_approved"] for t in real),
                [{"template_id": t["template_id"], "email_type": t["email_type"], "approved": short(t["approved_sha256"]),
                  "brevo": short(t["brevo_sha256"]), "ok": t["content_approved"]} for t in real]),
    ]


def _sql_text(v) -> str:
    return "NULL" if v is None else "'" + str(v).replace("\\", " ").replace("'", " ").replace("\n", " ") + "'"


def record(query, res, check_rows):
    """Replace today's sendtime_* rows of this plan run in mkt_control.shadow_selfcheck. Nothing else is written."""
    d, run = _day(res["send_date"]), res["plan_run"] or "none"
    query(f"DELETE FROM `{T_CHECK}` WHERE plan_date = DATE '{d}' AND check_name LIKE 'sendtime\\\\_%'")
    vals = ", ".join(f"(DATE '{d}', {_sql_text(run)}, {_sql_text(c['check_name'])}, {_sql_text(c['level'])}, "
                     f"{'TRUE' if c['ok'] else 'FALSE'}, {_sql_text(c['value'])}, {_sql_text(c['detail'])}, "
                     f"CURRENT_TIMESTAMP())" for c in check_rows)
    query(f"INSERT INTO `{T_CHECK}` (plan_date, run_id, check_name, level, ok, value, detail, checked_at) "
          f"VALUES {vals}")


def window_open(now: dt.datetime) -> dt.datetime:
    """The moment the gates are evaluated FOR: now, but never before the send window opens (the 08:55 check asks
    'could the 09:00 send go', not 'is it 09:00 yet')."""
    local = SP.riga(now)
    opens = local.replace(hour=SP.SEND_WINDOW[0].hour, minute=SP.SEND_WINDOW[0].minute, second=0, microsecond=0)
    return max(local, opens).astimezone(dt.timezone.utc)


def main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="send-time gates L10 / L8 / L9 on the real tables (read-only)")
    ap.add_argument("--record", action="store_true", help="write the sendtime_* rows to shadow_selfcheck")
    ap.add_argument("--sample", action="store_true", help="with --record: queue the daily shadow sample (once a "
                    "day) and start job " + SAMPLE_JOB)
    ap.add_argument("--sample-force", action="store_true", help="replace today's queue and mail again")
    ap.add_argument("--refresh-hashes", action="store_true", help="first rewrite the Brevo hash mirror (TC4)")
    ap.add_argument("--now", help="evaluate as if at this UTC ISO time (proofs)")
    ap.add_argument("--rows", type=int, default=0, help="print the first N evaluated rows per letter type")
    a = ap.parse_args(argv)
    now = dt.datetime.fromisoformat(a.now).replace(tzinfo=dt.timezone.utc) if a.now \
        else window_open(dt.datetime.now(dt.timezone.utc))
    q = cli_query if os.environ.get("BQ_TRANSPORT") == "cli" else rest_query
    refresh_error = refresh_hashes() if a.refresh_hashes else None
    wh = Warehouse(q)
    day = SP.riga(now).date()
    res = evaluate(wh, day, now)
    res["template_content"] = {"refresh_error": refresh_error, "refreshed": bool(a.refresh_hashes),
                               "rows": wh.template_content(dt.datetime.now(dt.timezone.utc))}
    res["akcija"] = akcija_plan(wh, day, personal_this_week(wh, day, now))          # SG7
    cs = checks(res)
    if a.record:
        akcija_record(q, day, res["akcija"])
        record(q, res, cs)
    sample = None
    if a.sample or a.sample_force:
        picks = sample_pick(res, day)
        sample = {"picks": [{k: p[k] for k in ("email_type", "template_id", "master_key", "skip_reason")} for p in picks],
                  "queued": False, "execution": None}
        # only inside the window check: a sample is a letter that could go today
        if a.record and not res["L10"] and any(p["skip_reason"] is None for p in picks):
            sample["queued"] = sample_record(q, res, picks, force=a.sample_force)
            if sample["queued"]:
                sample["execution"] = sample_trigger()
    res["akcija"] = {k: v for k, v in res["akcija"].items() if k != "exclude"}
    seen, rows_sample = {}, []
    for r in res["rows"]:
        if seen.setdefault(r["email_type"], 0) < a.rows:
            seen[r["email_type"]] += 1
            rows_sample.append({k: r[k] for k in ("email_type", "master_key", "gates", "person_block", "deliverable")})
    print(json.dumps({**{k: v for k, v in res.items() if k != "rows"}, "checks": cs, "sample": rows_sample,
                      "shadow_sample": sample,
                      "recorded": bool(a.record)}, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
