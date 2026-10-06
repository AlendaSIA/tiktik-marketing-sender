"""Send-time lookups (Sūtīšanas dzinējs 5, 2026-10-06): the four send_path lookups answered from the tables, with a
fake query that returns what the bq CLI returns (every scalar as text). L8 / L9 / L10 run through send_path's own
lock functions."""
import datetime as dt
import os
import sys
import unittest
import unittest.mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import presend as G  # noqa: E402
import send_lookups as L  # noqa: E402
import send_path as SP  # noqa: E402
import sequence as S  # noqa: E402

J = L._job()
D = "2026-10-06"
NOW = dt.datetime(2026, 10, 6, 7, 0, tzinfo=dt.timezone.utc)          # 10:00 Riga


def plan(mk, email, et, tid, rung=0, due=D, ovu=None, xvu=None, order=None, ws="true"):
    return {"master_key": mk, "email": email, "email_type": et, "template_id": str(tid), "offer_rung": str(rung),
            "planned_send_date": due, "offer_valid_until": ovu, "xsell_valid_until": xvu, "trigger_order_nr": order,
            "would_send": ws, "hold_reason": None, "lost_capped": None, "run_id": "plan-1"}


def lf(email, et, letter="RUNG", rung=0, zero="false", ovu="", xvu="", r1="", url="", order=""):
    return {"email": email, "email_type": et, "letter": letter, "rung": str(rung), "g15_zero_priced": zero,
            "R1_REF_PRICE": r1, "OFFER_VALID_UNTIL": ovu, "XSELL_VALID_UNTIL": xvu, "ANKETA_URL": url,
            "ORDER_NR": order, "plan_run_id": "plan-1", "run_id": "lf-1"}


PLAN = [
    plan("m1", "a@x.lv", "winback_1", 180, 1, ovu="2026-10-19"),
    plan("m2", "b@x.lv", "winback_1", 180, 1, ovu="2026-10-19"),
    plan("m3", "c@x.lv", "winback_1", 180, 1, ovu="2026-10-19"),
    plan("m4", "d@x.lv", "winback_1", 180, 1, ovu="2026-10-19"),
    plan("m5", "e@x.lv", "winback_1", 180, 1, ovu="2026-10-19"),
    plan("m6", "f@x.lv", S.XSELL, 235, xvu="2026-10-19"),
    plan("m7", "g@x.lv", S.PP1, 244, order="M-1"),
    plan("m8", "h@x.lv", S.PP1, 244, order="M-2"),
    plan("m9", "i@x.lv", "reorder_1", 179, due="2026-10-07"),
    plan("m10", "j@x.lv", "winback_1", 180, 1, ovu="2026-10-19"),
    plan("m11", "k@x.lv", "reorder_1", 179),
    plan("m12", "l@x.lv", "reorder_1", 179, ws="false"),
]
LF = [
    lf("a@x.lv", "winback_1", rung=1, ovu="19.10.2026"),
    lf("b@x.lv", "winback_1", rung=1, zero="true"),
    lf("d@x.lv", "winback_1", letter="EXCLUDED", rung=1),
    lf("e@x.lv", "winback_1", rung=1, ovu="20.10.2026"),
    lf("f@x.lv", S.XSELL, letter="XS", xvu="19.10.2026", r1="5,49 €"),
    lf("g@x.lv", S.PP1, letter="STD", url="https://plani.tiktik.lv/atsauksme.php?o=M-1&t=x", order="M-1"),
    lf("h@x.lv", S.PP1, letter="STD", url="https://plani.tiktik.lv/atsauksme.php?o=M-9&t=x", order="M-9"),
    lf("j@x.lv", "winback_1", rung=1, ovu="19.10.2026"),
    lf("k@x.lv", "reorder_1", letter="STD"),
    lf("i@x.lv", "reorder_1", letter="STD"),
]


