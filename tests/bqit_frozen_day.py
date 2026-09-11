"""LIVE integration test of the frozen day (MAIN, 2026-09-11, decision 1e) against real BigQuery.

Skipped unless RUN_BQ_INTEGRATION=1. It never touches a production table: it creates a scratch
dataset zz_frozen_day_<random> in EU, copies the SCHEMA (not the data) of the three tables the
planner and the press read with CREATE TABLE ... LIKE, points BQ_CONTROL and BQ_MARTS at it, runs
the real PLANNED_SNAPSHOT_SQL and DAY_BUILD_ID_SQL, and drops the dataset afterwards.

What it proves, in MAIN's words:
  1. iesaldēta diena pēc pārbūves ir baitu līmenī tā pati (TO_JSON_STRING of every row, incl.
     snapshot_id and built_at, before == after);
  2. neiesaldēta diena mainās;
  3. prese pēc pārbūves iziet, ja iesaldētā diena nav mainīta (AUDIENCE_CHANGED passes);
  4. AUDIENCE_CHANGED krīt, ja iesaldēto dienu apzināti maina.

Run: RUN_BQ_INTEGRATION=1 python -m unittest discover -s tests -p "bqit_*.py" -v
"""
import os
import sys
import unittest
import uuid

RUN = os.environ.get("RUN_BQ_INTEGRATION") == "1"
PROD_PROJECT = os.environ.get("BQ_PROJECT", "jaunais-za-aizv04022026")
SCRATCH = "zz_frozen_day_" + uuid.uuid4().hex[:8]
if RUN:
    # Must happen BEFORE config is imported: every table name is built from these at import time.
    os.environ["BQ_CONTROL"] = SCRATCH
    os.environ["BQ_MARTS"] = SCRATCH

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@unittest.skipUnless(RUN, "live BigQuery integration test; set RUN_BQ_INTEGRATION=1")
class FrozenDayLive(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import config as C
        import bq
        from google.cloud import bigquery
        cls.C, cls.bq = C, bq
        assert C.CONTROL == SCRATCH and C.MARTS == SCRATCH, "refusing to run against production"
        ds = bigquery.Dataset(f"{C.PROJECT}.{SCRATCH}")
        ds.location = C.BQ_LOCATION
        ds.default_table_expiration_ms = 6 * 3600 * 1000  # a crashed run cleans itself up
        bq.client().create_dataset(ds)
        for src, name in (("mkt_control", "campaign_audience_snapshot"),
                          ("mkt_control", "day_batch"),
                          ("business_marts", "contact_weekly_assignment")):
            bq.query(f"CREATE TABLE `{C.PROJECT}.{SCRATCH}.{name}` "
                     f"LIKE `{PROD_PROJECT}.{src}.{name}`")
        r = bq.query("SELECT CAST(DATE_ADD(CURRENT_DATE(), INTERVAL 1 DAY) AS STRING) d1, "
                     "CAST(DATE_ADD(CURRENT_DATE(), INTERVAL 2 DAY) AS STRING) d2, "
                     "CAST(DATE_ADD(CURRENT_DATE(), INTERVAL 3 DAY) AS STRING) d3")[0]
        cls.D1, cls.D2, cls.D3 = r["d1"], r["d2"], r["d3"]

    @classmethod
    def tearDownClass(cls):
        cls.bq.client().delete_dataset(f"{cls.C.PROJECT}.{SCRATCH}",
                                       delete_contents=True, not_found_ok=True)

    # -- helpers -------------------------------------------------------------------------------
    def _assign(self, rows):
        """Replace this week's assignment with rows = [(master_key, layer, send_date, email_type)]."""
        C, bq = self.C, self.bq
        bq.query(f"DELETE FROM {C.T_ASSIGNMENT} WHERE TRUE")
        values = ", ".join(
            f"(DATE_TRUNC(CURRENT_DATE(), WEEK(MONDAY)), '{mk}', '{mk}@example.invalid', '{ly}', "
            f"'t', '{et}', 1, 'bqit', CURRENT_TIMESTAMP(), DATE '{d}', 'bqit')"
            for mk, ly, d, et in rows)
        bq.query(f"INSERT INTO {C.T_ASSIGNMENT} (week_start, master_key, email, layer, track, "
                 f"email_type, template_id, chosen_because, built_at, send_date, written_by) "
                 f"VALUES {values}")

    def _plan(self, snapshot_id, rows):
        return self.bq.write_planned_snapshot(
            snapshot_id, [r[0] for r in rows], ["utm-" + r[0] for r in rows],
            layers=[r[1] for r in rows], send_dates=[r[2] for r in rows])

    def _day_json(self, d):
        return [r["j"] for r in self.bq.query(
            f"SELECT TO_JSON_STRING(t) j FROM {self.C.T_AUDIENCE_SNAPSHOT} t "
            f"WHERE send_date = DATE '{d}' ORDER BY master_key, email_type")]

    def _press(self, frozen_build_id, d):
        import press
        checks = press.evaluate(send_date=d, build_id_in_mail=frozen_build_id,
                                live_build_id=self.bq.day_build_id(d), overlap_people=0,
                                credits_available=10, credits_needed=1)
        return {c["id"]: c for c in checks}

    # -- the proof -----------------------------------------------------------------------------
    def test_frozen_day_survives_rebuild_and_audience_changed_still_bites(self):
        C, bq, D1, D2 = self.C, self.bq, self.D1, self.D2
        week1 = [("k1", "commercial", D1, "reorder_1"), ("k2", "educational", D1, "cimdi_info_2"),
                 ("k3", "commercial", D2, "reorder_1")]
        self._assign(week1)
        self.assertEqual(self._plan("run1", week1), 3)

        # Freeze D1 exactly as batch.py does: a day_batch row whose build_id is the day hash.
        frozen_id = bq.day_build_id(D1)
        bq.query(f"INSERT INTO `{C.PROJECT}.{SCRATCH}.day_batch` (batch_id, send_date, built_at, "
                 f"built_by, assignment_build_id, audience_total) VALUES ('b1', DATE '{D1}', "
                 f"CURRENT_TIMESTAMP(), 'bqit', '{frozen_id}', 2)")
        d1_before, d2_before = self._day_json(D1), self._day_json(D2)
        self.assertEqual(len(d1_before), 2)

        # The 07:30 rebuild with a CHANGED assignment for BOTH days.
        week2 = [("k1", "commercial", D1, "winback_1"),       # would change the frozen day
                 ("k4", "commercial", D1, "reorder_1"),       # would add to the frozen day
                 ("k3", "commercial", D2, "winback_1"),       # changes the open day
                 ("k5", "educational", D2, "cimdi_info_2")]   # adds to the open day
        self._assign(week2)
        self.assertEqual(self._plan("run2", week2), 2)       # only the two D2 rows are written

        # 1. frozen day byte-identical
        self.assertEqual(self._day_json(D1), d1_before)
        # 2. open day changed: new rows, new snapshot
        d2_after = self._day_json(D2)
        self.assertNotEqual(d2_after, d2_before)
        self.assertEqual(sorted(r["master_key"] for r in bq.query(
            f"SELECT master_key FROM {C.T_AUDIENCE_SNAPSHOT} WHERE send_date = DATE '{D2}'")),
            ["k3", "k5"])
        # 3. the press after the rebuild passes AUDIENCE_CHANGED
        self.assertEqual(bq.day_build_id(D1), frozen_id)
        self.assertTrue(self._press(frozen_id, D1)["AUDIENCE_CHANGED"]["passed"])

        # 4. somebody ELSE writes the frozen day -> AUDIENCE_CHANGED fails
        bq.query(f"UPDATE {C.T_AUDIENCE_SNAPSHOT} SET email_type = 'tampered' "
                 f"WHERE send_date = DATE '{D1}' AND master_key = 'k2'")
        self.assertNotEqual(bq.day_build_id(D1), frozen_id)
        self.assertFalse(self._press(frozen_id, D1)["AUDIENCE_CHANGED"]["passed"])

    def test_z_empty_frozen_day_stays_writable(self):
        """MAIN 18:50: a day frozen EMPTY is not frozen. The rebuild writes its rows, the day's
        build_id moves off the empty hash, and the press refuses the zero day the human saw."""
        C, bq, D3 = self.C, self.bq, self.D3
        empty_id = bq.day_build_id(D3)
        self.assertEqual(empty_id, "d41d8cd98f00b204e9800998ecf8427e")   # MD5 of ''
        bq.query(f"INSERT INTO `{C.PROJECT}.{SCRATCH}.day_batch` (batch_id, send_date, built_at, "
                 f"built_by, assignment_build_id, audience_total) VALUES ('b3', DATE '{D3}', "
                 f"CURRENT_TIMESTAMP(), 'bqit', '{empty_id}', 0)")
        self.assertNotIn(D3, bq.frozen_send_dates())
        rows = [("k7", "commercial", D3, "reorder_1"), ("k8", "educational", D3, "cimdi_info_2")]
        self._assign(rows)
        self.assertEqual(self._plan("run3", rows), 2)                    # rows ARE written
        self.assertEqual(len(self._day_json(D3)), 2)
        self.assertNotEqual(bq.day_build_id(D3), empty_id)
        self.assertFalse(self._press(empty_id, D3)["AUDIENCE_CHANGED"]["passed"])


if __name__ == "__main__":
    unittest.main()
