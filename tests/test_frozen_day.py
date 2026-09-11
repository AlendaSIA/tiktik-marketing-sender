"""Pins MAIN's frozen-day decision of 2026-09-11 (variant b).

  a) the planner's DELETE and INSERT skip every send_date that already has a day_batch;
  b) build_id is the identity of ONE send_date's planned rows, frozen when the batch is built;
  c) AUDIENCE_CHANGED compares that frozen day hash with the same day's planned rows now;
  d) contract A keeps its form - build_id stays an opaque string for the relay.

The byte-level proof against real BigQuery SQL is tests/bqit_frozen_day.py (run by the build
against a scratch dataset); these are the fast, warehouse-free pins.
"""
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    from google.cloud import bigquery as _bq  # noqa: F401
except Exception:  # noqa: BLE001
    google = sys.modules.setdefault("google", types.ModuleType("google"))
    cloud = types.ModuleType("google.cloud")
    stub = types.ModuleType("google.cloud.bigquery")

    class _P:
        def __init__(self, name, typ, value):
            self.name, self.type_, self.value = name, typ, value

    stub.ScalarQueryParameter = _P
    stub.ArrayQueryParameter = _P
    stub.Client = object
    stub.QueryJobConfig = lambda **kw: kw
    cloud.bigquery = stub
    google.cloud = cloud
    sys.modules["google.cloud"] = cloud
    sys.modules["google.cloud.bigquery"] = stub

import batch  # noqa: E402
import bq  # noqa: E402
import press  # noqa: E402
import press_live as PL  # noqa: E402

FROZEN = "AND send_date NOT IN (SELECT send_date FROM"


def _pv(params, name):
    for p in params or []:
        if getattr(p, "name", None) == name:
            return getattr(p, "values", None) if hasattr(p, "values") else p.value
    raise KeyError(name)


class PlannerSkipsFrozenDays(unittest.TestCase):

    def test_delete_and_insert_both_skip_frozen_days(self):
        sql = bq.PLANNED_SNAPSHOT_SQL
        delete = sql[sql.index("DELETE FROM"):sql.index("INSERT INTO")]
        insert = sql[sql.index("INSERT INTO"):]
        self.assertIn(FROZEN, delete)
        self.assertIn("AND a.send_date NOT IN (SELECT send_date FROM", insert)
        for part in (delete, insert):
            self.assertIn("day_batch` WHERE send_date IS NOT NULL", part)

    def test_frozen_rows_are_dropped_before_the_write_and_the_count_matches(self):
        calls = []
        saved = (bq.query, bq.scalar)

        def fake_query(sql, params=None):
            calls.append((sql, params))
            if sql is bq.FROZEN_DAYS_SQL:
                return [{"d": "2026-09-08"}]
            return []
        bq.query = fake_query
        bq.scalar = lambda sql, params=None: 1
        try:
            n = bq.write_planned_snapshot(
                "s", ["m1", "m2"], ["u1", "u2"], layers=["commercial", "educational"],
                send_dates=["2026-09-08", "2026-09-10"])
        finally:
            bq.query, bq.scalar = saved
        self.assertEqual(n, 1)
        write = [p for s, p in calls if s is bq.PLANNED_SNAPSHOT_SQL][0]
        self.assertEqual(_pv(write, "master_keys"), ["m2"])
        self.assertEqual(_pv(write, "layers"), ["educational"])

    def test_send_dates_are_required(self):
        with self.assertRaises(RuntimeError):
            bq.write_planned_snapshot("s", ["m1"], ["u1"], layers=["commercial"])


