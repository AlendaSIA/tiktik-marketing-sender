"""Block 1 (MAIN 2026-10-09 13:07): the send day of the NEW plan - D1 standing approval, D2 Tuesday/Thursday,
D3 cap (default 0), the frozen-clean member selection, DRY touches nothing in Brevo, LIVE goes list -> campaign ->
send_path.dispatch -> send_log / PD / state."""
import datetime as dt
import hashlib
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import presend  # noqa: E402
import send_day as SD  # noqa: E402
import send_path as SP  # noqa: E402

TUE = dt.date(2026, 10, 13)
NOW = dt.datetime(2026, 10, 13, 7, 30, tzinfo=dt.timezone.utc)          # 10:30 Riga, Tuesday
HTML = "<p>Laiks papildināt {{ contact.P1_NAME }}</p>"
SHA = hashlib.sha256(HTML.encode()).hexdigest()
TRACKS = [{"track": "reorder", "enabled": True, "enabled_by": "Raivis"}]
APPR = [{"template_id": 179, "email_type": "reorder_1", "approved": True, "approved_by": "Raivis", "approved_sha256": SHA}]
LIVE_ENV = {"PROOF_RECIPIENT": "alenda.jurmala@gmail.com", "DRY_RUN": "false", "ALLOW_SEND": "true", "GLOBAL_SEND_ENABLED": "true"}
DRY_ENV = {"PROOF_RECIPIENT": "alenda.jurmala@gmail.com", "DRY_RUN": "true", "ALLOW_SEND": "false", "GLOBAL_SEND_ENABLED": "false"}


def cand(i, due="2026-10-01"):
    return {"master_key": f"m{i}", "email": f"p{i}@x.lv", "email_type": "reorder_1", "template_id": 179,
            "track": "reorder", "rung": 0, "planned_send_date": "2026-10-13", "due_since": due,
            "person_id": 100 + i if i % 2 else None, "reason": "r", "offer_valid_until": None}


class Q:
    def __init__(self, cands, caps=(), tracks=TRACKS, appr=APPR, lf=None):
        self.cands, self.caps, self.tracks, self.appr, self.calls = cands, list(caps), tracks, appr, []
        self.lf = lf if lf is not None else {c["email"]: {"email": c["email"], "P1_NAME": "Cimdi", "OFFER_RUNG": 0}
                                             for c in cands}

    def __call__(self, sql):
        self.calls.append(sql)
        s = sql.strip()
        if s.startswith(("CREATE", "INSERT", "UPDATE", "ALTER")):
            return []
        if "CAST(planned_send_date AS STRING) AS planned_send_date, would_send, hold_reason" in s:
            return [{**c, "would_send": True, "hold_reason": None} for c in self.cands] + getattr(self, "held", [])
        if "GROUP BY run_id, plan_date" in s:
            return [{"run_id": "plan-1", "plan_date": "2026-10-13"}]
        if f"`{SD.T_CAP}`" in s:
            return self.caps
        if f"`{SD.T_TRACK}`" in s:
            return self.tracks
        if f"`{SD.T_APPROVAL}`" in s:
            return self.appr
        if "COUNT(*) AS n" in s:
            return [{"email_type": "reorder_1", "template_id": 179, "track": "reorder", "n": len(self.cands)}]
        if s.startswith("WITH p AS"):
            return self.cands
        if f"`{SD.T_SUPP}`" in s:
            return []
        if f"`{SD.T_LF}`" in s:
            return list(self.lf.values())
        if f"`{SD.T_PROOF}`" in s:
            return getattr(self, "proof_rows", [])
        raise AssertionError("unexpected SQL " + s[:80])

    def inserts(self, table):
        return [c for c in self.calls if c.startswith(f"INSERT INTO `{table}`")]


class WH:
    def __init__(self, blocks=None):
        self.blocks = blocks or {}

    def presend_ctx(self, campaign, d, mks):
        ok = presend.Ctx(letter_fields=True, template_id=179, template_approved=True)
        return {m: ok for m in mks}

    def person_blocks(self, d, mks):
        return {m: v for m, v in self.blocks.items() if m in mks}

    def lf_rows(self, d):
        return {}

    def plan_rows(self, d):
        return {}

    def letter_fields(self, d):
        return {"status": "OK", "plan_run_id": "plan-1", "run_id": "lf-1"}

    def plan_run(self, d):
        return "plan-1"


class Brevo:
    def __init__(self, attrs=None, html=HTML):
        self.calls, self.attrs, self.html, self.lists = [], attrs, html, {}

    def __call__(self, method, path, payload):
        self.calls.append((method, path))
        if method == "GET" and path == "/contacts/attributes":
            return {"attributes": [{"name": "P1_NAME", "category": "normal", "type": "text"},
                                   {"name": "OFFER_RUNG", "category": "normal", "type": "float"}]}
        if method == "GET" and path.startswith("/smtp/templates/"):
            return {"htmlContent": self.html, "subject": "Laiks papildināt krājumus?"}
        if method == "GET" and path.startswith("/contacts/lists/") and "/contacts?" in path:
            return {"contacts": [{"email": e} for e in self.lists.get(int(path.split("/")[3]), [])]}
        if method == "GET" and path.startswith("/contacts/lists/"):
            return {"uniqueSubscribers": len(self.lists.get(int(path.split("/")[3]), []))}
        if method == "GET" and path.startswith("/contacts/"):
            return {"attributes": self.attrs if self.attrs is not None else {"P1_NAME": "Cimdi", "OFFER_RUNG": 0}}
        if method == "POST" and path == "/contacts/lists":
            self.lists[77] = []
            return {"id": 77}
        if method == "POST" and path.endswith("/contacts/add"):
            self.lists[int(path.split("/")[3])] += payload["emails"]
            return {"contacts": {"success": payload["emails"]}}
        if method == "POST" and path == "/emailCampaigns":
            self.campaign = payload
            return {"id": 900}
        if method == "GET" and path.startswith("/emailCampaigns/"):
            return {"subject": self.campaign["subject"], "htmlContent": self.campaign["htmlContent"], "status": "sent",
                    "statistics": {"globalStats": {"sent": 1}}}
        if method == "PUT" and path.startswith("/contacts/"):
            self.attrs = dict(self.attrs or {}, **payload["attributes"])
            self.puts = getattr(self, "puts", []) + [(path, payload)]
            return {}
        if method == "POST" and path.endswith("/sendNow"):
            return {}
        raise AssertionError((method, path))

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


