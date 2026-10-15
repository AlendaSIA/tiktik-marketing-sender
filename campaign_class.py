"""D2 (MAIN 4 2026-10-15 11:55): class rows for campaigns the ENGINE creates.

A campaign created by the engine (akcija_launch: weekly 236 and its test; edu.send: EDU per contact) gets its
mkt_control.brevo_campaign_class row at creation, between the POST /emailCampaigns and sendNow, already reviewed
(classified_by 'engine', reviewed_by 'auto: engine-created'). The nightly send_log import (sendlog_sync.CLASS_SQL)
inserts its 'unclassified' row only when no row exists, so it never overwrites this one, and the planner
(sequence_job.UNREVIEWED_SQL) never holds the recipients. Hand-made campaigns keep the MAIN review path.
A failed class write never blocks a send: engine_class() never raises; the campaign then falls back to the nightly
'unclassified' row (MAIN review). This module writes ONLY brevo_campaign_class - no send_log, no Brevo call."""
import datetime as dt
import zoneinfo

T_CLASS = "jaunais-za-aizv04022026.mkt_control.brevo_campaign_class"
ENGINE_BY, ENGINE_REVIEWED = "engine", "auto: engine-created"
# cls -> (kind, email_type, track, basis); same values MAIN set by hand for 333 / 328 / 290-296
ENGINE_CLASSES = {
    "akcija_weekly": ("broadcast", "akcija_weekly", "akcija", "list broadcast (sale week / greeting / product); "
                      "residual layer, not a lifecycle step; frequency only (same rule as 225)"),
    "akcija_test": ("test", None, "akcija", "preview/test send of the engine 236 frame; not a client send "
                    "(same rule as 197)"),
    "edu": ("edu", None, "educational", "educational broadcast; frequency only (same rule as 228)"),
    "edu_test": ("test", None, "educational", "preview/test send of an engine EDU letter; not a client send "
                 "(same rule as 197)"),
}
GUARD = f"NOT IN (SELECT campaign_id FROM `{T_CLASS}` WHERE campaign_id IS NOT NULL)"


def _lit(v) -> str:
    """A BigQuery literal without query parameters (send_day's q takes SQL only)."""
    if v is None:
        return "NULL"
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ").replace("\r", " ") + "'"


def engine_class_sql(cid: int, name: str, cls: str, created_at: str) -> str:
    kind, email_type, track, basis = ENGINE_CLASSES[cls]
    c = int(cid)
    return (f"INSERT INTO `{T_CLASS}` (campaign_id, name, sent_date, kind, email_type, track, step, rung, "
            f"counts_for_sequence, basis, classified_by, classified_at, reviewed_by) "
            f"SELECT {c}, {_lit(name[:200])}, {_lit(created_at)}, {_lit(kind)}, {_lit(email_type)}, {_lit(track)}, "
            f"NULL, NULL, FALSE, {_lit(basis)}, {_lit(ENGINE_BY)}, CURRENT_TIMESTAMP(), {_lit(ENGINE_REVIEWED)} "
            f"FROM UNNEST([1]) WHERE {c} {GUARD}")


def engine_class(q, cid, name, cls, now=None):
    """None = written (or a row already existed); 'CLASS_ROW_FAILED ...' = not written. NEVER raises."""
    try:
        now = now or dt.datetime.now(zoneinfo.ZoneInfo("Europe/Riga"))
        q(engine_class_sql(cid, str(name or ""), cls, now.isoformat(timespec="seconds")))
        return None
    except Exception as e:  # noqa: BLE001
        return f"CLASS_ROW_FAILED {type(e).__name__}: {e}"[:200]
