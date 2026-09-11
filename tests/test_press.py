"""Pins MAIN's press decisions of 2026-09-11.

1. The press IS the approval. Its verdict judges three checks - AUDIENCE_CHANGED,
   PERSON_IN_TWO_LISTS, NOT_ENOUGH_CREDITS - and when all pass, record_approval writes ONE row and
   the answer says approval_recorded:true. NO_APPROVAL_ROW is not a press check any more (it made
   the press wait for its own approval: the first real press could never pass).
2. A retry with the same press_id replays: replayed:true and no second row.
3. One failing check: no approval row.
4. The verdict judges the PRESSED batch_id, never the newest day_batch of the date.
5. NO_APPROVAL_ROW lives at the send: without an approval for exactly this batch_id AND build_id,
   send_now() refuses with "Klusēšana nav piekrišana"; with one, it still raises, because the send
   path is not built.

The warehouse is an in-memory fake that answers the handful of statements the press path runs
and keeps every inserted row, so "exactly one row" is counted, not assumed.
"""
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:  # the real library in the image, a stub on a bare interpreter
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
import campaign  # noqa: E402
import press  # noqa: E402
import press_live as PL  # noqa: E402
import press_server as PS  # noqa: E402

BUILD_SHOWN = "build-shown"
BUILD_NEWER = "build-newer"


def _pv(params, name):
    for p in params or []:
        if getattr(p, "name", None) == name:
            return getattr(p, "values", None) if hasattr(p, "values") else p.value
    raise KeyError(name)


class FakeWarehouse:
    """Enough of BigQuery for the press path: day_batch, press_verdict, send_approval, overlap."""

    def __init__(self, overlap=0):
        self.overlap = overlap
        self.tables = {}
        # Two batches for ONE date. The newer one carries a different build: if anything reads
        # "the newest batch of the date" instead of the pressed batch_id, the tests see it.
        self.batches = {
            "B-shown": {"batch_id": "B-shown", "send_date": "2026-09-15",
                        "assignment_build_id": BUILD_SHOWN, "audience_total": 10,
                        "campaign_count": 1, "dedup_overlap": 0, "credit_headroom": 100,
                        "built_at": "2026-09-14T13:00:00Z"},
            "B-newer": {"batch_id": "B-newer", "send_date": "2026-09-15",
                        "assignment_build_id": BUILD_NEWER, "audience_total": 12,
                        "campaign_count": 1, "dedup_overlap": 0, "credit_headroom": 100,
                        "built_at": "2026-09-14T15:00:00Z"},
        }
        self.batch_queries = []

    def rows(self, table_suffix):
        return [r for t, rs in self.tables.items() if t.endswith(table_suffix) for r in rs]

    # bq.client().insert_rows_json
    def insert_rows_json(self, table, rows):
        self.tables.setdefault(table.strip("`"), []).extend(dict(r) for r in rows)
        return []

    def query(self, sql, params=None):
        if "day_batch" in sql and "batch_id = @b" in sql:
            b = _pv(params, "b")
            self.batch_queries.append(b)
            return [self.batches[b]] if b in self.batches else []
        if "day_batch" in sql and "send_date = @d" in sql:
            d = _pv(params, "d")  # the OLD behaviour: newest batch of the date
            cands = sorted((x for x in self.batches.values() if x["send_date"] == d),
                           key=lambda x: x["built_at"], reverse=True)
            return cands[:1]
        if "press_verdict" in sql and "press_id = @p" in sql:
            p = _pv(params, "p")
            return [r for r in self.rows("press_verdict") if r.get("press_id") == p][:1]
        if "send_approval" in sql and "press_id = @p" in sql:
            p = _pv(params, "p")
            return [r for r in self.rows("send_approval") if r.get("press_id") == p][:1]
        if "press_verdict" in sql and "verdict_id = @v" in sql:
            v = _pv(params, "v")
            return [r for r in self.rows("press_verdict") if r.get("verdict_id") == v][:1]
        if "send_approval" in sql and "batch_id = @b" in sql:
            b, i = _pv(params, "b"), _pv(params, "i")
            return [r for r in self.rows("send_approval")
                    if r.get("batch_id") == b and r.get("assignment_build_id") == i][:1]
        if "day_list_overlap" in sql:
            return [{"n": self.overlap}]
        raise AssertionError(f"unexpected SQL in the press path: {sql[:120]}")

    def scalar(self, sql, params=None):
        rows = self.query(sql, params)
        return None if not rows else list(rows[0].values())[0]


