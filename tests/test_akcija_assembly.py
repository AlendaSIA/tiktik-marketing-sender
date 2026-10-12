"""Weekly akcija 236 per contact (contract WA v1-v1.5, 7ae3f6db1a9f): week, texts, selector, picker, assembly."""
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import akcija_assembly as A  # noqa: E402


def g(h, cat="c1", kind="k1", name=None, buyers=0):
    return {"handle": h, "name": name or h, "img": "i", "url": f"https://www.tiktik.lv/veikals/item/{h}/", "sale": "1.5",
            "std": "2", "category": cat, "kind": kind, "buyers": buyers}


def own(h, maker="ZARYS", rnk=1, rm=1):
    return {"handle": h, "maker": maker, "rnk": str(rnk), "rnk_maker": str(rm), "name": "N " + h, "name_short": "s " + h,
            "img": "i", "url": f"https://www.tiktik.lv/veikals/item/{h}/", "sale_gross": "1.49", "std_gross": "1.99"}


def rules(*rows):
    return A.parse_rules([{"block_no": i + 1, "title": r.get("title"), "selector": r["sel"], "top_n": r.get("top", 4),
                           "max_per_kind": r.get("kind"), "max_per_category": r.get("cat")} for i, r in enumerate(rows)])


class Week(unittest.TestCase):
    def test_tuesday_to_monday(self):
        self.assertEqual(A.week_of(dt.date(2026, 10, 6)), (dt.date(2026, 10, 6), "2026-W41", dt.date(2026, 10, 12)))
        # WA10: on any other day the NEXT letter is assembled - the Tuesday on or after the day
        self.assertEqual(A.week_of(dt.date(2026, 10, 7)), (dt.date(2026, 10, 13), "2026-W42", dt.date(2026, 10, 19)))
        self.assertEqual(A.week_of(dt.date(2026, 10, 12))[1], "2026-W42")        # Monday: tomorrow's letter
        self.assertEqual(A.week_of(dt.date(2026, 10, 13))[1], "2026-W42")
        self.assertEqual(A.week_of(dt.date(2026, 12, 29))[1], "2026-W53")
        self.assertEqual(A.week_of(dt.date(2026, 12, 30))[:2], (dt.date(2027, 1, 5), "2027-W01"))

    def test_same_tuesday_as_the_planner_and_wa11_window(self):
        import send_lookups as L
        J = L._job()
        for d in (dt.date(2026, 10, 5), dt.date(2026, 10, 6), dt.date(2026, 10, 7), dt.date(2026, 12, 30)):
            tue, label, first, last = J.akcija_week(d)
            self.assertEqual((tue, label), A.week_of(d)[:2])
            self.assertEqual((first, last), (tue, tue + dt.timedelta(days=6)))       # Tuesday .. Monday
            self.assertEqual((first.weekday(), last.weekday()), (1, 0))

    def test_texts(self):
        self.assertEqual(A.lidz(dt.date(2026, 10, 12)), "pirmdienai, 12. oktobrim")
        self.assertEqual(A.h1("ZARYS"), "ZARYS produktu nedēļa")
        self.assertEqual(A.h1("MIX"), "MIX izpārdošana")
        self.assertEqual(A.price("3.5"), "3,50 €")
        self.assertEqual(A.price("19.999"), "20,00 €")
        self.assertEqual(A.uzruna("Sveika, Kristīne!"), "Kristīne")
        self.assertEqual(A.uzruna("Sveiki!"), "")
        self.assertEqual(A.uzruna(None), "")

    def test_subject(self):
        self.assertEqual(A.subject("Inga", "nitrila cimdi", "ZARYS"),
                         "Inga, nitrila cimdi un citas ZARYS preces šonedēļ par īpaši labām cenām")
        self.assertEqual(A.subject("Inga", None, "ZARYS"), "Inga, ZARYS preces šonedēļ par īpaši labām cenām")
        self.assertEqual(A.subject("", "nitrila cimdi!", "ZARYS"),
                         "Nitrila cimdi un citas ZARYS preces šonedēļ par īpaši labām cenām")


