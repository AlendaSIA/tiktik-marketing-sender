"""Pins the LAYER predicate on every read of business_marts.contact_weekly_assignment.

The form, issued by MAIN on 2026-09-11 in the same words to every reader of the table:
  grain = (week_start, master_key, layer), layer IN ('commercial', 'educational');
  every reader filters layer explicitly; assignment rows are not people - people are
  COUNT(DISTINCT master_key); every writer of mkt_control.campaign_audience_snapshot fills
  layer, and NULL is not allowed in new rows.

Why a test and not a comment: the defect class this node keeps meeting is a correct predicate
removed by the next, unrelated fix, with no error anywhere. Each test below fails the moment one
predicate goes, and test_every_assignment_read_names_its_layer fails the moment somebody adds a
NEW read of the table without one.

Runs with the standard library only (python -m unittest discover -s tests). google.cloud.bigquery
is stubbed when it is not installed, because nothing here talks to BigQuery.
"""
import os
import re
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:  # the real library in the image, a stub on a bare interpreter
    from google.cloud import bigquery as _bq  # noqa: F401
except Exception:  # noqa: BLE001
    google = sys.modules.setdefault("google", types.ModuleType("google"))
    cloud = types.ModuleType("google.cloud")
    stub = types.ModuleType("google.cloud.bigquery")

    class _P:  # ScalarQueryParameter / ArrayQueryParameter stand-in
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

import bq  # noqa: E402
import config as C  # noqa: E402

PRED = "layer IN ('commercial', 'educational')"
TABLE = "contact_weekly_assignment"


def _param(params, name):
    for p in params or []:
        if getattr(p, "name", None) == name:
            # stub: .value · real ScalarQueryParameter: .value · real ArrayQueryParameter: .values
            return getattr(p, "values", None) if hasattr(p, "values") else p.value
    raise KeyError(name)


