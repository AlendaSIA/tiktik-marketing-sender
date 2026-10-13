"""TUESDAY CHAIN ORCHESTRATOR — R1 (Raivis-approved contract, MAIN 3 13.10.2026; wording below is the contract's, do
not change it here: a misfit goes to MAIN 3).

Step order and the ONLY start condition of each step:
 S1 week prices  -> OK = week_price_run run_id for today, mode LIVE, 0 rows MOZ_FAIL/ERROR
                    ("today's LIVE run_id" = the latest mkt_control.week_price_run_summary row with kind='weekly' AND
                     mode='LIVE' AND finished_at today (Riga); hourly pd_edit runs and DRY runs never count - MAIN 3 12:11)
 S2 cab book     -> OK = cabinet_price_log row run_id cab-<YYYYMMDD> ok=true, written after S1 OK
                    (MAIN 3 12:11: ok=true AND logged_at after S1 finished_at; the VPS writer starts itself - PAP 14)
 S3 letter_fields+slots -> start only after S2 OK; OK = letter_fields_log status OK AND counts.cab_ok=true AND
                    counts.cab_run_id=cab-<YYYYMMDD>
 S4 FLOW/FLOW2/FLOWR + AK/maker build -> start only after S3 OK; OK = ak_tuesday_log status BUILT for today AND block
                    build after S3 finished_at
 S5 send-day     -> start only after S4 OK; gate checks EVERY rendered card (P and R, FLOW*) == cab-<YYYYMMDD>
Rules:
 a) letter_fields never logs OK when cab_ok=false -> HELD   (tiktik-marketing-sync b622fbb)
 b) this job polls every 10 min 03:30-08:45 Riga; each step starts the moment the previous OK row exists. The Tuesday
    clock triggers of S3/S4/S5 are removed; this job owns them.
 c) a step not OK by 08:45 -> nothing sent, WhatsApp alert to the MAIN line with the step name and reason. No manual
    override path (this module has none).
 d) Monday 19.10 DRY (no sends): one row per step in mkt_control.tuesday_chain_log (start / finish / ok).
MAIN 3 13.10 11:24 (4): S5 earliest start configurable, default 09:00 Riga; S5 starts at the later of S4 OK and that.

ONE execution = one tick: read today's rows + facts, write the step rows, start at most what is due, exit. Stateless
between ticks; the table is the state. Env:
  CHAIN_MODE        LIVE (default; Tuesdays only) | DRY (any day; every started job runs DRY / SHADOW)
  CHAIN_DATE        DRY only: the chain date (default today, Riga)
  CHAIN_S5_ENABLED  "1" = S5 may start send-day (LIVE). Anything else: S5 row DISABLED (until the Mon DRY is green)
  CHAIN_S5_NOT_BEFORE  HH:MM, default 09:00
  CHAIN_S3_NOT_BEFORE  HH:MM, default 00:00 (DRY 19.10: after the normal Monday 08:40/09:00 runs, see tools)
  CHAIN_START / CHAIN_DEADLINE  HH:MM, default 03:30 / 08:45 (DRY may move the deadline; LIVE never sets them)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import zoneinfo

P = "jaunais-za-aizv04022026"
M = f"{P}.mkt_control"
REGION = "europe-west1"
RIGA = zoneinfo.ZoneInfo("Europe/Riga")
T_LOG = f"{M}.tuesday_chain_log"
STEPS = ("S1", "S2", "S3", "S4", "S5")
WA_FROM, WA_TO = "+37122811277", "+37126711822"     # MAIN line: morning WhatsApp from +37122811277 to Raivis
MAX_STARTS = {"S3": 6, "S4": 18, "S5": 1}
DDL = f"""CREATE TABLE IF NOT EXISTS `{T_LOG}` (
  chain_date DATE, mode STRING, step STRING, started_at TIMESTAMP, finished_at TIMESTAMP, ok BOOL, status STRING,
  reason STRING, executions STRING, starts INT64, evidence STRING, alert_sent_at TIMESTAMP, updated_at TIMESTAMP)
