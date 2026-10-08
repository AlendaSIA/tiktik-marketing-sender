"""Educational letter through the engine (MAIN 2026-10-07 13:10): the decisions are pure and FAIL CLOSED."""
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import edu as E  # noqa: E402

SHA = E.content_sha("S", "P", "<html>{{ unsubscribe }}</html>")


def letter(**k):
    L = {"send_date": "2026-10-08", "letter_code": "G7", "source_campaign_id": 290, "approved_sha256": SHA,
         "expected_audience": 2957, "tolerance_pct": 10, "test_only": False, "armed": True}
    L.update(k)
    return L


def facts(**k):
    s = {"letter": letter(), "stops": 0, "plan_run": "p1", "plan_selfcheck_failed": 0, "writer_status": "OK",
         "sendtime_rows": 5, "hard_failed": [], "gate_rows": 7902, "sales_switch_open": {}, "tracks_enabled": 0,
         "segment_rows": 8136, "segment_age_h": 0.5, "audience": 2957, "audience_bad": None,
         "brevo": {"sha256": SHA, "status": "draft", "placeholders": "0", "unsubscribe_links": "1", "error": None}}
    s.update(k)
    return s


class Check(unittest.TestCase):
    def test_go_only_when_everything_holds(self):
        self.assertEqual(E.check_reasons(facts()), [])

    def test_every_single_failure_is_a_no_go(self):
        cases = {
            "NO_LETTER_ROW": dict(letter=None), "NOT_ARMED": dict(letter=letter(armed=False)),
            "NO_APPROVAL_HASH": dict(letter=letter(approved_sha256=None)), "STOPPED": dict(stops=1),
            "NO_PLAN_RUN_TODAY": dict(plan_run=None), "PLAN_SELFCHECK_FAILED": dict(plan_selfcheck_failed=2),
            "WRITER_NOT_OK": dict(writer_status="FAILED"), "NO_0855_SELFCHECK_ROWS": dict(sendtime_rows=0),
            "HARD_SELFCHECK_FAILED": dict(hard_failed=["sendtime_x"]), "NO_PERSON_GATE_ROWS_TODAY": dict(gate_rows=0),
            "SALES_SWITCH_OPEN": dict(sales_switch_open={"DRY_RUN": "false"}), "TRACK_ENABLED": dict(tracks_enabled=1),
            "SEGMENT_MISSING_OR_OLD": dict(segment_age_h=27.0), "AUDIENCE_EMPTY": dict(audience=0),
            "AUDIENCE_OUT_OF_TOLERANCE": dict(audience=2600), "AUDIENCE_HOLDS": dict(audience_bad={"suppressed": 1}),
            "BREVO_NOT_VERIFIED": dict(brevo=None),
            "CONTENT_HASH_MISMATCH": dict(brevo={"sha256": "x" * 64, "status": "draft", "placeholders": 0, "unsubscribe_links": 1}),
            "SOURCE_CAMPAIGN_STATUS": dict(brevo={"sha256": SHA, "status": "sent", "placeholders": 0, "unsubscribe_links": 1}),
            "PLACEHOLDERS_IN_LETTER": dict(brevo={"sha256": SHA, "status": "draft", "placeholders": 1, "unsubscribe_links": 1}),
            "NO_UNSUBSCRIBE_LINK": dict(brevo={"sha256": SHA, "status": "draft", "placeholders": 0, "unsubscribe_links": 0}),
            "BREVO_READ_ERROR": dict(brevo={"sha256": None, "status": None, "error": "HTTP 500"}),
        }
        for want, change in cases.items():
            got = E.check_reasons(facts(**change))
            self.assertTrue(got and any(r.startswith(want) for r in got), (want, got))

    def test_tolerance_and_test_audience(self):
        self.assertTrue(E.tolerance_ok(2957, 2957, 10) and E.tolerance_ok(2662, 2957, 10) and E.tolerance_ok(3252, 2957, 10))
        self.assertFalse(E.tolerance_ok(2661, 2957, 10) or E.tolerance_ok(3253, 2957, 10) or E.tolerance_ok(5, None, 10))
        t = facts(letter=letter(test_only=True, expected_audience=None), audience=1, audience_emails=["raivis@alenda.lv"])
        self.assertEqual(E.check_reasons(t), [])
        t["audience_emails"] = ["client@x.lv"]
        self.assertIn("TEST_AUDIENCE_IS_NOT_ONLY_THE_TEST_RECIPIENT", E.check_reasons(t))


def sendfacts(**k):
    s = {"letter": letter(), "send_date": "2026-10-08", "today": "2026-10-08", "stops": 0, "last_gate_kind": "GO",
         "go": {"check_run": "c1", "age_min": 30, "audience": 2957}, "audience": 2957, "started": 0}
    s.update(k)
    return s