class _PressBase(unittest.TestCase):

    def setUp(self):
        self.w = FakeWarehouse()
        self._saved = (bq.query, bq.scalar, bq.client, bq.assignment_build_id,
                       campaign.credit_headroom)
        bq.query = self.w.query
        bq.scalar = self.w.scalar
        bq.client = lambda: self.w
        bq.assignment_build_id = lambda: BUILD_SHOWN
        campaign.credit_headroom = lambda: 100

    def tearDown(self):
        (bq.query, bq.scalar, bq.client, bq.assignment_build_id,
         campaign.credit_headroom) = self._saved

    @staticmethod
    def _body(press_id="P1", batch_id="B-shown", build_id=BUILD_SHOWN):
        return {"press_id": press_id, "batch_id": batch_id, "build_id": build_id,
                "send_date": "2026-09-15",
                "counts": {"audience_total": 10}, "pressed_by": "raivis",
                "pressed_at": "2026-09-15T06:00:00+03:00"}



class PressPath(_PressBase):

    def test_evaluate_judges_exactly_three_checks(self):
        checks = press.evaluate(send_date="2026-09-15", build_id_in_mail="x", live_build_id="x",
                                overlap_people=0, credits_available=5, credits_needed=5)
        self.assertEqual([c["id"] for c in checks], list(press.PRESS_CHECKS))
        self.assertNotIn(press.CHECK_NO_APPROVAL, [c["id"] for c in checks])
        self.assertTrue(press.may_press(checks))

    def test_all_three_pass_writes_one_approval_row(self):
        status, ans = PS.handle_press(self._body())
        self.assertEqual(status, 200)
        self.assertTrue(ans["approval_recorded"])
        self.assertTrue(ans["may_press"])
        self.assertFalse(ans["replayed"])
        self.assertEqual([c["id"] for c in ans["checks"]], list(press.PRESS_CHECKS))
        approvals = self.w.rows("send_approval")
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["batch_id"], "B-shown")
        self.assertEqual(approvals[0]["assignment_build_id"], BUILD_SHOWN)
        self.assertEqual(approvals[0]["press_id"], "P1")
        self.assertEqual(len(self.w.rows("press_verdict")), 1)
        self.assertEqual(self.w.rows("press_verdict")[0]["batch_id"], "B-shown")

    def test_same_press_id_replays_and_writes_nothing(self):
        PS.handle_press(self._body())
        status, ans = PS.handle_press(self._body())
        self.assertEqual(status, 200)
        self.assertTrue(ans["replayed"])
        self.assertTrue(ans["approval_recorded"])
        self.assertEqual(len(self.w.rows("send_approval")), 1)
        self.assertEqual(len(self.w.rows("press_verdict")), 1)

    def test_one_failing_check_writes_no_approval_row(self):
        self.w.overlap = 1
        status, ans = PS.handle_press(self._body(press_id="P2"))
        self.assertEqual(status, 200)
        self.assertFalse(ans["approval_recorded"])
        self.assertFalse(ans["may_press"])
        failed = [c["id"] for c in ans["checks"] if not c["passed"]]
        self.assertEqual(failed, [press.CHECK_PERSON_IN_TWO_LISTS])
        self.assertEqual(self.w.rows("send_approval"), [])

    def test_verdict_judges_the_pressed_batch_not_the_newest(self):
        # The newest batch of the date carries BUILD_NEWER; the live build equals the SHOWN one.
        # Reading by date would fail AUDIENCE_CHANGED; reading the pressed batch passes.
        status, ans = PS.handle_press(self._body())
        self.assertTrue(ans["may_press"])
        self.assertIn("B-shown", self.w.batch_queries)
        self.assertNotIn("B-newer", self.w.batch_queries)
        inputs = PL.live_inputs("2026-09-15", 100, "B-shown")
        self.assertEqual(inputs["build_id_in_mail"], BUILD_SHOWN)
        self.assertEqual(inputs["credits_needed"], 10)

    def test_live_inputs_refuses_without_a_batch(self):
        with self.assertRaises(PL.PressRefused):
            PL.live_inputs("2026-09-15", 100, None)

    def test_batch_of_another_date_fails_closed(self):
        inputs = PL.live_inputs("2026-09-16", 100, "B-shown")
        self.assertIsNone(inputs["build_id_in_mail"])

    def test_record_approval_refuses_without_its_verdict(self):
        with self.assertRaises(PL.PressRefused):
            PL.record_approval("2026-09-15", "raivis", "B-shown", {}, press_id="Px")
        with self.assertRaises(PL.PressRefused):
            PL.record_approval("2026-09-15", "raivis", "B-shown", {}, press_id="Px",
                               verdict_id="no-such-verdict")
        self.assertEqual(self.w.rows("send_approval"), [])


