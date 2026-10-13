"""Pins MAIN 2026-09-25, F7, verbatim: "BUILD: a hard block in the send path — any '⟦' in subject,
preheader or HTML = REFUSED, before any customer send. Prove it with a negative test."

These are the offline negative tests. Every spelling of the bracket, in every part of the letter,
makes campaign.send_now() raise PlaceholderLeft BEFORE the approval is looked up; a clean letter
passes the content check and stops at the approval gate, exactly as before F7. Nothing here touches
the network: the content is injected through content_reader, and where the DEFAULT reader is
exercised, campaign.campaign is faked. The live half of the proof is send_guard_proof.py.

Runs with the standard library only (python -m unittest discover -s tests).
"""
import contextlib
import io
import json
import os
import re
import sys
import types
import unittest
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:  # the real library in the image, a stub on a bare interpreter
    from google.cloud import bigquery as _bq  # noqa: F401
except Exception:  # noqa: BLE001
    google = sys.modules.setdefault("google", types.ModuleType("google"))
    cloud = types.ModuleType("google.cloud")
    stub = types.ModuleType("google.cloud.bigquery")

    class _P:
        def __init__(self, name, typ, value):
            self.name, self.type_, self.value = name, typ, value

    stub.ScalarQueryParameter = _P
    stub.ArrayQueryParameter = _P
    stub.Client = object
    stub.QueryJobConfig = lambda **kw: kw
    cloud.bigquery = stub
    google.cloud = cloud
    sys.modules["google.cloud"] = cloud
    sys.modules["google.cloud.bigquery"] = stub

import campaign as C  # noqa: E402

CID = 4711
PREHEADER = "Tur jau ir preces no tava pirmā pasūtījuma."
# The house shape (templates/*.html): the preview line is the first display:none div in <body>.
CLEAN_HTML = (
    '<!DOCTYPE html><html lang="lv"><head><meta charset="utf-8"><title>Tavs kabinets ir gatavs'
    '</title></head><body style="margin:0;padding:0;">'
    f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{PREHEADER}</div>'
    '<p>Sveiki!</p><a href="https://www.tiktik.lv/veikals?utm_source=brevo&amp;'
    'utm_campaign=2026-w40-kabinets">Uz veikalu</a></body></html>')
APPROVED = {"batch_id": "B-1", "assignment_build_id": "build-1"}


def letter(subject="Tavs kabinets ir gatavs", preview="", html=CLEAN_HTML):
    return {"subject": subject, "previewText": preview, "htmlContent": html}


def in_preheader_div(text):
    return CLEAN_HTML.replace(PREHEADER, f"{PREHEADER} {text}")


def in_body(text):
    return CLEAN_HTML.replace("<p>Sveiki!</p>", f"<p>Sveiki! {text}</p>")


class _Lookup:
    """approval_lookup that remembers whether it was asked at all."""

    def __init__(self, row=None):
        self.row, self.calls = row, []

    def __call__(self, batch_id, build_id):
        self.calls.append((batch_id, build_id))
        return self.row


class _Base(unittest.TestCase):

    def _send(self, content, row=None):
        """send_now always raises; return (the SendRefused, the lookup, the ids the reader saw)."""
        lookup, seen = _Lookup(row), []

        def reader(campaign_id):
            seen.append(campaign_id)
            return content
        try:
            C.send_now(CID, "2026-09-29", "B-1", "build-1", lookup, content_reader=reader)
        except C.SendRefused as e:
            return e, lookup, seen
        self.fail("send_now returned instead of raising - nothing may pass it")

    def _refused(self, content, part):
        """The negative test: PlaceholderLeft, with a hit in `part`, and no approval asked for."""
        e, lookup, _ = self._send(content)
        self.assertIsInstance(e, C.PlaceholderLeft, str(e))
        self.assertEqual(lookup.calls, [], "the approval must not even be looked up")
        self.assertIn(part, [h["part"] for h in e.hits])
        self.assertIn(str(CID), str(e))
        return e