OPTIONS (description='R1 Tuesday chain: one row per step per chain_date and mode (orchestrator tiktik-tuesday-chain).')"""


def hhmm(v, default):
    try:
        h, m = (v or "").strip().split(":")
        return dt.time(int(h), int(m))
    except (ValueError, TypeError):
        return default


class Cfg:
    def __init__(self, env, now):
        self.mode = "DRY" if (env.get("CHAIN_MODE") or "").strip().upper() == "DRY" else "LIVE"
        today = now.astimezone(RIGA).date()
        self.date = dt.date.fromisoformat(env["CHAIN_DATE"]) if (self.mode == "DRY" and env.get("CHAIN_DATE")) else today
        self.s5_enabled = (env.get("CHAIN_S5_ENABLED") or "").strip() == "1"
        self.s5_not_before = hhmm(env.get("CHAIN_S5_NOT_BEFORE"), dt.time(9, 0))
        self.s3_not_before = hhmm(env.get("CHAIN_S3_NOT_BEFORE"), dt.time(0, 0))
        dry = self.mode == "DRY"
        self.start = hhmm(env.get("CHAIN_START"), dt.time(3, 30)) if dry else dt.time(3, 30)
        self.deadline = hhmm(env.get("CHAIN_DEADLINE"), dt.time(8, 45)) if dry else dt.time(8, 45)
        self.cab = f"cab-{self.date:%Y%m%d}"
        fd = (env.get("CHAIN_FIRST_DATE") or "").strip()
        self.first_date = dt.date.fromisoformat(fd) if fd else None

    def at(self, t):
        return dt.datetime.combine(self.date, t, RIGA).astimezone(dt.timezone.utc)


def _aware(v):
    if v in (None, ""):
        return None
    if isinstance(v, dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)
    x = dt.datetime.fromisoformat(str(v).replace(" UTC", "").replace("Z", "+00:00"))
    return x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)


# ------------------------------------------------------------------------------------------------ facts (SQL)
def facts_sql(c: Cfg) -> str:
    d, m = c.date.isoformat(), c.mode
    blk_mode = "DRY" if m == "DRY" else "LIVE"
    return f"""
WITH s1 AS (SELECT run_id, started_at, finished_at FROM `{M}.week_price_run_summary`
            WHERE kind = 'weekly' AND mode = '{m}' AND DATE(finished_at, 'Europe/Riga') = DATE '{d}'
            ORDER BY finished_at DESC LIMIT 1),
s1r AS (SELECT COUNT(*) AS n, COUNTIF(r.status IN ('MOZ_FAIL', 'ERROR')) AS bad, COUNTIF(r.mode != '{m}') AS other_mode
        FROM `{M}.week_price_run` r WHERE r.run_id = (SELECT run_id FROM s1)),
s2 AS (SELECT ok, reason, logged_at, `rows` AS n FROM `{M}.cabinet_price_log` WHERE run_id = '{c.cab}'
       ORDER BY logged_at DESC NULLS LAST LIMIT 1),
s2n AS (SELECT COUNT(*) AS n FROM `{M}.cabinet_price_log` WHERE run_id = '{c.cab}'),
s3 AS (SELECT run_id, run_ts, finished_at, status, reason, JSON_VALUE(counts, '$.cab_ok') AS cab_ok,
              JSON_VALUE(counts, '$.cab_run_id') AS cab_run_id
       FROM `{M}.letter_fields_log` WHERE plan_date = DATE '{d}' ORDER BY run_ts DESC LIMIT 1),
s3ok AS (SELECT run_id, run_ts, finished_at FROM `{M}.letter_fields_log`
         WHERE plan_date = DATE '{d}' AND status = 'OK' AND JSON_VALUE(counts, '$.cab_ok') = 'true'
           AND JSON_VALUE(counts, '$.cab_run_id') = '{c.cab}' ORDER BY run_ts DESC LIMIT 1),