class AssignmentReadsNameTheirLayer(unittest.TestCase):

    def test_config_has_exactly_two_layers(self):
        self.assertEqual(C.LAYERS, ("commercial", "educational"))
        self.assertEqual(C.ALL_LAYERS_SQL, "('commercial', 'educational')")

    def test_every_assignment_read_names_its_layer(self):
        """Every module-level SQL string in bq.py that reads the assignment carries the predicate.

        The ONE exception is the invariant that COUNTS rows outside the two layers, which must
        not filter them away - it is checked separately below.
        """
        readers = {name: val for name, val in vars(bq).items()
                   if isinstance(val, str) and TABLE in val}
        self.assertTrue(readers, "found no SQL reading the assignment - the scan is broken")
        expected = {"ASSIGNMENT_UNKNOWN_LAYER_SQL", "ASSIGNMENT_LEAKED_SQL",
                    "ASSIGNMENT_ROWS_SQL", "PLAN_SQL", "PLANNED_SNAPSHOT_SQL",
                    "ASSIGNMENT_WEEK_HASH_SQL", "ASSIGNMENT_PEOPLE_SQL",
                    "DEFAULT_DAY_ROWS_SQL", "COVERAGE_SQL"}
        self.assertEqual(set(readers), expected,
                         "a read of contact_weekly_assignment was added or removed; give it an "
                         "explicit layer predicate and add it to this list")
        for name, sql in readers.items():
            if name == "ASSIGNMENT_UNKNOWN_LAYER_SQL":
                continue
            self.assertIn(PRED, sql, f"{name} reads the assignment without {PRED}")

    def test_no_inline_assignment_sql_left_in_functions(self):
        """Every f-string in bq.py that interpolates C.T_ASSIGNMENT is the value of a module-level
        *_SQL constant - so the scan in the previous test sees every read. A function that builds
        its own query against the table fails here."""
        import ast
        tree = ast.parse(open(os.path.join(ROOT, "bq.py"), encoding="utf-8").read())
        allowed = set()
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.JoinedStr):
                allowed.add(id(node.value))
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            uses = any(isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Attribute)
                       and v.value.attr == "T_ASSIGNMENT" for v in node.values)
            if uses:
                self.assertIn(id(node), allowed,
                              f"bq.py line {node.lineno}: an f-string reads C.T_ASSIGNMENT outside "
                              f"a module-level *_SQL constant")

    def test_other_modules_do_not_read_the_assignment(self):
        """Outside bq.py and config.py the table may only be probed for existence or named in a
        log line; any real read has to live in bq.py, where the scan above sees it."""
        for fn in os.listdir(ROOT):
            if not fn.endswith(".py") or fn in ("bq.py", "config.py"):
                continue
            with open(os.path.join(ROOT, fn), encoding="utf-8") as fh:
                lines = fh.readlines()
            for i, line in enumerate(lines, 1):
                if TABLE in line and not line.lstrip().startswith("#") and '"""' not in line:
                    if "T_ASSIGNMENT" not in line and fn != "main.py":
                        self.fail(f"{fn}:{i} names {TABLE} directly")
                if "T_ASSIGNMENT" in line:
                    ok = "table_exists(C.T_ASSIGNMENT)" in line or line.strip() == "C.T_ASSIGNMENT)"
                    self.assertTrue(ok, f"{fn}:{i} reads C.T_ASSIGNMENT outside bq.py: {line.strip()}")

    def test_unknown_layer_invariant_counts_what_the_filters_drop(self):
        sql = bq.ASSIGNMENT_UNKNOWN_LAYER_SQL
        self.assertIn("layer IS NULL", sql)
        self.assertIn("layer NOT IN ('commercial', 'educational')", sql)
        self.assertNotIn(PRED, sql)

    def test_week_hash_is_both_layers_with_a_total_order(self):
        sql = bq.ASSIGNMENT_WEEK_HASH_SQL
        self.assertIn("ORDER BY master_key, layer DESC", sql)
        self.assertIn("FORMAT('%s|%s|%t', master_key, email_type, send_date)", sql)
        self.assertIn(PRED, sql)

    def test_people_are_distinct_master_keys(self):
        self.assertIn("COUNT(DISTINCT master_key)", bq.ASSIGNMENT_PEOPLE_SQL)
        self.assertIn("(SELECT COUNT(DISTINCT master_key) FROM asg) AS assignment_people",
                      bq.COVERAGE_SQL)
        self.assertIn("COUNT(DISTINCT FORMAT('%s|%s', master_key, layer))", bq.COVERAGE_SQL)
        self.assertNotIn("(SELECT COUNT(*) FROM asg) AS assignment_people", bq.COVERAGE_SQL)

    def test_plan_carries_the_layer(self):
        self.assertIn("SELECT master_key, email, layer, track", bq.PLAN_SQL)

    def test_plan_does_not_read_the_removed_discount_columns(self):
        """MAIN, 2026-09-11: discount codes are cancelled and the lifecycle columns are gone.
        Reading them failed every nightly run from 10.09; nothing stands in for them."""
        self.assertNotIn("c.next_discount_pct", bq.PLAN_SQL)
        self.assertNotIn("c.next_discount_code", bq.PLAN_SQL)
        src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
        self.assertNotIn('p["next_discount', src)
        self.assertNotIn('r["next_discount', src)


