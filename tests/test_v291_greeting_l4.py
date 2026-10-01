"""Pins contract v2.9.1 G2 and LADDER POLICY v1 L4 on the prepared templates (MAIN COMMAND 1 to Vestulu sabloni 2,
2026-09-28). Standard library only, no network; skips where templates/ is not in the image."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TEMPLATES = os.path.join(ROOT, "templates")
if not os.path.isdir(TEMPLATES):
    raise unittest.SkipTest("templates/ is not in the image")

import draft_test as D  # noqa: E402
import draft_test_az as A  # noqa: E402
import draft_test_v28 as V  # noqa: E402

FILES = {229: "welcome_1", 179: "reorder_1", 230: "reorder_2", 231: "reorder_3", 180: "winback_1",
         232: "winback_2", 233: "winback_3", 234: "lost_quarterly", 235: "active_xsell", 236: "akcija_weekly",
         9180: "winback_1_e2", 9232: "winback_2_e2", 9233: "winback_3_e2"}
TAG = "{{ contact.GREETING | default : 'Sveiki!' }}"
LINE = "<div style=\"font-size:22px;font-weight:800;color:#23303a;\">" + TAG + "</div>"   # 126 frame
NB, EUR = chr(0xA0), chr(0x20AC)
BASE = {"KABINETS_HAS_PRODUCTS": True, "KABINETS_URL": "https://plani.tiktik.lv/kabinets.php?k=x&tab=preces",
        "P1_NAME": "Salvetes", "P1_IMG": "https://x/1.jpg", "P1_PRICE": "0,89" + NB + EUR,
        "P1_REF_PRICE": "0,99" + NB + EUR, "P1_FRESH": True, "OFFER_VALID_UNTIL": "04.10.2026", "OFFER_RUNG": 2}


def tpl(tid):
    return open(os.path.join(TEMPLATES, FILES[tid] + ".html"), encoding="utf-8").read()


class GreetingAndL4(unittest.TestCase):

    def test_every_letter_prints_greeting_once_and_builds_none(self):
        for tid in FILES:
            t = tpl(tid)
            self.assertEqual(t.count(TAG), 1, tid)
            if tid != 236:
                self.assertEqual(t.count(LINE), 1, tid)
            self.assertNotIn("Sveiki, {{", t, tid)
            self.assertEqual(D.static_checks(t, "S")["outside_contract"], [], tid)

    def test_greeting_value_and_fallback(self):
        for tid in FILES:
            for attrs, want in ((dict(BASE, GREETING="Sveiks, Raivi!"), "Sveiks, Raivi!"),
                                (dict(BASE, GREETING=""), "Sveiki!"), (dict(BASE), "Sveiki!")):
                out = D.render(tpl(tid), attrs)
                self.assertTrue(A.greeting_check(out, attrs)["ok"], (tid, want))
                self.assertRegex(out, ">" + want + "(</div>| )", tid)

    def test_price_letters_have_no_progression_words(self):
        for tid in A.PRICE_LETTERS:
            for ovu in ("04.10.2026", ""):
                attrs = dict(BASE, OFFER_VALID_UNTIL=ovu)
                out = D.render(tpl(tid), attrs)
                self.assertTrue(A.words_check(tid, out, "S", attrs)["ok"], (tid, ovu))

    def test_price_letters_make_the_one_off_offer_only_with_a_date(self):
        for tid in A.PRICE_LETTERS:
            on = "\n".join(V.visible_lines(D.render(tpl(tid), BASE)))
            self.assertTrue("Saviem esošajiem klientiem šobrīd" in on or (tid == 180 and "Daļai esošo klientu" in on) or (tid == 9180 and "Akcijas ar labākām cenām daļai klientu" in on) or (tid == 232 and "Veicam noliktavas pielīdzināšanu" in on) or (tid == 233 and "ko tu pie mums pērc visbiežāk" in on) or (tid == 9233 and "Pirms nedēļas tev nosūtījām īpašu cenu" in on) or (tid == 9232 and "Pirms nedēļas daļai no tevis pirktajām precēm" in on), tid)
            self.assertIn(V.SPEKA_LIDZ + " 04.10.2026", on, tid)
            off = "\n".join(V.visible_lines(D.render(tpl(tid), dict(BASE, OFFER_VALID_UNTIL=""))))
            self.assertNotIn("Saviem esošajiem klientiem", off, tid)
            self.assertNotIn("Daļai esošo klientu", off, tid)
            self.assertNotIn("Akcijas ar labākām cenām daļai klientu", off, tid)
            self.assertNotIn("noliktavas pielīdzināšanu", off, tid)

    def test_reorder_letters_carry_no_discount_words(self):
        for tid in A.REORDER_LETTERS:
            attrs = dict(BASE, OFFER_VALID_UNTIL="", OFFER_RUNG=0)
            self.assertTrue(A.words_check(tid, D.render(tpl(tid), attrs), "S", attrs)["ok"], tid)

    def test_neutralise_leaves_no_customer_cabinet_link(self):
        html = '<a href="https://plani.tiktik.lv/kabinets.php?k=abc&amp;tab=preces">x</a><a href="https://www.tiktik.lv/veikals/item/a/">y</a>'
        out, n, left = A.neutralise(html, "https://plani.tiktik.lv/kabinets.php?k=OWN")
        self.assertEqual((n, left), (1, []))
        self.assertNotIn("k=abc", out)
        self.assertIn("veikals/item/a/", out)
        out, n, left = A.neutralise(html, "#")
        self.assertEqual((n, left), (1, []))
        self.assertIn('href="#"', out)

    def test_mask(self):
        self.assertEqual(A.mask("edgarssigai@gmail.com"), "ed***@gmail.com")


if __name__ == "__main__":
    unittest.main()
