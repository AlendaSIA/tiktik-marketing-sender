"""Phase 8: the machinery that lets a variant stop being pressed. Buildable now; unusable yet.

This sat last in the plan for the wrong reason. Only the EVIDENCE for flipping a variant to auto has
to wait for real sending days - the machinery does not, and building it late is what made it look
like the end of the plan.

THE ABSENT STATE STILL ASKS. A variant with no row in mkt_control.variant_send_mode is 'pressed'.
That is the whole design in one line: forgetting to configure a variant can never be what makes it
send by itself.

THE EVIDENCE CANNOT BE MANUFACTURED. clean_day_streak reads mkt_control.variant_clean_days, which is
derived from dispatch facts. There is no route to a streak except actually sending cleanly, and
today every streak is 0 because nothing has ever been sent.
"""
import logging

import bq
import config as C

log = logging.getLogger("autonomy")

DEFAULT_CLEAN_DAYS_REQUIRED = 4
T_MODE = f"`{C.PROJECT}.{C.CONTROL}.variant_send_mode`"
T_CLEAN = f"`{C.PROJECT}.{C.CONTROL}.variant_clean_days`"


def send_mode(email_type: str):
    """('pressed'|'auto', clean_days_required). Absent, revoked or unreadable all mean 'pressed'."""
    from google.cloud.bigquery import ScalarQueryParameter as P
    try:
        rows = bq.query(
            f"SELECT send_mode, clean_days_required FROM {T_MODE} "
            f"WHERE email_type = @t AND revoked_at IS NULL "
            f"ORDER BY granted_at DESC LIMIT 1", [P("t", "STRING", email_type)])
    except Exception as e:  # noqa: BLE001
        log.warning("SEND_MODE_UNREADABLE %r - treating %s as pressed", e, email_type)
        return "pressed", DEFAULT_CLEAN_DAYS_REQUIRED
    if not rows:
        return "pressed", DEFAULT_CLEAN_DAYS_REQUIRED
    return (rows[0]["send_mode"] or "pressed"), int(
        rows[0]["clean_days_required"] or DEFAULT_CLEAN_DAYS_REQUIRED)


def clean_day_streak(email_type: str) -> int:
    """Consecutive clean sending days, counted backwards from the most recent one.

    Counted here rather than in SQL because 'consecutive' is the whole meaning: a variant with four
    clean days and a dirty one in the middle has a streak of however many came after the dirty one,
    and a SQL COUNT would happily call that four.
    """
    from google.cloud.bigquery import ScalarQueryParameter as P
    rows = bq.query(
        f"SELECT send_date, clean FROM {T_CLEAN} WHERE email_type = @t "
        f"ORDER BY send_date DESC", [P("t", "STRING", email_type)])
    streak = 0
    for r in rows:
        if not r["clean"]:
            break
        streak += 1
    return streak


def may_auto_send(email_type: str):
    """(bool, reason_lv). False is the answer whenever anything is missing or unreadable."""
    mode, required = send_mode(email_type)
    if mode != "auto":
        return False, (f"{email_type} vel iet caur tavu pogu. Automatisku sutisanu iesledz tikai "
                       f"tu, pa vienam variantam.")
    streak = clean_day_streak(email_type)
    if streak < required:
        return False, (f"{email_type} ir atzimets ka automatisks, bet tiro sutisanas dienu pec "
                       f"kartas ir {streak}, vajag {required}. Sodien tas iet caur pogu.")
    return True, None
