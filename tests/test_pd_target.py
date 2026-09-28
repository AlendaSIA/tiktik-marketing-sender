"""PD target by send address (MAIN 2026-09-28 COMMAND 4, P-A..P-E). Standard library only."""
import datetime as dt
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pd_record  # noqa: E402
import pd_target as T  # noqa: E402

UTC = dt.timezone.utc
P = T.Person


def t(y, m, d):
    return dt.datetime(y, m, d, tzinfo=UTC)


def R(address, idx, **kw):
    kw.setdefault("persons_age_h", 4.0)
    return T.resolve(address, by_address=idx, **kw)


class Classes(unittest.TestCase):
    def test_c1_one_holder(self):
        tg = R("A@x.lv ", {"a@x.lv": [P(10, 5, "Anna", "a@x.lv")]})
        self.assertEqual((tg.kind, tg.cls, tg.person_id, tg.org_id, tg.participants), ("person", "C1", 10, 5, (10,)))

    def test_c1_same_person_twice_in_index_is_one(self):
        p = P(10, 5, "Anna", "a@x.lv")
        self.assertEqual(R("a@x.lv", {"a@x.lv": [p, p]}).kind, "person")

    def test_c2a_true_duplicates_held(self):
        idx = {"a@x.lv": [P(10, 5, "Anna Ozola", "a@x.lv"), P(11, 5, "anna  ozola", "a@x.lv")]}
        tg = R("a@x.lv", idx)
        self.assertEqual((tg.kind, tg.cls, tg.hold_reason, tg.person_id), ("held", "C2a", "pd_duplicate_person", None))

    def test_c2b_shared_mailbox_org_plus_all_holders_primary_by_primary_email(self):
        idx = {"info@f.lv": [P(20, 7, "Jānis", "janis@f.lv", t(2026, 9, 20)),
                             P(21, 7, "Ilze", "info@f.lv", t(2026, 1, 1)),
                             P(22, 7, "Pēteris", None, t(2026, 9, 27))]}
        tg = R("info@f.lv", idx)
        self.assertEqual((tg.kind, tg.cls, tg.org_id, tg.person_id), ("org_participants", "C2b", 7, 21))
        self.assertEqual(tg.participants[0], 21)
        self.assertEqual(set(tg.participants), {20, 21, 22})

    def test_c2b_primary_falls_back_to_newest_update(self):
        idx = {"info@f.lv": [P(20, 7, "Jānis", "j@f.lv", t(2026, 9, 20)), P(22, 7, "Pēteris", "p@f.lv", t(2026, 9, 27))]}
        self.assertEqual(R("info@f.lv", idx).person_id, 22)

    def test_c2c_multi_org_held_unless_override(self):
        idx = {"info@f.lv": [P(20, 7, "Jānis", "info@f.lv"), P(30, 8, "Ilze", "info@f.lv")]}
        tg = R("info@f.lv", idx)
        self.assertEqual((tg.kind, tg.cls, tg.hold_reason), ("held", "C2c", "pd_ambiguous_org"))
        ov = R("info@f.lv", idx, overrides={"info@f.lv": (30, 8)})
        self.assertEqual((ov.kind, ov.cls, ov.person_id, ov.org_id), ("person", "override", 30, 8))

    def test_c3_person_on_another_address_of_the_master(self):
        idx = {"old@x.lv": [P(40, None, "Liene", "old@x.lv")]}
        tg = R("new@x.lv", idx, master_other_addresses=["new@x.lv", "OLD@x.lv"])
        self.assertEqual((tg.kind, tg.cls, tg.person_id), ("person", "C3", 40))
        self.assertNotIn("new@x.lv", idx)          # the address is never added to the person

    def test_c4_address_only_on_an_org(self):
        tg = R("birojs@f.lv", {}, org_by_address={"birojs@f.lv": {7}})
        self.assertEqual((tg.kind, tg.cls, tg.person_id, tg.org_id, tg.participants), ("org_only", "C4", None, 7, ()))

    def test_c5_nowhere_held_no_person(self):
        tg = R("x@y.lv", {}, org_by_address={})
        self.assertEqual((tg.kind, tg.cls, tg.hold_reason), ("held", "C5", "pd_no_person"))

    def test_stale_or_unknown_snapshot_held_never_guessed(self):
        idx = {"a@x.lv": [P(10, 5, "Anna", "a@x.lv")]}
        for age in (26.1, 40, None):
            tg = R("a@x.lv", idx, persons_age_h=age)
            self.assertEqual((tg.kind, tg.hold_reason, tg.person_id), ("held", "pd_persons_stale", None), age)
        self.assertEqual(R("a@x.lv", idx, persons_age_h=26).kind, "person")


class Record(unittest.TestCase):
    def rec(self, tg):
        return pd_record.render(person_id=tg.person_id, org_id=tg.org_id, master_key="m", email="info@f.lv",
                                email_type="reorder_1", template_id=179, send_date=dt.date(2026, 9, 28),
                                offer_rung=0, reason="r", campaign_ref="(shadow)", participants=tg.participants)

    def test_participants_in_the_record_and_version_2(self):
        tg = T.Target("org_participants", "C2b", 21, 7, (21, 20))
        r = self.rec(tg)
        self.assertEqual((r["record_version"], r["participant_person_ids"], r["target_org_id"]), ("pd-record-v2", [21, 20], 7))

    def test_live_org_only_allowed_held_refused(self):
        seen = []
        pd_record.write({**self.rec(T.Target("org_only", "C4", None, 7, ())), "type_key": "k", "type_id": 1},
                        shadow=False, pd_writer=seen.append, shadow_sink=None)
        self.assertEqual(len(seen), 1)
        with self.assertRaises(ValueError):
            pd_record.write({**self.rec(T.Target("held", "C5", hold_reason="pd_no_person")), "type_key": "k", "type_id": 1},
                            shadow=False, pd_writer=seen.append, shadow_sink=None)


class JobWiring(unittest.TestCase):
    SRC = open(os.path.join(ROOT, "sequence_job.py")).read()

    def test_lifecycle_person_id_is_not_read(self):
        self.assertNotIn('f["person_id"]', self.SRC)
        self.assertNotIn("lc.person_id", self.SRC)

    def test_target_resolved_by_send_address(self):
        self.assertIn('pd_target.resolve(f["send_email"]', self.SRC)
        self.assertIn('"pd_hold_reason": tg.hold_reason', self.SRC)

    def test_built_at_is_logged_and_reported(self):
        self.assertIn("rung_price_built_at=%s", self.SRC)
        self.assertIn('"rung_price_built_at"', self.SRC)


if __name__ == "__main__":
    unittest.main()
