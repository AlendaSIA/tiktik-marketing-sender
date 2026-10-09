"""D4 send path: every lock refuses BEFORE any external call; with all locks open (fakes only) it
sends once, logs each recipient, writes the SAME PD record the shadow plan stored."""
import datetime as dt
import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pd_record  # noqa: E402
import presend  # noqa: E402
import send_path as SP  # noqa: E402

NOW = dt.datetime(2026, 10, 6, 7, 0, tzinfo=dt.timezone.utc)      # 10:00 Riga, 06.10
CAMP = {"campaign_id": 501, "email_type": "winback_2", "track": "winback", "template_id": 232,
        "rung": 2, "utm_campaign": "2026-w41-tava-cena-2", "brevo_list_id": 90}
AUD = [{"master_key": "m1", "email": "a@x.lv", "person_id": 11, "reason": "r1"},
       {"master_key": "m2", "email": "b@x.lv", "person_id": None, "reason": "r2"}]


class Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, *a, **k):
        self.calls.append(a)
        return {"ok": True}


def cfg(allow=True, dry=False):
    return types.SimpleNamespace(ALLOW_SEND=allow, DRY_RUN=dry)


class Lookups:
    def __init__(self, track=True, tpl=True, aud=AUD, sup=0, run="rg-1", goods=None):
        self.t, self.p, self.a, self.s, self.touched = track, tpl, aud, sup, []
        self.run = run
        self.g = {"m1": (2, False), "m2": (2, False)} if goods is None else goods

    def track_enabled(self, t): self.touched.append("track"); return self.t
    def template_approved(self, i): self.touched.append("tpl"); return self.p
    def audience(self, b, i): self.touched.append("aud"); return self.a
    def suppressed(self, e): self.touched.append("sup"); return self.s
    def goods_run(self, d): self.touched.append("goods_run"); return self.run
    def goods(self, r, mks): self.touched.append("goods"); return self.g
    ctx = None      # {mk: presend.Ctx}; default = a letter that passes every gate
    blocks = {}

    def presend_ctx(self, c, d, mks):
        self.touched.append("ctx")
        ok = presend.Ctx(letter_fields=True, template_id=c.get("template_id"), template_approved=True, offer_valid_until="2026-10-19",
                         goods=(c.get("rung"), False), r1_ref_price="9,90 €", xsell_valid_until="2026-10-19",
                         anketa_url="https://plani.tiktik.lv/atsauksme.php?o=X&t=t", order_nr="X")
        return {m: ok for m in mks} if self.ctx is None else self.ctx

    def person_blocks(self, d, mks): self.touched.append("person"); return self.blocks
    lf = {"status": "OK", "plan_run_id": "plan-1", "run_id": "lf-1"}
    plan = "plan-1"

    def letter_fields(self, d): self.touched.append("lf"); return self.lf
    def plan_run(self, d): self.touched.append("plan"); return self.plan


def run(config=cfg(), lookups=None, unlocked="RAIVIS-2026-10-06", type_key="TESTTYPE"):
    brevo, log, pd, st = Rec(), Rec(), Rec(), Rec()
    lookups = lookups or Lookups()
    with mock.patch.dict(os.environ, {"SEND_UNLOCKED_BY": unlocked} if unlocked else {}, clear=False), \
         mock.patch.object(pd_record, "PD_ACTIVITY_TYPE_KEY", type_key), \
         mock.patch.object(pd_record, "PD_ACTIVITY_TYPE_ID", 7 if type_key else None):
        if not unlocked:
            os.environ.pop("SEND_UNLOCKED_BY", None)
        try:
            out = SP.dispatch(dict(CAMP), send_date="2026-10-06", batch_id="B", build_id="b1",
                              config=config, lookups=lookups, brevo_send=brevo, log_sink=log,
                              pd_writer=pd, state_advance=st, now=NOW)
            return out, None, brevo, log, pd, st, lookups
        except SP.SendLocked as e:
            return None, e, brevo, log, pd, st, lookups


