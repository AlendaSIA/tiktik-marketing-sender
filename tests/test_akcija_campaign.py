import unittest

import akcija_campaign as C

G = [{"name": "Cimdi <M>", "url": "https://www.tiktik.lv/a", "img": "https://x/i.jpg", "sale": "3.5", "std": "4.2"},
     {"name": "Teips", "url": "https://www.tiktik.lv/b", "img": "https://x/j.jpg", "sale": "1", "std": "2"},
     {"name": "Salvetes", "url": "https://www.tiktik.lv/c", "img": "https://x/k.jpg", "sale": "2", "std": "3"}]


class Region(unittest.TestCase):
    def test_cards_two_per_row_escaped_prices(self):
        h = C.region_html([{"title": "Cimdi", "items": G}])
        self.assertEqual(h.count("<tr>"), 2)
        self.assertIn("Cimdi &lt;M&gt;", h)
        self.assertIn("3,50 €", h)
        self.assertIn("line-through", h)
        self.assertNotIn("{{", h)
        self.assertNotIn("kabinets.php", h)

    def test_fill_frame_needs_exactly_one_placeholder(self):
        r = C.region_html([{"title": "", "items": G[:1]}])
        self.assertIn(r, C.fill_frame("<p>a</p>" + C.PLACEHOLDER + "<p>b</p>", r))
        for frame in ("<p>none</p>", C.PLACEHOLDER * 2):
            with self.assertRaises(ValueError):
                C.fill_frame(frame, r)
        with self.assertRaises(ValueError):
            C.fill_frame(C.PLACEHOLDER, "  ")

    def test_audience_rule_i6(self):
        s = C.AUDIENCE_SQL
        self.assertIn("WHERE x.l3", s)
        for must in ("email_suppression_all", "'NO_PROMO'", "LIST_4_SUPPRESSION", "EN_LIST", "EE_LIST", "NOT_LV"):
            self.assertIn(must, s)
        self.assertEqual(C.CAMPAIGN_LISTS, {"listIds": [3], "exclusionListIds": [4, 46, 75, 96, 98]})