class Selector(unittest.TestCase):
    def test_vocabulary(self):
        p = A.parse_selector("handles:a,b ; name~tape.*5 ?cm#3; cat:uid-1,uid-2#2;auto_cat:2;rest#1")
        self.assertEqual([(x["src"], x["n"]) for x in p],
                         [("handles", None), ("name", 3), ("cat", 2), ("auto_cat", None), ("rest", 1)])
        self.assertEqual(p[0]["arg"], ["a", "b"])
        self.assertEqual(p[3]["arg"], 2)

    def test_unreadable_raises(self):
        for bad in ("", "auto_cat:x", "auto_cat:0", "foo:1", "cat:", "name~(", "rest#0", ";"):
            with self.assertRaises(A.SelectorError, msg=bad):
                A.parse_selector(bad)
        with self.assertRaises(A.SelectorError):
            A.parse_rules([{"block_no": 1, "selector": "rest", "top_n": 0}])
        with self.assertRaises(A.SelectorError):
            A.parse_rules([{"block_no": 1, "selector": "rest", "top_n": 1}, {"block_no": 1, "selector": "rest", "top_n": 1}])


class Picker(unittest.TestCase):
    GOODS = [g("a", "c1", "k1", buyers=9), g("b", "c1", "k1", buyers=8), g("c", "c2", "k2", buyers=7),
             g("d", "c1", "k3", buyers=6), g("e", "c2", "k2", buyers=5), g("f", "c3", "k4", buyers=4)]
    CATS = ["c1", "c2", "c3"]

    def pick(self, r, shown=()):
        return A.pick_blocks(r, A.build_pools(r, self.GOODS, self.CATS), set(shown), {"c1": "Cimdi", "c2": "Teipi"})

    def test_default_rule_and_titles(self):
        out = self.pick(A.parse_rules(A.DEFAULT_RULES))
        self.assertEqual([(b["title"], [i["handle"] for i in b["items"]]) for b in out],
                         [("Cimdi", ["a", "b", "d"]), ("Teipi", ["c", "e"]), ("", ["f"])])     # auto_cat:4 is not shown

    def test_wa6_drop_then_fill_from_the_next(self):
        r = rules({"sel": "auto_cat:1", "top": 2})
        self.assertEqual([i["handle"] for i in self.pick(r)[0]["items"]], ["a", "b"])
        self.assertEqual([i["handle"] for i in self.pick(r, shown=["a"])[0]["items"]], ["b", "d"])   # filled to top N
        self.assertEqual([i["handle"] for i in self.pick(r, shown=["a", "b"])[0]["items"]], ["d"])   # short block
        self.assertEqual(self.pick(r, shown=["a", "b", "d"]), [])                                     # empty: hidden

    def test_caps_handles_first_and_part_limit(self):
        r = rules({"sel": "rest", "top": 4, "kind": 1, "cat": 2})
        self.assertEqual([i["handle"] for i in self.pick(r)[0]["items"]], ["a", "c", "d", "f"])
        r = rules({"sel": "handles:f,zz,c;name~^[ab]$#1;rest", "top": 4, "title": "T"})
        self.assertEqual([i["handle"] for i in self.pick(r)[0]["items"]], ["f", "c", "a", "b"])
        self.assertEqual(self.pick(r)[0]["title"], "T")

    def test_rest_is_what_no_earlier_block_selects_and_nothing_twice(self):
        r = rules({"sel": "cat:c1", "top": 1}, {"sel": "rest", "top": 9}, {"sel": "cat:c1,c2", "top": 9})
        out = self.pick(r)
        self.assertEqual([[i["handle"] for i in b["items"]] for b in out], [["a"], ["c", "e", "f"], ["b", "d"]])


