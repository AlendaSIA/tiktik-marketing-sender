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
        rows = [c("a"), c("b"), c("n"), c("t")]
        toks = {"a@x.lv": "87ad673d0494372ad1de3cf0d0228586", "b@x.lv": "11111111111111111111111111111111",
                "n@x.lv": "22222222222222222222222222222222"}
        urls = {"a@x.lv": "https://plani.tiktik.lv/kabinets.php?k=87ad673d0494372ad1de3cf0d0228586&tab=preces",
                "b@x.lv": "https://plani.tiktik.lv/kabinets.php?k=87ad673d0494372ad1de3cf0d0228586&tab=preces"}
        got = DG.cab_check(rows, toks, urls)
        self.assertNotIn("a", got)
        self.assertIn("another token", got["b"])
        self.assertIn("no KABINETS_URL", got["n"])
        self.assertIn("no own cabinet link", got["t"])
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
            if "send_log" in sql:
                return [{"master_key": "sentbefore", "email": "x@y.lv"}]
            if "kabinets_links" in sql:
                return [{"email": e, "token": t, "n": 1} for e, t in toks]
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
                     toks=[("a@x.lv", "t1")])
        with mock.patch.object(DG, "brevo_attr", return_value={"a@x.lv": "https://x/?t=t1"}):
            out = SD.Day.day_guard(d, dt.date(2026, 10, 13), True)
        self.assertTrue(out["alert"]["sent"])
        posts = [x for x in d.brevo.call_args_list if x.args[1] == "/smtp/email"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].args[2]["to"], [{"email": "raivis@alenda.lv"}])

    def test_clean_day_holds_nothing(self):
        d = self.day({"reorder_1": [c("a")], "winback_1": [c("b")]}, [("a", "reorder_1"), ("b", "winback_1")],
                     toks=[("a@x.lv", "t1"), ("b@x.lv", "t2")])
        with mock.patch.object(DG, "brevo_attr", return_value={"a@x.lv": "https://x/?t=t1", "b@x.lv": "https://x/?t=t2"}):
            out = SD.Day.day_guard(d, dt.date(2026, 10, 13), True)
        self.assertEqual((out["d47_double"], out["d47_unexplained"], out["cab_checked"], len(d._cab)), (0, 0, 2, 0))
        self.assertNotIn("alert", out)
        d.brevo.assert_not_called()

    def test_ordered_since_monday_explains_a_new_contact(self):
        d = self.day({"reorder_1": [c("a"), c("new")]}, [("a", "reorder_1")], since=["new@x.lv"],
                     toks=[("a@x.lv", "t1"), ("new@x.lv", "t9")])
        with mock.patch.object(DG, "brevo_attr", return_value={"a@x.lv": "?t=t1", "new@x.lv": "?t=t9"}):
            out = SD.Day.day_guard(d, dt.date(2026, 10, 13), False)
        self.assertEqual(out["d47_unexplained"], 0)


if __name__ == "__main__":
    unittest.main()


class D44(unittest.TestCase):
    OK = {"wp_run": "wp-1", "wp_at": "2026-10-13 00:31:00", "cab_at": "2026-10-13 02:50:00",
          "lf_at": "2026-10-13 03:30:00", "ak_at": "2026-10-13 03:50:00", "below_min": 0, "ge_shop": 0, "too_deep": 0,
          "ak_status": "BUILT", "ak_wp_run": "wp-1", "ak_mirror_at": "2026-10-13 02:20:00",
          "ak_assembled_at": "2026-10-13 03:40:00"}

    def test_correct_order_passes(self):
        self.assertEqual(DG.d44_eval(self.OK), {"commercial": [], "akcija_236": []})

    def test_no_tuesday_week_price_run_holds(self):
        f = {**self.OK, "wp_run": None, "wp_at": None, "ak_status": "KLUDA", "ak_wp_run": None}
        r = DG.d44_eval(f)
        self.assertEqual(r["commercial"], ["D44_WEEK_PRICE_NOT_OK"])
        self.assertIn("D44_AK_NOT_BUILT", r["akcija_236"])

    def test_each_order_break_and_price_rule(self):
        self.assertIn("D44_CAB_BEFORE_WEEK_PRICE", DG.d44_eval({**self.OK, "cab_at": "2026-10-13 00:00:00"})["commercial"])
        self.assertIn("D44_LETTER_FIELDS_BEFORE_CAB", DG.d44_eval({**self.OK, "lf_at": "2026-10-13 02:00:00"})["commercial"])
        self.assertIn("D44_AK_WRITER_BEFORE_LETTER_FIELDS",
                      DG.d44_eval({**self.OK, "ak_at": "2026-10-13 03:00:00"})["commercial"])
        self.assertIn("D44_PRICE_BELOW_MIN 2", DG.d44_eval({**self.OK, "below_min": 2})["commercial"])
        self.assertIn("D44_AK_NOT_BUILT", DG.d44_eval({**self.OK, "ak_assembled_at": "2026-10-13 02:00:00"})["akcija_236"])
        self.assertEqual(DG.d44_eval({**self.OK, "ak_assembled_at": "2026-10-13 02:00:00"})["commercial"], [])

    def test_sql_reads_the_chain_of_the_day(self):
        s = DG.d44_facts_sql(dt.date(2026, 10, 13))
        for must in ("kind = 'weekly'", "mode = 'LIVE'", "cab-20261013", "letter_fields_log", "block_push_log",
                     "ak_tuesday_log", "min_gross", "shop_gross"):
            self.assertIn(must, s)


