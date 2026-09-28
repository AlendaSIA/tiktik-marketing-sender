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


def facts(stage, last_order=None, first=None, sup=False, rungs=None):
    return S.Facts(lifecycle_stage=stage, last_order_on=last_order, first_order_on=first, suppressed=sup,
                   rung_price_valid_until=rungs)


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
        r1 = {**r1}; r2 = {**r2}
        for r in (r1, r2):
            r["type_key"], r["type_id"] = "TEST_TYPE", 999
        shadow_rows.clear()
        pd_record.write(r1, shadow=True, pd_writer=lambda r: 1 / 0, shadow_sink=shadow_rows.append)
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

    def test_type_is_unresolved_config_and_never_whatsapp(self):
        r = pd_record.render(**REC)
        self.assertIsNone(r["type_key"]); self.assertIsNone(r["type_id"]); self.assertTrue(r["done"])
        self.assertNotIn("whatsapp_chat", open(os.path.join(ROOT, "pd_record.py")).read().split('"""', 2)[2])

    def test_live_without_type_is_refused_shadow_is_not(self):
        rows = []
        pd_record.write(pd_record.render(**REC), shadow=True, pd_writer=lambda r: 1 / 0, shadow_sink=rows.append)
        self.assertEqual(len(rows), 1)
        with self.assertRaises(ValueError):
            pd_record.write(pd_record.render(**REC), shadow=False, pd_writer=lambda r: None, shadow_sink=lambda r: None)

    def test_live_without_person_is_refused(self):
        with self.assertRaises(ValueError):
            pd_record.write(pd_record.render(**{**REC, "person_id": None}), shadow=False,
                            pd_writer=lambda r: None, shadow_sink=lambda r: None)



class OfferDeadline(unittest.TestCase):
    def test_winback_7_days_lost_14_reorder_none(self):
        far = {1: D(2026, 12, 31)}
        d = S.advance(S.State("o1"), facts("winback", D(2026, 3, 1), rungs=far), D(2026, 10, 6))
        self.assertEqual(d.offer_valid_until, D(2026, 10, 12))
        d = S.advance(S.State("o2"), facts("lost", D(2025, 3, 1), rungs=far), D(2026, 10, 6))
        self.assertEqual(d.offer_valid_until, D(2026, 10, 19))
        d = S.advance(S.State("o3"), facts("reorder_due", D(2026, 6, 1)), D(2026, 10, 6))
        self.assertIsNone(d.offer_valid_until)


class ShadowJobCannotSend(unittest.TestCase):
    def test_imports_no_send_module(self):
        import ast
        src = open(os.path.join(ROOT, "sequence_job.py")).read()
        names = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                names |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module)
        self.assertFalse(names & {"brevo", "campaign", "campaign_job", "main", "push", "press_live",
                                  "draft_test", "requests"}, names)
        self.assertNotIn("api.brevo.com", src)
        self.assertNotIn("pipedrive.com", src)

    def test_history_needs_review_and_counts_flag(self):
        src = open(os.path.join(ROOT, "sequence_job.py")).read()
        self.assertIn("c.counts_for_sequence AND c.reviewed_by IS NOT NULL", src)


class History(unittest.TestCase):
    H221 = {"track": "winback", "email_type": "winback_1", "rung": 1, "sent_on": D(2026, 9, 22), "source": "brevo_history"}

    def test_221_puts_person_at_rung_1_no_second_drop_in_september(self):
        st, src = S.apply_history(S.State("h1"), [self.H221])
        self.assertEqual((st.step, st.rung, st.rung_month, src), (1, 1, "2026-09", "brevo_history"))
        d = S.advance(st, facts("winback", D(2026, 3, 1)), D(2026, 9, 26))
        self.assertEqual((d.next_email_type, d.next_due_on, d.offer_rung), ("winback_2", D(2026, 10, 1), 2))

    def test_history_is_applied_once(self):
        st, _ = S.apply_history(S.State("h2"), [self.H221])
        st2, src = S.apply_history(st, [self.H221])
        self.assertEqual((st2.step, src), (1, None))


