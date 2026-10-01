"""LOST QUARTERLY v1 (contract sha 17a8936613da): LQ3 90 d, LQ6 lost_capped flag, LQ7 OFFER_RUNG 4. Stdlib only."""
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import sequence as S  # noqa: E402
import pd_writeback as W  # noqa: E402
from tests.test_cadence_v1 import _job, F  # noqa: E402

D = dt.date


class LostQuarterly(unittest.TestCase):
    def test_second_lost_letter_waits_90_days(self):
        st = S.record_sent(S.State("lq", "lost_wave", D(2026, 10, 1)), "lost_quarterly", D(2026, 10, 1), 4)
        for today in (D(2026, 10, 2), D(2026, 11, 2), D(2026, 12, 29)):
            self.assertEqual(S.advance(st, F("lost"), today).next_due_on, D(2026, 12, 30))
        self.assertEqual(S.advance(st, F("lost"), D(2027, 2, 1)).next_due_on, D(2027, 2, 1))

    def test_lost_price_gate_capped_reads_rung_1_uncapped_any_row(self):
        far = D(2027, 1, 1)
        self.assertTrue(S.has_rung_price(F("lost", rungs={1: far}), 4, D(2026, 10, 1), D(2026, 10, 14), D(2026, 10, 1), capped=True))
        self.assertFalse(S.has_rung_price(F("lost", rungs={2: far}), 4, D(2026, 10, 1), D(2026, 10, 14), D(2026, 10, 1), capped=True))
        self.assertTrue(S.has_rung_price(F("lost", rungs={2: far}), 4, D(2026, 10, 1), D(2026, 10, 14), D(2026, 10, 1)))
        self.assertFalse(S.has_rung_price(F("lost", rungs=None), 4, D(2026, 10, 1), D(2026, 10, 14), D(2026, 10, 1)))

    def test_pd_field_185_has_no_option_for_4_skipped_never_guessed(self):
        fw = W.field_writes(10, email_type="lost_quarterly", send_date=D(2026, 10, 1), offer_rung=4,
                            offer_valid_until=D(2026, 10, 14))
        rung = [f for f in fw if f["field_key"] == W.F_RUNG]
        self.assertEqual([(f["object"], f["field_value"]) for f in rung], [("person_field_skipped", None)])
        self.assertNotIn(str(W.RUNG_OPTION[3]), [f["field_value"] for f in rung])
        self.assertEqual(len([f for f in fw if f["object"] == "person_field"]), 3)

    def test_job_writes_lost_capped_on_the_plan_row_and_counts_it(self):
        _job()
        src = open(os.path.join(ROOT, "sequence_job.py")).read()
        self.assertIn('"lost_capped": d.lost_capped,', src)
        self.assertIn('"lost_capped": sum(bool(r.get("lost_capped")) for r in plan_rows)', src)
        self.assertIn('"pd_rung_option_missing"', src)


if __name__ == "__main__":
    unittest.main()