class Rules(unittest.TestCase):
    def test_two_rules_differ_only_in_the_track_filter(self):
        glove, everyone = E.audience_sql("lv_glove_buyers"), E.audience_sql("lv_all")
        self.assertEqual(sorted(E.RULES), sorted(["lv_all", "lv_edu_all", "lv_glove_buyers", "lv_edu_pick"]
                                                  + ["lv_edu_" + g for g in E.GROUPS]))
        with self.assertRaises(KeyError):
            E.audience_sql("lv_edu_all")
        self.assertNotEqual(glove, everyone)
        self.assertEqual(glove.count(" AND info_track = 'cimdi'"), 1)
        self.assertEqual(glove.replace(" AND info_track = 'cimdi'", ""), everyone)      # nothing else differs
        self.assertNotIn("info_track", everyone)
        for sql in (glove, everyone):                                                   # the same gates in both
            for must in ("language = 'lv'", "email_suppression_all", "shadow_akcija_audience",
                         "excluded_reason = 'PERSONAL_LETTER_THIS_WEEK' THEN 'IN'", "ELSE a.excluded_reason END"):
                self.assertIn(must, sql)

    def test_unknown_rule_never_builds_an_audience(self):
        with self.assertRaises(KeyError):
            E.audience_sql("everybody")
        got = E.check_reasons(facts(rule="everybody", rule_unknown=True, audience=None))
        self.assertTrue(any(r.startswith("UNKNOWN_AUDIENCE_RULE") for r in got) and "AUDIENCE_EMPTY" in got)


class ListFill(unittest.TestCase):
    """The add call's answer is not trusted: members are read back, the missing re-added, the counter must settle."""
    def run_fill(self, drop_first=(), counter_seq=(3,)):
        state = {"members": set(), "adds": 0, "counters": list(counter_seq)}

        def brevo(method, path, payload=None):
            if method == "POST" and path == "/contacts/lists":
                return {"id": 500}
            if method == "POST" and path.endswith("/contacts/add"):
                state["adds"] += 1
                keep = [e for e in payload["emails"] if not (state["adds"] == 1 and e in drop_first)]
                state["members"] |= set(keep)
                return {"contacts": {"success": payload["emails"], "failure": []}}     # Brevo says yes to all
            if method == "GET" and path.startswith("/contacts/lists/500/contacts"):
                return {"contacts": [{"email": e} for e in sorted(state["members"])]}
            if method == "GET" and path == "/contacts/lists/500":
                c = state["counters"].pop(0) if len(state["counters"]) > 1 else state["counters"][0]
                return {"uniqueSubscribers": c, "totalBlacklisted": 0}
            raise AssertionError(path)
        import unittest.mock as m
        with m.patch.object(E, "_brevo", brevo), m.patch.object(E, "log", lambda *a, **k: None), \
                m.patch.object(E.time, "sleep", lambda s: None):
            return E.fill_list(None, "2026-10-08", "G7", "t", "L", ["a@x.lv", "B@x.lv ", "c@x.lv"], settle_s=0), state

    def test_all_in_first_round(self):
        f, st = self.run_fill()
        self.assertEqual((f["added"], f["not_added"], f["calls"], f["counter_settled"]), (3, 0, 1, True))
        self.assertEqual(f["ok"], ["a@x.lv", "b@x.lv", "c@x.lv"])

    def test_brevo_says_yes_but_one_is_missing_then_readded(self):
        f, st = self.run_fill(drop_first=("b@x.lv",))
        self.assertEqual((f["brevo_said_added"], f["added"], f["calls"]), (4, 3, 2))
        self.assertEqual(f["rounds"][0]["missing"], 1)

    def test_counter_behind_is_not_settled(self):
        f, st = self.run_fill(counter_seq=(2,))
        self.assertFalse(f["counter_settled"])


