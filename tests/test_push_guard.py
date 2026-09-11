"""Pins MAIN 2026-09-11 18:50, decision 1c: a batch that cannot reach the relay is an ERROR.

Exit code != 0 (so the failed-execution alert, policy 13483623152752078293, sees it), never a
quiet status=refused with exit 0. And nothing is frozen before the target is known.
"""
import datetime as dt
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

import campaign_job as J  # noqa: E402


class PushGuard(unittest.TestCase):

    def setUp(self):
        self.env = dict(os.environ)
        self.saved = (J._write, J.B.build, J._frozen, J.PUSH.push_batch, J._riga_today)
        self.written = []
        self.built = []
        J._write = lambda r: self.written.append(dict(r))
        J.B.build = lambda *a, **k: self.built.append(a) or {}

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        J._write, J.B.build, J._frozen, J.PUSH.push_batch, J._riga_today = self.saved

    def _run(self, **env):
        for k in ("RELAY_INGEST_URL", "SEND_DATE", "PUSH_BATCH_ID"):
            os.environ.pop(k, None)
        os.environ.update(env)
        return J.main()

    def test_batch_without_url_exits_nonzero_and_freezes_nothing(self):
        rc = self._run(MODE="batch")
        self.assertEqual(rc, 1)
        self.assertEqual(self.built, [])
        self.assertEqual(self.written[-1]["status"], "push_failed")
        self.assertIn("RELAY_INGEST_URL is not set", self.written[-1]["error"])

    def test_blank_url_is_the_same_as_none(self):
        self.assertEqual(self._run(MODE="batch", RELAY_INGEST_URL="   "), 1)
        self.assertEqual(self.built, [])

    def test_repush_without_url_exits_nonzero(self):
        self.assertEqual(self._run(MODE="repush", PUSH_BATCH_ID="b"), 1)
        self.assertEqual(self.written[-1]["status"], "push_failed")

    def test_push_the_relay_did_not_store_exits_nonzero(self):
        J._frozen = lambda b: {"head": {"assignment_build_id": "h"}, "campaigns": []}
        J.PUSH.push_batch = lambda built: {
            "status": J.PUSH.FAILED, "http_status": 422, "detail": "relay answered 422",
            "bytes": 10, "sha256": "x", "campaigns": 0}
        rc = self._run(MODE="repush", PUSH_BATCH_ID="b", RELAY_INGEST_URL="https://r.invalid/i")
        self.assertEqual(rc, 1)
        self.assertEqual(self.written[-1]["status"], "push_failed")
        self.assertEqual(self.written[-1]["push_http"], 422)

    def test_delivered_push_exits_zero(self):
        J._frozen = lambda b: {"head": {"assignment_build_id": "h"}, "campaigns": []}
        J.PUSH.push_batch = lambda built: {
            "status": J.PUSH.SENT, "http_status": 200, "detail": "ok", "bytes": 10,
            "sha256": "x", "campaigns": 0}
        rc = self._run(MODE="repush", PUSH_BATCH_ID="b", RELAY_INGEST_URL="https://r.invalid/i")
        self.assertEqual(rc, 0)
        self.assertEqual(self.written[-1]["status"], "ok")

    def test_pinned_past_send_date_is_refused_before_anything_is_frozen(self):
        # The job env carried SEND_DATE=2026-09-10 until 2026-09-11: a nightly batch would have
        # rebuilt that old day forever. The date check runs after preflight, so stub it out.
        saved = (J.C.credit_headroom, J.C.effective_audience, J._approved_attributes,
                 J.C.template_discount_attributes)
        J.C.credit_headroom = lambda: 1
        J.C.effective_audience = lambda lid: 1
        J._approved_attributes = lambda: set()
        J.C.template_discount_attributes = lambda tid: []
        J._riga_today = lambda: dt.date(2026, 9, 11)
        try:
            rc = self._run(MODE="batch", RELAY_INGEST_URL="https://r.invalid/i",
                           SEND_DATE="2026-09-10")
        finally:
            (J.C.credit_headroom, J.C.effective_audience, J._approved_attributes,
             J.C.template_discount_attributes) = saved
        self.assertEqual(rc, 1)
        self.assertEqual(self.built, [])
        self.assertIn("day button", self.written[-1]["error"])


if __name__ == "__main__":
    unittest.main()