def day(q, brevo=None, env=DRY_ENV, wh=None, pd=None, dash=None):
    return SD.Day(q, wh or WH(), brevo or Brevo(), pd or mock.Mock(return_value=1), dash or mock.Mock(return_value=True),
                  env, now=NOW)


class PureRules(unittest.TestCase):
    def test_d2_tuesday_for_commercial(self):
        self.assertTrue(SD.is_send_day("reorder_1", TUE))
        self.assertFalse(SD.is_send_day("reorder_1", TUE + dt.timedelta(days=3)))
        self.assertEqual(SD.send_weekday("winback_1"), SD.TUESDAY)

    def test_d3_cap_default_zero_latest_row_wins(self):
        self.assertEqual(SD.cap_of([], "reorder_1", TUE), 0)
        rows = [{"send_date": "2026-10-13", "email_type": "reorder_1", "cap": 5, "set_at": "2026-10-10 10:00"},
                {"send_date": "2026-10-13", "email_type": "reorder_1", "cap": 20, "set_at": "2026-10-12 10:00"},
                {"send_date": "2026-10-13", "email_type": "winback_1", "cap": 99, "set_at": "2026-10-12 10:00"}]
        self.assertEqual(SD.cap_of(rows, "reorder_1", TUE), 20)
        self.assertEqual(SD.cap_of(rows, "reorder_1", TUE + dt.timedelta(days=7)), 0)

    def test_d1_all_three_conditions(self):
        h = lambda t: SHA  # noqa: E731
        self.assertEqual(SD.standing_approval("reorder_1", "reorder", 179, TRACKS, APPR, h), [])
        bad = SD.standing_approval("reorder_1", "reorder", 179, [{"track": "reorder", "enabled": False,
                                                                   "enabled_by": "Raivis"}], APPR, h)
        self.assertTrue(bad[0].startswith("TRACK_NOT_ENABLED_BY_RAIVIS"))
        self.assertTrue(SD.standing_approval("reorder_1", "reorder", 179, [dict(TRACKS[0], enabled_by="MAIN")],
                                             APPR, h)[0].startswith("TRACK_"))
        self.assertTrue(SD.standing_approval("reorder_1", "reorder", 179, [], APPR, h)[0].startswith("TRACK_"))
        self.assertTrue(SD.standing_approval("winback_1", "reorder", 179, TRACKS, APPR, h)[0].startswith("NO_APPROVAL"))
        self.assertTrue(SD.standing_approval("reorder_1", "reorder", 179, TRACKS, [dict(APPR[0], approved_by="MAIN")],
                                             h)[0].startswith("NO_APPROVAL"))
        self.assertTrue(SD.standing_approval("reorder_1", "reorder", 179, TRACKS, APPR,
                                             lambda t: "0" * 64)[0].startswith("TEMPLATE_HASH_CHANGED"))

        def boom(t):
            raise RuntimeError("brevo down")
        self.assertTrue(SD.standing_approval("reorder_1", "reorder", 179, TRACKS, APPR, boom)[0]
                        .startswith("TEMPLATE_HASH_CHANGED"))
        self.assertTrue(SD.standing_approval("reorder_1", "reorder", 179, TRACKS,
                                             [dict(APPR[0], approved_sha256=None)], h)[0].startswith("APPROVAL_HASH"))

    def test_selection_oldest_first_clean_only_cap_and_no_reads_past_cap(self):
        seen = []

        def refuse(c):
            seen.append(c["master_key"])
            return "L9_B2B_FLOW" if c["master_key"] == "m2" else None
        chosen, dec = SD.select_audience([cand(i) for i in range(1, 7)], 3, refuse)
        self.assertEqual([c["master_key"] for c in chosen], ["m1", "m3", "m4"])
        self.assertEqual(dec["m2"], "L9_B2B_FLOW")
        self.assertEqual(dec["m5"], "OVER_CAP")
        self.assertEqual(seen, ["m1", "m2", "m3", "m4"])
        self.assertEqual(SD.select_audience([cand(1)], 0, refuse)[1], {"m1": "OVER_CAP"})

    def test_l11_attrs_equal(self):
        self.assertEqual(SD.attrs_equal({"P1_NAME": "Cimdi", "OFFER_RUNG": 0, "email": "x"},
                                        {"P1_NAME": "Cimdi", "OFFER_RUNG": 0.0}), [])
        self.assertEqual(SD.attrs_equal({"P1_NAME": "Cimdi", "P1_PRICE": "9 €"}, {"P1_NAME": "Cimdi"}), ["P1_PRICE"])
        self.assertEqual(SD.attrs_equal({"P1_FRESH": True, "P2_NAME": None}, {"P1_FRESH": True, "P2_NAME": ""}), [])

    def test_l1_needs_all_three_env(self):
        self.assertTrue(SD.l1_open(LIVE_ENV))
        for k, v in (("DRY_RUN", "true"), ("ALLOW_SEND", "false"), ("GLOBAL_SEND_ENABLED", "false")):
            self.assertFalse(SD.l1_open(dict(LIVE_ENV, **{k: v})))
        self.assertFalse(SD.l1_open({}))


