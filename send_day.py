"""SEND DAY - the live send path of the NEW plan, the CAMPAIGN way (Block 1; MAIN 2026-10-09 13:07, Raivis 12:20 + 13:07).

ONE run = one send date. For every letter type the new plan (mkt_control.shadow_send_plan) holds for that date:

  D2  SEND DAY. Commercial types go on TUESDAY, educational types on THURSDAY (send_weekday). A letter that becomes
      due on another day is not sent that day; the planner keeps re-planning it (an overdue letter is planned for the
      plan's own date), so it is in the plan of the next such day. The new plan carries no educational type today.
  D1  STANDING APPROVAL (replaces the daily L2 word and the per-batch press). A type may be sent only when ALL hold at
      send time: (a) its track_enabled row is enabled AND enabled_by = 'Raivis'; (b) template_approval holds an
      approved row by 'Raivis' for exactly (email_type, template_id); (c) the sha256 of the template's htmlContent in
      Brevo, read NOW, equals that row's approved_sha256. Any failure: the type is refused for the day and one KĻŪDA
      row goes to the dash. A changed hash closes the type until a new approval row exists - there is no other way.
  D3  CAP. mkt_control.send_cap (send_date, email_type, cap) - the latest row of that pair. No row = 0 = nothing goes.
      Members are taken OLDEST DUE FIRST (due_since = the first plan date of the last 90 days on which the planner held
      this letter for this person as would_send), and only members that pass every send-time gate are counted. The
      overflow stays planned and comes first on the next send day.

Member gates, evaluated on the data of the moment BEFORE the audience is frozen (a frozen audience is clean or nothing
goes, as send_path L5 / L8): suppression (L5), G15 goods (L7), presend.gates (L8, the planner's own builder),
person re-check (L9: B2B / LEAD / EN / PA1 / PA2 / BOUGHT_SINCE_PLAN) and LETTER_FIELDS_NOT_IN_BREVO (L11, new):
a campaign renders the CONTACT's attributes, so the contact's Brevo attributes must equal its mkt_control.letter_fields
row of the plan date (send_path.letter_params, value for value) - otherwise the person would get another day's prices.

Then, per type, LIVE only: freeze the audience (mkt_control.send_day_audience) -> a new Brevo list holding exactly those
addresses (read back, counter settled) -> a campaign from the approved template's htmlContent (UTM week applied, the
suppression list excluded) -> its content read back (no ⟦…⟧) -> send_path.dispatch (L1, L6, L10, L3/L4 = D1, L5, L7,
L8, L9 again) -> Brevo sendNow -> one send_log row per recipient (source 'engine_live'), one Pipedrive activity
(pd_record, the SAME record the shadow plan renders) and one sequence state advance (mkt_control.send_state_advance +
contact_sequence_state last_sent_on / last_email_type / step).

DRY (DRY_RUN=true or ALLOW_SEND!=true or GLOBAL_SEND_ENABLED!=false... i.e. L1 closed): everything up to the freeze is
computed and recorded (send_day_run + send_day_audience with mode 'dry'); NOTHING is written to Brevo or Pipedrive
(Brevo is only READ: template HTML for the hash, contact attributes for L11).

PROOF (MAIN 2026-10-09 14:24, decision 2; `--proof=<email_type> --pd-person=<id>`): one mail of one type to
the PROOF_RECIPIENT address ONLY. The content is the letter_fields row of ONE real member of that type's plan of today who passes
every member gate (L5 L7 L8 L9) - copied onto that address' Brevo attributes (its previous values are recorded in
mkt_control.send_day_proof and put back by `--proof-restore`). The list holds that one address and nothing else
(checked before the campaign is created). send_log and send_state_advance carry master_key 'TEST:<PROOF_RECIPIENT>',
so no customer's sequence moves; the Pipedrive activity goes to Raivis' own person. D1 (b)+(c) (approval row by Raivis
and the LIVE Brevo hash) are enforced; D1 (a) track, D2 day and D3 cap are not (no track is enabled - that is
Raivis' act); L1 is opened by the EXECUTION's env only. The run waits until Brevo reports the campaign 'sent'.
MIRROR (decision 3): a LIVE type with a cap whose members are held for PAID_ORDERS_SOURCE_MISSING posts a KĻŪDA row.
D1 KĻŪDA dash row: posted for a type that has a cap > 0 (meant to go today) and fails D1; a type without a cap is off on
purpose (D3) and posts nothing. A whole-day refusal (no plan, a closed send_path lock) always posts.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
import time
import uuid
import zoneinfo

import block_gate as BG
import day_guard as DG
import pd_record
import presend as G
import s5_card_gate as S5
import send_path as SP
import sequence as S

P = "jaunais-za-aizv04022026"
M = f"{P}.mkt_control"
T_PLAN, T_LF = f"{M}.shadow_send_plan", f"{M}.letter_fields"
T_CAP, T_AUD, T_RUN = f"{M}.send_cap", f"{M}.send_day_audience", f"{M}.send_day_run"
T_TRACK, T_APPROVAL = f"{M}.track_enabled", f"{M}.template_approval"
T_LOG, T_ADV, T_STATE = f"{M}.send_log", f"{M}.send_state_advance", f"{M}.contact_sequence_state"
T_PDW, T_SUPP = f"{M}.shadow_pd_writes", f"{P}.business_marts.email_suppression_all"
T_OFFERED = f"{M}.xsell_offered"     # R7 PP3: one row per (recipient, R product) of every sent 235
RIGA = zoneinfo.ZoneInfo("Europe/Riga")
APPROVER = "Raivis"
INTERNAL_NOTICE_TO = {"raivis@alenda.lv"}      # D47/D48 notices may go while L1 is closed - never to a client
TUESDAY, THURSDAY = 1, 3                     # date.weekday(): Monday = 0
EDUCATIONAL_TYPES = frozenset()              # the new plan holds no educational letter today (Thursday = block 2)
SUPPRESSION_LIST_ID = 4
LIST_FOLDER_ID = 1
ADD_CHUNK = 150
BQ_CHUNK = 500                               # R6: rows per batched INSERT / keys per UPDATE (query size < 1 MB)
PD_WORKERS_DEFAULT = 4                       # R6: parallel Pipedrive activity writes (env PD_WORKERS overrides)
DUE_LOOKBACK_DAYS = 90
# THE PROOF RECIPIENT is ONE configured value: env PROOF_RECIPIENT on the Cloud Run job tiktik-send-day (MAIN
# 2026-10-09 18:49: alenda.jurmala@gmail.com). Unset or not one address -> every proof path refuses. A proof's rows
# are written under 'TEST:<recipient>' only - never under the recipient's own master_key.
LEGACY_PROOF_RECIPIENT = "raivis@alenda.lv"      # proof rows written before the column existed (09.10 only)
_ONE_ADDRESS = __import__("re").compile(r"[^@\s,;]+@[^@\s,;]+\.[a-z]{2,}")


# MAIN 3 13.10 (h): a template (235) also renders contact fields the 06:00 sync owns, not letter_fields. A proof copies
# them from the SOURCE member's Brevo attributes too ('' where the member has none), so the test address shows exactly
# what the member would see; proof_restore puts them back like every other written key.
PROOF_SYNC_KEYS = tuple(f"D{i}_{s}" for i in range(1, 5) for s in ("NAME", "URL")) + ("UZRUNA", "VARDS")


def proof_member_fields(src_attrs) -> dict:
    """The sync-owned fields of the source member, '' where it has none (clears a stale value on the test address)."""
    a = src_attrs or {}
    return {k: ("" if a.get(k) is None else a.get(k)) for k in PROOF_SYNC_KEYS}


def proof_recipient(env) -> str:
    r = (env.get("PROOF_RECIPIENT") or "").strip().lower()
    if not _ONE_ADDRESS.fullmatch(r):
        raise SP.SendLocked([("PROOF", "PROOF_RECIPIENT is not set to exactly one address on this job")])
    return r


def test_key(recipient) -> str:
    return "TEST:" + recipient
T_PROOF = f"{M}.send_day_proof"
# MAIN 11.10 12:55 LOGGING: every letter the plan holds for the send date, with what happened and why - one row per
# (run, person, letter): SENT / DRY_WOULD_SEND / HELD_IN_PLAN / NOT_DUE_YET / TYPE_REFUSED / MEMBER_REFUSED / OVER_CAP.
# With send_log (what went, now with the plan's reason) one query per person answers what, when, why and what not.
T_OUTCOME = f"{M}.send_outcome"

DDL = [f"""CREATE TABLE IF NOT EXISTS `{T_CAP}` (send_date DATE, email_type STRING, cap INT64, set_by STRING,
  set_at TIMESTAMP, note STRING)""",
       f"""CREATE TABLE IF NOT EXISTS `{T_AUD}` (send_date DATE, run_id STRING, batch_id STRING, mode STRING,
  email_type STRING, template_id INT64, track STRING, rung INT64, master_key STRING, email STRING, person_id INT64,
  due_since DATE, rank INT64, decision STRING, created_at TIMESTAMP)""",
       f"""CREATE TABLE IF NOT EXISTS `{T_RUN}` (run_id STRING, send_date DATE, mode STRING, email_type STRING,
  template_id INT64, track STRING, cap INT64, candidates INT64, chosen INT64, refused_json STRING,
  type_refusal STRING, batch_id STRING, list_id INT64, campaign_id INT64, sent INT64, pd_writes INT64,
  detail STRING, finished_at TIMESTAMP)""",
       f"""CREATE TABLE IF NOT EXISTS `{T_ADV}` (master_key STRING, email_type STRING, sent_on DATE, rung INT64,
  batch_id STRING, campaign_id INT64, sent_at TIMESTAMP)""",
       f"""CREATE TABLE IF NOT EXISTS `{T_PROOF}` (run_id STRING, email_type STRING, source_master_key STRING,
  source_email STRING, raivis_before STRING, written STRING, campaign_id INT64, list_id INT64, created_at TIMESTAMP,
  restored_at TIMESTAMP)""",
       f"ALTER TABLE `{T_PROOF}` ADD COLUMN IF NOT EXISTS recipient STRING",
       f"ALTER TABLE `{T_LOG}` ADD COLUMN IF NOT EXISTS reason STRING",
       f"""CREATE TABLE IF NOT EXISTS `{T_OUTCOME}` (send_date DATE, run_id STRING, mode STRING, plan_run STRING,
  master_key STRING, email STRING, email_type STRING, template_id INT64, planned_send_date DATE, outcome STRING,
  reason STRING, plan_reason STRING, created_at TIMESTAMP)"""]


# RAIVIS D50 (MAIN 2 19:50): 244 is not sent on a Tuesday any more (it moves to the delivery pipeline) - its contacts
# get 236; active_xsell only when the last order is at most 30 days before the send day (else -> 236).
D50_FEEDBACK, D50_XSELL, D50_XSELL_MAX_DAYS = "post_purchase_feedback", "active_xsell", 30
# RAIVIS D49: campaigns 10 min apart, smallest first, each after the previous one is 'sent'; 236 last.
D49_GAP_MIN, D49_FINISH_MAX_MIN = 10, 40
# RAIVIS D52 (MAIN 2 13.10 08:10): ONE-TIME override of the D44 chain for 13.10 only - the Monday-prepared data goes
# (block pushes of 12.10, cab-20261012, ak_maker_html of W42); every other gate stays; per-contact price holds come
# from mkt_control.d52_price_holds (measured against the current Mozello mirror); 236 may be held by the caller.
D52_ALLOWED_DATES = {"2026-10-13"}
D52_REASON = "D52_RAIVIS_OVERRIDE"
D52_HOLDS = f"{M}.d52_price_holds"


def d50_xsell_refusal(c, send_date) -> str | None:
    import re
    m = re.search(r"last_order=(\d{4}-\d{2}-\d{2})", str(c.get("reason") or ""))
    if not m:
        return "D50_XSELL_ORDER_OLD no last order known"
    lo = dt.date.fromisoformat(m.group(1))
    days = (send_date - lo).days
    return None if days <= D50_XSELL_MAX_DAYS else f"D50_XSELL_ORDER_OLD last order {lo} ({days} d)"


def report_mail(result, kind) -> tuple:
    """Latvian, short. kind 'check' = 08:05 Rīta pārbaude (GO / HOLD per campaign), 'sent' = Izsūtīts."""
    d = result.get("send_date")
    lines, hold = [], False
    for t in result.get("types", []):
        why = t.get("type_refusal")
        if kind == "check":
            dry_only = bool(why) and why.startswith("D1 TRACK") and result.get("mode") == "dry"
            go = (not why or dry_only) and t.get("chosen", 0) > 0
            hold = hold or not go
            lines.append(f"{t['email_type']}: {'GO' if go else 'HOLD'} - izvēlēti {t.get('chosen', 0)} no "
                         f"{t.get('candidates', 0)}" + (f"; iemesls: {why}" if why and not dry_only else "")
                         + (f"; aizturēti: {t.get('refused')}" if t.get('refused') else ""))
        else:
            lines.append(f"{t['email_type']}: nosūtīti {t.get('sent', 0)}, kampaņa {t.get('campaign_id')}"
                         + (f"; nesūtīts: {why}" if why else "") + (f"; aizturēti: {t.get('refused')}" if t.get('refused') else ""))
    a = result.get("akcija_236") or {}
    a_hold = a.get("holds") or ([a["error"]] if a.get("error") else [])
    if kind == "check":
        hold = hold or bool(a_hold)
        lines.append(f"236: {'HOLD - ' + '; '.join(a_hold) if a_hold else 'GO'} - auditorija {(a.get('audience') or {}).get('final')}")
    else:
        lines.append(f"236: {'palaists, kampaņa ' + str(a.get('campaign_id')) if a.get('launched') else 'NAV palaists - ' + '; '.join(a_hold)}")
    g = result.get("day_guard") or {}
    lines.append(f"2+ vēstules vienam kontaktam: {g.get('d47_double', '?')} (jābūt 0); nepaskaidrotas izmaiņas: "
                 f"{g.get('d47_unexplained', '?')}; kabineta saites neatbilst: {g.get('cab_mismatch', 0)}, nav saites: "
                 f"{g.get('cab_no_link', 0)}")
    if (result.get("d44") or {}).get("commercial"):
        lines.append("D44: " + "; ".join(result["d44"]["commercial"]))
    title = (f"Rīta pārbaude {d}: {'IR AIZTURĒTAIS' if hold else 'viss GO'}" if kind == "check"
             else f"Izsūtīts {d}")
    return title, "\n".join(lines)


def check_problems(result) -> list:
    """MAIN 3 13.10 (b): the 08:05 Rīta pārbaude mails Raivis ONLY when something is NOT OK; all OK = silent (logged).
    Pure. [] = all OK. NOT OK = the run was refused / found no plan / evaluated no letter type, any campaign is HOLD
    (report_mail's 'IR AIZTURĒTAIS'), 236 was not evaluated on a full Tuesday run, the day guard is missing, or the day
    guard shows a double (2+ letters to one contact) or a cabinet link pointing to another token. Per-contact holds
    (aizturēti / d47_unexplained / cab_no_link) inside a GO campaign are NOT a reason on their own - they stay in the log."""
    out = []
    if result.get("refused"):
        out.append(f"atteikts: {result['refused']}")
    if not result.get("types"):
        out.append("neviens vēstules tips nav novērtēts")
    subj, _ = report_mail(result, "check")
    if "IR AIZTURĒTAIS" in subj:
        out.append("ir aizturēta kampaņa")
    if "akcija_236" not in result and not result.get("only"):
        out.append("236 nav novērtēts")
    g = result.get("day_guard")
    if not isinstance(g, dict) or "d47_double" not in g:
        out.append("dienas sargs (D47) nav izpildīts")
    else:
        if int(g.get("d47_double") or 0) > 0:
            out.append(f"2+ vēstules vienam kontaktam: {g['d47_double']}")
        if int(g.get("cab_mismatch") or 0) > 0:
            out.append(f"kabineta saites neatbilst: {g['cab_mismatch']}")
    return out


def check_notify(day, res) -> dict:
    """08:05: mail only when NOT OK; always one CHECK_RESULT line in the log (silent-when-OK is still logged)."""
    subj, text = report_mail(res, "check")
    probs = check_problems(res)
    if probs:
        subj = subj.replace("viss GO", "NAV OK")
        text = "Kas nav kārtībā: " + "; ".join(probs) + "\n" + text
        mailed = day.notify(subj, text)
        day.dash("KĻŪDA", f"{subj}: " + text.replace("\n", " | ")[:350])
    else:
        mailed = False
    print("CHECK_RESULT " + json.dumps({"ok": not probs, "problems": probs, "mailed": bool(mailed), "subject": subj,
                                        "text": text}, ensure_ascii=False)[:8000], flush=True)
    return {"notice": subj, "check_ok": not probs, "check_problems": probs, "check_mailed": bool(mailed)}


def check_failed_notify(day, d, why) -> bool:
    """08:05 run that did not finish (refused / crashed): silence would read as OK -> always mail."""
    subj = f"Rīta pārbaude {d}: NAV OK - pārbaude nepabeidzās"
    text = f"Rīta pārbaude nepabeidzās, nekas nav sūtīts.\nIemesls: {str(why)[:600]}"
    mailed = day.notify(subj, text) if day is not None else False
    print("CHECK_RESULT " + json.dumps({"ok": False, "problems": ["pārbaude nepabeidzās"], "mailed": bool(mailed),
                                        "subject": subj, "text": text}, ensure_ascii=False), flush=True)
    return bool(mailed)


def outcome_rows(plan_rows, results, send_date, live) -> list:
    """Pure. plan_rows = the plan run's rows that carry a letter; results = run()['types'] with 'decisions'.
    -> [(master_key, email, email_type, template_id, planned_send_date, outcome, reason, plan_reason)]."""
    by_type = {r["email_type"]: r for r in results}
    out = []
    for p in plan_rows:
        et, mk = p["email_type"], p["master_key"]
        base = (mk, p.get("email"), et, p.get("template_id"), p.get("planned_send_date"))
        if not _b(p.get("would_send")):
            out.append(base + ("HELD_IN_PLAN", p.get("hold_reason"), p.get("reason")))
            continue
        if str(p.get("planned_send_date") or "") > str(send_date):
            out.append(base + ("NOT_DUE_YET", "planned " + str(p.get("planned_send_date")), p.get("reason")))
            continue
        t = by_type.get(et)
        if t is None:
            out.append(base + ("NOT_EVALUATED", "type not run", p.get("reason")))
            continue
        dec = (t.get("decisions") or {}).get(mk)
        if dec == "CHOSEN" and not t.get("type_refusal"):
            out.append(base + ("SENT" if live and t.get("sent") else "DRY_WOULD_SEND", None, p.get("reason")))
        elif dec == "CHOSEN":
            out.append(base + ("TYPE_REFUSED", t["type_refusal"], p.get("reason")))
        elif dec == "OVER_CAP":
            out.append(base + ("OVER_CAP", f"cap {t.get('cap')}", p.get("reason")))
        elif dec:
            out.append(base + ("MEMBER_REFUSED", dec, p.get("reason")))
        else:
            out.append(base + ("TYPE_REFUSED", t.get("type_refusal") or "not evaluated", p.get("reason")))
    return out


# ------------------------------------------------------------------------------------------------ pure rules
def send_weekday(email_type) -> int:
    """D2: Tuesday for commercial types, Thursday for educational types."""
    return THURSDAY if email_type in EDUCATIONAL_TYPES else TUESDAY


def is_send_day(email_type, d: dt.date) -> bool:
    return d.weekday() == send_weekday(email_type)


def cap_of(cap_rows, email_type, d) -> int:
    """D3: the latest send_cap row of (d, email_type); no row = 0."""
    rows = [r for r in cap_rows if str(r["send_date"]) == str(d) and r["email_type"] == email_type]
    if not rows:
        return 0
    return max(0, int(max(rows, key=lambda r: str(r.get("set_at") or ""))["cap"] or 0))


FUSE_FACTOR = 1.05        # MAIN 2 12.10 15:40 (MAIN 11.10 12:45 item 3): send_cap fuse = that day's plan + 5 %


def fuse_cap(planned: int) -> int:
    """The D3 fuse for an enabled type: ceil(plan x 1.05) - a guard against a runaway, never a throttle (Raivis
    12.10 14:05: no volume spreading). 469 -> 493."""
    import math
    return int(math.ceil(round(planned * FUSE_FACTOR, 6)))


def track_enabled(track, track_rows) -> bool:
    return any(r.get("track") == track and r.get("enabled") and r.get("enabled_by") == "Raivis" for r in track_rows)


def standing_approval(email_type, track, template_id, track_rows, approval_rows, live_hash) -> list:
    """D1 -> [] when the type may go, else the reasons (all of them). live_hash(template_id) -> hex | raises."""
    out = []
    tr = [r for r in track_rows if r.get("track") == track]
    if not tr or not all(_b(r.get("enabled")) for r in tr) or not all(r.get("enabled_by") == APPROVER for r in tr):
        out.append(f"TRACK_NOT_ENABLED_BY_RAIVIS track={track}")
    ap = [r for r in approval_rows if r.get("email_type") == email_type and _i(r.get("template_id")) == _i(template_id)
          and _b(r.get("approved")) and r.get("approved_by") == APPROVER]
    shas = {(r.get("approved_sha256") or "").strip().lower() for r in ap}
    if not ap:
        out.append(f"NO_APPROVAL_ROW_BY_RAIVIS {email_type}/{template_id}")
    elif len(shas) != 1 or "" in shas:
        out.append(f"APPROVAL_HASH_MISSING_OR_AMBIGUOUS {email_type}/{template_id}")
    else:
        try:
            h = (live_hash(template_id) or "").lower()
        except Exception as e:  # noqa: BLE001 - an unread template is not the approved one
            h = f"<unreadable {type(e).__name__}>"
        if h != next(iter(shas)):
            out.append(f"TEMPLATE_HASH_CHANGED {template_id}: brevo {h[:12]} != approved {next(iter(shas))[:12]}")
    return out


def select_audience(candidates, cap, member_refusal):
    """D3 + the frozen-clean rule. candidates are already ordered oldest due first. member_refusal(c) -> reason | None,
    called only until the cap is reached (it may read Brevo). -> (chosen, decisions{master_key: decision})."""
    chosen, decisions = [], {}
    for c in candidates:
        if len(chosen) >= cap:
            decisions[c["master_key"]] = "OVER_CAP"
            continue
        why = member_refusal(c)
        if why:
            decisions[c["master_key"]] = why
        else:
            chosen.append(c)
            decisions[c["master_key"]] = "CHOSEN"
    return chosen, decisions


def attrs_equal(letter_row, contact_attrs) -> list:
    """L11: the UPPERCASE letter fields that differ from the contact's Brevo attributes (names only)."""
    want, have, bad = SP.letter_params(letter_row or {}), contact_attrs or {}, []
    for k, v in sorted(want.items()):
        if _norm(v) != _norm(have.get(k)):
            bad.append(k)
    return bad


def _norm(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return {"True": "true", "False": "false"}.get(s, s)


def _b(v) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


def _i(v):
    return None if v in (None, "") else int(v)


def l1_open(env) -> bool:
    """L1 as the job's env says it: DRY_RUN=false AND ALLOW_SEND=true AND GLOBAL_SEND_ENABLED=true."""
    return (env.get("DRY_RUN", "true").lower() == "false" and env.get("ALLOW_SEND", "false").lower() == "true"
            and env.get("GLOBAL_SEND_ENABLED", "false").lower() == "true")


# ------------------------------------------------------------------------------------------------ SQL
def candidates_sql(plan_run, send_date, email_type):
    return f"""
WITH p AS (SELECT * FROM `{T_PLAN}` WHERE run_id = '{plan_run}' AND would_send AND email_type = '{email_type}'
             AND planned_send_date <= DATE '{send_date}'),
since AS (SELECT master_key, MIN(planned_send_date) AS due_since FROM `{T_PLAN}`
          WHERE email_type = '{email_type}' AND would_send
            AND plan_date >= DATE_SUB(DATE '{send_date}', INTERVAL {DUE_LOOKBACK_DAYS} DAY)
          GROUP BY master_key),
pd AS (SELECT master_key, ANY_VALUE(target_person_id) AS person_id FROM `{T_PDW}`
       WHERE run_id = '{plan_run}' AND email_type = '{email_type}' AND target_person_id IS NOT NULL GROUP BY 1)
SELECT p.master_key, LOWER(TRIM(p.email)) AS email, p.email_type, p.template_id, p.track, IFNULL(p.offer_rung, 0) AS rung,
  CAST(p.planned_send_date AS STRING) AS planned_send_date, CAST(IFNULL(s.due_since, p.planned_send_date) AS STRING)
  AS due_since, pd.person_id, p.reason, p.offer_valid_until
FROM p LEFT JOIN since s USING (master_key) LEFT JOIN pd USING (master_key)
ORDER BY due_since, planned_send_date, master_key"""


# ------------------------------------------------------------------------------------------------ the run
class Day:
    """IO is injected: q(sql) -> rows; wh = send_lookups.Warehouse; brevo(method, path, payload) -> dict;
    pd_post(record) -> dict; dash(status, text) -> bool; env = os.environ-like."""

    def __init__(self, q, wh, brevo, pd_post, dash, env, now=None):
        self.q, self.wh, self.brevo, self.pd_post, self.dash, self.env = q, wh, brevo, pd_post, dash, env
        self.now = now or dt.datetime.now(dt.timezone.utc)
        self.clock = (lambda: dt.datetime.now(dt.timezone.utc)) if now is None else (lambda: self.now)
        self.sleep = time.sleep
        self.run_id = f"sd-{self.now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
        self.pd_ids = []

    def _pd(self, record):
        r = self.pd_post(record)
        self.pd_ids.append(r)
        return r

    # -- reads
    def live_hash(self, template_id):
        html = (self.brevo("GET", f"/smtp/templates/{int(template_id)}", None) or {}).get("htmlContent") or ""
        return hashlib.sha256(html.encode("utf-8")).hexdigest()

    def template_html(self, template_id):
        """The template's htmlContent read from Brevo NOW (D31: which block fields it reads). Cached per run."""
        c = self.__dict__.setdefault("_html", {})
        if template_id not in c:
            c[template_id] = (self.brevo("GET", f"/smtp/templates/{int(template_id)}", None) or {}).get("htmlContent") or ""
        return c[template_id]

    def contact_attrs(self, email):
        try:
            return (self.brevo("GET", f"/contacts/{email}", None) or {}).get("attributes") or {}
        except Exception:  # noqa: BLE001 - an unread contact is not a matching one
            return None

    def plan_run_of(self, d):
        r = self.q(f"SELECT run_id, plan_date FROM `{T_PLAN}` WHERE plan_date <= DATE '{d}' "
                   f"GROUP BY run_id, plan_date ORDER BY plan_date DESC, MAX(planned_at) DESC LIMIT 1")
        return (r[0]["run_id"], str(r[0]["plan_date"])) if r else (None, None)

    def types_of(self, plan_run, d):
        return [dict(r) for r in self.q(
            f"SELECT email_type, ANY_VALUE(template_id) AS template_id, ANY_VALUE(track) AS track, COUNT(*) AS n "
            f"FROM `{T_PLAN}` WHERE run_id = '{plan_run}' AND would_send AND email_type IS NOT NULL "
            f"AND planned_send_date <= DATE '{d}' GROUP BY 1 ORDER BY 1")]

    def suppressed(self, emails):
        if not emails:
            return set()
        lst = ",".join("'" + e.replace("'", "") + "'" for e in emails)
        return {r["e"] for r in self.q(f"SELECT DISTINCT LOWER(TRIM(email)) AS e FROM `{T_SUPP}` "
                                       f"WHERE LOWER(TRIM(email)) IN ({lst})")}

    # -- member gates (L5 L7 L8 L9 L11), on the data date (= the plan's date)
    def member_refusal_fn(self, et, rung_of, data_date, cands, l11=True, blocks_needed=(), product=None):
        mks = [c["master_key"] for c in cands]
        supp = self.suppressed([c["email"] for c in cands])
        ctx = self.wh.presend_ctx({"email_type": et}, data_date, mks)
        blocks = self.wh.person_blocks(data_date, mks)
        lf = self.wh.lf_rows(data_date)
        full = self.full_lf_rows(data_date, [c["email"] for c in cands])

        def refuse(c):
            mk = c["master_key"]
            if c["email"] in supp:
                return "SUPPRESSED"
            if str(c["planned_send_date"]) != str(data_date):
                return "NOT_IN_TODAYS_DATA (planned " + str(c["planned_send_date"]) + ")"
            row = lf.get(c["email"])
            gh = S.goods_hold(et, rung_of(c), None, G.row_goods(row if row and row.get("email_type") == et else None))
            if gh:
                return "L7_" + gh
            g = G.gates(et, rung_of(c), ctx.get(mk) or G.Ctx())[:1]
            if g:
                return "L8_" + g[0]
            if blocks.get(mk):
                return "L9_" + blocks[mk]
            frow = full.get(c["email"])
            if not frow:
                return "L11_NO_LETTER_FIELDS_ROW"
            if not l11 and not blocks_needed:
                return None
            attrs = self.contact_attrs(c["email"])
            if attrs is None:
                return "L11_CONTACT_UNREADABLE"
            empty = (BG.all_empty(attrs, product) if product is not None      # v1.3: all product fields ""
                     else BG.empty_blocks(attrs, blocks_needed))               # D31: an empty block = no letter
            if empty:
                return "block_empty " + ",".join(empty)
            if not l11:
                return None
            diff = attrs_equal(frow, attrs)
            return ("L11_LETTER_FIELDS_NOT_IN_BREVO " + ",".join(diff[:5])) if diff else None
        return refuse

    def full_lf_rows(self, d, emails):
        if not emails:
            return {}
        lst = ",".join("'" + e.replace("'", "") + "'" for e in emails)
        rows = self.q(f"SELECT * FROM `{T_LF}` WHERE plan_date = DATE '{d}' AND LOWER(TRIM(email)) IN ({lst}) "
                      f"QUALIFY ROW_NUMBER() OVER (PARTITION BY LOWER(TRIM(email)) ORDER BY built_at DESC) = 1")
        return {str(r["email"]).strip().lower(): dict(r) for r in rows}

    # -- the day
    def run(self, send_date: dt.date, mode="auto", caps_override=None, only=None, gate_date=None) -> dict:
        live = mode != "dry" and l1_open(self.env)
        if caps_override is not None and live:
            raise SP.SendLocked([("D3", "a caps override exists only for a DRY rehearsal")])
        d52 = getattr(self, "d52", None)
        if d52 and str(send_date) not in D52_ALLOWED_DATES:
            raise SP.SendLocked([("D52", f"the Raivis override exists only for {sorted(D52_ALLOWED_DATES)}")])
        if gate_date is not None and live and not d52:
            raise SP.SendLocked([("D31", "a forced block-gate date exists only for a DRY rehearsal")])
        self.gate_date = gate_date          # MAIN 19:05: DRY test of the block gates against another day's push rows
        for ddl in DDL:
            self.q(ddl)
        plan_run, plan_date = self.plan_run_of(send_date)
        result = {"run_id": self.run_id, "send_date": str(send_date), "mode": "live" if live else "dry",
                  "plan_run": plan_run, "plan_date": plan_date, "types": []}
        if plan_run is None:
            result["refused"] = "NO_PLAN"
            return result
        cap_rows = [dict(r) for r in self.q(f"SELECT * FROM `{T_CAP}` WHERE send_date = DATE '{send_date}'")]
        track_rows = [dict(r) for r in self.q(f"SELECT track, enabled, enabled_by FROM `{T_TRACK}`")]
        appr_rows = [dict(r) for r in self.q(f"SELECT template_id, email_type, approved, approved_by, approved_sha256 "
                                             f"FROM `{T_APPROVAL}`")]
        types = self.types_of(plan_run, send_date)
        # MAIN 18:50: letters without blocks never wait - they go first; block letters may wait for the push row
        types = sorted(types, key=lambda t: bool(_i(t["template_id"]) and BG.fields_in(self.template_html(_i(t["template_id"])))))
        # D47 / D46 cab link: the whole day is looked at once, before any type goes
        self._cands = {t["email_type"]: [dict(r) for r in self.q(candidates_sql(plan_run, send_date, t["email_type"]))]
                       for t in types if (not only or t["email_type"] in only) and self.send_day_ok(t["email_type"], send_date)}
        fd = getattr(self, "d47_force_double", None)
        if fd and not live:                                # DRY proof: copy one contact into a second type
            src = next((c for rows in self._cands.values() for c in rows if c["master_key"] == fd), None)
            other = next((et for et, rows in self._cands.items() if src and all(c["master_key"] != fd for c in rows)), None)
            if src and other:
                self._cands[other] = self._cands[other] + [dict(src)]
                result["d47_forced_double"] = {"master_key": fd, "added_to": other}
        result["day_guard"] = self.day_guard(send_date, live)
        result["d44"] = self.d44(send_date, live)
        if getattr(self, "chain_dry", False):          # R1 Mon DRY: the chain's own S1-S4 rows stand in for D44
            result["d44"]["chain_dry"] = self._d44 = self.chain_d44(send_date)
        result["s5"] = self.s5_gate(send_date)
        if d52:                                    # RAIVIS D52 (13.10 08:10): send with the Monday-prepared data, once
            result["d44"]["override"] = D52_REASON
            self._d44 = {"commercial": [], "akcija_236": list(d52.get("hold_236") or [])}
            self._d52_holds = {r["master_key"]: f"D52_PRICE_CHECK {r['why']}" for r in
                               self.q(f"SELECT master_key, why FROM `{D52_HOLDS}` WHERE send_date = DATE '{send_date}'")}
            result["d52"] = {"reason": D52_REASON, "gate_date": str(gate_date), "price_holds": len(self._d52_holds),
                             "hold_236": d52.get("hold_236")}
        elif getattr(self, "d44_simulate_pass", False) and not live:        # DRY only: "what if the chain were in order"
            result["d44"]["simulated_pass"] = True
            self._d44 = {"commercial": [], "akcija_236": []}
        self._d50 = {c["master_key"]: why for c in self._cands.get(D50_XSELL, [])
                     for why in [d50_xsell_refusal(c, send_date)] if why}
        result["d50"] = {"feedback_moved": len(self._cands.get(D50_FEEDBACK, [])), "xsell_order_old": len(self._d50)}
        # D49 (Raivis): personal campaigns go smallest first, 10 min apart, each after the previous one finished
        types = sorted(types, key=lambda t: len(self._cands.get(t["email_type"], [])))
        result["d49_order"] = [(t["email_type"], len(self._cands.get(t["email_type"], []))) for t in types]
        self._track_rows = track_rows
        for t in types:
            et = t["email_type"]
            if only and et not in only:
                continue
            res = self._one(t, send_date, plan_date, plan_run, live, cap_rows, track_rows, appr_rows, caps_override)
            result["types"].append(res)
        result["outcome"] = self.write_outcome(plan_run, send_date, live, result["types"], only)
        result["akcija_excl"] = self.refill_akcija_excl(send_date, live)
        if send_date.weekday() == TUESDAY and not only:          # D48: the engine launches 236 itself, after 98
            result["akcija_236"] = self.launch_236(send_date, live, result["akcija_excl"])
        for t in result["types"]:
            t.pop("decisions", None)
        if live:
            self.tech_log(*report_mail(result, "sent"))
        return result

    @staticmethod
    def tech_log(subject, text):
        """R17 (MAIN 3 13.10): a per-run summary goes to the tech log (Cloud Logging) only - never to Raivis' inbox.
        On 13.10 12:22 the qzglq summary ("winback_1 nosūtīti 2550 ... 236 NAV palaists") was read as a second send.
        Raivis gets ONE Tuesday summary: the after-send check (post_send_check), WhatsApp line + the same e-mail."""
        print("RUN_SUMMARY " + json.dumps({"subject": subject, "text": text}, ensure_ascii=False)[:8000], flush=True)

    # -- D49 pacing (LIVE): 10 min between campaigns, each only after the previous one is 'sent' in Brevo
    def pace(self):
        last = getattr(self, "_last_campaign", None)
        if not last:
            return
        cid, started = last
        deadline = self.clock() + dt.timedelta(minutes=D49_FINISH_MAX_MIN)
        while True:
            st = (self.brevo("GET", f"/emailCampaigns/{int(cid)}", None) or {}).get("status")
            if st == "sent":
                break
            if self.clock() > deadline:
                raise SP.SendLocked([("D49", f"campaign {cid} not finished (status {st}) - the next one does not start")])
            self.sleep(30)
        wait = (started + dt.timedelta(minutes=D49_GAP_MIN) - self.clock()).total_seconds()
        if wait > 0:
            self.sleep(wait)

    def notify(self, subject, text):
        """Internal notice to raivis@alenda.lv (the only allowed non-client mail while flags are DRY)."""
        try:
            self.brevo("POST", "/smtp/email", {"sender": {"id": 2}, "to": [{"email": DG.ALERT_TO}],
                                               "subject": subject, "textContent": text, "tags": ["send-day-report"]})
            return True
        except Exception as e:  # noqa: BLE001 - a notice never stops the day
            print(f"NOTIFY_FAILED {type(e).__name__}: {str(e)[:200]}")
            return False

    def day_guard(self, d, live) -> dict:
        """D47_DOUBLE + D47_UNEXPLAINED_CHANGE (held in every type, one alert mail) and D46_CAB_LINK_MISMATCH
        (priced letters only). Sets self._guard / self._cab for _one. Never picks one letter of a double."""
        cands = self._cands
        double = DG.doubles(cands)
        mon = [dict(r) for r in self.q(DG.monday_sql(d))]
        out = {"monday_run": mon[0]["run_id"] if mon else None, "monday_rows": len(mon)}
        if mon:
            since = {r["k"] for r in self.q(DG.ordered_since_sql(str(mon[0]["planned_at"])))}
            unexpl = DG.unexplained(cands, {(r["master_key"], r["email_type"]) for r in mon}, since)
            today = {(c["master_key"], et) for et, rows in cands.items() for c in rows}
            out["monday_not_today"] = len({(r["master_key"], r["email_type"]) for r in mon} - today)
            out["ordered_since_monday_plan"] = len(since)
        else:
            unexpl = {c["master_key"]: "no Monday plan to compare with" for rows in cands.values() for c in rows}
        self._guard = {mk: f"{DG.DOUBLE} {','.join(ts)}" for mk, ts in double.items()}
        # a second run on the same day (D52 13.10): whoever already got a letter today gets nothing more
        sent = {(r["master_key"], (r["email"] or "").lower()) for r in self.q(
            f"SELECT master_key, LOWER(TRIM(email)) AS email FROM `{T_LOG}` WHERE DATE(sent_at, 'Europe/Riga') = DATE '{d}' "
            f"AND source != 'proof'")} if live or getattr(self, "d52", None) else set()
        sent_mk, sent_em = {m for m, _ in sent}, {e for _, e in sent}
        for rows in cands.values():
            for c in rows:
                if c["master_key"] in sent_mk or (c.get("email") or "").lower() in sent_em:
                    self._guard[c["master_key"]] = "ALREADY_SENT_TODAY"
        out["already_sent_today"] = len(sent)
        for mk, why in unexpl.items():
            self._guard.setdefault(mk, f"{DG.UNEXPLAINED} {why}")
        out.update({"d47_double": len(double), "d47_unexplained": len(unexpl)})
        priced = [c for et, rows in cands.items() if et in DG.PRICED_TYPES for c in rows]
        self._cab = {}
        if priced:
            toks = {r["email"]: r["token"] for r in self.q(DG.cab_tokens_sql(d)) if int(r.get("n") or 1) == 1}
            urls = DG.brevo_attr(self.brevo)
            self._cab = DG.cab_check(priced, toks, urls)
            out.update({"cab_checked": len({c["master_key"] for c in priced}),
                        "cab_mismatch": sum("another token" in v for v in self._cab.values()),
                        "cab_no_link": sum("another token" not in v for v in self._cab.values()),
                        "cab_reasons": dict(sorted(((v.split(" ", 1)[1].split(" for ")[0], 0) for v in self._cab.values())))})
            for v in self._cab.values():
                k = v.split(" ", 1)[1].split(" for ")[0]
                out["cab_reasons"][k] = out["cab_reasons"].get(k, 0) + 1
        if double or unexpl:
            em = {c["master_key"]: c.get("email") for rows in cands.values() for c in rows}
            subj, text = DG.alert_text(d, double, unexpl, em)
            out["alert"] = {"to": DG.ALERT_TO, "subject": subj, "lines": text.count("\n") - 1, "sent": False,
                            "text": text[:3000]}
            if live:
                self.brevo("POST", "/smtp/email", {"sender": {"id": 2}, "to": [{"email": DG.ALERT_TO}],
                                                   "subject": subj, "textContent": text, "tags": ["d47-guard"]})
                out["alert"]["sent"] = True
                self.dash("KĻŪDA", f"Sūtīšanas diena {d}: D47 aizturēti {len(self._guard)} kontakti - vēstule Raivim")
        return out

    def send_day_ok(self, et, d) -> bool:
        """D2, or the R1 Monday DRY of the Tuesday chain (--chain-dry=1, DRY only): that date stands for the Tuesday."""
        return is_send_day(et, d) or (bool(getattr(self, "chain_dry", False)) and et not in EDUCATIONAL_TYPES)

    def s5_gate(self, d) -> dict:
        """R1 S5 (Raivis' contract, MAIN 3 13.10): every rendered card (P and R, FLOW*) == cab-<YYYYMMDD>. Tuesday only
        (or the chain DRY). Holds per contact in every FLOW-reading type; a FLOW build not after today's OK
        letter_fields run holds those types whole. Never raises: an unanswerable gate holds (fail closed)."""
        dry = bool(getattr(self, "chain_dry", False))
        if d.weekday() != TUESDAY and not dry:
            self._s5 = {}
            return {"skipped": "not a Tuesday"}
        emails = {(c.get("email") or "").strip().lower() for rows in (getattr(self, "_cands", None) or {}).values() for c in rows}
        gd = getattr(self, "gate_date", None) or d       # a DRY rehearsal against another day's builds reads that day
        try:
            self._s5 = S5.evaluate(self.q, gd, emails, dry=dry)
        except Exception as e:  # noqa: BLE001
            self._s5 = {"order_hold": f"{S5.HOLD_FLOW} gate could not run: {type(e).__name__}: {str(e)[:200]}", "holds": {}}
        return {k: v for k, v in self._s5.items() if k != "holds"} | {"held_contacts": len(self._s5.get("holds", {}))}

    def chain_d44(self, d) -> dict:
        """R1 Monday DRY: D44 = the orchestrator's S1-S4 rows of mkt_control.tuesday_chain_log for this date, mode DRY."""
        rows = {r["step"]: dict(r) for r in self.q(
            f"SELECT step, ok, status, reason FROM `{M}.tuesday_chain_log` WHERE chain_date = DATE '{d}' AND mode = 'DRY'")}
        bad = [f"CHAIN_{s}_NOT_OK {(rows.get(s) or {}).get('status') or 'no row'}" for s in ("S1", "S2", "S3", "S4")
               if not (rows.get(s) or {}).get("ok")]
        return {"commercial": bad, "akcija_236": list(bad)}

    def d44(self, d, live) -> dict:
        """RAIVIS D44: the Tuesday price chain must have run today and in order; else commercial types are held."""
        rows = [dict(r) for r in self.q(DG.d44_facts_sql(d))]
        self._d44 = DG.d44_eval(rows[0] if rows else {})
        out = {"facts": {k: (str(v) if v is not None else None) for k, v in (rows[0] if rows else {}).items()},
               **self._d44}
        if live and self._d44["commercial"]:
            self.dash("KĻŪDA", f"Sūtīšanas diena {d}: D44 cenu ķēde nav pareizā secībā - "
                               + "; ".join(self._d44["commercial"]) + " - komerciālās vēstules aizturētas")
        return out

    def launch_236(self, d, live, refill, exclude_sent_today=False) -> dict:
        import akcija_launch as AL
        try:
            extra = [] if track_enabled("akcija", getattr(self, "_track_rows", None) or []) \
                else ["D1 TRACK_NOT_ENABLED_BY_RAIVIS track=akcija"]
            out = AL.launch(self.q, self.brevo, d, live, refill, getattr(self, "_d44", None), self.dash,
                            gate_date=getattr(self, "gate_date", None), pace=self.pace, extra_holds=extra,
                            exclude_sent_today=exclude_sent_today)
        except Exception as e:  # noqa: BLE001 - a failed launch is reported, never retried blind
            msg = f"{type(e).__name__}: {str(e)[:300]}"
            if live:
                self.dash("KĻŪDA", f"236 {d} nav palaists - {msg}")
            out = {"launched": False, "error": msg}
        if live:
            self.record_236(d, out)
        return out

    def record_236(self, d, out):
        """R6: the 236 launch leaves its send_day_run row too (email_type 'akcija_236', template 236; sent = the
        audience Brevo counts at launch - list 3 minus blacklist and exclusions)."""
        aud = (out or {}).get("audience") or {}
        res = {"email_type": "akcija_236", "template_id": 236, "track": "akcija", "cap": None,
               "candidates": int(aud.get("final") or 0), "chosen": int(aud.get("final") or 0) if out.get("launched") else 0,
               "refused": {}, "type_refusal": None if out.get("launched") else
               ("236_NOT_LAUNCHED " + str(out.get("error") or out.get("refused") or out.get("holds") or "")[:250]),
               "campaign_id": out.get("campaign_id"), "list_id": 3,
               "sent": int(aud.get("final") or 0) if out.get("launched") else 0}
        try:
            self._record(res, d, True, [], {})
        except Exception as e:  # noqa: BLE001 - the row is bookkeeping; the launch already happened
            print(f"RECORD_236_FAILED {type(e).__name__}: {str(e)[:200]}")

    def refill_akcija_excl(self, d, live) -> dict:
        """RAIVIS D46 (MAIN 2 12.10 18:35): right after the personal letters of a Tuesday, list 98 (AKCIJA IZSLĒGT) is
        rebuilt from THIS day's outcome - every personal letter that went (all types), plus (a)/(c)/(d) - so the
        hand-sent 236 reaches exactly the eligible LV contacts without a personal letter; a letter held at send time
        leaves its contact in the 236 audience. LIVE pushes to Brevo; DRY builds the forecast table only."""
        if d.weekday() != TUESDAY:
            return {"skipped": "not a Tuesday"}
        import flow_gate as FG
        try:
            l3 = FG.snapshot_l3(self.q, self.brevo, d.isoformat())      # D47: live list 3 (GET only)
            b = FG.build_excl(self.q, d.isoformat(), forecast=not live)
            b["list3_live"] = l3
            b["brevo"] = FG.sync_excl(self.q, self.brevo, d.isoformat()) if live else None
            return b
        except Exception as e:  # noqa: BLE001 - reported, and the 236 checklist refuses without a fresh list
            msg = f"{type(e).__name__}: {str(e)[:300]}"
            if live:
                self.dash("KĻŪDA", f"Akcijas 236 izslēgšanas saraksts (98) {d} nav atjaunots - {msg} - 236 nesūtīt")
            return {"error": msg}

    def write_outcome(self, plan_run, d, live, types, only=None) -> dict:
        plan = [dict(r) for r in self.q(
            f"SELECT master_key, LOWER(TRIM(email)) AS email, email_type, template_id, "
            f"CAST(planned_send_date AS STRING) AS planned_send_date, would_send, hold_reason, reason "
            f"FROM `{T_PLAN}` WHERE run_id = '{plan_run}' AND email_type IS NOT NULL")]
        if only:
            plan = [p for p in plan if p["email_type"] in only]
        rows = outcome_rows(plan, types, d, live)
        mode = "live" if live else "dry"
        for i in range(0, len(rows), 1000):
            vals = ",".join("(" + ",".join([f"DATE '{d}'", _s(self.run_id), _s(mode), _s(plan_run), _s(r[0]), _s(r[1]),
                                            _s(r[2]), _n(r[3]), (f"DATE '{r[4]}'" if r[4] else "NULL"), _s(r[5]),
                                            _s((r[6] or "")[:300] or None), _s((r[7] or "")[:300] or None),
                                            "CURRENT_TIMESTAMP()"]) + ")" for r in rows[i:i + 1000])
            self.q(f"INSERT INTO `{T_OUTCOME}` VALUES {vals}")
        counts = {}
        for r in rows:
            counts[r[5]] = counts.get(r[5], 0) + 1
        return {"table": T_OUTCOME, "rows": len(rows), "by_outcome": counts}

    def _one(self, t, d, plan_date, plan_run, live, cap_rows, track_rows, appr_rows, caps_override):
        et, tid, track = t["email_type"], _i(t["template_id"]), t["track"]
        res = {"email_type": et, "template_id": tid, "track": track, "candidates": 0, "chosen": 0, "refused": {},
               "type_refusal": None, "cap": None, "would_get": []}
        if not self.send_day_ok(et, d):
            res["type_refusal"] = f"NOT_A_SEND_DAY ({d:%a}; {et} goes on weekday {send_weekday(et)})"
            return self._record(res, d, live, [], {})
        cached = getattr(self, "_cands", None) or {}
        cands = cached[et] if et in cached else [dict(r) for r in self.q(candidates_sql(plan_run, d, et))]
        res["candidates"] = len(cands)
        cap = int((caps_override or {}).get(et, 0)) if caps_override is not None else cap_of(cap_rows, et, d)
        res["cap_source"] = "override" if caps_override is not None else ("send_cap" if cap else None)
        has_row = any(str(r["send_date"]) == str(d) and r["email_type"] == et for r in cap_rows)
        if caps_override is None and not has_row and track_enabled(track, track_rows):
            # FUSE (MAIN 2 12.10 15:40): an ENABLED type with no send_cap row gets plan + 5 %, written on a LIVE run;
            # a type that is not enabled keeps "no row = 0 = nothing goes" (D3 as built)
            cap = fuse_cap(len(cands))
            res["cap_source"] = "fuse_plan_plus_5pct"
            if live:
                self.q(f"INSERT INTO `{T_CAP}` VALUES (DATE '{d}', {_s(et)}, {cap}, 'engine_fuse', CURRENT_TIMESTAMP(), "
                       f"{_s(f'plan {len(cands)} + 5 % (MAIN 2 12.10 15:40)')})")
        res["cap"] = cap
        d1 = standing_approval(et, track, tid, track_rows, appr_rows, self.live_hash)
        d44 = (getattr(self, "_d44", None) or {}).get("commercial") if et in DG.COMMERCIAL else None
        if et == D50_FEEDBACK:
            res["type_refusal"] = "D50_FEEDBACK_MOVED (244 moves to the delivery pipeline; these contacts get 236)"
        elif d44:
            res["type_refusal"] = "D44 " + "; ".join(d44)
        elif d1:
            res["type_refusal"] = "D1 " + "; ".join(d1)
            if live and cap > 0:
                self.dash("KĻŪDA", f"Sūtīšanas diena {d}: {et} atteikts - " + "; ".join(d1) + " - nekas nav sūtīts")
        elif cap <= 0:
            res["type_refusal"] = "D3 NO_CAP (0)"
        # D31 (INTERFACE BLOCKS v1) + MAIN 18:50: a template that reads a block field goes only when the LATEST push row
        # of each of its blocks today is LIVE/OK/0, checked at SEND time; LIVE waits (5 min re-check) until 10:30 Riga
        cfg = BG.letter_config(tid, self.template_html(tid)) if tid else {"reads": [], "product": [], "mismatch": None,
                                                                           "source": "none"}
        blocks_needed = cfg["reads"]
        res["blocks"], res["block_config"] = blocks_needed, cfg["source"]
        if cfg["mismatch"] and not res["type_refusal"]:
            res["type_refusal"] = cfg["mismatch"]
        if blocks_needed:
            gd = getattr(self, "gate_date", None) or d
            res.update(self.block_gate(gd, blocks_needed, wait=live and not res["type_refusal"]))
            if gd != d:
                res["gate_date"] = str(gd)
            if res["block_hold"] and not res["type_refusal"]:
                res["type_refusal"] = res["block_hold"]
        refuse0 = self.member_refusal_fn(et, lambda c: int(c.get("rung") or 0), plan_date, cands,
                                         blocks_needed=blocks_needed, product=cfg["product"]) if cands else (lambda c: None)
        guard = getattr(self, "_guard", None) or {}
        cab = getattr(self, "_cab", None) or {}

        d50 = getattr(self, "_d50", None) or {}

        d52h = getattr(self, "_d52_holds", None) or {}

        s5 = getattr(self, "_s5", None) or {}
        s5_on = bool(s5) and any(f in S5.FLOW_FIELDS for f in blocks_needed)   # R1 S5: every rendered FLOW card == cab
        if s5_on and s5.get("order_hold") and not res["type_refusal"]:
            res["type_refusal"] = s5["order_hold"]
        s5h = s5.get("holds", {}) if s5_on else {}

        def refuse(c):   # day guards first: D47 holds the contact in EVERY type; the cab link only priced letters
            return (guard.get(c["master_key"]) or d52h.get(c["master_key"]) or (d50.get(c["master_key"]) if et == D50_XSELL else None)
                    or (cab.get(c["master_key"]) if et in DG.PRICED_TYPES else None)
                    or s5h.get((c.get("email") or "").strip().lower()) or refuse0(c))
        # the member selection is evaluated and recorded in DRY even for a refused type (who WOULD get it); LIVE sends
        # nothing for a refused type and reads no contact for it
        chosen, decisions = select_audience(cands, 0 if (live and res["type_refusal"]) else cap, refuse)
        rungs = {int(c.get("rung") or 0) for c in chosen}
        if len(rungs) > 1 and not res["type_refusal"]:
            res["type_refusal"] = f"MIXED_RUNG {sorted(rungs)} - one campaign carries one rung"
        res["chosen"] = len(chosen)
        res["decisions"] = dict(decisions)
        for v in decisions.values():
            if v != "CHOSEN":
                k = v.split(" ")[0]
                res["refused"][k] = res["refused"].get(k, 0) + 1
        res["would_get"] = [{"master_key": c["master_key"], "email": c["email"], "due_since": c["due_since"]}
                            for c in chosen]
        if live and cap > 0 and res["refused"].get("L9_" + S.HOLD_PAID_SOURCE_MISSING):
            self.dash("KĻŪDA", f"Sūtīšanas diena {d}: {et} - Mozello pasūtījumu spogulis vecāks par 60 min, "
                               f"{res['refused']['L9_' + S.HOLD_PAID_SOURCE_MISSING]} vēstules aizturētas")
        if live and chosen and not res["type_refusal"]:
            self.pace()
            try:
                res.update(self._send(et, tid, track, d, chosen, plan_run))
            except Exception as e:  # noqa: BLE001 - R6: a LIVE run ALWAYS leaves its send_day_run row, then raises
                res["type_refusal"] = f"SEND_ERROR {type(e).__name__}: {str(e)[:250]}"
                res["chosen"] = 0
                self._record(res, d, live, cands, decisions)
                raise
            self._last_campaign = (res["campaign_id"], self.clock())
            gone = set(res.get("not_in_brevo") or [])
            for c in chosen:
                if c["email"] in gone:
                    decisions[c["master_key"]] = res["decisions"][c["master_key"]] = \
                        "NOT_IN_BREVO no Brevo contact for this address"
                    res["refused"]["NOT_IN_BREVO"] = res["refused"].get("NOT_IN_BREVO", 0) + 1
            res["chosen"] = len(chosen) - len(gone)
        return self._record(res, d, live, cands, decisions)

    def block_gate(self, d, blocks, wait) -> dict:
        def check():
            return next((w for w in (BG.push_ok(self.q, str(d), b) for b in blocks) if w), None)
        if wait:
            released, why = BG.wait_for(check, self.clock, self.sleep, BG.deadline(d))
        else:
            why = check()
            released = self.clock() if why is None else None
        rows = {}
        if BG._exists(self.q, BG.T_BLOCK_LOG):
            for b in blocks:
                r = BG.latest_push(self.q, str(d), b)
                rows[b] = None if not r else {k: str(r.get(k)) for k in ("mode", "status", "errors", "finished_at")}
        ok_at = None
        if why is None and rows and all(rows.values()):
            ok_at = max(BG._aware(r["finished_at"]) for r in rows.values())
        start = BG.would_start(d, ok_at)
        out = {"block_hold": why, "block_rows": rows, "block_ok_at": ok_at and ok_at.isoformat(),
               "would_start": start and start.astimezone(BG.RIGA).isoformat(),
               "released_at": released and released.isoformat()}
        if why and not wait and self.clock() < BG.deadline(d):
            out["block_wait"] = f"would re-check every {BG.POLL_S // 60} min until {BG.WAIT_UNTIL:%H:%M} Riga"
        return out

    # -- LIVE: list, campaign, dispatch
    def _send(self, et, tid, track, d, chosen, plan_run, alias=None):
        batch = f"{self.run_id}-{et}"
        emails = [c["email"] for c in chosen]
        lst = self.fill_list((f"TEST proof {d} {et} {batch}" if alias is not None else f"SD {d} {et} {batch}"), emails)
        if alias is not None:
            rcpt = proof_recipient(self.env)
            if self.list_members(lst["list_id"]) != {rcpt}:
                raise SP.SendLocked([("PROOF", f"list {lst['list_id']} does not hold exactly {rcpt}")])
            if any(c["master_key"] != test_key(rcpt) for c in chosen):
                raise SP.SendLocked([("PROOF", "a proof writes under its TEST key only")])
        missing = [e for e in emails if e not in set(lst["ok"])]
        gone = self.not_in_brevo(missing) if missing and alias is None else []
        if gone and sorted(gone) == sorted(missing):          # only contacts Brevo does not have -> hold them, send the rest
            chosen = [c for c in chosen if c["email"] not in set(gone)]
            emails = [c["email"] for c in chosen]
        if not emails or sorted(lst["ok"]) != sorted(emails) or not lst["settled"]:
            raise SP.SendLocked([("LIST", f"list {lst['list_id']} holds {len(lst['ok'])}/{len(emails)}, "
                                          f"settled={lst['settled']} - refused")])
        # A proof runs the REAL path: list-4 exclusion and L5 included (MAIN 2026-10-09 18:20 removed the proof-only
        # exception of 9f87d90). If the test address sits in list 4, Brevo answers 400 "There are no contacts
        # associated with the given recipients info" - the fix is the address' suppression, not this request.
        camp = self.create_campaign(et, tid, d, lst["list_id"], batch,
                                    name=(f"TEST proof {et} {batch}" if alias is not None else None))
        content = self.brevo("GET", f"/emailCampaigns/{camp}", None) or {}
        import campaign as CAMP
        hits = CAMP.placeholder_hits(content.get("subject"), content.get("previewText"), content.get("htmlContent") or "")
        if hits:
            raise SP.SendLocked([("CONTENT", f"campaign {camp}: unfilled marks {hits[:5]}")])
        audience = [{"master_key": c["master_key"], "email": c["email"],
                     "person_id": c.get("person_id"), "reason": c.get("reason") or ""} for c in chosen]
        lookups = _DayLookups(self, et, track, tid, plan_run, audience, alias)
        out = SP.dispatch({"campaign_id": camp, "email_type": et, "track": track, "template_id": tid,
                           "rung": int(chosen[0].get("rung") or 0),
                           "utm_campaign": f"sd-{d}-{et}", "brevo_list_id": lst["list_id"]},
                          send_date=str(d), batch_id=batch, build_id=plan_run, config=_Cfg(self.env),
                          lookups=lookups,
                          brevo_send=lambda cid: self.brevo("POST", f"/emailCampaigns/{int(cid)}/sendNow", None),
                          log_sink=self.write_send_log, pd_writer=self._pd,
                          state_advance=lambda mk, e, on, rung: self.advance(mk, e, on, rung, batch, camp),
                          state_advance_batch=lambda items: self.advance_batch(items, batch, camp),
                          offered_sink=self.write_offered, now=self.now, pd_workers=self.pd_workers())
        return {"batch_id": batch, "list_id": lst["list_id"], "campaign_id": camp, "sent": out["sent"], "not_in_brevo": gone,
                "pd_writes": out["pd_writes"], "pd_errors": out.get("pd_errors", 0),
                "pd_activity_ids": list(self.pd_ids)}

    # -- PROOF (decision 2)
    def attr_types(self):
        r = self.brevo("GET", "/contacts/attributes", None) or {}
        return {a["name"]: a.get("type") for a in r.get("attributes") or [] if a.get("category") == "normal"}

    @staticmethod
    def typed(values, types):
        """letter_fields values -> Brevo attribute values of the attribute's own type. None: '' for text; a number or
        a boolean without a value is left out (Brevo keeps none)."""
        out = {}
        for k, v in values.items():
            t = types.get(k)
            if t is None:
                continue                                  # not an attribute in Brevo: nothing to write
            if t == "boolean":
                if v is not None:
                    out[k] = v is True or str(v).strip().lower() == "true"
            elif t == "float":
                if v not in (None, ""):
                    out[k] = float(v)
            else:
                out[k] = "" if v is None else str(v)
        return out

    def proof(self, et, d, pd_person, wait_s=600, source_email=None) -> dict:
        rcpt = proof_recipient(self.env)
        if not l1_open(self.env):
            raise SP.SendLocked([("L1", "a proof sends one real mail: open L1 on the execution only")])
        for ddl in DDL:
            self.q(ddl)
        plan_run, plan_date = self.plan_run_of(d)
        if str(plan_date) != str(d):
            raise SP.SendLocked([("PROOF", f"no plan of {d} (latest {plan_date})")])
        tpl = next((t for t in self.types_of(plan_run, d) if t["email_type"] == et), None)
        if tpl is None:
            raise SP.SendLocked([("PROOF", f"no {et} in plan {plan_run}")])
        tid, track = _i(tpl["template_id"]), tpl["track"]
        tr = [dict(r) for r in self.q(f"SELECT track, enabled, enabled_by FROM `{T_TRACK}`")]
        ap = [dict(r) for r in self.q(f"SELECT template_id, email_type, approved, approved_by, approved_sha256 "
                                      f"FROM `{T_APPROVAL}`")]
        d1 = [r for r in standing_approval(et, track, tid, tr, ap, self.live_hash) if not r.startswith("TRACK_")]
        if d1:
            raise SP.SendLocked([("D1", "; ".join(d1))])
        cands = [dict(r) for r in self.q(candidates_sql(plan_run, d, et))]
        gate = self.member_refusal_fn(et, lambda c: int(c.get("rung") or 0), plan_date, cands, l11=False)
        want_src = (source_email or "").strip().lower() or None
        src = next((c for c in cands if (want_src is None or (c.get("email") or "").strip().lower() == want_src)
                    and gate(c) is None), None)
        if src is None:
            raise SP.SendLocked([("PROOF", f"no member of {et} passes the member gates today"
                                           + (f" (asked for source {want_src})" if want_src else ""))])
        frow = self.full_lf_rows(plan_date, [src["email"]]).get(src["email"])
        if not frow:
            raise SP.SendLocked([("PROOF", f"no letter_fields row for the source member of {et}")])
        want = SP.letter_params(frow)
        src_attrs = self.contact_attrs(src["email"])
        if src_attrs is None:
            raise SP.SendLocked([("PROOF", "the source member's Brevo attributes could not be read")])
        want.update(proof_member_fields(src_attrs))          # (h) D1-D4, UZRUNA, VARDS as the member has them
        types = self.attr_types()
        before = {k: v for k, v in (self.contact_attrs(rcpt) or {}).items() if k in want}
        payload = self.typed(want, types)
        self.brevo("PUT", f"/contacts/{rcpt}", {"attributes": payload})
        self.q(f"INSERT INTO `{T_PROOF}` (run_id, email_type, source_master_key, source_email, raivis_before, written, "
               f"created_at, recipient) VALUES ({_s(self.run_id)}, {_s(et)}, {_s(src['master_key'])}, "
               f"{_s(src['email'])}, {_s(json.dumps(before, default=str))}, {_s(json.dumps(payload, default=str))}, "
               f"CURRENT_TIMESTAMP(), {_s(rcpt)})")
        time.sleep(5)
        diff = attrs_equal({k: v for k, v in want.items() if k in types}, self.contact_attrs(rcpt) or {})
        if diff:
            raise SP.SendLocked([("L11", f"{rcpt} attributes differ after the write: {diff[:8]}")])
        me = {"master_key": test_key(rcpt), "email": rcpt, "person_id": int(pd_person), "rung": src.get("rung"),
              "reason": f"PROOF of {et} (source letter: send_day_proof run {self.run_id})"}
        out = self._send(et, tid, track, d, [me], plan_run, alias={test_key(rcpt): src["master_key"]})
        t0, camp = time.time(), {}
        while time.time() - t0 < wait_s:
            camp = self.brevo("GET", f"/emailCampaigns/{out['campaign_id']}", None) or {}
            if camp.get("status") == "sent":
                break
            time.sleep(15)
        self.q(f"UPDATE `{T_PROOF}` SET campaign_id = {int(out['campaign_id'])}, list_id = {int(out['list_id'])} "
               f"WHERE run_id = {_s(self.run_id)}")
        html = camp.get("htmlContent") or ""
        attrs = self.contact_attrs(rcpt) or {}
        return {**out, "email_type": et, "template_id": tid, "source_master_key": src["master_key"],
                "brevo_status": camp.get("status"), "subject": camp.get("subject"),
                "unfilled": unfilled_marks(html + " " + (camp.get("subject") or ""), attrs),
                "stats": ((camp.get("statistics") or {}).get("globalStats") or {})}

    def proof_dry(self, et, d) -> dict:
        """MAIN 2026-10-09 18:20 (4): the proof up to, NOT including, any Brevo write - which member's letter it would
        carry, what L5 says for the test address, and the campaign request it would make (recipients included)."""
        rcpt = proof_recipient(self.env)
        plan_run, plan_date = self.plan_run_of(d)
        tpl = next((t for t in self.types_of(plan_run, d) if t["email_type"] == et), None)
        if tpl is None:
            raise SP.SendLocked([("PROOF", f"no {et} in plan {plan_run}")])
        tid, track = _i(tpl["template_id"]), tpl["track"]
        tr = [dict(r) for r in self.q(f"SELECT track, enabled, enabled_by FROM `{T_TRACK}`")]
        ap = [dict(r) for r in self.q(f"SELECT template_id, email_type, approved, approved_by, approved_sha256 "
                                      f"FROM `{T_APPROVAL}`")]
        d1 = standing_approval(et, track, tid, tr, ap, self.live_hash)
        cands = [dict(r) for r in self.q(candidates_sql(plan_run, d, et))]
        gate = self.member_refusal_fn(et, lambda c: int(c.get("rung") or 0), plan_date, cands, l11=False)
        src = next((c for c in cands if gate(c) is None), None)
        supp = self.suppressed([rcpt])
        attrs = self.contact_attrs(rcpt) or {}
        return {"email_type": et, "template_id": tid, "plan_run": plan_run, "d1_other_than_track":
                [r for r in d1 if not r.startswith("TRACK_")], "source_master_key": src and src["master_key"],
                "L5_suppressed": sorted(supp), "L5": "PASS" if not supp else "CLOSED",
                "recipient": rcpt, "test_key": test_key(rcpt),
                "recipient_brevo_list_ids": sorted(self.contact_lists(rcpt)),
                "recipient_attrs_count": len(attrs),
                "recipient_attrs_nonempty": sum(1 for v in attrs.values() if _norm(v) != ""),
                "planned_campaign": {"name": f"TEST proof {et} <run>", "recipients":
                                     {"listIds": [f"<new list of exactly {rcpt}>"],
                                      "exclusionListIds": [SUPPRESSION_LIST_ID]}},
                "brevo_writes": 0}

    def contact_lists(self, email):
        return set((self.brevo("GET", f"/contacts/{email}", None) or {}).get("listIds") or [])

    def draft_test(self, et, d, list_id) -> dict:
        """MAIN 2026-10-09 17:50 (2): ONE campaign create that is NEVER sent - the exact request of create_campaign,
        to a list holding exactly the PROOF_RECIPIENT address, named TEST. Returns Brevo's answer or its error body."""
        rcpt = proof_recipient(self.env)
        if not l1_open(self.env):
            raise SP.SendLocked([("L1", "a draft create is a Brevo write: open L1 on the execution only")])
        if self.list_members(int(list_id)) != {rcpt}:
            raise SP.SendLocked([("PROOF", f"list {list_id} does not hold exactly {rcpt}")])
        plan_run, _pd = self.plan_run_of(d)
        tpl = next((t for t in self.types_of(plan_run, d) if t["email_type"] == et), None)
        if tpl is None:
            raise SP.SendLocked([("PROOF", f"no {et} in plan {plan_run}")])
        tid = _i(tpl["template_id"])
        out = {"email_type": et, "template_id": tid, "list_id": int(list_id),
               "recipient_list_ids": sorted(self.contact_lists(rcpt))}
        try:
            cid = self.create_campaign(et, tid, d, int(list_id), self.run_id,
                                       name=f"TEST draft {et} {self.run_id} - never sent")
        except Exception as e:  # noqa: BLE001 - the point of this call is to read the error
            return {**out, "error": str(e)[:2000]}
        camp = self.brevo("GET", f"/emailCampaigns/{cid}", None) or {}
        return {**out, "campaign_id": cid, "status": camp.get("status"), "subject": camp.get("subject"),
                "name": camp.get("name")}

    def proof_restore(self, run_ids):
        """Put the PROOF_RECIPIENT's attributes back to what they were before the FIRST proof of these runs."""
        rcpt = proof_recipient(self.env)
        rows = self.q(f"SELECT run_id, raivis_before, written, recipient FROM `{T_PROOF}` WHERE run_id IN "
                      f"({','.join(_s(r) for r in run_ids)}) ORDER BY created_at")
        other = sorted({(r.get("recipient") or LEGACY_PROOF_RECIPIENT) for r in rows} - {rcpt})
        if other:
            raise SP.SendLocked([("PROOF", f"these runs wrote to {other}, not to the configured {rcpt}")])
        # ONLY the earliest run's before-values are the contact's own: a later run's "before" holds what an earlier
        # proof wrote (measured 2026-10-11: merging them left 27 proof values behind). Every key any run wrote and the
        # earliest run did not see filled goes back to no value.
        before = json.loads(rows[0]["raivis_before"] or "{}") if rows else {}
        keys = set()
        for r in rows:
            keys |= set(json.loads(r["written"] or "{}"))
        types = self.attr_types()
        # a key with no value before goes back to NO value. Brevo ignores null (measured 2026-10-09: a PUT with null
        # left OFFER_HAS_STD / OFFER_RUNG / P1_FRESH unchanged) and clears any type with '' (measured: the same three
        # disappeared from the contact after one PUT with '').
        restore = {k: before[k] if k in before else "" for k in keys}
        self.brevo("PUT", f"/contacts/{rcpt}", {"attributes": restore})
        self.q(f"UPDATE `{T_PROOF}` SET restored_at = CURRENT_TIMESTAMP() WHERE run_id IN "
               f"({','.join(_s(r) for r in run_ids)})")
        now = self.contact_attrs(rcpt) or {}
        differ = sorted(k for k, v in restore.items() if _norm(now.get(k)) != _norm(v))
        cleared = sorted(k for k in restore if k not in before)
        return {"restored": sorted(restore), "cleared": cleared, "differ_after": differ,
                "after_cleared": {k: now.get(k) for k in cleared}}

    def fill_list(self, name, emails, rounds=3, pause=6, settle_s=300):
        lid = int(self.brevo("POST", "/contacts/lists", {"name": name[:120], "folderId": LIST_FOLDER_ID})["id"])
        todo, members = list(emails), set()
        for _ in range(rounds):
            for i in range(0, len(todo), ADD_CHUNK):
                try:
                    self.brevo("POST", f"/contacts/lists/{lid}/contacts/add", {"emails": todo[i:i + ADD_CHUNK]})
                except Exception as e:  # noqa: BLE001 - Brevo answers 400 when NO address of the chunk could be added
                    # (measured 13.10 run df3907: the 2nd round held only addresses Brevo has no contact for); the
                    # members re-read below decides, _send holds the ones that are not in Brevo
                    if "does not exist" not in str(e):
                        raise
            time.sleep(pause)
            members = self.list_members(lid)
            todo = [e for e in emails if e not in members]
            if not todo:
                break
        t0 = time.time()
        while True:
            n = (self.brevo("GET", f"/contacts/lists/{lid}", None) or {}).get("uniqueSubscribers") or 0
            if n >= len(members & set(emails)) or time.time() - t0 > settle_s:
                break
            time.sleep(10)
        extra = members - set(emails)
        return {"list_id": lid, "ok": [e for e in emails if e in members],
                "settled": n >= len(members & set(emails)) and not extra}

    def not_in_brevo(self, emails):
        """The addresses Brevo answers 404 for (no contact) - they cannot get a letter, the rest of the type still can."""
        out = []
        for e in emails:
            try:
                self.brevo("GET", f"/contacts/{e}", None)
            except Exception as ex:  # noqa: BLE001
                if "404" in str(ex) or "document_not_found" in str(ex):
                    out.append(e)
                else:
                    raise
        return out

    def list_members(self, lid):
        out, off = set(), 0
        while True:
            r = self.brevo("GET", f"/contacts/lists/{lid}/contacts?limit=500&offset={off}", None) or {}
            page = [(c.get("email") or "").strip().lower() for c in r.get("contacts") or []]
            out |= set(page)
            off += 500
            if len(page) < 500:
                return out

    def create_campaign(self, et, tid, d, list_id, batch, name=None, exclude_suppression=True):
        import campaign as CAMP
        tpl = self.brevo("GET", f"/smtp/templates/{int(tid)}", None) or {}
        week = f"{d.isocalendar()[0]}-w{d.isocalendar()[1]:02d}"
        html = tpl.get("htmlContent") or ""
        if CAMP.UTM_WEEK_MARKER in html:              # the UTM seam where the template carries it (its own refusals)
            html, _pairs = CAMP.apply_utm_week(html, week)
        # else: byte for byte - the approved letters 179 / 180 / 244 carry no utm link at all (measured 2026-10-09);
        # their D1 hash is of exactly these bytes
        payload = {"name": (name or f"SD {d} {et} ({batch})")[:200], "subject": tpl.get("subject") or "",
                   "sender": {"id": CAMP.SENDER_ID}, "replyTo": "info@tiktik.lv", "htmlContent": html,
                   "recipients": ({"listIds": [int(list_id)], "exclusionListIds": [SUPPRESSION_LIST_ID]}
                                  if exclude_suppression else {"listIds": [int(list_id)]}),
                   "inlineImageActivation": False}
        return int(self.brevo("POST", "/emailCampaigns", payload)["id"])

    # -- writers
    def pd_workers(self):
        try:
            return max(1, int(self.env.get("PD_WORKERS") or PD_WORKERS_DEFAULT))
        except ValueError:
            return PD_WORKERS_DEFAULT

    def write_send_log(self, rows):
        """R6: one INSERT per BQ_CHUNK rows (2550 rows = 6 statements, was one per run but only after ~2 h)."""
        for i in range(0, len(rows or []), BQ_CHUNK):
            self._write_send_log(rows[i:i + BQ_CHUNK])

    def _write_send_log(self, rows):
        if not rows:
            return
        vals = ",".join(
            "(" + ",".join([_s(r["master_key"]), _s(r["email"]), str(int(r["campaign_id"])),
                            _n(r.get("brevo_list_id")), _s(r["email_type"]), _n(r.get("template_id")),
                            _s(r.get("track")), "NULL", _n(r.get("rung")), f"TIMESTAMP {_s(r['sent_at'])}",
                            _s(r["source"]), _s(r["run_id"]), "NULL", _s(r.get("utm_campaign")),
                            _s(r.get("reason"))]) + ")" for r in rows)
        self.q(f"INSERT INTO `{T_LOG}` (master_key, email, campaign_id, brevo_list_id, email_type, template_id, track, "
               f"step, rung, sent_at, source, run_id, brevo_message_id, utm_campaign, reason) VALUES {vals}")

    def advance_batch(self, items, batch, camp):
        """R6: the per-contact advance() for a whole campaign in 2 statements per BQ_CHUNK contacts (was 2 per contact,
        ~22 contacts/min). items = [(master_key, email_type, sent_on, rung)] - one email_type and one sent_on per
        campaign, so the UPDATE carries the same values advance() wrote one by one."""
        items = list(items or [])
        for i in range(0, len(items), BQ_CHUNK):
            part = items[i:i + BQ_CHUNK]
            vals = ",".join(f"({_s(mk)}, {_s(et)}, DATE '{on}', {int(rung or 0)}, {_s(batch)}, {int(camp)}, "
                            f"CURRENT_TIMESTAMP())" for mk, et, on, rung in part)
            self.q(f"INSERT INTO `{T_ADV}` VALUES {vals}")
            for (et, on) in sorted({(et, str(on)) for _, et, on, _ in part}):
                keys = ",".join(_s(mk) for mk, e, o, _ in part if e == et and str(o) == on)
                self.q(f"UPDATE `{T_STATE}` SET last_email_type = {_s(et)}, last_sent_on = DATE '{on}', "
                       f"step = IFNULL(step, 0) + 1, last_send_source = 'engine_live' WHERE master_key IN ({keys})")

    def write_offered(self, rows):
        """R7 PP3: every R product a sent 235 showed, so the next 235 never offers it again (presend XS_REPEAT)."""
        rows = list(rows or [])
        for i in range(0, len(rows), BQ_CHUNK):
            vals = ",".join(f"({_s(r['master_key'])}, {_s(r['email'])}, {_s(r['handle'])}, TIMESTAMP {_s(r['sent_at'])}, "
                            f"{int(r['campaign_id'])}, {_s(r['run_id'])})" for r in rows[i:i + BQ_CHUNK])
            self.q(f"INSERT INTO `{T_OFFERED}` (master_key, email, handle, sent_at, campaign_id, run_id) VALUES {vals}")

    def advance(self, mk, et, on, rung, batch, camp):
        self.q(f"INSERT INTO `{T_ADV}` VALUES ({_s(mk)}, {_s(et)}, DATE '{on}', {int(rung or 0)}, {_s(batch)}, "
               f"{int(camp)}, CURRENT_TIMESTAMP())")
        self.q(f"UPDATE `{T_STATE}` SET last_email_type = {_s(et)}, last_sent_on = DATE '{on}', step = IFNULL(step, 0) + 1, "
               f"last_send_source = 'engine_live' WHERE master_key = {_s(mk)}")

    @staticmethod
    def _detail(res):
        keys = ("gate_date", "blocks", "block_config", "block_hold", "block_rows", "block_ok_at", "would_start", "released_at", "block_wait")
        det = {k: res[k] for k in keys if res.get(k)}
        return json.dumps(det, sort_keys=True, default=str) if det else None

    def _record(self, res, d, live, cands, decisions):
        mode = "live" if live else "dry"
        self.q(f"INSERT INTO `{T_RUN}` VALUES ({_s(self.run_id)}, DATE '{d}', {_s(mode)}, {_s(res['email_type'])}, "
               f"{_n(res['template_id'])}, {_s(res['track'])}, {_n(res.get('cap'))}, {int(res['candidates'])}, "
               f"{int(res['chosen'])}, {_s(json.dumps(res['refused'], sort_keys=True))}, {_s(res['type_refusal'])}, "
               f"{_s(res.get('batch_id'))}, {_n(res.get('list_id'))}, {_n(res.get('campaign_id'))}, "
               f"{_n(res.get('sent'))}, {_n(res.get('pd_writes'))}, {_s(self._detail(res))}, CURRENT_TIMESTAMP())")
        if cands:
            rows = []
            for rank, c in enumerate(cands, 1):
                dec = decisions.get(c["master_key"], "NOT_EVALUATED")
                rows.append("(" + ",".join([f"DATE '{d}'", _s(self.run_id), _s(res.get("batch_id")), _s(mode),
                                            _s(res["email_type"]), _n(res["template_id"]), _s(res["track"]),
                                            _n(c.get("rung")), _s(c["master_key"]), _s(c["email"]), _n(c.get("person_id")),
                                            f"DATE '{c['due_since']}'", str(rank), _s(dec[:300]),
                                            "CURRENT_TIMESTAMP()"]) + ")")
            for i in range(0, len(rows), 500):
                self.q(f"INSERT INTO `{T_AUD}` VALUES " + ",".join(rows[i:i + 500]))
        return res


class _Cfg:
    """send_path's L1 from the job's env: ALLOW_SEND and DRY_RUN, plus GLOBAL_SEND_ENABLED (it closes L1 too)."""
    def __init__(self, env):
        self.ALLOW_SEND = l1_open(env)
        self.DRY_RUN = not l1_open(env)


class _DayLookups:
    """send_path.dispatch's lookups for ONE frozen audience of this run. L3 / L4 = D1 re-read at dispatch time;
    everything else answers from the warehouse on the send date (= today, L10)."""
    def __init__(self, day, et, track, tid, plan_run, audience, alias=None):
        self.day, self.et, self.track, self.tid, self._plan_run, self.aud = day, et, track, tid, plan_run, audience
        self._ok, self.alias = None, alias or {}
        self.back = {v: k for k, v in self.alias.items()}

    def _src(self, mks):
        return [self.alias.get(m, m) for m in mks]

    def _mine(self, d):
        return {self.back.get(k, k): v for k, v in d.items()}

    def _d1(self):
        if self._ok is None:
            tr = [dict(r) for r in self.day.q(f"SELECT track, enabled, enabled_by FROM `{T_TRACK}`")]
            ap = [dict(r) for r in self.day.q(f"SELECT template_id, email_type, approved, approved_by, approved_sha256 "
                                               f"FROM `{T_APPROVAL}`")]
            self._ok = standing_approval(self.et, self.track, self.tid, tr, ap, self.day.live_hash)
        return self._ok

    def track_enabled(self, track):                                  # L3 = D1 (a); a PROOF batch skips it
        return bool(self.alias) or not any(r.startswith("TRACK_") for r in self._d1())

    def template_approved(self, template_id):                        # L4 = D1 (b) + (c)
        return not any(not r.startswith("TRACK_") for r in self._d1())

    def audience(self, batch_id, build_id):
        return self.aud

    def suppressed(self, emails):
        return len(self.day.suppressed(emails))

    def letter_fields(self, send_date):
        return self.day.wh.letter_fields(send_date)

    def plan_run(self, send_date):
        return self.day.wh.plan_run(send_date)

    def goods_run(self, send_date):
        lf = self.day.wh.letter_fields(send_date)
        return lf and lf.get("run_id")

    def goods(self, run_id, master_keys):
        d = self.day.now.astimezone(RIGA).date()
        lf, plan, out = self.day.wh.lf_rows(d), self.day.wh.plan_rows(d), {}
        for mk in master_keys:
            p = plan.get(self.alias.get(mk, mk))
            row = lf.get(p["email"]) if p else None
            g = G.row_goods(row if row and row.get("email_type") == self.et else None)
            if g is not None:
                out[mk] = g
        return out

    def presend_ctx(self, campaign, send_date, master_keys):
        return self._mine(self.day.wh.presend_ctx(campaign, send_date, self._src(master_keys)))

    def person_blocks(self, send_date, master_keys):
        return self._mine(self.day.wh.person_blocks(send_date, self._src(master_keys)))


_ATTR_REF = __import__("re").compile(r"\{\{\s*contact\.([A-Z0-9_]+)\s*(\|[^}]*)?\}\}")


def unfilled_marks(text, attrs) -> list:
    """The {{ contact.X }} references of a letter that would print EMPTY for this contact (no value and no default
    filter), plus any ⟦ left. Brevo renders at send time; this reads what the contact holds after the send."""
    out = sorted({m.group(1) for m in _ATTR_REF.finditer(text or "")
                  if _norm((attrs or {}).get(m.group(1))) == "" and "default" not in (m.group(2) or "")})
    return out + (["⟦"] if "⟦" in (text or "") else [])


def _s(v):
    if v is None:
        return "NULL"
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _n(v):
    return "NULL" if v in (None, "") else str(int(v))


# ------------------------------------------------------------------------------------------------ production wiring
def _prod():
    from google.cloud import bigquery
    import send_lookups
    import campaign as CAMP
    c = bigquery.Client(project=P)
    q = lambda sql: [dict(r) for r in c.query(sql).result()]  # noqa: E731

    def brevo(method, path, payload):
        internal = path == "/smtp/email" and payload and \
            all((r.get("email") or "").lower() in INTERNAL_NOTICE_TO for r in payload.get("to") or [{}]) \
            and not payload.get("cc") and not payload.get("bcc")
        if method != "GET" and not l1_open(os.environ) and not internal:
            raise SP.SendLocked([("L1", f"Brevo {method} {path} refused while L1 is closed")])
        return CAMP._call(method, path, payload)

    tok_cache = {}

    def pd_post(record):
        import urllib.error
        import urllib.request
        import gsecret
        if "t" not in tok_cache:                         # R6: one Secret Manager read per run, not per contact
            tok_cache["t"] = gsecret.read("PIPEDRIVE_API_TOKEN")
        tok = tok_cache["t"]
        body = {"subject": record["subject"], "type": record["type_key"], "due_date": record["due_date"],
                "done": 1 if record["done"] else 0, "note": record["note"]}
        if record.get("target_person_id"):
            body["person_id"] = int(record["target_person_id"])
        req = urllib.request.Request(f"https://api.pipedrive.com/v1/activities?api_token={tok}",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json"})
        for attempt in range(5):                         # R6: parallel writes meet Pipedrive's burst limit -> back off
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read()).get("data", {}).get("id")
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504) or attempt == 4:
                    raise
                time.sleep(2 * (attempt + 1))

    def dash(status, text):
        import edu
        return edu.dash_row(status, text)
    return q, send_lookups.warehouse(), brevo, pd_post, dash


def main(argv) -> int:
    args = dict(a.lstrip("-").split("=", 1) for a in argv if "=" in a)
    d = dt.date.fromisoformat(args.get("date") or os.environ.get("SEND_DATE")
                              or dt.datetime.now(RIGA).date().isoformat())
    mode = args.get("mode") or os.environ.get("SEND_MODE") or "auto"
    caps = json.loads(args["caps"]) if args.get("caps") else None
    if args.get("proof-restore"):
        q, wh, brevo, pd_post, dash = _prod()
        print("PROOF_RESTORE " + json.dumps(Day(q, wh, brevo, pd_post, dash, os.environ)
                                            .proof_restore(args["proof-restore"].split(",")), default=str))
        return 0
    if args.get("proof-dry"):
        q, wh, brevo, pd_post, dash = _prod()
        day = Day(q, wh, brevo, pd_post, dash, os.environ)
        print("PROOF_DRY " + json.dumps({"run_id": day.run_id, **day.proof_dry(args["proof-dry"], d)},
                                        ensure_ascii=False, default=str)[:6000])
        return 0
    if args.get("draft-test"):
        q, wh, brevo, pd_post, dash = _prod()
        day = Day(q, wh, brevo, pd_post, dash, os.environ)
        try:
            res = day.draft_test(args["draft-test"], d, int(args["list"]))
        except SP.SendLocked as e:
            print("DRAFT_TEST_REFUSED " + str(e)[:800])
            return 3
        print("DRAFT_TEST " + json.dumps({"run_id": day.run_id, **res}, ensure_ascii=False, default=str)[:6000])
        return 0
    if args.get("proof"):
        q, wh, brevo, pd_post, dash = _prod()
        day = Day(q, wh, brevo, pd_post, dash, os.environ)
        try:
            res = day.proof(args["proof"], d, int(args["pd-person"]), source_email=args.get("proof-source"))
        except SP.SendLocked as e:
            print("PROOF_REFUSED " + str(e)[:800])
            return 3
        print("PROOF " + json.dumps({"run_id": day.run_id, **res}, ensure_ascii=False, default=str)[:20000])
        return 0
    if args.get("only-236"):                       # D52 13.10: 236 alone, after the personal runs of the day
        q, wh, brevo, pd_post, dash = _prod()
        day = Day(q, wh, brevo, pd_post, dash, os.environ)
        live = (args.get("mode") != "dry") and l1_open(os.environ)
        if live and not SP.riga(day.now).time() >= SP.SEND_WINDOW[0]:
            print("ONLY_236_REFUSED outside the send window")
            return 3
        day.gate_date = dt.date.fromisoformat(args["gate-date"]) if args.get("gate-date") else None
        day._track_rows = [dict(r) for r in q(f"SELECT track, enabled, enabled_by FROM `{T_TRACK}`")]
        day._d44 = {"commercial": [], "akcija_236": [h for h in (args.get("d52-hold-236") or "").split("|") if h]} \
            if args.get("d52-override") == D52_REASON else day.d44(d, live)
        refill = day.refill_akcija_excl(d, live)
        res = {"run_id": day.run_id, "mode": "live" if live else "dry", "akcija_excl": refill,
               "akcija_236": day.launch_236(d, live, refill, exclude_sent_today=True)}
        if live:
            day.tech_log(*report_mail({"send_date": str(d), "types": [], **res}, "sent"))
        print("ONLY_236 " + json.dumps(res, ensure_ascii=False, default=str)[:6000])
        return 0
    if args.get("test-236"):                       # D48 test: real campaign to the 2 test addresses only
        import akcija_launch as AL
        q, wh, brevo, pd_post, dash = _prod()
        gd = dt.date.fromisoformat(args["gate-date"]) if args.get("gate-date") else None
        print("TEST_236 " + json.dumps(AL.test(q, brevo, d, gate_date=gd), ensure_ascii=False, default=str)[:6000])
        return 0
    only = set(args["only"].split(",")) if args.get("only") else None
    sim = args.get("d44-simulate-pass") == "1"
    force_double = args.get("d47-force-double")
    q, wh, brevo, pd_post, dash = _prod()
    day = Day(q, wh, brevo, pd_post, dash, os.environ)
    try:
        gd = dt.date.fromisoformat(args["gate-date"]) if args.get("gate-date") else None
        day.d44_simulate_pass = sim
        if args.get("d52-override") == D52_REASON:
            day.d52 = {"hold_236": [h for h in (args.get("d52-hold-236") or "").split("|") if h]}
        day.d47_force_double = force_double
        if args.get("chain-dry") == "1":                    # R1 Monday DRY of the Tuesday chain: never live
            if mode != "dry":
                print("SEND_DAY_REFUSED --chain-dry needs --mode=dry")
                return 3
            day.chain_dry = True
        res = day.run(d, mode=mode, caps_override=caps, only=only, gate_date=gd)
        if args.get("notify") == "check":                  # 08:05 Rīta pārbaude - mail ONLY when NOT OK (MAIN 3 (b))
            if only:
                res["only"] = sorted(only)
            res.update(check_notify(day, res))
    except SP.SendLocked as e:
        print("SEND_DAY_REFUSED " + str(e)[:800])
        dash("KĻŪDA", f"Sūtīšanas diena {d}: atteikts - {str(e)[:250]} - nekas nav sūtīts")
        if args.get("notify") == "check":
            check_failed_notify(day, d, f"atteikts - {e}")
        return 3
    except Exception as e:  # noqa: BLE001 - a crashed 08:05 check must not be silent (silent = OK)
        if args.get("notify") == "check":
            check_failed_notify(day, d, f"{type(e).__name__}: {e}")
        raise
    print("SEND_DAY " + json.dumps(res, ensure_ascii=False, default=str)[:20000])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
