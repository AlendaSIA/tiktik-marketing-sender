"""MAIN 3 13.10 (b): 08:05 Rīta pārbaude mails Raivis ONLY when something is NOT OK; silent (but logged) when all OK."""
import io
import contextlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import send_day as SD  # noqa: E402


def ok_result(**kw):
    r = {"send_date": "2026-10-20", "mode": "dry", "types": [
            {"email_type": "reorder_1", "chosen": 400, "candidates": 420, "type_refusal": None, "refused": 20},
            {"email_type": "winback_1", "chosen": 2500, "candidates": 2550, "type_refusal": "D1 TRACK off"}],
         "akcija_236": {"holds": [], "audience": {"final": 3900}},
         "day_guard": {"d47_double": 0, "d47_unexplained": 13, "cab_mismatch": 0, "cab_no_link": 1}}
    r.update(kw)
    return r


class FakeDay:
    def __init__(self):
        self.mails, self.dashes = [], []

    def notify(self, s, t):
        self.mails.append((s, t))
        return True

    def dash(self, st, t):
        self.dashes.append((st, t))


def run(res):
    day, buf = FakeDay(), io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = SD.check_notify(day, res)
    logged = [json.loads(l[len("CHECK_RESULT "):]) for l in buf.getvalue().splitlines() if l.startswith("CHECK_RESULT ")]
    return day, out, logged


class CheckMail(unittest.TestCase):
    def test_all_ok_is_silent_but_logged(self):
        day, out, logged = run(ok_result())
        self.assertEqual(day.mails, [])
        self.assertEqual(day.dashes, [])
        self.assertTrue(out["check_ok"])
        self.assertFalse(out["check_mailed"])
        self.assertEqual(len(logged), 1)
        self.assertTrue(logged[0]["ok"])
        self.assertIn("viss GO", logged[0]["subject"])

    def test_campaign_hold_mails_raivis_once(self):
        r = ok_result()
        r["types"][0]["type_refusal"] = "D44_WEEK_PRICE_NOT_OK"
        day, out, logged = run(r)
        self.assertEqual(len(day.mails), 1)
        self.assertIn("IR AIZTURĒTAIS", day.mails[0][0])
        self.assertTrue(day.mails[0][1].startswith("Kas nav kārtībā: ir aizturēta kampaņa"))
        self.assertEqual(day.dashes[0][0], "KĻŪDA")
        self.assertFalse(logged[0]["ok"])
        self.assertTrue(out["check_mailed"])

    def test_236_hold_mails(self):
        day, _, _ = run(ok_result(akcija_236={"holds": ["D44_AK_NOT_BUILT"], "audience": {}}))
        self.assertEqual(len(day.mails), 1)

    def test_no_plan_is_not_ok(self):            # report_mail alone said "viss GO" for this
        day, out, _ = run({"send_date": "2026-10-20", "mode": "dry", "types": [], "refused": "NO_PLAN"})
        self.assertEqual(len(day.mails), 1)
        self.assertIn("NAV OK", day.mails[0][0])
        self.assertIn("atteikts: NO_PLAN", out["check_problems"])

    def test_236_missing_on_full_run_is_not_ok(self):
        r = ok_result()
        del r["akcija_236"]
        self.assertIn("236 nav novērtēts", SD.check_problems(r))
        r["only"] = ["reorder_1"]
        self.assertNotIn("236 nav novērtēts", SD.check_problems(r))

    def test_guard_double_and_cab_mismatch_and_missing(self):
        self.assertTrue(SD.check_problems(ok_result(day_guard={"d47_double": 2, "cab_mismatch": 0})))
        self.assertTrue(SD.check_problems(ok_result(day_guard={"d47_double": 0, "cab_mismatch": 3})))
        self.assertTrue(SD.check_problems(ok_result(day_guard={"skipped": "x"})))
        self.assertTrue(SD.check_problems(ok_result(day_guard=None)))

    def test_member_holds_alone_stay_silent(self):
        self.assertEqual(SD.check_problems(ok_result()), [])     # 13 unexplained + 1 no-link + 20 refused

    def test_unfinished_check_always_mails(self):
        day, buf = FakeDay(), io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertTrue(SD.check_failed_notify(day, "2026-10-20", "RuntimeError: boom"))
        self.assertEqual(len(day.mails), 1)
        self.assertIn("NAV OK", day.mails[0][0])
        self.assertIn("CHECK_RESULT", buf.getvalue())


class MainWiring(unittest.TestCase):
    """main(--notify=check): OK -> notify never called; NOT OK -> one notify; SendLocked/crash -> one notify."""
    def _main(self, run_impl):
        calls = []
        orig = (SD._prod, SD.Day.run, SD.Day.notify)
        SD._prod = lambda: (None, None, None, None, lambda st, t: None)
        SD.Day.run = run_impl
        SD.Day.notify = lambda self, s, t: calls.append(s) or True
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    rc = SD.main(["--date=2026-10-20", "--mode=dry", "--notify=check"])
                except RuntimeError:
                    rc = "raised"
        finally:
            SD._prod, SD.Day.run, SD.Day.notify = orig
        return rc, calls

    def test_ok(self):
        rc, calls = self._main(lambda self, d, **k: ok_result())
        self.assertEqual((rc, calls), (0, []))

    def test_not_ok(self):
        rc, calls = self._main(lambda self, d, **k: ok_result(akcija_236={"holds": ["X"]}))
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)

    def test_refused(self):
        def boom(self, d, **k):
            raise SD.SP.SendLocked([("D31", "x")])
        rc, calls = self._main(boom)
        self.assertEqual(rc, 3)
        self.assertEqual(len(calls), 1)

    def test_crash(self):
        def boom(self, d, **k):
            raise RuntimeError("boom")
        rc, calls = self._main(boom)
        self.assertEqual(rc, "raised")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
