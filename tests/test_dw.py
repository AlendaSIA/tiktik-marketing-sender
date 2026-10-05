"""DATES AND SEND WINDOW v1 (MAIN 2026-10-05 18:30, contract sha 9d7c6584cc16), DW1-DW6. Standard library only."""
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import send_path as SP  # noqa: E402
import sequence as S  # noqa: E402
from tests.test_cadence_v1 import _job  # noqa: E402
from tests.test_engine4 import F as PF  # noqa: E402

D, TD = dt.date, dt.timedelta
UTC = dt.timezone.utc


def WF(stage, last_order=D(2026, 3, 1), rungs=None):
    return S.Facts(stage, last_order, D(2025, 1, 1), False, rungs, 30)


class DW1Dates(unittest.TestCase):
    def test_e1_send_plus_13_with_or_without_a_price(self):
        for rungs in (None, {1: D(2099, 1, 1)}):
            d = S.advance(S.State("a"), WF("winback", rungs=rungs), D(2026, 10, 6))
            self.assertEqual((d.next_email_type, d.next_due_on, d.offer_valid_until),
                             ("winback_1", D(2026, 10, 6), D(2026, 10, 19)))

    def test_e2_carries_the_date_of_its_e1(self):
        d1 = S.advance(S.State("b"), WF("winback", rungs={1: D(2099, 1, 1)}), D(2026, 10, 6))
        st = S.record_sent(d1.state, "winback_1", D(2026, 10, 6), 1)
        for rungs in (None, {1: D(2099, 1, 1)}):                       # also when the E2 is held for no price
            d2 = S.advance(st, WF("winback", rungs=rungs), D(2026, 10, 10))
            self.assertEqual((d2.next_email_type, d2.next_due_on, d2.offer_valid_until),
                             ("winback_1_e2", D(2026, 10, 13), d1.offer_valid_until))

    def test_e1_of_rung_2_and_3(self):
        st = S.State("c", "winback", D(2026, 9, 1), 2, 1, D(2026, 8, 1), "2026-08", None, "winback_1_e2", D(2026, 8, 8))
        d = S.advance(st, WF("winback"), D(2026, 10, 6))
        self.assertEqual((d.next_email_type, d.offer_valid_until - d.next_due_on), ("winback_2", TD(days=13)))
        st = S.State("c3", "winback", D(2026, 9, 1), 4, 2, D(2026, 8, 1), "2026-08", None, "winback_2_e2", D(2026, 8, 8))
        d = S.advance(st, WF("winback"), D(2026, 10, 6))
        self.assertEqual((d.next_email_type, d.offer_valid_until - d.next_due_on), ("winback_3", TD(days=13)))

    def test_234_send_plus_14(self):
        for rungs in (None, {4: D(2099, 1, 1)}):
            d = S.advance(S.State("l"), WF("lost", D(2025, 1, 1), rungs), D(2026, 10, 6))
            self.assertEqual((d.next_email_type, d.offer_valid_until), ("lost_quarterly", D(2026, 10, 20)))
        later = S.advance(S.record_sent(S.State("l2", "lost_wave", D(2026, 9, 1)), "lost_quarterly", D(2026, 9, 1), 4),
                          WF("lost", D(2025, 1, 1)), D(2026, 10, 6))          # planned for a later day
        self.assertEqual(later.offer_valid_until, later.next_due_on + TD(days=14))

    def test_235_xsell_valid_until_send_plus_13_in_its_own_column(self):
        d = S.advance(S.State("x"), PF(nr=None, order_on=None, ship=None), D(2026, 10, 5))
        self.assertEqual((d.next_email_type, d.xsell_valid_until - d.next_due_on, d.offer_valid_until),
                         (S.XSELL, TD(days=13), None))

    def test_letters_without_a_price_carry_no_date(self):
        self.assertIsNone(S.advance(S.State("r"), WF("reorder_due"), D(2026, 10, 6)).offer_valid_until)
        d = S.advance(S.State("p"), PF(), D(2026, 10, 5))
        self.assertEqual((d.next_email_type, d.offer_valid_until, d.xsell_valid_until), (S.PP1, None, None))

    def test_job_writes_the_date_on_held_rows_too(self):
        src = open(os.path.join(ROOT, "sequence_job.py")).read()
        self.assertNotIn("(would and d.", src)