class Send(unittest.TestCase):
    def test_sends_only_with_a_fresh_go_and_no_stop(self):
        self.assertEqual(E.send_refusals(sendfacts()), [])

    def test_no_go_sends_nothing(self):
        self.assertIn("NO_GO_RECORD", E.send_refusals(sendfacts(go=None, last_gate_kind=None, audience=None)))
        self.assertIn("NO_GO_RECORD", E.send_refusals(sendfacts(go=None, last_gate_kind="NO-GO", audience=None)))

    def test_stop_blocks_even_with_go(self):
        self.assertEqual(E.send_refusals(sendfacts(stops=1)), ["STOPPED"])

    def test_every_other_refusal(self):
        cases = {"NOT_THE_SEND_DATE": dict(today="2026-10-09"), "NOT_ARMED": dict(letter=letter(armed=False)),
                 "NO_APPROVAL_HASH": dict(letter=letter(approved_sha256="")), "NO_LETTER_ROW": dict(letter=None),
                 "GO_TOO_OLD": dict(go={"check_run": "c1", "age_min": 61, "audience": 2957}),
                 "FROZEN_AUDIENCE_DIFFERS": dict(audience=2956), "ALREADY_STARTED": dict(started=1),
                 "TEST_AUDIENCE_IS_NOT_ONLY": dict(letter=letter(test_only=True), audience_emails=["a@x.lv"])}
        for want, change in cases.items():
            got = E.send_refusals(sendfacts(**change))
            self.assertTrue(any(r.startswith(want) for r in got), (want, got))

    def test_content_must_be_the_approved_bytes(self):
        camp = {"subject": "S", "previewText": "P", "htmlContent": "<html>{{ unsubscribe }}</html>"}
        self.assertEqual(E.content_refusals(letter(), camp, 0, 1), [])
        self.assertTrue(E.content_refusals(letter(), {**camp, "htmlContent": camp["htmlContent"] + " "}, 0, 1))
        self.assertTrue(E.content_refusals(letter(), {**camp, "subject": "S!"}, 0, 1))
        self.assertIn("NO_UNSUBSCRIBE_LINK", E.content_refusals(letter(), camp, 0, 0))
        self.assertTrue(E.content_refusals(letter(), camp, 1, 1))

    def test_campaign_copy_and_lists(self):
        src = {"subject": "S", "previewText": "P", "htmlContent": "H", "sender": {"id": 2}, "replyTo": "info@tiktik.lv",
               "name": "NESŪTĪT", "recipients": {"lists": [99]}}
        p = E.campaign_payload(src, "ENGINE", 500, E.exclusion_lists(False))
        self.assertEqual((p["subject"], p["previewText"], p["htmlContent"]), ("S", "P", "H"))
        self.assertEqual(p["recipients"], {"listIds": [500], "exclusionListIds": [4, 46, 75]})
        self.assertEqual((p["sender"], p["replyTo"], p["mirrorActive"]), ({"id": 2}, "info@tiktik.lv", False))
        self.assertTrue(E.campaign_payload({**src, "mirrorActive": True}, "E", 1, [])["mirrorActive"])
        self.assertEqual(E.exclusion_lists(True), [46, 75])                 # the test address sits in list 4
        self.assertEqual(E.TEST_RECIPIENT, "raivis@alenda.lv")
        self.assertEqual(len(E.chunks(list(range(301)), 150)), 3)

    def test_this_path_writes_no_sales_table_and_has_one_send_call(self):
        code = open(os.path.join(ROOT, "edu.py"), encoding="utf-8").read().split('"""', 2)[2]
        self.assertEqual(code.count("/sendNow"), 1)
        self.assertEqual(code.count('"/emailCampaigns", campaign_payload('), 1)       # one place creates a campaign
        reh = code[code.index("def rehearse("):code.index("def verify(")]
        self.assertNotIn("emailCampaigns", reh)                                        # the rehearsal: no campaign, no send
        self.assertNotIn("sendNow", reh)
        self.assertEqual(code.count("f = fill_list(q, d, code, who,"), 2)                # rehearsal and send: one filler
        for word in ("send_log", "contact_sequence", "shadow_send_plan", "letter_fields`", "/smtp/email", "pd_record",
                     "track_enabled` SET", "UPDATE "):
            self.assertNotIn(word, code, word)
        writes = [ln for ln in code.splitlines() if "INSERT INTO" in ln or "CREATE OR REPLACE" in ln]
        self.assertTrue(writes and all("{T_" in ln for ln in writes), writes)



