"""PLAN = ASSIGNMENT v1 (contract 61e3f7f95f3a, PA1-PA4)."""
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import selfcheck as SC  # noqa: E402
import send_lookups as L  # noqa: E402
import sequence as S  # noqa: E402

J = L._job()
D = dt.date(2026, 10, 6)


class Holds(unittest.TestCase):
    def test_pa1_person_level_keeps_other_person_reasons(self):
        self.assertEqual(S.buyer_hold("lost_quarterly", None, False), S.HOLD_NOT_BUYER)
        self.assertEqual(S.buyer_hold(None, None, False), S.HOLD_NOT_BUYER)                 # also with no letter planned
        self.assertEqual(S.buyer_hold("reorder_1", "SEQUENCE_DONE", False), S.HOLD_NOT_BUYER)
        for keep in ("SUPPRESSED", "BLOCKED_OR_UNKNOWN", S.HOLD_B2B, S.HOLD_LEAD, S.HOLD_EN):
            self.assertEqual(S.buyer_hold("reorder_1", keep, False), keep)
        self.assertIsNone(S.buyer_hold("reorder_1", None, True))
        self.assertIn(S.HOLD_NOT_BUYER, S.PERSON_HOLDS)
        self.assertNotIn(S.HOLD_RULE8A, S.PERSON_HOLDS)                                     # PA2: stays in the akcija

    def test_pa2_only_179_180_233_234_and_only_a_letter_that_would_go(self):
        for et in ("reorder_1", "winback_1", "winback_2", "winback_3", "winback_1_e2", "lost_quarterly"):
            self.assertEqual(S.rule8a_hold(et, None, False), S.HOLD_RULE8A, et)
            self.assertIsNone(S.rule8a_hold(et, None, True), et)
        for et in (S.XSELL, S.PP1, None):
            self.assertIsNone(S.rule8a_hold(et, None, False), et)
        self.assertEqual(S.rule8a_hold("winback_1", S.HOLD_B2B, False), S.HOLD_B2B)

    def test_sources_are_read_as_the_assignment_reads_them(self):
        self.assertIn("business_marts.tiktik_buyer_master` WHERE is_tiktik_buyer", J.BUYER_SQL)
        self.assertIn("business_marts.marketing_brevo_attrs", J.KAB_SQL)
        self.assertIn("KABINETS_HAS_PRODUCTS IS TRUE", J.KAB_SQL)
        self.assertIn("layer = 'commercial'", J.ASSIGN_SQL)
        src = open(os.path.join(ROOT, "sequence_job.py")).read()
        self.assertLess(src.index("hold = S.language_hold("), src.index("hold = S.buyer_hold("))
        self.assertLess(src.index("hold = S.buyer_hold("), src.index("hold = S.rule8a_hold("))
        self.assertLess(src.index("hold = S.rule8a_hold("), src.index("would = hold is None"))
        self.assertIn("refusing to plan", src[src.index("kab_ok = {"):src.index("kab_ok = {") + 400])

    def test_akcija_pa1_out_pa2_in(self):
        week = J.akcija_week(D)
        base = dict(mk="m", email="a@x.lv", stage="lost", suppressed=False, flow=None, is_en=False, week=week)
        held = {"email_type": "lost_quarterly", "would_deliver": False, "planned_send_date": None}
        r = J.akcija_row(**base, plan=held, is_buyer=False)
        self.assertEqual((r["in_audience"], r["excluded_reason"]), (False, S.HOLD_NOT_BUYER))
        self.assertTrue(J.akcija_row(**base, plan=held, is_buyer=True)["in_audience"])      # the rule 8a person
        self.assertEqual(J.akcija_row(**{**base, "flow": "B2B"}, plan=held, is_buyer=False)["excluded_reason"], S.HOLD_B2B)


class PA3SelfCheck(unittest.TestCase):
    def run_(self, rows, **k):
        base = dict(prev_counts=[], flows={}, en_masters=set(), suppressed_send_address=0, suppressed_any_address=0,
                    today=D, ages_h={}, no_letter_fields={}, map_disagreements=[])
        base.update(k)
        return {c["check_name"]: c for c in SC.run(rows, [], **base)}

    @staticmethod
    def row(mk, et, due="2026-10-06", ws=True):
        return {"master_key": mk, "email_type": et, "planned_send_date": due, "would_send": ws, "would_deliver": False,
                "offer_rung": 0, "template_id": 1, "hold_reason": None, "presend_gate": None, "diff_vs_prev": "same"}

    def test_counts_only_due_179_180_234_that_would_send(self):
        rows = [self.row("a", "lost_quarterly"), self.row("b", "lost_quarterly"), self.row("c", "winback_1"),
                self.row("d", "reorder_1"), self.row("e", S.XSELL), self.row("f", "lost_quarterly", due="2026-10-09"),
                self.row("g", "winback_1", ws=False), self.row("h", "winback_2")]
        asg = {"a": "lost_quarterly", "b": "akcija_weekly", "d": "reorder_1", "e": "akcija_weekly", "f": "akcija_weekly",
               "g": "akcija_weekly", "h": "winback_1"}
        cs = self.run_(rows, buyers={"a", "b", "c", "d", "e", "f", "h"}, assignment=asg,
                       priced_type={"a": "winback_1", "d": "reorder_1"})
        c = cs["plan_equals_assignment_due_179_180_234"]
        self.assertEqual((c["level"], c["ok"], c["value"]), ("warn", False, "2"))           # b (akcija) and c (missing)
        self.assertIn('"lost_quarterly | akcija_weekly": 1', c["detail"])
        self.assertIn('"winback_1 | (not in assignment)": 1', c["detail"])
        self.assertEqual(cs["pa4_price_lag_due_today"]["value"], "1")                       # a: priced as winback_1
        self.assertTrue(cs["not_tiktik_buyer_among_would_send"]["ok"])
        cs = self.run_([self.row("a", "lost_quarterly")], buyers=set(), assignment={"a": "lost_quarterly"})
        self.assertTrue(cs["plan_equals_assignment_due_179_180_234"]["ok"])
        self.assertFalse(cs["not_tiktik_buyer_among_would_send"]["ok"])                     # hard

    def test_old_callers_without_the_new_inputs_still_work(self):
        cs = self.run_([self.row("a", "lost_quarterly")])
        self.assertNotIn("plan_equals_assignment_due_179_180_234", cs)


if __name__ == "__main__":
    unittest.main()