class DryRun(unittest.TestCase):
    def test_dry_with_caps_override_touches_nothing_in_brevo_and_records(self):
        q, b = Q([cand(i) for i in range(1, 5)]), Brevo()
        res = day(q, b).run(TUE, caps_override={"reorder_1": 2})
        t = res["types"][0]
        self.assertEqual(res["mode"], "dry")
        self.assertEqual(t["chosen"], 2)
        self.assertEqual(t["refused"], {"OVER_CAP": 2})
        self.assertEqual(b.writes(), [])
        self.assertEqual(len(q.inserts(SD.T_RUN)), 1)
        self.assertEqual(len(q.inserts(SD.T_AUD)), 1)
        self.assertEqual(q.inserts(SD.T_LOG), [])

    def test_dry_without_cap_row_not_enabled_is_d3_zero(self):
        q = Q([cand(1)], tracks=[])
        t = day(q).run(TUE)["types"][0]
        self.assertTrue(t["type_refusal"].startswith("D1"))
        self.assertEqual((t["cap"], t["cap_source"]), (0, None))              # no row + not enabled = nothing goes

    def test_fuse_enabled_type_without_row_gets_plan_plus_5pct(self):        # MAIN 2 12.10 15:40
        self.assertEqual((SD.fuse_cap(469), SD.fuse_cap(20), SD.fuse_cap(0), SD.fuse_cap(100)), (493, 21, 0, 105))
        q = Q([cand(i) for i in range(1, 4)])
        t = day(q).run(TUE)["types"][0]
        self.assertEqual((t["cap"], t["cap_source"], t["chosen"]), (4, "fuse_plan_plus_5pct", 3))
        self.assertEqual(q.inserts(SD.T_CAP), [])                             # DRY writes no cap row
        q2 = Q([cand(1)], caps=[{"send_date": "2026-10-13", "email_type": "reorder_1", "cap": 0, "set_at": "x"}])
        t2 = day(q2).run(TUE)["types"][0]                                     # an explicit row (even 0) wins
        self.assertEqual((t2["cap"], t2["cap_source"], t2["type_refusal"]), (0, None, "D3 NO_CAP (0)"))

    def test_fuse_is_written_on_a_live_run(self):
        q, b = Q([cand(1), cand(2)]), Brevo()
        with mock.patch.object(SD.time, "sleep"):
            t = day(q, b, env=LIVE_ENV, pd=mock.Mock(return_value=5)).run(TUE)["types"][0]
        rows = q.inserts(SD.T_CAP)
        self.assertEqual(len(rows), 1)
        self.assertIn("'reorder_1', 3, 'engine_fuse'", rows[0])
        self.assertEqual((t["cap"], t["chosen"]), (3, 2))

    def test_not_a_send_day(self):
        q = Q([cand(1)])
        t = day(q).run(TUE + dt.timedelta(days=3), caps_override={"reorder_1": 5})["types"][0]
        self.assertTrue(t["type_refusal"].startswith("NOT_A_SEND_DAY"))

    def test_dry_d1_refusal_still_shows_who_would_get_it_no_dash(self):
        q, dash = Q([cand(1)], tracks=[]), mock.Mock()
        t = day(q, dash=dash).run(TUE, caps_override={"reorder_1": 5})["types"][0]
        self.assertTrue(t["type_refusal"].startswith("D1 TRACK_NOT_ENABLED"))
        self.assertEqual(t["chosen"], 1)
        dash.assert_not_called()

    def test_l11_and_l9_refusals_by_reason(self):
        q = Q([cand(1), cand(2), cand(3)])
        b = Brevo(attrs={"P1_NAME": "cits"})
        t = day(q, b, wh=WH(blocks={"m2": "B2B_FLOW"})).run(TUE, caps_override={"reorder_1": 5})["types"][0]
        self.assertEqual(t["refused"], {"L11_LETTER_FIELDS_NOT_IN_BREVO": 2, "L9_B2B_FLOW": 1})
        self.assertEqual(t["chosen"], 0)

    def test_caps_override_refused_in_live(self):
        with self.assertRaises(SP.SendLocked):
            day(Q([cand(1)]), env=LIVE_ENV).run(TUE, caps_override={"reorder_1": 1})


class Live(unittest.TestCase):
    def test_live_full_path(self):
        q, b, pd = Q([cand(1), cand(2), cand(3)], caps=[{"send_date": "2026-10-13", "email_type": "reorder_1",
                                                         "cap": 2, "set_at": "x"}]), Brevo(), mock.Mock(return_value=5)
        with mock.patch.object(SD.time, "sleep"):
            res = day(q, b, env=LIVE_ENV, pd=pd).run(TUE)
        t = res["types"][0]
        self.assertEqual(res["mode"], "live")
        self.assertEqual((t["chosen"], t["sent"], t["campaign_id"], t["list_id"]), (2, 2, 900, 77))
        self.assertEqual(b.lists[77], ["p1@x.lv", "p2@x.lv"])
        self.assertEqual(b.campaign["recipients"], {"listIds": [77], "exclusionListIds": [4]})
        self.assertEqual(b.campaign["htmlContent"], HTML)
        self.assertIn(("POST", "/emailCampaigns/900/sendNow"), b.calls)
        self.assertEqual(len(q.inserts(SD.T_LOG)), 1)
        self.assertIn("'engine_live'", q.inserts(SD.T_LOG)[0])
        self.assertEqual(pd.call_count, 1)                      # only m1 has a Pipedrive person
        self.assertEqual(len(q.inserts(SD.T_ADV)), 2)

    def test_live_d1_fail_with_cap_posts_kluda_and_sends_nothing(self):
        q, b, dash = Q([cand(1)], caps=[{"send_date": "2026-10-13", "email_type": "reorder_1", "cap": 2,
                                         "set_at": "x"}]), Brevo(html="<p>changed</p>"), mock.Mock()
        t = day(q, b, env=LIVE_ENV, dash=dash).run(TUE)["types"][0]
        self.assertTrue(t["type_refusal"].startswith("D1 TEMPLATE_HASH_CHANGED"))
        self.assertEqual(b.writes(), [])
        dash.assert_called_once()
        self.assertEqual(dash.call_args[0][0], "KĻŪDA")

    def test_live_without_cap_posts_nothing(self):
        q, b, dash = Q([cand(1)], tracks=[]), Brevo(), mock.Mock()
        t = day(q, b, env=LIVE_ENV, dash=dash).run(TUE)["types"][0]
        self.assertTrue(t["type_refusal"].startswith("D1"))
        dash.assert_not_called()
        self.assertEqual(b.writes(), [])

    def test_placeholder_in_campaign_refuses_before_send(self):
        q = Q([cand(1)], caps=[{"send_date": "2026-10-13", "email_type": "reorder_1", "cap": 1, "set_at": "x"}])
        b = Brevo(html="<p>⟦TEMA⟧</p>")
        appr = [dict(APPR[0], approved_sha256=hashlib.sha256("<p>⟦TEMA⟧</p>".encode()).hexdigest())]
        q.appr = appr
        with mock.patch.object(SD.time, "sleep"), self.assertRaises(SP.SendLocked):
            day(q, b, env=LIVE_ENV).run(TUE)
        self.assertNotIn(("POST", "/emailCampaigns/900/sendNow"), b.calls)


