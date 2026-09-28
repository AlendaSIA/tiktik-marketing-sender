"""PD write-back planned at live send (contract v2.9.3 + v2.9.4, sha 3818b716589d). Standard library only."""
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pd_record  # noqa: E402
import pd_target as T  # noqa: E402
import pd_writeback as W  # noqa: E402
import sequence as S  # noqa: E402

D = dt.date
ORGS = [{"id": 7, "name": "SIA \"Zobu Ārsts\"", "reg_number": "40003123456"},
        {"id": 8, "name": "Salons Rīga", "reg_number": None},
        {"id": 9, "name": "Salons  rīga", "reg_number": None}]
PERSONS = [{"org_id": 7, "emails": ["anna@zobuarsts.lv"]}, {"org_id": 7, "emails": ["x@gmail.com"]}]
IDX = W.build_org_index(ORGS, PERSONS)


def P(tg, **kw):
    a = dict(email="new@firma.lv", person_name="Jānis Bērziņš", org_name="Jānis Bērziņš", reg_nr=None, org_idx=IDX)
    a.update(kw)
    return W.plan(tg, **a)


class OrgGuard(unittest.TestCase):
    def test_reg_nr_first_then_exact_name_then_own_domain(self):
        self.assertEqual(W.find_org(IDX, reg_nr="LV 40003123456"), (7, "reg_nr", False))
        self.assertEqual(W.find_org(IDX, name="Zobu ārsts SIA"), (7, "exact_name", False))
        self.assertEqual(W.find_org(IDX, email="new@zobuarsts.lv"), (7, "domain", False))

    def test_free_mail_domain_never_links(self):
        self.assertEqual(W.find_org(IDX, email="someone@gmail.com"), (None, None, False))

    def test_free_mail_domain_never_enters_the_index(self):
        self.assertNotIn("gmail.com", IDX["domain"])
        self.assertEqual(IDX["domain"].get("zobuarsts.lv"), {7})

    def test_lookup_refuses_free_mail_even_if_an_index_holds_it(self):
        bad = {"reg": {}, "name": {}, "domain": {"gmail.com": {7}}}
        self.assertEqual(W.find_org(bad, email="x@gmail.com"), (None, None, False))

    def test_two_orgs_with_one_name_is_ambiguous_not_a_guess(self):
        self.assertEqual(W.find_org(IDX, name="Salons Rīga"), (None, "exact_name", True))


class Creates(unittest.TestCase):
    def test_c1_person_with_org_creates_nothing(self):
        self.assertEqual(P(T.Target("person", "C1", 10, 7, (10,)))["creates"], [])

    def test_person_without_org_links_existing_org_instead_of_creating(self):
        w = P(T.Target("person", "C1", 10, None, (10,)), org_name="Zobu Ārsts", email="a@zobuarsts.lv")
        self.assertEqual([c["object"] for c in w["creates"]], ["org_link", "person_org_link"])
        self.assertEqual((w["org_ref"], w["creates"][0]["match_how"]), (7, "exact_name"))

    def test_person_without_org_creates_org_with_same_name_then_links(self):
        w = P(T.Target("person", "C1", 10, None, (10,)))
        self.assertEqual([c["object"] for c in w["creates"]], ["org_create", "person_org_link"])
        self.assertEqual((w["creates"][0]["name"], w["org_ref"], w["person_ref"]), ("Jānis Bērziņš", "new:org", 10))

    def test_no_person_creates_person_and_org(self):
        w = P(T.Target("held", "C5", hold_reason="pd_no_person"))
        self.assertEqual([c["object"] for c in w["creates"]], ["org_create", "person_create"])
        self.assertEqual((w["person_ref"], w["hold_reason"]), ("new:person", None))

    def test_org_only_gets_a_person_in_that_org(self):
        w = P(T.Target("org_only", "C4", None, 7, ()))
        self.assertEqual([(c["object"], c.get("org_ref")) for c in w["creates"]], [("person_create", 7)])

    def test_ambiguous_org_holds_instead_of_creating(self):
        w = P(T.Target("person", "C1", 10, None, (10,)), org_name="Salons Rīga")
        self.assertEqual((w["creates"], w["hold_reason"]), ([], "pd_org_ambiguous"))

    def test_other_holds_stay_holds(self):
        for h in ("pd_duplicate_person", "pd_ambiguous_org", "pd_persons_stale"):
            w = P(T.Target("held", "C2a", hold_reason=h))
            self.assertEqual((w["creates"], w["hold_reason"]), ([], h))


