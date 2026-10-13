import datetime as dt
import unittest

import info_track as I

CAT = [{"letter_key": k, "ord": o, "audience": "post_purchase", "active": True} for k, o in I.SEED]
D = dt.date


class Rules(unittest.TestCase):
    def test_window_is_28_days(self):
        self.assertEqual(I.week_no(D(2026, 10, 13), D(2026, 10, 13)), 1)
        self.assertEqual(I.week_no(D(2026, 10, 20), D(2026, 10, 13)), 2)
        self.assertEqual(I.week_no(D(2026, 11, 10), D(2026, 10, 13)), 4)       # day 28
        self.assertIsNone(I.week_no(D(2026, 11, 11), D(2026, 10, 13)))         # day 29 -> akcija again
        self.assertIsNone(I.week_no(D(2026, 10, 13), None))
        self.assertIsNone(I.week_no(D(2026, 10, 12), D(2026, 10, 13)))

    def test_click_never_again_open_does_not_count_max_two(self):
        k1 = I.SEED[0][0]
        self.assertEqual(I.pick(1, CAT, set(), {}), ("INFO", k1, "WEEK_1"))
        self.assertEqual(I.pick(1, CAT, {k1}, {})[1], I.SEED[1][0])            # clicked -> next eligible
        self.assertEqual(I.pick(1, CAT, set(), {k1: 2})[1], I.SEED[1][0])      # sent twice unclicked -> used up
        self.assertEqual(I.pick(1, CAT, set(), {k1: 1})[1], k1)                # once unclicked -> still allowed
        allk = {k for k, _ in I.SEED}
        self.assertEqual(I.pick(2, CAT, allk, {}), ("EMPTY", None, "NOTHING_UNSEEN"))

    def test_outside_window_akcija_unless_a_new_letter_for_all(self):
        self.assertEqual(I.pick(None, CAT, set(), {})[0], "AKCIJA")
        cat = CAT + [{"letter_key": "info_new", "ord": 5, "audience": "all", "active": True}]
        self.assertEqual(I.pick(None, cat, set(), {}), ("INFO", "info_new", "NEW_LETTER_ALL"))
        self.assertEqual(I.pick(None, cat, {"info_new"}, {})[0], "AKCIJA")
        self.assertEqual(I.pick(None, cat, set(), {"info_new": 2})[0], "AKCIJA")

    def test_simulation_counts_sends_across_weeks(self):
        people = [{"master_key": "m", "email": "a@b.lv", "last_purchase": D(2026, 10, 10)}]
        rows = I.simulate(people, CAT[:1], {}, {}, I.tuesdays(D(2026, 10, 11), 4))
        self.assertEqual([r["slot"] for r in rows], ["INFO", "INFO", "EMPTY", "EMPTY"])   # one letter, max 2 sends

    def test_tuesdays(self):
        self.assertEqual(I.tuesdays(D(2026, 10, 11), 2), [D(2026, 10, 13), D(2026, 10, 20)])
        self.assertEqual(I.tuesdays(D(2026, 10, 13), 1), [D(2026, 10, 13)])

    def test_sql_reads_clicks_not_opens(self):
        sql = I.clicks_sql([{"letter_key": "info_x", "ord": 1, "template_id": 301}])
        self.assertIn("clicked_links_count", sql)
        self.assertIn("event = 'clicks'", sql)
        self.assertNotIn("open", sql.lower().replace("open_local", ""))
        self.assertIn("WHEN 301 THEN 'info_x'", sql)
        I.clicks_sql([{"letter_key": "info_x", "ord": 1, "template_id": None}])   # no ids yet: still valid SQL text