plan AS (SELECT COUNT(*) AS n FROM `{M}.shadow_send_plan` WHERE plan_date = DATE '{d}'),
fl AS (SELECT run_id, status, errors, finished_at FROM `{M}.block_push_log`
       WHERE run_date = DATE '{d}' AND block = 'FLOW_HTML' AND mode = '{blk_mode}' ORDER BY finished_at DESC LIMIT 1),
tue AS (SELECT run_id, status, reason, logged_at, built_at FROM `{M}.ak_tuesday_log`
        WHERE run_date = DATE '{d}' AND mode = '{blk_mode}' ORDER BY logged_at DESC LIMIT 1),
tue_built AS (SELECT run_id, logged_at FROM `{M}.ak_tuesday_log`
              WHERE run_date = DATE '{d}' AND mode = '{blk_mode}' AND status = 'BUILT' ORDER BY logged_at DESC LIMIT 1),
mir AS (SELECT LEAST((SELECT MAX(ingested_at) FROM `{P}.channel_raw.mozello_products`),
                     (SELECT MAX(ingested_at) FROM `{P}.channel_raw.mozello_variants`)) AS t_at),
asm AS (SELECT last_modified_time AS t_at FROM `{M}.INFORMATION_SCHEMA.PARTITIONS`
        WHERE table_name = 'akcija_assembled' AND partition_id = '{c.date:%Y%m%d}' AND total_rows > 0)
SELECT TO_JSON_STRING(STRUCT(
  (SELECT AS STRUCT * FROM s1) AS s1, (SELECT AS STRUCT * FROM s1r) AS s1r,
  (SELECT AS STRUCT * FROM s2) AS s2, (SELECT n FROM s2n) AS s2_rows,
  (SELECT AS STRUCT * FROM s3) AS s3, (SELECT AS STRUCT * FROM s3ok) AS s3ok, (SELECT n FROM plan) AS plan_rows,
  (SELECT AS STRUCT * FROM fl) AS flow, (SELECT AS STRUCT * FROM tue) AS tue, (SELECT AS STRUCT * FROM tue_built) AS tue_built,
  (SELECT t_at FROM mir) AS mirror_at, (SELECT t_at FROM asm) AS assembled_at)) AS j"""


def s5_sql(c: Cfg, since) -> str:
    mode = "live" if c.mode == "LIVE" else "dry"
    return f"""
SELECT run_id, COUNT(*) AS types, STRING_AGG(DISTINCT type_refusal, ' | ') AS refusals, SUM(IFNULL(sent, 0)) AS sent,
       MAX(finished_at) AS finished_at
FROM `{M}.send_day_run` WHERE send_date = DATE '{c.date}' AND mode = '{mode}' AND finished_at >= TIMESTAMP '{since}'
GROUP BY run_id ORDER BY MAX(finished_at) DESC LIMIT 1"""


def s5_holds_sql(run_id) -> str:
    return f"""SELECT COUNTIF(STARTS_WITH(decision, 'S5_CARD_NE_CAB')) AS card_holds, COUNT(DISTINCT master_key) AS members