class Proof(unittest.TestCase):
    def test_proof_sends_one_mail_to_raivis_only_with_a_real_members_letter(self):
        q = Q([cand(1), cand(2)], tracks=[])                          # no track enabled: a proof does not need one
        b, pd = Brevo(attrs={}), mock.Mock(return_value=4711)
        with mock.patch.object(SD.time, "sleep"):
            res = day(q, b, env=LIVE_ENV, pd=pd).proof("reorder_1", TUE, 99)
        self.assertEqual(b.lists[77], ["alenda.jurmala@gmail.com"])
        self.assertEqual(b.puts[0], ("/contacts/alenda.jurmala@gmail.com", {"attributes": {"P1_NAME": "Cimdi", "OFFER_RUNG": 0.0}}))
        self.assertEqual(res["source_master_key"], "m1")
        self.assertEqual((res["sent"], res["pd_activity_ids"], res["brevo_status"]), (1, [4711], "sent"))
        self.assertEqual(pd.call_args[0][0]["target_person_id"], 99)
        log = q.inserts(SD.T_LOG)[0]
        self.assertIn("'TEST:alenda.jurmala@gmail.com'", log)
        self.assertNotIn("'m1'", log)                                  # no customer's history moves
        self.assertIn("'TEST:alenda.jurmala@gmail.com'", q.inserts(SD.T_ADV)[0])
        self.assertEqual(res["unfilled"], [])

    def test_proof_needs_l1_and_the_approved_hash(self):
        with self.assertRaises(SP.SendLocked):
            day(Q([cand(1)]), env=DRY_ENV).proof("reorder_1", TUE, 99)
        with mock.patch.object(SD.time, "sleep"), self.assertRaises(SP.SendLocked) as e:
            day(Q([cand(1)]), Brevo(html="<p>other</p>"), env=LIVE_ENV).proof("reorder_1", TUE, 99)
        self.assertIn("TEMPLATE_HASH_CHANGED", str(e.exception))

    def test_proof_campaign_carries_the_suppression_exclusion(self):
        b = Brevo(attrs={})
        with mock.patch.object(SD.time, "sleep"):
            day(Q([cand(1)], tracks=[]), b, env=LIVE_ENV, pd=mock.Mock(return_value=1)).proof("reorder_1", TUE, 99)
        self.assertEqual(b.campaign["recipients"], {"listIds": [77], "exclusionListIds": [4]})
        self.assertTrue(b.campaign["name"].startswith("TEST proof reorder_1"))

    def test_proof_dry_makes_no_brevo_write_and_reports_l5(self):
        b = Brevo(attrs={})
        res = day(Q([cand(1)], tracks=[]), b, env=DRY_ENV).proof_dry("reorder_1", TUE)
        self.assertEqual((res["L5"], res["brevo_writes"], b.writes()), ("PASS", 0, []))
        self.assertEqual(res["planned_campaign"]["recipients"]["exclusionListIds"], [4])
        self.assertEqual(res["source_master_key"], "m1")

    def test_restore_clears_keys_that_had_no_value_with_empty_string(self):
        q = Q([cand(1)])
        q.proof_rows = [{"run_id": "r1", "raivis_before": '{"P1_NAME": "Vecais"}',
                         "written": '{"P1_NAME": "Cimdi", "OFFER_RUNG": 0.0}',
                         "recipient": "alenda.jurmala@gmail.com"}]
        b = Brevo(attrs={"P1_NAME": "Cimdi", "OFFER_RUNG": 0.0})
        res = day(q, b, env=LIVE_ENV).proof_restore(["r1"])
        self.assertEqual(b.puts[-1][1], {"attributes": {"P1_NAME": "Vecais", "OFFER_RUNG": ""}})
        self.assertEqual(res["cleared"], ["OFFER_RUNG"])

    def test_draft_test_creates_and_never_sends(self):
        b = Brevo()
        b.lists[90] = ["alenda.jurmala@gmail.com"]
        res = day(Q([cand(1)]), b, env=LIVE_ENV).draft_test("reorder_1", TUE, 90)
        self.assertEqual(res["campaign_id"], 900)
        self.assertTrue(b.campaign["name"].startswith("TEST draft reorder_1"))
        self.assertFalse(any(p.endswith("/sendNow") for _m, p in b.calls))
        with self.assertRaises(SP.SendLocked):
            day(Q([cand(1)]), b, env=DRY_ENV).draft_test("reorder_1", TUE, 90)

    def test_proof_refuses_without_one_configured_recipient(self):
        for bad in ({}, {"PROOF_RECIPIENT": ""}, {"PROOF_RECIPIENT": "a@x.lv,b@x.lv"}):
            env = {k: v for k, v in LIVE_ENV.items() if k != "PROOF_RECIPIENT"}
            env.update(bad)
            b = Brevo(attrs={})
            with mock.patch.object(SD.time, "sleep"), self.assertRaises(SP.SendLocked):
                day(Q([cand(1)], tracks=[]), b, env=env).proof("reorder_1", TUE, 99)
            self.assertEqual(b.writes(), [])

    def test_proof_never_writes_under_the_recipients_own_master_key(self):
        own = dict(cand(2), master_key="cid:1256385", email="alenda.jurmala@gmail.com")
        q, b, pd = Q([own, cand(1)], tracks=[]), Brevo(attrs={}), mock.Mock(return_value=4711)
        with mock.patch.object(SD.time, "sleep"):
            res = day(q, b, env=LIVE_ENV, pd=pd).proof("reorder_1", TUE, 99)
        writes = [c for c in q.calls if c.lstrip().startswith(("INSERT", "UPDATE")) and "send_day_audience" not in c]
        self.assertTrue(writes)
        self.assertFalse([w for w in writes if "cid:1256385" in w and "send_day_proof" not in w])
        self.assertIn("'TEST:alenda.jurmala@gmail.com'", q.inserts(SD.T_LOG)[0])
        self.assertIn("WHERE master_key = 'TEST:alenda.jurmala@gmail.com'",
                      [c for c in q.calls if c.startswith(f"UPDATE `{SD.T_STATE}`")][0])
        self.assertEqual(pd.call_args[0][0]["target_person_id"], 99)
        self.assertIn("master_key: TEST:alenda.jurmala@gmail.com", pd.call_args[0][0]["note"])
        self.assertEqual(res["sent"], 1)

    def test_restore_uses_only_the_earliest_runs_before_values(self):
        q = Q([cand(1)])
        q.proof_rows = [
            {"run_id": "r1", "raivis_before": '{"P1_NAME": "Mans"}', "written": '{"P1_NAME": "A", "P2_NAME": "B"}',
             "recipient": "alenda.jurmala@gmail.com"},
            {"run_id": "r2", "raivis_before": '{"P1_NAME": "A", "P2_NAME": "B"}',
             "written": '{"P1_NAME": "C", "P2_NAME": "D", "P3_NAME": "E"}', "recipient": "alenda.jurmala@gmail.com"}]
        b = Brevo(attrs={})
        day(q, b, env=LIVE_ENV).proof_restore(["r1", "r2"])
        self.assertEqual(b.puts[-1][1], {"attributes": {"P1_NAME": "Mans", "P2_NAME": "", "P3_NAME": ""}})

    def test_restore_refuses_runs_of_another_recipient(self):
        q = Q([cand(1)])
        q.proof_rows = [{"run_id": "old", "raivis_before": "{}", "written": "{}", "recipient": None}]
        with self.assertRaises(SP.SendLocked):
            day(q, Brevo(), env=LIVE_ENV).proof_restore(["old"])

    def test_unfilled_marks(self):
        self.assertEqual(SD.unfilled_marks("{{ contact.P1_NAME }} {{ contact.UZRUNA | default : 'Sveiki' }}",
                                           {"P1_NAME": ""}), ["P1_NAME"])
        self.assertEqual(SD.unfilled_marks("{{ contact.P1_NAME }} ⟦X⟧", {"P1_NAME": "a"}), ["⟦"])

    def test_live_stale_mirror_posts_kluda(self):
        q = Q([cand(1)], caps=[{"send_date": "2026-10-13", "email_type": "reorder_1", "cap": 2, "set_at": "x"}])
        dash = mock.Mock()
        day(q, Brevo(), env=LIVE_ENV, wh=WH(blocks={"m1": "PAID_ORDERS_SOURCE_MISSING"}), dash=dash).run(TUE)
        self.assertEqual(dash.call_args[0][0], "KĻŪDA")
        self.assertIn("60 min", dash.call_args[0][1])


