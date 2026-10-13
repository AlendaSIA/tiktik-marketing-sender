import unittest
from unittest import mock

import flow_gate as FG

ENV = {"PROOF_RECIPIENT": "alenda.jurmala@gmail.com"}


class Q:
    def __init__(self, members=(), lists=()):
        self.calls, self.members, self.lists = [], list(members), list(lists)

    def __call__(self, s):
        self.calls.append(s)
        if s.startswith("SELECT list_name, list_id"):
            return self.lists
        if s.startswith(f"SELECT email FROM `{FG.T_MEMBERS}`"):
            key = s.split("list_name = '")[1].split("'")[0]
            return [{"email": e} for k, e in self.members if k == key]
        if s.startswith("INSERT INTO `" + FG.T_LISTS):
            return []
        if "FROM `" + FG.T_EVENTS in s and s.startswith("SELECT e.master_key"):
            return [{"master_key": "cid:1", "order_id": "M-1", "client": "SIA X"}]
        return []


class Brevo:
    def __init__(self, have=None):
        self.calls, self.have, self.next_id = [], {k: set(v) for k, v in (have or {}).items()}, 500

    def __call__(self, method, path, payload):
        self.calls.append((method, path, payload))
        if method == "POST" and path == "/contacts/lists":
            self.next_id += 1
            self.have[self.next_id] = set()
            return {"id": self.next_id}
        if method == "GET" and "/contacts?" in path:
            lid = int(path.split("/")[3])
            off = int(path.split("offset=")[1])
            return {"contacts": [{"email": e} for e in sorted(self.have.get(lid, set()))[off:off + 500]]}
        if path.endswith("/contacts/add"):
            lid = int(path.split("/")[3])
            ok = [e for e in payload["emails"] if not e.startswith("ghost")]
            self.have[lid] |= set(ok)
            return {"contacts": {"success": ok, "failure": [e for e in payload["emails"] if e.startswith("ghost")]}}
        if path.endswith("/contacts/remove"):
            self.have[int(path.split("/")[3])] -= set(payload["emails"])
            return {}
        if path == "/smtp/email":
            return {"messageId": "<m@x>"}
        raise AssertionError((method, path))


class FlowGate(unittest.TestCase):
    def test_build_runs_the_gate_on_the_given_run(self):
        q = Q()
        FG.build(q, "run-x")
        self.assertTrue(any("WHERE p.run_id = 'run-x'" in c for c in q.calls))
        self.assertFalse(any("@run_id" in c for c in q.calls))

    def test_lists_are_created_once_and_synced_add_remove(self):
        q = Q(members=[("NO_PROMO", "a@x.lv"), ("NO_PROMO", "ghost@x.lv"), ("NO_ANY", "a@x.lv")],
              lists=[{"list_name": "NO_PROMO", "list_id": 7}])
        b = Brevo(have={7: {"old@x.lv"}})
        res = FG.sync_lists(q, b)
        self.assertEqual(res["NO_PROMO"], {"list_id": 7, "wanted": 2, "added": 1, "removed": 1,
                                           "not_a_brevo_contact": 1, "refused_by_brevo": 0, "members_now": 1})
        self.assertEqual(res["NO_ANY"]["list_id"], 501)
        self.assertEqual(len([c for c in b.calls if c[1] == "/contacts/lists"]), 1)
        self.assertFalse([c for c in b.calls if "emailCampaigns" in c[1] or c[1] == "/smtp/email"])

    def test_one_bad_address_does_not_sink_the_chunk(self):
        class B(Brevo):
            def __call__(self, method, path, payload):
                if path.endswith("/contacts/add") and any(e.startswith("bad") for e in payload["emails"]):
                    raise RuntimeError("400 invalid")
                return super().__call__(method, path, payload)
        q = Q(members=[("NO_ANY", "a@x.lv"), ("NO_ANY", "bad@x"), ("NO_ANY", "c@x.lv")],
              lists=[{"list_name": "NO_PROMO", "list_id": 7}, {"list_name": "NO_ANY", "list_id": 8}])
        b = B(have={7: set(), 8: set()})
        res = FG.sync_lists(q, b)["NO_ANY"]
        self.assertEqual((res["added"], res["refused_by_brevo"], res["members_now"]), (2, 1, 2))

    def test_notify_stays_off_without_the_flag(self):
        b = Brevo()
        self.assertEqual(FG.notify(Q(), b, ENV)["sent"], 0)
        self.assertEqual(b.calls, [])

    def test_notify_goes_only_to_the_proof_recipient(self):
        b, q = Brevo(), Q()
        res = FG.notify(q, b, dict(ENV, FLOW_GATE_NOTIFY="on"))
        self.assertEqual(res, {"to": "alenda.jurmala@gmail.com", "sent": 1})
        self.assertEqual(b.calls[0][2]["to"], [{"email": "alenda.jurmala@gmail.com"}])
        self.assertIn("SIA X no manuālās plūsmas nopirka veikalā (pasūtījums M-1), pārlikts uz Veikals",
                      b.calls[0][2]["textContent"])

    def test_notify_test_refuses_without_a_recipient(self):
        import send_path as SP
        with self.assertRaises(SP.SendLocked):
            FG.notify_test(Brevo(), {})
        r = FG.notify_test(Brevo(), ENV)
        self.assertEqual(r["to"], "alenda.jurmala@gmail.com")


