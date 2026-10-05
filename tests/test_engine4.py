"""Sūtīšanas dzinējs 4 (MAIN 2026-10-05 16:10, contract sha 682ad015f0ab): POST-PURCHASE v1.2 chain, B2B / LEAD guard,
pre-send gates, akcija audience, self-check. Standard library only."""
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import presend as G  # noqa: E402
import selfcheck as SC  # noqa: E402
import sequence as S  # noqa: E402
from tests.test_cadence_v1 import _job  # noqa: E402

D = dt.date
TD = dt.timedelta


def F(stage="active", last_order=D(2026, 10, 1), nr="M-860325-35000", order_on=D(2026, 10, 1), ship=D(2026, 10, 2),
      pp=None, xs=None, thr=60, rungs=None):
    return S.Facts(stage, last_order, D(2025, 1, 1), False, rungs, thr, nr, order_on, ship, pp, xs)


def sent(d, day):
    return S.record_sent(d.state, d.next_email_type, day, d.offer_rung)


class Interface(unittest.TestCase):
    def test_v2_is_exactly_the_commanded_letters(self):
        self.assertEqual(S.INTERFACE_V2, {
            "reorder_1": 179, "winback_1": 180, "winback_1_e2": 9180, "winback_2": 232, "winback_2_e2": 9232,
            "winback_3": 233, "winback_3_e2": 9233, "lost_quarterly": 234, "active_xsell": 235,
            "post_purchase_feedback": 244, "akcija_weekly": 236})

    def test_229_is_out_no_mapping_no_track(self):
        self.assertNotIn(229, S.INTERFACE_V2.values())
        self.assertNotIn("welcome_1", S.INTERFACE_V2)
        names = {n for _, letters in S.TRACKS.values() for n in letters}
        self.assertFalse(S.NEVER_PLANNED & names)
        for stage in ("new", "active"):                                 # a new buyer follows the same chain
            d = S.advance(S.State("n"), F(stage), D(2026, 10, 5))
            self.assertEqual(d.next_email_type, S.PP1)

    def test_env_overrides_a_provisional_id_without_code(self):
        J = _job()
        d = S.advance(S.record_sent(S.State("e", "winback", D(2026, 9, 1)), "winback_1", D(2026, 10, 6), 1),
                      S.Facts("winback", D(2026, 3, 1), None, False, {1: D(2099, 1, 1)}, 30), D(2026, 10, 13))
        self.assertEqual(J.plan_template(d, {}, {**S.INTERFACE_V2, "winback_1_e2": 251}), (251, None, "config"))


