"""LADDER POLICY v1 (Raivis 2026-09-28, contract sha 326480dce080), rules L1-L8. Standard library only."""
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import sequence as S  # noqa: E402

D = dt.date
FAR = {1: D(2027, 1, 1), 2: D(2027, 1, 1), 3: D(2027, 1, 1)}


def F(stage, last_order=D(2026, 3, 1), thr=30, rungs=FAR):
    return S.Facts(stage, last_order, D(2025, 1, 1), False, rungs, thr)


def walk(st, stage, day, **kw):
    d = S.advance(st, F(stage, **kw), day)
    if d.next_due_on == day and d.hold_reason is None:
        return S.record_sent(d.state, d.next_email_type, day, d.offer_rung), d
    return d.state, d


class L1NoPriceOutsideLadder(unittest.TestCase):
    def test_welcome_reorder_active_rung_0(self):
        for stage in ("new", "reorder_due", "active"):
            st = S.State("x", rung=2, rung_month="2026-08", rung_set_on=D(2026, 8, 1))
            self.assertEqual(S.advance(st, F(stage, last_order=D(2026, 7, 1)), D(2026, 10, 5)).offer_rung, 0, stage)


class L2RungByStage(unittest.TestCase):
    def test_winback_1_2_3_are_rung_1_2_3(self):
        st, got = S.State("a"), []
        for day in (D(2026, 10, 1), D(2026, 11, 2), D(2026, 12, 1)):
            st, d = walk(st, "winback", day)
            got.append((d.next_email_type, d.offer_rung))
        self.assertEqual(got, [("winback_1", 1), ("winback_2", 2), ("winback_3", 3)])

    def test_rung_is_the_stage_not_previous_plus_one(self):
        # rung and step diverged (e.g. imported history): the stage decides, not "last rung + 1"
        st = S.State("b0", "winback", D(2026, 9, 1), 2, 1, D(2026, 10, 1), "2026-10", None, "winback_2", D(2026, 11, 2))
        self.assertEqual(S.planned_rung(st, "winback_3", D(2026, 12, 1)), 3)
        self.assertEqual(S.planned_rung(S.State("b1"), "winback_2", D(2026, 12, 1)), 2)

    def test_not_by_time_alone_rung_waits_for_the_next_letter(self):
        st, _ = walk(S.State("b"), "winback", D(2026, 10, 1))
        d = S.advance(st, F("winback"), D(2026, 10, 20))          # same month: next letter 01.11, rung stays until then
        self.assertEqual((d.next_email_type, d.next_due_on, d.state.rung), ("winback_2", D(2026, 11, 1), 1))


class L3Lost(unittest.TestCase):
    def test_lost_keeps_last_rung_never_deeper_month_after_month(self):
        st = S.State("c", "winback", D(2026, 9, 1), 2, 2, D(2026, 10, 1), "2026-10", None, "winback_2", D(2026, 10, 1))
        rungs = []
        for day in (D(2026, 11, 3), D(2026, 12, 1), D(2027, 1, 4)):
            st, d = walk(st, "lost", day)
            rungs.append(d.offer_rung)
        self.assertEqual(rungs, [2, 2, 2])

    def test_lost_without_reached_rung_starts_at_entry_rung(self):
        self.assertEqual(S.advance(S.State("c2"), F("lost"), D(2026, 10, 1)).offer_rung, S.LOST_ENTRY_RUNG)


class L4Window(unittest.TestCase):
    def test_window_plus_6_and_plus_13(self):
        self.assertEqual(S.advance(S.State("w"), F("winback"), D(2026, 10, 1)).offer_valid_until, D(2026, 10, 7))
        self.assertEqual(S.advance(S.State("w2"), F("lost"), D(2026, 10, 1)).offer_valid_until, D(2026, 10, 14))


class L5PurchaseEnds(unittest.TestCase):
    def test_paid_order_rung_0_next_letter_reorder(self):
        st = S.State("p", "winback", D(2026, 9, 1), 2, 2, D(2026, 10, 1), "2026-10", None, "winback_2", D(2026, 10, 1))
        d = S.advance(st, F("reorder_due", last_order=D(2026, 10, 3)), D(2026, 11, 20))
        self.assertEqual((d.state.rung, d.next_email_type, d.offer_rung), (None, "reorder_1", 0))