class A2RungMonthOwnedHere(unittest.TestCase):
    """Contract v2.8.1 A2: the rung advances at most once per calendar month, via rung_month."""

    def test_rung_month_not_last_sent_decides_the_rung(self):
        # rung set 01.10, a non-ladder letter went in September: on 15.10 the letter is due at once
        # (last send in another month) but the rung must NOT climb - rung_month is October.
        st = S.State("a1", "winback", D(2026, 9, 1), 1, 1, D(2026, 10, 1), "2026-10", None,
                     "reorder_1", D(2026, 9, 20))
        d = S.advance(st, facts("winback", D(2026, 3, 1), rungs={1: D(2026, 12, 1), 2: D(2026, 12, 1)}),
                      D(2026, 10, 15))
        self.assertEqual((d.next_email_type, d.next_due_on, d.offer_rung), ("winback_2", D(2026, 10, 15), 1))

    def test_rung_climbs_one_step_never_skips_never_falls(self):
        with self.assertRaises(AssertionError):
            S.record_sent(S.State("a2"), "winback_1", D(2026, 9, 25), 2)          # start must be 1
        st = S.record_sent(S.State("a3"), "winback_1", D(2026, 9, 25), 1)
        with self.assertRaises(AssertionError):
            S.record_sent(st, "winback_2", D(2026, 10, 2), 3)                     # skip
        st3 = dc_replace(st, rung=3, rung_month="2026-09")
        with self.assertRaises(AssertionError):
            S.record_sent(st3, "lost_quarterly", D(2026, 11, 2), 2)              # fall
        self.assertEqual(S.record_sent(st3, "lost_quarterly", D(2026, 11, 2), 3).rung_month, "2026-09")  # cap: no change

    def test_second_change_in_one_month_refused_even_one_step_up(self):
        st = S.record_sent(S.State("a4"), "winback_1", D(2026, 10, 1), 1)
        with self.assertRaises(AssertionError):
            S.record_sent(st, "winback_2", D(2026, 10, 30), 2)


class P5ReorderNeverTouchesTheRung(unittest.TestCase):
    def test_reorder_with_a_held_rung_offers_rung_0_and_no_deadline(self):
        st = S.State("r1", "winback", D(2026, 9, 1), 1, 2, D(2026, 10, 1), "2026-10", None, "winback_2", D(2026, 10, 1))
        d = S.advance(st, facts("reorder_due", D(2026, 3, 1)), D(2026, 11, 5))
        self.assertIn(d.next_email_type, S.NO_LADDER_TYPES)
        self.assertEqual((d.offer_rung, d.offer_valid_until), (0, None))

    def test_reorder_sends_do_not_start_advance_or_clear(self):
        held = S.State("r2", "reorder", D(2026, 11, 1), 0, 2, D(2026, 10, 1), "2026-10", None, None, None)
        after = held
        for i, et in enumerate(["reorder_1", "reorder_2", "reorder_3"]):
            after = S.record_sent(after, et, D(2026, 11, 1) + dt.timedelta(days=14 * i), 0)
        self.assertEqual((after.rung, after.rung_set_on, after.rung_month, after.ladder_cleared_on),
                         (2, D(2026, 10, 1), "2026-10", None))
        fresh = S.State("r3")
        for et in ["reorder_1", "reorder_2", "reorder_3"]:
            fresh = S.record_sent(fresh, et, D(2026, 11, 1), 0)
        self.assertIsNone(fresh.rung); self.assertIsNone(fresh.rung_month)

    def test_reorder_history_row_with_a_rung_is_refused(self):
        with self.assertRaises(AssertionError):
            S.apply_history(S.State("r4"), [{"track": "reorder", "email_type": "reorder_2", "rung": 1,
                                            "sent_on": D(2026, 10, 1), "source": "brevo_history"}])