class PostPurchase(unittest.TestCase):
    def test_244_three_days_after_hand_over_with_the_order_number(self):
        d = S.advance(S.State("p1"), F(ship=D(2026, 10, 2)), D(2026, 10, 3))
        self.assertEqual((d.next_email_type, d.next_due_on, d.hold_reason, d.trigger_order_nr, d.offer_rung),
                         (S.PP1, D(2026, 10, 5), None, "M-860325-35000", 0))
        self.assertIsNone(d.offer_valid_until)

    def test_not_handed_over_yet_waits_then_skips(self):
        d = S.advance(S.State("p2"), F(ship=None), D(2026, 10, 5))
        self.assertEqual((d.next_email_type, d.next_due_on, d.hold_reason), (S.PP1, None, S.HOLD_PP_NOT_SHIPPED))
        self.assertEqual(d.trigger_order_nr, "M-860325-35000")
        self.assertEqual(S.advance(S.State("p2"), F(ship=None), D(2026, 10, 22)).hold_reason, S.HOLD_PP_NOT_SHIPPED)
        d = S.advance(S.State("p2"), F(ship=None), D(2026, 10, 23))     # 22 days after the order
        self.assertEqual(d.next_email_type, S.XSELL)                    # PP1 skipped, chain goes on

    def test_at_most_one_244_per_30_days(self):
        # 244 sent 20.09 for an earlier order; new order handed over 02.10 -> due 05.10 < 20.09 + 30 -> skipped
        f = F(pp=D(2026, 9, 20), last_order=D(2026, 10, 1))
        st = S.record_sent(S.State("p3", "post_purchase", D(2026, 9, 1)), S.PP1, D(2026, 9, 20), 0)
        d = S.advance(st, f, D(2026, 10, 5))
        self.assertEqual(d.next_email_type, S.XSELL)
        self.assertIn("PP1 skipped: last 244", d.reason)
        # 31 days after the previous one it goes again
        f = F(pp=D(2026, 9, 1), last_order=D(2026, 10, 1))
        st = S.record_sent(S.State("p3b", "post_purchase", D(2026, 8, 1)), S.PP1, D(2026, 9, 1), 0)
        self.assertEqual(S.advance(st, f, D(2026, 10, 5)).next_email_type, S.PP1)

    def test_late_244_is_skipped_never_sent_late(self):
        d = S.advance(S.State("p4"), F(last_order=D(2026, 8, 1), order_on=D(2026, 8, 1), ship=D(2026, 8, 2)),
                      D(2026, 10, 5))
        self.assertEqual(d.next_email_type, S.XSELL)
        self.assertIn("PP1 skipped: was due 2026-08-05", d.reason)
        # K8 may not push it out of its window either: E1 on 22.09, order 21.09 handed over 23.09 -> floor 03.11
        st = S.record_sent(S.State("p4c", "winback", D(2026, 9, 1)), "winback_1", D(2026, 9, 22), 1)
        late = S.advance(st, F(last_order=D(2026, 9, 21), order_on=D(2026, 9, 21), ship=D(2026, 9, 23),
                               rungs={1: D(2099, 1, 1)}), D(2026, 9, 24))
        self.assertNotEqual(late.next_email_type, S.PP1)
        self.assertIn("cannot go before", late.reason)
        ok = S.advance(S.State("p4b"), F(ship=D(2026, 9, 25), last_order=D(2026, 9, 24), order_on=D(2026, 9, 24)),
                       D(2026, 10, 5))                                 # due 28.09, 7 days late = last day
        self.assertEqual((ok.next_email_type, ok.next_due_on), (S.PP1, D(2026, 10, 5)))

    def test_235_after_244_with_an_akcija_week_between_and_xsell_valid_until(self):
        d1 = S.advance(S.State("p5"), F(), D(2026, 10, 5))
        st = sent(d1, D(2026, 10, 5))
        d2 = S.advance(st, F(pp=D(2026, 10, 5)), D(2026, 10, 6))
        self.assertEqual((d2.next_email_type, d2.next_due_on), (S.XSELL, D(2026, 10, 19)))   # 244 + 14 (K8)
        late = S.advance(S.record_sent(d1.state, S.PP1, D(2026, 10, 12), 0), F(pp=D(2026, 10, 12)), D(2026, 10, 13))
        self.assertEqual(late.next_due_on, D(2026, 10, 26))                                   # a late 244 pushes 235
        self.assertEqual(d2.xsell_valid_until, D(2026, 11, 1))                               # XS2: send + 13
        self.assertEqual((d2.offer_rung, d2.offer_valid_until, d2.trigger_order_nr), (0, None, None))
        # at least one Tuesday (weekly akcija) lies strictly between the two letters
        days = [D(2026, 10, 5) + TD(days=i) for i in range(1, 14)]
        self.assertTrue(any(x.weekday() == 1 for x in days))

    def test_235_once_per_purchase_then_done_until_179(self):
        st = S.record_sent(S.record_sent(S.State("p6", "post_purchase", D(2026, 10, 1)), S.PP1, D(2026, 10, 5), 0),
                           S.XSELL, D(2026, 10, 19), 0)
        d = S.advance(st, F(pp=D(2026, 10, 5), xs=D(2026, 10, 19)), D(2026, 11, 20))
        self.assertEqual((d.next_email_type, d.hold_reason), (None, "SEQUENCE_DONE"))
        # the stage turns reorder_due -> 179 (K8: >= 14 d after the last letter)
        d = S.advance(st, F("reorder_due", pp=D(2026, 10, 5), xs=D(2026, 10, 19)), D(2026, 11, 30))
        self.assertEqual((d.next_email_type, d.next_due_on), ("reorder_1", D(2026, 11, 30)))

    def test_179_falling_due_earlier_wins_and_the_rest_is_skipped(self):
        st = sent(S.advance(S.State("p7"), F(), D(2026, 10, 5)), D(2026, 10, 5))
        d = S.advance(st, F("reorder_due", pp=D(2026, 10, 5)), D(2026, 10, 12))     # frequent buyer: due before 235
        self.assertEqual((d.next_email_type, d.next_due_on), ("reorder_1", D(2026, 10, 19)))   # K8 floor
        st = sent(d, D(2026, 10, 19))
        d = S.advance(st, F("reorder_due", pp=D(2026, 10, 5)), D(2026, 10, 20))
        self.assertEqual(d.hold_reason, "SEQUENCE_DONE")                              # no 235 after 179

    def test_a_new_purchase_restarts_the_chain(self):
        st = S.record_sent(S.record_sent(S.State("p8", "post_purchase", D(2026, 8, 1)), S.PP1, D(2026, 8, 5), 0),
                           S.XSELL, D(2026, 8, 19), 0)
        f = F(last_order=D(2026, 10, 1), order_on=D(2026, 10, 1), ship=D(2026, 10, 2), pp=D(2026, 8, 5),
              xs=D(2026, 8, 19), nr="M-860325-36000")
        d = S.advance(st, f, D(2026, 10, 5))
        self.assertEqual((d.next_email_type, d.trigger_order_nr), (S.PP1, "M-860325-36000"))

    def test_k8_floor_applies_to_244(self):
        st = S.record_sent(S.State("p9", "reorder", D(2026, 9, 1)), "reorder_1", D(2026, 9, 28), 0)
        d = S.advance(st, F(), D(2026, 10, 5))                         # bought 01.10 after 179 of 28.09
        self.assertEqual((d.next_email_type, d.next_due_on), (S.PP1, D(2026, 10, 12)))   # 28.09 + 14

    def test_no_shop_order_for_this_purchase_goes_straight_to_235(self):
        d = S.advance(S.State("p10"), F(nr=None, order_on=None, ship=None), D(2026, 10, 5))
        self.assertEqual((d.next_email_type, d.next_due_on), (S.XSELL, D(2026, 10, 18)))   # last order + 17
        J = _job()
        self.assertEqual(J.pp_facts({"order_nr": "M-1", "order_on": D(2026, 5, 1), "ship_on": D(2026, 5, 2)},
                                    D(2026, 10, 1)), (None, None, None))      # an older order is not THIS purchase
        self.assertEqual(J.pp_facts({"order_nr": " M-2 ", "order_on": D(2026, 9, 29), "ship_on": None},
                                    D(2026, 10, 1)), ("M-2", D(2026, 9, 29), None))

    def test_state_never_moves_in_shadow(self):
        st = S.State("p11")
        d = S.advance(st, F(), D(2026, 10, 5))
        d2 = S.advance(d.state, F(), D(2026, 10, 6))
        self.assertEqual((d2.next_email_type, d2.state.last_sent_on, d2.state.step), (S.PP1, None, 0))