class BrevoErrorBodyIsLogged(unittest.TestCase):
    def test_non_2xx_carries_status_endpoint_and_body_never_the_key(self):
        import io
        import urllib.error
        import campaign
        err = urllib.error.HTTPError("https://api.brevo.com/v3/emailCampaigns", 400, "Bad Request", {},
                                     io.BytesIO(b'{"code":"invalid_parameter","message":"sender is invalid"}'))
        with mock.patch.object(campaign, "api_key", return_value="SECRETKEY"), \
                mock.patch.object(campaign.urllib.request, "urlopen", side_effect=err), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as se, self.assertRaises(urllib.error.HTTPError) as e:
            campaign._call("POST", "/emailCampaigns", {"name": "x"})
        self.assertEqual(e.exception.code, 400)
        for text in (str(e.exception), se.getvalue()):
            self.assertIn("400 POST /emailCampaigns", text)
            self.assertIn("sender is invalid", text)
            self.assertNotIn("SECRETKEY", text)


class BrevoWritesRefusedWhileL1Closed(unittest.TestCase):
    def test_prod_brevo_wrapper_refuses_writes(self):
        with mock.patch.dict(os.environ, DRY_ENV, clear=False):
            import campaign
            with mock.patch.object(campaign, "_call") as call:
                q, wh, brevo, pd, dash = (None,) * 5
                # build only the brevo wrapper the way _prod does
                def brevo_w(method, path, payload):
                    if method != "GET" and not SD.l1_open(os.environ):
                        raise SP.SendLocked([("L1", "x")])
                    return campaign._call(method, path, payload)
                with self.assertRaises(SP.SendLocked):
                    brevo_w("POST", "/emailCampaigns", {})
                call.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class Outcome(unittest.TestCase):
    """MAIN 11.10 12:55: every planned letter of the date with what happened and why."""

    def test_outcome_rows(self):
        plan = [{"master_key": "a", "email": "a@x", "email_type": "reorder_1", "template_id": 179,
                 "planned_send_date": "2026-10-13", "would_send": True, "reason": "r"},
                {"master_key": "b", "email": "b@x", "email_type": "reorder_1", "template_id": 179,
                 "planned_send_date": "2026-10-13", "would_send": True, "reason": "r"},
                {"master_key": "c", "email": "c@x", "email_type": "reorder_1", "template_id": 179,
                 "planned_send_date": "2026-10-13", "would_send": False, "hold_reason": "B2B_FLOW", "reason": "r"},
                {"master_key": "d", "email": "d@x", "email_type": "winback_1", "template_id": 180,
                 "planned_send_date": "2026-10-20", "would_send": True, "reason": "w"},
                {"master_key": "e", "email": "e@x", "email_type": "winback_1", "template_id": 180,
                 "planned_send_date": "2026-10-13", "would_send": True, "reason": "w"}]
        types = [{"email_type": "reorder_1", "type_refusal": None, "cap": 1,
                  "decisions": {"a": "CHOSEN", "b": "OVER_CAP"}},
                 {"email_type": "winback_1", "type_refusal": "D3 NO_CAP (0)", "cap": 0,
                  "decisions": {"e": "OVER_CAP"}}]
        got = {r[0]: (r[5], r[6]) for r in SD.outcome_rows(plan, types, "2026-10-13", live=False)}
        self.assertEqual(got["a"], ("DRY_WOULD_SEND", None))
        self.assertEqual(got["b"], ("OVER_CAP", "cap 1"))
        self.assertEqual(got["c"], ("HELD_IN_PLAN", "B2B_FLOW"))
        self.assertEqual(got["d"][0], "NOT_DUE_YET")
        self.assertEqual(got["e"][0], "OVER_CAP")

    def test_dry_run_writes_the_outcome_table_and_send_log_carries_the_reason(self):
        q = Q([cand(1), cand(2)], caps=[{"send_date": "2026-10-13", "email_type": "reorder_1", "cap": 5,
                                          "set_at": "x"}])
        src = open(SD.__file__).read()
        self.assertIn("ADD COLUMN IF NOT EXISTS reason STRING", src)
        self.assertIn("utm_campaign, reason) VALUES", src)
        import send_path
        self.assertIn('"reason": a.get("reason") or None', open(send_path.__file__).read())