class Locks(unittest.TestCase):
    def assertLockedBeforeAnything(self, e, brevo, log, pd, st, first):
        self.assertIsNotNone(e, "dispatch did not refuse")
        self.assertEqual(e.closed[0][0], first)
        for r in (brevo, log, pd, st):
            self.assertEqual(r.calls, [])

    def test_L1_today_config_refuses_and_touches_nothing(self):
        out, e, brevo, log, pd, st, lk = run(config=cfg(allow=False, dry=True))
        self.assertLockedBeforeAnything(e, brevo, log, pd, st, "L1")
        self.assertEqual(lk.touched, [], "a config lock must refuse before any lookup")
        self.assertIn("SEND PATH LOCKED", str(e))

    def test_L2_word_must_be_raivis_today(self):
        for word in (None, "RAIVIS-2026-10-05", "MAIN-2026-10-06", "raivis-2026-10-06"):
            out, e, brevo, log, pd, st, lk = run(unlocked=word)
            self.assertLockedBeforeAnything(e, brevo, log, pd, st, "L2")

    def test_L6_pd_type_unresolved(self):
        out, e, brevo, log, pd, st, lk = run(type_key=None)
        self.assertLockedBeforeAnything(e, brevo, log, pd, st, "L6")

    def test_L3_L4_L5(self):
        for lk, first in ((Lookups(track=False), "L3"), (Lookups(tpl=False), "L4"),
                          (Lookups(aud=[]), "L5"), (Lookups(sup=1), "L5")):
            out, e, brevo, log, pd, st, _ = run(lookups=lk)
            self.assertLockedBeforeAnything(e, brevo, log, pd, st, first)

    def test_all_open_sends_once_logs_all_pd_same_record(self):
        out, e, brevo, log, pd, st, lk = run()
        self.assertIsNone(e)
        self.assertEqual(brevo.calls, [(501,)])
        self.assertEqual(len(log.calls), 1)
        rows = log.calls[0][0]
        self.assertEqual([r["email"] for r in rows], ["a@x.lv", "b@x.lv"])
        self.assertTrue(all(r["source"] == "engine_live" for r in rows))
        self.assertEqual(out, {"sent": 2, "pd_writes": 1, "no_person": 1})
        # the live PD record is byte-identical to what the shadow plan renders for the same send
        with mock.patch.object(pd_record, "PD_ACTIVITY_TYPE_KEY", "TESTTYPE"), \
             mock.patch.object(pd_record, "PD_ACTIVITY_TYPE_ID", 7):
            shadow = pd_record.render(person_id=11, org_id=None, master_key="m1", email="a@x.lv",
                                      email_type="winback_2", template_id=232, send_date="2026-10-06",
                                      offer_rung=2, reason="r1", campaign_ref="2026-w41-tava-cena-2")
        self.assertEqual(pd_record.canonical(pd.calls[0][0]), pd_record.canonical(shadow))
        self.assertEqual(len(st.calls), 2)


