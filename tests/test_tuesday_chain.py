"""R1 Tuesday chain orchestrator (MAIN 3 13.10): each step starts on the previous step's OK row, not on a clock."""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tuesday_chain as T  # noqa: E402

TUE = dt.date(2026, 10, 20)
U = dt.timezone.utc


def z(h, m):                       # Riga 20.10 (EEST, UTC+3) -> UTC iso
    return dt.datetime(2026, 10, 20, h - 3, m, tzinfo=U).isoformat()


def facts(**kw):
    f = {"s1": {"run_id": "wp-20261020-033000-aaaa", "started_at": z(3, 30), "finished_at": z(3, 50)},
         "s1r": {"n": 1411, "bad": 0, "other_mode": 0},
         "s2": {"ok": True, "reason": None, "logged_at": z(4, 20), "n": 18000}, "s2_rows": 1,
         "s3": None, "s3ok": None, "plan_rows": 7900, "flow": None, "tue": None, "tue_built": None,
         "mirror_at": z(4, 0), "assembled_at": z(4, 5)}
    f.update(kw)
    return f


class FakeJob:
    def __init__(self, rows):
        self.rows = rows

    def result(self):
        return self.rows


class FakeBQ:
    def __init__(self, f, s5run=None, s5holds=None):
        self.f, self.log, self.sql, self.s5run, self.s5holds = f, {}, [], s5run, s5holds or {"card_holds": 0, "members": 0}

    def query(self, sql):
        self.sql.append(sql)
        if "TO_JSON_STRING(STRUCT(" in sql:
            return FakeJob([{"j": json.dumps(self.f)}])
        if sql.startswith("SELECT * FROM `") and "tuesday_chain_log" in sql:
            return FakeJob([dict(v) for v in self.log.values()])
        if sql.lstrip().startswith("MERGE"):
            sel = sql.split("USING (SELECT ", 1)[1].split(") s\n", 1)[0]
            step = sel.split(" AS step")[0].rsplit("'", 2)[1]
            self.log[step] = {"step": step, "_sql": sel}
            return FakeJob([])
        if "FROM `jaunais-za-aizv04022026.mkt_control.send_day_run`" in sql:
            return FakeJob([self.s5run] if self.s5run else [])
        if "send_day_audience" in sql:
            return FakeJob([self.s5holds])
        return FakeJob([])


class FakeRun:
    def __init__(self, state="SUCCEEDED"):
        self.calls, self.state = [], state

    def __call__(self, job, args, env):
        self.calls.append((job, args, env))
        return f"projects/p/locations/europe-west1/jobs/{job}/executions/{job}-{len(self.calls)}"

    def status(self, name):
        return self.state


def chain(f, now_riga, env=None, run=None, alerts=None, bq=None):
    bq = bq or FakeBQ(f)
    ch = T.Chain(bq, run or FakeRun(), (lambda t: (alerts.append(t) if alerts is not None else None) or True),
                 env or {}, now=dt.datetime.fromisoformat(z(*now_riga)))
    return ch, bq


def test_live_only_on_tuesday_and_not_before_0330():
    ch, _ = chain(facts(), (5, 0))
    ch.now = dt.datetime(2026, 10, 19, 3, 0, tzinfo=U)
    ch.c = T.Cfg({}, ch.now)
    assert "Tuesdays only" in ch.tick()["skip"]
    ch2, _ = chain(facts(), (3, 20))
    assert "before 03:30" in ch2.tick()["skip"]


def test_s1_contract_rows_and_mode():
    c = T.Cfg({}, dt.datetime.fromisoformat(z(5, 0)))
    assert T.verdict_s1(facts(), c)[0]
    assert "102 rows MOZ_FAIL/ERROR" in T.verdict_s1(facts(s1r={"n": 1411, "bad": 102, "other_mode": 0}), c)[2]
    assert not T.verdict_s1(facts(s1=None), c)[0]
    s = T.facts_sql(c)
    assert "kind = 'weekly' AND mode = 'LIVE'" in s and "status IN ('MOZ_FAIL', 'ERROR')" in s   # pd_edit never counts


def test_s2_needs_ok_and_logged_after_s1():
    c = T.Cfg({}, dt.datetime.fromisoformat(z(5, 0)))
    s1 = dt.datetime.fromisoformat(z(3, 50))
    assert T.verdict_s2(facts(), c, s1)[0]
    assert "not after S1" in T.verdict_s2(facts(s2={"ok": True, "logged_at": z(3, 40), "n": 1}), c, s1)[2]
    assert "ok=false" in T.verdict_s2(facts(s2={"ok": False, "reason": "book not ready 08:30", "logged_at": z(8, 30)}), c, s1)[2]
    assert "logged_at is NULL" in T.verdict_s2(facts(s2={"ok": True, "logged_at": None}), c, s1)[2]