class FlowGuard(unittest.TestCase):
    def test_b2b_and_lead_get_no_lifecycle_letter(self):
        for et, hold in (("reorder_1", None), ("winback_1", None), (S.PP1, None), (S.XSELL, None),
                         ("lost_quarterly", None), ("winback_2", "no_offer_valid_until"), (None, "SEQUENCE_DONE"),
                         (S.PP1, S.HOLD_PP_NOT_SHIPPED)):
            self.assertEqual(S.flow_hold(et, hold, "B2B"), "B2B_FLOW", et)
            self.assertEqual(S.flow_hold(et, hold, "LEAD"), "LEAD_FLOW", et)

    def test_shop_and_unclassified_unchanged_and_suppressed_keeps_its_reason(self):
        for flow in (None, "SHOP"):
            self.assertIsNone(S.flow_hold("reorder_1", None, flow))
        for hold in S.FLOW_KEEPS:
            self.assertEqual(S.flow_hold(None, hold, "B2B"), hold)

    def test_b2b_wins_over_en_and_job_wires_both_sources_before_would_send(self):
        self.assertEqual(S.language_hold("reorder_1", S.flow_hold("reorder_1", None, "B2B"), True), "EN_PENDING")
        # documented order: flow first, then language; either way the contact gets nothing
        J = _job()
        src = open(os.path.join(ROOT, "sequence_job.py")).read()
        self.assertLess(src.index("S.flow_hold("), src.index("S.language_hold(d.next_email_type, hold"))
        self.assertLess(src.index("S.flow_hold("), src.index("would = hold is None"))
        self.assertIn("b2b_shop_flow_classification_v2", J.FLOW_SQL)
        self.assertIn("flow IN ('B2B', 'LEAD')", J.FLOW_SQL)
        self.assertIn(J.F309_KEY, J.FLOW_SQL)
        self.assertIn("= '716'", J.FLOW_SQL)
        self.assertIn('raise RuntimeError("B2B guard source empty', src)