class CampaignLayerStillRefuses(unittest.TestCase):
    def test_production_dispatch_refuses_even_with_config_open(self):
        with mock.patch.dict(os.environ, {"SEND_UNLOCKED_BY": "RAIVIS-" + dt.datetime.now(SP.RIGA).date().isoformat()}), \
             mock.patch.object(pd_record, "PD_ACTIVITY_TYPE_KEY", "X"):
            import config as C
            with mock.patch.object(C, "ALLOW_SEND", True), mock.patch.object(C, "DRY_RUN", False):
                with self.assertRaises(SP.SendLocked) as e:
                    SP.production_dispatch(1, dt.datetime.now(SP.RIGA).date().isoformat(), "B", "b")   # today: past L10
        self.assertTrue("not wired" in str(e.exception) or "refused" in str(e.exception))

    def test_production_lookups_fail_closed_and_only_four_are_wired(self):
        import send_lookups
        lk = SP.ProductionLookups()
        with mock.patch.object(send_lookups, "warehouse", side_effect=RuntimeError("no warehouse here")):
            for name, args in (("letter_fields", ("2026-10-06",)), ("plan_run", ("2026-10-06",)),
                               ("person_blocks", ("2026-10-06", ["m"])), ("presend_ctx", ({}, "2026-10-06", ["m"]))):
                with self.assertRaises(SP.SendLocked) as e:
                    getattr(lk, name)(*args)
                self.assertEqual(e.exception.closed[0][0], "WIRE")
        with mock.patch.object(send_lookups, "warehouse", side_effect=RuntimeError("no warehouse here")):
            with self.assertRaises(SP.SendLocked) as e:                            # L4 wired by TC4: fails closed too
                lk.template_approved(180)
            self.assertEqual(e.exception.closed[0][0], "WIRE")
        wh = mock.Mock(); wh.template_approved.return_value = False
        with mock.patch.object(send_lookups, "warehouse", return_value=wh):
            self.assertIs(SP.ProductionLookups().template_approved(180), False)
            self.assertIs(wh.template_approved.call_args[0][1], SP._brevo_live_hash)   # hashed live, where the key is
        for name in ("track_enabled", "audience", "suppressed", "goods_run", "goods"):
            with self.assertRaises(SP.SendLocked) as e:
                getattr(lk, name)(1)
            self.assertIn("not wired", str(e.exception))
        fake = mock.Mock(); fake.plan_run.return_value = "plan-1"
        with mock.patch.object(send_lookups, "warehouse", return_value=fake):
            self.assertEqual(SP.ProductionLookups().plan_run("2026-10-06"), "plan-1")

    def test_production_dispatch_with_real_config_is_L1(self):
        import config as C
        with self.assertRaises(SP.SendLocked) as e:
            SP.production_dispatch(1, "2026-10-06", "B", "b")
        self.assertEqual(e.exception.closed[0][0], "L1")
        self.assertFalse(C.ALLOW_SEND)


if __name__ == "__main__":
    unittest.main()


class L7G15PreSend(unittest.TestCase):
    """G15.2: the hard G15 gate is pre-send (L7); refusal before the Brevo call, never a fallback."""

    def _closed(self, lk, camp=None):
        global CAMP
        old = CAMP
        try:
            if camp:
                CAMP = camp
            out, err, brevo, log, pd, st, _ = run(lookups=lk)
        finally:
            CAMP = old
        return out, err, brevo, log, pd, st

    def test_no_goods_run_for_send_date_refuses(self):
        out, err, brevo, log, pd, st = self._closed(Lookups(run=None))
        self.assertIsNotNone(err)
        self.assertIn("L7", [k for k, _ in err.closed])
        self.assertIn("no goods run for send date 2026-10-06", str(err))
        self.assertEqual((brevo.calls, log.calls, pd.calls, st.calls), ([], [], [], []))

    def test_zero_priced_member_refuses(self):
        _, err, brevo, *_ = self._closed(Lookups(goods={"m1": (2, True), "m2": (2, False)}))
        self.assertIn("NO_PRICED_SLOTS 1", str(err)); self.assertEqual(brevo.calls, [])

    def test_missing_row_refuses(self):
        _, err, brevo, *_ = self._closed(Lookups(goods={"m1": (2, False)}))
        self.assertIn("NO_SLOT_ROW 1", str(err)); self.assertEqual(brevo.calls, [])

    def test_rung_mismatch_refuses(self):
        _, err, brevo, *_ = self._closed(Lookups(goods={"m1": (1, False), "m2": (2, False)}))
        self.assertIn("NO_SLOT_ROW 1", str(err)); self.assertEqual(brevo.calls, [])

    def test_e2_is_a_price_letter_too(self):
        camp = {**CAMP, "email_type": "winback_2_e2"}
        _, err, *_ = self._closed(Lookups(run=None), camp)
        self.assertIn("L7", [k for k, _ in err.closed])

    def test_non_price_letter_skips_l7(self):
        camp = {**CAMP, "email_type": "reorder_1", "rung": 0, "track": "reorder", "template_id": 179}
        lk = Lookups(run=None)
        out, err, brevo, *_ = self._closed(lk, camp)
        self.assertIsNone(err); self.assertEqual(len(brevo.calls), 1)
        self.assertNotIn("goods_run", lk.touched)

    def test_l7_never_reached_while_config_locks_closed(self):
        lk = Lookups(run=None)
        out, err, brevo, log, pd, st, _ = run(config=cfg(allow=False), lookups=lk)
        self.assertEqual([k for k, _ in err.closed][0], "L1")
        self.assertEqual(lk.touched, [])

    def test_production_lookups_refuse_g15_too(self):
        with self.assertRaises(SP.SendLocked):
            SP._ProductionLookupsNotWired().goods_run("2026-10-06")