class Assembly(unittest.TestCase):
    def one(self, own_rows, greeting="Sveika, Inga!", maker="ZARYS", goods=Picker.GOODS):
        r = A.parse_rules(A.DEFAULT_RULES)
        return A.assemble_one({"email": "x@x.lv", "master_key": "mk1"}, own_rows, greeting, maker=maker,
                              week_id="2026-W41", end=dt.date(2026, 10, 12), rules=r,
                              pools=A.build_pools(r, goods, Picker.CATS), cat_names={}, plan_date=dt.date(2026, 10, 7))

    def test_params_keys_exactly(self):
        x = self.one([own("a", rm=1), own("zz", maker="OTHER", rnk=1, rm=1)])
        self.assertEqual(list(x["params"]), ["uzruna", "h1", "lidz", "main_url", "own", "blocks"])
        self.assertEqual(set(x["params"]["own"][0]), {"name", "img", "url", "sale", "std"})
        self.assertEqual(set(x["params"]["blocks"][0]), {"title", "items"})
        self.assertEqual(x["params"]["own"], [{"name": "N a", "img": "i", "url": "https://www.tiktik.lv/veikals/item/a/",
                                               "sale": "1,49 €", "std": "1,99 €"}])
        self.assertEqual((x["n_own"], x["n_blocks"], x["n_block_goods"], x["status"]), (1, 3, 5, "OK"))
        self.assertEqual(x["subject"], "Inga, s a un citas ZARYS preces šonedēļ par īpaši labām cenām")
        self.assertNotIn("a", [i["name"] for b in x["params"]["blocks"] for i in b["items"]])

    def test_cap_12_and_the_13th_may_appear_below(self):
        rows = [own(f"o{i}", rm=i) for i in range(1, 14)] + [own("a", rm=14)]
        x = self.one(rows)
        self.assertEqual(x["n_own"], 12)
        self.assertIn("a", [i["name"] for b in x["params"]["blocks"] for i in b["items"]])

    def test_mix_week_reads_every_maker_by_rnk(self):
        x = self.one([own("h", maker="HYGOSTAR", rnk=2), own("z", maker="ZARYS", rnk=1)], maker="MIX")
        self.assertEqual([i["name"] for i in x["params"]["own"]], ["N z", "N h"])
        self.assertEqual(x["params"]["h1"], "MIX izpārdošana")

    def test_no_own_goods_fallback_and_nothing_to_show(self):
        x = self.one([], greeting="Sveiki!")
        self.assertEqual((x["subject"], x["status"], x["reason"]),
                         ("ZARYS preces šonedēļ par īpaši labām cenām", "OK", "NO_OWN_GOODS;NO_UZRUNA"))
        y = self.one([], goods=[])
        self.assertEqual((y["status"], y["n_blocks"]), ("REFUSED", 0))
        self.assertIn("NOTHING_TO_SHOW", y["reason"])

    def test_checks_catch_a_cabinet_link_and_a_repeat(self):
        x = self.one([own("a")])
        ok = {c["check_name"]: c for c in A.checks([x], audience_n=1, week_id="2026-W41", maker="ZARYS",
              rules_source="DEFAULT", goods_n=6, goods_dropped={}, audience_week="2026-W42")}
        self.assertTrue(all(c["ok"] for n, c in ok.items() if n != "akcija_asm_audience_same_letter"))
        self.assertFalse(ok["akcija_asm_audience_same_letter"]["ok"])
        x["params"]["own"][0]["url"] = "https://plani.tiktik.lv/kabinets.php?k=1"
        x["params"]["blocks"][0]["items"].append(dict(x["params"]["blocks"][0]["items"][0]))
        bad = {c["check_name"]: c["ok"] for c in A.checks([x], audience_n=2, week_id="w", maker="m", rules_source="d",
               goods_n=0, goods_dropped={}, audience_week="w")}
        self.assertFalse(bad["akcija_asm_cabinet_url_found"] or bad["akcija_asm_good_shown_twice"]
                         or bad["akcija_asm_rows_equal_audience"])


class Run(unittest.TestCase):
    def test_no_week_row_and_missing_own_goods_refuse(self):
        def q(sql):
            if "akcija_week`" in sql:
                return []
            if "shadow_akcija_audience" in sql:
                return [{"run_id": "r1", "offer_week": "2026-W45"}]
            return []
        res = A.run(q, dt.date(2026, 11, 4), record=False)                      # Wednesday -> Tuesday 10.11 = W46
        self.assertEqual(res["status"], "NO_LETTERS")
        self.assertIn("no akcija_week row for 2026-W46", res["why"])

        def q2(sql):
            if "akcija_week`" in sql:
                return [{"maker": "ZARYS"}]
            if "akcija_own_goods_log" in sql:
                return [{"status": "FAILED", "run_id": "x"}]
            if "shadow_akcija_audience" in sql:
                return [{"run_id": "r1", "offer_week": "2026-W41"}]
            return []
        res = A.run(q2, dt.date(2026, 10, 7), record=False)
        self.assertEqual(res["status"], "NO_LETTERS")
        self.assertIn("never used", res["why"])