def test_s3_starts_only_after_s2_ok_and_needs_cab_ok_row():
    run = FakeRun(state="SUCCEEDED")
    ch, bq = chain(facts(s2=None), (4, 30), run=run)
    out = ch.tick()
    assert out["waiting_on"] == "S2" and run.calls == []                      # no S2 OK -> letter_fields not started
    run2 = FakeRun()
    ch2, _ = chain(facts(), (4, 30), run=run2)
    out2 = ch2.tick()
    assert out2["waiting_on"] == "S3" and run2.calls == [("tiktik-letter-fields", None, {})]
    held = facts(s3={"run_id": "lf-1", "run_ts": z(4, 31), "status": "HELD", "cab_ok": "false"})
    c = T.Cfg({}, dt.datetime.fromisoformat(z(5, 0)))
    assert not T.verdict_s3(held, c, dt.datetime.fromisoformat(z(4, 20)))[0]
    okrow = facts(s3ok={"run_id": "lf-2", "run_ts": z(4, 40), "finished_at": z(4, 49)})
    assert T.verdict_s3(okrow, c, dt.datetime.fromisoformat(z(4, 20)))[0]
    assert not T.verdict_s3(okrow, c, dt.datetime.fromisoformat(z(4, 45)))[0]   # built before the cab row: not OK


def test_s3_waits_for_todays_plan():
    run = FakeRun()
    ch, bq = chain(facts(plan_rows=0), (4, 30), run=run)
    ch.tick()
    assert run.calls == [] and "no shadow_send_plan rows" in bq.log["S3"]["_sql"]


def test_s4_starts_flow_and_ak_tuesday_with_chain_clock():
    f = facts(s3ok={"run_id": "lf-2", "run_ts": z(4, 40), "finished_at": z(4, 49)})
    run = FakeRun()
    ch, _ = chain(f, (5, 0), run=run)
    assert ch.tick()["waiting_on"] == "S4"
    jobs = {(j, e.get("BLOCKS_PART")) for j, a, e in run.calls}
    assert jobs == {("tiktik-ak-writer", None), ("tiktik-ak-writer", "ak_tuesday")}
    tue = [e for j, a, e in run.calls if e.get("BLOCKS_PART") == "ak_tuesday"][0]
    assert tue["BLOCKS_TUE_KLUDA_AT"] == "08:45" and tue["BLOCKS_TUE_LATEST"] == "08:45" and "BLOCKS_WRITE" not in tue


def test_s4_refreshes_mirror_then_assembly_first():
    base = dict(s3ok={"run_id": "lf-2", "run_ts": z(4, 40), "finished_at": z(4, 49)})
    run = FakeRun()
    ch, _ = chain(facts(mirror_at=z(3, 0), **base), (5, 0), run=run)
    ch.tick()
    assert ("mozello-products-ingest", None, {}) in run.calls
    run2 = FakeRun()
    ch2, _ = chain(facts(mirror_at=z(4, 0), assembled_at=z(3, 59), **base), (5, 0), run=run2)
    ch2.tick()
    assert any(j == "tiktik-shadow-selfcheck-dash" and "akcija_assembly.py --record" in a[1] for j, a, e in run2.calls)


def test_s4_ok_needs_both_builds_after_s3():
    c = T.Cfg({}, dt.datetime.fromisoformat(z(6, 0)))
    s3 = dt.datetime.fromisoformat(z(4, 49))
    good = facts(flow={"run_id": "blk-1", "status": "OK", "errors": 0, "finished_at": z(4, 55)},
                 tue_built={"run_id": "blk-2", "logged_at": z(4, 58)})
    assert T.verdict_s4(good, c, s3)[0]
    assert not T.verdict_s4(facts(flow={"run_id": "blk-1", "status": "OK", "errors": 0, "finished_at": z(4, 40)},
                                  tue_built={"run_id": "blk-2", "logged_at": z(4, 58)}), c, s3)[0]
    assert not T.verdict_s4(facts(flow={"run_id": "blk-1", "status": "OK", "errors": 0, "finished_at": z(4, 55)},
                                  tue_built={"run_id": "blk-2", "logged_at": z(4, 40)}), c, s3)[0]