class ContractBv2(_PressBase):
    """MAIN's contract B v2, 2026-09-11: exact body keys, exact answer keys, 200 on every verdict."""

    def test_answer_has_exactly_the_contract_keys_fresh_and_replayed(self):
        _, fresh = PS.handle_press(self._body(press_id="K1"))
        _, again = PS.handle_press(self._body(press_id="K1"))
        for ans in (fresh, again):
            self.assertEqual(set(ans), set(PS.ANSWER_KEYS))
        self.assertEqual(PS.ANSWER_KEYS, ("verdict_id", "press_id", "batch_id", "send_date",
                                          "may_press", "approval_recorded", "checks",
                                          "refusal_text_lv", "replayed"))
        for c in fresh["checks"]:
            self.assertEqual(set(c), {"id", "passed", "reason_lv", "detail"})

    def test_no_alias_keys(self):
        _, ans = PS.handle_press(self._body(press_id="K2"))
        for alias in ("approved", "verdict", "failed_checks", "checks_failed", "shown_in_mail",
                      "verdict_may_press", "credits_available"):
            self.assertNotIn(alias, ans)

    def test_send_date_is_required(self):
        body = self._body(press_id="K3")
        del body["send_date"]
        status, ans = PS.handle_press(body)
        self.assertEqual(status, 400)
        self.assertEqual(ans["error"], "INCOMPLETE_PRESS")

    def test_field_shapes(self):
        for field, value in (("counts", "10"), ("pressed_at", "2026-09-15T06:00:00"),
                             ("send_date", "15.09.2026"), ("press_id", 123)):
            body = self._body(press_id="K4")
            body[field] = value
            status, ans = PS.handle_press(body)
            self.assertEqual(status, 400, field)
            self.assertEqual(ans["error"], "BAD_PRESS_FIELD", field)
        self.assertEqual(self.w.rows("send_approval"), [])

    def test_refusal_is_200_with_may_press_false(self):
        self.w.overlap = 3
        status, ans = PS.handle_press(self._body(press_id="K5"))
        self.assertEqual(status, 200)
        self.assertIs(ans["may_press"], False)
        self.assertIs(ans["approval_recorded"], False)
        self.assertTrue(ans["refusal_text_lv"])


class SendTimeGate(unittest.TestCase):

    def setUp(self):
        self.row = {"batch_id": "B-shown", "assignment_build_id": BUILD_SHOWN}

    def _send(self, row):
        return campaign.send_now(1, "2026-09-15", "B-shown", BUILD_SHOWN, lambda b, i: row)

    def test_no_approval_refuses_with_the_same_words(self):
        with self.assertRaises(campaign.SendRefused) as e:
            self._send(None)
        self.assertIn("Klusēšana nav piekrišana", str(e.exception))

    def test_approval_for_another_batch_or_build_refuses(self):
        for row in ({"batch_id": "B-newer", "assignment_build_id": BUILD_SHOWN},
                    {"batch_id": "B-shown", "assignment_build_id": BUILD_NEWER}):
            with self.assertRaises(campaign.SendRefused) as e:
                self._send(row)
            self.assertIn("Klusēšana nav piekrišana", str(e.exception))

    def test_revoked_approval_refuses(self):
        with self.assertRaises(campaign.SendRefused) as e:
            self._send(dict(self.row, revoked_at="2026-09-15T07:00:00Z", revoked_reason="x"))
        self.assertIn("atsaukts", str(e.exception))

    def test_matching_approval_still_does_not_send(self):
        with self.assertRaises(campaign.SendRefused) as e:
            self._send(self.row)
        self.assertIn("the send path is not built", str(e.exception))

    def test_gate_is_pure_and_keyed_on_both(self):
        ok = press.send_gate(send_date="d", batch_id="B-shown", build_id=BUILD_SHOWN,
                             approval_row=self.row)
        self.assertTrue(ok["passed"])
        self.assertEqual(ok["id"], press.CHECK_NO_APPROVAL)


if __name__ == "__main__":
    unittest.main()
