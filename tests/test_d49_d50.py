import datetime as dt
import unittest
from unittest import mock

import send_day as SD

TUE = dt.date(2026, 10, 13)


class D50(unittest.TestCase):
    def test_xsell_only_within_30_days_of_the_send_day(self):
        self.assertIsNone(SD.d50_xsell_refusal({"reason": "track=post_purchase last_order=2026-09-13 x"}, TUE))
        self.assertIn("31 d", SD.d50_xsell_refusal({"reason": "last_order=2026-09-12"}, TUE))
        self.assertIn("no last order", SD.d50_xsell_refusal({"reason": "last_order=None"}, TUE))


class D49(unittest.TestCase):
    def day(self, statuses):
        d = SD.Day.__new__(SD.Day)
        now = [dt.datetime(2026, 10, 13, 6, 0, tzinfo=dt.timezone.utc)]
        d.clock = lambda: now[0]

        def sleep(s):
            now[0] += dt.timedelta(seconds=s)
        d.sleep = sleep
        it = iter(statuses)
        d.brevo = lambda m, p, b: {"status": next(it)}
        return d, now

    def test_waits_for_sent_and_10_minutes(self):
        d, now = self.day(["in_process", "in_process", "sent"])
        d._last_campaign = (5, now[0])
        d.pace()
        self.assertEqual(now[0], dt.datetime(2026, 10, 13, 6, 10, tzinfo=dt.timezone.utc))

    def test_never_finished_stops_the_day(self):
        d, now = self.day(["in_process"] * 200)
        d._last_campaign = (5, now[0])
        with self.assertRaises(SD.SP.SendLocked):
            d.pace()

    def test_first_campaign_does_not_wait(self):
        d, now = self.day([])
        t0 = now[0]
        d.pace()
        self.assertEqual(now[0], t0)


class Mails(unittest.TestCase):
    RES = {"send_date": "2026-10-13", "mode": "dry",
           "types": [{"email_type": "reorder_1", "chosen": 470, "candidates": 480, "type_refusal": "D1 TRACK_NOT_ENABLED",
                      "refused": {"block_empty": 10}},
                     {"email_type": "post_purchase_feedback", "chosen": 89, "candidates": 89,
                      "type_refusal": "D50_FEEDBACK_MOVED (x)", "refused": {}}],
           "akcija_236": {"holds": [], "audience": {"final": 2400}},
           "day_guard": {"d47_double": 0, "d47_unexplained": 0, "cab_mismatch": 0, "cab_no_link": 0}, "d44": {}}

    def test_check_mail_go_hold_per_campaign(self):
        subj, text = SD.report_mail(self.RES, "check")
        self.assertIn("IR AIZTURĒTAIS", subj)
        self.assertIn("reorder_1: GO", text)
        self.assertIn("post_purchase_feedback: HOLD", text)
        self.assertIn("236: GO", text)

    def test_internal_notice_allowed_while_l1_closed_client_mail_refused(self):
        with mock.patch.dict("os.environ", {"DRY_RUN": "true"}), \
                mock.patch("campaign._call", return_value={"ok": 1}) as call, \
                mock.patch("google.cloud.bigquery.Client"):
            q, wh, brevo, pd_post, dash = SD._prod()
            brevo("POST", "/smtp/email", {"to": [{"email": "raivis@alenda.lv"}], "subject": "x"})
            self.assertEqual(call.call_count, 1)
            with self.assertRaises(SD.SP.SendLocked):
                brevo("POST", "/smtp/email", {"to": [{"email": "client@x.lv"}], "subject": "x"})
            with self.assertRaises(SD.SP.SendLocked):
                brevo("POST", "/emailCampaigns", {"name": "x"})


if __name__ == "__main__":
    unittest.main()
