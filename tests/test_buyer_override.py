"""BUYER OVERRIDE (Raivis 2026-10-08 13:29, MAIN 14:00; Sūtīšanas dzinējs 7): the 04:55 step, the 07:30 re-check,
the planner's second order source and the SP patch text. No warehouse: q is a fake."""
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sql"))

import assign_early as A          # noqa: E402
import sequence as S              # noqa: E402
import sp_buyer_override as O     # noqa: E402


class Q:
    def __init__(self, orders_today=True, identity_today=True, n=3, viol=0, log_ok=1):
        self.calls, self.a = [], dict(orders_today=orders_today, identity_today=identity_today, n=n, viol=viol,
                                      log_ok=log_ok)

    def __call__(self, sql):
        self.calls.append(sql)
        if sql == A.FRESH_SQL:
            return [{"orders_today": self.a["orders_today"], "identity_today": self.a["identity_today"]}]
        if sql.startswith("SELECT COUNT(*) AS n FROM (") and A.OVR_SELECT in sql:
            return [{"n": self.a["n"]}]
        if A.T_GRAIN in sql:
            return [{"n": self.a["viol"]}]
        if sql == A.RECHECK_SQL:
            return [{"ok": self.a["log_ok"]}]
        return []


class EarlyStep(unittest.TestCase):
    def test_builds_in_order_when_sources_are_of_today(self):
        q = Q()
        self.assertEqual(A.run(q, "early"), 0)
        order = [next(k for k in ("DELETE FROM", "INSERT INTO `" + A.T_OVR, "CALL", A.T_GRAIN, "INSERT INTO `" + A.T_LOG)
                      if k in c) for c in q.calls
                 if any(k in c for k in ("DELETE FROM", "INSERT INTO", "CALL", A.T_GRAIN))]
        self.assertEqual(order, ["DELETE FROM", "INSERT INTO `" + A.T_OVR, "CALL", A.T_GRAIN, "INSERT INTO `" + A.T_LOG])
        self.assertIn("TRUE", q.calls[-1])

    def test_stale_source_builds_nothing_and_logs_not_ok(self):
        for kw in ({"orders_today": False}, {"identity_today": False}):
            q = Q(**kw)
            self.assertEqual(A.run(q, "early"), 2)
            self.assertFalse(any(c.startswith(("CALL", "DELETE")) or ("INSERT INTO `" + A.T_OVR) in c for c in q.calls))
            self.assertIn("FALSE", q.calls[-1])
            self.assertIn("SOURCE_NOT_OF_TODAY", q.calls[-1])

    def test_dry_writes_nothing(self):
        q = Q()
        self.assertEqual(A.run(q, "dry"), 0)
        self.assertFalse(any(c.lstrip().startswith(("CREATE", "INSERT", "DELETE", "CALL")) for c in q.calls))

    def test_grain_violation_is_not_ok(self):
        q = Q(viol=2)
        self.assertEqual(A.run(q, "early"), 3)
        self.assertIn("FALSE", q.calls[-1])

    def test_override_rule_text(self):
        self.assertIn("mo.payment_status = 'paid'", A.OVR_SELECT)
        self.assertIn("HAVING MAX(o.created_date) > IFNULL(ANY_VALUE(lc.last_order)", A.OVR_SELECT)   # strictly newer

    def test_recheck(self):
        self.assertTrue(A.recheck_ok(Q(log_ok=1)))
        self.assertFalse(A.recheck_ok(Q(log_ok=0)))

        def boom(sql):
            raise RuntimeError("no table")
        self.assertFalse(A.recheck_ok(boom))


class SpPatch(unittest.TestCase):
    BODY = "BEGIN\n  CREATE OR REPLACE TABLE x AS\n  SELECT 1\n" + O.OLD + "\n    AND master_key IN (1);\nEND"

    def test_one_exact_replacement(self):
        new = O.patched(self.BODY)
        self.assertEqual(new.count(f"FROM `{O.P}.mkt_control.buyer_override`"), 1)
        self.assertIn("WHERE built_on = CURRENT_DATE('Europe/Riga')", new)            # only today's rows
        self.assertTrue(new.endswith("    AND master_key IN (1);\nEND"))

    def test_refuses_a_changed_body(self):
        with self.assertRaises(SystemExit):
            O.patched("BEGIN END")
        with self.assertRaises(SystemExit):
            O.patched(self.BODY + O.OLD)


class PureHelpers(unittest.TestCase):
    def test_latest_shop_order(self):
        d1, d2 = dt.date(2026, 10, 4), dt.date(2026, 10, 7)
        self.assertEqual(S.latest_shop_order(None, d2), d2)
        self.assertEqual(S.latest_shop_order(d1, d2), d2)
        self.assertIsNone(S.latest_shop_order(None, None))
        # the 04.10 slip: no P6 deal, a paid Mozello order -> the hold now sees it
        self.assertEqual(S.recent_order_hold("lost_quarterly", None, S.latest_shop_order(None, d1),
                                             dt.date(2026, 6, 1)), S.HOLD_ORDER_AFTER)

    def test_bought_since_plan(self):
        t = dt.datetime(2026, 10, 9, 5, 5, tzinfo=dt.timezone.utc)
        after, before = t + dt.timedelta(hours=1), t - dt.timedelta(hours=1)
        self.assertTrue(S.bought_since_plan("winback_1", t, [after]))
        self.assertTrue(S.bought_since_plan("lost_quarterly", t, [before, after]))
        self.assertTrue(S.bought_since_plan("reorder_1", None, [before]))           # no plan time = fail closed
        self.assertFalse(S.bought_since_plan("winback_1", t, [before]))
        self.assertFalse(S.bought_since_plan(S.XSELL, t, [after]))                  # not a reactivation letter
        self.assertFalse(S.bought_since_plan(S.PP1, t, [after]))
        self.assertEqual(S.REACTIVATION_TYPES, {"reorder_1"} | S.LADDER_TYPES)


class SevenThirty(unittest.TestCase):
    def test_bq_recheck_mode_never_calls_the_procedure(self):
        src = open(os.path.join(ROOT, "bq.py"), encoding="utf-8").read()
        i = src.index('if os.environ.get("ASSIGN_MODE") == "recheck":')
        block = src[i:src.index('query(f"CALL {C.SP_ASSIGNMENT}()")')]
        self.assertIn("recheck_ok", block)
        self.assertIn("raise RuntimeError", block)
        self.assertIn("else:", block)                              # the CALL is only in the default branch


if __name__ == "__main__":
    unittest.main()