class PlaceholderInEveryPartIsRefused(_Base):

    def test_token_in_subject(self):
        e = self._refused(letter(subject="⟦NEDĒĻAS TĒMA⟧ nedēļa: lētāk nekā parasti"), "subject")
        self.assertEqual(e.hits, [{"part": "subject", "token": "⟦NEDĒĻAS TĒMA⟧"}])

    def test_token_in_preview_text(self):
        e = self._refused(letter(preview="Tava cena spēkā līdz ⟦LĪDZ DATUMAM⟧."), "preheader")
        self.assertEqual(e.hits, [{"part": "preheader", "token": "⟦LĪDZ DATUMAM⟧"}])

    def test_token_in_hidden_preheader_div_is_preheader_not_html(self):
        e = self._refused(letter(html=in_preheader_div("⟦LĪDZ DATUMAM⟧")), "preheader")
        self.assertEqual(e.hits, [{"part": "preheader", "token": "⟦LĪDZ DATUMAM⟧"}])

    def test_token_in_body_html(self):
        e = self._refused(letter(html=in_body("<span>⟦TAVA CENA P1⟧</span>")), "html")
        self.assertEqual(e.hits, [{"part": "html", "token": "⟦TAVA CENA P1⟧"}])

    def test_token_in_title_is_html(self):
        html = CLEAN_HTML.replace("<title>Tavs kabinets ir gatavs", "<title>⟦NEDĒĻAS TĒMA⟧ nedēļa")
        self._refused(letter(html=html), "html")

    def test_token_inside_a_liquid_block_still_counts(self):
        # A block a contact may not see is still a block other contacts do see.
        self._refused(letter(html=in_body("{% if contact.P1_NAME %}⟦TAVA CENA P1⟧{% endif %}")),
                      "html")

    def test_lone_bracket_is_reported_with_its_context(self):
        e = self._refused(letter(html=in_body("cena ⟦ nav aizpildīta")), "html")
        self.assertIn("⟦ nav aizpildīta", e.hits[0]["token"])


class EncodedBracketsAreRefused(_Base):

    ENTITY_FORMS = ("&#10214;", "&#x27E6;", "&#x27e6;", "&#0010214;", "&lobrk;",
                    "&LeftDoubleBracket;")

    def test_entity_encoded_in_body(self):
        for form in self.ENTITY_FORMS:
            with self.subTest(form=form):
                e = self._refused(letter(html=in_body(f"{form}TAVA CENA P1&#10215;")), "html")
                self.assertEqual(e.hits[0]["token"], "⟦TAVA CENA P1⟧")

    def test_entity_encoded_in_subject_and_preheader_div(self):
        self._refused(letter(subject="&#10214;NEDĒĻAS TĒMA&#10215; nedēļa"), "subject")
        self._refused(letter(html=in_preheader_div("&lobrk;LĪDZ DATUMAM&robrk;")), "preheader")

    def test_url_encoded_in_a_link(self):
        for form in ("%E2%9F%A6", "%e2%9f%a6", "%e2%9F%A6"):
            with self.subTest(form=form):
                href = f"https://www.tiktik.lv/veikals/item/{form}SKU%E2%9F%A7"
                html = in_body(f'<a href="{href}">x</a>')
                e = self._refused(letter(html=html), "html")
                self.assertEqual(e.hits[0]["token"], "⟦SKU⟧")

    def test_url_encoded_entity_and_entity_encoded_percent(self):
        for href in ("https://t.lv/?q=%26%2310214%3BSKU",
                     "https://t.lv/?q=&#37;E2&#37;9F&#37;A6SKU"):
            with self.subTest(href=href):
                self._refused(letter(html=in_body(f'<a href="{href}">x</a>')), "html")


class EveryHitIsNamed(_Base):

    def test_all_parts_listed_in_reading_order_with_the_campaign_id(self):
        content = letter(subject="⟦NEDĒĻAS TĒMA⟧ nedēļa", preview="līdz ⟦LĪDZ DATUMAM⟧",
                         html=in_body("⟦TAVA CENA P1⟧ un ⟦TAVA CENA P1⟧").replace(
                             PREHEADER, "⟦PARASTĀ CENA P1⟧"))
        e = self._refused(content, "subject")
        self.assertEqual(e.hits, [
            {"part": "subject", "token": "⟦NEDĒĻAS TĒMA⟧"},
            {"part": "preheader", "token": "⟦LĪDZ DATUMAM⟧"},
            {"part": "preheader", "token": "⟦PARASTĀ CENA P1⟧"},
            {"part": "html", "token": "⟦TAVA CENA P1⟧"},
            {"part": "html", "token": "⟦TAVA CENA P1⟧"},   # one hit per bracket, never deduplicated
        ])
        self.assertEqual(e.campaign_id, CID)
        for h in e.hits:
            self.assertIn(f"{h['part']}: {h['token']}", str(e))

    def test_placeholder_hits_is_pure_and_empty_for_a_clean_letter(self):
        self.assertEqual(C.placeholder_hits("Tavs kabinets ir gatavs", "", CLEAN_HTML), [])
        # The CLOSING bracket alone, percent signs and ordinary brackets are not placeholders.
        self.assertEqual(C.placeholder_hits("50% [x] ⟧", None, "<p>&#10215; 100%</p>"), [])
        self.assertEqual(C.placeholder_hits(None, None, None), [])


