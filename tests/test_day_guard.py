import datetime as dt
import unittest
from unittest import mock

import day_guard as DG
import send_day as SD


def c(mk, em=None):
    return {"master_key": mk, "email": em or f"{mk}@x.lv"}


class Doubles(unittest.TestCase):
    def test_clean_day_has_none(self):
        self.assertEqual(DG.doubles({"reorder_1": [c("a")], "winback_1": [c("b")]}), {})

    def test_same_contact_in_two_types_is_held_in_both(self):
        self.assertEqual(DG.doubles({"reorder_1": [c("a")], "winback_1": [c("a"), c("b")]}),
                         {"a": ["reorder_1", "winback_1"]})

    def test_two_master_keys_one_inbox(self):
        got = DG.doubles({"reorder_1": [c("a", "same@x.lv")], "active_xsell": [c("b", "SAME@x.lv ")]})
        self.assertEqual(got, {"a": ["active_xsell", "reorder_1"], "b": ["active_xsell", "reorder_1"]})


class Freeze(unittest.TestCase):
    def test_only_an_order_explains_a_change(self):
        cands = {"reorder_1": [c("a"), c("b"), c("n")], "winback_1": [c("w")]}
        mon = {("a", "reorder_1"), ("w", "reorder_1")}
        got = DG.unexplained(cands, mon, ordered_since={"b", "zz@x.lv"})
        self.assertEqual(set(got), {"n", "w"})
        self.assertIn("Monday: reorder_1", got["w"])


class CabLink(unittest.TestCase):
    def test_own_token_passes_other_or_missing_is_held(self):
        rows = [c("a"), c("b"), c("n"), c("t"), c("e")]
        toks = {"a": ("5500c62958ad0deefa", "a@x.lv"), "b": ("aaaa00000000000001", "b@x.lv"),
                "n": ("bbbb00000000000002", "n@x.lv"), "e": ("cccc00000000000003", "other@x.lv")}
        urls = {"a@x.lv": "https://tiktik.lv/k/?t=5500c62958ad0deefa&utm_campaign=w42-",
                "b@x.lv": "https://tiktik.lv/k/?t=5500c62958ad0deefa", "e@x.lv": "https://x/?t=cccc00000000000003"}
        got = DG.cab_check(rows, toks, urls)
        self.assertNotIn("a", got)
        self.assertIn("another token", got["b"])
        self.assertIn("no KABINETS_URL", got["n"])
        self.assertIn("no cabinet token", got["t"])
        self.assertIn("!=", got["e"])
        self.assertTrue(all(v.startswith(DG.CAB) for v in got.values()))

    def test_token_must_stand_alone(self):
        self.assertFalse(DG.token_in("https://x/?t=ab12cd", "ab12c"))
        self.assertTrue(DG.token_in("https://x/?t=ab12cd&u=1", "ab12cd"))


class Wiring(unittest.TestCase):
    def day(self, cands, mon, since=(), toks=(), urls=None):
        d = SD.Day.__new__(SD.Day)
        d._cands = cands
        d.brevo, d.dash = mock.Mock(), mock.Mock()

        def q(sql):
            if "shadow_send_plan" in sql:
                return [{"master_key": m, "email_type": t, "planned_at": "2026-10-12 05:05:00", "run_id": "mon"}
                        for m, t in mon]
            if "mozello_orders" in sql:
                return [{"k": k} for k in since]
            if "cabinet_price_current" in sql:
                return [{"master_key": m, "token": t, "email": e} for m, t, e in toks]
            raise AssertionError(sql)
        d.q = q
        return d

    def test_dry_double_holds_both_and_shows_the_alert_without_sending(self):
        d = self.day({"reorder_1": [c("a")], "post_purchase_feedback": [c("a")]},
                     [("a", "reorder_1"), ("a", "post_purchase_feedback")])
        with mock.patch.object(DG, "brevo_attr", return_value={}):
            out = SD.Day.day_guard(d, dt.date(2026, 10, 13), False)
        self.assertEqual(out["d47_double"], 1)
        self.assertTrue(d._guard["a"].startswith("D47_DOUBLE"))
        self.assertFalse(out["alert"]["sent"])
        self.assertIn("a@x.lv", out["alert"]["text"])
        d.brevo.assert_not_called()
        d.dash.assert_not_called()

    def test_live_double_mails_raivis_once(self):
        d = self.day({"reorder_1": [c("a")], "winback_1": [c("a")]}, [("a", "reorder_1"), ("a", "winback_1")],
                     toks=[("a", "t1", "a@x.lv")])
        with mock.patch.object(DG, "brevo_attr", return_value={"a@x.lv": "https://x/?t=t1"}):
            out = SD.Day.day_guard(d, dt.date(2026, 10, 13), True)
        self.assertTrue(out["alert"]["sent"])
        posts = [x for x in d.brevo.call_args_list if x.args[1] == "/smtp/email"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].args[2]["to"], [{"email": "raivis@alenda.lv"}])

    def test_clean_day_holds_nothing(self):
        d = self.day({"reorder_1": [c("a")], "winback_1": [c("b")]}, [("a", "reorder_1"), ("b", "winback_1")],
                     toks=[("a", "t1", "a@x.lv"), ("b", "t2", "b@x.lv")])
        with mock.patch.object(DG, "brevo_attr", return_value={"a@x.lv": "https://x/?t=t1", "b@x.lv": "https://x/?t=t2"}):
            out = SD.Day.day_guard(d, dt.date(2026, 10, 13), True)
        self.assertEqual((out["d47_double"], out["d47_unexplained"], out["cab_checked"], len(d._cab)), (0, 0, 2, 0))
        self.assertNotIn("alert", out)
        d.brevo.assert_not_called()

    def test_ordered_since_monday_explains_a_new_contact(self):
        d = self.day({"reorder_1": [c("a"), c("new")]}, [("a", "reorder_1")], since=["new@x.lv"],
                     toks=[("a", "t1", "a@x.lv"), ("new", "t9", "new@x.lv")])
        with mock.patch.object(DG, "brevo_attr", return_value={"a@x.lv": "?t=t1", "new@x.lv": "?t=t9"}):
            out = SD.Day.day_guard(d, dt.date(2026, 10, 13), False)
        self.assertEqual(out["d47_unexplained"], 0)


if __name__ == "__main__":
    unittest.main()
