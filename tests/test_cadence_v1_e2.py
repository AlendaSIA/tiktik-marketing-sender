"""CADENCE v1 K3/K5/K7 (Raivis 2026-09-30 19:11): each price episode's E2 letter holds the SAME price and date as
its E1, says the price holds until OFFER_VALID_UNTIL and then the standard shop price, carries no progression words,
and the three E2 texts differ from each other and from their E1. Standard library only."""
import json, os, sys, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import draft_test as D  # noqa: E402
import draft_test_az as A  # noqa: E402
import draft_test_v28 as V  # noqa: E402

NB, EUR = chr(0xA0), chr(0x20AC)
BASE = {"KABINETS_HAS_PRODUCTS": True, "KABINETS_URL": "https://plani.tiktik.lv/kabinets.php?k=x&tab=preces",
        "P1_NAME": "Salvetes", "P1_IMG": "https://x/1.jpg", "P1_PRICE": "0,89" + NB + EUR,
        "P1_REF_PRICE": "0,99" + NB + EUR, "P1_FRESH": False, "OFFER_VALID_UNTIL": "13.10.2026", "OFFER_RUNG": 1,
        "GREETING": "Sveika, Anna!"}
M = json.load(open(os.path.join(ROOT, "templates_manifest.json"), encoding="utf-8"))
E2 = {r["id"]: r for r in M["episode_e2"]}
E1 = {r["id"]: r for r in M["templates"] + M["live_mapped"]}


def read(row):
    return open(os.path.join(ROOT, row["file"]), encoding="utf-8").read()


class CadenceE2(unittest.TestCase):
    def test_three_e2_rows_one_per_rung(self):
        self.assertEqual(sorted(r["rung"] for r in E2.values()), [1, 2, 3])
        self.assertEqual({r["e1_template_id"] for r in E2.values()}, {180, 232, 233})

    def test_e2_holds_date_then_shop_price_and_no_progression(self):
        for tid, row in E2.items():
            out = D.render(read(row), BASE)
            body = "\n".join(V.visible_lines(out))
            self.assertIn("13.10.2026", body, tid)
            self.assertIn("pēc tam parastā veikala cena", body, tid)
            self.assertTrue(A.words_check(tid, out, D.render(row["subject"], BASE), BASE)["ok"], tid)
            self.assertIn("13.10.2026", D.render(row["subject"], BASE), tid)
            self.assertEqual(D.static_checks(read(row), row["subject"])["outside_contract"], [], tid)

    def test_texts_differ(self):
        heads = [V.preheader(D.render(read(r), BASE)) for r in E2.values()]
        self.assertEqual(len(set(heads)), 3)
        self.assertEqual(len({r["subject"] for r in E2.values()} | {E1[r["e1_template_id"]]["subject"] for r in E2.values()}), 6)

    def test_e1_rung2_head_has_no_seven_days(self):
        self.assertNotIn("7 dienas", E1[232]["subject"])
        self.assertNotIn("7 dienas", read(E1[232]).split("</h1>")[0])


if __name__ == "__main__":
    unittest.main()
