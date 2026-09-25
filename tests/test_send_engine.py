"""Sūtīšanas dzinējs: utm reorder_3, the sequence/rung rules, and the shadow PD record.
Standard library only."""
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import utm  # noqa: E402
import sequence as S  # noqa: E402
import pd_record  # noqa: E402

D = dt.date


class UtmReorder3(unittest.TestCase):
    def test_reorder_3_has_a_theme(self):
        self.assertEqual(utm.theme("reorder_3"), "papildinam-3")

    def test_reorder_3_slug(self):
        self.assertEqual(utm.slug("2026-09-21", "reorder_3", "lv"), "2026-w39-papildinam-3")

    def test_every_interface_v1_letter_slugs(self):
        for et in S.INTERFACE_V1:
            if et == "akcija_weekly":
                continue
            utm.slug("2026-09-21", et, "lv")   # raises UnknownVariantTheme on a miss


def facts(stage, last_order=None, first=None, sup=False):
    return S.Facts(lifecycle_stage=stage, last_order_on=last_order, first_order_on=first, suppressed=sup)


class Ladder(unittest.TestCase):
    def test_reorder_never_gets_a_rung(self):
        st = S.State("m1")
        for i in range(3):
            d = S.advance(st, facts("reorder_due", D(2026, 5, 1)), D(2026, 9, 25) + dt.timedelta(days=20 * i))
            self.assertEqual(d.offer_rung, 0)
            self.assertIn(d.next_email_type, S.NO_LADDER_TYPES)
            st = S.record_sent(d.state, d.next_email_type, d.next_due_on, d.offer_rung)
        self.assertIsNone(st.rung)

    def test_first_price_letter_is_rung_1_then_monthly_plus_one_cap_3(self):
        st, got = S.State("m2"), []
        days = [D(2026, 9, 25), D(2026, 10, 2), D(2026, 11, 3), D(2026, 12, 1)]
        stages = ["winback", "winback", "winback", "lost"]
        for today, stage in zip(days, stages):
            d = S.advance(st, facts(stage, D(2026, 3, 1)), today)
            got.append((d.next_email_type, d.offer_rung, d.next_due_on))
            if d.next_due_on == today:
                st = S.record_sent(d.state, d.next_email_type, today, d.offer_rung)
            else:
                st = d.state
        self.assertEqual(got[0][:2], ("winback_1", 1))
        # 02.10 is a new calendar month -> winback_2 due today at rung 2
        self.assertEqual(got[1][:2], ("winback_2", 2))
        self.assertEqual(got[2][:2], ("winback_3", 3))
        self.assertEqual(got[3][:2], ("lost_quarterly", 3))        # cap 3, ladder carries into lost

    def test_never_two_letters_or_two_drops_in_one_month(self):
        st = S.State("m3")
        d = S.advance(st, facts("winback", D(2026, 3, 1)), D(2026, 9, 3))
        st = S.record_sent(d.state, d.next_email_type, D(2026, 9, 3), d.offer_rung)
        d2 = S.advance(st, facts("winback", D(2026, 3, 1)), D(2026, 9, 20))
        self.assertEqual(d2.next_due_on, D(2026, 10, 1))
        with self.assertRaises(AssertionError):
            S.record_sent(st, "winback_2", D(2026, 9, 21), 2)

    def test_purchase_clears_the_ladder(self):
        st = S.State("m4")
        d = S.advance(st, facts("winback", D(2026, 3, 1)), D(2026, 9, 3))
        st = S.record_sent(d.state, d.next_email_type, D(2026, 9, 3), d.offer_rung)
        self.assertEqual(st.rung, 1)
        d = S.advance(st, facts("active", D(2026, 9, 10)), D(2026, 9, 12))
        self.assertIsNone(d.state.rung)
        self.assertEqual(d.state.ladder_cleared_on, D(2026, 9, 10))
        self.assertEqual(d.offer_rung, 0)

    def test_suppressed_and_blocked_get_nothing(self):
        self.assertEqual(S.advance(S.State("m5"), facts("winback", sup=True), D(2026, 9, 25)).hold_reason, "SUPPRESSED")
        self.assertEqual(S.advance(S.State("m6"), facts("blocked"), D(2026, 9, 25)).hold_reason, "BLOCKED_OR_UNKNOWN")

    def test_only_interface_v1_names_are_emitted(self):
        names = {n for _, letters in S.TRACKS.values() for n in letters}
        self.assertTrue(names <= set(S.INTERFACE_V1))
        self.assertNotIn("rhythm_next", names)

    def test_rung_on_non_ladder_letter_is_refused(self):
        with self.assertRaises(AssertionError):
            S.record_sent(S.State("m7"), "reorder_1", D(2026, 9, 25), 1)


REC = dict(person_id=15238, org_id=None, master_key="mk-1", email="a@b.lv", email_type="winback_1",
           template_id=180, send_date=D(2026, 10, 6), offer_rung=1, reason="track=winback step=1/3",
           campaign_ref="2026-w41-tava-cena")


class ShadowPipedrive(unittest.TestCase):
    def test_same_bytes_in_both_modes(self):
        seen = {}
        shadow_rows = []
        r1 = pd_record.render(**REC)
        pd_record.write(r1, shadow=True, pd_writer=lambda r: 1 / 0, shadow_sink=shadow_rows.append)
        r2 = pd_record.render(**REC)
        pd_record.write(r2, shadow=False, pd_writer=lambda r: seen.setdefault("live", pd_record.canonical(r)),
                        shadow_sink=lambda row: 1 / 0)
        self.assertEqual(shadow_rows[0]["record_json"].encode(), seen["live"])
        self.assertEqual(pd_record.canonical(r1), pd_record.canonical(r2))

    def test_shadow_makes_zero_pipedrive_calls(self):
        calls = []
        for i in range(50):
            rec = pd_record.render(**{**REC, "master_key": f"mk-{i}"})
            pd_record.write(rec, shadow=True, pd_writer=calls.append, shadow_sink=lambda row: None)
        self.assertEqual(calls, [])

    def test_type_is_chosen_by_key(self):
        r = pd_record.render(**REC)
        self.assertEqual((r["type_key"], r["type_id"], r["done"]), ("whatsapp_chat", 21, True))

    def test_live_without_person_is_refused(self):
        with self.assertRaises(ValueError):
            pd_record.write(pd_record.render(**{**REC, "person_id": None}), shadow=False,
                            pd_writer=lambda r: None, shadow_sink=lambda r: None)


if __name__ == "__main__":
    unittest.main()
