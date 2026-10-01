"""235 active_xsell intro price (Raivis 2026-10-01 17:53): R1..R4 at shop -10 % until XSELL_VALID_UNTIL, own goods
at shop price, no 'Kad nākamreiz' line. Field names PROPOSED, pending MAIN contract."""
import os, unittest
import draft_test as D, draft_test_v28 as V
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = open(os.path.join(ROOT, "templates", "active_xsell.html"), encoding="utf-8").read()
NB = "\u00a0\u20ac"
BASE = {"KABINETS_HAS_PRODUCTS": True, "KABINETS_URL": "https://plani.tiktik.lv/kabinets.php?k=x", "UZRUNA": "Elīna",
        "GREETING": "Sveika, Elīna!", "P1_NAME": "A", "P1_PRICE": "4,99" + NB, "P1_REF_PRICE": "",
        "R1_NAME": "R one", "R1_PRICE": "11,25" + NB, "R2_NAME": "R two", "R2_PRICE": "26,99" + NB}
ON = dict(BASE, R1_REF_PRICE="12,50" + NB, R2_REF_PRICE="29,99" + NB, XSELL_VALID_UNTIL="14.10.2026")

class IntroPrice(unittest.TestCase):
    def test_on(self):
        out = D.render(TPL, ON)
        body = "\n".join(V.visible_lines(out))
        self.assertEqual(out.count(V._XLABEL), 2)
        self.assertIn("Iepazīšanās cena " + V.SPEKA_LIDZ + " 14.10.2026", body)
        self.assertIn("papildu 10 procentu atlaide līdz 14.10.2026", body)
        self.assertNotIn("Kad nākamreiz", body)
        self.assertTrue(V.display_checks(TPL, out, "S", ON)["ok"])

    def test_off_is_clean(self):
        out = D.render(TPL, BASE)
        body = "\n".join(V.visible_lines(out))
        self.assertEqual(out.count(V._XLABEL), 0)
        self.assertNotIn(V.SPEKA_LIDZ, body)
        self.assertNotIn("atlaide", body)
        self.assertTrue(V.display_checks(TPL, out, "S", BASE)["ok"])

if __name__ == "__main__":
    unittest.main()