class L6AntiWaitingCap(unittest.TestCase):
    def test_purchase_inside_rung_2_window_caps_12_months(self):
        sends = [{"email_type": "winback_2", "rung": 2, "sent_on": D(2026, 11, 2)}]
        self.assertEqual(S.ladder_marks(sends, [D(2026, 11, 5)]), (1, D(2027, 11, 5), None))

    def test_purchase_outside_window_or_at_rung_1_no_cap(self):
        self.assertEqual(S.ladder_marks([{"email_type": "winback_2", "rung": 2, "sent_on": D(2026, 11, 2)}],
                                        [D(2026, 11, 9)])[:2], (None, None))
        self.assertEqual(S.ladder_marks([{"email_type": "winback_1", "rung": 1, "sent_on": D(2026, 11, 2)}],
                                        [D(2026, 11, 3)])[:2], (None, None))

    def test_lost_window_is_14_days(self):
        self.assertEqual(S.ladder_marks([{"email_type": "lost_quarterly", "rung": 3, "sent_on": D(2026, 11, 2)}],
                                        [D(2026, 11, 15)])[:2], (1, D(2027, 11, 15)))

    def test_capped_ladder_starts_and_stays_at_1(self):
        st = S.State("k", rung_cap=1, rung_cap_until=D(2027, 11, 5))
        got = []
        for day in (D(2027, 3, 1), D(2027, 4, 1), D(2027, 5, 3), D(2027, 6, 1)):
            st, d = walk(st, "winback" if day < D(2027, 6, 1) else "lost", day)
            got.append(d.offer_rung)
        self.assertEqual(got, [1, 1, 1, 1])

    def test_cap_expires(self):
        st = S.State("k2", "winback", D(2027, 11, 1), 1, 1, D(2027, 11, 2), "2027-11", None, "winback_1",
                     D(2027, 11, 2), rung_cap=1, rung_cap_until=D(2027, 11, 5))
        self.assertEqual(S.advance(st, F("winback"), D(2027, 12, 1)).offer_rung, 2)


class L7NoRestart(unittest.TestCase):
    def test_lost_back_to_winback_without_purchase_is_held(self):
        st = S.State("r", "lost_wave", D(2026, 12, 1), 1, 3, D(2026, 12, 1), "2026-12", None, "lost_quarterly", D(2026, 12, 1))
        d = S.advance(st, F("winback", last_order=D(2026, 3, 1)), D(2027, 1, 5))
        self.assertEqual((d.hold_reason, d.next_email_type), ("LADDER_NO_RESTART", None))

    def test_after_a_purchase_a_new_walk_is_allowed(self):
        st = S.State("r2", "lost_wave", D(2026, 12, 1), 1, None, None, None, D(2027, 1, 2), "lost_quarterly", D(2026, 12, 1))
        d = S.advance(st, F("winback", last_order=D(2027, 1, 2)), D(2027, 4, 1))
        self.assertEqual((d.hold_reason, d.next_email_type, d.offer_rung), (None, "winback_1", 1))


class L8DoubledGap(unittest.TestCase):
    def test_reorder_worked_is_detected_only_before_a_price_letter(self):
        worked = [{"email_type": "reorder_1", "rung": 0, "sent_on": D(2026, 5, 1)}]
        self.assertEqual(S.ladder_marks(worked, [D(2026, 3, 1), D(2026, 5, 10)])[2], D(2026, 5, 10))
        priced = worked + [{"email_type": "winback_1", "rung": 1, "sent_on": D(2026, 6, 1)}]
        self.assertIsNone(S.ladder_marks(priced, [D(2026, 3, 1), D(2026, 6, 3)])[2])
        self.assertIsNone(S.ladder_marks([], [D(2026, 5, 10)])[2])        # no reorder letter -> nothing learned

    def test_winback_1_waits_2x_gap_in_later_cycles_only(self):
        st = S.State("g", reorder_worked_at=D(2026, 5, 10))
        # later cycle (last order 2026-05-10), threshold 30: normal winback from +86 d, L8 from 30 + 2*56 = +142 d
        d = S.advance(st, F("winback", last_order=D(2026, 5, 10), thr=30), D(2026, 8, 10))
        self.assertEqual((d.next_email_type, d.next_due_on), ("winback_1", D(2026, 5, 10) + dt.timedelta(days=142)))
        plain = S.advance(S.State("g2"), F("winback", last_order=D(2026, 5, 10), thr=30), D(2026, 8, 10))
        self.assertEqual(plain.next_due_on, D(2026, 8, 10))

    def test_factor_is_fixed_2_and_gap_is_the_configured_56(self):
        self.assertEqual((S.L8_FACTOR, S.REORDER_TO_WINBACK1_GAP_DAYS), (2, 56))


class JobWiring(unittest.TestCase):
    SRC = open(os.path.join(ROOT, "sequence_job.py")).read()

    def test_state_carries_new_columns_and_policy_name(self):
        for c in ('"rung_cap"', '"rung_cap_until"', '"reorder_worked_at"', "ladder-policy-v1", "S.ladder_marks("):
            self.assertIn(c, self.SRC)
        self.assertNotIn("defaults-UNCONFIRMED", self.SRC)


if __name__ == "__main__":
    unittest.main()
