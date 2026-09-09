"""The three re-checks that run at the MOMENT Raivis presses, not when the mail was written.

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
CHECK_NO_APPROVAL = "NO_APPROVAL_ROW"


def evaluate(*, send_date, build_id_in_mail, live_build_id, overlap_people,
             credits_available, credits_needed, approval_row=None):
    """Return one entry per check, in the order they should be shown. Never raises."""
    checks = []

    checks.append(_check(
        CHECK_NO_APPROVAL,
        passed=bool(approval_row) and not approval_row.get("revoked_at"),
        reason_lv=(f"Neizsūtu: {send_date} nav tava apstiprinājuma. Klusēšana nav piekrišana."
                   if not approval_row else
                   f"Neizsūtu: {send_date} apstiprinājums ir atsaukts "
                   f"({(approval_row or {}).get('revoked_reason') or 'iemesls nav pierakstīts'})."),
        detail=f"approval_row={'present' if approval_row else 'absent'}"))

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
