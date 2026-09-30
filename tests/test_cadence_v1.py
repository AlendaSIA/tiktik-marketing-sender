"""CADENCE v1 (Raivis 2026-09-30 19:11, contract K1-K7) in the shadow engine. Standard library only."""
import datetime as dt
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import sequence as S  # noqa: E402
import cadence_sim as C  # noqa: E402

D = dt.date
E1_OF = lambda e2: e2[:-3]  # noqa: E731


def F(stage, last_order=D(2026, 3, 1), thr=30, rungs=C.FAR):
    return S.Facts(stage, last_order, D(2025, 1, 1), False, rungs, thr)


def sent(st, d, day):
    return S.record_sent(d.state, d.next_email_type, day, d.offer_rung)


class K1OneReorderLetter(unittest.TestCase):
    def test_reorder_track_is_reorder_1_only(self):
        self.assertEqual(S.TRACKS["reorder_due"][1], ["reorder_1"])

    def test_reorder_2_3_never_planned_in_a_year(self):
        lo = D(2026, 1, 1)
        out = C.project(S, S.State("k1"), stage_today="reorder_due", last_order=lo, first_order=None, thr=30,
                        start=lo + dt.timedelta(days=30), days=365)
        types_ = [o[1] for o in out]
        self.assertEqual(types_.count("reorder_1"), 1)
        self.assertFalse({"reorder_2", "reorder_3"} & set(types_))


class K2QuietAfterReorder(unittest.TestCase):
    def test_winback_1_not_before_reorder_plus_35_even_if_stage_says_winback(self):
        st = S.record_sent(S.State("k2", "reorder", D(2026, 9, 1)), "reorder_1", D(2026, 9, 1), 0)
        d = S.advance(st, F("winback"), D(2026, 9, 15))
        self.assertEqual((d.next_email_type, d.next_due_on), ("winback_1", D(2026, 10, 6)))

    def test_stage_still_decides_when_it_is_later(self):
        lo, thr = D(2026, 1, 1), 30
        out = C.project(S, S.State("k2b"), stage_today="reorder_due", last_order=lo, first_order=None, thr=thr,
                        start=lo + dt.timedelta(days=thr), days=120)
        self.assertEqual([(o[1], (o[0] - lo).days) for o in out[:2]], [("reorder_1", 30), ("winback_1", 87)])

    def test_a_purchase_ends_the_quiet(self):
        st = S.record_sent(S.State("k2c", "reorder", D(2026, 9, 1)), "reorder_1", D(2026, 9, 1), 0)
        self.assertEqual(S.cadence_floor(st, F("active", last_order=D(2026, 9, 3))), D(2026, 9, 15))


class K3PriceEpisode(unittest.TestCase):
    def setUp(self):
        self.d1 = S.advance(S.State("k3"), F("winback"), D(2026, 10, 6))
        self.st = sent(None, self.d1, D(2026, 10, 6))

    def test_e1_rung_1_window_14_days(self):
        self.assertEqual((self.d1.next_email_type, self.d1.offer_rung, self.d1.offer_valid_until),
                         ("winback_1", 1, D(2026, 10, 19)))

    def test_e2_exactly_one_week_later_same_rung_same_date(self):
        for today in (D(2026, 10, 7), D(2026, 10, 13)):
            d = S.advance(self.st, F("winback"), today)
            self.assertEqual((d.next_email_type, d.next_due_on, d.offer_rung, d.offer_valid_until, d.hold_reason),
                             ("winback_1_e2", D(2026, 10, 13), 1, D(2026, 10, 19), None))

    def test_e2_never_late(self):
        d = S.advance(self.st, F("winback"), D(2026, 10, 14))
        self.assertEqual((d.next_email_type, d.next_due_on), ("winback_2", D(2026, 11, 17)))

    def test_e2_needs_the_price(self):
        d = S.advance(self.st, F("winback", rungs=None), D(2026, 10, 13))
        self.assertEqual((d.next_email_type, d.hold_reason), ("winback_1_e2", "no_offer_valid_until"))
        d = S.advance(self.st, F("winback", rungs={1: D(2026, 10, 18)}), D(2026, 10, 13))
        self.assertEqual(d.hold_reason, "no_offer_valid_until")            # PAP horizon < the episode's date

    def test_e2_goes_even_if_stage_moved_to_lost(self):
        d = S.advance(self.st, F("lost"), D(2026, 10, 13))
        self.assertEqual((d.next_email_type, d.offer_rung, d.offer_valid_until), ("winback_1_e2", 1, D(2026, 10, 19)))

    def test_e2_record_keeps_the_rung_and_month(self):
        d = S.advance(self.st, F("winback"), D(2026, 10, 13))
        st2 = S.record_sent(d.state, d.next_email_type, D(2026, 10, 13), d.offer_rung)
        self.assertEqual((st2.rung, st2.rung_set_on, st2.rung_month), (1, D(2026, 10, 6), "2026-10"))

    def test_e2_template_is_held_never_a_fallback(self):
        J = _job()
        d = S.advance(self.st, F("winback"), D(2026, 10, 13))
        tmap = {"winback_1": 180, "winback_2": 232, "reorder_1": 179}
        self.assertEqual(J.plan_template(d, tmap), (None, "E2_TEMPLATE_PENDING"))
        self.assertEqual(J.plan_template(d, {**tmap, "winback_1_e2": 9001}), (9001, None))
        self.assertEqual(J.plan_template(self.d1, tmap), (180, None))
        self.assertIsNone(S.INTERFACE_V1.get("winback_1_e2"))


