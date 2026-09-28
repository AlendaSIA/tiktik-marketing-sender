"""Pending queue for held PD records (MAIN 2026-09-28 COMMAND 5, decision 2). Standard library only."""
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pd_pending as Q  # noqa: E402
import pd_target as T  # noqa: E402

NOW = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.timezone.utc)
HELD = T.Target("held", "C2a", hold_reason="pd_duplicate_person")
OK = T.Target("person", "C1", 10, 5, (10,))


def h(mk="m1", et="winback_1", reason="pd_duplicate_person", cls="C2a"):
    return {"master_key": mk, "email": f"{mk}@x.lv", "email_type": et, "hold_reason": reason, "pd_class": cls,
            "template_id": 180, "send_date": "2026-09-28", "offer_rung": 1, "reason": "r"}


def render(r, t):
    return f"rec:{r['master_key']}:{t.person_id}"


class Queue(unittest.TestCase):
    def test_held_goes_to_queue_once_per_person_letter(self):
        rows, st = Q.step([], [h(), h()], lambda e, m: HELD, render, NOW)
        self.assertEqual((len(rows), st["pending_new"], st["pending_open"]), (1, 1, 1))
        rows, st = Q.step(rows, [h()], lambda e, m: HELD, render, NOW + dt.timedelta(days=1))
        self.assertEqual((len(rows), st["pending_new"], rows[0]["tries"], st["pending_oldest_h"]), (1, 0, 2, 24.0))

    def test_retried_every_run_and_closed_when_target_resolves(self):
        rows, _ = Q.step([], [h()], lambda e, m: HELD, render, NOW)
        rows, st = Q.step(rows, [], lambda e, m: OK, render, NOW + dt.timedelta(days=1))
        r = rows[0]
        self.assertEqual((st["pending_resolved"], st["pending_open"], r["resolved_kind"], r["resolved_person_id"],
                          r["resolved_record_json"], r["tries"]), (1, 0, "person", 10, "rec:m1:10", 2))
        rows2, st2 = Q.step(rows, [], lambda e, m: 1 / 0, render, NOW + dt.timedelta(days=2))   # closed rows not retried
        self.assertEqual((st2["pending_resolved"], rows2[0]["tries"]), (0, 2))

    def test_reason_follows_current_data_and_counts_by_reason(self):
        rows, _ = Q.step([], [h("a"), h("b", reason="pd_ambiguous_org", cls="C2c")], lambda e, m: HELD, render, NOW)
        multi = T.Target("held", "C2c", hold_reason="pd_ambiguous_org")
        rows, st = Q.step(rows, [], lambda e, m: multi, render, NOW)
        self.assertEqual(st["pending_by_reason"], {"pd_ambiguous_org": 2})

    def test_a_new_hold_after_resolution_opens_a_new_row(self):
        rows, _ = Q.step([], [h()], lambda e, m: HELD, render, NOW)
        rows, _ = Q.step(rows, [], lambda e, m: OK, render, NOW)
        rows, st = Q.step(rows, [h()], lambda e, m: HELD, render, NOW)
        self.assertEqual((st["pending_new"], st["pending_open"]), (1, 1))

    def test_job_never_holds_the_email_for_a_pd_hold(self):
        src = open(os.path.join(ROOT, "sequence_job.py")).read()
        self.assertIn("held_today.append(", src)
        self.assertIn("pd_pending.step(", src)
        # would_send is decided before the PD target and never reads it
        import re
        head = src.split("tg = resolve_now(", 1)[0]
        self.assertRegex(head, re.compile(r"^\s+would = hold is None and d\.next_email_type is not None$", re.M))
        self.assertEqual(len(re.findall(r"^\s+would = ", src, re.M)), 1)
        self.assertNotIn("tg.", src.split('would = hold is None and d.next_email_type is not None', 1)[1].split("plan_rows.append(", 1)[0])


if __name__ == "__main__":
    unittest.main()
