"""R9 (Dzinējs 9): after-send check - 2+ letters = 0, nobody empty, from Brevo's own recipients; WAIT rules."""
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import post_send_check as PSC  # noqa: E402

D = dt.date(2026, 10, 13)
NOW = dt.datetime(2026, 10, 13, 9, 30, tzinfo=dt.timezone.utc)      # 12:30 Riga


def camp(i, sent, name=None, status="sent", lists=(102,)):
    return {"id": i, "name": name or f"SD 2026-10-13 x {i}", "status": status, "sentDate": sent,
            "recipients": {"lists": list(lists), "exclusionLists": [4]}}


class Brevo:
    def __init__(self, camps, rcpt, l3, excl=None):
        self.camps, self.rcpt, self.l3, self.excl, self.calls = camps, rcpt, l3, excl or {}, []

    def __call__(self, method, path, payload):
        self.calls.append((method, path))
        if path.startswith("/emailCampaigns?"):
            st = path.split("status=")[1].split("&")[0]
            off = int(path.split("offset=")[1].split("&")[0])
            return {"campaigns": [c for c in self.camps if c["status"] == st][off:off + 100]}
        if path.endswith("/exportRecipients"):
            return {"processId": int(path.split("/")[2])}
        if path.startswith("/processes/"):
            return {"status": "completed", "export_url": "u:" + path.split("/")[2]}
        if path.startswith("/contacts/lists/"):
            lid, off = int(path.split("/")[3]), int(path.split("offset=")[1])
            src = self.l3 if lid == 3 else self.excl.get(lid, {})
            items = [{"email": e, "emailBlacklisted": m.get("bl", False), "createdAt": m.get("created")}
                     for e, m in src.items()]
            return {"contacts": items[off:off + 500]}
        raise AssertionError(path)

    def fetch(self, url):
        cid = int(url.split(":")[1])
        return "EMAIL;FIRSTNAME\n" + "\n".join(f"{e};x" for e in self.rcpt[cid])


class Q:
    def __init__(self, existing=(), slog=()):
        self.calls, self.existing, self.slog = [], list(existing), list(slog)

    def __call__(self, sql):
        self.calls.append(sql)
        if sql.startswith("CREATE") or sql.startswith("INSERT"):
            return []
        if f"`{PSC.T_CHECK}`" in sql:
            return self.existing
        if f"`{PSC.T_LOG}`" in sql:
            return [{"e": e} for e in self.slog]
        if f"`{PSC.T_RUN}`" in sql:
            return getattr(self, "runs", [])
        if f"`{PSC.T_EXCL98}`" in sql:
            return [{"e": e} for e in getattr(self, "cd", [])]
        raise AssertionError(sql[:80])


def l3(*emails, **kw):
    return {e: {"created": "2026-01-01T00:00:00Z"} for e in emails} | kw