if __name__ == "__main__":
    unittest.main()


class LiveGate(unittest.TestCase):
    """MAIN 11.10 11:33 F2/F4: the gate in the live plan (sequence.py), the akcija audience and L9."""

    def setUp(self):
        import sequence as S
        self.S = S

    def test_manual_gets_only_order_letters(self):
        S = self.S
        for et in ("reorder_1", "winback_1", "lost_quarterly", S.XSELL):
            self.assertEqual(S.flow_gate_hold(et, None, None, "MANUAL_P11"), "MANUAL_P11")
            self.assertEqual(S.flow_gate_hold(et, None, "B2B", "MANUAL_P11"), "MANUAL_P11")
        self.assertIsNone(S.flow_gate_hold(S.PP1, None, None, "MANUAL_P11"))
        self.assertIsNone(S.flow_gate_hold(S.PP1, None, "B2B", "MANUAL_P11"))      # F2 even for a B2B manual
        self.assertEqual(S.flow_gate_hold(S.PP1, "SUPPRESSED", "B2B", "MANUAL_P11"), "SUPPRESSED")
        self.assertEqual(S.language_hold("reorder_1", "MANUAL_P11", True), "MANUAL_P11")
        self.assertEqual(S.buyer_hold("reorder_1", "MANUAL_P11", False), "MANUAL_P11")

    def test_b2b_by_either_source_and_g4_moves_to_shop(self):
        S = self.S
        for g in ("B2B_EDU_ONLY", "B2B_NO_SHOP", "B2B_SHOP_UNKNOWN"):
            self.assertEqual(S.flow_gate_hold("reorder_1", None, None, g), "B2B_FLOW")
            self.assertEqual(S.flow_gate_hold(S.PP1, None, None, g), "B2B_FLOW")       # unchanged for B2B
        self.assertEqual(S.flow_gate_hold("reorder_1", None, "B2B", "NONE"), "B2B_FLOW")
        self.assertEqual(S.flow_gate_hold("reorder_1", None, "B2B", None), "B2B_FLOW")
        self.assertEqual(S.flow_gate_hold("reorder_1", None, "LEAD", "NONE"), "LEAD_FLOW")
        self.assertIsNone(S.flow_gate_hold("reorder_1", None, "B2B", "MANUAL_TO_SHOP"))
        self.assertIsNone(S.flow_gate_hold("reorder_1", None, None, "NONE"))

    def test_akcija_reasons(self):
        import sequence_job as J
        wk = J.akcija_week(__import__("datetime").date(2026, 10, 12))
        row = lambda **k: J.akcija_row(mk="m", email="a@b.lv", stage="active", suppressed=False, is_en=False,  # noqa: E731
                                       plan=None, week=wk, **k)
        self.assertEqual(row(flow=None, gate="MANUAL_P11")["excluded_reason"], "MANUAL_P11")
        self.assertEqual(row(flow="B2B", gate="B2B_EDU_ONLY")["excluded_reason"], "B2B_EDU_ONLY")
        self.assertEqual(row(flow=None, gate="B2B_EDU_ONLY")["excluded_reason"], "B2B_EDU_ONLY")
        self.assertEqual(row(flow=None, gate="B2B_NO_SHOP")["excluded_reason"], "B2B_FLOW")
        self.assertEqual(row(flow="B2B", gate=None)["excluded_reason"], "B2B_FLOW")
        self.assertTrue(row(flow="B2B", gate="MANUAL_TO_SHOP")["in_audience"])
        self.assertTrue(row(flow=None, gate="NONE")["in_audience"])

    def test_send_time_l9(self):
        S = self.S
        self.assertEqual(S.gate_person_block("reorder_1", None, "MANUAL_P11"), "MANUAL_P11")
        self.assertIsNone(S.gate_person_block(S.PP1, "B2B", "MANUAL_P11"))
        self.assertEqual(S.gate_person_block("reorder_1", None, "B2B_EDU_ONLY"), "B2B_FLOW")
        self.assertIsNone(S.gate_person_block("reorder_1", "B2B", "MANUAL_TO_SHOP"))
        self.assertIsNone(S.gate_person_block("reorder_1", None, "NONE"))

    def test_core_refuses_an_empty_gate(self):
        with self.assertRaises(RuntimeError):
            FG.check_core({}, {})
        with self.assertRaises(RuntimeError):
            FG.check_core({"NONE": 5}, {"m": "NONE"})
        FG.check_core({"B2B_NO_SHOP": 1}, {"m": "B2B_NO_SHOP"})
        q = Q()
        FG.build_core(q)
        self.assertTrue(q.calls[0].startswith("-- FLOW GATE"))
        self.assertTrue(any("flow_gate_list_members" in c for c in q.calls))
        self.assertFalse(any("flow_gate_plan_shadow" in c for c in q.calls))

    def test_planner_wiring_and_no_brevo_import(self):
        import os
        src = open(os.path.join(os.path.dirname(FG.__file__), "sequence_job.py")).read()
        self.assertLess(src.index("FG.build_core("), src.index("for f in facts:"))
        self.assertLess(src.index("FG.check_core("), src.index("for f in facts:"))
        fsrc = open(FG.__file__).read().split("def _prod")[0]
        for bad in ("\nimport campaign", "\nimport brevo", "\nfrom campaign", "\nimport send_day"):
            self.assertNotIn(bad, fsrc)