class Fields(unittest.TestCase):
    def test_four_fields_with_contract_keys(self):
        f = W.field_writes(10, email_type="winback_2", send_date=D(2026, 10, 1), offer_rung=2,
                           offer_valid_until=D(2026, 10, 7))
        self.assertEqual([(x["field_key"], x["field_value"]) for x in f], [
            ("20e977e74489c1ff17fcea50c5a65e09692d1d60", "726"),
            ("98eb0a33b6563fd78b1a704dfec70a581cfd5dd5", "2026-10-07"),
            ("29a3179972cc1079f80eeefdb4aeec5af4837b1e", "winback_2"),
            ("a849d7df5eeded7a2fc631a6f86261858454199f", "2026-10-01")])

    def test_rung_0_option_724_and_empty_until(self):
        f = W.field_writes(10, email_type="reorder_1", send_date=D(2026, 10, 1), offer_rung=0, offer_valid_until=None)
        self.assertEqual((f[0]["field_value"], f[1]["field_value"]), ("724", ""))


class Activity(unittest.TestCase):
    def test_type_32_done_person_and_org_and_offer_in_subject_note(self):
        tail, lines = W.offer_summary([{"sku": "NIT-M", "price": "8,90 €", "ref": "10,50 €"}, {"sku": None, "price": None, "ref": None}])
        r = pd_record.render(person_id=10, org_id=7, master_key="m", email="a@b.lv", email_type="winback_1",
                             template_id=180, send_date=D(2026, 10, 1), offer_rung=1, reason="r", campaign_ref="c",
                             offer_valid_until=D(2026, 10, 7), offer_tail=tail, product_lines=lines)
        self.assertEqual((r["type_key"], r["type_id"], r["done"], r["target_person_id"], r["target_org_id"]),
                         ("_e_pasts_automatisks", 32, True, 10, 7))
        self.assertIn("1 personīgas cenas", r["subject"])
        self.assertIn("NIT-M: 8,90 € (veikalā 10,50 €)", r["note"])
        self.assertIn("spēkā līdz: 2026-10-07", r["note"])

    def test_shadow_still_makes_zero_pd_calls(self):
        calls = []
        pd_record.write(pd_record.render(person_id=1, org_id=None, master_key="m", email="a@b.lv", email_type="reorder_1",
                                         template_id=179, send_date=D(2026, 10, 1), offer_rung=0, reason="r",
                                         campaign_ref="c"), shadow=True, pd_writer=calls.append, shadow_sink=lambda r: None)
        self.assertEqual(calls, [])


class JobWiring(unittest.TestCase):
    SRC = open(os.path.join(ROOT, "sequence_job.py")).read()

    def test_c5_is_created_not_queued_and_writes_only_to_shadow_table(self):
        self.assertIn("hold_pd = wb[\"hold_reason\"]", self.SRC)
        self.assertIn("W.field_writes(", self.SRC)
        self.assertNotIn("pd_writer=pd", self.SRC)          # the shadow job never hands a real writer


class PlannerStepAdvancesOnlyOnSend(unittest.TestCase):
    """Item 1 (28.09): reorder_2/3 and winback_3 only follow a SENT previous letter - shadow never sends."""

    def test_reorder_1_2_3_after_sends_14_days_apart(self):
        f = S.Facts("reorder_due", D(2026, 8, 1), D(2025, 1, 1), False, None, 30)
        st, got = S.State("r"), []
        day = D(2026, 9, 28)
        for _ in range(3):
            d = S.advance(st, f, day)
            got.append((d.next_email_type, d.next_due_on))
            st = S.record_sent(d.state, d.next_email_type, d.next_due_on, d.offer_rung)
            day = d.next_due_on + dt.timedelta(days=14)
        self.assertEqual(got, [("reorder_1", D(2026, 9, 28)), ("reorder_2", D(2026, 10, 12)), ("reorder_3", D(2026, 10, 26))])

    def test_without_a_send_the_step_stays(self):
        f = S.Facts("reorder_due", D(2026, 8, 1), D(2025, 1, 1), False, None, 30)
        st = S.advance(S.State("r2"), f, D(2026, 9, 28)).state
        self.assertEqual(S.advance(st, f, D(2026, 10, 20)).next_email_type, "reorder_1")

    def test_winback_3_follows_a_sent_winback_2_next_month(self):
        far = {3: D(2027, 1, 1)}
        st = S.State("w", "winback", D(2026, 9, 1), 2, 2, D(2026, 10, 1), "2026-10", None, "winback_2", D(2026, 10, 1))
        d = S.advance(st, S.Facts("winback", D(2026, 3, 1), D(2025, 1, 1), False, far, 30), D(2026, 10, 20))
        self.assertEqual((d.next_email_type, d.next_due_on, d.offer_rung), ("winback_3", D(2026, 11, 1), 3))


if __name__ == "__main__":
    unittest.main()
