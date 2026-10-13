"""Sūtīšanas dzinējs 9 (MAIN 3 13.10): R6 batched bookkeeping + a send_day_run row for every LIVE run (236 included),
R7 PP3 offered_sink wired for 235."""
import datetime as dt
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import pd_record  # noqa: E402
import presend  # noqa: E402
import send_day as SD  # noqa: E402
import send_path as SP  # noqa: E402
import test_send_day as T  # noqa: E402

setUpModule, tearDownModule = T.setUpModule, T.tearDownModule


class BatchedBookkeeping(unittest.TestCase):
    def _day(self):
        q = T.Q([])
        return SD.Day(q, T.WH(), T.Brevo(), mock.Mock(return_value=1), mock.Mock(), T.LIVE_ENV, now=T.NOW), q

    def test_4000_contacts_take_a_handful_of_statements(self):
        d, q = self._day()
        items = [(f"m{i}", "winback_1", dt.date(2026, 10, 13), 0) for i in range(4000)]
        d.advance_batch(items, "B", 332)
        self.assertEqual(len(q.inserts(SD.T_ADV)), 8)
        ups = [c for c in q.calls if c.startswith(f"UPDATE `{SD.T_STATE}`")]
        self.assertEqual(len(ups), 8)
        self.assertEqual(sum(u.count("'m") for u in ups), 4000)
        self.assertTrue(all(len(c) < 1_000_000 for c in q.calls))

    def test_send_log_chunks(self):
        d, q = self._day()
        rows = [{"master_key": f"m{i}", "email": f"p{i}@x.lv", "campaign_id": 332, "email_type": "winback_1",
                 "sent_at": "2026-10-13T07:12:30+00:00", "source": "engine_live", "run_id": "B"} for i in range(2550)]
        d.write_send_log(rows)
        ins = q.inserts(SD.T_LOG)
        self.assertEqual(len(ins), 6)
        self.assertEqual(sum(c.count("'engine_live'") for c in ins), 2550)
        self.assertTrue(all(len(c) < 1_000_000 for c in ins))

    def test_pd_workers_from_env(self):
        d, _ = self._day()
        self.assertEqual(d.pd_workers(), SD.PD_WORKERS_DEFAULT)
        d.env = dict(T.LIVE_ENV, PD_WORKERS="8")
        self.assertEqual(d.pd_workers(), 8)


class DispatchOrder(unittest.TestCase):
    """send_log is written right after the send call, before the state advance and before any Pipedrive write."""

    def test_order_and_pd_error_does_not_stop(self):
        seen = []
        camp = {"campaign_id": 9, "email_type": "reorder_1", "track": "reorder", "template_id": 179, "rung": 0}
        aud = [{"master_key": f"m{i}", "email": f"p{i}@x.lv", "person_id": i, "reason": "r"} for i in range(1, 6)]

        class LK:
            def __getattr__(self, n):
                return {"track_enabled": lambda t: True, "template_approved": lambda t: True,
                        "audience": lambda b, bb: aud, "suppressed": lambda e: 0,
                        "letter_fields": lambda d: {"status": "OK", "plan_run_id": "p"}, "plan_run": lambda d: "p",
                        "presend_ctx": lambda c, d, m: {k: presend.Ctx(letter_fields=True, template_id=179,
                                                                         template_approved=True) for k in m},
                        "person_blocks": lambda d, m: {}, "goods_run": None, "goods": None}[n]

        def pd(rec):
            seen.append("pd")
            if rec["target_person_id"] == 3:
                raise RuntimeError("HTTP 500")
            return 1
        env = {"SEND_UNLOCKED_BY": "RAIVIS-2026-10-06"}
        cfg = type("C", (), {"ALLOW_SEND": True, "DRY_RUN": False})()
        with mock.patch.dict(os.environ, env), mock.patch.object(pd_record, "PD_ACTIVITY_TYPE_KEY", "T"), \
                mock.patch.object(pd_record, "PD_ACTIVITY_TYPE_ID", 7), \
                mock.patch.object(SP, "window_lock", lambda **k: []), mock.patch.object(SP, "g15_lock", lambda **k: []):
            out = SP.dispatch(camp, send_date="2026-10-13", batch_id="B", build_id="p", config=cfg, lookups=LK(),
                              brevo_send=lambda c: seen.append("send"), log_sink=lambda r: seen.append("log"),
                              pd_writer=pd, state_advance=None,
                              state_advance_batch=lambda items: seen.append(("adv", len(items))),
                              now=T.NOW, pd_workers=3)
        self.assertEqual(seen[:3], ["send", "log", ("adv", 5)])
        self.assertEqual(seen.count("pd"), 5)
        self.assertEqual((out["sent"], out["pd_writes"], out["pd_errors"]), (5, 4, 1))