class Gate232233(unittest.TestCase):
    """winback_2 / winback_3 only with a rung price (stand-in for non-empty OFFER_VALID_UNTIL)."""
    ST = S.State("g", "winback", D(2026, 9, 1), 1, 1, D(2026, 9, 22), "2026-09", None, "winback_1", D(2026, 9, 22))

    def test_no_price_rows_hold(self):
        d = S.advance(self.ST, facts("winback", D(2026, 3, 1)), D(2026, 10, 1))
        self.assertEqual((d.next_email_type, d.offer_rung, d.hold_reason, d.offer_valid_until),
                         ("winback_2", 2, "no_offer_valid_until", None))

    def test_price_only_at_other_rung_holds(self):
        d = S.advance(self.ST, facts("winback", D(2026, 3, 1), rungs={1: D(2026, 10, 7)}), D(2026, 10, 1))
        self.assertEqual(d.hold_reason, "no_offer_valid_until")

    def test_expired_before_send_date_holds(self):
        d = S.advance(self.ST, facts("winback", D(2026, 3, 1), rungs={2: D(2026, 9, 30)}), D(2026, 10, 1))
        self.assertEqual(d.hold_reason, "no_offer_valid_until")

    def test_price_at_planned_rung_passes(self):
        d = S.advance(self.ST, facts("winback", D(2026, 3, 1), rungs={2: D(2026, 10, 7)}), D(2026, 10, 1))
        self.assertEqual((d.next_email_type, d.hold_reason, d.offer_valid_until), ("winback_2", None, D(2026, 10, 7)))

    def test_a6_pap_valid_until_below_our_date_on_send_day_holds(self):
        # 28.09 measured case: PAP valid_until 05.10 < OFFER_VALID_UNTIL 07.10 for a send on 01.10
        d = S.advance(self.ST, facts("winback", D(2026, 3, 1), rungs={2: D(2026, 10, 6)}), D(2026, 10, 1))
        self.assertEqual((d.hold_reason, d.offer_valid_until), ("no_offer_valid_until", None))

    def test_a6_condition_is_deferred_to_the_send_date(self):
        # planned 28.09 for 01.10: today's table (05.10) cannot decide; existence counts, date is ours
        d = S.advance(self.ST, facts("winback", D(2026, 3, 1), rungs={2: D(2026, 10, 5)}), D(2026, 9, 28))
        self.assertEqual((d.next_due_on, d.hold_reason, d.offer_valid_until), (D(2026, 10, 1), None, D(2026, 10, 7)))

    def test_winback_3_gated_too(self):
        st = dc_replace(self.ST, step=2, rung=2, rung_month="2026-10", last_sent_on=D(2026, 10, 1),
                        last_email_type="winback_2")
        self.assertEqual(S.advance(st, facts("winback", D(2026, 3, 1)), D(2026, 11, 2)).hold_reason,
                         "no_offer_valid_until")
        ok = S.advance(st, facts("winback", D(2026, 3, 1), rungs={3: D(2026, 11, 9)}), D(2026, 11, 2))
        self.assertEqual((ok.next_email_type, ok.offer_rung, ok.hold_reason), ("winback_3", 3, None))

    def test_other_letters_are_not_gated(self):
        for stage in ("winback", "lost", "reorder_due", "new", "active"):
            d = S.advance(S.State("g2"), facts(stage, D(2026, 3, 1), D(2026, 3, 1)), D(2026, 10, 1))
            self.assertNotEqual(d.hold_reason, "no_offer_valid_until", stage)

    def test_job_sql_refuses_stale_table_a7(self):
        self.assertIn("INTERVAL 26 HOUR", _job().RUNG_PRICE_SQL)

    def test_job_sql_is_strict_5_percent_per_rung(self):
        import importlib, types
        try:
            import google.cloud.bigquery  # noqa: F401  real client present (job image)
        except ImportError:               # stdlib-only runner: stub just enough to import the module
            sys.modules.setdefault("google", types.ModuleType("google"))
            cloud = sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
            cloud.bigquery = types.ModuleType("google.cloud.bigquery")
            sys.modules["google.cloud.bigquery"] = cloud.bigquery
        J = importlib.import_module("sequence_job")
        for r in (1, 2, 3):
            self.assertIn(f"IF(price_r{r} < 0.95 * shop_gross, valid_until, NULL)", J.RUNG_PRICE_SQL)
        self.assertIn("pap_block_current_v281", J.RUNG_PRICE_SOURCE)
        self.assertEqual(J.rung_price_map({"vu_r1": "2026-10-05", "vu_r2": None, "vu_r3": None}),
                         {1: D(2026, 10, 5)})


