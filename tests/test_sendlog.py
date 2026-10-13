"""Nightly send_log import + planner gates SEND_LOG_STALE / CAMPAIGN_UNREVIEWED (MAIN 2026-10-08 17:45)."""
import datetime as dt
import os
import sys
import zoneinfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sendlog_sync as SL  # noqa: E402
import sequence as S  # noqa: E402

RIGA = zoneinfo.ZoneInfo("Europe/Riga")
NOW = dt.datetime(2026, 10, 9, 4, 30, tzinfo=RIGA)


class FakeQ:
    def __init__(self, new=(), counts=None, first_send=None):
        self.new, self.counts, self.first_send, self.calls = list(new), counts or {}, first_send, []

    def __call__(self, sql, **p):
        self.calls.append((sql, p))
        if sql == SL.NEW_SQL:
            return [{"c": c} for c in self.new]
        if sql == SL.COUNT_SQL:
            return [{"n": self.counts.get(int(p["c"]), 0)}]
        if sql == SL.FIRST_SEND_SQL:
            return [{"t": self.first_send}]
        return []

    def of(self, sql):
        return [p for s, p in self.calls if s == sql]

    def state(self):
        return [(s, p) for s, p in self.calls if s.startswith(f"INSERT INTO `{SL.T_STATE}`")]


def camp(sent=100, lists=(42,), date="2026-10-09T07:00:00.000+00:00"):
    return {"name": "X", "sentDate": date, "recipients": {"lists": list(lists)},
            "statistics": {"globalStats": {"sent": sent}}}


def test_nothing_new_is_ok_and_writes_one_state_row():
    q = FakeQ()
    gets = []
    assert SL.run(q, lambda p: gets.append(p) or {}, lambda q_: 0, NOW) == 0
    assert gets == []
    (sql, p), = q.state()
    assert "TRUE" in sql and p["run"] == "sendlog-20261009" and p["nc"] == "0"


def test_new_campaign_gets_unclassified_class_and_rows_one_get_per_id():
    q = FakeQ(new=[300], counts={300: 100})
    gets = []
    assert SL.run(q, lambda p: gets.append(p) or camp(), lambda q_: 0, NOW) == 0
    assert gets == ["/emailCampaigns/300"]
    (c,), (r,) = q.of(SL.CLASS_SQL), q.of(SL.ROWS_SQL)
    assert c["c"] == "300" and c["by"] == "sendlog sendlog-20261009"
    assert "'unclassified'" in SL.CLASS_SQL and "FALSE" in SL.CLASS_SQL
    assert r["run"] == "sendlog-20261009" and r["lst"] == "42" and r["sent"].startswith("2026-10-09T07")


def test_rows_far_from_brevo_sent_is_not_ok():
    q = FakeQ(new=[300], counts={300: 80})
    assert SL.run(q, lambda p: camp(sent=100), lambda q_: 0, NOW) == 1
    assert "FALSE" in q.state()[0][0]


def test_small_difference_is_tolerated():
    assert SL.within(5689, 5680) and SL.within(8097, 8102) and not SL.within(80, 100)
    assert not SL.within(5, None)


def test_history_not_complete_is_not_ok():
    q = FakeQ()
    assert SL.run(q, lambda p: {}, lambda q_: 1, NOW) == 1


def test_history_crash_is_not_ok():
    def boom(q_):
        raise RuntimeError("brevo down")
    q = FakeQ()
    assert SL.run(q, lambda p: {}, boom, NOW) == 1


def test_brevo_get_error_is_not_ok_and_no_rows():
    def bad(p):
        raise RuntimeError("429")
    q = FakeQ(new=[300])
    assert SL.run(q, bad, lambda q_: 0, NOW) == 1
    assert q.of(SL.ROWS_SQL) == []


def test_send_time_falls_back_to_history_send_local():
    q = FakeQ(new=[300], counts={300: 100}, first_send="2026-10-09T06:00:00Z")
    c = camp(date=None)
    assert SL.run(q, lambda p: c, lambda q_: 0, NOW) == 0
    assert q.of(SL.ROWS_SQL)[0]["sent"] == "2026-10-09T06:00:00Z"


def test_campaign_stats_sum_when_no_global():
    assert SL.brevo_sent({"statistics": {"campaignStats": [{"sent": 3}, {"sent": 4}]}}) == 7
    assert SL.brevo_sent({}) is None


# ---- planner gates
T = dt.date(2026, 10, 9)


def test_send_log_stale_boundaries():
    assert S.send_log_stale(None, T)
    assert not S.send_log_stale(T, T)
    assert not S.send_log_stale(T - dt.timedelta(days=1), T)           # yesterday is still fresh
    assert S.send_log_stale(T - dt.timedelta(days=2), T)               # older than yesterday


def test_sendlog_hold_only_reactivation_and_only_would_send():
    assert S.sendlog_hold("winback_1", None, True, False) == S.HOLD_SEND_LOG_STALE
    assert S.sendlog_hold("reorder_1", None, False, True) == S.HOLD_CAMPAIGN_UNREVIEWED
    assert S.sendlog_hold("winback_1", None, True, True) == S.HOLD_SEND_LOG_STALE
    assert S.sendlog_hold("winback_1", "EN_PENDING", True, True) == "EN_PENDING"
    assert S.PP1 not in S.REACTIVATION_TYPES
    assert S.sendlog_hold(S.PP1, None, True, True) is None
    assert S.sendlog_hold(None, None, True, True) is None
    assert S.sendlog_hold("winback_2", None, False, False) is None
    for t in S.REACTIVATION_TYPES:
        assert S.sendlog_hold(t, None, True, False) == S.HOLD_SEND_LOG_STALE


def test_edu_itself_never_writes_send_log():
    code = open(os.path.join(os.path.dirname(__file__), "..", "edu.py"), encoding="utf-8").read()
    assert "sendlog_sync" not in code
    src = open(os.path.join(os.path.dirname(__file__), "..", "sendlog_sync.py"), encoding="utf-8").read()
    assert '"POST"' not in src and "sendNow" not in src and "/smtp/" not in src      # GET only, sends nothing