class BlocksD31(unittest.TestCase):
    """INTERFACE BLOCKS v1 (MAIN 11.10 17:05 tasks 4a/4b)."""
    BHTML = "<div>{{ contact.FLOW_HTML|safe }}</div>"

    class QB(Q):
        def __init__(self, *a, log_exists=True, rows=1, **k):
            super().__init__(*a, **k)
            self.log_exists, self.rows = log_exists, rows

        def __call__(self, sql):
            if "INFORMATION_SCHEMA.TABLES" in sql:
                self.calls.append(sql)
                return [{"x": 1}] if self.log_exists else []
            if "block_push_log" in sql:
                self.calls.append(sql)
                return [{"mode": "LIVE", "status": "OK", "errors": 0, "week_id": "2026-W42",
                         "finished_at": "2026-10-13 06:05:00+00:00"}] if self.rows else []
            return super().__call__(sql)

    def _run(self, attrs, **k):
        q = self.QB([cand(1)], **k)
        b = Brevo(attrs=attrs, html=self.BHTML)
        return day(q, b).run(TUE, caps_override={"reorder_1": 5})["types"][0]

    def test_empty_block_is_not_sent_with_reason(self):
        t = self._run({"P1_NAME": "Cimdi", "OFFER_RUNG": 0, "FLOW_HTML": ""})
        self.assertEqual(t["blocks"], ["FLOW_HTML"])
        self.assertEqual(t["refused"], {"block_empty": 1})
        self.assertEqual(t["chosen"], 0)

    def test_filled_block_passes_when_push_logged(self):
        t = self._run({"P1_NAME": "Cimdi", "OFFER_RUNG": 0, "FLOW_HTML": "<table>x</table>"})
        self.assertEqual(t["chosen"], 1)
        self.assertTrue(t["type_refusal"].startswith("D1 "))            # only the (expected) track refusal

    def test_no_log_table_or_no_row_holds_the_type(self):
        import block_gate as BG
        self.assertIn("does not exist", BG.push_ok(self.QB([], log_exists=False), "2026-10-13", "FLOW_HTML"))
        self.assertIsNone(BG.push_ok(self.QB([], rows=1), "2026-10-13", "FLOW_HTML"))
        self.assertIn("no push row", BG.push_ok(self.QB([], rows=0), "2026-10-13", "FLOW_HTML"))
        tracks_on = [{"track": "reorder", "enabled": True, "enabled_by": "Raivis"}]
        import hashlib
        appr = [dict(APPR[0], approved_sha256=hashlib.sha256(self.BHTML.encode()).hexdigest())]
        q = self.QB([cand(1)], log_exists=False, tracks=tracks_on, appr=appr)
        t = day(q, Brevo(attrs={"FLOW_HTML": "x", "P1_NAME": "Cimdi", "OFFER_RUNG": 0}, html=self.BHTML)).run(
            TUE, caps_override={"reorder_1": 5})["types"][0]
        self.assertTrue(t["type_refusal"].startswith("HOLD FLOW_HTML"))

    def test_fields_and_outcome_reason(self):
        import block_gate as BG
        self.assertEqual(BG.fields_in("{{ contact.AK_HTML|safe }} {{contact.FLOW_HTML | safe}}"), ["AK_HTML", "FLOW_HTML"])
        self.assertEqual(BG.fields_in(HTML), [])
        self.assertEqual(BG.empty_blocks({"FLOW_HTML": "  "}, ["FLOW_HTML"]), ["FLOW_HTML"])
        # v1.1: two fields per family; only the FIRST is checked, the 2nd may be ""
        self.assertEqual(BG.fields_in("{{ contact.FLOW_HTML|safe }}{{ contact.FLOW2_HTML|safe }}"), ["FLOW_HTML"])
        self.assertEqual(BG.fields_in("{{ contact.AK2_HTML|safe }}"), ["AK_HTML"])
        self.assertEqual(BG.empty_blocks({"FLOW_HTML": "<t>", "FLOW2_HTML": ""}, ["FLOW_HTML"]), [])
        # v1.2: AK_HTML = own goods only, may be "" -> never block_empty
        self.assertEqual(BG.empty_blocks({"AK_HTML": "", "AK2_HTML": ""}, ["AK_HTML"]), [])
        self.assertEqual(BG.empty_blocks({"AK_HTML": "", "FLOW_HTML": ""}, ["AK_HTML", "FLOW_HTML"]), ["FLOW_HTML"])
        plan = [{"master_key": "m1", "email": "a", "email_type": "reorder_1", "planned_send_date": "2026-10-13",
                 "would_send": True, "reason": "r"}]
        got = SD.outcome_rows(plan, [{"email_type": "reorder_1", "decisions": {"m1": "block_empty FLOW_HTML"},
                                      "type_refusal": None}], "2026-10-13", False)
        self.assertEqual(got[0][5:7], ("MEMBER_REFUSED", "block_empty FLOW_HTML"))


