"""Pins contract v2.8 (d1f506313dc1) on the prepared templates - the READER's part (MAIN 2026-09-25 17:05):
P2 display on every letter that shows P-slot prices (229-235, 179, 180), P4 display and P6 on 232/233/234.
Standard library only, no network. The image does not carry templates/, so this file skips there, like
test_draft_test.py."""
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TEMPLATES = os.path.join(ROOT, "templates")
if not os.path.isdir(TEMPLATES):
    raise unittest.SkipTest("templates/ is not in the image")

import campaign as C  # noqa: E402
import draft_test as D  # noqa: E402
import draft_test_v28 as V  # noqa: E402
import presend as P  # noqa: E402

NB, EUR = chr(0xA0), chr(0x20AC)
FILES = {232: "winback_2.html", 233: "winback_3.html", 234: "lost_quarterly.html"}   # P2, P4, P6
PLAIN = {229: "welcome_1.html", 230: "reorder_2.html", 231: "reorder_3.html", 235: "active_xsell.html",
         179: "reorder_1.html", 180: "winback_1.html"}                             # P2 only
ALL = {**FILES, **PLAIN}
BASE = {"KABINETS_HAS_PRODUCTS": True, "KABINETS_URL": "https://plani.tiktik.lv/kabinets.php?k=x&tab=preces",
        "VARDS": "Raivis", "P1_NAME": "Salvetes", "P1_IMG": "https://x/1.jpg", "P2_NAME": "Cimdi",
        "P2_IMG": "https://x/2.jpg", "P2_PRICE": "4,50" + NB + EUR}
EMPTY_REFS = {f"P{i}_REF_PRICE": "" for i in range(1, 9)}
WITH_REF = dict(BASE, **EMPTY_REFS)
WITH_REF.update(P1_PRICE="0,89" + NB + EUR, P1_REF_PRICE="0,99" + NB + EUR, P1_FRESH=True,
                OFFER_VALID_UNTIL="09.10.2026", OFFER_RUNG=1)
NO_REF = dict(BASE, **EMPTY_REFS)
NO_REF.update(P1_PRICE="0,99" + NB + EUR, P1_FRESH=False, OFFER_VALID_UNTIL="", OFFER_RUNG=0)


def tpl(tid):
    return open(os.path.join(TEMPLATES, ALL[tid]), encoding="utf-8").read()


