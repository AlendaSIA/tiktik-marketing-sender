"""Daily shadow sample (CF5): the pick (send_lookups) and the letter (shadow_sample). No network, no warehouse."""
import datetime as dt
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import send_lookups as L  # noqa: E402
import sequence as S  # noqa: E402
import shadow_sample as X  # noqa: E402

import hashlib  # noqa: E402

DAY = dt.date(2026, 10, 6)
TPL = {"subject": "{{ contact.GREETING }}, tava cena līdz {{ contact.OFFER_VALID_UNTIL }}", "isActive": False,
       "modifiedAt": "2026-10-01", "htmlContent": "<html><body><p>{{ contact.VARDS | default: \"Sveiki\" }}</p>"
       "{% if contact.P1_NAME %}<b>{{ contact.P1_NAME }} {{ contact.P1_PRICE }}</b>{% endif %}"
       "{% if contact.P2_NAME %}<i>{{ contact.P2_NAME }}</i>{% endif %}"
       "<a href=\"{{ contact.KABINETS_URL }}\">Kabinets</a></body></html>"}
ROW = {"email_type": "winback_1", "letter": "RUNG", "run_id": "lf-1", "email": "a@x.lv", "n_priced": 1,
       "GREETING": "Sveika", "OFFER_VALID_UNTIL": "19.10.2026", "P1_NAME": "Cimdi", "P1_PRICE": "4,04 €", "P2_NAME": None}
BREVO = {"VARDS": "Anna", "KABINETS_URL": "https://plani.tiktik.lv/kabinets.php?t=abc", "P1_PRICE": "9,99 €",
         "P2_NAME": "Vecā prece"}
PICK = {"email_type": "winback_1", "template_id": 180, "master_key": "cid:1", "email": "a@x.lv", "skip_reason": None}
REAL_APPROVED = dict(X.APPROVED_SHA256)
X.APPROVED_SHA256[180] = hashlib.sha256(TPL["htmlContent"].encode()).hexdigest()      # the test file IS the approved one


def res(rows, camps):
    return {"send_date": "2026-10-06", "plan_run": "plan-1", "L10": [], "rows": rows, "campaigns": camps}


def row(mk, et, ok=True, rung=0):
    return {"master_key": mk, "email": mk + "@x.lv", "email_type": et, "offer_rung": rung, "deliverable": ok}


def camp(et, tid, rung=0, l8=()):
    return {"email_type": et, "rung": rung, "template_id": tid, "L8": list(l8), "L9": []}


class Pick(unittest.TestCase):
    def test_one_deliverable_per_type_skips_named_and_rotates_by_day(self):
        r = res([row("a", "winback_1", rung=1), row("b", "winback_1", rung=1), row("c", "winback_1", False, 1),
                 row("d", S.PP1), row("e", "lost_quarterly", False, 4)],
                [camp("winback_1", 180, 1), camp(S.PP1, 244), camp("lost_quarterly", 234, 4, [("L8", "pre-send gates: NO_PRICE_ROW 1")])])
        picks = {p["email_type"]: p for p in L.sample_pick(r, DAY)}
        self.assertIn(picks["winback_1"]["master_key"], ("a", "b"))               # never the gated "c"
        self.assertIsNone(picks["winback_1"]["skip_reason"])
        self.assertIn("v4", picks[S.PP1]["skip_reason"])                           # 244: MAIN's skip, although deliverable
        self.assertIsNone(picks[S.PP1]["email"])
        self.assertIn("NO_PRICE_ROW", picks["lost_quarterly"]["skip_reason"])
        self.assertEqual(picks["akcija_weekly"]["template_id"], 236)               # 236 named as skipped
        days = {L.sample_pick(r, DAY + dt.timedelta(days=i))[-1]["master_key"] for i in range(12)}
        self.assertEqual(days, {"a", "b"})                                         # rotates, deterministic per day
        self.assertEqual(L.sample_pick(r, DAY), L.sample_pick(r, DAY))

    def test_queue_is_written_once_a_day(self):
        calls, n = [], [0]

        def q(sql):
            calls.append(sql)
            return [{"n": str(n[0])}] if sql.startswith("SELECT COUNT") else []
        r = res([row("a", "winback_1", rung=1)], [camp("winback_1", 180, 1)])
        picks = L.sample_pick(r, DAY)
        self.assertTrue(L.sample_record(q, r, picks))
        self.assertTrue(calls[-1].startswith(f"INSERT INTO `{L.T_SAMPLE}`"))
        self.assertIn("'a@x.lv'", calls[-1])
        n[0], before = 3, len(calls)
        self.assertFalse(L.sample_record(q, r, picks))                             # second run of the day: nothing
        self.assertEqual(len(calls), before + 1)
        self.assertTrue(L.sample_record(q, r, picks, force=True))
        self.assertTrue(calls[-2].startswith("DELETE"))