FROM `{M}.send_day_audience` WHERE run_id = '{run_id}'"""


# ------------------------------------------------------------------------------------------------ pure verdicts
def verdict_s1(f, c: Cfg):
    s1, r = f.get("s1"), f.get("s1r") or {}
    if not s1:
        return False, None, f"no weekly {c.mode} week-price run finished today"
    if int(r.get("n") or 0) == 0:
        return False, None, f"{s1['run_id']}: no week_price_run rows"
    if int(r.get("other_mode") or 0):
        return False, None, f"{s1['run_id']}: rows of another mode"
    if int(r.get("bad") or 0):
        return False, None, f"{s1['run_id']}: {int(r['bad'])} rows MOZ_FAIL/ERROR"
    return True, _aware(s1["finished_at"]), f"{s1['run_id']} {c.mode} 0 MOZ_FAIL/ERROR of {int(r['n'])}"


def verdict_s2(f, c: Cfg, s1_at):
    s2 = f.get("s2")
    if not s2:
        return False, None, f"no cabinet_price_log row {c.cab}"
    la = _aware(s2.get("logged_at"))
    if not s2.get("ok"):
        return False, None, f"{c.cab} ok=false {s2.get('reason') or ''}".strip()
    if la is None:
        return False, None, f"{c.cab} ok=true but logged_at is NULL"
    if s1_at is None or la <= s1_at:
        return False, None, f"{c.cab} logged_at {la:%H:%M:%S}Z is not after S1"
    extra = f" ({int(f.get('s2_rows') or 0)} log rows)" if int(f.get("s2_rows") or 0) > 1 else ""
    return True, la, f"{c.cab} ok=true logged {la:%H:%M:%S}Z, {int(s2.get('n') or 0)} rows{extra}"


def verdict_s3(f, c: Cfg, s2_at, min_ts=None):
    """min_ts (DRY only): the OK run must be one the chain itself started (the normal Monday 08:40 run does not count)."""
    ok = f.get("s3ok")
    if ok and s2_at and _aware(ok["run_ts"]) > s2_at and (min_ts is None or _aware(ok["run_ts"]) >= min_ts):
        return True, _aware(ok["finished_at"]), f"{ok['run_id']} OK cab_ok=true {c.cab}"
    last = f.get("s3") or {}
    if last:
        return False, None, f"latest {last.get('run_id')} {last.get('status')} cab_ok={last.get('cab_ok')} {(last.get('reason') or '')[:150]}"
    return False, None, "no letter_fields run today"


def verdict_s4(f, c: Cfg, s3_at):
    tb, fl = f.get("tue_built"), f.get("flow") or {}
    flow_ok = bool(fl) and fl.get("status") == "OK" and int(fl.get("errors") or 0) == 0 and s3_at is not None \
        and _aware(fl["finished_at"]) > s3_at
    ak_ok = bool(tb) and s3_at is not None and _aware(tb["logged_at"]) > s3_at
    if flow_ok and ak_ok:
        return True, max(_aware(fl["finished_at"]), _aware(tb["logged_at"])), \
            f"FLOW {fl['run_id']} OK + ak_tuesday {tb['run_id']} BUILT, both after S3"
    why = []
    if not flow_ok:
        why.append(f"FLOW {fl.get('run_id')} {fl.get('status')} at {fl.get('finished_at')}" if fl else "no FLOW build today")
    if not ak_ok:
        t = f.get("tue") or {}
        why.append(f"ak_tuesday {t.get('status')} {(t.get('reason') or '')[:120]}" if t else "no ak_tuesday row today")
    return False, None, "; ".join(why) + " (S4 needs both after S3)"


# ------------------------------------------------------------------------------------------------ the tick
class Chain:
    def __init__(self, bq, run_api, alert, env, now=None):
        self.bq, self.run_api, self.alert_fn, self.env = bq, run_api, alert, env
        self.now = now or dt.datetime.now(dt.timezone.utc)
        self.c = Cfg(env, self.now)

    def q(self, sql):
        return [dict(r) for r in self.bq.query(sql).result()]

    def rows(self):
        return {r["step"]: r for r in self.q(f"SELECT * FROM `{T_LOG}` WHERE chain_date = DATE '{self.c.date}' "
                                              f"AND mode = '{self.c.mode}'")}

    def put(self, step, **kw):
        """Upsert the step row (one row per chain_date x mode x step)."""
        cur = dict(self._rows.get(step) or {"chain_date": self.c.date.isoformat(), "mode": self.c.mode, "step": step,
                                             "starts": 0})
        cur.update(kw)
        cur["updated_at"] = self.now.isoformat()
        self._rows[step] = cur
        cols = ("chain_date", "mode", "step", "started_at", "finished_at", "ok", "status", "reason", "executions",
                "starts", "evidence", "alert_sent_at", "updated_at")
        typ = {"chain_date": "DATE", "started_at": "TIMESTAMP", "finished_at": "TIMESTAMP", "ok": "BOOL",
               "starts": "INT64", "alert_sent_at": "TIMESTAMP", "updated_at": "TIMESTAMP"}

        def lit(k):
            v = cur.get(k)
            if v is None:
                return f"CAST(NULL AS {typ.get(k, 'STRING')})"
            if k == "ok":
                return "TRUE" if v else "FALSE"
            if k == "starts":
                return str(int(v))
            if isinstance(v, (dt.datetime, dt.date)):
                v = v.isoformat()
            s = str(v).replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
            return f"{typ[k]} '{s}'" if k in typ else f"'{s}'"
        sel = ", ".join(f"{lit(k)} AS {k}" for k in cols)
        self.q(f"""MERGE `{T_LOG}` t USING (SELECT {sel}) s
          ON t.chain_date = s.chain_date AND t.mode = s.mode AND t.step = s.step
          WHEN MATCHED THEN UPDATE SET {", ".join(f"{k} = s.{k}" for k in cols[3:])}
          WHEN NOT MATCHED THEN INSERT ROW""")

    def start(self, step, jobs, why):
        row = self._rows.get(step) or {}
        n = int(row.get("starts") or 0)
        if n >= MAX_STARTS.get(step, 3):
            self.put(step, status="FAILED", ok=False, reason=f"{why}; gave up after {n} starts")
            return False
        batch = {"at": self.now.isoformat(), "names": [self.run_api(job, args, env) for job, args, env in jobs]}
        prev = json.loads(row.get("executions") or "[]")
        self.put(step, status="RUNNING", ok=False, started_at=row.get("started_at") or self.now.isoformat(),
                 starts=n + 1, executions=json.dumps(prev + [batch]), reason=why)
        return True

    def last_batch(self, step):
        b = json.loads((self._rows.get(step) or {}).get("executions") or "[]")
        return b[-1] if b else None

    def busy(self, step):
        """True while an execution of the step's last start is still running (Cloud Run says so; < 60 min old)."""
        b = self.last_batch(step)
        if not b or (self.now - _aware(b["at"])) > dt.timedelta(minutes=60):
            return False
        return any(self.run_api.status(n) == "RUNNING" for n in b["names"])

    def alert(self, step, reason):
        r = self._rows.get(step) or {}
        if r.get("alert_sent_at"):
            return
        text = (f"{'[DRY] ' if self.c.mode == 'DRY' else ''}⚠️ Otrdienas ķēde {self.c.date:%d.%m}: {step} nav OK līdz "
                f"{self.c.deadline:%H:%M} — {reason[:300]}. Nekas netiek sūtīts. (Sūtīšanas dzinējs 8, tiktik-tuesday-chain)")
        ok = self.alert_fn(text)
        self.put(step, alert_sent_at=self.now.isoformat() if ok else None,
                 reason=(reason if ok else f"{reason} | ALERT_FAILED"))

    # -- one tick
    def tick(self) -> dict:
        c, now_r = self.c, self.now.astimezone(RIGA)
        if c.mode == "LIVE" and c.date.weekday() != 1:
            return {"skip": "LIVE mode runs on Tuesdays only"}
        if c.mode == "LIVE" and c.first_date and c.date < c.first_date:
            return {"skip": f"chain starts on {c.first_date}"}
        if now_r.time() < c.start and now_r.date() == c.date:
            return {"skip": f"before {c.start:%H:%M}"}
        self.q(DDL)
        self._rows = self.rows()
        f = json.loads(self.q(facts_sql(c))[0]["j"])
        past_deadline = self.now > c.at(c.deadline)
        out = {"date": str(c.date), "mode": c.mode, "now": now_r.isoformat(timespec="minutes"), "steps": {}}

        def done(step, ok, at, why, extra=None):
            r = self._rows.get(step) or {}
            if ok and not r.get("ok"):
                self.put(step, ok=True, status="OK", finished_at=at.isoformat(), reason=why,
                         started_at=r.get("started_at") or (extra or {}).get("started_at"),
                         evidence=json.dumps(extra or {}, default=str)[:4000])
            out["steps"][step] = ("OK " if ok else "") + why
            return ok

        # S1 - started by its own 03:30 clock (Build 3 job); DRY: this job starts the DRY weekly run once
        ok1, at1, why1 = verdict_s1(f, c)
        if not done("S1", ok1, at1, why1, {"started_at": (f.get("s1") or {}).get("started_at"), "run": f.get("s1")}):
            if c.mode == "DRY" and not (self._rows.get("S1") or {}).get("starts"):
                self.start("S1", [("tiktik-week-prices", ["--dry"], {})], "DRY weekly run started by the chain")
            elif not self.busy("S1"):
                self.put("S1", ok=False, status="WAITING" if not past_deadline else "DEADLINE", reason=why1)
            return self.finish(out, "S1", why1, past_deadline)
        # S2 - the VPS cab writer starts itself as soon as S1 is OK (PAP cenu bloks 14); we only read its row
        ok2, at2, why2 = verdict_s2(f, c, at1)
        if not done("S2", ok2, at2, why2, {"cab": f.get("s2"), "log_rows": f.get("s2_rows")}):
            self.put("S2", ok=False, status="WAITING", reason=why2, started_at=(self._rows.get("S2") or {}).get("started_at") or at1.isoformat())
            return self.finish(out, "S2", why2, past_deadline)
        # S3 - letter_fields: start only after S2 OK
        s3_first = _aware((self._rows.get("S3") or {}).get("started_at")) if c.mode == "DRY" else None
        ok3, at3, why3 = verdict_s3(f, c, at2, min_ts=(s3_first or self.now) if c.mode == "DRY" else None)
        if not done("S3", ok3, at3, why3, {"lf": f.get("s3ok")}):
            if past_deadline:
                return self.finish(out, "S3", why3, True)
            if now_r.time() < c.s3_not_before:
                self.put("S3", ok=False, status="WAITING", reason=f"S2 OK; S3 not before {c.s3_not_before:%H:%M} (DRY)")
            elif int(f.get("plan_rows") or 0) == 0:
                self.put("S3", ok=False, status="WAITING", reason=f"S2 OK; no shadow_send_plan rows for {c.date} yet")
            elif not self.busy("S3"):        # first start, or the last start finished without an OK row -> again
                env = {"LETTER_FIELDS_WRITE": "SHADOW"} if c.mode == "DRY" else {}
                self.start("S3", [("tiktik-letter-fields", None, env)], f"S2 OK ({why2}); {why3}")
            return self.finish(out, "S3", why3, False)
        # S4 - FLOW/FLOW2/FLOWR + AK/maker: start only after S3 OK
        ok4, at4, why4 = verdict_s4(f, c, at3)
        if not done("S4", ok4, at4, why4, {"flow": f.get("flow"), "ak_tuesday": f.get("tue_built")}):
            if past_deadline:
                return self.finish(out, "S4", why4, True)
            self.step4(f, c, at1, at3, why4)
            return self.finish(out, "S4", why4, False)
        # S5 - send-day: start only after S4 OK, not before CHAIN_S5_NOT_BEFORE, never when S4 was late
        return self.step5(f, c, at4, out, now_r)

    def step4(self, f, c, s1_at, s3_at, why):
        dry = c.mode == "DRY"
        base = {"BLOCKS_WRITE": "DRY"} if dry else {}
        jobs = []
        fl = f.get("flow") or {}
        flow_after = bool(fl) and s3_at and _aware(fl["finished_at"]) > s3_at
        if not flow_after or fl.get("status") != "OK":
            jobs.append(("tiktik-ak-writer", None, dict(base, BLOCKS_WAIT_MIN="0")))
        tb = f.get("tue_built")
        if not (tb and _aware(tb["logged_at"]) > s3_at):
            mir, asm = _aware(f.get("mirror_at")), _aware(f.get("assembled_at"))
            if mir is None or (s1_at and mir <= s1_at):
                jobs.append(("mozello-products-ingest", None, {}))      # the shop mirror after the week prices
            elif asm is None or asm <= mir:
                jobs.append(("tiktik-shadow-selfcheck-dash", ["-c", ASSEMBLY_CMD], {}))   # akcija_assembled after the mirror
            else:
                env = dict(base, BLOCKS_PART="ak_tuesday", BLOCKS_TUE_KLUDA_AT=f"{c.deadline:%H:%M}",
                           BLOCKS_TUE_LATEST=f"{c.deadline:%H:%M}")
                if dry:
                    env.update(BLOCKS_FORCE_TUESDAY="1", BLOCKS_WEEK_RUN_MODE="DRY", BLOCKS_FORCE_NOW="06:00")
                jobs.append(("tiktik-ak-writer", None, env))
        if jobs and not self.busy("S4"):
            self.start("S4", jobs, f"S3 OK; {why}")
        elif not jobs:
            self.put("S4", ok=False, status="WAITING", reason=why)

    def step5(self, f, c, s4_at, out, now_r):
        r = self._rows.get("S5") or {}
        late = s4_at > c.at(c.deadline)
        if late:
            self.put("S5", ok=False, status="DEADLINE", reason=f"S4 OK only at {s4_at.astimezone(RIGA):%H:%M} (> {c.deadline:%H:%M}) - nothing sent")
            self.alert("S5", f"S4 OK only at {s4_at.astimezone(RIGA):%H:%M}")
            out["steps"]["S5"] = "DEADLINE"
            return out
        if c.mode == "LIVE" and not c.s5_enabled:
            self.put("S5", ok=False, status="DISABLED", reason="S4 OK; S5 start disabled until the Mon 19.10 chain DRY is green (CHAIN_S5_ENABLED)")
            out["steps"]["S5"] = "DISABLED"
            return out
        if not r.get("starts"):
            if now_r.time() < c.s5_not_before:
                self.put("S5", ok=False, status="WAITING", reason=f"S4 OK; S5 not before {c.s5_not_before:%H:%M}")
                out["steps"]["S5"] = f"WAITING until {c.s5_not_before:%H:%M}"
                return out
            args = ["send_day.py", "--mode=dry", "--chain-dry=1", f"--date={c.date}"] if c.mode == "DRY" else None
            self.start("S5", [("tiktik-send-day", args, {})], "S4 OK")
            out["steps"]["S5"] = "STARTED"
            return out
        st = self.run_api.status((self.last_batch("S5") or {"names": [None]})["names"][-1])
        if st == "RUNNING":
            out["steps"]["S5"] = "RUNNING"
            return out
        run = self.q(s5_sql(c, r["started_at"]))
        run = run[0] if run else None
        bad = [x for x in ((run or {}).get("refusals") or "").split(" | ")
               if x.startswith(("D44", "CHAIN_", "S5_FLOW"))]
        holds = self.q(s5_holds_sql(run["run_id"]))[0] if run else {}
        ok = st == "SUCCEEDED" and run is not None and not bad
        why = (f"send-day {st}; run {(run or {}).get('run_id')} types {(run or {}).get('types')} sent {(run or {}).get('sent')}; "
               f"S5 card holds {holds.get('card_holds')}" + (f"; refused: {bad}" if bad else ""))
        self.put("S5", ok=ok, status="OK" if ok else "FAILED", reason=why,
                 finished_at=_aware((run or {}).get("finished_at") or self.now).isoformat(),
                 evidence=json.dumps({"run": run, "holds": holds, "execution_state": st}, default=str)[:4000])
        if not ok:
            self.alert("S5", why)
        out["steps"]["S5"] = ("OK " if ok else "FAILED ") + why
        return out

    def finish(self, out, step, why, past_deadline):
        if past_deadline:
            r = self._rows.get(step) or {}
            if r.get("status") != "DEADLINE":
                self.put(step, ok=False, status="DEADLINE", reason=f"not OK by {self.c.deadline:%H:%M}: {why}")
            self.alert(step, why)
            for s in STEPS[STEPS.index(step) + 1:]:
                if not (self._rows.get(s) or {}).get("status"):
                    self.put(s, ok=False, status="NOT_STARTED", reason=f"{step} not OK by {self.c.deadline:%H:%M}")
        out["waiting_on"] = step
        return out