class DayIdentity(unittest.TestCase):

    def test_day_hash_is_one_send_date_of_planned_rows_in_total_order(self):
        sql = bq.DAY_BUILD_ID_SQL
        self.assertIn("campaign_audience_snapshot", sql)
        self.assertIn("WHERE send_date = @d AND dispatch_state = 'planned'", sql)
        self.assertIn("FORMAT('%s|%s|%t', master_key, email_type, send_date)", sql)
        self.assertIn("ORDER BY master_key, email_type", sql)
        self.assertIn("IFNULL(STRING_AGG(", sql)  # empty day = md5(''), never NULL
        self.assertNotIn("contact_weekly_assignment", sql)

    def test_batch_and_press_use_the_day_identity(self):
        seen = []
        saved = (bq.day_build_id, bq.query, bq.scalar)
        bq.day_build_id = lambda d: seen.append(str(d)) or "H-" + str(d)
        bq.query = lambda sql, params=None: (
            [{"batch_id": "B", "send_date": "2026-09-15", "assignment_build_id": "H-2026-09-15",
              "audience_total": 3}] if "batch_id = @b" in sql else [])
        bq.scalar = lambda sql, params=None: 0
        try:
            v = PL.live_inputs("2026-09-15", 10, "B")
        finally:
            bq.day_build_id, bq.query, bq.scalar = saved
        self.assertEqual(v["live_build_id"], "H-2026-09-15")
        self.assertEqual(seen, ["2026-09-15"])
        src = open(os.path.join(ROOT, "batch.py"), encoding="utf-8").read()
        self.assertIn("build_id = bq.day_build_id(send_date)", src)
        self.assertNotIn("assignment_build_id()", src + open(
            os.path.join(ROOT, "press_live.py"), encoding="utf-8").read())

    def test_press_after_rebuild_passes_when_the_frozen_day_is_untouched(self):
        checks = press.evaluate(send_date="d", build_id_in_mail="H1", live_build_id="H1",
                                overlap_people=0, credits_available=5, credits_needed=3)
        self.assertTrue(press.may_press(checks))

    def test_audience_changed_fails_when_the_frozen_day_was_written(self):
        checks = press.evaluate(send_date="d", build_id_in_mail="H1", live_build_id="H2",
                                overlap_people=0, credits_available=5, credits_needed=3)
        self.assertEqual([c["id"] for c in checks if not c["passed"]], ["AUDIENCE_CHANGED"])


if __name__ == "__main__":
    unittest.main()


class EmptyFrozenDayIsNotFrozen(unittest.TestCase):
    """MAIN 2026-09-11 18:50: only a day whose frozen batch holds MORE THAN ZERO rows is frozen."""

    def test_one_fragment_requires_rows(self):
        self.assertIn("COALESCE(audience_total, 0) > 0", bq.FROZEN_DATES_SUBQUERY)
        self.assertIn("send_date IS NOT NULL", bq.FROZEN_DATES_SUBQUERY)

    def test_delete_insert_and_python_filter_share_it(self):
        sql = bq.PLANNED_SNAPSHOT_SQL
        delete = sql[sql.index("DELETE FROM"):sql.index("INSERT INTO")]
        insert = sql[sql.index("INSERT INTO"):]
        frag = f"NOT IN ({bq.FROZEN_DATES_SUBQUERY})"
        self.assertIn(frag, delete)
        self.assertIn(frag, insert)
        self.assertIn(bq.FROZEN_DATES_SUBQUERY, bq.FROZEN_DAYS_SQL)
        # no second, older spelling left anywhere
        self.assertNotIn("FROM {T_DAY_BATCH} WHERE send_date IS NOT NULL)".replace(
            "{T_DAY_BATCH}", bq.T_DAY_BATCH), sql)


class OneNameOneMeaning(unittest.TestCase):
    """MAIN 2026-09-11 18:50: the week hash is assignment_week_hash; build_id is only the day."""

    def _src(self, name):
        with open(os.path.join(ROOT, name), encoding="utf-8") as f:
            return f.read()

    def test_build_log_reads_and_writes_the_renamed_column(self):
        src = self._src("bq.py")
        self.assertIn("SELECT assignment_week_hash FROM {C.T_BUILD_LOG}", src)
        self.assertIn('"assignment_week_hash": week_hash', src)
        self.assertNotIn('"build_id": build_id', src)

    def test_run_report_key_is_renamed(self):
        src = self._src("main.py")
        self.assertIn('report["assignment_week_hash"] = step2b_assignment()', src)
        self.assertNotIn("assignment_build_id", src)

    def test_the_rename_ddl_is_on_record(self):
        ddl = self._src("sql/schema.sql")
        self.assertIn("RENAME COLUMN assignment_build_id TO assignment_week_hash", ddl)
        self.assertIn("RENAME COLUMN build_id TO assignment_week_hash", ddl)
