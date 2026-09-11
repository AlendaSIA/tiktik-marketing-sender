"""Pins UTM SEAM v1 (MAIN, 2026-09-11), issued in the same words to the template builder:

  "Veidnes saitēs utm_campaign vērtība ir __UTM_WEEK__-<bāze> ... Kampaņu slānis, veidojot
   melnrakstu, nolasa veidnes HTML, aizvieto katru __UTM_WEEK__ ar utm.py nedēļas daļu (YYYY-Www)
   un veido kampaņu ar šo HTML. Ja pēc aizvietošanas HTML vēl satur __UTM_WEEK__, melnraksts tiek
   atteikts. mkt_control.utm_dictionary rindas raksta TIKAI kampaņu slānis, ar MERGE, vienu rindu
   katram (utm_campaign, utm_content) pārim."

Standard library only; Brevo and BigQuery are faked, and every refusal is proved to happen BEFORE
any campaign exists.
"""
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    from google.cloud import bigquery as _bq  # noqa: F401
except Exception:  # noqa: BLE001
    google = sys.modules.setdefault("google", types.ModuleType("google"))
    cloud = types.ModuleType("google.cloud")
    stub = types.ModuleType("google.cloud.bigquery")

    class _P:
        def __init__(self, name, typ, value):
            self.name, self.type_, self.value = name, typ, value

    stub.ScalarQueryParameter = _P
    stub.ArrayQueryParameter = _P
    stub.Client = object
    stub.QueryJobConfig = lambda **kw: kw
    cloud.bigquery = stub
    google.cloud = cloud
    sys.modules["google.cloud"] = cloud
    sys.modules["google.cloud.bigquery"] = stub

import bq  # noqa: E402
import campaign as C  # noqa: E402
import utm  # noqa: E402

WEEK = "2026-w37"
HERO = ('<a href="{{ contact.HERO_PRODUCT_URL }}{% if "?" in contact.HERO_PRODUCT_URL %}&amp;'
        '{% else %}?{% endif %}utm_source=brevo&amp;utm_medium=email&amp;'
        'utm_campaign=__UTM_WEEK__-papildinam&amp;utm_content=hero">P</a>')
SHOP = ('<a href="https://www.tiktik.lv/veikals?utm_source=brevo&amp;utm_medium=email&amp;'
        'utm_campaign=__UTM_WEEK__-papildinam&amp;utm_content=shop-all">S</a>')
TEMPLATE = ("<!DOCTYPE html><html><body>{{ contact.SVEICIENS }}"
            "{% if contact.HERO_PRODUCT_URL %}" + HERO + "{% endif %}" + SHOP + "</body></html>")


def _pv(params, name):
    for p in params or []:
        if getattr(p, "name", None) == name:
            return getattr(p, "values", None) if hasattr(p, "values") else p.value
    raise KeyError(name)


class Replacement(unittest.TestCase):

    def test_week_part_is_utm_py(self):
        self.assertEqual(utm.iso_week("2026-09-11"), WEEK)

    def test_every_marker_is_replaced_and_pairs_come_out(self):
        after, pairs = C.apply_utm_week(TEMPLATE, WEEK)
        self.assertNotIn(C.UTM_WEEK_MARKER, after)
        self.assertEqual(after.count("utm_campaign=2026-w37-papildinam"), 2)
        self.assertEqual(pairs, [("2026-w37-papildinam", "hero"),
                                 ("2026-w37-papildinam", "shop-all")])

    def test_matches_the_planner_slug(self):
        _, pairs = C.apply_utm_week(TEMPLATE, WEEK)
        self.assertEqual({c for c, _ in pairs}, {utm.slug("2026-09-07", "reorder_1", "lv")})


class Refusals(unittest.TestCase):

    def test_marker_left_behind_is_refused(self):
        for leftover in ("utm_campaign=%5F%5FUTM_WEEK%5F%5F-x", "utm_campaign=__utm_week__-x",
                         "utm_campaign=&#95;&#95;UTM_WEEK__-x"):
            html = TEMPLATE + f'<a href="https://t.lv/?{leftover}">x</a>'
            with self.assertRaises(C.UtmSeamRefused):
                C.apply_utm_week(html, WEEK)

    def test_template_without_marker_is_refused(self):
        # The live shape of 179/180 on 2026-09-11: the week written in.
        html = TEMPLATE.replace("__UTM_WEEK__", "2026-w38")
        with self.assertRaises(C.UtmSeamRefused) as e:
            C.apply_utm_week(html, WEEK)
        self.assertIn("2026-w38-papildinam", str(e.exception))

    def test_one_hardcoded_link_among_markers_is_refused(self):
        html = TEMPLATE + ('<a href="https://t.lv/?utm_campaign=2026-w38-papildinam'
                           '&amp;utm_content=x">x</a>')
        with self.assertRaises(C.UtmSeamRefused):
            C.apply_utm_week(html, WEEK)

    def test_untagged_letter_is_refused(self):
        with self.assertRaises(C.UtmSeamRefused):
            C.apply_utm_week("<p>__UTM_WEEK__</p><a href='https://t.lv/'>x</a>", WEEK)

    def test_bad_week_is_refused(self):
        with self.assertRaises(C.UtmSeamRefused):
            C.apply_utm_week(TEMPLATE, "2026-09")


