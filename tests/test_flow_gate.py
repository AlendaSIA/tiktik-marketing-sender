import unittest

import flow_gate as FG

ENV = {"PROOF_RECIPIENT": "alenda.jurmala@gmail.com"}


class Q:
    def __init__(self, members=(), lists=()):
        self.calls, self.members, self.lists = [], list(members), list(lists)

    def __call__(self, s):
        self.calls.append(s)
        if s.startswith("SELECT list_name, list_id"):
            return self.lists
        if s.startswith(f"SELECT email FROM `{FG.T_MEMBERS}`"):
            key = s.split("list_name = '")[1].split("'")[0]
            return [{"email": e} for k, e in self.members if k == key]
        if s.startswith("INSERT INTO `" + FG.T_LISTS):
            return []
        if "FROM `" + FG.T_EVENTS in s and s.startswith("SELECT e.master_key"):
            return [{"master_key": "cid:1", "order_id": "M-1", "client": "SIA X"}]
        return []


class Brevo:
    def __init__(self, have=None):
        self.calls, self.have, self.next_id = [], {k: set(v) for k, v in (have or {}).items()}, 500

    def __call__(self, method, path, payload):
        self.calls.append((method, path, payload))
        if method == "POST" and path == "/contacts/lists":
            self.next_id += 1
            self.have[self.next_id] = set()
            return {"id": self.next_id}
        if method == "GET" and "/contacts?" in path:
            lid = int(path.split("/")[3])
            off = int(path.split("offset=")[1])
            return {"contacts": [{"email": e} for e in sorted(self.have.get(lid, set()))[off:off + 500]]}
        if path.endswith("/contacts/add"):
            lid = int(path.split("/")[3])
            ok = [e for e in payload["emails"] if not e.startswith("ghost")]
            self.have[lid] |= set(ok)
            return {"contacts": {"success": ok, "failure": [e for e in payload["emails"] if e.startswith("ghost")]}}
        if path.endswith("/contacts/remove"):
            self.have[int(path.split("/")[3])] -= set(payload["emails"])
            return {}
        if path == "/smtp/email":
            return {"messageId": "<m@x>"}
        raise AssertionError((method, path))


class FlowGate(unittest.TestCase):
    def test_build_runs_the_gate_on_the_given_run(self):
        q = Q()
        FG.build(q, "run-x")
        self.assertTrue(any("WHERE p.run_id = 'run-x'" in c for c in q.calls))
        self.assertFalse(any("@run_id" in c for c in q.calls))

    def test_lists_are_created_once_and_synced_add_remove(self):
        q = Q(members=[("NO_PROMO", "a@x.lv"), ("NO_PROMO", "ghost@x.lv"), ("NO_ANY", "a@x.lv")],
              lists=[{"list_name": "NO_PROMO", "list_id": 7}])
        b = Brevo(have={7: {"old@x.lv"}})
        res = FG.sync_lists(q, b)
        self.assertEqual(res["NO_PROMO"], {"list_id": 7, "wanted": 2, "added": 1, "removed": 1,
                                           "not_a_brevo_contact": 1, "refused_by_brevo": 0, "members_now": 1})
        self.assertEqual(res["NO_ANY"]["list_id"], 501)
        self.assertEqual(len([c for c in b.calls if c[1] == "/contacts/lists"]), 1)
        self.assertFalse([c for c in b.calls if "emailCampaigns" in c[1] or c[1] == "/smtp/email"])

    def test_one_bad_address_does_not_sink_the_chunk(self):
        class B(Brevo):
            def __call__(self, method, path, payload):
                if path.endswith("/contacts/add") and any(e.startswith("bad") for e in payload["emails"]):
                    raise RuntimeError("400 invalid")
                return super().__call__(method, path, payload)
        q = Q(members=[("NO_ANY", "a@x.lv"), ("NO_ANY", "bad@x"), ("NO_ANY", "c@x.lv")],
              lists=[{"list_name": "NO_PROMO", "list_id": 7}, {"list_name": "NO_ANY", "list_id": 8}])
        b = B(have={7: set(), 8: set()})
        res = FG.sync_lists(q, b)["NO_ANY"]
        self.assertEqual((res["added"], res["refused_by_brevo"], res["members_now"]), (2, 1, 2))

    def test_notify_stays_off_without_the_flag(self):
        b = Brevo()
        self.assertEqual(FG.notify(Q(), b, ENV)["sent"], 0)
        self.assertEqual(b.calls, [])

    def test_notify_goes_only_to_the_proof_recipient(self):
        b, q = Brevo(), Q()
        res = FG.notify(q, b, dict(ENV, FLOW_GATE_NOTIFY="on"))
        self.assertEqual(res, {"to": "alenda.jurmala@gmail.com", "sent": 1})
        self.assertEqual(b.calls[0][2]["to"], [{"email": "alenda.jurmala@gmail.com"}])
        self.assertIn("SIA X no manuālās plūsmas nopirka veikalā (pasūtījums M-1), pārlikts uz Veikals",
                      b.calls[0][2]["textContent"])

    def test_notify_test_refuses_without_a_recipient(self):
        import send_path as SP
        with self.assertRaises(SP.SendLocked):
            FG.notify_test(Brevo(), {})
        r = FG.notify_test(Brevo(), ENV)
        self.assertEqual(r["to"], "alenda.jurmala@gmail.com")


if __name__ == "__main__":
    unittest.main()