AKCIJA = [{"master_key": m, "in_audience": "true", "excluded_reason": None} for m in ("m1", "m2", "m6", "m7", "m9", "m13")] + [
    {"master_key": "m10", "in_audience": "false", "excluded_reason": "B2B_FLOW"},
    {"master_key": "m11", "in_audience": "false", "excluded_reason": "EN_PENDING"},
    {"master_key": "m14", "in_audience": "false", "excluded_reason": "PERSONAL_LETTER_THIS_WEEK"}]


H1, H2 = "a" * 64, "b" * 64
_T = (NOW - dt.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
TC = [{"template_id": "180", "email_type": "winback_1", "approved": "true", "approved_sha256": H1, "approved_commit": "c1", "html_sha256": H1, "error": None, "checked_at": _T},
      {"template_id": "234", "email_type": "lost_quarterly", "approved": "true", "approved_sha256": H1, "approved_commit": "c2", "html_sha256": H2, "error": None, "checked_at": _T},
      {"template_id": "179", "email_type": "reorder_1", "approved": "true", "approved_sha256": None, "approved_commit": None, "html_sha256": H2, "error": None, "checked_at": _T},
      {"template_id": "235", "email_type": S.XSELL, "approved": "true", "approved_sha256": H1, "approved_commit": "c3", "html_sha256": H1, "error": None,
       "checked_at": (NOW - dt.timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")},
      {"template_id": "9180", "email_type": "winback_1_e2", "approved": "true", "approved_sha256": None, "approved_commit": None, "html_sha256": None, "error": None, "checked_at": None}]


class Fake:
    """query(sql) -> rows, matched on the SQL TEXT the engine really sends."""

    def __init__(self, **over):
        self.calls, self.over = [], over

    def __call__(self, sql):
        self.calls.append(sql)
        o = self.over
        if "GROUP BY run_id" in sql:
            return o.get("run", [{"run_id": "plan-1"}])
        if L.T_LFLOG in sql:
            return o.get("log", [{"run_id": "lf-1", "status": "OK", "plan_run_id": "plan-1"}])
        if f"FROM `{L.T_PLAN}`" in sql:
            return [dict(r) for r in PLAN]
        if f"FROM `{L.T_AKCIJA}`" in sql and sql.startswith("SELECT"):
            return [dict(r) for r in o.get("akcija", AKCIJA)]
        if f"FROM `{J.T_APPROVAL}` a LEFT JOIN" in sql:
            return [dict(r) for r in o.get("tc", TC)]
        if f"FROM `{L.T_LF}`" in sql:
            return [dict(r) for r in o.get("lf", LF)]
        fresh = (NOW - dt.timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        answers = {
            J.RUNG_BUILT_SQL: [{"built_at": o.get("rung_built", fresh)}],
            J.LQXS_BUILT_SQL: [{"built_at": fresh}],
            J.RUNG_PRICE_SQL: [{"master_key": m, "vu_r1": "2026-10-20", "vu_r2": None, "vu_r3": None}
                               for m in ("m1", "m2", "m3", "m4", "m5", "m10")],
            J.LQXS_SQL: [{"master_key": "m6", "vu_lost": None, "vu_lost_capped": None, "xs_handles": ["h1"],
                          "vu_xs": "2026-10-20"}],
            J.R_SQL: [{"email": p["email"], "r": ["h1", "h2"], "r_cab": ["h1", "h2"]} for p in PLAN],
            J.OFFERED_SQL: [],
            J.APPROVAL_SQL: o.get("approved", [{"template_id": "180", "email_type": "winback_1"}, {"template_id": "235", "email_type": S.XSELL},
                             {"template_id": "244", "email_type": S.PP1}, {"template_id": "179", "email_type": "reorder_1"}]),
            J.FLOW_SQL: o.get("flows", [{"master_key": "m10", "flow": "B2B", "src": "pd_field_309"}]),
            J.EN_SQL: [{"master_key": "m11"}],
            J.BUYER_SQL: o.get("buyers", [{"master_key": p["master_key"]} for p in PLAN]),
            J.KAB_SQL: o.get("kab", [{"email": p["email"]} for p in PLAN]),
        }
        if sql in answers:
            return answers[sql]
        if sql.startswith(("DELETE", "INSERT", "UPDATE")):
            return []
        raise AssertionError("unexpected query: " + sql[:120])


def gates_of(wh, mk, et, rung=0):
    ctx = wh.presend_ctx({"email_type": et}, D, [mk], NOW)
    return G.gates(et, rung, ctx.get(mk) or G.Ctx())


class L8OnTables(unittest.TestCase):
    def test_each_gate_from_the_rows(self):
        wh = L.Warehouse(Fake())
        self.assertEqual(gates_of(wh, "m1", "winback_1", 1), [])                       # priced row, dates equal
        self.assertEqual(gates_of(wh, "m2", "winback_1", 1), [G.NO_PRICED])            # G15 from the row
        self.assertEqual(gates_of(wh, "m3", "winback_1", 1), [G.NO_LF])                # no row: only that
        self.assertEqual(gates_of(wh, "m4", "winback_1", 1), [G.EXCLUDED])
        self.assertEqual(gates_of(wh, "m5", "winback_1", 1), [G.DATE_MISMATCH])        # DW2
        self.assertEqual(gates_of(wh, "m6", S.XSELL), [])
        self.assertEqual(gates_of(wh, "m7", S.PP1), [])
        self.assertEqual(gates_of(wh, "m8", S.PP1), [G.NO_ANKETA])                     # WO3: link of another order

    def test_no_ctx_for_another_letter_a_later_day_or_a_held_row_and_the_lock_refuses(self):
        wh = L.Warehouse(Fake())
        self.assertEqual(wh.presend_ctx({"email_type": "winback_2"}, D, ["m1"], NOW), {})
        self.assertEqual(wh.presend_ctx({"email_type": "reorder_1"}, D, ["m9", "m12", "nobody"], NOW), {})
        closed = SP.presend_lock(campaign={"email_type": "winback_2", "rung": 2}, send_date=D,
                                 audience=[{"master_key": "m1"}], presend_ctx=lambda *a: wh.presend_ctx(*a, NOW))
        self.assertEqual(closed[0][0], "L8")

    def test_stale_price_table_is_no_price(self):
        old = (NOW - dt.timedelta(hours=40)).strftime("%Y-%m-%d %H:%M:%S")
        wh = L.Warehouse(Fake(rung_built=old))
        self.assertTrue(wh._shared(NOW)["rung_stale"])

    def test_one_builder_for_planner_and_send_path(self):
        job, look = open(os.path.join(ROOT, "sequence_job.py")).read(), open(os.path.join(ROOT, "send_lookups.py")).read()
        self.assertIn("G.build_ctx(", job)
        self.assertNotIn("G.Ctx(", job)
        self.assertIn("G.build_ctx(", look)
        self.assertNotIn("G.Ctx(\n", look)


class TemplateContentTC4(unittest.TestCase):
    def test_sql_is_closed_on_null_mismatch_and_stale_mirror(self):
        sql = J.APPROVAL_SQL
        for must in ("a.approved_sha256 IS NOT NULL", "LOWER(TRIM(a.approved_sha256)) = b.html_sha256",
                     "JOIN `" + J.T_BREVO_HASH + "` b ON b.template_id = a.template_id", "b.checked_at >= TIMESTAMP_SUB",
                     "LENGTH(b.html_sha256) = 64"):
            self.assertIn(must, sql)
        self.assertNotIn("LEFT JOIN", sql)

    def test_l4_from_the_mirror_and_live(self):
        wh = L.Warehouse(Fake())
        tc = {t["template_id"]: t for t in wh.template_content(NOW)}
        self.assertEqual({k: v["content_approved"] for k, v in tc.items()},
                         {180: True, 234: False, 179: False, 235: False, 9180: False})   # mismatch, NULL, stale mirror
        self.assertTrue(wh.template_approved(180))
        for tid in (234, 179, 235, 9180, 999):
            self.assertFalse(wh.template_approved(tid), tid)
        self.assertTrue(wh.template_approved(180, live_hash=lambda i: H1))            # live Brevo read wins over the mirror
        self.assertFalse(wh.template_approved(180, live_hash=lambda i: H2))
        self.assertTrue(wh.template_approved(235, live_hash=lambda i: H1))            # live: the stale mirror does not matter
        self.assertFalse(wh.template_approved(179, live_hash=lambda i: H2))           # NULL stays closed

    def test_nothing_approved_closes_every_letter_first(self):
        wh = L.Warehouse(Fake(approved=[]))
        self.assertEqual(gates_of(wh, "m1", "winback_1", 1), [G.T_NOT_APPROVED])
        self.assertEqual(gates_of(wh, "m3", "winback_1", 1), [G.T_NOT_APPROVED, G.NO_LF])
        res = L.evaluate(wh, D, NOW)
        self.assertEqual(sum(r["deliverable"] for r in res["rows"]), 0)

    def test_mirror_checks(self):
        res = L.evaluate(L.Warehouse(Fake()), D, NOW)
        res["template_content"] = {"refresh_error": None, "refreshed": True, "rows": L.Warehouse(Fake()).template_content(NOW)}
        cs = {c["check_name"]: c for c in L.checks(res)}
        self.assertFalse(cs["sendtime_template_hash_mirror_fresh"]["ok"])              # 235's mirror row is 30 h old
        self.assertEqual(cs["sendtime_template_content"]["value"], "1")
        fresh = [t for t in res["template_content"]["rows"] if t["template_id"] != 235]
        res["template_content"] = {"refresh_error": None, "refreshed": True, "rows": fresh}
        self.assertTrue({c["check_name"]: c for c in L.checks(res)}["sendtime_template_hash_mirror_fresh"]["ok"])
        res["template_content"]["refresh_error"] = "job failed"
        self.assertFalse({c["check_name"]: c for c in L.checks(res)}["sendtime_template_hash_mirror_fresh"]["ok"])


class L9OnTables(unittest.TestCase):
    def test_b2b_and_en(self):
        wh = L.Warehouse(Fake())
        self.assertEqual(wh.person_blocks(D, ["m1", "m10", "m11"], NOW), {"m10": S.HOLD_B2B, "m11": S.HOLD_EN})
        closed = SP.person_lock(send_date=D, audience=[{"master_key": "m10"}],
                                person_blocks=lambda *a: wh.person_blocks(*a, NOW))
        self.assertEqual(closed, [("L9", "person re-check: B2B_FLOW 1")])

    def test_pa1_pa2_rechecked_at_send(self):
        # m1 is not a tiktik buyer; m5 (winback_1) and m6 (235) have no cabinet products; m10 stays B2B, m11 stays EN
        wh = L.Warehouse(Fake(buyers=[{"master_key": p["master_key"]} for p in PLAN if p["master_key"] != "m1"],
                              kab=[{"email": p["email"]} for p in PLAN if p["master_key"] not in ("m5", "m6")]))
        self.assertEqual(wh.person_blocks(D, ["m1", "m2", "m5", "m6", "m10", "m11"], NOW),
                         {"m1": S.HOLD_NOT_BUYER, "m5": S.HOLD_RULE8A, "m10": S.HOLD_B2B, "m11": S.HOLD_EN})   # 235 has no rule 8a
        res = L.evaluate(wh, D, NOW)
        by = {r["master_key"]: r for r in res["rows"]}
        self.assertFalse(by["m1"]["deliverable"])
        self.assertFalse(by["m5"]["deliverable"])
        self.assertTrue(by["m6"]["deliverable"])
        with self.assertRaises(RuntimeError):
            L.Warehouse(Fake(buyers=[])).person_blocks(D, ["m1"], NOW)

    def test_empty_guard_source_is_no_answer(self):
        with self.assertRaises(RuntimeError):
            L.Warehouse(Fake(flows=[])).person_blocks(D, ["m1"], NOW)


class L10OnTables(unittest.TestCase):
    def lock(self, now=NOW, **over):
        wh = L.Warehouse(Fake(**over))
        return SP.window_lock(send_date=D, now=now, letter_fields=wh.letter_fields, plan_run=wh.plan_run)

    def test_open_refused_and_why(self):
        self.assertEqual(self.lock(), [])
        self.assertIn("outside the send window", self.lock(now=NOW.replace(hour=5, minute=59))[0][1])
        self.assertIn("not OK", self.lock(log=[])[0][1])
        self.assertIn("not OK", self.lock(log=[{"run_id": "lf-2", "status": "FAILED", "plan_run_id": "plan-1"}])[0][1])
        self.assertIn("rebuilt after the letter writer", self.lock(run=[{"run_id": "plan-2"}])[0][1])
        self.assertIn("no plan run", self.lock(run=[])[0][1])

    def test_window_open_moves_0855_to_0900_and_keeps_later_times(self):
        early = dt.datetime(2026, 10, 6, 5, 55, tzinfo=dt.timezone.utc)                # 08:55 Riga
        self.assertEqual(SP.riga(L.window_open(early)).strftime("%H:%M"), "09:00")
        self.assertEqual(L.window_open(NOW), NOW)

    def test_a_date_that_is_not_a_date_never_reaches_sql(self):
        q = Fake()
        L.Warehouse(q).plan_run("2026-10-06' OR 1=1 --")              # only the ISO date part is ever used
        self.assertIn("plan_date = DATE '2026-10-06' GROUP BY", q.calls[0])
        self.assertNotIn("1=1", q.calls[0])
        with self.assertRaises(ValueError):
            L.Warehouse(Fake()).plan_run("tomorrow")


class EvaluateAndRecord(unittest.TestCase):
    def test_send_time_answer(self):
        res = L.evaluate(L.Warehouse(Fake()), D, NOW)
        self.assertEqual((res["L10"], res["would_send"], res["due_today"], res["planned_later"]),
                         ([], 11, 10, {"reorder_1": 1}))
        by = {r["master_key"]: r for r in res["rows"]}
        self.assertEqual(sorted(m for m, r in by.items() if r["deliverable"]), ["m1", "m6", "m7"])
        self.assertEqual((by["m10"]["gates"], by["m10"]["person_block"]), ([], S.HOLD_B2B))
        camp = {c["email_type"]: c for c in res["campaigns"]}
        self.assertEqual((camp["winback_1"]["audience"], camp["winback_1"]["deliverable"]), (6, 1))
        self.assertEqual(camp["winback_1"]["L8"][0][0], "L8")
        self.assertEqual(camp["winback_1"]["L9"], [("L9", "person re-check: B2B_FLOW 1")])
        cs = {c["check_name"]: c for c in L.checks(res)}
        self.assertTrue(cs["sendtime_l10_writer_ok_on_latest_plan"]["ok"])
        self.assertFalse(cs["sendtime_person_blocked_among_due"]["ok"])                 # hard: plan and send path disagree
        self.assertEqual(cs["sendtime_deliverable_by_type"]["value"], "3")
        self.assertEqual({c["level"] for c in cs.values()}, {"hard", "info"})

    def test_writer_missing_is_a_hard_failure(self):
        res = L.evaluate(L.Warehouse(Fake(log=[], lf=[])), D, NOW)
        cs = {c["check_name"]: c for c in L.checks(res)}
        self.assertFalse(cs["sendtime_l10_writer_ok_on_latest_plan"]["ok"])
        self.assertEqual(cs["sendtime_deliverable_by_type"]["value"], "0")

    def test_record_touches_only_the_sendtime_rows_of_the_selfcheck_table(self):
        q = Fake()
        res = L.evaluate(L.Warehouse(q), D, NOW)
        q.calls.clear()
        L.record(q, res, L.checks(res))
        self.assertEqual(len(q.calls), 2)
        self.assertTrue(q.calls[0].startswith(f"DELETE FROM `{L.T_CHECK}` WHERE plan_date = DATE '{D}' AND check_name LIKE 'sendtime"))
        self.assertTrue(q.calls[1].startswith(f"INSERT INTO `{L.T_CHECK}` "))
        self.assertEqual(q.calls[1].count("'sendtime_"), 6)

    def test_sg7_akcija_excludes_by_the_send_time_answer(self):
        # week of Tue 06.10 = Mon 05.10 .. Sun 11.10. m1 (180) and m6 (235) are deliverable sales letters; m7 is a 244
        # (not a sales letter); m2 is gated; m9 (179, due 07.10) is PLANNED later this week (SG7a); m10 / m11 keep the
        # planner's own reason; m14 was PERSONAL by an older evaluation and has no deliverable letter now -> back in.
        q = Fake()
        wh = L.Warehouse(q)
        personal = L.personal_this_week(wh, D, NOW)
        self.assertEqual(personal, {"m1": ("winback_1", "2026-10-06", "deliverable_today"),
                                    "m6": (S.XSELL, "2026-10-06", "deliverable_today"),
                                    "m9": ("reorder_1", "2026-10-07", "planned_later_this_week")})
        plan = L.akcija_plan(wh, D, personal)
        self.assertEqual((plan["rows"], plan["before_in_audience"], plan["before_personal"], plan["in_audience"],
                          plan["personal"]), (9, 6, 1, 4, 3))
        self.assertEqual(plan["personal_by_rule"], {"deliverable_today": 2, "planned_later_this_week": 1})
        # SG7a: a later letter of the week excludes even when it could not pass a gate today (no writer row, template
        # not approved); a letter planned OUTSIDE the week (12.10) does not; a held (would_send false) one does not
        later = [dict(p) for p in PLAN]
        for p in later:
            if p["master_key"] == "m9":
                p["planned_send_date"] = "2026-10-11"
            if p["master_key"] == "m2":
                p["planned_send_date"] = "2026-10-12"
            if p["master_key"] == "m12":
                p["planned_send_date"] = "2026-10-08"
        with unittest.mock.patch(__name__ + ".PLAN", later):
            p2 = L.personal_this_week(L.Warehouse(Fake(approved=[], lf=[])), D, NOW)
        self.assertEqual(p2, {"m9": ("reorder_1", "2026-10-11", "planned_later_this_week")})
        q.calls.clear()
        L.akcija_record(q, D, plan)
        self.assertEqual(len(q.calls), 2)
        self.assertIn("SET in_audience = TRUE, excluded_reason = NULL", q.calls[0])
        self.assertIn("excluded_reason = 'PERSONAL_LETTER_THIS_WEEK'", q.calls[0])
        self.assertIn("STRUCT('m1' AS mk, 'winback_1' AS et, DATE '2026-10-06' AS sd)", q.calls[1])
        self.assertIn("AND t.in_audience", q.calls[1])                    # never overrides another exclusion
        self.assertNotIn("'m10'", q.calls[1])
        self.assertNotIn("'m7'", q.calls[1])

    def test_reads_only(self):
        q = Fake()
        L.evaluate(L.Warehouse(q), D, NOW)
        for sql in q.calls:
            self.assertTrue(sql.lstrip().upper().startswith(("SELECT", "WITH")), sql[:60])


if __name__ == "__main__":
    unittest.main()
