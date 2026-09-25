"""Pins MAIN 2026-09-25, F5, verbatim: "APPROVED: 232–234 send only with P1_NAME filled; 235 only
with R1_NAME filled."

The gate lives in bq.PLAN_SQL because that is the ONE definition of who may go: its SEND rows are
what main.step4b freezes into the planned snapshot, and the day batch's variant lists read that
snapshot. What is pinned here:
  - both gate lines stand in BOTH ladders, after every template check and before NOT_IN_LIFECYCLE
    and the final ELSE;
  - the template ids come from config.SLOT_GATES and are written nowhere else in the SQL;
  - the slots CTE is aggregated by the join key, so the LEFT JOIN can never multiply a plan row;
  - main.step4_plan counts the gated rows in its PLAN log line.
The SQL itself was dry-run and executed read-only against BigQuery on 2026-09-25 (9 935 plan rows,
the same count as without the gate); this file keeps its shape from drifting. Standard library only.
"""
import datetime as dt
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

import bq  # noqa: E402
import config as C  # noqa: E402

TEMPLATE_CHECKS = ("'NO_TEMPLATE'", "'TEMPLATE_NOT_SENDABLE'", "'TEMPLATE_STATUS_UNKNOWN'",
                   "'TEMPLATE_STATUS_STALE'", "'TEMPLATE_INACTIVE_IN_BREVO'")
GATE_P1 = ("WHEN template_id IN (232, 233, 234) AND p1_filled IS NOT TRUE",
           "THEN 'SLOT_GATE_P1_EMPTY'")
GATE_R1 = ("WHEN template_id IN (235) AND r1_filled IS NOT TRUE", "THEN 'SLOT_GATE_R1_EMPTY'")
_GATE_LINE = re.compile(
    r"WHEN template_id IN \(([0-9, ]+)\) AND ([a-z0-9_]+)_filled IS NOT TRUE\s+"
    r"THEN 'SLOT_GATE_([A-Z0-9_]+)_EMPTY'")


def _ladders():
    """(decision ladder, decision_if_enabled ladder) - the two CASE blocks of PLAN_SQL."""
    sql = bq.PLAN_SQL
    tail = sql[sql.index("SELECT *,\n  CASE"):]
    first = tail[:tail.index("END AS decision,")]
    rest = tail[tail.index("END AS decision,"):]
    second = rest[rest.index("CASE"):rest.index("END AS decision_if_enabled")]
    return first, second


class GatesStandInBothLadders(unittest.TestCase):

    def test_config_is_mains_f5(self):
        self.assertEqual(C.SLOT_GATES, {232: "P1_NAME", 233: "P1_NAME", 234: "P1_NAME",
                                        235: "R1_NAME"})

    def test_both_gates_in_both_ladders_in_the_right_place(self):
        for name, ladder in zip(("decision", "decision_if_enabled"), _ladders()):
            with self.subTest(ladder=name):
                pos = {}
                for gate in (GATE_P1, GATE_R1):
                    self.assertEqual(ladder.count(gate[0]), 1, gate[0])
                    line = ladder[ladder.index(gate[0]):].split("\n", 1)[0]
                    self.assertIn(gate[1], line, "the WHEN and its THEN are one line")
                    pos[gate[1]] = ladder.index(gate[0])
                last_template_check = max(ladder.index(t) for t in TEMPLATE_CHECKS)
                not_in_lifecycle = ladder.index("'NOT_IN_LIFECYCLE'")
                final_else = ladder.index("ELSE")
                for gate_pos in pos.values():
                    self.assertLess(last_template_check, gate_pos)
                    self.assertLess(gate_pos, not_in_lifecycle)
                    self.assertLess(gate_pos, final_else)

    def test_each_gate_is_written_exactly_twice(self):
        for text in (GATE_P1[0], GATE_R1[0], "'SLOT_GATE_P1_EMPTY'", "'SLOT_GATE_R1_EMPTY'"):
            self.assertEqual(bq.PLAN_SQL.count(text), 2, text)