def ready():
    return facts(s3ok={"run_id": "lf-2", "run_ts": z(4, 40), "finished_at": z(4, 49)},
                 flow={"run_id": "blk-1", "status": "OK", "errors": 0, "finished_at": z(4, 55)},
                 tue_built={"run_id": "blk-2", "logged_at": z(4, 58)})


def test_s5_disabled_until_dry_green_then_not_before_0900():
    run = FakeRun()
    ch, bq = chain(ready(), (5, 10), run=run)
    assert ch.tick()["steps"]["S5"] == "DISABLED" and run.calls == []
    ch2, _ = chain(ready(), (5, 10), env={"CHAIN_S5_ENABLED": "1"}, run=run)
    assert ch2.tick()["steps"]["S5"] == "WAITING until 09:00" and run.calls == []
    ch3, _ = chain(ready(), (9, 0), env={"CHAIN_S5_ENABLED": "1"}, run=run)
    assert ch3.tick()["steps"]["S5"] == "STARTED" and run.calls == [("tiktik-send-day", None, {})]
    ch4, _ = chain(ready(), (7, 0), env={"CHAIN_S5_ENABLED": "1", "CHAIN_S5_NOT_BEFORE": "07:00"}, run=FakeRun())
    assert ch4.tick()["steps"]["S5"] == "STARTED"


def test_deadline_alerts_once_and_nothing_starts():
    alerts, run = [], FakeRun()
    ch, bq = chain(facts(s1r={"n": 1411, "bad": 102, "other_mode": 0}), (8, 50), run=run, alerts=alerts)
    out = ch.tick()
    assert out["waiting_on"] == "S1" and run.calls == [] and len(alerts) == 1
    assert "S1 nav OK līdz 08:45" in alerts[0] and "102 rows MOZ_FAIL/ERROR" in alerts[0] and "Nekas netiek sūtīts" in alerts[0]
    assert "NOT_STARTED" in bq.log["S5"]["_sql"]


def test_s4_late_means_no_send():
    late = ready()
    late["tue_built"] = {"run_id": "blk-2", "logged_at": z(8, 50)}
    alerts, run = [], FakeRun()
    ch, _ = chain(late, (9, 0), env={"CHAIN_S5_ENABLED": "1"}, run=run, alerts=alerts)
    assert ch.tick()["steps"]["S5"] == "DEADLINE" and run.calls == [] and alerts


def test_dry_mode_runs_dry_everywhere():
    env = {"CHAIN_MODE": "DRY", "CHAIN_DATE": "2026-10-19", "CHAIN_S3_NOT_BEFORE": "09:30", "CHAIN_DEADLINE": "11:45",
           "CHAIN_S5_NOT_BEFORE": "00:00"}
    c = T.Cfg(env, dt.datetime(2026, 10, 19, 7, 0, tzinfo=U))
    assert c.mode == "DRY" and c.date == dt.date(2026, 10, 19) and c.cab == "cab-20261019" and c.deadline == dt.time(11, 45)
    assert "mode = 'DRY'" in T.facts_sql(c)
    assert T.Cfg({"CHAIN_DEADLINE": "11:45"}, dt.datetime(2026, 10, 20, 7, 0, tzinfo=U)).deadline == dt.time(8, 45)  # LIVE never moves it
    run = FakeRun()
    f = facts(s1=None)
    ch = T.Chain(FakeBQ(f), run, lambda t: True, env, now=dt.datetime(2026, 10, 19, 0, 31, tzinfo=U))
    ch.tick()
    assert run.calls == [("tiktik-week-prices", ["--dry"], {})]


def test_first_date_guard_and_dry_s3_must_be_chain_started():
    ch, _ = chain(facts(), (5, 0), env={"CHAIN_FIRST_DATE": "2026-10-27"})
    assert "chain starts on 2026-10-27" in ch.tick()["skip"]
    c = T.Cfg({"CHAIN_MODE": "DRY"}, dt.datetime.fromisoformat(z(10, 0)))
    daily = facts(s3ok={"run_id": "lf-0840", "run_ts": z(8, 40), "finished_at": z(8, 48)})
    s2 = dt.datetime.fromisoformat(z(5, 50))
    assert not T.verdict_s3(daily, c, s2, min_ts=dt.datetime.fromisoformat(z(9, 30)))[0]
    assert T.verdict_s3(daily, c, s2, min_ts=dt.datetime.fromisoformat(z(8, 30)))[0]