class AlreadySent(unittest.TestCase):
    def test_second_run_same_day_holds_who_already_got_a_letter(self):
        d = Wiring().day({"winback_1": [c("a"), c("sentbefore", "x@y.lv")]}, [("a", "winback_1"), ("sentbefore", "winback_1")],
                         toks=[("a@x.lv", "t1"), ("x@y.lv", "t2")])
        with mock.patch.object(DG, "brevo_attr", return_value={"a@x.lv": "?t=t1", "x@y.lv": "?t=t2"}):
            out = SD.Day.day_guard(d, dt.date(2026, 10, 13), True)
        self.assertEqual(d._guard, {"sentbefore": "ALREADY_SENT_TODAY"})
        self.assertEqual(out["already_sent_today"], 1)


def test_r1_d44_s1_is_the_contract_definition():
    """MAIN 3 13.10 (b): D44 uses the chain's S1 - latest weekly LIVE summary today + 0 MOZ_FAIL/ERROR rows, no status."""
    import datetime as _dt
    import day_guard as _DG
    s = _DG.d44_facts_sql(_dt.date(2026, 10, 20))
    assert "kind = 'weekly' AND mode = 'LIVE'" in s and "status = 'OK'" not in s.split("cab AS")[0]
    assert "IFNULL(n_mismatch" not in s and "r.status IN ('MOZ_FAIL', 'ERROR')" in s


class D44PricesOff(unittest.TestCase):
    """S1 prices off (Raivis-approved wording, MAIN 5 19.10): the D44 gate uses the SAME S1 as the chain."""
    SKIP = {"wp_run": "SKIPPED_PRICES_OFF", "wp_at": "2026-10-20 00:30:00", "prices_enabled": False,
            "cab_at": "2026-10-20 02:50:00", "lf_at": "2026-10-20 03:30:00", "ak_at": "2026-10-20 03:50:00",
            "below_min": 0, "ge_shop": 0, "too_deep": 0, "ak_status": "BUILT", "ak_wp_run": "SKIPPED_PRICES_OFF",
            "ak_mirror_at": "2026-10-20 02:20:00", "ak_assembled_at": "2026-10-20 03:40:00"}

    def test_skipped_s1_passes_and_236_needs_the_same_run_id(self):
        self.assertEqual(DG.d44_eval(self.SKIP), {"commercial": [], "akcija_236": []})
        self.assertIn("D44_AK_NOT_BUILT", DG.d44_eval({**self.SKIP, "ak_wp_run": None})["akcija_236"])

    def test_skipped_s1_cab_before_chain_start_holds(self):
        self.assertIn("D44_CAB_BEFORE_WEEK_PRICE", DG.d44_eval({**self.SKIP, "cab_at": "2026-10-20 00:10:00"})["commercial"])

    def test_sql_skip_branch_only_when_enabled_is_false(self):
        s = DG.d44_facts_sql(dt.date(2026, 10, 20))
        self.assertEqual(DG.S1_SKIPPED, "SKIPPED_PRICES_OFF")
        self.assertIn("week_price_config` WHERE id = 1", s)
        self.assertIn("IFNULL((SELECT enabled FROM cfg), TRUE)", s)                     # enabled / no row -> old contract
        self.assertIn("SELECT 'SKIPPED_PRICES_OFF' AS run_id, TIMESTAMP(DATETIME(DATE '2026-10-20', TIME '03:30:00'), "
                      "'Europe/Riga')", s)
        self.assertIn("WHERE (SELECT enabled FROM cfg) = FALSE", s)
        self.assertIn("AS prices_enabled", s)


class D44WrittenOnly(unittest.TestCase):
    """Raivis 19.10 16:31 (MAIN 5 16:35): D44 counts only cabinet rows with status 'written' (what a client sees). A
    dropped_worse_than_shop row (never in the cabinet or a letter) no longer holds the day; a WRITTEN row >= shop still does."""

    def test_sql_counts_only_written_rows_of_the_cab_run(self):
        s = DG.d44_facts_sql(dt.date(2026, 10, 20))
        cab = s.split("cab AS (")[1].split("lf AS (")[0]
        self.assertIn("WHERE run_id = 'cab-20261020'", cab)
        self.assertIn("AND status = 'written'", cab)
        self.assertIn("COUNTIF(cabinet_price_gross >= shop_gross) AS ge_shop", cab)

    def test_dropped_rows_filtered_out_pass_written_ge_shop_holds_the_day(self):
        ok = {"wp_run": "SKIPPED_PRICES_OFF", "wp_at": "2026-10-20 00:30:00", "cab_at": "2026-10-20 02:50:00",
              "lf_at": "2026-10-20 03:30:00", "ak_at": "2026-10-20 03:50:00", "below_min": 0, "ge_shop": 0, "too_deep": 0,
              "ak_status": "BUILT", "ak_wp_run": "SKIPPED_PRICES_OFF", "ak_mirror_at": "2026-10-20 02:20:00",
              "ak_assembled_at": "2026-10-20 03:40:00"}
        # 111 dropped rows >= shop are not counted by the SQL -> ge_shop 0 -> the day passes
        self.assertEqual(DG.d44_eval(ok), {"commercial": [], "akcija_236": []})
        # one WRITTEN row >= shop -> the whole day is held (Raivis' D44 rule unchanged)
        r = DG.d44_eval({**ok, "ge_shop": 1})
        self.assertEqual(r["commercial"], ["D44_PRICE_NOT_BELOW_NEW_SHOP 1"])
        self.assertIn("D44_PRICE_NOT_BELOW_NEW_SHOP 1", r["akcija_236"])