class MixList(unittest.TestCase):
    """MAIN 19:00: a MIX week's goods = ONLY mkt_control.week_price_mix16 rows of that week; none -> HOLD MIX."""

    def _q(self, mix):
        good = lambda h, c: {"handle": h, "name": h, "img": "https://x/" + h, "url": "https://www.tiktik.lv/" + h,  # noqa: E731
                             "sale": "1", "std": "2", "category": c, "kind": "k", "buyers": 5, "maker": "Z",
                             "sellable": True}

        def q(sql):
            if "akcija_week`" in sql:
                return [{"maker": "MIX"}]
            if "akcija_own_goods_log" in sql:
                return [{"status": "OK", "run_id": "o"}]
            if "shadow_akcija_audience" in sql and "GROUP BY run_id" in sql:
                return [{"run_id": "r1", "offer_week": "2026-W44"}]
            if "shadow_akcija_audience" in sql:
                return [{"email": "a@x.lv", "master_key": "m1"}]
            if "INFORMATION_SCHEMA.TABLES" in sql:
                return [{"x": 1}]
            if "week_price_mix16" in sql:
                return [{"product_handle": h, "category": c, "rank": r, "units_30d": u} for h, c, r, *u in mix
                        for u in [u[0] if u else 0]]
            if "product_catalog" in sql and "eff_price < c.std_price" in sql and "ORDER BY buyers DESC, c.handle" in sql:
                return [good("a", "c1"), good("b", "c1"), good("z", "c2")]
            if "akcija_own_goods`" in sql:
                return [{"email": "a@x.lv", "maker": "Z", "rnk": i, "rnk_maker": i, "handle": h, "name": h,
                         "name_short": h, "img": "i", "url": "u" + h, "sale_gross": "1", "std_gross": "2"}
                        for i, h in enumerate(["z", "b"], 1)]
            return []
        return q

    def test_no_rows_holds_mix(self):
        res = A.run(self._q([]), dt.date(2026, 10, 26), record=False)          # Monday -> Tuesday 27.10 = W44
        self.assertEqual(res["status"], "NO_LETTERS")
        self.assertIn("HOLD MIX: no week_price_mix16 rows for 2026-W44", res["why"])

    def test_only_mix16_goods_and_own_goods_within_it(self):
        res = A.run(self._q([("b", "c1", 1), ("a", "c1", 2)]), dt.date(2026, 10, 26), record=False)
        self.assertEqual(res["status"], "ASSEMBLED")
        self.assertEqual(res["week_goods"], 2)                                  # "z" is not in the MIX list
        wk = [c for c in res["checks"] if c["check_name"] == "akcija_asm_week"][0]
        self.assertIn('"mix_list_not_on_sale": 0', wk["detail"])
        self.assertEqual(res["rules"], "week_price_mix16")

    def test_d37_blocks_are_the_mix16_categories_in_rank_order(self):
        rules = A.mix_rules([{"product_handle": "b", "category": "Papīrs", "rank": 2, "units_30d": 5},
                             {"product_handle": "a", "category": "Papīrs", "rank": 1, "units_30d": 5},
                             {"product_handle": "z", "category": "Cimdi", "rank": 1, "units_30d": 50}])
        self.assertEqual([(r["block_no"], r["title"], r["top_n"], r["parts"][0]["arg"]) for r in rules],
                         [(1, "Cimdi", 4, ["z"]), (2, "Papīrs", 4, ["a", "b"])])
        g = lambda h, c: {"handle": h, "name": h, "category": c, "kind": "k", "img": "i", "url": "u",  # noqa: E731
                          "sale": "1", "std": "2"}
        goods = [g("z", "leaf1"), g("a", "leaf2"), g("b", "leaf3")]
        blocks = A.pick_blocks(rules, A.build_pools(rules, goods, []), set(), {})
        self.assertEqual([(b["title"], [i["handle"] for i in b["items"]]) for b in blocks],
                         [("Cimdi", ["z"]), ("Papīrs", ["a", "b"])])      # leaf shop categories do not matter
        blocks = A.pick_blocks(rules, A.build_pools(rules, goods, []), {"a"}, {})
        self.assertEqual([i["handle"] for i in blocks[1]["items"]], ["b"])   # WA6: own good not repeated below


if __name__ == "__main__":
    unittest.main()
