"""Pins the lifecycle templates rebuilt on INTERFACE-BLOCKS v1.3 (MAIN 2026-10-11 19:10, Raivis 18:32) and 236
HYGOSTAR on v1.2: products only from block fields, texts stay in the template, greeting in 3 gender variants, no old
product slot fields. Group rules: Ventures/tiktik.lv/marketing/_FLOW-GROUP-RULES.md. Standard library only."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "templates")
if not os.path.isdir(TEMPLATES):
    raise unittest.SkipTest("templates/ is not in the image")

GREET = ('{% if contact.GENDER == "F" and contact.UZRUNA %}Sveika, {{ contact.UZRUNA }}!'
         '{% elif contact.GENDER == "M" and contact.UZRUNA %}Sveiks, {{ contact.UZRUNA }}!{% else %}Sveiki!{% endif %}')
FLOW = "{{ contact.FLOW_HTML|safe }}{{ contact.FLOW2_HTML|safe }}"
FLOWS = "{{ contact.FLOWS_HTML|safe }}{{ contact.FLOWS2_HTML|safe }}"
FLOWR = "{{ contact.FLOWR_HTML|safe }}"
AK = "{{ contact.AK_HTML|safe }}{{ contact.AK2_HTML|safe }}"
GRID = {"reorder_1": True, "winback_1": True, "winback_1_e2": True, "welcome_1": True, "reorder_2": True,
        "reorder_3": False, "active_xsell": True}          # value = has an R part
ROWS = ["winback_2", "winback_2_e2", "winback_3", "winback_3_e2", "lost_quarterly"]
OLD = re.compile(r"contact\.(P[2-8]_|R\d_|P1_IMG|P1_FRESH|HERO_|PAP_|GROUP2?_|TOP_PROD_|LAST_GLOVE_)")


def tpl(name):
    return open(os.path.join(TEMPLATES, name + ".html"), encoding="utf-8").read()


class BlocksV13(unittest.TestCase):

    def test_grid_letters(self):
        for name, has_r in GRID.items():
            t = tpl(name)
            self.assertEqual(t.count(FLOW), 1, name)
            self.assertNotIn("FLOWS_HTML", t, name)
            self.assertEqual(t.count(FLOWR), 1 if has_r else 0, name)

    def test_rows_letters_split_a_b(self):
        for name in ROWS:
            t = tpl(name)
            self.assertEqual((t.count(FLOW), t.count(FLOWS), t.count(FLOWR)), (1, 1, 1), name)
            self.assertLess(t.index(FLOW), t.index(FLOWS), name)
            self.assertIn("{% if contact.FLOWS_HTML %}", t, name)

    def test_greeting_once_no_old_fields_no_greeting_attr(self):
        for name in list(GRID) + ROWS + ["akcija_hygostar"]:
            t = tpl(name)
            self.assertEqual(t.count(GREET), 1, name)
            self.assertIsNone(OLD.search(t), name)
            self.assertNotIn("contact.GREETING", t, name)

    def test_236_has_maker_marker_after_own_block(self):
        t = tpl("akcija_hygostar")
        self.assertEqual(t.count(AK), 1)
        self.assertLess(t.index(AK), t.index("<!--MAKER_BLOCKS-->"))


if __name__ == "__main__":
    unittest.main()