class ContractV28Display(unittest.TestCase):

    def test_no_placeholder_is_left(self):
        for tid in FILES:
            self.assertNotIn(chr(0x27E6), tpl(tid), tid)

    def test_every_rendered_field_is_in_contract_v28(self):
        for tid in ALL:
            self.assertEqual(D.static_checks(tpl(tid), "S")["outside_contract"], [], tid)
        for f in V.V28_FIELDS:
            self.assertIn(f, D.CONTRACT_FIELDS)

    def test_both_cases_pass_every_display_rule(self):
        for tid in ALL:
            for attrs in (WITH_REF, NO_REF):
                r = V.display_checks(tpl(tid), D.render(tpl(tid), attrs), "S", attrs)
                self.assertTrue(r["ok"], (tid, attrs["OFFER_VALID_UNTIL"], r))

    def test_p2_strike_only_on_the_slot_that_has_a_reference(self):
        for tid in ALL:
            out = D.render(tpl(tid), WITH_REF)
            self.assertEqual(V._STRUCK.findall(out), [("0,99" + NB + EUR, "0,89" + NB + EUR)], tid)
            self.assertEqual(out.count(V._LABEL), 1, tid)  # TAVA CENA under P1 only; P2 is a plain price
            self.assertIn("4,50" + NB + EUR, out, tid)

    def test_p2_no_reference_no_strike_no_label(self):
        for tid in ALL:
            out = D.render(tpl(tid), NO_REF)
            self.assertNotIn("line-through", out, tid)
            self.assertNotIn(V._LABEL, out, tid)
            self.assertIn("0,99" + NB + EUR, out, tid)

    def test_p4_valid_until_line_and_preheader_follow_the_field(self):
        for tid in FILES:
            on = D.render(tpl(tid), WITH_REF)
            self.assertIn(V.SPEKA_LIDZ + " 09.10.2026", "\n".join(V.visible_lines(on)), tid)
            self.assertTrue(V.preheader(on), tid)
            off = D.render(tpl(tid), NO_REF)
            self.assertNotIn(V.SPEKA_LIDZ, "\n".join(V.visible_lines(off)), tid)
            self.assertEqual(V.preheader(off), "", tid)

    def test_p6_fresh_line_is_gated_on_p1_fresh(self):
        for tid in FILES:
            self.assertEqual(P.fresh_line_blockers(tpl(tid), "P1_FRESH"), [], tid)
            self.assertEqual(len(P.fresh_line_blockers(tpl(tid), None)), 1, tid)  # the line exists
            both = dict(WITH_REF, P1_FRESH=False)
            self.assertNotIn("partij", "\n".join(V.visible_lines(D.render(tpl(tid), both))).lower(), tid)
            self.assertIn("partij", "\n".join(V.visible_lines(D.render(tpl(tid), WITH_REF))).lower(), tid)

    def test_p6_false_is_not_a_missing_field(self):
        # Rule 11: false is written, never deleted - and a missing P1_FRESH must not show the line either.
        for tid in FILES:
            gone = {k: v for k, v in WITH_REF.items() if k != "P1_FRESH"}
            self.assertNotIn("partij", "\n".join(V.visible_lines(D.render(tpl(tid), gone))).lower(), tid)

    def test_without_a_personal_price_the_body_claims_none(self):
        for tid in ALL:
            out = D.render(tpl(tid), NO_REF)
            h1 = V._text(V._H1.search(out).group(1))
            self.assertEqual([ln for ln in V.visible_lines(out) if V._CLAIM.search(ln) and ln != h1], [], tid)

    def test_heads_that_promise_a_price_are_reported(self):
        rows = json.load(open(os.path.join(ROOT, "templates_manifest.json"), encoding="utf-8"))["templates"]
        subj = {r["id"]: r["subject"] for r in rows}
        for tid, want in ((232, True), (233, True), (234, False)):
            r = V.display_checks(tpl(tid), D.render(tpl(tid), NO_REF), subj[tid], NO_REF)
            self.assertEqual(r["head_claims_a_personal_price"], want, tid)

    def test_every_tag_resolves(self):
        for tid in ALL:
            for attrs in (WITH_REF, NO_REF):
                left = [x for x in re.findall(r"\{\{.*?\}\}|\{%.*?%\}", D.render(tpl(tid), attrs), re.S)
                        if not C.is_brevo_system_link(x)]
                self.assertEqual(left, [], tid)

    def test_cases_follow_rule_11_and_p2(self):
        for name, case in V.CASES.items():
            self.assertEqual(sorted(k for k in V.V28_FIELDS if k in case), sorted(V.V28_FIELDS), name)
        self.assertIs(V.CASES["no_ref"]["P1_FRESH"], False)
        num = lambda s: float(s.replace(NB + EUR, "").replace(",", "."))  # noqa: E731
        w = V.CASES["with_ref"]
        self.assertLess(num(w["P1_PRICE"]), 0.95 * num(w["P1_REF_PRICE"]))


    def test_letters_without_those_lines_never_show_them(self):
        for tid in PLAIN:
            for attrs in (WITH_REF, NO_REF):
                body = "\n".join(V.visible_lines(D.render(tpl(tid), attrs)))
                self.assertNotIn(V.SPEKA_LIDZ, body, tid)
                self.assertNotIn("partij", body.lower(), tid)

    def test_one_price_cell_design_everywhere(self):
        cell = ('{%% if contact.P%d_REF_PRICE %%}<div><span style="color:#98a2ad;text-decoration:line-through;font-size:13px;">'
                '{{ contact.P%d_REF_PRICE }}</span> <span style="color:#12603f;font-weight:bold;font-size:17px;">{{ contact.P%d_PRICE }}'
                '</span></div><div style="color:#12603f;font-size:11px;font-weight:bold;letter-spacing:.5px;">TAVA CENA</div>{%% else %%}'
                '<div style="color:#1c2b23;font-weight:bold;font-size:15px;">{{ contact.P%d_PRICE }}</div>{%% endif %%}')
        for tid in ALL:
            for n in range(1, 9):
                self.assertEqual(tpl(tid).count(cell % (n, n, n, n)), 2 if n == 1 else 1, (tid, n))

if __name__ == "__main__":
    unittest.main()