class CleanLetterReachesTheApprovalGate(_Base):

    def test_clean_letter_is_refused_by_the_gate_not_by_the_guard(self):
        e, lookup, _ = self._send(letter())
        self.assertIsInstance(e, C.SendRefused)
        self.assertNotIsInstance(e, C.PlaceholderLeft)
        self.assertEqual(lookup.calls, [("B-1", "build-1")])
        self.assertIn("Klusēšana nav piekrišana", str(e))

    def test_clean_approved_letter_still_does_not_send(self):
        e, lookup, _ = self._send(letter(), row=APPROVED)
        self.assertNotIsInstance(e, C.PlaceholderLeft)
        self.assertEqual(len(lookup.calls), 1)
        self.assertIn("SEND PATH LOCKED", str(e))

    def test_an_approval_does_not_outrank_a_placeholder(self):
        e, lookup, _ = self._send(letter(subject="⟦NEDĒĻAS TĒMA⟧"), row=APPROVED)
        self.assertIsInstance(e, C.PlaceholderLeft)
        self.assertEqual(lookup.calls, [])


class ContentIsReadForThisCampaign(_Base):

    def test_content_reader_receives_the_campaign_id(self):
        _, _, seen = self._send(letter())
        self.assertEqual(seen, [CID])

    def test_placeholder_left_is_a_send_refused(self):
        self.assertTrue(issubclass(C.PlaceholderLeft, C.SendRefused))
        try:
            raise C.PlaceholderLeft(1, [{"part": "html", "token": "⟦X⟧"}])
        except C.SendRefused as e:  # every path that stops on SendRefused stops here too
            self.assertEqual(e.hits, [{"part": "html", "token": "⟦X⟧"}])

    def test_unreadable_content_is_refused_before_the_approval(self):
        lookup = _Lookup(APPROVED)

        def broken(campaign_id):
            raise RuntimeError("Brevo said 503")
        with self.assertRaises(C.SendRefused) as e:
            C.send_now(CID, "2026-09-29", "B-1", "build-1", lookup, content_reader=broken)
        self.assertNotIsInstance(e.exception, C.PlaceholderLeft)
        self.assertIn("could not be read", str(e.exception))
        self.assertEqual(lookup.calls, [])

    def test_content_without_html_is_refused_before_the_approval(self):
        for content in ({"subject": "x", "previewText": ""}, {"htmlContent": "  "}, None, "html"):
            with self.subTest(content=content):
                e, lookup, _ = self._send(content, row=APPROVED)
                self.assertNotIsInstance(e, C.PlaceholderLeft)
                self.assertIn("no htmlContent", str(e))
                self.assertEqual(lookup.calls, [])

    def test_default_reader_is_the_campaign_as_brevo_holds_it_now(self):
        asked = []
        saved = C.campaign

        def fake_campaign(campaign_id, statistics="campaignStats"):
            asked.append(campaign_id)
            return {"subject": "Tavs kabinets ir gatavs", "previewText": "līdz ⟦LĪDZ DATUMAM⟧",
                    "htmlContent": CLEAN_HTML, "status": "draft"}
        C.campaign = fake_campaign
        try:
            lookup = _Lookup(APPROVED)
            with self.assertRaises(C.PlaceholderLeft) as e:
                C.send_now(CID, "2026-09-29", "B-1", "build-1", lookup)
        finally:
            C.campaign = saved
        self.assertEqual(asked, [CID])
        self.assertEqual(e.exception.hits, [{"part": "preheader", "token": "⟦LĪDZ DATUMAM⟧"}])
        self.assertEqual(lookup.calls, [])


class DraftsMayCarryTokens(unittest.TestCase):
    """The block is at SEND time only: the weekly akcija draft is built WITH its tokens."""

    def setUp(self):
        self.calls = []
        self._saved = (C._call, C.effective_audience)
        html = in_body('⟦NEDĒĻAS PIEDĀVĀJUMS⟧ <a href="https://www.tiktik.lv/veikals?'
                       'utm_campaign=__UTM_WEEK__-akcija&amp;utm_content=hero">x</a>')

        def fake_call(method, path, payload=None, timeout=30):
            self.calls.append((method, path, payload))
            if method == "GET" and path.startswith("/smtp/templates/"):
                return {"htmlContent": html, "subject": "⟦NEDĒĻAS TĒMA⟧ nedēļa: lētāk nekā parasti",
                        "isActive": False}
            if method == "POST" and path == "/emailCampaigns":
                return {"id": 998}
            raise AssertionError(f"unexpected Brevo call {method} {path}")
        C._call = fake_call
        C.effective_audience = lambda list_id: 1

    def tearDown(self):
        C._call, C.effective_audience = self._saved

    def test_create_draft_does_not_refuse_a_token(self):
        d = C.create_draft(name="t", list_id=62, template_id=236, week="2026-w40",
                           approved_attributes=set())
        post = [c[2] for c in self.calls if c[0] == "POST"][0]
        self.assertEqual(d["id"], 998)
        self.assertIn("⟦NEDĒĻAS PIEDĀVĀJUMS⟧", post["htmlContent"])
        self.assertIn("⟦NEDĒĻAS TĒMA⟧", post["subject"])