class K4K5Episodes(unittest.TestCase):
    def test_full_walk_rungs_dates_and_windows(self):
        lo, thr = D(2026, 1, 1), 30
        out = C.project(S, S.State("k5"), stage_today="winback", last_order=lo, first_order=None, thr=thr,
                        start=lo + dt.timedelta(days=thr + 57), days=400)
        wb = [o for o in out if o[1].startswith("winback")]
        self.assertEqual([(o[1], o[2]) for o in wb], [("winback_1", 1), ("winback_1_e2", 1), ("winback_2", 2),
                                                      ("winback_2_e2", 2), ("winback_3", 3), ("winback_3_e2", 3)])
        e1 = [o[0] for o in wb if not o[1].endswith("_e2")]
        self.assertEqual([(b - a).days for a, b in zip(e1, e1[1:])], [42, 42])        # K5 >= 6 weeks
        for a, b in zip(wb[::2], wb[1::2]):
            self.assertEqual(((b[0] - a[0]).days, a[3], b[3]), (7, a[0] + dt.timedelta(days=13), a[3]))
        lost = [o for o in out if o[1] == "lost_quarterly"]
        self.assertGreaterEqual((lost[0][0] - wb[-1][0]).days, S.QUIET_GAP_DAYS)       # K4 before lost
        self.assertEqual({o[2] for o in lost}, {3})                                     # L3 rung kept
        self.assertTrue(all((b[0] - a[0]).days >= S.QUIET_GAP_DAYS for a, b in zip(lost, lost[1:])))

    def test_after_rung_3_episode_quiet_until_lost(self):
        st = S.State("k5b", "winback", D(2026, 1, 1), 6, 3, D(2026, 12, 18), "2026-12", None,
                     "winback_3_e2", D(2026, 12, 25))
        self.assertEqual(S.advance(st, F("winback"), D(2027, 3, 1)).hold_reason, "SEQUENCE_DONE")
        d = S.advance(st, F("lost"), D(2027, 1, 5))
        self.assertEqual((d.next_email_type, d.next_due_on, d.offer_rung), ("lost_quarterly", D(2027, 1, 29), 3))


class K6Purchase(unittest.TestCase):
    def test_purchase_after_e1_no_e2_rung_cleared(self):
        st = sent(None, S.advance(S.State("k6"), F("winback"), D(2026, 10, 6)), D(2026, 10, 6))
        d = S.advance(st, F("active", last_order=D(2026, 10, 8)), D(2026, 10, 13))
        self.assertIsNone(d.state.rung)
        self.assertNotIn(d.next_email_type, S.E2_TYPES)

    def test_purchase_ends_the_episode_even_if_the_stage_lags(self):
        # lifecycle stage can lag a day behind the paid order; the E2 must not go on stage alone
        st = sent(None, S.advance(S.State("k6c"), F("winback"), D(2026, 10, 6)), D(2026, 10, 6))
        self.assertIsNone(S.pending_e2(st, F("winback", last_order=D(2026, 10, 8)), D(2026, 10, 13)))
        d = S.advance(st, F("winback", last_order=D(2026, 10, 8)), D(2026, 10, 13))
        self.assertNotIn(d.next_email_type, S.E2_TYPES)

    def test_l8_still_doubles_winback_1(self):
        st = S.State("k6b", reorder_worked_at=D(2026, 3, 1))
        d = S.advance(st, F("winback", last_order=D(2026, 3, 1), thr=30), D(2026, 5, 1))
        self.assertEqual(d.next_due_on, D(2026, 3, 1) + dt.timedelta(days=30 + 112))


class NoConsecutiveWeeks(unittest.TestCase):
    def test_every_path_over_a_year_only_e1_e2_pairs_are_consecutive(self):
        for thr in (20, 45, 60, 75, 90, 118):
            for stage, lag in (("reorder_due", 0), ("winback", 60), ("lost", 200), ("active", -5), ("new", -25)):
                lo = D(2026, 1, 1)
                out = C.project(S, S.State(f"n{thr}{stage}"), stage_today=stage, last_order=lo,
                                first_order=lo if stage == "new" else None, thr=thr,
                                start=lo + dt.timedelta(days=max(0, thr + lag)), days=365)
                self.assertEqual(C.violations(out, S.E2_TYPES, E1_OF), [], (thr, stage))

    def test_the_checker_bites(self):
        a = (D(2026, 10, 6), "winback_1", 1, None)
        self.assertEqual(C.violations([a, (D(2026, 10, 13), "winback_1_e2", 1, None)], S.E2_TYPES, E1_OF), [])
        self.assertEqual(len(C.violations([a, (D(2026, 10, 12), "lost_quarterly", 1, None)], S.E2_TYPES, E1_OF)), 1)
        self.assertEqual(len(C.violations([a, (D(2026, 10, 14), "winback_1_e2", 1, None)], S.E2_TYPES, E1_OF)), 1)


def _job():
    try:
        import google.cloud.bigquery  # noqa: F401
    except ImportError:
        sys.modules.setdefault("google", types.ModuleType("google"))
        cloud = sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
        cloud.bigquery = types.ModuleType("google.cloud.bigquery")
        sys.modules["google.cloud.bigquery"] = cloud.bigquery
    import importlib
    return importlib.import_module("sequence_job")


if __name__ == "__main__":
    unittest.main()
