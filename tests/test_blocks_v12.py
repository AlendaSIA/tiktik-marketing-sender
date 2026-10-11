"""Pins templates rebuilt on INTERFACE-BLOCKS v1.2 (MAIN 18:20, Raivis 17:55): products only from block fields,
greeting in 3 gender variants, no old product slot fields. Standard library only."""
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
AK = "{{ contact.AK_HTML|safe }}{{ contact.AK2_HTML|safe }}"
BLOCKS = {"reorder_1": FLOW, "akcija_hygostar": AK}
OLD = re.compile(r"contact\.(P\d|R\d|D\d|HERO_|XSELL_|PAP_|GROUP2?_|TOP_PROD_|LAST_GLOVE_)")


def tpl(name):
    return open(os.path.join(TEMPLATES, name + ".html"), encoding="utf-8").read()


class BlocksV12(unittest.TestCase):

    def test_block_once_greeting_once_no_old_fields(self):
        for name, tag in BLOCKS.items():
            t = tpl(name)
            self.assertEqual(t.count(tag), 1, name)
            self.assertEqual(t.count(GREET), 1, name)
            self.assertIsNone(OLD.search(t), name)
            self.assertNotIn("contact.GREETING", t, name)

    def test_236_has_maker_marker_after_own_block(self):
        t = tpl("akcija_hygostar")
        self.assertEqual(t.count("<!--MAKER_BLOCKS-->"), 1)
        self.assertLess(t.index(AK), t.index("<!--MAKER_BLOCKS-->"))


if __name__ == "__main__":
    unittest.main()
