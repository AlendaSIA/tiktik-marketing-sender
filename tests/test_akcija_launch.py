import datetime as dt
import hashlib
import unittest
from unittest import mock

import akcija_launch as AL

D = dt.date(2026, 10, 13)
FRAME = "<p>{{ contact.AK_HTML }}</p><!--MAKER_BLOCKS--><p>end</p>"
SHA = hashlib.sha256(FRAME.encode()).hexdigest()


class Q:
    def __init__(self, approved=True, excl=("p@x.lv",)):
        self.approved, self.excl = approved, excl

    def __call__(self, s):
        if "template_approval" in s:
            return [{"template_id": 313, "approved_sha256": SHA}] if self.approved else []
        if "akcija_exclusion_members" in s:
            return [{"email": e} for e in self.excl]
        raise AssertionError(s)


def brevo_fn(sent):
    lists = {3: [("a@x.lv", False), ("p@x.lv", False), ("b@x.lv", True), ("e@x.lv", False)], 4: [], 46: [("e@x.lv", 0)],
             75: [], 96: []}

    def call(m, path, body):
        if path.startswith("/smtp/templates/313"):
            return {"htmlContent": FRAME, "subject": "Nedēļas akcija"}
        if path.startswith("/contacts/lists/") and "/contacts?" in path:
            lid = int(path.split("/")[3])
            off = int(path.split("offset=")[1])
            rows = lists.get(lid, []) if off == 0 else []
            return {"contacts": [{"email": e, "emailBlacklisted": b} for e, b in rows]}
        if m == "POST" and path == "/emailCampaigns":
            sent.append(("create", body))
            return {"id": 901}
        if m == "GET" and path == "/emailCampaigns/901":
            r = sent[-1][1]["recipients"]
            return {"recipients": {"lists": r["listIds"], "exclusionLists": r["exclusionListIds"]}}
        if path.endswith("/sendNow"):
            sent.append(("send", path))
            return {}
        if path == "/smtp/email":
            sent.append(("alert", body))
            return {}
        raise AssertionError((m, path))
    return call


OK_GATES = mock.patch.multiple("block_gate", ak_h1=mock.Mock(return_value=None),
                               maker_html=mock.Mock(return_value=({"html": "<b>maker</b>"}, None)))


class Launch(unittest.TestCase):
    def test_dry_builds_and_measures_never_creates(self):
        sent = []
        with OK_GATES:
            r = AL.launch(Q(), brevo_fn(sent), D, False, {"members": 1, "brevo": None}, {"akcija_236": []}, mock.Mock())
        self.assertEqual(r["holds"], [])
        self.assertFalse(r["launched"])
        self.assertEqual(r["audience"]["final"], 1)          # a only: b blacklisted, e EN, p personal (98)
        self.assertEqual(sent, [])

    def test_live_all_green_creates_and_sends_with_98(self):
        sent = []
        with OK_GATES:
            r = AL.launch(Q(), brevo_fn(sent), D, True, {"brevo": {"list_id": 98}}, {"akcija_236": []}, mock.Mock())
        self.assertTrue(r["launched"])
        body = sent[0][1]
        self.assertEqual(body["recipients"], {"listIds": [3], "exclusionListIds": [4, 46, 75, 96, 98]})
        self.assertIn("<b>maker</b>", body["htmlContent"])
        self.assertEqual(sent[1], ("send", "/emailCampaigns/901/sendNow"))

    def test_live_any_hold_no_campaign_kluda_and_alert(self):
        for d44, refill in ((["D44_AK_NOT_BUILT"], {"brevo": {"list_id": 98}}), ([], {"error": "x"}),
                            ([], {"brevo": None})):
            sent, dash = [], mock.Mock()
            with OK_GATES:
                r = AL.launch(Q(), brevo_fn(sent), D, True, refill, {"akcija_236": d44}, dash)
            self.assertFalse(r["launched"])
            self.assertEqual([s[0] for s in sent], ["alert"])
            self.assertEqual(sent[0][1]["to"], [{"email": "raivis@alenda.lv"}])
            dash.assert_called_once()

    def test_frame_sha_must_match(self):
        sent = []
        with OK_GATES:
            r = AL.launch(Q(), brevo_fn(sent), D, False, {"brevo": None},
                          {"akcija_236": []}, mock.Mock()) if False else None
        q = Q()
        q.__call__ = None
        with OK_GATES, mock.patch.object(AL.AC, "frame_template",
                                         return_value=({"template_id": 313, "approved_sha256": "0" * 64}, None)):
            b = AL.build(Q(), brevo_fn(sent), D)
        self.assertTrue(any(h.startswith("D1 236 frame 313") for h in b["holds"]))


if __name__ == "__main__":
    unittest.main()
