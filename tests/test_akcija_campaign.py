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


class ShadowV12(unittest.TestCase):
    """INTERFACE BLOCKS v1.2 (MAIN 17:45): the writer's ak_maker_html goes at <!--MAKER_BLOCKS-->."""
    maker = "ZARYS"
    FRAME = "<p>Sveiki</p>{{ contact.AK_HTML|safe }}{{ contact.AK2_HTML|safe }}" + C.PLACEHOLDER + "<p>beigas</p>"

    def _q(self, maker=True, log=True):
        import hashlib
        h = "<table>maker goods</table>"
        def q(sql):
            if sql.startswith("SELECT last_modified_time"):
                return [{"t": "2026-10-13 06:00:00"}]
            if "INFORMATION_SCHEMA.TABLES" in sql:
                return [{"x": 1}] if ("'ak_maker_html'" in sql and maker) or ("'block_push_log'" in sql and log) \
                    or "'week_price_mix16'" in sql else []
            if "ak_maker_html" in sql:
                return [{"week_id": "w", "html": h, "html_sha256": hashlib.sha256(h.encode()).hexdigest(),
                         "built_at": "t"}] if maker else []
            if "block_push_log" in sql:
                return [{"mode": "LIVE", "status": "OK", "errors": 0, "week_id": "2026-W42", "built_match": True,
                         "finished_at": "2026-10-13 06:05:00+00:00"}] if log else []
            if "akcija_week`" in sql:
                return [{"maker": self.maker}]
            if "week_price_mix16" in sql:
                return []
            if "edu_brevo_list" in sql:
                return [{"gate": "IN"}, {"gate": "IN"}, {"gate": "NO_PROMO"}]
            return []
        return q

    def test_maker_inserted_at_marker_and_gate_ok(self):
        import datetime as dt
        put = {}
        r = C.shadow(self._q(), lambda p, t: put.setdefault(p, t) and p, dt.date(2026, 10, 12), frame_fn=lambda t: self.FRAME)
        self.assertEqual(r["gate"], "OK")
        self.assertTrue(r["maker"]["sha_matches_html"])
        self.assertTrue(r["campaign"]["frame_order_ok"])
        html = next(iter(put.values()))
        self.assertIn("<table>maker goods</table><p>beigas</p>", html)
        self.assertNotIn(C.PLACEHOLDER, html)
        self.assertIn("{{ contact.AK_HTML|safe }}", html)              # own goods stay a per-contact field
        self.assertEqual(r["audience_in"], 2)

    def test_no_maker_row_holds(self):
        import datetime as dt
        r = C.shadow(self._q(maker=False), lambda p, t: p, dt.date(2026, 10, 12), frame_fn=lambda t: self.FRAME)
        self.assertTrue(r["gate"].startswith("HOLD MAKER"))
        self.assertTrue(r["maker"].startswith("HOLD MAKER"))
        self.assertNotIn("campaign", r)

    def test_frame_without_marker_holds(self):
        import datetime as dt
        r = C.shadow(self._q(), lambda p, t: p, dt.date(2026, 10, 12), frame_fn=lambda t: "<p>no marker</p>")
        self.assertTrue(r["campaign"].startswith("HOLD FRAME"))
        self.assertFalse(C.frame_order_ok(C.PLACEHOLDER + "{{ contact.AK_HTML|safe }}"))


class MixGate(ShadowV12):
    maker = "MIX"

    def test_mix_week_without_rows_holds(self):
        import datetime as dt
        r = C.shadow(self._q(), lambda p, t: p, dt.date(2026, 10, 12), frame_fn=lambda t: self.FRAME)
        self.assertEqual(r["gate"], "HOLD MIX: no week_price_mix16 rows for 2026-W42")
        self.assertEqual(r["mix_goods"], 0)

    test_maker_inserted_at_marker_and_gate_ok = None
