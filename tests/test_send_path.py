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
                    SP.production_dispatch(1, "2026-10-06", "B", "b")
        self.assertIn("not wired", str(e.exception))

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