class OrderAfterLastPurchase(unittest.TestCase):
    def test_a_newer_shop_order_holds_every_sales_letter_of_the_old_cycle(self):
        for et in ("reorder_1", "winback_1", "winback_1_e2", "winback_3", "lost_quarterly"):
            self.assertEqual(S.recent_order_hold(et, None, D(2026, 10, 3), D(2026, 6, 1)), S.HOLD_ORDER_AFTER, et)
        self.assertEqual(S.recent_order_hold("winback_2", "no_offer_valid_until", D(2026, 10, 3), D(2026, 6, 1)),
                         S.HOLD_ORDER_AFTER)

    def test_same_or_older_order_and_post_purchase_letters_are_untouched(self):
        self.assertIsNone(S.recent_order_hold("reorder_1", None, D(2026, 6, 1), D(2026, 6, 1)))
        self.assertIsNone(S.recent_order_hold("reorder_1", None, D(2026, 5, 1), D(2026, 6, 1)))
        self.assertIsNone(S.recent_order_hold("reorder_1", None, None, D(2026, 6, 1)))
        for et in (S.PP1, S.XSELL):
            self.assertIsNone(S.recent_order_hold(et, None, D(2026, 10, 3), D(2026, 6, 1)), et)
        self.assertEqual(S.recent_order_hold("reorder_1", "SUPPRESSED", D(2026, 10, 3), D(2026, 6, 1)), "SUPPRESSED")

    def test_job_wires_it_before_would_send(self):
        src = open(os.path.join(ROOT, "sequence_job.py")).read()
        self.assertLess(src.index("S.recent_order_hold("), src.index("would = hold is None"))
        self.assertLess(src.index("S.recent_order_hold("), src.index("S.flow_hold("))


def ctx(**k):
    base = dict(template_id=180, template_approved=True, offer_valid_until=D(2026, 10, 19), goods=(1, False),
                r_handles=("a", "b"), r_cabinet=("a", "b"), r1_ref_price="9,90 €",
                xsell_valid_until=D(2026, 10, 19), anketa_url="https://x/atsauksme.php?o=M-1&t=t", order_nr="M-1")
    base.update(k)
    return G.Ctx(**base)


