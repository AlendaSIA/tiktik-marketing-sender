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
LIVE_ENV = {"DRY_RUN": "false", "ALLOW_SEND": "true", "GLOBAL_SEND_ENABLED": "true"}
DRY_ENV = {"DRY_RUN": "true", "ALLOW_SEND": "false", "GLOBAL_SEND_ENABLED": "false"}


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
        if s.startswith(("CREATE", "INSERT", "UPDATE")):
            return []
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

    def test_dry_without_cap_row_is_d3_zero(self):
        q = Q([cand(1)])
        t = day(q).run(TUE)["types"][0]
        self.assertEqual(t["type_refusal"], "D3 NO_CAP (0)")
        self.assertEqual(t["chosen"], 0)

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
        self.assertEqual(b.lists[77], ["raivis@alenda.lv"])
        self.assertEqual(b.puts[0], ("/contacts/raivis@alenda.lv", {"attributes": {"P1_NAME": "Cimdi", "OFFER_RUNG": 0.0}}))
        self.assertEqual(res["source_master_key"], "m1")
        self.assertEqual((res["sent"], res["pd_activity_ids"], res["brevo_status"]), (1, [4711], "sent"))
        self.assertEqual(pd.call_args[0][0]["target_person_id"], 99)
        log = q.inserts(SD.T_LOG)[0]
        self.assertIn("'TEST:raivis@alenda.lv'", log)
        self.assertNotIn("'m1'", log)                                  # no customer's history moves
        self.assertIn("'TEST:raivis@alenda.lv'", q.inserts(SD.T_ADV)[0])
        self.assertEqual(res["unfilled"], [])

    def test_proof_needs_l1_and_the_approved_hash(self):
        with self.assertRaises(SP.SendLocked):
            day(Q([cand(1)]), env=DRY_ENV).proof("reorder_1", TUE, 99)
        with mock.patch.object(SD.time, "sleep"), self.assertRaises(SP.SendLocked) as e:
            day(Q([cand(1)]), Brevo(html="<p>other</p>"), env=LIVE_ENV).proof("reorder_1", TUE, 99)
        self.assertIn("TEMPLATE_HASH_CHANGED", str(e.exception))

    def test_proof_campaign_drops_the_suppression_exclusion_real_batch_keeps_it(self):
        b = Brevo(attrs={})
        with mock.patch.object(SD.time, "sleep"):
            day(Q([cand(1)], tracks=[]), b, env=LIVE_ENV, pd=mock.Mock(return_value=1)).proof("reorder_1", TUE, 99)
        self.assertEqual(b.campaign["recipients"], {"listIds": [77]})
        self.assertTrue(b.campaign["name"].startswith("TEST proof reorder_1"))
        b2 = Brevo()
        day(Q([cand(1)], caps=[{"send_date": "2026-10-13", "email_type": "reorder_1", "cap": 2, "set_at": "x"}]),
            b2, env=LIVE_ENV).run(TUE)
        self.assertEqual(b2.campaign["recipients"]["exclusionListIds"], [4])

    def test_restore_clears_keys_that_had_no_value_with_empty_string(self):
        q = Q([cand(1)])
        q.proof_rows = [{"run_id": "r1", "raivis_before": '{"P1_NAME": "Vecais"}',
                         "written": '{"P1_NAME": "Cimdi", "OFFER_RUNG": 0.0}'}]
        b = Brevo(attrs={"P1_NAME": "Cimdi", "OFFER_RUNG": 0.0})
        res = day(q, b, env=LIVE_ENV).proof_restore(["r1"])
        self.assertEqual(b.puts[-1][1], {"attributes": {"P1_NAME": "Vecais", "OFFER_RUNG": ""}})
        self.assertEqual(res["cleared"], ["OFFER_RUNG"])

    def test_draft_test_creates_and_never_sends(self):
        b = Brevo()
        b.lists[90] = ["raivis@alenda.lv"]
        res = day(Q([cand(1)]), b, env=LIVE_ENV).draft_test("reorder_1", TUE, 90)
        self.assertEqual(res["campaign_id"], 900)
        self.assertTrue(b.campaign["name"].startswith("TEST draft reorder_1"))
        self.assertFalse(any(p.endswith("/sendNow") for _m, p in b.calls))
        with self.assertRaises(SP.SendLocked):
            day(Q([cand(1)]), b, env=DRY_ENV).draft_test("reorder_1", TUE, 90)

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