class EduAllRule(unittest.TestCase):
    """lv_edu_all (Raivis 2026-10-07): every old contact of the hand list, never a B2B lead."""
    BASE = {"l3": True, "l4": False, "l46": False, "l75": False, "lead": False, "suppressed": False, "blocked": False,
            "language": None, "in_engine": False, "excluded_reason": None, "master_key": None, "in_lv_all": False,
            "b2b_email": False}

    def row(self, email, **kw):
        return dict(self.BASE, email=email, **kw)

    def test_nobody_who_must_stay_out_can_enter(self):
        for kw, gate in [({"lead": True}, "B2B_LEAD"), ({"suppressed": True}, "SUPPRESSED"),
                         ({"excluded_reason": "SUPPRESSED", "in_engine": True}, "SUPPRESSED"),
                         ({"l4": True}, "LIST_4_SUPPRESSION"), ({"l46": True}, "EN_LIST"), ({"l75": True}, "EE_LIST"),
                         ({"language": "en"}, "NOT_LV"), ({"excluded_reason": "EN_PENDING"}, "NOT_LV"),
                         ({"blocked": True}, "BREVO_BLOCKLISTED"), ({"excluded_reason": "BLOCKED_OR_UNKNOWN"}, "BLOCKED_OR_UNKNOWN"),
                         ({"excluded_reason": "A_REASON_NOBODY_HAS_SEEN"}, "A_REASON_NOBODY_HAS_SEEN"),
                         ({"excluded_reason": "B2B_FLOW", "in_engine": True}, "B2B_FLOW"),               # change 1
                         ({"excluded_reason": "NOT_TIKTIK_BUYER", "in_engine": True}, "NOT_TIKTIK_BUYER"),
                         ({"excluded_reason": "LEAD_FLOW", "in_engine": True}, "LEAD_FLOW")]:
            for extra in ({}, {"in_lv_all": True, "in_engine": True, "master_key": "m1"}):
                self.assertEqual(E.edu_all_gate(self.row("x@y.lv", **dict(extra, **kw))), gate, kw)
                self.assertEqual([g for _, _, g in E.edu_all_gates([self.row("x@y.lv", **dict(extra, **kw))])], [gate])
        # BigQuery answers booleans as the strings "true" / "false"
        self.assertEqual(E.edu_all_gate(self.row("x@y.lv", lead="true")), "B2B_LEAD")
        self.assertEqual(E.edu_all_gate(self.row("x@y.lv", lead="false", l4="false", l46="false", l75="false",
                                                 suppressed="false", blocked="false")), "IN")

    def test_who_enters(self):
        self.assertEqual(E.EDU_ALL_PASS_REASONS, (None, "", "PERSONAL_LETTER_THIS_WEEK"))
        for kw in ({}, {"language": "lv"}, {"excluded_reason": "PERSONAL_LETTER_THIS_WEEK", "in_engine": True},
                   {"l3": False, "in_lv_all": True}, {"in_engine": True, "b2b_email": True}):
            self.assertEqual(E.edu_all_gate(self.row("x@y.lv", **kw)), "IN", kw)

    def test_every_lv_all_address_is_inside(self):
        rows = [self.row(f"p{i}@y.lv", in_lv_all=True, in_engine=True, master_key=f"m{i}", language="lv",
                         l3=bool(i % 2)) for i in range(50)]
        rows += [self.row(f"second{i}@y.lv", master_key=f"m{i}") for i in range(50)]      # other addresses of the same people
        rows += [self.row(f"old{i}@y.lv") for i in range(20)]
        got = {e: g for e, _, g in E.edu_all_gates(rows)}
        self.assertTrue(all(got[f"p{i}@y.lv"] == "IN" for i in range(50)))
        self.assertTrue(all(got[f"second{i}@y.lv"] == "SECOND_ADDRESS_OF_PERSON" for i in range(50)))
        self.assertTrue(all(got[f"old{i}@y.lv"] == "IN" for i in range(20)))

    def test_no_duplicates_and_one_address_per_person(self):
        rows = [self.row("B@y.lv", master_key="m"), self.row("a@y.lv", master_key="m"), self.row("a@y.lv", master_key="m"),
                self.row(" b@y.lv ", master_key="m"), self.row("c@y.lv", master_key="m", lead=True),
                self.row("n1@y.lv"), self.row("n2@y.lv"), self.row("", master_key="m")]
        got = E.edu_all_gates(rows)
        self.assertEqual(len(got), len({e for e, _, _ in got}))
        ins = [e for e, _, g in got if g == "IN"]
        self.assertEqual(sorted(ins), ["a@y.lv", "n1@y.lv", "n2@y.lv"])
        self.assertEqual(dict((e, g) for e, _, g in got)["c@y.lv"], "B2B_LEAD")
        # the engine's own address wins over an alphabetically earlier one
        got = dict((e, g) for e, _, g in E.edu_all_gates([self.row("a@y.lv", master_key="m"),
                                                          self.row("z@y.lv", master_key="m", in_engine=True, in_lv_all=True)]))
        self.assertEqual(got, {"a@y.lv": "SECOND_ADDRESS_OF_PERSON", "z@y.lv": "IN"})

    def test_unknown_address_of_a_b2b_customer_stays_out(self):                          # change 3
        self.assertEqual(E.edu_all_gate(self.row("x@y.lv", b2b_email=True)), "B2B_FLOW_BY_EMAIL")
        self.assertEqual(E.edu_all_gate(self.row("x@y.lv", b2b_email="true", in_engine="false")), "B2B_FLOW_BY_EMAIL")
        # a person the engine holds is judged by the engine's own reason, not by the e-mail table
        self.assertEqual(E.edu_all_gate(self.row("x@y.lv", b2b_email=True, in_engine=True)), "IN")
        self.assertEqual(E.edu_all_gate(self.row("x@y.lv", b2b_email=True, in_engine=True, excluded_reason="B2B_FLOW")), "B2B_FLOW")
        sql = E.edu_all_sql()
        for must in ("b2b_shop_flow_classification_v2", E.F309_KEY, "b2b_email"):
            self.assertIn(must, sql)

    def test_self_sign_up_wins_over_a_cold_source(self):                                 # change 2
        self.assertIn("list_id IN (55, 56, 57, 58)", E.LEADS_SQL)
        self.assertIn("b.email IS NULL AND su.email IS NULL AS is_lead", E.LEADS_SQL)
        self.assertNotRegex(E.LEADS_SQL, r"\)\s*\n\w+ AS \(")                      # every CTE is followed by a comma
        self.assertTrue(set(E.SIGNUP_LISTS) <= set(E.FRESH_LISTS) <= set(E.INPUT_LISTS))

    def test_stale_or_missing_input_is_a_no_go(self):
        self.assertEqual(E.stale_inputs([{"name": "a", "day": "2026-10-08"}, {"name": "b", "day": "2026-10-07"},
                                         {"name": "c", "day": None}], "2026-10-08"), ["b", "c"])
        base = {"rule": "lv_edu_all", "letter": {"approved_sha256": "x", "armed": True}}
        self.assertTrue(any(r.startswith("EDU_ALL_INPUT_NOT_OF_TODAY") for r in E.check_reasons(dict(base))))            # not checked
        self.assertTrue(any(r.startswith("EDU_ALL_INPUT_NOT_OF_TODAY=b2b_lead_email")
                            for r in E.check_reasons(dict(base, inputs_stale=["b2b_lead_email"]))))
        self.assertFalse(any(r.startswith("EDU_ALL_INPUT") for r in E.check_reasons(dict(base, inputs_stale=[]))))
        self.assertFalse(any(r.startswith("EDU_ALL_INPUT") for r in E.check_reasons(dict(base, rule="lv_all"))))

    def test_the_texts(self):
        sql = E.edu_all_sql()
        for must in ("list_id IN (3, 4, 46, 75)", "b2b_lead_email", "email_suppression_all", "brevo_contacts_snapshot",
                     "customer_identity"):
            self.assertIn(must, sql)
        self.assertNotIn(" LIKE CONCAT", E.LEADS_SQL)                       # the Pipedrive match is an exact split
        self.assertIn("'shop_paid'", E.LEADS_SQL)
        self.assertIn("'old_account'", E.LEADS_SQL)