class AkH1(unittest.TestCase):
    @staticmethod
    def q_of(part, tables=("block_push_log", "ak_maker_html"), n=1, maker=({"week_id": "2026-W42", "html": "<t>m</t>",
                                                                          "html_sha256": "ab", "built_at": "x"},)):
        def q(sql):
            if "INFORMATION_SCHEMA.PARTITIONS" in sql and sql.startswith("SELECT last_modified_time"):
                return [{"t": "2026-10-13 06:00:00"}] if part else []
            if "INFORMATION_SCHEMA.TABLES" in sql:
                return [{"x": 1}] if any(f"'{t}'" in sql for t in tables) else []
            if "ak_maker_html" in sql:
                return [dict(m) for m in maker]
            return [{"mode": "LIVE", "status": "OK", "errors": 0, "week_id": "2026-W42", "built_match": True,
                     "finished_at": "2026-10-13 06:05:00+00:00"}] if n else []
        return q

    def test_gate(self):
        import block_gate as BG
        q_of = self.q_of
        self.assertIsNone(BG.ak_h1(q_of(True), "2026-10-13", "2026-W42"))
        self.assertIn("no akcija_assembled partition", BG.ak_h1(q_of(False), "2026-10-13", "2026-W42"))
        self.assertIn("does not exist", BG.ak_h1(q_of(True, tables=()), "2026-10-13", "2026-W42"))
        self.assertIn("does not exist", BG.ak_h1(q_of(True, tables=("ak_push_log",)), "2026-10-13", "2026-W42"))
        seen = []
        BG.ak_h1(lambda s: seen.append(s) or q_of(True)(s), "2026-10-13", "2026-W42")
        self.assertFalse([x for x in seen if "ak_push_log" in x and "block_push_log" not in x])
        self.assertIn("no LIVE/OK", BG.ak_h1(q_of(True, n=0), "2026-10-13", "2026-W42"))
        self.assertIn("ORDER BY finished_at DESC LIMIT 1", "".join(seen))
        self.assertIn("partition_id = '20261013'", BG.assembled_built_at_sql("2026-10-13"))

    def test_v12_maker_row_required(self):
        import block_gate as BG
        q_of = self.q_of
        self.assertIn("HOLD MAKER", BG.ak_h1(q_of(True, tables=("block_push_log",)), "2026-10-13", "2026-W42"))
        self.assertIn("HOLD MAKER", BG.ak_h1(q_of(True, maker=()), "2026-10-13", "2026-W42"))
        for bad in ({"html": "<t/>", "html_sha256": " "}, {"html": "", "html_sha256": "ab"}):
            self.assertIn("HOLD MAKER", BG.ak_h1(q_of(True, maker=(bad,)), "2026-10-13", "2026-W42"))
        seen = []
        BG.ak_h1(lambda s: seen.append(s) or q_of(True)(s), "2026-10-13", "2026-W42")
        self.assertTrue([x for x in seen if "ak_maker_html" in x and "week_id = '2026-W42'" in x])


class BlockTiming(unittest.TestCase):
    """MAIN 18:50: latest push row per block, checked at send time, wait (5 min) until 10:30 Riga."""

    def q_rows(self, rows):
        def q(sql):
            if "INFORMATION_SCHEMA.TABLES" in sql:
                return [{"x": 1}]
            if "block_push_log" in sql:
                return rows[:1]                 # the SQL orders by finished_at DESC; the test passes them latest first
            return []
        return q

    def test_latest_row_wins(self):
        import block_gate as BG
        ok = {"mode": "LIVE", "status": "OK", "errors": 0, "finished_at": "2026-10-13 06:05:00+00:00"}
        err = {"mode": "LIVE", "status": "ERROR", "errors": 3, "finished_at": "2026-10-13 06:01:00+00:00"}
        self.assertIsNone(BG.push_ok(self.q_rows([ok, err]), "2026-10-13", "FLOW_HTML"))
        self.assertIn("ERROR", BG.push_ok(self.q_rows([dict(err, finished_at="2026-10-13 06:09:00+00:00"), ok]),
                                          "2026-10-13", "FLOW_HTML"))
        seen = []
        BG.push_ok(lambda s: seen.append(s) or self.q_rows([ok])(s), "2026-10-13", "FLOW_HTML")
        self.assertTrue([x for x in seen if "ORDER BY finished_at DESC LIMIT 1" in x])

    def test_wait_releases_on_ok_and_holds_at_1030(self):
        import datetime as dt
        import block_gate as BG
        R = BG.RIGA
        t = [dt.datetime(2026, 10, 13, 9, 0, tzinfo=R)]
        slept = []

        def sleep(s):
            slept.append(s)
            t[0] += dt.timedelta(seconds=s)
        answers = iter(["HOLD x", "HOLD x", None])
        at, why = BG.wait_for(lambda: next(answers), lambda: t[0], sleep, BG.deadline("2026-10-13"))
        self.assertEqual((at.astimezone(R).strftime("%H:%M"), why, slept), ("09:10", None, [300, 300]))
        t[0] = dt.datetime(2026, 10, 13, 9, 0, tzinfo=R)
        at, why = BG.wait_for(lambda: "HOLD x", lambda: t[0], sleep, BG.deadline("2026-10-13"))
        self.assertIsNone(at)
        self.assertIn("waited until 10:30 Riga", why)
        self.assertLessEqual(t[0].astimezone(R).time(), dt.time(10, 30))

    def test_would_start_and_236_sends(self):
        import block_gate as BG
        f = lambda ts: [x.astimezone(BG.RIGA).strftime("%H:%M") for x in BG.sends_236("2026-10-13", ts)]  # noqa: E731
        self.assertEqual(f("2026-10-13 05:50:00+00:00"), ["09:00", "09:15", "09:30"])       # OK before 09:00
        self.assertEqual(f("2026-10-13 06:05:10+00:00"), ["09:10", "09:25", "09:40"])       # OK 09:05:10 -> 09:10
        self.assertEqual(f("2026-10-13 07:31:00+00:00"), [])                                 # after 10:30 -> HOLD
        self.assertEqual(f(None), [])

    def test_send_day_dry_does_not_sleep_and_records_timing(self):
        bh = BlocksD31.BHTML
        q = BlocksD31.QB([cand(1)], rows=0)
        dd = day(q, Brevo(attrs={"P1_NAME": "Cimdi", "OFFER_RUNG": 0, "FLOW_HTML": "<t/>"}, html=bh))
        dd.sleep = lambda s: self.fail("DRY must not wait")
        t = dd.run(TUE, caps_override={"reorder_1": 5})["types"][0]
        self.assertTrue(t["block_hold"].startswith("HOLD FLOW_HTML"))
        self.assertIsNone(t["would_start"])

    def test_live_waits_then_holds(self):
        import send_day as SDm
        dd = day(BlocksD31.QB([cand(1)], rows=0), Brevo(attrs={"FLOW_HTML": "<t/>"}, html=BlocksD31.BHTML))
        import datetime as dt
        import block_gate as BG
        t = [dt.datetime(2026, 10, 13, 9, 0, tzinfo=BG.RIGA)]
        dd.clock = lambda: t[0]
        n = []
        dd.sleep = lambda s: (n.append(s), t.__setitem__(0, t[0] + dt.timedelta(seconds=s)))
        out = dd.block_gate(dt.date(2026, 10, 13), ["FLOW_HTML"], wait=True)
        self.assertEqual(len(n), 18)                                   # checks 09:00 ... 10:30, every 5 min
        self.assertIn("waited until 10:30", out["block_hold"])
        self.assertTrue(hasattr(SDm.Day, "block_gate"))

    def test_block_types_go_after_plain_types(self):
        import inspect
        src = inspect.getsource(SD.Day.run)
        self.assertIn("types = sorted(types, key=lambda t: bool(", src)