class A6OneOfferValidUntil(unittest.TestCase):
    def test_send_plus_6_and_plus_13(self):
        self.assertEqual(S.offer_valid_until("winback_1", D(2026, 10, 1)), D(2026, 10, 7))
        self.assertEqual(S.offer_valid_until("winback_3", D(2026, 10, 1)), D(2026, 10, 7))
        self.assertEqual(S.offer_valid_until("lost_quarterly", D(2026, 10, 1)), D(2026, 10, 14))
        for et in ("reorder_1", "welcome_1", "active_xsell"):
            self.assertIsNone(S.offer_valid_until(et, D(2026, 10, 1)))

    def test_null_without_rung_price_reorder_welcome(self):
        far = {1: D(2026, 12, 31), 2: D(2026, 12, 31), 3: D(2026, 12, 31)}
        for stage in ("reorder_due", "new", "active"):
            d = S.advance(S.State("n"), facts(stage, D(2026, 9, 1), D(2026, 9, 1), rungs=far), D(2026, 10, 1))
            self.assertIsNone(d.offer_valid_until, stage)
        d = S.advance(S.State("n2"), facts("winback", D(2026, 3, 1)), D(2026, 10, 1))   # winback_1, no price
        self.assertEqual((d.next_email_type, d.hold_reason, d.offer_valid_until), ("winback_1", None, None))

    def test_job_writes_ovu_only_on_would_send(self):
        src = open(os.path.join(ROOT, "sequence_job.py")).read()
        self.assertIn('"offer_valid_until": (would and d.offer_valid_until', src)


class DiffNoise(unittest.TestCase):
    def test_due_dates_on_or_before_plan_date_are_one_value(self):
        J = _job()
        self.assertEqual(J.due_token(D(2026, 9, 27), D(2026, 9, 27)), J.due_token(D(2026, 9, 28), D(2026, 9, 28)))
        self.assertEqual(J.due_token(D(2026, 9, 20), D(2026, 9, 28)), "due")
        self.assertEqual(J.due_token(D(2026, 10, 1), D(2026, 9, 28)), "2026-10-01")
        self.assertNotEqual(J.due_token(D(2026, 10, 1), D(2026, 9, 28)), J.due_token(D(2026, 10, 2), D(2026, 9, 28)))
        self.assertIsNone(J.due_token(None, D(2026, 9, 28)))

    def test_job_key_uses_due_token(self):
        src = open(os.path.join(ROOT, "sequence_job.py")).read()
        self.assertIn("due_token(d.next_due_on, today)", src)
        self.assertIn('due_token(_d(lp["planned_send_date"]), _d(lp["plan_date"]))', src)


def _job():
    import importlib, types
    try:
        import google.cloud.bigquery  # noqa: F401
    except ImportError:
        sys.modules.setdefault("google", types.ModuleType("google"))
        cloud = sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
        cloud.bigquery = types.ModuleType("google.cloud.bigquery")
        sys.modules["google.cloud.bigquery"] = cloud.bigquery
    return importlib.import_module("sequence_job")


def dc_replace(st, **kw):
    import dataclasses
    return dataclasses.replace(st, **kw)


if __name__ == "__main__":
    unittest.main()