class RunRowAlways(unittest.TestCase):
    def test_send_error_still_writes_send_day_run(self):
        q = T.Q([T.cand(1)], caps=[{"send_date": "2026-10-13", "email_type": "reorder_1", "cap": 1, "set_at": "x"}])
        d = T.day(q, T.Brevo(), env=T.LIVE_ENV)
        d._send = mock.Mock(side_effect=RuntimeError("boom"))
        with mock.patch.object(SD.time, "sleep"), self.assertRaises(RuntimeError):
            d.run(T.TUE)
        rows = q.inserts(SD.T_RUN)
        self.assertEqual(len(rows), 1)
        self.assertIn("'live'", rows[0]); self.assertIn("SEND_ERROR RuntimeError: boom", rows[0])

    def test_236_launch_writes_its_row(self):
        q = T.Q([])
        d = T.day(q, T.Brevo(), env=T.LIVE_ENV)
        d.record_236(T.TUE, {"launched": True, "campaign_id": 333, "audience": {"final": 3946}})
        r = q.inserts(SD.T_RUN)[0]
        self.assertIn("'akcija_236', 236, 'akcija'", r)
        self.assertIn(", 3, 333, 3946,", r)

    def test_236_not_launched_row_names_why(self):
        q = T.Q([])
        T.day(q, T.Brevo(), env=T.LIVE_ENV).record_236(T.TUE, {"launched": False, "error": "X: y"})
        self.assertIn("236_NOT_LAUNCHED X: y", q.inserts(SD.T_RUN)[0])


class R17SummaryToLogOnly(unittest.TestCase):
    """13.10 12:22: the per-run summary mail was read by Raivis as a second send - it now goes to the tech log only."""

    def test_live_run_summary_never_mails(self):
        q = T.Q([T.cand(1)], caps=[{"send_date": "2026-10-13", "email_type": "reorder_1", "cap": 1, "set_at": "x"}])
        d = T.day(q, T.Brevo(), env=T.LIVE_ENV)
        with mock.patch.object(SD.time, "sleep"), mock.patch.object(SD.Day, "notify") as n, \
                mock.patch("builtins.print") as pr:
            d.run(T.TUE)
        n.assert_not_called()
        self.assertTrue(any(str(c.args[0]).startswith("RUN_SUMMARY ") for c in pr.call_args_list))


class OfferedSinkWired(unittest.TestCase):
    """R7: 235 was refused with WIRE '235 needs offered_sink' (draft 331, 149 contacts, 13.10)."""

    def test_235_writes_xsell_offered(self):
        c = dict(T.cand(1), email_type="active_xsell", template_id=235, track="post_purchase", rung=0)
        appr = [dict(T.APPR[0], template_id=235, email_type="active_xsell")]
        q = T.Q([c], caps=[{"send_date": "2026-10-13", "email_type": "active_xsell", "cap": 1, "set_at": "x"}],
                tracks=[{"track": "post_purchase", "enabled": True, "enabled_by": "Raivis"}], appr=appr)
        q.__class__ = type("Q235", (T.Q,), {"__call__": lambda self, sql: (
            [{"email_type": "active_xsell", "template_id": 235, "track": "post_purchase", "n": 1}]
            if "COUNT(*) AS n" in sql else T.Q.__call__(self, sql))})

        class WH(T.WH):
            def presend_ctx(self, campaign, d, mks):
                return {m: presend.Ctx(letter_fields=True, template_id=235, template_approved=True,
                                       r_handles=("h-a", "h-b")) for m in mks}
        d = T.day(q, T.Brevo(), env=T.LIVE_ENV, wh=WH())
        d._d50 = {}
        with mock.patch.object(SD.time, "sleep"), mock.patch.object(SD, "d50_xsell_refusal", lambda c, s: None), \
                mock.patch.object(presend, "gates", lambda *a, **k: []):
            res = d.run(T.TUE)
        t = res["types"][0]
        self.assertIsNone(t["type_refusal"]); self.assertEqual(t["sent"], 1)
        off = q.inserts(SD.T_OFFERED)
        self.assertEqual(len(off), 1)
        self.assertIn("'m1', 'p1@x.lv', 'h-a'", off[0]); self.assertIn("'h-b'", off[0])


class L4OneRegistry(unittest.TestCase):
    """R10: a superseded approval row (approved = FALSE) stays as history and is not read as a second approved hash."""

    def test_superseded_rows_do_not_make_l4_ambiguous(self):
        import send_lookups as L
        wh = L.Warehouse.__new__(L.Warehouse)
        rows = [{"template_id": 179, "email_type": "reorder_1", "approved": False, "approved_sha256": "087d" * 16},
                {"template_id": 179, "email_type": "reorder_1", "approved": False, "approved_sha256": "db96" * 16},
                {"template_id": 179, "email_type": "reorder_1", "approved": True, "approved_sha256": "b1ea" * 16}]
        wh.template_content = lambda now=None: rows
        self.assertTrue(wh.template_approved(179, live_hash=lambda t: "b1ea" * 16))
        self.assertFalse(wh.template_approved(179, live_hash=lambda t: "db96" * 16))
        wh.template_content = lambda now=None: rows[:2]
        self.assertFalse(wh.template_approved(179, live_hash=lambda t: "db96" * 16))   # nothing approved -> closed


if __name__ == "__main__":
    unittest.main()