class DraftUsesHtmlContent(unittest.TestCase):

    def setUp(self):
        self.calls = []
        self._saved = (C._call, C.effective_audience)
        self.html = TEMPLATE

        def fake_call(method, path, payload=None, timeout=30):
            self.calls.append((method, path, payload))
            if method == "GET" and path.startswith("/smtp/templates/"):
                return {"htmlContent": self.html, "subject": "Vai krājumi vēl turas?",
                        "isActive": False}
            if method == "POST" and path == "/emailCampaigns":
                return {"id": 999}
            raise AssertionError(f"unexpected Brevo call {method} {path}")
        C._call = fake_call
        C.effective_audience = lambda list_id: 1

    def tearDown(self):
        C._call, C.effective_audience = self._saved

    def test_campaign_is_built_from_replaced_html(self):
        d = C.create_draft(name="t", list_id=62, template_id=179, week=WEEK,
                           approved_attributes={"SVEICIENS", "HERO_PRODUCT_URL"})
        post = [c for c in self.calls if c[0] == "POST"][0][2]
        self.assertNotIn("templateId", post)
        self.assertIn("utm_campaign=2026-w37-papildinam", post["htmlContent"])
        self.assertNotIn(C.UTM_WEEK_MARKER, post["htmlContent"])
        self.assertEqual(post["recipients"]["exclusionListIds"], [4])
        self.assertEqual(post["subject"], "Vai krājumi vēl turas?")
        self.assertEqual(d["id"], 999)
        self.assertFalse(d["template_active"])

    def test_refusal_creates_no_campaign(self):
        self.html = TEMPLATE.replace("__UTM_WEEK__", "2026-w38")
        with self.assertRaises(C.UtmSeamRefused):
            C.create_draft(name="t", list_id=62, template_id=179, week=WEEK,
                           approved_attributes={"SVEICIENS", "HERO_PRODUCT_URL"})
        self.assertFalse([c for c in self.calls if c[0] == "POST"])

    def test_list_allowlist_still_holds(self):
        with self.assertRaises(C.ListNotAllowed):
            C.create_draft(name="t", list_id=3, template_id=179, week=WEEK,
                           approved_attributes=set())


class LinkRendering(unittest.TestCase):

    def test_liquid_href_is_captured_whole_and_rendered_per_contact(self):
        after, _ = C.apply_utm_week(TEMPLATE, WEEK)
        with_url = C.render_for_contact(after, {"HERO_PRODUCT_URL": "https://t.lv/p?a=1"})
        hrefs = [C.substitute(h, {"HERO_PRODUCT_URL": "https://t.lv/p?a=1"})
                 for h in C.hrefs_in(with_url)]
        self.assertIn("https://t.lv/p?a=1&amp;utm_source=brevo&amp;utm_medium=email&amp;"
                      "utm_campaign=2026-w37-papildinam&amp;utm_content=hero", hrefs)
        without = C.hrefs_in(C.render_for_contact(after, {}))
        self.assertEqual(len(without), 1)  # the hero block does not exist for this contact

    def test_unknown_liquid_stays_and_blocks(self):
        html = '{% if contact.A or contact.B %}<a href="https://t.lv/">x</a>{% endif %}'
        self.assertIn("{% if", C.render_for_contact(html, {"A": "1"}))


class DictionaryMerge(unittest.TestCase):
    """One row per (utm_campaign, utm_content); a second run adds nothing."""

    def setUp(self):
        self.table = []
        self._saved = (bq.query, bq.scalar)

        def fake_query(sql, params=None):
            assert sql is bq.UTM_MERGE_SQL
            assert "MERGE" in sql and "WHEN NOT MATCHED" in sql and "WHEN MATCHED" not in sql
            for c, ct, l in zip(_pv(params, "campaigns"), _pv(params, "contents"),
                                _pv(params, "labels")):
                key = (c, ct or None)
                if key not in [(r[0], r[1]) for r in self.table]:
                    self.table.append((c, ct or None, l))
            return []

        def fake_scalar(sql, params=None):
            keys = set(zip(_pv(params, "campaigns"), [x or None for x in _pv(params, "contents")]))
            return sum(1 for r in self.table if (r[0], r[1]) in keys)
        bq.query, bq.scalar = fake_query, fake_scalar

    def tearDown(self):
        bq.query, bq.scalar = self._saved

    def test_second_run_adds_no_duplicate(self):
        rows = [("2026-w37-papildinam", "hero", "reorder_1"),
                ("2026-w37-papildinam", "shop-all", "reorder_1"),
                ("2026-w37-papildinam", None, "reorder_1")]
        self.assertEqual(bq.merge_utm_rows(rows, "tiktik-campaign-layer", "t"), 3)
        self.assertEqual(bq.merge_utm_rows(rows + rows, "tiktik-campaign-layer", "t"), 3)
        self.assertEqual(len(self.table), 3)

    def test_null_content_travels_as_empty_and_back(self):
        sql = bq.UTM_MERGE_SQL
        self.assertIn("NULLIF(ct, '')", sql)
        self.assertIn("IFNULL(t.utm_content, '') = IFNULL(s.utm_content, '')", sql)
        self.assertIn("SELECT DISTINCT", sql)

    def test_one_pair_two_labels_is_refused(self):
        with self.assertRaises(RuntimeError):
            bq.merge_utm_rows([("x", "hero", "a"), ("x", "hero", "b")], "t", "t")
        self.assertEqual(self.table, [])

    def test_planner_whole_campaign_row_uses_the_same_merge(self):
        self.assertEqual(bq.write_utm_dictionary([("2026-w37-papildinam", "reorder_1")]), 1)
        self.assertEqual(self.table, [("2026-w37-papildinam", None, "reorder_1")])


if __name__ == "__main__":
    unittest.main()