class PreSendGates(unittest.TestCase):
    def test_a_complete_letter_passes(self):
        for et, rung, tid in (("reorder_1", 0, 179), ("winback_1", 1, 180), ("winback_2", 2, 232),
                              ("lost_quarterly", 4, 234), (S.XSELL, 0, 235), (S.PP1, 0, 244)):
            g = (rung, False) if rung else None
            self.assertEqual(G.gates(et, rung, ctx(template_id=tid, goods=g)), [], et)

    def test_price_letter_without_offer_valid_until_or_priced_slots(self):
        for et, rung in (("winback_1", 1), ("winback_1_e2", 1), ("winback_3", 3), ("lost_quarterly", 4)):
            self.assertEqual(G.gates(et, rung, ctx(offer_valid_until=None, goods=(rung, False))), [G.NO_OVU], et)
            self.assertEqual(G.gates(et, rung, ctx(goods=(rung, True))), [G.NO_PRICED], et)
            self.assertEqual(G.gates(et, rung, ctx(goods=None)), [G.NO_SLOT_ROW], et)
            self.assertEqual(G.gates(et, rung, ctx(goods=(rung + 1, False))), [G.NO_SLOT_ROW], et)
        self.assertEqual(G.gates("winback_1", 1, ctx(offer_valid_until=None, price_stale=True)), [G.PRICE_STALE])
        self.assertEqual(G.gates("reorder_1", 0, ctx(offer_valid_until=None, goods=None)), [])   # no price letter

    def test_235_xs4_needs_both_fields(self):
        self.assertEqual(G.gates(S.XSELL, 0, ctx(r1_ref_price="")), [G.XS4])
        self.assertEqual(G.gates(S.XSELL, 0, ctx(r1_ref_price=None)), [G.XS4])
        self.assertEqual(G.gates(S.XSELL, 0, ctx(xsell_valid_until=None)), [G.XS4])
        self.assertEqual(G.gates(S.XSELL, 0, ctx(r1_ref_price=None, price_stale=True)), [G.PRICE_STALE])

    def test_235_never_repeats_an_r_product(self):
        self.assertEqual(G.gates(S.XSELL, 0, ctx(xsell_offered=frozenset({"a", "b", "z"}))), [G.XS_NOTHING_NEW])
        self.assertEqual(G.gates(S.XSELL, 0, ctx(xsell_offered=frozenset({"b"}))), [G.XS_REPEAT])
        self.assertEqual(G.gates(S.XSELL, 0, ctx(xsell_offered=frozenset({"z"}))), [])
        self.assertEqual(G.gates("reorder_1", 0, ctx(xsell_offered=frozenset({"a", "b"}))), [])   # 235 only

    def test_244_needs_the_link_and_the_order(self):
        self.assertEqual(G.gates(S.PP1, 0, ctx(anketa_url="")), [G.NO_ANKETA])
        self.assertEqual(G.gates(S.PP1, 0, ctx(anketa_url=None)), [G.NO_ANKETA])
        self.assertEqual(G.gates(S.PP1, 0, ctx(order_nr=None)), [G.NO_ANKETA])
        self.assertEqual(G.gates(S.PP1, 0, ctx(r_cabinet=())), [])       # 244 has no R slots -> R-CAB not asked

    def test_rcab_letter_r_goods_must_equal_the_cabinet(self):
        for et, rung in (("reorder_1", 0), ("winback_1", 1), (S.XSELL, 0), ("lost_quarterly", 4)):
            c = ctx(r_cabinet=("a",), goods=(rung, False))
            self.assertEqual(G.gates(et, rung, c), [G.RCAB], et)
        self.assertEqual(G.gates("reorder_1", 0, ctx(r_handles=(), r_cabinet=())), [])

    def test_template_provisional_and_not_approved(self):
        self.assertEqual(G.gates("winback_1_e2", 1, ctx(template_id=9180)), [G.T_PROVISIONAL])
        self.assertEqual(G.gates("reorder_1", 0, ctx(template_id=179, template_approved=False)), [G.T_NOT_APPROVED])

    def test_empty_ctx_refuses_and_gates_are_named_and_ordered(self):
        self.assertEqual(G.gates("winback_1", 1, G.Ctx()), [G.T_NOT_APPROVED, G.NO_OVU, G.NO_SLOT_ROW])
        self.assertEqual(G.gates(S.XSELL, 0, G.Ctx()), [G.T_NOT_APPROVED, G.XS4])
        self.assertEqual(G.gates(S.PP1, 0, G.Ctx()), [G.T_NOT_APPROVED, G.NO_ANKETA])
        self.assertEqual(G.gates(None, 0, G.Ctx()), [])
        self.assertEqual(len(set(G.ALL)), 11)

    def test_job_stores_the_gate_next_to_the_plan_and_never_changes_would_send(self):
        src = open(os.path.join(ROOT, "sequence_job.py")).read()
        self.assertIn('"would_deliver": would and not gate_list', src)
        self.assertIn('"presend_gate": gate_list[0] if gate_list else None', src)
        self.assertLess(src.index("would = hold is None"), src.index("gate_list = G.gates("))
        self.assertNotIn("hold = gate_list", src)                      # G15.2: a gate is never a planner hold


