"""G-EN guard (Raivis 2026-09-30 17:47): an EN contact never receives an LV engine letter. Standard library only."""
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import sequence as S  # noqa: E402
from tests.test_cadence_v1 import _job, F  # noqa: E402

D = dt.date


class EnGuard(unittest.TestCase):
    def test_every_planned_letter_of_an_en_contact_is_held(self):
        for et, hold in (("reorder_1", None), ("winback_1", None), ("welcome_1", None),
                         ("winback_2", "no_offer_valid_until"), ("lost_quarterly", "NO_TEMPLATE_IN_MAP"),
                         ("winback_1_e2", "E2_TEMPLATE_PENDING")):
            self.assertEqual(S.language_hold(et, hold, True), "EN_PENDING", et)

    def test_lv_contact_unchanged(self):
        for hold in (None, "NO_TEMPLATE_IN_MAP", "SUPPRESSED"):
            self.assertEqual(S.language_hold("reorder_1", hold, False), hold)

    def test_no_letter_holds_keep_their_reason(self):
        for hold in S.EN_KEEPS:
            self.assertEqual(S.language_hold(None if hold != "SUPPRESSED" else "reorder_1", hold, True), hold)

    def test_state_does_not_advance(self):
        st = S.State("en1")
        d = S.advance(st, F("reorder_due", last_order=D(2026, 6, 1)), D(2026, 10, 1))
        self.assertEqual(S.language_hold(d.next_email_type, d.hold_reason, True), "EN_PENDING")
        # held -> never record_sent -> next day the same letter, step 0, no rung, no last_sent
        d2 = S.advance(d.state, F("reorder_due", last_order=D(2026, 6, 1)), D(2026, 10, 2))
        self.assertEqual((d2.next_email_type, d2.state.step, d2.state.last_sent_on, d2.state.rung),
                         ("reorder_1", 0, None, None))

    def test_job_wires_the_guard_and_both_sources(self):
        J = _job()
        src = open(os.path.join(ROOT, "sequence_job.py")).read()
        self.assertIn("hold = S.language_hold(d.next_email_type, hold, mk in en_masters)", src)
        self.assertIn("46 IN UNNEST(list_ids)", J.EN_ADDR_SQL)
        self.assertIn("LOWER(TRIM(LANGUAGE)) = 'en'", J.EN_ADDR_SQL)
        self.assertIn("customer_identity", J.EN_SQL)
        self.assertIn('raise RuntimeError("G-EN source empty', src)
        # the guard sits before would_send / PD writes are derived from hold
        self.assertLess(src.index("S.language_hold("), src.index("would = hold is None"))


if __name__ == "__main__":
    unittest.main()