class EduGroupsAndPick(unittest.TestCase):
    TRACKS = ["cimdi", "dezinfekcija", "teipi", "papirs", "medicina", "tirisana", "apgerbs", "cits", "inventars", None, "",
              "CIMDI ", "something new"]

    def gates(self):
        g = [(f"p{i}@y.lv", None, "IN") for i in range(len(self.TRACKS) * 5)]
        g += [("lead@y.lv", None, "B2B_LEAD"), ("b2b@y.lv", "m", "B2B_FLOW"), ("sup@y.lv", None, "SUPPRESSED")]
        tracks = {f"p{i}@y.lv": self.TRACKS[i % len(self.TRACKS)] for i in range(len(self.TRACKS) * 5)}
        tracks.update({"lead@y.lv": "cimdi", "b2b@y.lv": "papirs", "sup@y.lv": "teipi"})
        return g, tracks

    def test_seven_groups_exclusive_and_complete(self):
        self.assertEqual(len(E.GROUPS), 7)
        self.assertEqual(sorted(E.GROUP_RULES.values()), sorted(E.GROUPS))
        g, tracks = self.gates()
        base = {e for e, _, x in g if x == "IN"}
        per = {r: {e for e, _, x in E.narrow(g, tracks, r) if x == "IN"} for r in E.GROUP_RULES}
        self.assertEqual(set().union(*per.values()), base)                                  # complete
        self.assertEqual(sum(len(v) for v in per.values()), len(base))                      # exclusive
        for r, v in per.items():                                                            # nobody gated out comes in
            self.assertFalse(v & {"lead@y.lv", "b2b@y.lv", "sup@y.lv"})
            self.assertEqual({x for _, _, x in E.narrow(g, tracks, r)} - {"IN", "OTHER_GROUP"},
                             {"B2B_LEAD", "B2B_FLOW", "SUPPRESSED"})
        for tr in ("cits", "inventars", None, "", "something new"):                         # no track = the gloves line
            self.assertEqual(E.group_of(tr), "cimdi")
        self.assertEqual(E.group_of("CIMDI "), "cimdi")
        self.assertEqual(E.group_of("papirs"), "papirs")
        self.assertEqual(E.narrow(g, tracks, "lv_edu_all"), g)

    def test_pick_rule_audience(self):
        g, tracks = self.gates()
        self.assertEqual({x for _, _, x in E.narrow(g, tracks, "lv_edu_pick", None) if x.startswith("NO_")},
                         {"NO_SELECTION_FOR_THE_DATE"})
        got = E.narrow(g, tracks, "lv_edu_pick", {"p1@y.lv", "lead@y.lv"})
        self.assertEqual([e for e, _, x in got if x == "IN"], ["p1@y.lv"])                  # a picked lead stays out

    CAT = [{"letter_code": "G7", "edu_group": "cimdi", "piece": "G7", "sendable": True},
           {"letter_code": "G1", "edu_group": "cimdi", "piece": "G1", "sendable": False},
           {"letter_code": "D4", "edu_group": "dezinfekcija", "piece": "D4", "sendable": "true"},
           {"letter_code": "K9", "edu_group": "visiem", "piece": "K9", "sendable": True}]

    def test_pick_letter(self):
        P = E.pick_letter
        self.assertEqual(P("cimdi", 0, set(), self.CAT), ("G7", "own", "PICKED"))
        self.assertEqual(P("cimdi", 0, {"G7"}, self.CAT), (None, "own", "NOTHING_UNSEEN_OWN"))     # never a seen piece
        self.assertEqual(P("cimdi", 0, {"G7"}, self.CAT, fallback=True), ("D4", "own", "PICKED_FALLBACK"))
        self.assertEqual(P("cimdi", 0, set(), self.CAT[1:2]), (None, "own", "NOTHING_UNSEEN_OWN"))  # not sendable
        self.assertEqual(P("teipi", 0, set(), self.CAT), (None, "own", "NOTHING_UNSEEN_OWN"))      # group has no letter
        self.assertEqual(P("cimdi", 2, set(), self.CAT), ("D4", "other", "PICKED"))                # the mix: 3rd week other
        self.assertEqual(P("cimdi", 2, {"D4"}, self.CAT), ("K9", "other", "PICKED"))
        self.assertEqual(P("cimdi", 2, {"D4", "K9"}, self.CAT), (None, "other", "NOTHING_UNSEEN_OTHER"))
        self.assertEqual([P("cimdi", i, set(), self.CAT)[1] for i in range(8)],
                         ["own", "own", "other", "other", "other", "own", "other", "own"])
        self.assertEqual(P("cimdi", 0, set(), self.CAT, mix=("other",)), ("D4", "other", "PICKED"))  # the mix is a parameter
        for grp in E.GROUPS:                                                 # at most one letter per person
            for slot in range(9):
                self.assertIn(P(grp, slot, set(), self.CAT)[0], (None, "G7", "D4", "K9"))

    def test_alert(self):
        cat = [dict(c, title="T-" + c["piece"]) for c in self.CAT]
        self.assertEqual(E.render_alert("2026-10-08", [{"edu_group": "cimdi", "letter_code": "G7", "seen": ""}], cat), "")
        a = E.render_alert("2026-10-08", [{"edu_group": "teipi", "letter_code": "", "seen": "G1,K9"},
                                          {"edu_group": "teipi", "letter_code": None, "seen": "G1"},
                                          {"edu_group": "papirs", "letter_code": "", "seen": ""},
                                          {"edu_group": "cimdi", "letter_code": "G7", "seen": ""}], cat)
        self.assertIn("nebūs ko sūtīt 3 klientiem", a)
        self.assertIn("Grupa teipi: 2 klienti", a)
        self.assertIn("T-G1 (2); T-K9 (1)", a)
        self.assertIn("Grupa papirs: 1 klienti", a)
        self.assertNotIn("Grupa cimdi", a)

    def test_one_letter_per_person_and_date(self):
        base = {"rule": "lv_edu_cimdi", "letter": {"approved_sha256": "x", "armed": True}, "inputs_stale": []}
        self.assertTrue(any(r.startswith("OVERLAPS_ANOTHER_LETTER_OF_THE_DATE=3") for r in E.check_reasons(dict(base, overlap=3))))
        self.assertFalse(any(r.startswith("OVERLAPS") for r in E.check_reasons(dict(base, overlap=0))))
        self.assertTrue(any(r.startswith("EDU_ALL_INPUT_NOT_OF_TODAY") for r in E.check_reasons(dict(base, inputs_stale=None))))
        s = {"letter": {"armed": True, "approved_sha256": "x"}, "send_date": "2026-10-08", "today": "2026-10-08",
             "go": {"age_min": 1, "audience": 5}, "last_gate_kind": "GO", "audience": 5}
        self.assertEqual(E.send_refusals(dict(s)), [])
        self.assertEqual(E.send_refusals(dict(s, already_got=2)), ["RECIPIENTS_ALREADY_GOT_A_LETTER_TODAY=2"])
        self.assertIn("letter_code != @c", E.SEEN_SQL + open(E.__file__, encoding="utf-8").read())



