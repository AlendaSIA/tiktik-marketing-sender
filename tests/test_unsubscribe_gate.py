"""Pins the no_unsubscribe gate (MAIN, 2026-09-11; rule #2): exactly ONE Brevo {{ unsubscribe }}."""
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
    stub.Client = object
    cloud.bigquery = stub
    google.cloud = cloud
    sys.modules["google.cloud"] = cloud
    sys.modules["google.cloud.bigquery"] = stub

import campaign as C  # noqa: E402

SHOP = '<a href="https://www.tiktik.lv/veikals?utm_campaign=2026-w37-x">s</a>'
UNSUB = '<a href="{{ unsubscribe }}">Atrakstīties</a>'


class UnsubscribeGate(unittest.TestCase):

    def _check(self, html):
        saved = (C.campaign, C.contact_attributes, C.urllib.request.urlopen)

        class _R:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        C.campaign = lambda cid, statistics="campaignStats": {"htmlContent": html}
        C.contact_attributes = lambda email: {}
        C.urllib.request.urlopen = lambda req, timeout=20: _R()
        try:
            return C.check_links(1, as_contact="t@x.lv")
        finally:
            C.campaign, C.contact_attributes, C.urllib.request.urlopen = saved

    def test_letter_without_unsubscribe_is_refused(self):
        r = self._check("<html><body>" + SHOP + "</body></html>")
        self.assertEqual(r["unsubscribe_links"], 0)
        self.assertTrue(r["no_unsubscribe"])

    def test_letter_with_exactly_one_passes(self):
        r = self._check("<html><body>" + SHOP + UNSUB + "</body></html>")
        self.assertEqual(r["unsubscribe_links"], 1)
        self.assertFalse(r["no_unsubscribe"])

    def test_two_footers_are_refused(self):
        r = self._check("<html><body>" + SHOP + UNSUB + UNSUB + "</body></html>")
        self.assertTrue(r["no_unsubscribe"])

    def test_unsubscribe_inside_a_longer_url_does_not_count(self):
        r = self._check('<a href="https://x.lv/?u={{ unsubscribe }}">x</a>' + SHOP)
        self.assertEqual(r["unsubscribe_links"], 0)
        self.assertTrue(r["no_unsubscribe"])

    def test_the_job_turns_it_into_a_named_refusal(self):
        src = open(os.path.join(ROOT, "campaign_job.py"), encoding="utf-8").read()
        self.assertIn('if links["no_unsubscribe"]:', src)
        self.assertIn('f"no_unsubscribe: ', src)


if __name__ == "__main__":
    unittest.main()