class AkcijaExclusion(unittest.TestCase):
    """MAIN 11.10 12:55 TUESDAY CONTENT 2: (a) INFO slot, (b) reorder_1 that day, (c) cold B2B leads in list 3."""

    def test_sql_has_the_three_sources_for_the_date(self):
        s = FG.excl_sql("2026-10-13")
        for must in ("send_outcome", "outcome = 'SENT'", "send_date = DATE '2026-10-13'", "l.is_lead",
                     FG.T_L3LIVE, "'b_personal_'", "'c_cold_b2b_lead'", "'d_quiet_never_buyer'"):
            self.assertIn(must, s)
        self.assertNotIn("a_info_slot", s)                                    # D47: INFO never on a Tuesday
        self.assertNotIn("edu_brevo_list", s)                                 # D47: live list 3 only
        self.assertNotIn("email_type = 'reorder_1'", s)                       # D46: every personal type, not one
        f = FG.excl_sql("2026-10-13", forecast=True)
        self.assertIn("'D1 TRACK'", f)
        self.assertIn("DRY_WOULD_SEND", f)

    def test_d46_no_send_day_run_no_list(self):
        q = mock.Mock(return_value=[])
        with self.assertRaises(FG.NoSendDayRun):
            FG.build_excl(q, "2026-10-13")
        self.assertEqual(q.call_count, 1)                                   # nothing built, nothing deleted

    def test_sync_one_adds_and_removes_and_the_list_is_its_own(self):
        b = Brevo(have={501: {"old@x.lv"}})
        q = Q(lists=[{"list_name": FG.EXCL_KEY, "list_id": 501}])
        q.excl = ["new@x.lv"]
        orig = q.__call__

        def call(s):
            if s.startswith(f"SELECT email FROM `{FG.T_EXCL}`"):
                return [{"email": e} for e in q.excl]
            return orig(s)
        r = FG.sync_excl(call, b, "2026-10-13")
        self.assertEqual(r["list_id"], 501)
        self.assertEqual(b.have[501], {"new@x.lv"})
        self.assertNotIn(FG.EXCL_KEY, FG.LIST_NAMES)        # never synced by the daily 96/97 step


class QuietPartD(unittest.TestCase):
    def test_d_is_in_the_exclusion_and_the_rule_reads_clicks_and_real_opens(self):
        self.assertIn("'d_quiet_never_buyer'", FG.excl_sql("2026-10-13"))
        for must in ("total_opens, 0) > IFNULL(apple_mpp_opens", "clicked_links_count", "legacy_paytraq.client_map",
                     "payment_status = 'paid'", "event = 'clicks'"):
            self.assertIn(must, FG.QUIET_SQL)


class ReplanKeepsLaterRows(unittest.TestCase):
    def test_planner_deletes_only_its_own_check_rows(self):
        import os
        src = open(os.path.join(os.path.dirname(FG.__file__), "sequence_job.py")).read()
        self.assertIn("NOT STARTS_WITH(check_name, 'akcija_asm_')", src)
        self.assertIn("NOT STARTS_WITH(check_name, 'sendtime_')", src)
