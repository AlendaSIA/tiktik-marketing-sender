"""Pins MAIN command 5 (2026-09-25): the fresh line must be wrapped in the v2.8 fresh flag before any send,
and the round prefix of re-sent test letters. Standard library only, no network."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import presend as P  # noqa: E402
import draft_test_round as R  # noqa: E402

UNGATED = '{% if contact.P1_NAME %}Tikko iepirkam jaunu partiju ({{ contact.P1_NAME }}).{% endif %}'
GATED = '{% if contact.P1_FRESH %}' + UNGATED + '{% endif %}'
IN_ELSE = '{% if contact.P1_FRESH %}x{% else %}' + UNGATED + '{% endif %}'
DOT = chr(0xB7)  # the middle dot draft_test puts between the prefix parts


class FreshLine(unittest.TestCase):

    def test_no_flag_in_the_contract_blocks_every_fresh_line(self):
        self.assertEqual(len(P.fresh_line_blockers(GATED, flag=None)), 1)
        self.assertIsNone(P.FRESH_FLAG_FIELD)

    def test_inside_the_flag_passes(self):
        self.assertEqual(P.fresh_line_blockers(GATED, flag="P1_FRESH"), [])

    def test_outside_the_flag_blocks(self):
        self.assertEqual(len(P.fresh_line_blockers(UNGATED, flag="P1_FRESH")), 1)

    def test_else_branch_of_the_flag_blocks(self):
        self.assertEqual(len(P.fresh_line_blockers(IN_ELSE, flag="P1_FRESH")), 1)

    def test_a_letter_without_the_word_passes(self):
        self.assertEqual(P.fresh_line_blockers("<p>Tava cena</p>", flag=None), [])


class RoundPrefix(unittest.TestCase):

    def test_round_goes_after_tests(self):
        base = lambda t, i, n, v=None, s=None: "[TESTS %s %s %s %s %s]" % (t, DOT, v, DOT, s)  # noqa: E731
        f = R.round_prefix("v2", base)
        self.assertEqual(f(232, 1, 1, "winback_2", "4/8"), "[TESTS v2 232 %s winback_2 %s 4/8]" % (DOT, DOT))

    def test_bad_round_is_refused(self):
        with self.assertRaises(ValueError):
            R.round_prefix("2", lambda *a: "[TESTS x]")


if __name__ == "__main__":
    unittest.main()