ASSEMBLY_CMD = ("rm -rf /tmp/eng && mkdir -p /tmp/eng && gsutil -q cp gs://jaunais-za-aizv04022026-brevo-history/_code/"
                "send_engine.tgz /tmp/eng/b.tgz && tar xzf /tmp/eng/b.tgz -C /tmp/eng && python3 /tmp/eng/akcija_assembly.py --record")


# ------------------------------------------------------------------------------------------------ Cloud Run + alert
class RunApi:
    """Cloud Run Admin API v2: start a job execution with overrides; read an execution's state."""
    def __init__(self, session):
        self.s = session

    def __call__(self, job, args, env):
        ov = {}
        if args is not None:
            ov["args"] = list(args)
        if env:
            ov["env"] = [{"name": k, "value": str(v)} for k, v in env.items()]
        body = {"overrides": {"containerOverrides": [ov]}} if ov else {}
        r = self.s.post(f"https://run.googleapis.com/v2/projects/{P}/locations/{REGION}/jobs/{job}:run", json=body, timeout=60)
        r.raise_for_status()
        return (r.json().get("metadata") or {}).get("name") or f"{job}:?"

    def status(self, name):
        if not name or name.endswith(":?"):
            return "UNKNOWN"
        r = self.s.get(f"https://run.googleapis.com/v2/{name}", timeout=60)
        if r.status_code != 200:
            return "UNKNOWN"
        e = r.json()
        if not e.get("completionTime"):
            return "RUNNING"
        return "SUCCEEDED" if int(e.get("succeededCount") or 0) >= 1 and not int(e.get("failedCount") or 0) else "FAILED"


def whatsapp(token):
    import requests

    def send(text):
        try:
            r = requests.post("https://app.timelines.ai/integrations/api/messages",
                              headers={"Authorization": f"Bearer {token}"},
                              json={"phone": WA_TO, "whatsapp_account_phone": WA_FROM, "text": text}, timeout=30)
            print(f"ALERT {r.status_code} {r.text[:200]}")
            return r.status_code < 300
        except Exception as e:  # noqa: BLE001 - an alert failure is logged in the row, never fatal
            print(f"ALERT_FAILED {type(e).__name__}: {e}")
            return False
    return send


def main(argv=None):
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    from google.cloud import bigquery
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    sess = AuthorizedSession(creds)
    token = os.environ.get("TIMELINESAI_API_TOKEN") or ""
    if not token:
        r = sess.get(f"https://secretmanager.googleapis.com/v1/projects/{P}/secrets/TIMELINESAI_API_TOKEN/versions/latest:access", timeout=30)
        if r.status_code == 200:
            import base64
            token = base64.b64decode(r.json()["payload"]["data"]).decode()
    ch = Chain(bigquery.Client(project=P), RunApi(sess), whatsapp(token), os.environ)
    res = ch.tick()
    print("TUESDAY_CHAIN " + json.dumps(res, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