class Akcija(unittest.TestCase):
    def setUp(self):
        self.J = _job()
        self.week = self.J.akcija_week(D(2026, 10, 5))                 # Monday 05.10 -> Tuesday 06.10, W41

    def row(self, **k):
        base = dict(mk="m", email="a@x.lv", stage="active", suppressed=False, flow=None, is_en=False, plan=None,
                    week=self.week)
        base.update(k)
        return self.J.akcija_row(**base)

    def test_week(self):
        self.assertEqual(self.week, (D(2026, 10, 6), "2026-W41", D(2026, 10, 5), D(2026, 10, 11)))
        self.assertEqual(self.J.akcija_week(D(2026, 10, 6))[0], D(2026, 10, 6))
        self.assertEqual(self.J.akcija_week(D(2026, 10, 7))[0], D(2026, 10, 13))

    def test_personal_sales_letter_or_akcija_never_both(self):
        p = {"email_type": "winback_1", "would_deliver": True, "planned_send_date": "2026-10-08"}
        r = self.row(plan=p)
        self.assertEqual((r["in_audience"], r["excluded_reason"], r["personal_email_type"]),
                         (False, "PERSONAL_LETTER_THIS_WEEK", "winback_1"))
        self.assertTrue(self.row(plan={**p, "planned_send_date": "2026-10-13"})["in_audience"])   # next week
        self.assertTrue(self.row(plan={**p, "would_deliver": False})["in_audience"])   # blocked letter -> akcija
        self.assertTrue(self.row(plan={**p, "email_type": S.PP1})["in_audience"])      # 244 is not a sales letter
        for et in S.SALES_TYPES:
            self.assertFalse(self.row(plan={**p, "email_type": et})["in_audience"], et)

    def test_person_level_exclusions(self):
        self.assertEqual(self.row(suppressed=True)["excluded_reason"], "SUPPRESSED")
        self.assertEqual(self.row(stage="blocked")["excluded_reason"], "BLOCKED_OR_UNKNOWN")
        self.assertEqual(self.row(flow="B2B")["excluded_reason"], "B2B_FLOW")
        self.assertEqual(self.row(flow="LEAD")["excluded_reason"], "LEAD_FLOW")
        self.assertEqual(self.row(is_en=True)["excluded_reason"], "EN_PENDING")
        self.assertTrue(self.row(flow="SHOP")["in_audience"])


def plan(mk, et="reorder_1", would=True, hold=None, gate=None, **k):
    r = {"master_key": mk, "email_type": et, "would_send": would, "hold_reason": hold, "presend_gate": gate,
         "would_deliver": would and not gate, "template_id": 179, "planned_send_date": "2026-10-05",
         "offer_rung": 0, "offer_valid_until": None, "diff_vs_prev": "same"}
    r.update(k)
    return r


