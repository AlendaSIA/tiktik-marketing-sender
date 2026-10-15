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
        if m == "GET" and path.startswith("/emailCampaigns?"):
            return {"campaigns": [
                {"name": "SD 2026-10-13 winback_1 (x)", "status": "queued", "recipients": {"lists": [104]}},
                {"name": "SD 2026-10-13 active_xsell (y)", "status": "draft", "recipients": {"lists": [103]}},
                {"name": "SD 2026-10-12 reorder_1 (z)", "status": "sent", "recipients": {"lists": [90]}},
                {"name": "SD 2026-10-13 reorder_1 (w)", "status": "sent", "recipients": {"lists": [102]}}]}
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

    def test_only_236_also_excludes_the_lists_the_engine_sent_to_today(self):
        """D52 13.10: send_log/outcome of a big personal campaign are written ~2 h after the send - 236 must not wait."""
        sent = []
        with OK_GATES:
            r = AL.launch(Q(), brevo_fn(sent), D, True, {"brevo": {"list_id": 98}}, {"akcija_236": []}, mock.Mock(),
                          exclude_sent_today=True)
        self.assertEqual(r["sent_today_lists"], [102, 104])            # the draft (103) and yesterday's (90) are not
        self.assertEqual(sent[0][1]["recipients"], {"listIds": [3], "exclusionListIds": [4, 46, 75, 96, 98, 102, 104]})

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


class EngineClassD2(unittest.TestCase):
    """D2 (MAIN 4 15.10): the 236 campaign gets its reviewed class row between create and sendNow."""

    class QC(Q):
        def __init__(self, fail=False, events=None):
            super().__init__()
            self.fail, self.events, self.class_sql = fail, events, []

        def __call__(self, s):
            if "brevo_campaign_class" in s:
                if self.events is not None:
                    self.events.append(("class", None))
                if self.fail:
                    raise RuntimeError("BigQuery refused")
                self.class_sql.append(s)
                return []
            return super().__call__(s)

    def test_live_writes_broadcast_row_before_send(self):
        sent, events = [], []
        q = self.QC(events=events)
        b = brevo_fn(sent)

        def call(m, path, body):
            if m == "POST":
                events.append((path, None))
            return b(m, path, body)
        with OK_GATES:
            r = AL.launch(q, call, D, True, {"brevo": {"list_id": 98}}, {"akcija_236": []}, mock.Mock())
        self.assertTrue(r["launched"])
        self.assertEqual([e[0] for e in events], ["/emailCampaigns", "class", "/emailCampaigns/901/sendNow"])
        self.assertEqual(r["class_row"], "ok")
        (sql,) = q.class_sql
        self.assertIn("SELECT 901, '236 2026-10-13 ", sql)
        self.assertIn("'broadcast', 'akcija_weekly', 'akcija'", sql)
        self.assertIn("'auto: engine-created'", sql)

    def test_test_send_writes_kind_test(self):
        sent = []
        q = self.QC()
        with OK_GATES, mock.patch("flow_gate.list_ids", return_value={"TEST_236_AUTO": 777}), \
                mock.patch("flow_gate.sync_one"), mock.patch("flow_gate.list_members", return_value=AL.TEST_RECIPIENTS):
            b = brevo_fn(sent)

            def call(m, path, body):
                if m == "GET" and path == "/emailCampaigns/901":
                    return {"recipients": {"lists": [777], "exclusionLists": []}}
                return b(m, path, body)
            r = AL.test(q, call, D)
        self.assertTrue(r["sent"])
        (sql,) = q.class_sql
        self.assertIn("'TEST 236 auto 2026-10-13 ", sql)
        self.assertIn("'test', NULL, 'akcija'", sql)

    def test_failed_class_write_never_blocks_the_send(self):
        sent = []
        with OK_GATES:
            r = AL.launch(self.QC(fail=True), brevo_fn(sent), D, True, {"brevo": {"list_id": 98}},
                          {"akcija_236": []}, mock.Mock())
        self.assertTrue(r["launched"])
        self.assertEqual(sent[-1], ("send", "/emailCampaigns/901/sendNow"))
        self.assertTrue(r["class_row"].startswith("CLASS_ROW_FAILED RuntimeError"))
        r2 = AL.create_and_send(brevo_fn([]), {"subject": "S", "html": "<p/>"}, 5, [4], "n",
                                on_created=mock.Mock(side_effect=ValueError("x")))
        self.assertTrue(r2["sent"] and r2["class_row"].startswith("CLASS_ROW_FAILED ValueError"))


if __name__ == "__main__":
    unittest.main()
