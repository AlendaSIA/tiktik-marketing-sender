"""The three checks that run at the MOMENT Raivis presses, and the one gate that runs at the send.

Pure functions on purpose: no BigQuery client, no Brevo client, no clock. Everything they judge
arrives as an argument, so every refusal can be exercised in a test without a warehouse and without
a credential - which is the only reason it is possible to prove today that they refuse.

WHY A RE-CHECK EXISTS AT ALL. The approval e-mail is written the afternoon before and pressed the
next day. Between those two moments the night rebuilds the assignment, lists are materialised, and
credits are spent. Approving a batch is approving THOSE numbers; if they moved, he has approved
something that no longer exists.

EACH CHECK REFUSES SEPARATELY AND NAMES ITSELF. A single "checks failed" tells a man standing in
front of a button nothing he can act on. reason_lv is what he reads; detail is what the log keeps.
The Latvian wording is Marketing's to finalise - copy is theirs, not the builder's - but it ships
written rather than as a TODO, because a placeholder string is what reaches production.
"""

CHECK_AUDIENCE_CHANGED = "AUDIENCE_CHANGED"
CHECK_PERSON_IN_TWO_LISTS = "PERSON_IN_TWO_LISTS"
CHECK_CREDITS = "NOT_ENOUGH_CREDITS"
# NOT a press check any more (MAIN, 2026-09-11). It is the SEND-TIME gate: see send_gate().
CHECK_NO_APPROVAL = "NO_APPROVAL_ROW"

# The three checks the PRESS judges, in the order they are shown. Pinned by tests.
PRESS_CHECKS = (CHECK_AUDIENCE_CHANGED, CHECK_PERSON_IN_TWO_LISTS, CHECK_CREDITS)


def evaluate(*, send_date, build_id_in_mail, live_build_id, overlap_people,
             credits_available, credits_needed):
    """Return one entry per PRESS check, in the order they should be shown. Never raises.

    THE PRESS IS THE APPROVAL (MAIN, 2026-09-11). Until this change the press also demanded that
    an approval row already exist (NO_APPROVAL_ROW), while record_approval() refused to write that
    row without a passing verdict - each waited for the other, so the first real press could never
    pass. The press is the act of approving; asking it for a prior approval asks it for itself.
    "Silence is not consent" did not go away: it moved to the only moment it can mean something,
    the send, in send_gate() below.
    """
    checks = []

    same_build = (build_id_in_mail is not None and build_id_in_mail == live_build_id)
    checks.append(_check(
        CHECK_AUDIENCE_CHANGED,
        passed=same_build,
        reason_lv=("Neizsūtu: kopš vēstules uzrakstīšanas ir mainījies, kurš ko saņem. "
                   "Tu apstiprināji citu sarakstu, nekā aizietu tagad. Atver jauno vēstuli."),
        detail=f"build_id in mail={build_id_in_mail} live={live_build_id}"))

    checks.append(_check(
        CHECK_PERSON_IN_TWO_LISTS,
        passed=(overlap_people == 0),
        reason_lv=(f"Neizsūtu: {overlap_people} cilvēk(i) šodien ir divos sarakstos un saņemtu "
                   f"divas vēstules vienā dienā."),
        detail=f"day_list_overlap rows={overlap_people}"))

    checks.append(_check(
        CHECK_CREDITS,
        passed=(credits_available >= credits_needed),
        reason_lv=(f"Neizsūtu: Brevo kredītu ir {credits_available}, bet šai dienai vajag "
                   f"{credits_needed}. Daļa cilvēku vēstuli nesaņemtu, un mēs neuzzinātu, kuri."),
        detail=f"credits available={credits_available} needed={credits_needed}"))

    return checks


def send_gate(*, send_date, batch_id, build_id, approval_row):
    """The SEND-TIME gate: no approval row for exactly THIS batch_id and THIS build_id, no send.

    Same words as the old press check, because it is the same rule in its right place: silence is
    not consent. approval_row is whatever the lookup found for (batch_id, build_id); it is checked
    against both again here, so a lookup that returned a row for another batch or another build -
    a newer day, a recomputed audience - is refused rather than trusted. Pure: no clock, no client.
    """
    row = approval_row or {}
    matches = (bool(approval_row) and row.get("batch_id") == batch_id
               and row.get("assignment_build_id") == build_id)
    revoked = bool(row.get("revoked_at"))
    if matches and revoked:
        reason = (f"Neizsūtu: {send_date} apstiprinājums ir atsaukts "
                  f"({row.get('revoked_reason') or 'iemesls nav pierakstīts'}).")
    else:
        reason = f"Neizsūtu: {send_date} nav tava apstiprinājuma. Klusēšana nav piekrišana."
    return _check(
        CHECK_NO_APPROVAL,
        passed=matches and not revoked,
        reason_lv=reason,
        detail=(f"batch_id={batch_id} build_id={build_id} approval_row="
                f"{'absent' if not approval_row else 'present'}"
                f"{'' if not approval_row else ' batch=' + str(row.get('batch_id')) + ' build=' + str(row.get('assignment_build_id'))}"))


def _check(check_id, *, passed, reason_lv, detail):
    return {"id": check_id, "passed": bool(passed),
            "reason_lv": None if passed else reason_lv, "detail": detail}


def may_press(checks) -> bool:
    """Fail-closed: every check must have passed. An empty list is NOT a pass."""
    return bool(checks) and all(c["passed"] for c in checks)


def refusal_text(checks) -> str:
    """What the board shows when the press is refused. Every failed check, not just the first.

    Showing only the first would make the second one a surprise tomorrow, which is how a person
    learns to press twice and stop reading.
    """
    bad = [c for c in checks if not c["passed"]]
    if not bad:
        return ""
    return "\n".join(f"· {c['reason_lv']}" for c in bad)