class IdsComeFromConfig(unittest.TestCase):

    def test_plan_gates_are_exactly_the_config_map(self):
        """Read the map BACK out of the SQL and compare it with config: nothing added or lost."""
        slot_field = dict((col, field) for field, col in re.findall(
            r"LOGICAL_AND\(IFNULL\(TRIM\(([A-Z0-9_]+)\), ''\) != ''\) AS ([a-z0-9_]+)_filled",
            bq.PLAN_SQL))
        first, _ = _ladders()
        found = {}
        for ids, col, slot in _GATE_LINE.findall(first):
            self.assertEqual(col, slot.lower())
            for tid in ids.split(","):
                found[int(tid)] = slot_field[col]
        self.assertEqual(found, C.SLOT_GATES)

    def test_generated_fragments_are_the_ones_in_the_plan(self):
        frag = bq.slot_gate_sql(C.SLOT_GATES)
        self.assertEqual(bq.PLAN_SQL.count(frag["whens"]), 2)
        self.assertEqual(bq.PLAN_SQL.count(frag["columns"]), 1)
        self.assertEqual(bq.PLAN_SQL.count(frag["joined"]), 1)

    def test_template_ids_are_written_nowhere_else_in_the_sql(self):
        for tid in C.SLOT_GATES:
            self.assertEqual(len(re.findall(rf"\b{tid}\b", bq.PLAN_SQL)), 2, tid)

    def test_another_field_gets_its_own_decision(self):
        frag = bq.slot_gate_sql({999: "D1_URL", 998: "P2_NAME", 997: "P2_NAME"})
        self.assertIn("LOGICAL_AND(IFNULL(TRIM(D1_URL), '') != '') AS d1_url_filled",
                      frag["columns"])
        self.assertRegex(frag["whens"], r"WHEN template_id IN \(999\) AND d1_url_filled IS NOT TRUE"
                                        r"\s+THEN 'SLOT_GATE_D1_URL_EMPTY'")
        self.assertRegex(frag["whens"], r"WHEN template_id IN \(997, 998\) AND p2_filled IS NOT "
                                        r"TRUE\s+THEN 'SLOT_GATE_P2_EMPTY'")
        self.assertIn("sl.d1_url_filled", frag["joined"])

    def test_no_gates_means_no_fragments(self):
        self.assertEqual(bq.slot_gate_sql({}), {"columns": "", "joined": "", "whens": ""})

    def test_a_malformed_gate_is_refused_before_it_reaches_sql(self):
        for gates in ({"232": "P1_NAME"}, {232: "p1_name"}, {232: "P1_NAME) OR (1=1"},
                      {True: "P1_NAME"}):
            with self.subTest(gates=gates):
                with self.assertRaises(ValueError):
                    bq.slot_gate_sql(gates)


class SlotsAreOneRowPerEmail(unittest.TestCase):

    def _cte(self):
        m = re.search(r"\nslots AS \((.*?)\n\),", bq.PLAN_SQL, re.S)
        self.assertIsNotNone(m, "no slots CTE in PLAN_SQL")
        return m.group(1)

    def test_slots_cte_is_aggregated_by_email(self):
        cte = self._cte()
        self.assertIn(f"FROM {C.T_BREVO_ATTRS}", cte)
        self.assertIn("GROUP BY LOWER(TRIM(email))", cte)
        select = cte[cte.index("SELECT") + len("SELECT"):cte.index("FROM")]
        items = [s.strip() for s in select.split(",\n")]
        self.assertEqual(items[0], "LOWER(TRIM(email)) AS email")
        # Every other column is an aggregate, so the CTE holds ONE row per LOWER(TRIM(email)).
        for item in items[1:]:
            self.assertTrue(item.startswith("LOGICAL_AND("), item)

    def test_joined_on_that_key_and_left(self):
        self.assertIn("LEFT JOIN slots sl ON sl.email = a.email", bq.PLAN_SQL)
        self.assertNotRegex(bq.PLAN_SQL, r"(?<!LEFT )JOIN slots")

    def test_table_name(self):
        self.assertEqual(C.T_BREVO_ATTRS, f"`{C.PROJECT}.{C.MARTS}.marketing_brevo_attrs`")


class PlanLogCountsTheGate(unittest.TestCase):

    def setUp(self):
        import main
        self.main = main
        self._saved = (bq.table_exists, bq.build_plan, bq.write_send_plan)
        bq.table_exists = lambda t: True
        bq.write_send_plan = lambda run_id, rows: len(rows)

    def tearDown(self):
        bq.table_exists, bq.build_plan, bq.write_send_plan = self._saved

    @staticmethod
    def _row(mk, decision, if_enabled):
        return {"week_start": None, "master_key": mk, "email": f"{mk}@x.lv", "layer": "commercial",
                "send_date": dt.date(2026, 9, 29), "track": "t", "email_type": "winback_2",
                "template_id": 232, "decision": decision, "decision_if_enabled": if_enabled,
                "lifecycle_stage": "lapsed", "full_name": "N", "gender_greeting": "",
                "language": "lv", "hero_product_name": None, "hero_product_url": None,
                "hero_product_image": None}

    def test_slot_gated_rows_are_counted_on_either_column(self):
        bq.build_plan = lambda: [
            self._row("m1", "TRACK_OFF", "SLOT_GATE_P1_EMPTY"),       # hidden by the switch
            self._row("m2", "SLOT_GATE_R1_EMPTY", "SLOT_GATE_R1_EMPTY"),  # one row, counted once
            self._row("m3", "SEND", "READY"),
        ]
        with self.assertLogs("sender", level="INFO") as logs:
            plan = self.main.step4_plan()
        self.assertEqual(len(plan), 3)
        line = [m for m in logs.output if m.split(":", 2)[2].startswith("PLAN rows=")][0]
        self.assertIn("slot_gated=2", line)
        self.assertIn("send=1", line)


if __name__ == "__main__":
    unittest.main()