class Letter(unittest.TestCase):
    def test_row_wins_over_brevo_and_the_letter_goes_only_to_raivis(self):
        b = X.build(DAY, PICK, ROW, TPL, BREVO)
        self.assertEqual(b["problems"], [])
        p = b["payload"]
        self.assertEqual(p["to"], [{"email": "raivis@alenda.lv"}])
        self.assertEqual(p["subject"], "PARAUGS 06.10.2026 · winback_1 · Sveika, tava cena līdz 19.10.2026")
        h = p["htmlContent"]
        self.assertIn("Cimdi 4,04 €", h)                                           # the writer's price, not Brevo's 9,99
        self.assertNotIn("9,99", h)
        self.assertNotIn("Vecā prece", h)                                          # row NULL blanks a stale Brevo value
        self.assertIn("<p>Anna</p>", h)                                            # base field from Brevo
        self.assertIn("kabinets.php?t=abc", h)                                     # the client's own cabinet link
        self.assertLess(h.index("PARAUGS 06.10.2026"), h.index("<p>Anna</p>"))     # banner on top
        self.assertIn("cid:1", h)
        self.assertNotIn("a@x.lv", h)                                              # master key, not the e-mail
        self.assertEqual(p["tags"], ["shadow-sample", "winback_1"])
        self.assertFalse({"cc", "bcc", "templateId"} & set(p))

    def test_fail_closed(self):
        self.assertIn("no letter_fields row", X.build(DAY, PICK, None, TPL, BREVO)["problems"][0])
        self.assertIn("EXCLUDED", X.build(DAY, PICK, {**ROW, "letter": "EXCLUDED"}, TPL, BREVO)["problems"][0])
        self.assertIn("not winback_1", X.build(DAY, PICK, {**ROW, "email_type": "reorder_1"}, TPL, BREVO)["problems"][0])
        b = X.build(DAY, PICK, ROW, TPL, {"VARDS": "Anna"})                        # no KABINETS_URL anywhere
        self.assertIsNone(b["payload"])
        self.assertIn("contact.KABINETS_URL", b["problems"][0])
        t = {**TPL, "htmlContent": TPL["htmlContent"].replace("</body>", "⟦CENA⟧{% for x in y %}</body>")}
        self.assertIn("Brevo holds another file than the approved one", X.build(DAY, PICK, ROW, t, BREVO)["problems"][0])
        self.assertIn("no approved content hash", X.build(DAY, {**PICK, "template_id": 999}, ROW, TPL, BREVO)["problems"][0])
        self.assertEqual(sorted(REAL_APPROVED), [179, 180, 234, 235])
        self.assertTrue(all(len(v) == 64 for v in REAL_APPROVED.values()))
        with mock.patch.dict(X.APPROVED_SHA256, {180: hashlib.sha256(t["htmlContent"].encode()).hexdigest()}):
            pr = " | ".join(X.build(DAY, PICK, ROW, t, BREVO)["problems"])
        self.assertIn("unresolved block", pr)
        self.assertIn("visible placeholder", pr)

    def test_send_guards(self):
        b = X.build(DAY, PICK, ROW, TPL, BREVO)
        with mock.patch.object(X.C, "_call", return_value={"messageId": "<m1>"}) as call:
            self.assertEqual(X.send(b["payload"]), {"messageId": "<m1>"})
            call.assert_called_once()
            self.assertEqual(call.call_args[0][:2], ("POST", "/smtp/email"))
            for bad in ({**b["payload"], "to": [{"email": "client@x.lv"}]}, {**b["payload"], "bcc": [{"email": "x@x.lv"}]},
                        {**b["payload"], "to": [{"email": "raivis@alenda.lv"}, {"email": "client@x.lv"}]}):
                with self.assertRaises(AssertionError):
                    X.send(bad)
            call.assert_called_once()

    def test_module_calls_nothing_that_writes_state(self):
        src = open(os.path.join(ROOT, "shadow_sample.py")).read()
        code = src.split('"""', 2)[2]
        for word in ("send_log", "contact_sequence", "pd_record", "pd_writeback", "INSERT", "UPDATE", "DELETE",
                     '"PUT"', '"PATCH"', "/contacts\"", "sendNow"):
            self.assertNotIn(word, code, word)
        self.assertEqual(code.count("C._call("), 1)                                # the one POST /smtp/email


if __name__ == "__main__":
    unittest.main()