class HistoryAndColdRegister(unittest.TestCase):
    def test_pick_refuses_an_old_history(self):
        H = E.history_refusal
        self.assertEqual(H(None), "HISTORY_NEVER_REFRESHED")
        self.assertEqual(H({"age_min": "3", "missing": "0", "newest_sent_id": "292"}), "")
        self.assertTrue(H({"age_min": "3", "missing": "2", "newest_sent_id": "292"}).startswith("HISTORY_OLDER_THAN_BREVO"))
        self.assertTrue(H({"age_min": "31", "missing": "0"}).startswith("HISTORY_NOT_FRESH"))
        self.assertTrue(H({"age_min": None, "missing": "0"}).startswith("HISTORY_NOT_FRESH"))
        self.assertTrue(H({"age_min": "-5", "missing": "0"}).startswith("HISTORY_NOT_FRESH"))

    def test_export_parsing(self):
        csv = 'Email_ID;Send_Date;Open\n"A@b.lv";01-10-2026;x\nc@d.com;;\nnot an address;;\na@b.lv;dup;\n'
        self.assertEqual(E.emails_of_export(csv), ["a@b.lv", "c@d.com"])
        self.assertEqual(E.emails_of_export("EMAIL,DATE\nx@y.lv,2026\n"), ["x@y.lv"])
        self.assertEqual(E.emails_of_export(""), [])

    def test_these_modes_only_read_the_outside(self):
        code = open(os.path.join(ROOT, "edu.py"), encoding="utf-8").read()
        cr = code[code.index("def coldreg("):code.index("def edu_all_stale(")]
        self.assertNotIn("POST", cr)
        self.assertNotIn("PUT", cr)
        self.assertNotIn("DELETE\"", cr)
        self.assertNotIn("method=", cr)                                   # Pipedrive: plain GET requests only
        hi = code[code.index("def history("):code.index("def rehearse(")]
        self.assertEqual(hi.count('_brevo("POST"'), 1)                    # the recipients export, nothing else
        self.assertIn("exportRecipients", hi)
        rk = code[code.index("def rawkeys("):code.index("def history(")]
        self.assertNotIn('"POST"', rk)
        self.assertIn("pipedrive_label4_live", code)                      # the live register must be of the day too