class ProofScriptOffline(unittest.TestCase):
    """send_guard_proof.py against a fake Brevo: its verdicts, its exit code, its GET-only guard -
    so the live run the orchestrator makes is the only thing left unproven, not the script."""

    TEMPLATES = {
        229: {"name": "welcome_1", "subject": "Tavs kabinets ir gatavs", "htmlContent": CLEAN_HTML},
        232: {"name": "winback_2", "subject": "Jauna preču partija — par tavu cenu",
              "htmlContent": in_body("⟦PARASTĀ CENA P1⟧ ⟦TAVA CENA P1⟧")},
        233: {"name": "winback_3", "subject": "Tavu cenu turam vēl šoreiz",
              "htmlContent": in_preheader_div("⟦LĪDZ DATUMAM⟧")},
        236: {"name": "akcija_weekly", "subject": "⟦NEDĒĻAS TĒMA⟧ nedēļa: lētāk nekā parasti",
              "htmlContent": in_body("⟦NEDĒĻAS PIEDĀVĀJUMS⟧")},
    }

    def setUp(self):
        import send_guard_proof as P
        self.P = P
        self.seen = []
        self._saved = C._call

        def fake_brevo(method, path, payload=None, timeout=30):
            self.seen.append((method, path))
            m = re.fullmatch(r"/smtp/templates/(\d+)", path)
            if method == "GET" and m and int(m.group(1)) in self.TEMPLATES:
                return dict(self.TEMPLATES[int(m.group(1))], isActive=False)
            raise urllib.error.HTTPError(path, 404, "Not Found", None, None)
        self.fake_brevo = fake_brevo
        C._call = fake_brevo

    def tearDown(self):
        C._call = self._saved

    def _run(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.P.main(list(argv))
        return rc, json.loads(out.getvalue())

    def test_default_run_refuses_every_template_that_carries_a_token(self):
        rc, rep = self._run()
        self.assertEqual(rc, 0, rep)
        self.assertTrue(rep["ok"])
        got = {r["id"]: r for r in rep["templates"]}
        self.assertEqual(got[229]["outcome"], self.P.PASSED_THEN_GATE)
        self.assertTrue(got[229]["approval_lookup_called"])
        for tid, part in ((232, "html"), (233, "preheader"), (236, "subject")):
            self.assertEqual(got[tid]["outcome"], self.P.REFUSED_PLACEHOLDER)
            self.assertFalse(got[tid]["approval_lookup_called"])
            self.assertGreater(got[tid]["placeholder_hits"]["count_per_part"][part], 0)
        self.assertEqual(got[232]["placeholder_hits"]["count_per_part"],
                         {"subject": 0, "preheader": 0, "html": 2})
        self.assertEqual(rep["non_get_attempts"], [])
        self.assertEqual(rep["brevo_calls"], [{"method": "GET", "path": f"/smtp/templates/{i}"}
                                              for i in (229, 232, 233, 236)])
        self.assertIs(C._call, self.fake_brevo, "the guard must be taken off again")

    def test_a_run_without_any_token_proves_nothing(self):
        rc, rep = self._run("--templates", "229")
        self.assertEqual(rc, 1)
        self.assertFalse(rep["ok"])
        self.assertIn("why_not_ok", rep)

    def test_an_unreadable_template_fails_the_run(self):
        rc, rep = self._run("--templates", "232+404")
        self.assertEqual(rc, 1)
        self.assertEqual([r["outcome"] for r in rep["templates"]],
                         [self.P.REFUSED_PLACEHOLDER, "TEMPLATE_UNREADABLE"])

    def test_the_guard_refuses_every_write_before_the_network(self):
        calls = []
        real = self.P.guard_get_only(calls)
        try:
            for method, path in (("POST", "/emailCampaigns"), ("delete", "/emailCampaigns/1"),
                                 ("PUT", "/smtp/templates/236"),
                                 ("POST", "/emailCampaigns/1/sendNow")):
                with self.assertRaises(self.P.NonGetRefused):
                    C._call(method, path, {})
        finally:
            C._call = real
        self.assertEqual(self.seen, [], "the fake Brevo must never see a write")
        self.assertEqual(len(calls), 4)

    def test_ids_parse_with_commas_plus_or_spaces(self):
        self.assertEqual(self.P.parse_ids("229,232+233 236"), [229, 232, 233, 236])


if __name__ == "__main__":
    unittest.main()