class L8L9PreSend(unittest.TestCase):
    """MAIN 2026-10-05 16:10 item 3: the data gates (L8) and the person re-check (L9) refuse before the Brevo call."""

    def _run(self, camp, lk, offered="rec"):
        brevo, log, pd, st, off = Rec(), Rec(), Rec(), Rec(), Rec()
        with mock.patch.dict(os.environ, {"SEND_UNLOCKED_BY": "RAIVIS-2026-10-06"}, clear=False), \
             mock.patch.object(pd_record, "PD_ACTIVITY_TYPE_KEY", "T"), mock.patch.object(pd_record, "PD_ACTIVITY_TYPE_ID", 7):
            try:
                out = SP.dispatch(dict(camp), send_date="2026-10-06", batch_id="B", build_id="b1", config=cfg(),
                                  lookups=lk, brevo_send=brevo, log_sink=log, pd_writer=pd, state_advance=st,
                                  offered_sink=off if offered == "rec" else offered, now=NOW)
                return out, None, brevo, off
            except SP.SendLocked as e:
                return None, e, brevo, off

    def _lk(self, ctx=None, blocks=None):
        lk = Lookups()
        lk.ctx = ctx
        lk.blocks = blocks or {}
        return lk

    def test_any_member_with_a_gate_refuses_the_campaign(self):
        ok = presend.Ctx(letter_fields=True, template_id=232, template_approved=True, offer_valid_until="2026-10-19", goods=(2, False))
        bad = presend.Ctx(letter_fields=True, template_id=232, template_approved=True, offer_valid_until=None, goods=(2, False))
        _, err, brevo, _ = self._run(CAMP, self._lk({"m1": ok, "m2": bad}))
        self.assertIn(("L8", "pre-send gates: NO_OFFER_VALID_UNTIL 1"), err.closed)
        self.assertEqual(brevo.calls, [])
        out, err, brevo, _ = self._run(CAMP, self._lk({"m1": ok, "m2": ok}))
        self.assertIsNone(err); self.assertEqual(len(brevo.calls), 1)

    def test_a_member_without_ctx_refuses_never_passes(self):
        ok = presend.Ctx(letter_fields=True, template_id=232, template_approved=True, offer_valid_until="2026-10-19", goods=(2, False))
        _, err, brevo, _ = self._run(CAMP, self._lk({"m1": ok}))
        self.assertIn("L8", [k for k, _ in err.closed]); self.assertEqual(brevo.calls, [])

    def test_244_without_anketa_url_and_235_without_intro_price(self):
        c244 = {**CAMP, "email_type": "post_purchase_feedback", "template_id": 244, "rung": 0, "track": "post_purchase"}
        no = presend.Ctx(letter_fields=True, template_id=244, template_approved=True, anketa_url="", order_nr="M-1")
        _, err, brevo, _ = self._run(c244, self._lk({"m1": no, "m2": no}))
        self.assertIn("NO_ANKETA_URL 2", str(err)); self.assertEqual(brevo.calls, [])
        c235 = {**CAMP, "email_type": "active_xsell", "template_id": 235, "rung": 0, "track": "post_purchase"}
        no = presend.Ctx(letter_fields=True, template_id=235, template_approved=True, r1_ref_price="", xsell_valid_until="2026-10-19")
        _, err, brevo, _ = self._run(c235, self._lk({"m1": no, "m2": no}))
        self.assertIn("XS4_NO_INTRO_PRICE 2", str(err)); self.assertEqual(brevo.calls, [])

    def test_L9_b2b_lead_en_recheck_at_send_time(self):
        for reason in ("B2B_FLOW", "LEAD_FLOW", "EN_PENDING"):
            _, err, brevo, _ = self._run(CAMP, self._lk(blocks={"m2": reason}))
            self.assertIn(("L9", f"person re-check: {reason} 1"), err.closed)
            self.assertEqual(brevo.calls, [])

    def test_235_records_every_offered_r_product_and_refuses_without_the_sink(self):
        c235 = {**CAMP, "email_type": "active_xsell", "template_id": 235, "rung": 0, "track": "post_purchase"}
        ok = presend.Ctx(letter_fields=True, template_id=235, template_approved=True, r1_ref_price="9 €", xsell_valid_until="2026-10-19",
                         r_handles=("h1", "h2"), r_cabinet=("h1", "h2"))
        out, err, brevo, off = self._run(c235, self._lk({"m1": ok, "m2": ok}))
        self.assertIsNone(err)
        rows = off.calls[0][0]
        self.assertEqual(sorted((r["master_key"], r["handle"]) for r in rows),
                         [("m1", "h1"), ("m1", "h2"), ("m2", "h1"), ("m2", "h2")])
        _, err, brevo, _ = self._run(c235, self._lk({"m1": ok, "m2": ok}), offered=None)
        self.assertIn("WIRE", [k for k, _ in err.closed]); self.assertEqual(brevo.calls, [])
        # a non-235 letter writes nothing there
        out, err, brevo, off = self._run(CAMP, self._lk())
        self.assertIsNone(err); self.assertEqual(off.calls, [])

    def test_never_reached_while_config_locks_closed_and_production_is_unwired(self):
        lk = self._lk()
        with mock.patch.dict(os.environ, {"SEND_UNLOCKED_BY": "x"}, clear=False):
            with self.assertRaises(SP.SendLocked):
                SP.dispatch(dict(CAMP), send_date="2026-10-06", batch_id="B", build_id="b1", config=cfg(False, True),
                            lookups=lk, brevo_send=Rec(), log_sink=Rec(), pd_writer=Rec(), state_advance=Rec(), now=NOW)
        self.assertEqual(lk.touched, [])
        for name in ("presend_ctx", "person_blocks"):
            with self.assertRaises(SP.SendLocked):
                getattr(SP._ProductionLookupsNotWired(), name)(1, 2, 3)


class L10DatesAndWindow(L8L9PreSend):
    """DW3 / DW4 through dispatch(): refused before the audience is read and before Brevo."""

    def test_writer_missing_or_on_an_older_plan_refuses(self):
        for lf, word in ((None, "letter_fields_log is not OK"),
                         ({"status": "OK", "plan_run_id": "plan-0", "run_id": "lf-0"}, "rebuilt after the letter writer")):
            lk = self._lk()
            lk.lf = lf
            _, err, brevo, _ = self._run(CAMP, lk)
            self.assertEqual([k for k, _ in err.closed], ["L10"])
            self.assertIn(word, str(err))
            self.assertEqual(brevo.calls, [])
            self.assertNotIn("aud", lk.touched)

    def test_passes_with_the_writer_on_the_latest_plan(self):
        out, err, brevo, _ = self._run(CAMP, self._lk())
        self.assertIsNone(err)
        self.assertEqual(len(brevo.calls), 1)
