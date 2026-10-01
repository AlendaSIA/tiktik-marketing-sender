"""G15 / G15.1 INTERFACE (MAIN 2026-10-01 16:35, contract sha 95d569b22d9a). Standard library only."""
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import sequence as S  # noqa: E402
from tests.test_cadence_v1 import _job, F  # noqa: E402

D = dt.date
PRICE_LETTERS = [("winback_1", 1), ("winback_1_e2", 1), ("winback_2", 2), ("winback_2_e2", 2),
                 ("winback_3", 3), ("winback_3_e2", 3), ("lost_quarterly", 1), ("lost_quarterly", 3)]


class G15(unittest.TestCase):
    def test_zero_priced_holds_every_price_letter(self):
        for et, r in PRICE_LETTERS:
            self.assertEqual(S.goods_hold(et, r, None, (r, True)), "NO_PRICED_SLOTS", et)

    def test_priced_goods_pass(self):
        for et, r in PRICE_LETTERS:
            self.assertIsNone(S.goods_hold(et, r, None, (r, False)), et)

    def test_no_row_is_held_never_a_fallback(self):
        for et, r in PRICE_LETTERS:
            self.assertEqual(S.goods_hold(et, r, None, None), "NO_SLOT_ROW", et)

    def test_row_for_another_rung_is_not_this_letters_goods(self):
        self.assertEqual(S.goods_hold("winback_2", 2, None, (1, False)), "NO_SLOT_ROW")

    def test_non_price_letters_untouched(self):
        for et in ("reorder_1", "welcome_1", "active_xsell"):
            self.assertIsNone(S.goods_hold(et, 0, None, None), et)
        self.assertIsNone(S.goods_hold("winback_1", 0, None, None))       # rung 0 = no price letter

    def test_template_gap_is_overridden_planner_holds_stay(self):
        self.assertEqual(S.goods_hold("lost_quarterly", 1, "NO_TEMPLATE_IN_MAP", (1, True)), "NO_PRICED_SLOTS")
        self.assertEqual(S.goods_hold("winback_1_e2", 1, "E2_TEMPLATE_PENDING", None), "NO_SLOT_ROW")
        for hold in ("SUPPRESSED", "no_offer_valid_until", "BLOCKED_OR_UNKNOWN"):
            self.assertEqual(S.goods_hold("winback_2", 2, hold, (2, True)), hold)

    def test_rung_does_not_advance_when_held(self):
        st = S.State("g15", "winback", D(2026, 9, 25), 1, 1, D(2026, 9, 22), "2026-09", None, "winback_1", D(2026, 9, 22))
        d = S.advance(st, F("winback"), D(2026, 11, 3))
        self.assertEqual(S.goods_hold(d.next_email_type, d.offer_rung, d.hold_reason, (2, True)), "NO_PRICED_SLOTS")
        d2 = S.advance(d.state, F("winback"), D(2026, 11, 4))            # held -> never record_sent
        self.assertEqual((d2.next_email_type, d2.offer_rung, d2.state.rung, d2.state.rung_month, d2.state.step),
                         ("winback_2", 2, 1, "2026-09", 1))

    def test_planner_reports_only_from_latest_available_run(self):
        # G15.2: the 08:05 planner reads the latest AVAILABLE goods run and never changes a hold or would_send
        J = _job()
        self.assertIn("mkt_control.shadow_rung_goods_slots", J.T_GOODS)
        self.assertIn("ORDER BY built_at DESC LIMIT 1", J.GOODS_LATEST)
        self.assertNotIn("plan_date", J.GOODS_LATEST)
        self.assertIn("LOGICAL_OR(g15_zero_priced)", J.GOODS_SQL)
        src = open(os.path.join(ROOT, "sequence_job.py")).read()
        self.assertIn("g15 = S.goods_hold(d.next_email_type, d.offer_rung, hold, goods.get(mk))", src)
        self.assertNotIn("hold = S.goods_hold(", src)
        self.assertIn('"g15_would_be_no_priced"', src)
        self.assertIn('"g15_would_be_no_slot_row"', src)


if __name__ == "__main__":
    unittest.main()