class SelfCheck(unittest.TestCase):
    def run_(self, rows, akc=(), **k):
        base = dict(prev_counts=[], flows={}, en_masters=set(), suppressed_send_address=0, suppressed_any_address=0,
                    today=D(2026, 10, 5), ages_h={k: 1 for k in SC.MAX_AGE_H} | {"goods_run_days": 0},
                    writer_missing=[], map_disagreements=[])
        base.update(k)
        return {c["check_name"]: c for c in SC.run(rows, list(akc), **base)}

    def failed(self, out, level="hard"):
        return sorted(k for k, c in out.items() if c["level"] == level and not c["ok"])

    def test_clean_plan_has_no_hard_failure(self):
        out = self.run_([plan("a"), plan("b", None, False, "SUPPRESSED")])
        self.assertEqual(self.failed(out), [])
        self.assertEqual(out["count_would_send_by_type"]["value"], "1")

    def test_each_hard_rule_bites(self):
        self.assertEqual(self.failed(self.run_([plan("a")], suppressed_any_address=1)), ["suppressed_among_would_send"])
        self.assertEqual(self.failed(self.run_([plan("a")], flows={"a": "B2B"})), ["b2b_lead_among_would_send"])
        self.assertEqual(self.failed(self.run_([plan("a")], flows={"a": "LEAD"})), ["b2b_lead_among_would_send"])
        self.assertEqual(self.failed(self.run_([plan("a")], en_masters={"a"})), ["en_among_would_send"])
        self.assertEqual(self.failed(self.run_([plan("a", "welcome_1", False, "X")])), ["retired_letters_planned"])
        self.assertEqual(self.failed(self.run_([plan("a", template_id=None)])), ["would_send_without_template_or_date"])
        self.assertEqual(self.failed(self.run_([plan("a", "winback_1", offer_rung=1)])),
                         ["price_letter_deliverable_without_offer_valid_until"])
        self.assertEqual(self.failed(self.run_([plan("a", S.PP1, template_id=244)])), ["244_deliverable_without_order_nr"])
        self.assertEqual(self.failed(self.run_([plan("a", S.XSELL, template_id=235)])),
                         ["235_deliverable_without_xsell_valid_until"])
        self.assertEqual(self.failed(self.run_([plan("a"), plan("a")])), ["one_plan_row_per_person"])
        akc = [{"master_key": "a", "in_audience": True, "send_date": "2026-10-06", "excluded_reason": None}]
        self.assertEqual(self.failed(self.run_([plan("a")], akc)), ["personal_letter_and_akcija_same_week"])

    def test_a_gated_letter_is_not_a_hard_failure(self):
        out = self.run_([plan("a", "winback_1", gate="NO_OFFER_VALID_UNTIL", offer_rung=1)])
        self.assertEqual(self.failed(out), [])
        self.assertIn("winback_1 | NO_OFFER_VALID_UNTIL", out["count_blocked_by_gate"]["detail"])

    def test_stale_inputs_and_open_seams_are_warnings_only(self):
        out = self.run_([plan("a")], ages_h={"rung_price": 40, "goods_run_days": 4}, writer_missing=["ANKETA_URL"],
                        map_disagreements=[("lost_quarterly", 234, None)])
        self.assertEqual(self.failed(out), [])
        w = self.failed(out, "warn")
        for name in ("fresh_rung_price", "goods_run_not_older_than_1_day", "writer_fields_present",
                     "template_map_agrees_with_interface_v2", "fresh_pd_persons"):
            self.assertIn(name, w)

    def test_diff_vs_yesterday_names_the_groups_that_moved(self):
        prev = [{"email_type": "lost_quarterly", "would_send": False, "hold_reason": "NO_TEMPLATE_IN_MAP", "n": 2}]
        n, rows = SC.diff([plan("a", "lost_quarterly"), plan("b", "lost_quarterly")], prev)
        self.assertEqual(n, 2)
        self.assertEqual({(r["group"], r["delta"]) for r in rows},
                         {("lost_quarterly | False | NO_TEMPLATE_IN_MAP", -2), ("lost_quarterly | True | None", 2)})


if __name__ == "__main__":
    unittest.main()