class FlowV13(unittest.TestCase):
    """INTERFACE BLOCKS v1.3 (MAIN 19:00): per-letter config, all-product-fields-empty rule, push gate per group."""
    GRID = "{{ contact.FLOW_HTML|safe }}{{ contact.FLOW2_HTML|safe }}{{ contact.FLOWR_HTML|safe }}"
    ROWS = "{{ contact.FLOW_HTML|safe }}{{ contact.FLOWS_HTML|safe }}{{ contact.FLOWS2_HTML|safe }}{{ contact.FLOWR_HTML|safe }}"

    def tearDown(self):
        import block_gate as BG
        BG.FLOW_RULES_APPROVED_SHA = None

    def test_new_fields_are_groups(self):
        import block_gate as BG
        self.assertEqual(BG.fields_in(self.ROWS), ["FLOW_HTML", "FLOWS_HTML", "FLOWR_HTML"])
        self.assertEqual(BG.fields_in("{{ contact.FLOWS2_HTML }}"), ["FLOWS_HTML"])

    def test_inactive_until_main_hands_the_sha(self):
        import block_gate as BG
        c = BG.letter_config(232, self.ROWS)
        self.assertEqual((c["source"], c["product"]), ("template", ["FLOW_HTML"]))      # v1.2 behaviour kept
        self.assertEqual(c["reads"], ["FLOW_HTML", "FLOWS_HTML", "FLOWR_HTML"])          # push gate = what it reads
        BG.FLOW_RULES_APPROVED_SHA = "something-else"
        self.assertEqual(BG.letter_config(232, self.ROWS)["source"], "template")

    def test_active_rules(self):
        import block_gate as BG
        BG.FLOW_RULES_APPROVED_SHA = BG.FLOW_RULES_SOURCE_SHA
        c = BG.letter_config(232, self.ROWS)
        self.assertEqual((c["product"], c["mismatch"]), (["FLOW_HTML", "FLOWS_HTML"], None))
        self.assertEqual(BG.letter_config(179, self.GRID)["product"], ["FLOW_HTML"])
        self.assertIn("CONFIG_MISMATCH 231", BG.letter_config(231, self.GRID)["mismatch"])  # 231 has no R part
        self.assertEqual(BG.letter_config(999, self.GRID)["source"], "template")          # not in the rules

    def test_all_product_fields_empty(self):
        import block_gate as BG
        p = ["FLOW_HTML", "FLOWS_HTML"]
        self.assertEqual(BG.all_empty({"FLOW_HTML": "", "FLOWS_HTML": " "}, p), p)
        self.assertEqual(BG.all_empty({"FLOW_HTML": "", "FLOWS_HTML": "<t/>"}, p), [])
        self.assertEqual(BG.all_empty({"FLOWR_HTML": "<t/>"}, ["FLOW_HTML"]), ["FLOW_HTML"])
        self.assertEqual(BG.all_empty({}, []), [])

    def test_send_day_rows_letter_goes_with_flows_only_when_active(self):
        import block_gate as BG
        attrs = {"P1_NAME": "Cimdi", "OFFER_RUNG": 0, "FLOW_HTML": "", "FLOWS_HTML": "<t/>", "FLOWR_HTML": ""}
        q = BlocksD31.QB([cand(1)])
        t = day(q, Brevo(attrs=attrs, html=self.ROWS)).run(TUE, caps_override={"reorder_1": 5})["types"][0]
        self.assertEqual(t["refused"], {"block_empty": 1})                   # inactive: FLOW_HTML "" refuses
        self.assertEqual(t["blocks"], ["FLOW_HTML", "FLOWS_HTML", "FLOWR_HTML"])
        BG.FLOW_RULES_APPROVED_SHA = BG.FLOW_RULES_SOURCE_SHA
        BG.FLOW_RULES[179] = BG.FLOW_RULES[232]                               # reorder_1 fixture uses template 179
        try:
            t = day(BlocksD31.QB([cand(1)]), Brevo(attrs=attrs, html=self.ROWS)).run(
                TUE, caps_override={"reorder_1": 5})["types"][0]
        finally:
            BG.FLOW_RULES[179] = BG._GRID
        self.assertEqual(t["chosen"], 1)
        self.assertEqual(t["block_config"], "rules b34fcb1d56f8")


class GateDate(unittest.TestCase):
    def test_dry_only_and_used_for_the_push_rows(self):
        import datetime as dt
        q = BlocksD31.QB([cand(1)])
        dd = day(q, Brevo(attrs={"P1_NAME": "Cimdi", "OFFER_RUNG": 0, "FLOW_HTML": "<t/>"}, html=BlocksD31.BHTML))
        t = dd.run(TUE, caps_override={"reorder_1": 5}, gate_date=dt.date(2026, 10, 11))["types"][0]
        self.assertEqual(t["gate_date"], "2026-10-11")
        self.assertTrue([x for x in q.calls if "run_date = DATE '2026-10-11'" in x])
        self.assertFalse([x for x in q.calls if "block_push_log" in x and f"run_date = DATE '{TUE}'" in x])
        live = day(BlocksD31.QB([cand(1)]), Brevo())
        live.env = {"DRY_RUN": "false", "ALLOW_SEND": "true", "GLOBAL_SEND_ENABLED": "true"}
        with self.assertRaises(SD.SP.SendLocked):
            live.run(TUE, mode="live", gate_date=dt.date(2026, 10, 11))