class SnapshotWritersFillLayer(unittest.TestCase):

    def setUp(self):
        self.calls = []
        self._q, self._s, self._c = bq.query, bq.scalar, bq.client

    def tearDown(self):
        bq.query, bq.scalar, bq.client = self._q, self._s, self._c

    def test_planned_snapshot_sql_writes_and_joins_on_layer(self):
        sql = bq.PLANNED_SNAPSHOT_SQL
        self.assertIn("written_by, layer)", sql)
        self.assertIn("@written_by, a.layer", sql)
        self.assertIn("p.master_key = a.master_key AND p.layer = a.layer", sql)
        self.assertIn("UNNEST(@layers)", sql)

    def test_planned_snapshot_refuses_without_layers(self):
        with self.assertRaises(RuntimeError):
            bq.write_planned_snapshot("s1", ["m1"], ["u1"])
        with self.assertRaises(RuntimeError):
            bq.write_planned_snapshot("s1", ["m1"], ["u1"], layers=[None])
        with self.assertRaises(RuntimeError):
            bq.write_planned_snapshot("s1", ["m1"], ["u1"], layers=["loyalty"])
        with self.assertRaises(RuntimeError):
            bq.write_planned_snapshot("s1", ["m1", "m2"], ["u1", "u2"], layers=["commercial"])

    def test_planned_snapshot_passes_layers_paired_by_position(self):
        bq.query = lambda sql, params=None: self.calls.append((sql, params)) or []
        bq.scalar = lambda sql, params=None: 2
        n = bq.write_planned_snapshot("s1", ["m1", "m1"], ["u1", "u2"],
                                      layers=["commercial", "educational"],
                                      send_dates=["2026-09-08", "2026-09-10"])
        self.assertEqual(n, 2)
        sql, params = [c for c in self.calls if c[0] is bq.PLANNED_SNAPSHOT_SQL][0]
        self.assertEqual(_param(params, "layers"), ["commercial", "educational"])
        self.assertEqual(_param(params, "master_keys"), ["m1", "m1"])

    def test_audience_snapshot_refuses_a_row_without_layer(self):
        class _Client:
            def __init__(s):
                s.rows = None

            def insert_rows_json(s, table, rows):
                s.rows = rows
                return []
        cl = _Client()
        bq.client = lambda: cl
        row = {"built_at": "t", "week_start": "2026-09-07", "email_type": "reorder_1",
               "track": "reorder", "master_key": "m1", "email": "a@b.lv"}
        with self.assertRaises(RuntimeError):
            bq.write_audience_snapshot("s1", "2026-09-08", [dict(row)])
        with self.assertRaises(RuntimeError):
            bq.write_audience_snapshot("s1", "2026-09-08", [dict(row, layer=None)])
        self.assertIsNone(cl.rows, "a refused write must write nothing")
        bq.write_audience_snapshot("s1", "2026-09-08", [dict(row, layer="commercial")])
        self.assertEqual(cl.rows[0]["layer"], "commercial")


class BuildAssignmentRefusesAThirdLayer(unittest.TestCase):

    def setUp(self):
        self._q, self._s = bq.query, bq.scalar

    def tearDown(self):
        bq.query, bq.scalar = self._q, self._s

    def test_unknown_layer_fails_the_build(self):
        bq.query = lambda sql, params=None: []          # CALL + clean grain guard
        bq.scalar = lambda sql, params=None: 5 if sql is bq.ASSIGNMENT_UNKNOWN_LAYER_SQL else 0
        with self.assertRaises(RuntimeError) as e:
            bq.build_assignment()
        self.assertIn("not one of", str(e.exception))

    def test_clean_build_returns_rows(self):
        bq.query = lambda sql, params=None: []
        vals = {id(bq.ASSIGNMENT_UNKNOWN_LAYER_SQL): 0, id(bq.ASSIGNMENT_LEAKED_SQL): 0,
                id(bq.ASSIGNMENT_ROWS_SQL): 10047}
        bq.scalar = lambda sql, params=None: vals[id(sql)]
        self.assertEqual(bq.build_assignment(), 10047)


class PlanGuardIsPerLayer(unittest.TestCase):

    def setUp(self):
        import main
        self.main = main
        self._saved = (bq.table_exists, bq.build_plan, bq.write_send_plan)
        bq.table_exists = lambda t: True
        bq.write_send_plan = lambda run_id, rows: len(rows)

    def tearDown(self):
        bq.table_exists, bq.build_plan, bq.write_send_plan = self._saved

    @staticmethod
    def _row(mk, layer):
        import datetime as _dt
        return {"week_start": None, "master_key": mk, "email": f"{mk}@x.lv", "layer": layer,
                "send_date": _dt.date(2026, 9, 8),
                "track": "t", "email_type": f"e-{layer}", "template_id": 1, "decision": "SEND",
                "decision_if_enabled": "READY", "lifecycle_stage": "active", "full_name": "N",
                "gender_greeting": "", "language": "lv", "hero_product_name": None,
                "hero_product_url": None, "hero_product_image": None}

    def test_one_person_in_both_layers_is_allowed(self):
        bq.build_plan = lambda: [self._row("m1", "commercial"), self._row("m1", "educational")]
        self.assertEqual(len(self.main.step4_plan()), 2)

    def test_two_rows_in_one_layer_is_refused(self):
        bq.build_plan = lambda: [self._row("m1", "commercial"), self._row("m1", "commercial")]
        with self.assertRaises(self.main.GuardFailure):
            self.main.step4_plan()


if __name__ == "__main__":
    unittest.main()