class Checks(unittest.TestCase):
    def _run(self, camps, rcpt, members, excl=None, slog=(), now=NOW, existing=(), force=False):
        b, q, sent = Brevo(camps, rcpt, members, excl), Q(existing, slog), []
        res = PSC.run(q, b, b.fetch, lambda t: sent.append(t) or "uid-1", D, now=now, force=force, sleep=lambda s: None)
        return res, q, sent, b

    def test_all_good(self):
        res, q, sent, _ = self._run([camp(330, "2026-10-13T06:20:00Z"), camp(333, "2026-10-13T07:22:00Z", "236 x")],
                                    {330: {"a@x.lv"}, 333: {"b@x.lv", "c@x.lv"}}, l3("a@x.lv", "b@x.lv", "c@x.lv"),
                                    slog=("a@x.lv",))
        self.assertTrue(res["ok"])
        self.assertEqual((res["doubles"], res["empty"], res["eligible_lv"], res["recipients_distinct"]), (0, 0, 3, 3))
        self.assertEqual(res["brevo_not_in_send_log"], 2)
        self.assertEqual(len(sent), 1); self.assertTrue(sent[0].startswith("✅"))
        self.assertEqual(sent[0], "✅ Otrdienas vēstules 13.10: izsūtītas 2 (SD 2026-10-13 x 330 1, nedēļas akcija 2), "
                                  "kopā saņēma 3. Neviens nesaņēma divas vēstules. Visi, kam vēstule pienācās, to saņēma.")
        self.assertEqual(len([c for c in q.calls if c.startswith(f"INSERT INTO `{PSC.T_CHECK}`")]), 1)

    def test_double_and_empty_are_found_and_listed(self):
        res, q, sent, _ = self._run([camp(330, "2026-10-13T06:20:00Z"), camp(333, "2026-10-13T07:22:00Z")],
                                    {330: {"a@x.lv"}, 333: {"a@x.lv", "b@x.lv"}},
                                    l3("a@x.lv", "b@x.lv", "d@x.lv", "bl@x.lv", "ee@x.lv", "new@x.lv")
                                    | {"bl@x.lv": {"bl": True}, "new@x.lv": {"created": "2026-10-13T08:00:00Z"}},
                                    excl={75: {"ee@x.lv": {}}})
        self.assertFalse(res["ok"])
        self.assertEqual(res["doubles_sample"], {"a@x.lv": [330, 333]})
        self.assertEqual(res["empty_sample"], ["d@x.lv"])         # blacklisted, EE list and a newer contact left out
        self.assertTrue(sent[0].startswith("⚠️") and "1 cilvēks palika bez vēstules (piem. d@x.lv)" in sent[0])
        self.assertIn("1 cilvēks saņēma divas vēstules (piem. a@x.lv)", sent[0])
        det = [c for c in q.calls if c.startswith(f"INSERT INTO `{PSC.T_DETAIL}`")][0]
        self.assertIn("'DOUBLE', 'a@x.lv', '330,333'", det); self.assertIn("'EMPTY', 'd@x.lv', NULL", det)

    def test_test_campaigns_and_test_addresses_do_not_count(self):
        res, *_ = self._run([camp(1, "2026-10-13T06:00:00Z", "TEST proof x"), camp(2, "2026-10-13T06:10:00Z")],
                            {1: {"a@x.lv"}, 2: {"a@x.lv", "raivis@alenda.lv"}}, l3("a@x.lv", "raivis@alenda.lv"))
        self.assertEqual([c["id"] for c in res["campaigns"]], [2])
        self.assertTrue(res["ok"])

    def test_brevo_is_only_read(self):
        *_, b = self._run([camp(330, "2026-10-13T06:20:00Z")], {330: {"a@x.lv"}}, l3("a@x.lv"))
        self.assertEqual({m for m, p in b.calls if m != "GET"}, {"POST"})
        self.assertTrue(all(p.endswith("/exportRecipients") for m, p in b.calls if m == "POST"))

    def test_c_and_d_of_list_98_are_not_empty(self):
        b, q, sent = Brevo([camp(330, "2026-10-13T06:20:00Z")], {330: {"a@x.lv"}}, l3("a@x.lv", "cold@x.lv")), Q(), []
        q.cd = ["cold@x.lv"]
        res = PSC.run(q, b, b.fetch, lambda t: sent.append(t) or "u", D, now=NOW, sleep=lambda s: None)
        self.assertTrue(res["ok"]); self.assertEqual((res["empty"], res["excluded_cd"], res["eligible_lv"]), (0, 1, 1))
        self.assertIn("STARTS_WITH(why, 'c_') OR STARTS_WITH(why, 'd_')", [c for c in q.calls if PSC.T_EXCL98 in c][0])

    def test_mail_and_bookkeeping_time(self):
        b, q, mails = Brevo([camp(330, "2026-10-13T06:27:05Z", "SD 2026-10-13 reorder_1 (x)")], {330: {"a@x.lv"}},
                            l3("a@x.lv")), Q(), []
        q.runs = [{"campaign_id": 330, "f": dt.datetime(2026, 10, 13, 6, 40, 33, tzinfo=dt.timezone.utc)}]
        res = PSC.run(q, b, b.fetch, lambda t: "u", D, now=NOW, sleep=lambda s: None,
                      mail=lambda subj, text: mails.append((subj, text)))
        self.assertEqual(res["bookkeeping_max_s"], 808)
        self.assertEqual(res["campaigns"][0]["bookkeeping_s"], 808)
        self.assertEqual(mails[0][0], "Otrdienas vēstules 13.10 — viss kārtībā")
        self.assertIn("atgādinājums pasūtīt vēlreiz 1", mails[0][1]); self.assertEqual(res["email"], "SENT")
        q2, mails2 = Q(), []
        PSC.run(q2, b, b.fetch, lambda t: "u", D, now=NOW, sleep=lambda s: None, no_notify=True, whatsapp=False,
                mail=lambda subj, text: mails2.append(subj))
        self.assertEqual(mails2, [])                                     # a hand test run never mails Raivis

    def test_no_campaign_line(self):
        self.assertEqual(PSC.whatsapp_line(D, [], {}), "⚠️ Otrdienas vēstules 13.10: šodien neviena vēstule netika izsūtīta.")


class Wait(unittest.TestCase):
    def test_wait_rules(self):
        c = [{"id": 1, "status": "sent", "sent_at": "2026-10-13T09:00:00Z"}]
        self.assertTrue(PSC.decide(c, NOW, []).startswith("LAST_SENT_LESS_THAN_60"))
        self.assertIsNone(PSC.decide(c, NOW + dt.timedelta(minutes=31), []))
        self.assertTrue(PSC.decide(c + [{"id": 2, "status": "in_process", "sent_at": None}], NOW, []).startswith("CAMPAIGN_STILL"))
        self.assertEqual(PSC.decide([], NOW, []), "NO_CAMPAIGN_YET")
        self.assertEqual(PSC.decide(c, NOW + dt.timedelta(hours=2), [{"run_id": "x"}]), "ALREADY_CHECKED")
        final = dt.datetime(2026, 10, 13, 11, 35, tzinfo=dt.timezone.utc)       # 14:35 Riga: runs whatever the state
        self.assertIsNone(PSC.decide([], final, []))
        self.assertIsNone(PSC.decide(c, NOW, [{"run_id": "x"}], force=True))

    def test_wait_writes_nothing(self):
        b, q = Brevo([camp(1, "2026-10-13T09:00:00Z")], {}, {}), Q()
        res = PSC.run(q, b, b.fetch, lambda t: 1 / 0, D, now=NOW)
        self.assertIn("wait", res)
        self.assertFalse([c for c in q.calls if c.startswith("INSERT")])


class Notify(unittest.TestCase):
    def test_sends_unsent_rows_and_stamps_them(self):
        calls = []

        def q(sql):
            calls.append(sql)
            if sql.startswith("SELECT run_id, whatsapp_text"):
                return [{"run_id": "psc-1", "whatsapp_text": "✅ ok"}]
            return []
        sent = []
        out = PSC.notify(q, lambda t: sent.append(t) or "uid-9", D)
        self.assertEqual(sent, ["✅ ok"])
        self.assertEqual(out["sent"], [{"run_id": "psc-1", "uid": "uid-9", "error": None}])
        self.assertTrue(any("SET whatsapp_uid = 'uid-9'" in c and "'psc-1'" in c for c in calls))


if __name__ == "__main__":
    unittest.main()