class DW4NoReplanAfterTheWriter(unittest.TestCase):
    ROW = {"run_id": "lf-1", "plan_run_id": "plan-1"}

    def test_refused_once_the_writer_ran(self):
        J = _job()
        with self.assertRaises(J.ReplanRefused) as e:
            J.replan_guard(self.ROW, False, False)
        self.assertIn("lf-1", str(e.exception))

    def test_before_the_writer_and_temp_runs_are_free(self):
        J = _job()
        self.assertFalse(J.replan_guard(None, False, False))
        self.assertFalse(J.replan_guard(self.ROW, False, True))

    def test_override_is_visible(self):
        J = _job()
        self.assertTrue(J.replan_guard(self.ROW, True, False))
        src = open(os.path.join(ROOT, "sequence_job.py")).read()
        self.assertIn('"replanned_after_writer": replanned', src)
        self.assertLess(src.index("replan_guard(next("), src.index("facts = list(bq.query(INPUT_SQL)"))


OK = {"status": "OK", "plan_run_id": "plan-1", "run_id": "lf-1"}


def lock(now, lf=OK, plan="plan-1", send_date="2026-10-13"):
    return SP.window_lock(send_date=send_date, now=now, letter_fields=lambda d: lf, plan_run=lambda d: plan)


class DW3DW4Gate(unittest.TestCase):
    def test_passes_inside_the_window_with_the_writer_on_the_latest_plan(self):
        self.assertEqual(lock(dt.datetime(2026, 10, 13, 6, 0, tzinfo=UTC)), [])        # 09:00 Riga
        self.assertEqual(lock(dt.datetime(2026, 10, 13, 19, 59, tzinfo=UTC)), [])      # 22:59 Riga

    def test_outside_the_window_refuses(self):
        for h, m in ((5, 59), (20, 0)):                                                # 08:59 and 23:00 Riga
            c = lock(dt.datetime(2026, 10, 13, h, m, tzinfo=UTC))
            self.assertEqual([k for k, _ in c], ["L10"])
            self.assertIn("outside the send window", c[0][1])

    def test_window_follows_riga_winter_time(self):
        self.assertEqual(lock(dt.datetime(2026, 11, 3, 7, 0, tzinfo=UTC), send_date="2026-11-03"), [])   # 09:00 EET
        self.assertTrue(lock(dt.datetime(2026, 11, 3, 6, 30, tzinfo=UTC), send_date="2026-11-03"))       # 08:30 EET

    def test_writer_not_ok_or_missing_refuses(self):
        now = dt.datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
        for lf in (None, {"status": "FAILED", "plan_run_id": "plan-1"}):
            self.assertIn("letter_fields_log is not OK", lock(now, lf=lf)[0][1])

    def test_plan_rebuilt_after_the_writer_refuses(self):
        c = lock(dt.datetime(2026, 10, 13, 8, 0, tzinfo=UTC), plan="plan-2")
        self.assertIn("rebuilt after the letter writer ran", c[0][1])
        self.assertIn("no plan run", lock(dt.datetime(2026, 10, 13, 8, 0, tzinfo=UTC), plan=None)[0][1])

    def test_yesterdays_send_date_refuses(self):
        self.assertIn("is not today in Riga", lock(dt.datetime(2026, 10, 14, 8, 0, tzinfo=UTC))[0][1])

    def test_dispatch_checks_it_before_the_audience_and_production_is_unwired(self):
        src = open(os.path.join(ROOT, "send_path.py")).read()
        self.assertLess(src.index("closed = window_lock("), src.index("audience = lookups.audience(batch_id, build_id)"))
        for name in ("letter_fields", "plan_run"):
            with self.assertRaises(SP.SendLocked):
                getattr(SP._ProductionLookupsNotWired(), name)("2026-10-13")


if __name__ == "__main__":
    unittest.main()
