"""Test letters of a later ROUND: "[TESTS v2 232 - winback_2 - 4/8]" style prefix (MAIN, command 5, 2026-09-25).

draft_test.py writes "[TESTS <id> . <variant> . N/M]" in front of the subject. When a changed template is sent
to Raivis again, the new letter must be told apart from the first round in his inbox, so MAIN asked for
"[TESTS v2 <id> . <variant> . N/8]". This file runs draft_test.main() UNCHANGED except for that prefix:
    python draft_test_round.py --round v2 <every draft_test.py argument>
Everything else - the checks, the only recipient (draft_test.TEST_RECIPIENT), --send-limit, and the job
wrapper that refuses --send while the job is locked - is draft_test's own and is not touched here.
"""
import re
import sys

import draft_test as D


def round_prefix(round_label, base):
    """Wrap draft_test's prefix function so its output reads "[TESTS <round> <rest>"."""
    if not re.fullmatch(r"v\d{1,2}", round_label or ""):
        raise ValueError("round must look like v2")

    def prefixed(template_id, idx, n, variant=None, seq=None):
        p = base(template_id, idx, n, variant, seq)
        assert p.startswith("[TESTS "), p
        return "[TESTS " + round_label + " " + p[len("[TESTS "):]
    return prefixed


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--round" not in argv or argv.index("--round") + 1 >= len(argv):
        sys.exit("--round vN is required, e.g. --round v2")
    i = argv.index("--round")
    label = argv[i + 1]
    del argv[i:i + 2]
    D.subject_prefix = round_prefix(label, D.subject_prefix)
    return D.main(argv)


if __name__ == "__main__":
    sys.exit(main())
