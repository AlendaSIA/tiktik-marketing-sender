"""R18 (MAIN 3 14.10): send-time state advance writes rung / rung_set_on / rung_month exactly like sequence.record_sent."""
import datetime as dt
import os
import re
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import send_day as SD  # noqa: E402
import sequence as S  # noqa: E402
import test_send_day as T  # noqa: E402

setUpModule, tearDownModule = T.setUpModule, T.tearDownModule
ON = dt.date(2026, 10, 13)


def apply_fragment(row, frag):
    """Tiny evaluator of the SET fragment with BigQuery semantics (every expression reads the OLD row)."""
    new = dict(row)
    if not frag:
        return new
    r = int(re.search(r"rung = (\d+),", frag).group(1))
    changed = (row["rung"] if row["rung"] is not None else -1) != r
    on = re.search(r"DATE '([\d-]+)'", frag).group(1)
    m = re.search(r"'(\d{4}-\d{2})', rung_month", frag).group(1)
    new["rung"] = r
    if changed:
        new["rung_set_on"], new["rung_month"] = dt.date.fromisoformat(on), m
    return new


def via_record_sent(row, et, on, rung):
    s = SEED(row)
    s = S.record_sent(s, et, on, rung)
    return {"rung": s.rung, "rung_set_on": s.rung_set_on, "rung_month": s.rung_month}


def SEED(row):
    import dataclasses as dc
    req = {f.name: None for f in dc.fields(S.State)
           if f.default is dc.MISSING and f.default_factory is dc.MISSING}
    req.update({"master_key": "m"} if "master_key" in req else {})
    s = S.State(**req)
    return dc.replace(s, rung=row["rung"], rung_set_on=row["rung_set_on"], rung_month=row["rung_month"],
                      track="winback", step=getattr(s, "step", 0) or 0)


class Fragment(unittest.TestCase):
    CASES = [
        ({"rung": None, "rung_set_on": None, "rung_month": None}, "winback_1", ON, 1),     # the 13.10 case
        ({"rung": 1, "rung_set_on": dt.date(2026, 9, 8), "rung_month": "2026-09"}, "winback_2", ON, 2),
        ({"rung": 1, "rung_set_on": ON, "rung_month": "2026-10"}, "winback_1_e2", dt.date(2026, 10, 20), 1),
        ({"rung": None, "rung_set_on": None, "rung_month": None}, "reorder_1", ON, 0),
        ({"rung": 2, "rung_set_on": dt.date(2026, 8, 1), "rung_month": "2026-08"}, S.LOST, ON, S.LOST_OFFER_RUNG),
        ({"rung": None, "rung_set_on": None, "rung_month": None}, "active_xsell", ON, 0),
    ]

    def test_same_result_as_record_sent(self):
        for row, et, on, rung in self.CASES:
            with self.subTest(et=et, rung=rung):
                self.assertEqual(apply_fragment(row, SD.state_rung_sql(et, on, rung)),
                                 {**row, **via_record_sent(row, et, on, rung)})

    def test_no_rung_no_change(self):
        self.assertEqual(SD.state_rung_sql("reorder_1", ON, 0), "")
        self.assertEqual(SD.state_rung_sql("reorder_1", ON, None), "")
        self.assertEqual(SD.state_rung_sql(S.LOST, ON, S.LOST_OFFER_RUNG), "")

    def test_text(self):
        self.assertEqual(SD.state_rung_sql("winback_1", "2026-10-13", 1),
                         ", rung = 1, rung_set_on = IF(IFNULL(rung, -1) != 1, DATE '2026-10-13', rung_set_on), "
                         "rung_month = IF(IFNULL(rung, -1) != 1, '2026-10', rung_month)")


class Advance(unittest.TestCase):
    def _day(self):
        q = T.Q([])
        return SD.Day(q, T.WH(), T.Brevo(), mock.Mock(return_value=1), mock.Mock(), T.LIVE_ENV, now=T.NOW), q

    def _ups(self, q):
        return [c for c in q.calls if c.startswith(f"UPDATE `{SD.T_STATE}`")]

    def test_batch_writes_rung(self):
        d, q = self._day()
        d.advance_batch([(f"m{i}", "winback_1", ON, 1) for i in range(5)], "B", 332)
        ups = self._ups(q)
        self.assertEqual(len(ups), 1)
        self.assertIn("rung = 1, rung_set_on = IF(", ups[0])
        self.assertIn("'2026-10'", ups[0])

    def test_batch_mixed_rungs_split_statements(self):
        d, q = self._day()
        d.advance_batch([("a", "winback_1", ON, 1), ("b", "winback_1", ON, 0)], "B", 332)
        ups = self._ups(q)
        self.assertEqual(len(ups), 2)
        self.assertEqual(sum("rung = 1" in u for u in ups), 1)
        self.assertTrue(any("'b'" in u and "rung =" not in u for u in ups))

    def test_batch_rung0_unchanged_sql(self):
        d, q = self._day()
        d.advance_batch([("a", "reorder_1", ON, 0)], "B", 330)
        self.assertNotIn("rung =", self._ups(q)[0])

    def test_single_advance_writes_rung(self):
        d, q = self._day()
        d.advance("a", "winback_1", ON, 1, "B", 332)
        self.assertIn("rung = 1", self._ups(q)[0])


if __name__ == "__main__":
    unittest.main()