class ProductSlots(unittest.TestCase):
    def vals(self, **over):
        v = {}
        for i, sl in enumerate(E.PARAM_SLOTS):
            v.update({sl + "_NAME": "Prece " + sl, sl + "_URL": f"https://www.tiktik.lv/veikals/item/p{i}/",
                      sl + "_IMG": f"https://img.example/p{i}.jpg", sl + "_STD": "9,99 €", sl + "_PRICE": "7.99",
                      sl + "_SKU": f" sku-{i} "})
        v.update(over)
        return v

    def rows(self, v, day="2026-10-08"):
        return [{"param_key": k, "param_value": x, "set_day": day} for k, x in v.items()]

    def shop(self):
        return {f"SKU-{i}": {"visible": True, "mozello_price": "9.99", "mozello_sale_price": "7.99", "mozello_stock": 5}
                for i in range(8)}

    def ok(self, v):
        return {v[f"{sl}_{f}"]: True for sl in E.PARAM_SLOTS for f in ("URL", "IMG")}

    def test_the_forty_keys(self):
        self.assertEqual(len(E.PARAM_KEYS), 40)
        self.assertEqual(len(E.FORM_KEYS), 48)
        self.assertEqual(E.SKU_KEYS, tuple(sl + "_SKU" for sl in E.PARAM_SLOTS))
        self.assertFalse(set(E.SKU_KEYS) & set(E.PARAM_KEYS))
        self.assertEqual(E.PARAM_KEYS[:5], ("G1_NAME", "G1_URL", "G1_IMG", "G1_STD", "G1_PRICE"))
        self.assertEqual(E.PARAM_KEYS[-1], "T4_PRICE")

    def test_complete_for_the_date(self):
        v = self.vals()
        self.assertEqual(E.param_problems(self.rows(v), "2026-10-08"), [])
        self.assertTrue(E.param_problems([], "2026-10-08")[0].startswith("MISSING 48 of 48"))
        r = [x for x in self.rows(v) if x["param_key"] != "T3_PRICE"]
        self.assertEqual(E.param_problems(r, "2026-10-08"), ["MISSING 1 of 48: T3_PRICE"])
        r = [x for x in self.rows(v) if x["param_key"] != "G2_SKU"]
        self.assertEqual(E.param_problems(r, "2026-10-08"), ["MISSING 1 of 48: G2_SKU"])
        forty = [x for x in self.rows(v) if not x["param_key"].endswith("_SKU")]           # yesterday's 40-key form
        self.assertTrue(E.param_problems(forty, "2026-10-08")[0].startswith("MISSING 8 of 48"))
        self.assertIn("EMPTY T4_SKU", E.param_problems(self.rows(self.vals(T4_SKU="")), "2026-10-08"))
        self.assertIn("EMPTY G2_NAME", E.param_problems(self.rows(self.vals(G2_NAME="  ")), "2026-10-08"))
        self.assertEqual(len(E.param_problems(self.rows(v, "2026-10-07"), "2026-10-08")), 48)       # yesterday's values
        self.assertIn("DUPLICATE G1_URL", E.param_problems(self.rows(v) + self.rows(v)[1:2], "2026-10-08"))
        self.assertIn("UNKNOWN_KEY G9_NAME", E.param_problems(self.rows(dict(v, G9_NAME="x")), "2026-10-08"))

    def test_values_hash(self):
        v = self.vals()
        h = E.params_sha(v)
        self.assertEqual(len(h), 64)
        self.assertEqual(h, E.params_sha(dict(reversed(list(v.items())))))               # order of rows does not matter
        self.assertNotEqual(h, E.params_sha(dict(v, T2_PRICE="8.99")))                    # one price does
        self.assertNotEqual(h, E.params_sha(dict(v, T2_SKU="other")))                     # one SKU does too
        self.assertEqual(E.send_refusals_params(h, v), [])
        self.assertTrue(E.send_refusals_params(h, dict(v, G1_PRICE="1.00"))[0].startswith("PARAM_VALUES_NOT_THOSE_OF_THE_GO"))
        self.assertTrue(E.send_refusals_params(None, v)[0].startswith("PARAM_VALUES_NOT_THOSE_OF_THE_GO"))
        self.assertEqual(E.send_refusals_params(h, None), ["PARAMS_NOT_COMPLETE_AT_SEND"])

    def test_shop_gate(self):
        v = self.vals()
        self.assertEqual(E.slot_problems(v, self.shop(), self.ok(v)), [])
        sh = self.shop(); sh["SKU-0"]["mozello_stock"] = 0                                  # Mozello stock 0: never shown (Raivis 08.10 13:58)
        self.assertEqual(E.slot_problems(v, sh, self.ok(v)), ["G1 MOZELLO_STOCK_0 sku= sku-0  stock=0"])
        sh = self.shop(); sh["SKU-0"]["mozello_stock"] = "0.000"
        self.assertTrue(E.slot_problems(v, sh, self.ok(v))[0].startswith("G1 MOZELLO_STOCK_0"))
        sh = self.shop(); sh["SKU-0"]["mozello_stock"] = -1                                 # oversold = nothing to sell
        self.assertTrue(E.slot_problems(v, sh, self.ok(v))[0].startswith("G1 MOZELLO_STOCK_0"))
        sh = self.shop(); sh["SKU-0"]["mozello_stock"] = None                               # untracked stock passes
        self.assertEqual(E.slot_problems(v, sh, self.ok(v)), [])
        sh = self.shop(); sh["SKU-0"]["mozello_stock"] = 1                                  # low stock is never a reason
        self.assertEqual(E.slot_problems(v, sh, self.ok(v)), [])
        self.assertNotIn("OUT_OF_STOCK", open(os.path.join(ROOT, "edu.py"), encoding="utf-8").read())  # no other stock rule
        sh = self.shop(); sh["SKU-1"]["mozello_sale_price"] = "8.49"
        self.assertTrue(E.slot_problems(v, sh, self.ok(v))[0].startswith("G2 PRICE_DIFFERS"))
        sh = self.shop(); sh["SKU-1"]["mozello_sale_price"] = None                        # no sale price: PRICE = variant price
        self.assertTrue(E.slot_problems(v, sh, self.ok(v))[0].startswith("G2 PRICE_DIFFERS"))
        self.assertEqual(E.slot_problems(self.vals(G2_PRICE="9.99"), sh, self.ok(v)), [])
        sh = self.shop(); sh["SKU-7"]["visible"] = False
        self.assertEqual(E.slot_problems(v, sh, self.ok(v)), ["T4 NOT_VISIBLE sku= sku-7 "])
        sh = self.shop(); del sh["SKU-7"]
        self.assertEqual(E.slot_problems(v, sh, self.ok(v)), ["T4 SKU_NOT_IN_MOZELLO sku= sku-7 "])
        self.assertEqual(E.slot_problems(self.vals(G1_PRICE="7,99 €", G1_STD="9.99 EUR"), self.shop(), self.ok(v)), [])  # numbers, not form
        sh = self.shop(); sh["SKU-0"].update(mozello_price=9, mozello_sale_price=None)
        self.assertEqual(E.slot_problems(self.vals(G1_PRICE="9", G1_STD="9,00"), sh, self.ok(v)), [])
        self.assertEqual(E.sku_key("\tab-1\u00a0"), "AB-1")
        ok = self.ok(v); ok[v["T1_IMG"]] = False; del ok[v["G3_URL"]]
        self.assertEqual(E.slot_problems(v, self.shop(), ok), ["G3 URL_DOES_NOT_ANSWER", "T1 IMAGE_DOES_NOT_ANSWER"])
        self.assertTrue(E.slot_problems(self.vals(G4_STD="n/a"), self.shop(), self.ok(v))[0].startswith("G4 STD_DIFFERS"))
        self.assertEqual(E.money("7,99 €"), 7.99)
        self.assertEqual(E.money("19.90"), 19.9)
        self.assertIsNone(E.money("1.2.3"))
        self.assertIsNone(E.money(""))
        self.assertEqual(E.norm_url("HTTP://tiktik.lv/a/b/?utm=1#x"), "https://www.tiktik.lv/a/b")

    def test_copy_carries_params_and_check_refuses(self):
        src = {"subject": "s", "htmlContent": "<p>{{ params.G1_NAME }}</p>", "sender": {"id": 2}}
        v = self.vals()
        p = E.campaign_payload(src, "E", 9, [4], v)
        self.assertEqual(list(p["params"]), list(E.PARAM_KEYS))
        self.assertEqual(p["params"]["T4_PRICE"], "7.99")
        self.assertFalse([k for k in p["params"] if k.endswith("_SKU")])                   # _SKU never goes to Brevo
        self.assertNotIn("params", E.campaign_payload(src, "E", 9, [4]))
        self.assertNotIn("params", E.campaign_payload(src, "E", 9, [4], None))
        base = {"rule": "lv_all", "letter": {"approved_sha256": "x", "armed": True}}
        self.assertTrue(any(r.startswith("PARAMS_NOT_READY=G1 OUT_OF_STOCK") for r in
                            E.check_reasons(dict(base, param_problems=["G1 OUT_OF_STOCK"]))))
        self.assertFalse(any(r.startswith("PARAMS_NOT_READY") for r in E.check_reasons(dict(base, param_problems=[]))))
        code = open(os.path.join(ROOT, "edu.py"), encoding="utf-8").read()
        self.assertNotIn("INSERT INTO `{T_PARAM}`", code)                                # this code only READS the table
        self.assertNotIn("DELETE FROM `{T_PARAM}`", code)


if __name__ == "__main__":
    unittest.main()
