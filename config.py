"""Configuration. Every knob is an environment variable with a safe default.

Safety defaults are deliberate: DRY_RUN starts true and ALLOW_SEND starts false, so a
misconfigured deployment reports instead of sending. Turning both off is the only way to
send, and the gate in main.py additionally refuses unless the plan is clean.
"""
import os

PROJECT = os.environ.get("BQ_PROJECT", "jaunais-za-aizv04022026")
MARTS = os.environ.get("BQ_MARTS", "business_marts")
CONTROL = os.environ.get("BQ_CONTROL", "mkt_control")
BQ_LOCATION = os.environ.get("BQ_LOCATION", "EU")

# --- guards -----------------------------------------------------------------
IDENTITY_MAX_AGE_H = float(os.environ.get("IDENTITY_MAX_AGE_H", "36"))
SUPPRESSION_MAX_AGE_H = float(os.environ.get("SUPPRESSION_MAX_AGE_H", "26"))

# Freshness of the Brevo template mirror (mkt_control.brevo_template_status), which is
# written by blk-brevo-contacts-snapshot at 06:30. 36 h is the same threshold
# track_send_readiness uses, so the two never disagree about what "stale" means.
# This is NOT decoration: that scheduler had never run successfully before 2026-09-04,
# and a mirror that quietly freezes reads as current. A stale mirror BLOCKS.
TEMPLATE_STATUS_MAX_AGE_H = float(os.environ.get("TEMPLATE_STATUS_MAX_AGE_H", "36"))

# --- frequency rules (identity rule b: per PERSON, not per address) ---------
MAX_EMAILS_PER_WEEK = int(os.environ.get("MAX_EMAILS_PER_WEEK", "2"))
MIN_DAYS_BETWEEN = int(os.environ.get("MIN_DAYS_BETWEEN", "2"))

# --- sending ----------------------------------------------------------------
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
ALLOW_SEND = os.environ.get("ALLOW_SEND", "false").lower() == "true"
SEND_LIMIT = int(os.environ.get("SEND_LIMIT", "0"))  # 0 = no cap
# Orphan coverage is NOT this job's gate (Marketing, 2026-09-02). An orphan is by definition
# someone the engine does not know, so the sender never mails one - the gate measured nothing
# this job does. It moved to where orphans actually receive mail: the weekly akcija campaign,
# which does not go out unless every list-3 address is either in the engine or a known
# non-buyer with a defined state. Here it stays a REPORTED number on every run.
GATE_ON_ORPHANS = os.environ.get("GATE_ON_ORPHANS", "false").lower() == "true"
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "info@tiktik.lv")
SENDER_NAME = os.environ.get("SENDER_NAME", "tiktik.lv")

# --- snapshot refresh -------------------------------------------------------
REFRESH_SNAPSHOT = os.environ.get("REFRESH_SNAPSHOT", "true").lower() != "false"

# --- weekly assignment ------------------------------------------------------
# The sender OWNS this refresh. A table nobody rebuilds goes stale and reads as current,
# which is the failure this whole build exists to remove.
BUILD_ASSIGNMENT = os.environ.get("BUILD_ASSIGNMENT", "true").lower() != "false"
SP_ASSIGNMENT = f"`{PROJECT}.{CONTROL}.sp_build_contact_weekly_assignment`"
SNAPSHOT_GCS_URI = os.environ.get(
    "SNAPSHOT_GCS_URI",
    "gs://jaunais-za-aizv04022026-iso-tools/brevo/contacts_snapshot_latest.ndjson",
)

# --- table names ------------------------------------------------------------
def t(dataset: str, name: str) -> str:
    return f"`{PROJECT}.{dataset}.{name}`"

T_LIFECYCLE = t(MARTS, "customer_lifecycle")
T_TODAY_SENDS = t(MARTS, "today_sends")
T_SUPPRESSION = t(MARTS, "email_suppression_all")
T_SNAPSHOT = t(MARTS, "brevo_contacts_snapshot")
T_IDENTITY = t(MARTS, "customer_identity")
T_MASTER = t(MARTS, "customer_master")
T_SEND_LOG = t(MARTS, "email_send_log")
T_ASSIGNMENT = t(MARTS, "contact_weekly_assignment")
T_SEND_PLAN = t(CONTROL, "send_plan")
T_RUN_REPORT = t(CONTROL, "sender_run_report")

# The three control surfaces the sender must obey. Before 2026-09-04 it read NONE of them,
# which is how 598 rows on nine switched-off tracks, all pointing at templates Brevo had
# deactivated the day before, came out of the plan as SEND.
#   T_TRACK_ENABLED    - Raivis' per-track switch. Nothing sends on a track that is off.
#   T_TEMPLATE_MAP     - the ONE email_type -> template_id mapping. It is a VIEW that already
#                        unions the manual lifecycle table with info_email_catalog, so reading
#                        it does not create a second source; reading the catalog directly did.
#   T_TEMPLATE_STATUS  - the Brevo mirror: is_active + checked_at, 84 templates per run.
T_TRACK_ENABLED = t(CONTROL, "track_enabled")
T_TEMPLATE_MAP = t(CONTROL, "email_template_map")
T_TEMPLATE_STATUS = t(CONTROL, "brevo_template_status")

SNAPSHOT_TABLE_PLAIN = f"{PROJECT}.{MARTS}.brevo_contacts_snapshot"
