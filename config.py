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

# --- frequency rules (identity rule b: per PERSON, not per address) ---------
MAX_EMAILS_PER_WEEK = int(os.environ.get("MAX_EMAILS_PER_WEEK", "2"))
MIN_DAYS_BETWEEN = int(os.environ.get("MIN_DAYS_BETWEEN", "2"))

# --- sending ----------------------------------------------------------------
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
ALLOW_SEND = os.environ.get("ALLOW_SEND", "false").lower() == "true"
SEND_LIMIT = int(os.environ.get("SEND_LIMIT", "0"))  # 0 = no cap
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "info@tiktik.lv")
SENDER_NAME = os.environ.get("SENDER_NAME", "tiktik.lv")

# --- snapshot refresh -------------------------------------------------------
REFRESH_SNAPSHOT = os.environ.get("REFRESH_SNAPSHOT", "true").lower() != "false"
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

SNAPSHOT_TABLE_PLAIN = f"{PROJECT}.{MARTS}.brevo_contacts_snapshot"
