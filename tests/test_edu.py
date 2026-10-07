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


if __name__ == "__main__":
    unittest.main()
