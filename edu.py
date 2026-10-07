"""EDUCATIONAL LETTER through the engine: ONE letter, ONE date, ONE audience rule, ONE Brevo campaign. LV only.

MAIN's block of 2026-10-07 13:10 (Raivis' decisions 12:59). This is its own weekly slot: it reads the sales engine's
morning tables and never writes one of them - no plan row, no letter_fields row, no send_log row, no sequence state.

FAIL CLOSED. A send for (send_date, letter_code) happens only when ALL of this is true at that moment:
  the letter row exists, carries the approved content hash and is ARMED            mkt_control.edu_letter
  a GO record of that date exists, written by the check, not older than 60 minutes  mkt_control.edu_gate
  NO STOP record exists for that date and letter (a STOP wins over any GO, for good) mkt_control.edu_gate
  nothing was started for it before (one send per letter and date, no retry)         mkt_control.edu_log
  the frozen audience of that GO is there, with the count the GO recorded            mkt_control.edu_audience
  the source campaign in Brevo still hashes to the approved content, has no ⟦ and has an unsubscribe link
No GO (check failed, hung, never ran) = nothing is sent and the log says so.

TWO IDENTITIES, because no identity holds both rights (as with the daily sample):
  ops account   (reads every table; no Brevo key)   prepare | check | stop | status      - the ops shell
  campaign SA   (the Brevo key; mkt_control only)    verify | send                        - Cloud Run job tiktik-edu-send
The check starts the job in verify mode to compare the approved hash with what Brevo holds NOW.

WHAT A SEND DOES: makes a Brevo list holding exactly the frozen audience, creates the engine's OWN campaign from the
source campaign's subject, preview text and HTML (the source draft is never changed and never sent), reads it back,
re-reads STOP, calls sendNow once, and writes one mkt_control.edu_sent row per person.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

P = "jaunais-za-aizv04022026"
M = f"{P}.mkt_control"
T_LETTER, T_SEGMENT, T_AUD, T_GATE = f"{M}.edu_letter", f"{M}.edu_segment", f"{M}.edu_audience", f"{M}.edu_gate"
T_BREVO, T_SENT, T_LOG = f"{M}.edu_brevo", f"{M}.edu_sent", f"{M}.edu_log"
T_BLIST = f"{M}.edu_brevo_list"                 # snapshot of Brevo lists (EE, EN), read by the campaign identity
SEND_JOB, REGION = "tiktik-edu-send", "europe-west1"
SALES_JOB = "tiktik-marketing-sender"
TEST_RECIPIENT = "raivis@alenda.lv"            # the only address a test_only letter can reach - a constant
# Audience rules. They differ ONLY in the track filter; gates, freeze and checks are the same code.
RULES = {"lv_glove_buyers": " AND info_track = 'cimdi'",     # LV people whose own category is gloves
         "lv_all": "",                                        # every LV person the engine holds (MAIN 2026-10-07 14:21)
         "lv_edu_all": None}   # every OLD contact of the hand list minus B2B leads (Raivis 2026-10-07 15:17); own SQL
EDU_ALL = "lv_edu_all"
# SEVEN INTEREST GROUPS (MAIN 2026-10-07 17:19) by purchase track. Anybody without one of these tracks - no track at
# all (old-account customers, sign-ups), 'cits', 'inventars' - is on the GLOVES line.
GROUPS = ("cimdi", "dezinfekcija", "teipi", "papirs", "medicina", "tirisana", "apgerbs")
DEFAULT_GROUP, GENERAL_GROUP = "cimdi", "visiem"          # 'visiem' = a letter of no group (documents, plans)
GROUP_RULES = {"lv_edu_" + g: g for g in GROUPS}           # one audience rule per group, on top of lv_edu_all
PICK_RULE = "lv_edu_pick"                                  # audience = whom the Tuesday selection gave THIS letter
RULES.update({r: None for r in GROUP_RULES})
RULES[PICK_RULE] = None
EDU_ALL_RULES = (EDU_ALL, PICK_RULE) + tuple(GROUP_RULES)
# THE MIX (Raivis 2026-10-07 11:02: "mostly their own group, but not the same group week after week", his example
# gloves, gloves, disinfection, documents, cleaning plan, gloves, paper). One entry per Thursday the person has
# already had from the engine, cycled: 'own' = a letter of the person's group, 'other' = of any other group.
EDU_MIX = ("own", "own", "other", "other", "other", "own", "other")
T_CAT, T_PSRC, T_PICK, T_PICKRUN = f"{M}.edu_catalog", f"{M}.edu_piece_source", f"{M}.edu_pick", f"{M}.edu_pick_run"
T_SEENX = f"{M}.edu_seen_extra"
T_HIST, T_HSTATE = f"{M}.brevo_campaign_recipients_hist", f"{M}.edu_hist_state"   # who received which campaign
# PRODUCT SLOTS (MAIN 2026-10-07 18:40, the form is fixed by MAIN): 40 campaign params per letter and date in
# mkt_control.edu_letter_param. This code only READS that table. Brevo does not give campaign params back, so the
# engine's copy sets them from the table and the read-back cannot prove them - the values hash logged at GO does.
T_PARAM = f"{M}.edu_letter_param"
PARAM_SLOTS, PARAM_FIELDS = ("G1", "G2", "G3", "G4", "T1", "T2", "T3", "T4"), ("NAME", "URL", "IMG", "STD", "PRICE")
PARAM_KEYS = tuple(f"{a}_{b}" for a in PARAM_SLOTS for b in PARAM_FIELDS)
T_SHOP = f"{P}.business_marts.product_catalog"      # the shop as the letter must match it: visible products only
HIST_MAX_AGE_MIN = 30                              # a pick reads a history refreshed at most this long ago
T_LEAD, T_REG = f"{M}.b2b_lead_email", f"{M}.b2b_cold_register"     # who is a B2B cold lead; the GP / dental registers
INPUT_LISTS = (3, 4, 46, 75, 52, 53, 55, 56, 57, 58)                 # Brevo lists the snapshot job reads for this rule
FRESH_LISTS = (3, 4, 46, 75, 52, 53, 55, 56, 57, 58)                 # these must be of the day of the check
SIGNUP_LISTS = (55, 56, 57, 58)                                      # questionnaire / hygiene-plan sign-ups
# Raivis 2026-10-07 15:55: "b2b klientiem un ne veikala pircējiem nesūtam, sūtam veikala pircējiem, anketu pildītājiem
# un vecajiem klientiem". So the ONLY engine reason that does not keep a person out is the personal letter of the
# week; B2B_FLOW, NOT_TIKTIK_BUYER and any reason nobody has seen yet keep the address out.
EDU_ALL_PASS_REASONS = (None, "", "PERSONAL_LETTER_THIS_WEEK")
F309_KEY, F309_B2B = "036687330a0d889920e7166c94392ca6238c0115", "716"   # Pipedrive org field "MKT Plūsma" = B2B
SUPPRESSION_LIST, EN_LIST, EE_LIST = 4, 46, 75  # Brevo lists: tiktik_suppression, EN_foreign_LANG_en, EE_klienti_tel372
EXCLUDE_LISTS = (SUPPRESSION_LIST, EN_LIST, EE_LIST)
TEST_EXCLUDE_LISTS = (EN_LIST, EE_LIST)         # the test address itself sits in list 4 (measured 2026-10-07)
PASS_REASONS = (None, "PERSONAL_LETTER_THIS_WEEK")   # own weekly slot: a personal sales letter does not exclude
SEGMENT_MAX_AGE_H, GO_MAX_AGE_MIN, ADD_CHUNK, ADD_FAIL_PCT, DEFAULT_TOLERANCE_PCT = 26, 60, 150, 2.0, 10
DASH = "Company-Alenda-SIA/shared-platforms/_agent-sessions.md"
AGENT = "tiktik.lv › Marketing · Sūtīšanas dzinējs (izglītojošā vēstule)"

DDL = f"""
CREATE TABLE IF NOT EXISTS `{T_LETTER}` (send_date DATE, letter_code STRING, source_campaign_id INT64,
  approved_sha256 STRING, approved_by STRING, approved_at TIMESTAMP, audience_rule STRING, expected_audience INT64,
  tolerance_pct INT64, test_only BOOL, armed BOOL, armed_by STRING, armed_at TIMESTAMP, note STRING);
CREATE TABLE IF NOT EXISTS `{T_SEGMENT}` (built_at TIMESTAMP, email STRING, language STRING, info_track STRING);
CREATE TABLE IF NOT EXISTS `{T_AUD}` (send_date DATE, letter_code STRING, check_run STRING, email STRING,
  master_key STRING, built_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS `{T_GATE}` (send_date DATE, letter_code STRING, kind STRING, check_run STRING,
  reason STRING, detail STRING, written_at TIMESTAMP, written_by STRING);
CREATE TABLE IF NOT EXISTS `{T_BREVO}` (send_date DATE, letter_code STRING, check_run STRING,
  source_campaign_id INT64, sha256 STRING, status STRING, subject STRING, modified_at STRING, placeholders INT64,
  unsubscribe_links INT64, html_bytes INT64, error STRING, checked_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS `{T_SENT}` (send_date DATE, letter_code STRING, campaign_id INT64,
  source_campaign_id INT64, brevo_list_id INT64, master_key STRING, email STRING, sent_at TIMESTAMP,
  check_run STRING, test_only BOOL);
CREATE TABLE IF NOT EXISTS `{T_BLIST}` (list_id INT64, email STRING, fetched_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS `{T_CAT}` (letter_code STRING, edu_group STRING, piece STRING, title STRING,
  source_campaign_id INT64, sendable BOOL, prio INT64, note STRING, updated_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS `{T_PSRC}` (piece STRING, kind STRING, ref STRING);
CREATE TABLE IF NOT EXISTS `{T_PARAM}` (send_date DATE, letter_code STRING, param_key STRING, param_value STRING,
  source STRING, set_at TIMESTAMP);
ALTER TABLE `{T_BREVO}` ADD COLUMN IF NOT EXISTS param_mentions INT64;
CREATE TABLE IF NOT EXISTS `{T_HSTATE}` (refreshed_at TIMESTAMP, snapshot_id STRING, sent_campaigns INT64,
  newest_sent_id INT64, covered INT64, missing INT64, added_campaigns INT64, added_rows INT64, detail STRING);
ALTER TABLE `{T_REG}` ADD COLUMN IF NOT EXISTS loaded_at TIMESTAMP;
CREATE TABLE IF NOT EXISTS `{T_SEENX}` (email STRING, piece STRING, source STRING, loaded_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS `{T_PICK}` (pick_run STRING, thursday DATE, email STRING, master_key STRING,
  track STRING, edu_group STRING, slot INT64, slot_kind STRING, letter_code STRING, reason STRING, seen STRING,
  picked_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS `{T_PICKRUN}` (pick_run STRING, thursday DATE, mix STRING, fallback BOOL, people INT64,
  picked INT64, nothing INT64, alert_text STRING, created_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS `{T_LOG}` (send_date DATE, letter_code STRING, logged_at TIMESTAMP, who STRING, event STRING,
  detail STRING);
"""


# ------------------------------------------------------------------------------------------------ pure parts
def content_sha(subject, preview_text, html) -> str:
    """THE approval hash: sha256, lowercase hex, of subject + LF + previewText + LF + htmlContent, each exactly as
    GET /emailCampaigns/{id} returns it (None = empty). No normalisation."""
    return hashlib.sha256("\n".join([subject or "", preview_text or "", html or ""]).encode("utf-8")).hexdigest()


def riga(now: dt.datetime) -> dt.datetime:
    import zoneinfo
    return now.astimezone(zoneinfo.ZoneInfo("Europe/Riga"))


def tolerance_ok(n: int, expected, pct) -> bool:
    if not expected or expected <= 0:
        return False
    return abs(n - expected) * 100 <= (pct if pct is not None else DEFAULT_TOLERANCE_PCT) * expected


def edu_all_gate(r: dict) -> str:
    """ONE address of the hand list -> 'IN' or the first reason it stays out. Pure; order = the order of the funnel."""
    reason = r.get("excluded_reason")
    if _b(r.get("l4")):
        return "LIST_4_SUPPRESSION"
    if _b(r.get("l46")):
        return "EN_LIST"
    if _b(r.get("l75")):
        return "EE_LIST"
    if _b(r.get("lead")):
        return "B2B_LEAD"
    if not _b(r.get("in_engine")) and _b(r.get("b2b_email")):
        return "B2B_FLOW_BY_EMAIL"                 # the engine holds no person, but the address is a B2B customer's
    if _b(r.get("suppressed")) or reason == "SUPPRESSED":
        return "SUPPRESSED"
    if _b(r.get("blocked")):
        return "BREVO_BLOCKLISTED"
    if (r.get("language") or "lv").strip().lower() != "lv" or reason == "EN_PENDING":
        return "NOT_LV"
    if reason not in EDU_ALL_PASS_REASONS:
        return str(reason)
    return "IN"


def edu_all_gates(rows) -> list:
    """All addresses -> [(email, master_key, gate)], one row per address. Among the addresses that pass, a person the
    engine knows (master_key) keeps ONE: the engine's own address first, then one of list 3, then by alphabet."""
    seen, out, best = set(), [], {}
    for r in rows:
        e = (r.get("email") or "").strip().lower()
        if not e or e in seen:
            continue
        seen.add(e)
        g, mk = edu_all_gate(r), (r.get("master_key") or None)
        out.append([e, mk, g])
        if g == "IN" and mk:
            rank = (not _b(r.get("in_lv_all")), not _b(r.get("in_engine")), not _b(r.get("l3")), e)
            if mk not in best or rank < best[mk][0]:
                best[mk] = (rank, e)
    for row in out:
        if row[2] == "IN" and row[1] and best[row[1]][1] != row[0]:
            row[2] = "SECOND_ADDRESS_OF_PERSON"
    return [tuple(x) for x in out]


def group_of(track) -> str:
    """Every person has exactly ONE group: the track when it is one of the seven, otherwise the gloves line."""
    t = (track or "").strip().lower()
    return t if t in GROUPS else DEFAULT_GROUP


def narrow(gates, tracks, rule, picked=None) -> list:
    """lv_edu_all -> the audience of ONE rule. gates = edu_all_gates(...); tracks = {email: track}.
    A group rule keeps the addresses of its group, the pick rule keeps the addresses the selection gave this letter
    (picked = a set, None = no selection exists -> nobody). Nobody who is not IN in lv_edu_all can come in."""
    if rule == EDU_ALL:
        return list(gates)
    out = []
    for e, mk, g in gates:
        if g == "IN":
            if rule in GROUP_RULES:
                g = "IN" if group_of(tracks.get(e)) == GROUP_RULES[rule] else "OTHER_GROUP"
            elif rule == PICK_RULE:
                g = "NO_SELECTION_FOR_THE_DATE" if picked is None else ("IN" if e in picked else "NOT_PICKED_FOR_THIS_LETTER")
            else:
                g = "UNKNOWN_RULE"
        out.append((e, mk, g))
    return out


def pick_letter(group, slot, seen, catalog, mix=EDU_MIX, fallback=False):
    """THE TUESDAY CHOICE for one person, pure. catalog = rows in catalogue order (letter_code, edu_group, piece,
    sendable); seen = the pieces the person has had. -> (letter_code or None, 'own' | 'other', reason).
    Only a sendable letter whose piece the person has NOT had can be chosen. Without fallback an 'own' Thursday with
    nothing unseen in the own group is 'nothing' (and goes to the alert); with fallback it takes another group's."""
    kind = mix[int(slot or 0) % len(mix)]
    cands = [c for c in catalog if _b(c.get("sendable")) and c.get("piece") not in seen]
    own = [c for c in cands if c.get("edu_group") == group]
    other = [c for c in cands if c.get("edu_group") != group]
    first, second = (own, other) if kind == "own" else (other, own)
    if first:
        return first[0]["letter_code"], kind, "PICKED"
    if fallback and second:
        return second[0]["letter_code"], kind, "PICKED_FALLBACK"
    return None, kind, "NOTHING_UNSEEN_" + kind.upper()


def render_alert(thursday, picks, catalog) -> str:
    """The ALERT text for Raivis (Latvian), or '' when everybody has a letter. picks = [{edu_group, letter_code,
    seen}] of one selection. Per group with people left without a letter: how many, and what they have already had."""
    title = {c["piece"]: (c.get("title") or c["piece"]) for c in catalog}
    left = {}
    for p in picks:
        if not p.get("letter_code"):
            g = left.setdefault(p["edu_group"], {"n": 0, "seen": {}})
            g["n"] += 1
            for piece in [x for x in (p.get("seen") or "").split(",") if x]:
                g["seen"][piece] = g["seen"].get(piece, 0) + 1
    if not left:
        return ""
    total = sum(g["n"] for g in left.values())
    lines = [f"ALERT: ceturtdien {thursday} nebūs ko sūtīt {total} klientiem (izglītojošā vēstule).", ""]
    for name, g in sorted(left.items(), key=lambda kv: -kv[1]["n"]):
        lines.append(f"Grupa {name}: {g['n']} klienti bez jaunas vēstules.")
        had = sorted(g["seen"].items(), key=lambda kv: (-kv[1], kv[0]))
        lines.append("  Jau saņemts: " + ("; ".join(f"{title.get(p, p)} ({n})" for p, n in had[:12]) or "nekas"))
    lines += ["", "Vajag jaunu šablonu katrai no šīm grupām līdz ceturtdienai, citādi šie klienti vēstuli nesaņems."]
    return "\n".join(lines)


def param_problems(rows, today) -> list:
    """Is the letter complete for the date? rows = [{param_key, param_value, set_day}] of one date + letter.
    All 40 keys, each once, each non-empty, each set TODAY; no key outside the 40. -> [] or every problem."""
    out, seen = [], {}
    for r in rows:
        seen.setdefault(r.get("param_key"), []).append(r)
    missing = [k for k in PARAM_KEYS if k not in seen]
    if missing:
        out.append(f"MISSING {len(missing)} of 40: " + ",".join(missing[:8]))
    for k, rs in seen.items():
        if k not in PARAM_KEYS:
            out.append(f"UNKNOWN_KEY {k}")
        elif len(rs) > 1:
            out.append(f"DUPLICATE {k}")
        elif not (rs[0].get("param_value") or "").strip():
            out.append(f"EMPTY {k}")
        elif str(rs[0].get("set_day") or "")[:10] != today:
            out.append(f"NOT_SET_TODAY {k} set={rs[0].get('set_day')}")
    return out


def params_sha(values) -> str:
    """THE hash of the 40 slot values: sha256 of compact JSON, keys in the fixed order. Logged at GO, recomputed by
    the send - one changed price between the two and the send refuses."""
    return hashlib.sha256(json.dumps([[k, values[k]] for k in PARAM_KEYS], ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def money(v):
    """'7,99 €' / '7.99' / 7.99 -> 7.99; anything that is not one amount -> None."""
    t = "".join(ch for ch in str(v if v is not None else "").replace(",", ".") if ch.isdigit() or ch == ".")
    try:
        return round(float(t), 2) if t and t.count(".") <= 1 else None
    except ValueError:
        return None


def norm_url(u) -> str:
    u = (u or "").strip().lower().split("#")[0].split("?")[0]
    return u.replace("http://", "https://").replace("https://tiktik.lv", "https://www.tiktik.lv").rstrip("/")


def slot_problems(values, shop, http_ok) -> list:
    """The shop gate, pure. shop = {norm_url: {std_price, eff_price, stock}} of VISIBLE products; http_ok = {url: bool}
    for every URL and image of the letter. Per slot: the product is visible and in stock, PRICE and STD equal the
    shop's, the page and the image answer. Anything unknown is a problem (fail closed)."""
    out = []
    for sl in PARAM_SLOTS:
        url, img = values.get(sl + "_URL"), values.get(sl + "_IMG")
        p = shop.get(norm_url(url))
        if not p:
            out.append(f"{sl} NOT_A_VISIBLE_SHOP_PRODUCT")
        else:
            if not (p.get("stock") is not None and float(p["stock"]) > 0):
                out.append(f"{sl} OUT_OF_STOCK")
            for fld, col in (("PRICE", "eff_price"), ("STD", "std_price")):
                a, b = money(values.get(f"{sl}_{fld}")), money(p.get(col))
                if a is None or b is None or abs(a - b) > 0.005:
                    out.append(f"{sl} {fld}_DIFFERS letter={values.get(sl + '_' + fld)} shop={p.get(col)}")
        if http_ok.get(url) is not True:
            out.append(f"{sl} URL_DOES_NOT_ANSWER")
        if http_ok.get(img) is not True:
            out.append(f"{sl} IMAGE_DOES_NOT_ANSWER")
    return out


def history_refusal(state, max_age_min=HIST_MAX_AGE_MIN):
    """May a pick trust the recipients history? state = the latest edu_hist_state row (age_min added). -> '' or why not.
    It must be fresh AND cover every campaign Brevo has sent - otherwise a piece somebody already had looks unseen."""
    if not state:
        return "HISTORY_NEVER_REFRESHED"
    if state.get("age_min") is None or int(state["age_min"]) > max_age_min or int(state["age_min"]) < 0:
        return f"HISTORY_NOT_FRESH age_min={state.get('age_min')}"
    if int(state.get("missing") or 0) != 0:
        return f"HISTORY_OLDER_THAN_BREVO missing_campaigns={state.get('missing')} newest_sent={state.get('newest_sent_id')}"
    return ""


def emails_of_export(text) -> list:
    """The addresses of a Brevo recipients export (CSV, any delimiter): the first address of every data line."""
    out, seen = [], set()
    for ln in (text or "").splitlines()[1:]:
        for tok in ln.replace(";", ",").replace("\t", ",").split(","):
            tok = tok.strip().strip('"').strip().lower()
            if "@" in tok and "." in tok.rsplit("@", 1)[-1] and " " not in tok:
                if tok not in seen:
                    seen.add(tok)
                    out.append(tok)
                break
    return out


def stale_inputs(rows, today: str) -> list:
    """Which inputs of lv_edu_all are NOT of the day of the check. rows = [{name, day}]. Missing = stale."""
    return sorted(r["name"] for r in rows if str(r.get("day") or "")[:10] != today)


def exclusion_lists(test_only: bool) -> list:
    return list(TEST_EXCLUDE_LISTS if test_only else EXCLUDE_LISTS)


def check_reasons(s: dict) -> list:
    """The 09:30 decision, pure. s = the facts the check gathered. -> [] = GO, else every reason for NO-GO."""
    r, L = [], s.get("letter")
    if not L:
        return ["NO_LETTER_ROW"]
    if not L.get("approved_sha256"):
        r.append("NO_APPROVAL_HASH")
    if not L.get("armed"):
        r.append("NOT_ARMED")
    if s.get("stops"):
        r.append("STOPPED")
    if s.get("rule_unknown"):
        r.append(f"UNKNOWN_AUDIENCE_RULE={s.get('rule')}")
    if not s.get("plan_run"):
        r.append("NO_PLAN_RUN_TODAY")
    elif s.get("plan_selfcheck_failed") not in (0, "0"):
        r.append(f"PLAN_SELFCHECK_FAILED={s.get('plan_selfcheck_failed')}")
    if s.get("writer_status") != "OK":
        r.append(f"WRITER_NOT_OK={s.get('writer_status')}")
    if not s.get("sendtime_rows"):
        r.append("NO_0855_SELFCHECK_ROWS")
    if s.get("hard_failed"):
        r.append("HARD_SELFCHECK_FAILED=" + ",".join(sorted(s["hard_failed"]))[:200])
    if not s.get("gate_rows"):
        r.append("NO_PERSON_GATE_ROWS_TODAY")
    if s.get("sales_switch_open"):
        r.append("SALES_SWITCH_OPEN=" + str(s["sales_switch_open"])[:120])
    if s.get("tracks_enabled") not in (0, "0"):
        r.append(f"TRACK_ENABLED={s.get('tracks_enabled')}")
    if s.get("param_problems"):
        r.append("PARAMS_NOT_READY=" + "; ".join(s["param_problems"])[:300])
    if s.get("overlap"):
        r.append(f"OVERLAPS_ANOTHER_LETTER_OF_THE_DATE={s['overlap']} (one educational letter per person and date)")
    if s.get("rule") in EDU_ALL_RULES and s.get("inputs_stale") != []:
        r.append("EDU_ALL_INPUT_NOT_OF_TODAY=" + ",".join(s.get("inputs_stale") or ["not checked"])[:200])
    if s.get("segment_age_h") is None or s["segment_age_h"] > SEGMENT_MAX_AGE_H or not s.get("segment_rows"):
        r.append(f"SEGMENT_MISSING_OR_OLD age_h={s.get('segment_age_h')}")
    n = s.get("audience")
    if not n:
        r.append("AUDIENCE_EMPTY")
    elif L.get("test_only"):
        if n != 1 or s.get("audience_emails") != [TEST_RECIPIENT]:
            r.append("TEST_AUDIENCE_IS_NOT_ONLY_THE_TEST_RECIPIENT")
    elif not tolerance_ok(int(n), L.get("expected_audience"), L.get("tolerance_pct")):
        r.append(f"AUDIENCE_OUT_OF_TOLERANCE n={n} expected={L.get('expected_audience')} "
                 f"pct={L.get('tolerance_pct') or DEFAULT_TOLERANCE_PCT}")
    if s.get("audience_bad"):
        r.append(f"AUDIENCE_HOLDS_SUPPRESSED_OR_DUPLICATE={s['audience_bad']}")
    b = s.get("brevo")
    if not b:
        r.append("BREVO_NOT_VERIFIED (the verify job gave no answer)")
    else:
        if b.get("error"):
            r.append("BREVO_READ_ERROR=" + str(b["error"])[:160])
        if b.get("sha256") != (L.get("approved_sha256") or "").strip().lower():
            r.append(f"CONTENT_HASH_MISMATCH brevo={str(b.get('sha256'))[:12]} approved={str(L.get('approved_sha256'))[:12]}")
        if b.get("status") != "draft":
            r.append(f"SOURCE_CAMPAIGN_STATUS={b.get('status')}")
        if int(b.get("placeholders") or 0):
            r.append(f"PLACEHOLDERS_IN_LETTER={b.get('placeholders')}")
        if not int(b.get("unsubscribe_links") or 0):
            r.append("NO_UNSUBSCRIBE_LINK")
    return r


def send_refusals(s: dict) -> list:
    """The send decision, pure, evaluated by the job right before it touches Brevo. [] = may send."""
    r, L = [], s.get("letter")
    if not L:
        return ["NO_LETTER_ROW"]
    if s.get("today") != s.get("send_date"):
        r.append(f"NOT_THE_SEND_DATE today={s.get('today')}")
    if not L.get("armed"):
        r.append("NOT_ARMED")
    if not L.get("approved_sha256"):
        r.append("NO_APPROVAL_HASH")
    if s.get("stops"):
        r.append("STOPPED")
    g = s.get("go")
    if not g:
        r.append("NO_GO_RECORD")
    else:
        if s.get("last_gate_kind") != "GO":
            r.append(f"LATEST_CHECK_IS_{s.get('last_gate_kind')}")
        if g.get("age_min") is None or g["age_min"] > GO_MAX_AGE_MIN or g["age_min"] < 0:
            r.append(f"GO_TOO_OLD age_min={g.get('age_min')}")
        if int(s.get("audience") or 0) != int(g.get("audience") or -1) or not s.get("audience"):
            r.append(f"FROZEN_AUDIENCE_DIFFERS now={s.get('audience')} go={g.get('audience')}")
    if s.get("started"):
        r.append("ALREADY_STARTED (one send per letter and date)")
    if s.get("already_got"):
        r.append(f"RECIPIENTS_ALREADY_GOT_A_LETTER_TODAY={s['already_got']}")
    if L.get("test_only") and s.get("audience_emails") not in (None, [TEST_RECIPIENT]):
        r.append("TEST_AUDIENCE_IS_NOT_ONLY_THE_TEST_RECIPIENT")
    return r


def send_refusals_params(go_sha, params) -> list:
    """Pure: the send may use the slot values only when they are complete and hash to what the GO logged."""
    if not params:
        return ["PARAMS_NOT_COMPLETE_AT_SEND"]
    now = params_sha(params)
    return [] if go_sha and now == go_sha else [f"PARAM_VALUES_NOT_THOSE_OF_THE_GO now={now[:12]} go={str(go_sha)[:12]}"]


def content_refusals(letter, camp, placeholders, unsub) -> list:
    r = []
    sha = content_sha(camp.get("subject"), camp.get("previewText"), camp.get("htmlContent"))
    if sha != (letter.get("approved_sha256") or "").strip().lower():
        r.append(f"CONTENT_HASH_MISMATCH brevo={sha[:12]} approved={str(letter.get('approved_sha256'))[:12]}")
    if placeholders:
        r.append(f"PLACEHOLDERS_IN_LETTER={placeholders}")
    if not unsub:
        r.append("NO_UNSUBSCRIBE_LINK")
    return r


def campaign_payload(source, name, list_id, excl, params=None) -> dict:
    """The engine's own campaign: the source's subject, preview text and HTML, byte for byte; nothing else of it.
    params = the 40 slot values of the letter and date, set here because Brevo does not copy or return them."""
    p = {"name": name, "subject": source.get("subject"), "sender": {"id": (source.get("sender") or {}).get("id") or 2},
         "replyTo": source.get("replyTo") or "info@tiktik.lv", "htmlContent": source.get("htmlContent"),
         "recipients": {"listIds": [list_id]}, "inlineImageActivation": False,
         "mirrorActive": bool(source.get("mirrorActive"))}
    if source.get("previewText"):
        p["previewText"] = source["previewText"]
    if excl:
        p["recipients"]["exclusionListIds"] = list(excl)
    if params:
        p["params"] = {k: params[k] for k in PARAM_KEYS}
    return p


def chunks(xs, n):
    return [xs[i:i + n] for i in range(0, len(xs), n)]


# ------------------------------------------------------------------------------------------------- BigQuery
_TOK = {}


def _token() -> str:
    if _TOK.get("exp", 0) < time.time() + 60:
        try:
            req = urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/instance/"
                                         "service-accounts/default/token", headers={"Metadata-Flavor": "Google"})
            j = json.loads(urllib.request.urlopen(req, timeout=5).read())
            _TOK.update(t=j["access_token"], exp=time.time() + int(j.get("expires_in", 300)))
        except Exception:  # noqa: BLE001 - not on a metadata server: the gcloud identity of the shell
            _TOK.update(t=subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip(),
                        exp=time.time() + 600)
    return _TOK["t"]


def _param(name, v):
    if name == "d":                                    # the send date, always: a DATE, never text
        t, val = {"type": "DATE"}, {"value": dt.date.fromisoformat(str(v)).isoformat()}
    elif isinstance(v, bool):
        t, val = {"type": "BOOL"}, {"value": "true" if v else "false"}
    elif isinstance(v, int):
        t, val = {"type": "INT64"}, {"value": str(v)}
    elif isinstance(v, dt.date):
        t, val = {"type": "DATE"}, {"value": v.isoformat()}
    elif isinstance(v, (list, tuple)):
        t, val = {"type": "ARRAY", "arrayType": {"type": "STRING"}}, {"arrayValues": [{"value": str(x)} for x in v]}
    else:
        t, val = {"type": "STRING"}, {"value": None if v is None else str(v)}
    return {"name": name, "parameterType": t, "parameterValue": val}


def query(sql, **params):
    """BigQuery REST jobs.query with NAMED parameters; rows as dicts, scalars as text. Works for both identities."""
    base = f"https://bigquery.googleapis.com/bigquery/v2/projects/{P}/queries"

    def call(url, body=None):
        req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + _token(), "Content-Type": "application/json"})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=120).read())
        except urllib.error.HTTPError as e:
            raise RuntimeError("BigQuery refused: " + e.read().decode()[:600]) from None
    body = {"query": sql, "useLegacySql": False, "timeoutMs": 60000, "maxResults": 20000}
    if params:
        body.update(parameterMode="NAMED", queryParameters=[_param(k, v) for k, v in params.items()])
    r = call(base, body)
    ref = r["jobReference"]
    more = f"{base}/{ref['jobId']}?" + urllib.parse.urlencode({"location": ref.get("location", ""), "timeoutMs": 60000,
                                                              "maxResults": 20000})
    deadline = time.time() + 300
    while not r.get("jobComplete"):
        if time.time() > deadline:
            raise RuntimeError("BigQuery job did not finish in 300 s")
        r = call(more)
    fields, out = (r.get("schema") or {}).get("fields") or [], []
    while True:
        out += [{f["name"]: c["v"] for f, c in zip(fields, row["f"])} for row in r.get("rows") or []]
        if not r.get("pageToken"):
            return out
        r = call(more + "&" + urllib.parse.urlencode({"pageToken": r["pageToken"]}))


def _b(v) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


def _i(v):
    return None if v in (None, "") else int(v)


def log(q, d, code, who, event, detail=None):
    txt = detail if detail is None or isinstance(detail, str) else json.dumps(detail, ensure_ascii=False, default=str)
    print(f"EDU_LOG {event} " + (txt or ""))
    q(f"INSERT INTO `{T_LOG}` (send_date, letter_code, logged_at, who, event, detail) "
      f"VALUES (@d, @c, CURRENT_TIMESTAMP(), @w, @e, @t)", d=d, c=code, w=who, e=event, t=txt)


def letter_row(q, d, code):
    rows = q(f"SELECT * FROM `{T_LETTER}` WHERE send_date = @d AND letter_code = @c", d=d, c=code)
    if len(rows) != 1:
        return None
    L = rows[0]
    for k in ("source_campaign_id", "expected_audience", "tolerance_pct"):
        L[k] = _i(L.get(k))
    for k in ("test_only", "armed"):
        L[k] = _b(L.get(k))
    return L


def stops(q, d, code) -> int:
    return int(q(f"SELECT COUNT(*) AS n FROM `{T_GATE}` WHERE send_date = @d AND letter_code = @c AND kind = 'STOP'",
                 d=d, c=code)[0]["n"])


# ---------------------------------------------------------------------------------------------- ops identity
def setup(q):
    for stmt in [s for s in DDL.split(";") if s.strip()]:
        q(stmt)
    print("EDU_SETUP ok")
    return 0


def prepare(q):
    """The slow part, done ahead of the check: who is an LV glove buyer (own category gloves), from the nightly
    payload view - the same source the hand lists were cut from (info_track via business_marts.info_track_map)."""
    q(f"""CREATE OR REPLACE TABLE `{T_SEGMENT}` AS
SELECT CURRENT_TIMESTAMP() AS built_at, LOWER(TRIM(p.email)) AS email, p.LANGUAGE AS language,
       COALESCE(m.track, 'cits') AS info_track
FROM `{P}.business_marts.marketing_brevo_payload` p
LEFT JOIN `{P}.business_marts.info_track_map` m ON m.category = p.FAVORITE_CATEGORY
WHERE p.email IS NOT NULL""")
    r = q(f"SELECT COUNT(*) AS n, COUNTIF(info_track = 'cimdi') AS gloves, "
          f"COUNTIF(info_track = 'cimdi' AND language = 'lv') AS lv_gloves, COUNT(DISTINCT email) AS emails "
          f"FROM `{T_SEGMENT}`")[0]
    print("EDU_PREPARE " + json.dumps(r))
    return 0 if int(r["lv_gloves"]) > 0 and r["n"] == r["emails"] else 1


def audience_sql(rule: str) -> str:
    """One text for every rule; the rule adds its track filter and nothing else. An unknown rule raises."""
    track = RULES[rule]
    if track is None:
        raise KeyError(rule)                       # lv_edu_all has its own text (edu_all_sql) and its own gates
    return f"""
WITH g AS (SELECT DISTINCT email FROM `{T_SEGMENT}` WHERE language = 'lv'{track}),
a AS (SELECT LOWER(TRIM(email)) AS email, master_key, excluded_reason FROM `{M}.shadow_akcija_audience`
      WHERE plan_date = @d AND run_id = @run),
s AS (SELECT DISTINCT LOWER(TRIM(email)) AS email FROM `{P}.business_marts.email_suppression_all`)
SELECT g.email, a.master_key,
       CASE WHEN a.email IS NULL THEN 'NOT_IN_ENGINE_POPULATION'
            WHEN s.email IS NOT NULL THEN 'SUPPRESSED'
            WHEN a.excluded_reason IS NULL OR a.excluded_reason = 'PERSONAL_LETTER_THIS_WEEK' THEN 'IN'
            ELSE a.excluded_reason END AS gate
FROM g LEFT JOIN a USING (email) LEFT JOIN s USING (email)"""


LEADS_SQL = f"""
CREATE OR REPLACE TABLE `{T_LEAD}` AS
WITH ex AS (  -- Pipedrive persons, EXACT split of email_all (never LIKE)
  SELECT p.id AS person_id, p.org_id, LOWER(TRIM(e)) AS email
  FROM `{P}.channel_raw.pipedrive_persons` p,
       UNNEST(SPLIT(REGEXP_REPLACE(IFNULL(p.email_all, ''), r'[;\\s]+', ','), ',')) e
  WHERE REGEXP_CONTAINS(TRIM(e), r'^[^@\\s,]+@[^@\\s,]+\\.[^@\\s,]+$')),
l4 AS (SELECT id FROM `{P}.channel_raw.pipedrive_orgs`
       WHERE 4 IN UNNEST(ARRAY(SELECT SAFE_CAST(x AS INT64) FROM UNNEST(JSON_VALUE_ARRAY(raw_json, '$.label_ids')) x))),
src AS (
  SELECT LOWER(TRIM(email)) AS email, IF(REGEXP_CONTAINS(tag, r'(^|,)dent-'), 'cold_mail_dent', 'cold_mail_gp') AS source
  FROM `{P}.business_marts.brevo_events_raw`
  WHERE event = 'delivered' AND REGEXP_CONTAINS(IFNULL(tag, ''), r'(^|,)(gp|dent)-')
  UNION ALL SELECT ex.email, 'pd_org_label4' FROM ex JOIN l4 ON l4.id = ex.org_id
  UNION ALL SELECT LOWER(TRIM(email)), source FROM `{T_REG}` WHERE status IN ('', 'JAUNS')
  UNION ALL SELECT LOWER(TRIM(email)), CONCAT('brevo_list_', CAST(list_id AS STRING))
            FROM `{T_BLIST}` WHERE list_id IN (52, 53)),
buy AS (  -- bought ANYWHERE: old account, new account, paid shop order, or the engine's person has orders
  SELECT DISTINCT LOWER(TRIM(e)) AS email, 'old_account' AS why FROM `{P}.legacy_paytraq.client_map`,
         UNNEST(REGEXP_EXTRACT_ALL(IFNULL(emails, ''), r'[^,; ]+@[^,; ]+')) e WHERE docs > 0
  UNION DISTINCT SELECT DISTINCT LOWER(TRIM(c.email)), 'new_account' FROM `{P}.paytraq_core.clients` c
         JOIN `{P}.paytraq_core.sales_documents_list` d ON d.client_id = c.client_id
         WHERE d.document_type = 'sale' AND d.document_status NOT IN ('voided', 'draft') AND c.email LIKE '%@%'
  UNION DISTINCT SELECT DISTINCT LOWER(TRIM(email)), 'shop_paid' FROM `{P}.business_marts.mozello_orders`
         WHERE payment_status = 'paid' AND email LIKE '%@%'
  UNION DISTINCT SELECT DISTINCT i.email_norm, 'person_bought' FROM `{P}.business_marts.customer_identity` i
         JOIN `{P}.business_marts.customer_master` m USING (master_key) WHERE m.orders > 0 AND i.email_norm LIKE '%@%'),
g AS (SELECT email, ARRAY_AGG(DISTINCT source ORDER BY source) AS sources FROM src WHERE email LIKE '%@%' GROUP BY 1),
b AS (SELECT email, STRING_AGG(DISTINCT why ORDER BY why) AS bought FROM buy GROUP BY 1),
su AS (SELECT DISTINCT LOWER(TRIM(email)) AS email FROM `{T_BLIST}` WHERE list_id IN (55, 56, 57, 58))
SELECT CURRENT_TIMESTAMP() AS built_at, g.email, g.sources, b.bought, su.email IS NOT NULL AS signed_up,
       b.email IS NULL AND su.email IS NULL AS is_lead     -- bought anywhere, or signed up himself = NOT a lead
FROM g LEFT JOIN b USING (email) LEFT JOIN su USING (email)"""


def inputs(q) -> int:
    """The inputs of lv_edu_all, made fresh: the Brevo lists (snapshot job, GET only) and the B2B-lead table.
    A B2B lead = a cold-outreach contact that never bought anywhere. Exit 0 only if both are of now."""
    out = subprocess.run(["gcloud", "run", "jobs", "execute", SEND_JOB, "--region", REGION, "--project", P, "--wait",
                          "--update-env-vars", "^@^EDU_MODE=snapshot@EDU_SNAPSHOT_LISTS=" + ",".join(map(str, INPUT_LISTS))],
                         capture_output=True, text=True, timeout=160)
    q(LEADS_SQL)
    r = q(f"SELECT COUNT(*) AS n, COUNTIF(is_lead) AS leads, COUNTIF(NOT is_lead) AS cold_source_not_lead FROM `{T_LEAD}`")[0]
    stale = edu_all_stale(q)
    print("EDU_INPUTS " + json.dumps({"snapshot_job_exit": out.returncode, "lead_table": r, "not_of_today": stale}))
    return 0 if not out.returncode and not stale and int(r["leads"]) > 0 else 1


def coldreg(q) -> int:
    """LIVE Pipedrive, GET only: every person e-mail of an organisation that carries Label 4 ("Cold lead") ->
    mkt_control.b2b_cold_register, source pd_label4_live (replaced). The nightly mirror is a day behind a label set
    today; this is not. Nothing is written to Pipedrive."""
    tok = subprocess.run(["gcloud", "secrets", "versions", "access", "latest", "--secret", "PIPEDRIVE_API_TOKEN",
                          "--project", P], capture_output=True, text=True, timeout=60).stdout.strip()

    def pages(what):
        start = 0
        while True:
            with urllib.request.urlopen(f"https://api.pipedrive.com/v1/{what}?limit=500&start={start}&api_token={tok}",
                                        timeout=60) as r:
                d = json.loads(r.read())
            for x in d.get("data") or []:
                yield x
            pg = (d.get("additional_data") or {}).get("pagination") or {}
            if not pg.get("more_items_in_collection"):
                return
            start = pg["next_start"]

    orgs = {o["id"] for o in pages("organizations")
            if 4 in (o.get("label_ids") or ([o["label"]] if o.get("label") else []))}
    rows = set()
    for p in pages("persons"):
        org = p.get("org_id")
        org = org.get("value") if isinstance(org, dict) else org
        if org in orgs:
            for e in p.get("email") or []:
                v = (e.get("value") or "").strip().lower() if isinstance(e, dict) else ""
                if "@" in v:
                    rows.add((v, str(org)))
    if not orgs or not rows:
        print("EDU_COLDREG " + json.dumps({"done": False, "label4_orgs": len(orgs), "emails": len(rows)}))
        return 1
    q(f"DELETE FROM `{T_REG}` WHERE source = 'pd_label4_live'")
    for part in chunks(sorted(rows), 4000):
        q(f"INSERT INTO `{T_REG}` (source, file, file_sha, email, status, pd_org_id, loaded_at) "
          f"SELECT 'pd_label4_live', 'pipedrive api', '', e, '', k, CURRENT_TIMESTAMP() FROM UNNEST(@es) AS e WITH OFFSET o "
          f"JOIN UNNEST(@ks) AS k WITH OFFSET o2 ON o = o2", es=[x[0] for x in part], ks=[x[1] for x in part])
    print("EDU_COLDREG " + json.dumps({"done": True, "label4_orgs_live": len(orgs), "person_emails": len({r[0] for r in rows}),
                                       "rows": len(rows), "pipedrive_written": False}))
    return 0


def edu_all_stale(q) -> list:
    lists = ", ".join(map(str, FRESH_LISTS))
    rows = q(f"""
SELECT CONCAT('brevo_list_', CAST(l AS STRING)) AS name,
       CAST((SELECT DATE(MAX(fetched_at), 'Europe/Riga') FROM `{T_BLIST}` WHERE list_id = l) AS STRING) AS day,
       CAST(CURRENT_DATE('Europe/Riga') AS STRING) AS today
FROM UNNEST([{lists}]) AS l
UNION ALL SELECT 'b2b_lead_email', CAST((SELECT DATE(MAX(built_at), 'Europe/Riga') FROM `{T_LEAD}`) AS STRING),
       CAST(CURRENT_DATE('Europe/Riga') AS STRING)
UNION ALL SELECT 'pipedrive_label4_live', CAST((SELECT DATE(MAX(loaded_at), 'Europe/Riga') FROM `{T_REG}`
       WHERE source = 'pd_label4_live') AS STRING), CAST(CURRENT_DATE('Europe/Riga') AS STRING)
UNION ALL SELECT 'brevo_contacts_snapshot', CAST((SELECT DATE(TIMESTAMP_MILLIS(last_modified_time), 'Europe/Riga')
       FROM `{P}.business_marts.__TABLES__` WHERE table_id = 'brevo_contacts_snapshot') AS STRING),
       CAST(CURRENT_DATE('Europe/Riga') AS STRING)""")
    return stale_inputs(rows, rows[0]["today"]) if rows else ["no answer"]


def edu_all_sql() -> str:
    """Every address of Brevo list 3 (and every lv_all address, should one not be in the list yet) with the facts the
    gates need. One row per address; the gates themselves are edu_all_gate (pure, tested)."""
    lv_all = audience_sql("lv_all")
    return f"""
WITH bl AS (SELECT DISTINCT LOWER(TRIM(email)) AS email, list_id FROM `{T_BLIST}` WHERE list_id IN (3, 4, 46, 75)),
lvall AS (SELECT DISTINCT email FROM ({lv_all}) WHERE gate = 'IN'),
base AS (SELECT email FROM bl WHERE list_id = 3 UNION DISTINCT SELECT email FROM lvall),
a AS (SELECT LOWER(TRIM(email)) AS email, MAX(master_key) AS master_key, MAX(excluded_reason) AS excluded_reason
      FROM `{M}.shadow_akcija_audience` WHERE plan_date = @d AND run_id = @run GROUP BY 1),
i AS (SELECT email_norm AS email, MIN(master_key) AS master_key FROM `{P}.business_marts.customer_identity`
      WHERE email_norm IS NOT NULL GROUP BY 1),
s AS (SELECT DISTINCT LOWER(TRIM(email)) AS email FROM `{P}.business_marts.email_suppression_all`),
ld AS (SELECT DISTINCT email FROM `{T_LEAD}` WHERE is_lead),
bs AS (SELECT LOWER(TRIM(email)) AS email, LOGICAL_OR(email_blocklisted OR NOT email_subscribed) AS blocked
       FROM `{P}.business_marts.brevo_contacts_snapshot` GROUP BY 1),
sg AS (SELECT email, MIN(language) AS language, MIN(info_track) AS info_track FROM `{T_SEGMENT}` GROUP BY 1),
-- B2B by e-mail, for addresses the engine holds no person for: the flow classification (its own e-mail, its Paytraq
-- client's e-mail, the persons of its Pipedrive organisations) and Pipedrive organisation field 309 = B2B
ex AS (SELECT p.org_id, LOWER(TRIM(e)) AS email FROM `{P}.channel_raw.pipedrive_persons` p,
            UNNEST(SPLIT(REGEXP_REPLACE(IFNULL(p.email_all, ''), r'[;\\s]+', ','), ',')) e WHERE TRIM(e) LIKE '%@%'),
cls AS (SELECT customer_key, LOWER(TRIM(email)) AS email, pd_org_ids
        FROM `{P}.legacy_paytraq.b2b_shop_flow_classification_v2` WHERE flow = 'B2B'),
borg AS (SELECT DISTINCT SAFE_CAST(o AS INT64) AS org_id FROM cls, UNNEST(REGEXP_EXTRACT_ALL(IFNULL(pd_org_ids, ''), r'\\d+')) o
         UNION DISTINCT SELECT id FROM `{P}.channel_raw.pipedrive_orgs`
         WHERE REGEXP_EXTRACT(raw_json, r'"{F309_KEY}":\\s*"?(\\d+)') = '{F309_B2B}'),
bcid AS (SELECT REGEXP_EXTRACT(customer_key, r'^cid:(.+)$') AS cid FROM cls
         UNION DISTINCT SELECT paytraq_client_id FROM `{P}.channel_raw.pipedrive_orgs` o JOIN borg ON borg.org_id = o.id),
b2b AS (SELECT email FROM cls WHERE email LIKE '%@%'
        UNION DISTINCT SELECT ex.email FROM ex JOIN borg USING (org_id)
        UNION DISTINCT SELECT LOWER(TRIM(c.email)) FROM `{P}.paytraq_core.clients` c JOIN bcid ON bcid.cid = CAST(c.client_id AS STRING)
                       WHERE c.email LIKE '%@%'),
x AS (SELECT email, LOGICAL_OR(list_id = 3) AS l3, LOGICAL_OR(list_id = 4) AS l4, LOGICAL_OR(list_id = 46) AS l46,
             LOGICAL_OR(list_id = 75) AS l75 FROM bl GROUP BY 1)
SELECT b.email, IFNULL(x.l3, FALSE) AS l3, IFNULL(x.l4, FALSE) AS l4, IFNULL(x.l46, FALSE) AS l46,
       IFNULL(x.l75, FALSE) AS l75,
       ld.email IS NOT NULL AS lead, s.email IS NOT NULL AS suppressed, IFNULL(bs.blocked, FALSE) AS blocked,
       sg.language, sg.info_track AS track, a.email IS NOT NULL AS in_engine, a.excluded_reason,
       b2b.email IS NOT NULL AS b2b_email,
       COALESCE(a.master_key, i.master_key) AS master_key, lvall.email IS NOT NULL AS in_lv_all
FROM base b LEFT JOIN x USING (email) LEFT JOIN a USING (email) LEFT JOIN i USING (email) LEFT JOIN s USING (email)
LEFT JOIN ld USING (email) LEFT JOIN bs USING (email) LEFT JOIN sg USING (email) LEFT JOIN lvall USING (email)
LEFT JOIN b2b USING (email)"""


def latest_pick(q, d, code=None):
    """The latest selection for Thursday d -> (pick_run or None, set of addresses given `code`)."""
    r = q(f"SELECT pick_run FROM `{T_PICKRUN}` WHERE thursday = @d ORDER BY created_at DESC LIMIT 1", d=d)
    if not r:
        return None, None
    rows = q(f"SELECT email FROM `{T_PICK}` WHERE pick_run = @r AND letter_code = @c", r=r[0]["pick_run"], c=code or "")
    return r[0]["pick_run"], {x["email"] for x in rows}


def build_audience_edu_all(q, d, code, check_run, plan_run, test_only, rule=EDU_ALL):
    rows = q(edu_all_sql(), d=d, run=plan_run)
    picked = latest_pick(q, d, code)[1] if rule == PICK_RULE else None
    gates = narrow(edu_all_gates(rows), {(r.get("email") or "").strip().lower(): r.get("track") for r in rows},
                   rule, picked)
    funnel = {}
    for _, _, g in gates:
        funnel[g] = funnel.get(g, 0) + 1
    if rule == PICK_RULE:
        print("EDU_PICK_IN_FORCE " + json.dumps({"date": d, "letter": code, "pick_run": latest_pick(q, d)[0]}))
    ins = [(TEST_RECIPIENT, "test")] if test_only else [(e, mk or "") for e, mk, g in gates if g == "IN"]
    for part in chunks(ins, 4000):
        q(f"INSERT INTO `{T_AUD}` (send_date, letter_code, check_run, email, master_key, built_at) "
          f"SELECT @d, @c, @cr, e, NULLIF(k, ''), CURRENT_TIMESTAMP() FROM UNNEST(@es) AS e WITH OFFSET o "
          f"JOIN UNNEST(@ks) AS k WITH OFFSET o2 ON o = o2", d=d, c=code, cr=check_run,
          es=[x[0] for x in part], ks=[x[1] for x in part])
    n = int(q(f"SELECT COUNT(*) AS n FROM `{T_AUD}` WHERE send_date = @d AND letter_code = @c AND check_run = @cr",
              d=d, c=code, cr=check_run)[0]["n"])
    return n, funnel


def build_audience(q, d, code, check_run, plan_run, test_only, rule):
    """Freeze the audience of this check. -> (n, funnel). The real rule is always measured; a test_only letter
    then keeps ONE row, the test recipient - whatever the rule says about him."""
    if rule in EDU_ALL_RULES:
        return build_audience_edu_all(q, d, code, check_run, plan_run, test_only, rule)
    sql = audience_sql(rule)
    funnel = {r["gate"]: int(r["n"]) for r in q(f"SELECT gate, COUNT(*) AS n FROM ({sql}) GROUP BY 1", d=d, run=plan_run)}
    if test_only:
        q(f"INSERT INTO `{T_AUD}` (send_date, letter_code, check_run, email, master_key, built_at) "
          f"VALUES (@d, @c, @cr, @e, 'test', CURRENT_TIMESTAMP())", d=d, c=code, cr=check_run, e=TEST_RECIPIENT)
    else:
        q(f"INSERT INTO `{T_AUD}` (send_date, letter_code, check_run, email, master_key, built_at) "
          f"SELECT @d, @c, @cr, email, master_key, CURRENT_TIMESTAMP() FROM ({sql}) WHERE gate = 'IN'",
          d=d, c=code, cr=check_run, run=plan_run)
    n = int(q(f"SELECT COUNT(*) AS n FROM `{T_AUD}` WHERE send_date = @d AND letter_code = @c AND check_run = @cr",
              d=d, c=code, cr=check_run)[0]["n"])
    return n, funnel


def read_params(q, d, code):
    rows = q(f"SELECT param_key, param_value, CAST(DATE(set_at, 'Europe/Riga') AS STRING) AS set_day, "
             f"CAST(CURRENT_DATE('Europe/Riga') AS STRING) AS today FROM `{T_PARAM}` "
             f"WHERE send_date = @d AND letter_code = @c", d=d, c=code)
    today = rows[0]["today"] if rows else q("SELECT CAST(CURRENT_DATE('Europe/Riga') AS STRING) AS t")[0]["t"]
    return rows, today


def http_answers(url) -> bool:
    """GET, 200 and a body. A page that redirects elsewhere or errors is not an answer."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.5.0", "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200 and bool(r.read(64))
    except Exception:  # noqa: BLE001 - no answer is no answer
        return False


def param_gate(q, d, code, fetch=http_answers):
    """THE PARAM GATE of the send day. -> (problems, values hash or None, values or None)."""
    rows, today = read_params(q, d, code)
    problems = param_problems(rows, today)
    if problems:
        return problems, None, None
    values = {r["param_key"]: r["param_value"] for r in rows}
    shop = {norm_url(r["url"]): r for r in q(f"SELECT url, std_price, eff_price, stock FROM `{T_SHOP}` WHERE url IS NOT NULL")}
    urls = sorted({values[f"{sl}_{f}"] for sl in PARAM_SLOTS for f in ("URL", "IMG")})
    problems = slot_problems(values, shop, {u: fetch(u) for u in urls})
    return problems, params_sha(values), values


def overlap_other_letters(q, d, code, check_run) -> int:
    """How many addresses of THIS frozen audience another letter of the same date already holds: in the audience of
    its latest check when that check is a GO, or already sent to. Must be 0 - one educational letter per person."""
    return int(q(f"""
WITH lg AS (SELECT letter_code, ARRAY_AGG(STRUCT(kind, check_run) ORDER BY written_at DESC LIMIT 1)[OFFSET(0)] AS g
            FROM `{T_GATE}` WHERE send_date = @d AND letter_code != @c AND kind IN ('GO', 'NO-GO') GROUP BY 1),
o AS (SELECT a.email FROM `{T_AUD}` a JOIN lg ON lg.letter_code = a.letter_code AND lg.g.check_run = a.check_run
      WHERE a.send_date = @d AND lg.g.kind = 'GO' AND a.email != '{TEST_RECIPIENT}'
      UNION DISTINCT SELECT LOWER(email) FROM `{T_SENT}` WHERE send_date = @d AND letter_code != @c
                     AND NOT IFNULL(test_only, FALSE))
SELECT COUNT(*) AS n FROM `{T_AUD}` a JOIN o USING (email)
WHERE a.send_date = @d AND a.letter_code = @c AND a.check_run = @cr""", d=d, c=code, cr=check_run)[0]["n"])


SEEN_SQL = f"""
WITH s AS (
  SELECT LOWER(TRIM(h.email)) AS email, p.piece FROM `{M}.brevo_campaign_recipients_hist` h
         JOIN `{T_PSRC}` p ON p.kind = 'campaign' AND SAFE_CAST(p.ref AS INT64) = h.campaign_id
  UNION DISTINCT SELECT LOWER(TRIM(email)), piece FROM `{T_SEENX}`       -- sends outside the campaign history (04.06 catalogue letters), loaded once
  UNION DISTINCT SELECT LOWER(TRIM(e.email)), c.piece FROM `{T_SENT}` e JOIN `{T_CAT}` c USING (letter_code)
         WHERE NOT IFNULL(e.test_only, FALSE))
SELECT email, STRING_AGG(piece, ',' ORDER BY piece) AS pieces FROM s WHERE email IS NOT NULL GROUP BY 1"""


def pick(q, thursday, mix=EDU_MIX, fallback=False) -> int:
    """THE TUESDAY SELECTION, a dry computation: for every address of lv_edu_all the letter of the coming Thursday, or
    nothing unseen. Writes mkt_control.edu_pick + one edu_pick_run row with the ALERT text. Sends nothing, mails
    nothing, schedules nothing. The plan run and the gates are those of TODAY."""
    today = q("SELECT CAST(CURRENT_DATE('Europe/Riga') AS STRING) AS t")[0]["t"]
    pr = q(f"SELECT run_id FROM `{M}.shadow_run_report` WHERE plan_date = @d ORDER BY finished_at DESC LIMIT 1", d=today)
    stale = edu_all_stale(q)
    hs = q(f"SELECT *, TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), refreshed_at, MINUTE) AS age_min FROM `{T_HSTATE}` "
           f"ORDER BY refreshed_at DESC LIMIT 1")
    hist_no = history_refusal(hs[0] if hs else None)
    if not pr or stale or hist_no:
        print("EDU_PICK " + json.dumps({"done": False, "why": "no plan run today" if not pr else
                                        ("inputs not of today" if stale else hist_no), "not_of_today": stale}))
        return 3
    rows = q(edu_all_sql(), d=today, run=pr[0]["run_id"])
    tracks = {(r.get("email") or "").strip().lower(): r.get("track") for r in rows}
    people = [(e, mk) for e, mk, g in edu_all_gates(rows) if g == "IN"]
    catalog = q(f"SELECT letter_code, edu_group, piece, title, sendable FROM `{T_CAT}` ORDER BY prio, letter_code")
    seen = {r["email"]: r["pieces"] for r in q(SEEN_SQL)}
    slots = {r["email"]: int(r["n"]) for r in q(
        f"SELECT LOWER(TRIM(email)) AS email, COUNT(DISTINCT send_date) AS n FROM `{T_SENT}` "
        f"WHERE NOT IFNULL(test_only, FALSE) GROUP BY 1")}
    run = "pick-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    picks = []
    for e, mk in people:
        g, had = group_of(tracks.get(e)), seen.get(e, "")
        code, kind, why = pick_letter(g, slots.get(e, 0), set(had.split(",")) if had else set(), catalog, mix, fallback)
        picks.append({"e": e, "m": mk or "", "t": tracks.get(e) or "", "edu_group": g, "s": slots.get(e, 0),
                      "k": kind, "letter_code": code or "", "r": why, "seen": had})
    for part in chunks(picks, 2000):
        q(f"INSERT INTO `{T_PICK}` (pick_run, thursday, email, master_key, track, edu_group, slot, slot_kind, "
          f"letter_code, reason, seen, picked_at) SELECT @run, @d, JSON_VALUE(j, '$.e'), NULLIF(JSON_VALUE(j, '$.m'), ''), "
          f"NULLIF(JSON_VALUE(j, '$.t'), ''), JSON_VALUE(j, '$.edu_group'), CAST(JSON_VALUE(j, '$.s') AS INT64), "
          f"JSON_VALUE(j, '$.k'), NULLIF(JSON_VALUE(j, '$.letter_code'), ''), JSON_VALUE(j, '$.r'), "
          f"JSON_VALUE(j, '$.seen'), CURRENT_TIMESTAMP() FROM UNNEST(@js) AS j", run=run, d=thursday,
          js=[json.dumps(p, ensure_ascii=False) for p in part])
    alert = render_alert(thursday, picks, catalog)
    n_pick = sum(1 for p in picks if p["letter_code"])
    q(f"INSERT INTO `{T_PICKRUN}` (pick_run, thursday, mix, fallback, people, picked, nothing, alert_text, created_at) "
      f"VALUES (@run, @d, @mix, @fb, @n, @p, @z, @a, CURRENT_TIMESTAMP())", run=run, d=thursday, mix=",".join(mix),
      fb=bool(fallback), n=len(picks), p=n_pick, z=len(picks) - n_pick, a=alert)
    by = {}
    for p in picks:
        k = (p["edu_group"], p["letter_code"] or "-", p["r"])
        by[k] = by.get(k, 0) + 1
    in_force = latest_pick(q, thursday)[0]
    print("EDU_PICK " + json.dumps({"done": True, "pick_run": run, "in_force_for_the_date": in_force,
                                    "this_run_is_in_force": in_force == run, "history_snapshot": hs[0]["snapshot_id"],
                                    "history_newest_campaign": hs[0]["newest_sent_id"],
                                    "thursday": thursday, "mix": list(mix),
                                    "fallback": bool(fallback), "people": len(picks), "picked": n_pick,
                                    "nothing": len(picks) - n_pick, "alert_sent": False,
                                    "by_group": [list(k) + [v] for k, v in sorted(by.items())]}, ensure_ascii=False))
    print("EDU_ALERT_TEXT (rendered only, NOT sent)\n" + (alert or "(no alert: everybody has a letter)"))
    return 0


def sales_switches() -> dict:
    """The SALES sender's switches, read from the live job. They must be CLOSED: this path does not use them and an
    open one is a state nobody ordered. -> {} when closed as expected, else what is open / unreadable."""
    try:
        j = json.loads(subprocess.check_output(["gcloud", "run", "jobs", "describe", SALES_JOB, "--region", REGION,
                                                "--project", P, "--format=json"], timeout=60))
        env = {e["name"]: e.get("value") for e in
               j["spec"]["template"]["spec"]["template"]["spec"]["containers"][0].get("env", [])}
    except Exception as e:  # noqa: BLE001 - unreadable = not known to be closed
        return {"unreadable": f"{type(e).__name__}: {e}"[:120]}
    bad = {}
    if env.get("DRY_RUN") != "true":
        bad["DRY_RUN"] = env.get("DRY_RUN")
    if env.get("GLOBAL_SEND_ENABLED") != "false":
        bad["GLOBAL_SEND_ENABLED"] = env.get("GLOBAL_SEND_ENABLED")
    if env.get("ALLOW_SEND") in ("true", "True", "1"):
        bad["ALLOW_SEND"] = env.get("ALLOW_SEND")
    if env.get("SEND_UNLOCKED_BY"):
        bad["SEND_UNLOCKED_BY"] = env.get("SEND_UNLOCKED_BY")
    return bad


def run_job(mode, d, code, check_run="") -> str:
    out = subprocess.run(["gcloud", "run", "jobs", "execute", SEND_JOB, "--region", REGION, "--project", P, "--async",
                          "--update-env-vars", f"EDU_MODE={mode},EDU_DATE={d},EDU_LETTER={code},EDU_CHECK_RUN={check_run}",
                          "--format=value(metadata.name)"], capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise RuntimeError("job execute failed: " + (out.stderr or out.stdout)[-300:])
    return out.stdout.strip().splitlines()[-1]


def dash_row(status, what):
    try:
        import zoneinfo
        now = dt.datetime.now(zoneinfo.ZoneInfo("Europe/Riga")).strftime("%Y-%m-%d %H:%M")
        row = f"| {now} | {AGENT} | {status} | {what.replace('|', '/')} | nav zināma |\n"
        if not os.path.exists("/root/iso_drive.py"):
            subprocess.run(["gsutil", "-q", "cp", f"gs://{P}-iso-tools/iso_drive.py", "/root/iso_drive.py"],
                           check=True, timeout=60)
        r = subprocess.run([sys.executable, "/root/iso_drive.py", "append", DASH], input=row.encode(),
                           capture_output=True, timeout=90)
        ok = "VERIFIED ok=True" in (r.stdout.decode() + r.stderr.decode())
        print("EDU_DASH " + ("written " if ok else "NOT WRITTEN ") + row.strip())
        return ok
    except Exception as e:  # noqa: BLE001 - the dash row never decides anything
        print(f"EDU_DASH NOT WRITTEN ({type(e).__name__}: {e})")
        return False


def check(q, d, code, wait_s=110, dash=True) -> int:
    """THE CHECK. Writes exactly one GO or NO-GO record for (date, letter) and prints one EDU_CHECK line.
    Exit 0 = GO, 3 = NO-GO, 4 = the check itself broke (also recorded as NO-GO when a record can still be written)."""
    check_run = "chk-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    s = {"send_date": d, "letter_code": code, "check_run": check_run}
    try:
        L = s["letter"] = letter_row(q, d, code)
        s["stops"] = stops(q, d, code)
        execution = None
        if L and L.get("source_campaign_id"):
            execution = run_job("verify", d, code, check_run)               # runs while the tables are read
        pr = q(f"SELECT run_id, selfcheck_failed FROM `{M}.shadow_run_report` WHERE plan_date = @d "
               f"ORDER BY finished_at DESC LIMIT 1", d=d)
        s["plan_run"] = pr[0]["run_id"] if pr else None
        s["plan_selfcheck_failed"] = _i(pr[0]["selfcheck_failed"]) if pr else None
        w = q(f"SELECT status FROM `{M}.letter_fields_log` WHERE plan_date = @d ORDER BY finished_at DESC LIMIT 1", d=d)
        s["writer_status"] = w[0]["status"] if w else None
        sc = q(f"SELECT check_name, level, ok FROM `{M}.shadow_selfcheck` WHERE plan_date = @d", d=d)
        s["sendtime_rows"] = sum(r["check_name"].startswith("sendtime_") for r in sc)
        s["hard_failed"] = sorted({r["check_name"] for r in sc if r["level"] == "hard" and not _b(r["ok"])})
        s["gate_rows"] = int(q(f"SELECT COUNT(*) AS n FROM `{M}.shadow_akcija_audience` WHERE plan_date = @d "
                               f"AND run_id = @r", d=d, r=s["plan_run"] or "")[0]["n"])
        s["sales_switch_open"] = sales_switches()
        s["tracks_enabled"] = int(q(f"SELECT COUNTIF(enabled) AS n FROM `{M}.track_enabled`")[0]["n"])
        seg = q(f"SELECT COUNT(*) AS n, TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(built_at), MINUTE) AS age_min "
                f"FROM `{T_SEGMENT}`")[0]
        s["segment_rows"], s["segment_age_h"] = int(seg["n"]), (None if seg["age_min"] is None
                                                                else round(int(seg["age_min"]) / 60, 1))
        s["rule"] = (L or {}).get("audience_rule")
        s["rule_unknown"] = bool(L) and s["rule"] not in RULES
        if s["rule"] in EDU_ALL_RULES:
            s["inputs_stale"] = edu_all_stale(q)
        if L and s["plan_run"] and s["segment_rows"] and not s["rule_unknown"] and not s.get("inputs_stale"):
            s["audience"], s["funnel"] = build_audience(q, d, code, check_run, s["plan_run"], L["test_only"], s["rule"])
            bad = q(f"SELECT COUNT(*) - COUNT(DISTINCT a.email) AS dup, COUNTIF(s.email IS NOT NULL) AS supp "
                    f"FROM `{T_AUD}` a LEFT JOIN (SELECT DISTINCT LOWER(TRIM(email)) AS email "
                    f"FROM `{P}.business_marts.email_suppression_all`) s USING (email) "
                    f"WHERE a.send_date = @d AND a.letter_code = @c AND a.check_run = @cr", d=d, c=code, cr=check_run)[0]
            dup, supp = int(bad["dup"]), int(bad["supp"])
            s["audience_bad"] = None if not dup and (not supp or L["test_only"]) else {"duplicates": dup, "suppressed": supp}
            if not L["test_only"]:
                s["overlap"] = overlap_other_letters(q, d, code, check_run)
            if L["test_only"]:
                s["audience_emails"] = [r["email"] for r in q(
                    f"SELECT email FROM `{T_AUD}` WHERE send_date = @d AND letter_code = @c AND check_run = @cr",
                    d=d, c=code, cr=check_run)]
        deadline = time.time() + wait_s
        while execution and time.time() < deadline and not s.get("brevo"):
            b = q(f"SELECT * FROM `{T_BREVO}` WHERE check_run = @cr", cr=check_run)
            if b:
                s["brevo"] = b[0]
            else:
                time.sleep(6)
        s["verify_execution"] = execution
        b = s.get("brevo") or {}
        s["params_needed"] = b.get("param_mentions") is None or int(b.get("param_mentions") or 0) > 0
        if s.get("brevo") and s["params_needed"]:
            s["param_problems"], s["params_sha"], _ = param_gate(q, d, code)
        reasons = check_reasons(s)
        broke = False
    except Exception as e:  # noqa: BLE001 - a check that cannot finish is a NO-GO
        reasons, broke = [f"CHECK_BROKE {type(e).__name__}: {e}"[:300]], True
    kind = "NO-GO" if reasons else "GO"
    detail = {k: v for k, v in s.items() if k not in ("letter",)}
    detail["approved_sha256"] = ((s.get("letter") or {}).get("approved_sha256") or "")[:12]
    q(f"INSERT INTO `{T_GATE}` (send_date, letter_code, kind, check_run, reason, detail, written_at, written_by) "
      f"VALUES (@d, @c, @k, @cr, @r, @t, CURRENT_TIMESTAMP(), 'edu.check')", d=d, c=code, k=kind, cr=check_run,
      r="; ".join(reasons) or None, t=json.dumps(detail, ensure_ascii=False, default=str))
    back = q(f"SELECT kind FROM `{T_GATE}` WHERE check_run = @cr", cr=check_run)
    print("EDU_CHECK " + json.dumps({"date": d, "letter": code, "result": kind, "check_run": check_run, "rule": s.get("rule"),
                                     "audience": s.get("audience"), "funnel": s.get("funnel"), "reasons": reasons,
                                     "record_read_back": [r["kind"] for r in back]}, ensure_ascii=False))
    if dash:
        if kind == "GO":
            dash_row("STRĀDĀ", f"Izglītojošā vēstule {code} {d}: pārbaude OK (GO), auditorija {s.get('audience')}; "
                               f"dzinējs sūtīs")
        else:
            dash_row("KĻŪDA", f"Izglītojošā vēstule {code} {d}: NO-GO — {'; '.join(reasons)[:260]} — dzinējs NESŪTA; "
                              f"jāsūta rezerves kampaņa")
    return 4 if broke else (0 if kind == "GO" else 3)


def stop(q, d, code, by, why) -> int:
    """THE STOP. One record; from that moment no send of (date, letter) is possible, whatever GO exists.
    Exit 0 only when the record was read back."""
    q(f"INSERT INTO `{T_GATE}` (send_date, letter_code, kind, check_run, reason, detail, written_at, written_by) "
      f"VALUES (@d, @c, 'STOP', NULL, @r, NULL, CURRENT_TIMESTAMP(), @b)", d=d, c=code, r=why, b=by)
    n = stops(q, d, code)
    started = q(f"SELECT event, CAST(logged_at AS STRING) AS logged FROM `{T_LOG}` WHERE send_date = @d "
                f"AND letter_code = @c AND event IN ('SEND_STARTED', 'SENT') ORDER BY logged_at", d=d, c=code)
    print("EDU_STOP " + json.dumps({"date": d, "letter": code, "stop_records_read_back": n, "engine_send_possible": n == 0,
                                    "send_already_started": started}, ensure_ascii=False))
    return 0 if n >= 1 else 1


def status(q, d, code) -> int:
    out = {"letter": letter_row(q, d, code),
           "gate": q(f"SELECT kind, check_run, reason, CAST(written_at AS STRING) AS written, written_by FROM `{T_GATE}` "
                     f"WHERE send_date = @d AND letter_code = @c ORDER BY written_at", d=d, c=code),
           "sent_rows": int(q(f"SELECT COUNT(*) AS n FROM `{T_SENT}` WHERE send_date = @d AND letter_code = @c",
                              d=d, c=code)[0]["n"]),
           "log": q(f"SELECT CAST(logged_at AS STRING) AS logged, who, event, detail FROM `{T_LOG}` "
                    f"WHERE send_date = @d AND letter_code = @c ORDER BY logged_at", d=d, c=code)}
    print("EDU_STATUS " + json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return 0


# ------------------------------------------------------------------------------------------- campaign identity
def _brevo(method, path, payload=None):
    import campaign as C
    try:
        return C._call(method, path, payload)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Brevo {method} {path} -> {e.code}: {e.read().decode()[:300]}") from None


def _facts(camp) -> dict:
    import campaign as C
    html = camp.get("htmlContent") or ""
    return {"sha256": content_sha(camp.get("subject"), camp.get("previewText"), html),
            "placeholders": len(C.placeholder_hits(camp.get("subject"), camp.get("previewText"), html)),
            "unsubscribe_links": C.unsubscribe_links(html), "html_bytes": len(html.encode("utf-8")),
            "param_mentions": html.count("params.")}


def list_members(list_id) -> set:
    """Who IS in a Brevo list now (GET, 500 a page)."""
    out, offset = set(), 0
    while True:
        r = _brevo("GET", f"/contacts/lists/{int(list_id)}/contacts?limit=500&offset={offset}")
        page = [(c.get("email") or "").strip().lower() for c in r.get("contacts") or []]
        out |= {e for e in page if e}
        offset += 500
        if len(page) < 500:
            return out


def fill_list(q, d, code, who, name, emails, rounds=3, pause=6, settle_s=300) -> dict:
    """Create a Brevo list and put exactly these e-mails into it, 150 a call. THE SAME CODE for the size rehearsal
    and for the send. Creates no campaign and sends nothing.
    MEASURED 2026-10-07 (rehearsal lv_all, list 78): Brevo answered 'success' for all 5 374, yet the list's own
    counter (uniqueSubscribers) said 4 924 right after and 5 374 about three minutes later - Brevo settles a list
    behind the answer. A campaign sent into an unsettled list could miss people, so nothing is trusted but a read:
    the MEMBERS are read back, the missing are added again (up to `rounds` times), 'ok' is only who the list really
    holds, and the counter must reach that number (`settle_s` seconds at most) before the caller may go on."""
    t0 = time.time()
    asked = [e.strip().lower() for e in emails]
    list_id = int(_brevo("POST", "/contacts/lists", {"name": name, "folderId": 1})["id"])
    errors, calls, said_ok, history, todo, members = [], 0, 0, [], list(asked), set()
    for rnd in range(1, rounds + 1):
        for part in chunks(todo, ADD_CHUNK):
            calls += 1
            try:
                r = _brevo("POST", f"/contacts/lists/{list_id}/contacts/add", {"emails": part}).get("contacts") or {}
                said_ok += len(r.get("success") or [])
            except RuntimeError as e:
                errors.append(str(e)[:300])
        time.sleep(pause)
        members = list_members(list_id)
        todo = [e for e in asked if e not in members]
        history.append({"round": rnd, "in_list": len(members & set(asked)), "missing": len(todo)})
        if not todo:
            break
    ok = [e for e in asked if e in members]
    extra = sorted(members - set(asked))
    t1, counter, black = time.time(), None, None
    while True:                                            # the list's own counter, as a campaign would see it
        info = _brevo("GET", f"/contacts/lists/{list_id}")
        counter, black = info.get("uniqueSubscribers"), info.get("totalBlacklisted")
        if (counter or 0) >= len(ok) or time.time() - t1 > settle_s:
            break
        time.sleep(10)
    settled = (counter or 0) >= len(ok)
    out = {"list_id": list_id, "name": name, "asked": len(asked), "calls": calls, "brevo_said_added": said_ok,
           "added": len(ok), "not_added": len(asked) - len(ok),
           "not_added_pct": round(100.0 * (len(asked) - len(ok)) / max(len(asked), 1), 2), "limit_pct": ADD_FAIL_PCT,
           "rounds": history, "in_list_but_not_asked": len(extra), "counter": counter, "counter_blacklisted": black,
           "counter_settled": settled, "settle_seconds": round(time.time() - t1, 1),
           "seconds": round(time.time() - t0, 1),
           "call_errors": errors[:5], "not_added_examples": todo[:10]}
    log(q, d, code, who, "LIST_FILLED", out)
    return {**out, "ok": ok, "failed": todo, "extra": extra}       # callers: refuse unless counter_settled


def snapshot_lists(q, list_ids) -> dict:
    """What Brevo holds in these lists NOW -> mkt_control.edu_brevo_list (replaced per list). GET only."""
    out = {}
    for lid in list_ids:
        emails, offset = [], 0
        while True:
            r = _brevo("GET", f"/contacts/lists/{int(lid)}/contacts?limit=500&offset={offset}")
            page = [(c.get("email") or "").strip().lower() for c in r.get("contacts") or []]
            emails += [e for e in page if e]
            offset += 500
            if len(page) < 500:
                break
        q(f"DELETE FROM `{T_BLIST}` WHERE list_id = @l", l=int(lid))
        for part in chunks(sorted(set(emails)), 5000):
            q(f"INSERT INTO `{T_BLIST}` (list_id, email, fetched_at) SELECT @l, e, CURRENT_TIMESTAMP() "
              f"FROM UNNEST(@es) AS e", l=int(lid), es=part)
        out[int(lid)] = len(set(emails))
    return out


def rawkeys(ids) -> int:
    """GET only: which fields Brevo returns for these campaigns, and the keys of `params` when it returns them."""
    for cid in ids:
        c = _brevo("GET", f"/emailCampaigns/{int(cid)}")
        p = c.get("params")
        print("EDU_RAWKEYS " + json.dumps({"id": int(cid), "name": c.get("name"), "status": c.get("status"),
              "keys": sorted(c), "has_params": "params" in c, "params_type": type(p).__name__,
              "params_keys": sorted(p) if isinstance(p, dict) else None,
              "html_mentions_params": (c.get("htmlContent") or "").count("params."),
              "html_mentions_contact": (c.get("htmlContent") or "").count("contact."),
              "sha256": content_sha(c.get("subject"), c.get("previewText"), c.get("htmlContent")),
              "recipients": c.get("recipients")}, ensure_ascii=False))
    return 0


def _download(url) -> str:
    """The export file. Brevo's link may want the key; a refusal is reported with the host and the status."""
    import campaign as C
    last = None
    ua = {"User-Agent": "curl/8.5.0", "Accept": "*/*"}     # the file host refuses the default Python client name (403)
    for headers in (ua, dict(ua, **{"api-key": C.api_key()})):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as f:
                return f.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = f"download {urllib.parse.urlsplit(url).netloc}{urllib.parse.urlsplit(url).path[:40]} -> {e.code} {e.read()[:120]!r}"
    raise RuntimeError(last)


def history(q, budget_s=480) -> int:
    """Who received which campaign, refreshed from Brevo: for every SENT campaign the history does not hold yet, a
    recipients export (Brevo builds a file; nothing in the account changes) appended as a new snapshot. Then one
    edu_hist_state row: how many sent campaigns Brevo has and how many are still missing (0 = the pick may run)."""
    t0, snap = time.time(), "edu-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    have = {int(r["c"]) for r in q(f"SELECT DISTINCT campaign_id AS c FROM `{T_HIST}`")}
    sent, off = [], 0
    while True:
        r = _brevo("GET", f"/emailCampaigns?status=sent&limit=100&offset={off}&excludeHtmlContent=true&sort=desc")
        cs = r.get("campaigns") or []
        sent += [int(c["id"]) for c in cs]
        off += 100
        if len(cs) < 100:
            break
    added, rows_added, errors = [], 0, []
    for cid in sorted(set(sent) - have):
        if time.time() - t0 > budget_s:
            break
        try:
            pid = _brevo("POST", f"/emailCampaigns/{cid}/exportRecipients", {"recipientsType": "all"}).get("processId")
            url = None
            for _ in range(40):
                pr = _brevo("GET", f"/processes/{pid}")
                if pr.get("status") == "completed":
                    url = pr.get("export_url")
                    break
                time.sleep(3)
            if not url:
                raise RuntimeError("export not completed")
            emails = emails_of_export(_download(url))
            for part in chunks(emails, 5000):
                q(f"INSERT INTO `{T_HIST}` (snapshot_id, campaign_id, email) SELECT @s, CAST(@c AS INT64), e "
                  f"FROM UNNEST(@es) AS e", s=snap, c=str(cid), es=part)
            if not emails:                                  # a sent campaign nobody received still counts as covered
                q(f"INSERT INTO `{T_HIST}` (snapshot_id, campaign_id, email) VALUES (@s, CAST(@c AS INT64), NULL)",
                  s=snap, c=str(cid))
            added.append(cid)
            rows_added += len(emails)
        except Exception as e:  # noqa: BLE001 - a campaign that cannot be read stays missing and blocks the pick
            errors.append(f"{cid}: {type(e).__name__}: {e}"[:160])
    have |= set(added)
    missing = sorted(set(sent) - have)
    st = {"snapshot_id": snap, "sent_campaigns": len(set(sent)), "newest_sent_id": max(sent) if sent else None,
          "covered": len(set(sent) & have), "missing": len(missing), "added_campaigns": len(added), "added_rows": rows_added}
    q(f"INSERT INTO `{T_HSTATE}` (refreshed_at, snapshot_id, sent_campaigns, newest_sent_id, covered, missing, "
      f"added_campaigns, added_rows, detail) VALUES (CURRENT_TIMESTAMP(), @s, CAST(@n AS INT64), CAST(@mx AS INT64), "
      f"CAST(@cv AS INT64), CAST(@mi AS INT64), CAST(@ac AS INT64), CAST(@ar AS INT64), @dt)", s=snap,
      n=str(st["sent_campaigns"]), mx=str(st["newest_sent_id"] or 0), cv=str(st["covered"]), mi=str(st["missing"]),
      ac=str(st["added_campaigns"]), ar=str(rows_added),
      dt=json.dumps({"added": added, "missing": missing[:60], "errors": errors[:20]}))
    print("EDU_HISTORY " + json.dumps(dict(st, added=added, missing_ids=missing[:60], errors=errors[:10])))
    return 0 if not missing else 1


def rehearse(q, d, code, check_run, name) -> int:
    """SIZE REHEARSAL (MAIN 2026-10-07 13:36): the frozen audience of a check goes into a Brevo list with the send's
    own list filler. NO campaign is created and nothing is sent; the list stays, attached to nothing."""
    who = "edu.rehearse"
    aud = [r["email"] for r in q(f"SELECT email FROM `{T_AUD}` WHERE send_date = @d AND letter_code = @c "
                                 f"AND check_run = @cr ORDER BY email", d=d, c=code, cr=check_run)]
    if not aud or not name:
        print("EDU_REHEARSAL " + json.dumps({"done": False, "why": "no frozen audience for that check, or no list name"}))
        return 3
    lists = snapshot_lists(q, [int(x) for x in (os.environ.get("EDU_SNAPSHOT_LISTS") or f"{EE_LIST},{EN_LIST}").split(",")])
    f = fill_list(q, d, code, who, name, aud)
    back = _brevo("GET", f"/contacts/lists/{f['list_id']}")
    res = {k: v for k, v in f.items() if k not in ("ok", "failed", "extra")}
    res.update(done=True, check_run=check_run, list_read_back={k: back.get(k) for k in ("id", "name", "uniqueSubscribers",
               "totalSubscribers", "totalBlacklisted", "campaignStats")}, would_pass_2pct_rule=f["not_added_pct"] <= ADD_FAIL_PCT and f["counter_settled"] and not f["extra"],
               brevo_lists_snapshot=lists, campaign_created=False, sent=False)
    if f["failed"]:
        q(f"INSERT INTO `{T_LOG}` (send_date, letter_code, logged_at, who, event, detail) "
          f"SELECT @d, @c, CURRENT_TIMESTAMP(), @w, 'LIST_NOT_ADDED', e FROM UNNEST(@es) AS e",
          d=d, c=code, w=who, es=f["failed"][:5000])
    print("EDU_REHEARSAL " + json.dumps(res, ensure_ascii=False, default=str))
    return 0


def verify(q, d, code, check_run) -> int:
    """GET only: what Brevo holds NOW for the source campaign -> one mkt_control.edu_brevo row for this check."""
    L = letter_row(q, d, code)
    row = {"sid": (L or {}).get("source_campaign_id"), "sha": None, "st": None, "subj": None, "mod": None, "ph": None,
           "un": None, "hb": None, "err": None, "pm": None}
    try:
        camp = _brevo("GET", f"/emailCampaigns/{int(row['sid'])}")
        f = _facts(camp)
        row.update(sha=f["sha256"], st=camp.get("status"), subj=camp.get("subject"), mod=camp.get("modifiedAt"),
                   ph=f["placeholders"], un=f["unsubscribe_links"], hb=f["html_bytes"], pm=f["param_mentions"])
    except Exception as e:  # noqa: BLE001 - an unread letter is not an approved one
        row["err"] = f"{type(e).__name__}: {e}"[:300]
    q(f"INSERT INTO `{T_BREVO}` (send_date, letter_code, check_run, source_campaign_id, sha256, status, subject, "
      f"modified_at, placeholders, unsubscribe_links, html_bytes, error, checked_at, param_mentions) "
      f"VALUES (@d, @c, @cr, CAST(@sid AS INT64), @sha, @st, @subj, @mod, CAST(@ph AS INT64), CAST(@un AS INT64), "
      f"CAST(@hb AS INT64), @err, CURRENT_TIMESTAMP(), CAST(@pm AS INT64))", d=d, c=code, cr=check_run,
      **{k: (None if v is None else str(v)) for k, v in row.items()})
    print("EDU_VERIFY " + json.dumps({"date": d, "letter": code, "check_run": check_run, **row}, ensure_ascii=False))
    return 0 if not row["err"] else 1


def send(q, d, code, now=None) -> int:
    """THE SEND. Exit 0 = sent, 3 = refused (nothing was sent), 5 = failed after sendNow was called."""
    now = now or dt.datetime.now(dt.timezone.utc)
    who = "edu.send"
    L = letter_row(q, d, code)
    gates = q(f"SELECT kind, check_run, detail, TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), written_at, MINUTE) AS age_min "
              f"FROM `{T_GATE}` WHERE send_date = @d AND letter_code = @c AND kind IN ('GO', 'NO-GO') "
              f"ORDER BY written_at DESC LIMIT 1", d=d, c=code)
    s = {"letter": L, "send_date": d, "today": riga(now).date().isoformat(), "stops": stops(q, d, code),
         "last_gate_kind": gates[0]["kind"] if gates else None, "go": None,
         "started": int(q(f"SELECT COUNT(*) AS n FROM `{T_LOG}` WHERE send_date = @d AND letter_code = @c "
                          f"AND event = 'SEND_STARTED'", d=d, c=code)[0]["n"])}
    aud = []
    if gates and gates[0]["kind"] == "GO":
        g = gates[0]
        s["go"] = {"check_run": g["check_run"], "age_min": _i(g["age_min"]),
                   "audience": (json.loads(g["detail"] or "{}")).get("audience"),
                   "params_sha": (json.loads(g["detail"] or "{}")).get("params_sha")}
        aud = q(f"SELECT email, master_key FROM `{T_AUD}` WHERE send_date = @d AND letter_code = @c AND check_run = @cr "
                f"ORDER BY email", d=d, c=code, cr=g["check_run"])
        s["audience"] = len(aud)
        if L and L["test_only"]:
            s["audience_emails"] = [a["email"] for a in aud]
    if s["go"] and L and not L["test_only"]:
        s["already_got"] = overlap_other_letters(q, d, code, s["go"]["check_run"])
    refusals = send_refusals(s)
    if refusals:
        log(q, d, code, who, "SEND_REFUSED", {"reasons": refusals, "nothing_sent": True})
        print("EDU_SEND " + json.dumps({"date": d, "letter": code, "sent": False, "reasons": refusals}))
        return 3
    check_run, test_only = s["go"]["check_run"], L["test_only"]
    source = _brevo("GET", f"/emailCampaigns/{L['source_campaign_id']}")
    f = _facts(source)
    refusals = content_refusals(L, source, f["placeholders"], f["unsubscribe_links"])
    params = None
    if f["param_mentions"]:                                    # the letter has product slots: the values of the GO or no send
        rows, today = read_params(q, d, code)
        if not param_problems(rows, today):
            params = {r["param_key"]: r["param_value"] for r in rows}
        refusals += send_refusals_params(s["go"].get("params_sha"), params)
    if refusals:
        log(q, d, code, who, "SEND_REFUSED", {"reasons": refusals, "nothing_sent": True})
        print("EDU_SEND " + json.dumps({"date": d, "letter": code, "sent": False, "reasons": refusals}))
        return 3
    # one send per letter and date: the marker is written BEFORE Brevo is touched and never removed
    log(q, d, code, who, "SEND_STARTED", {"check_run": check_run, "audience": len(aud), "test_only": test_only})
    if int(q(f"SELECT COUNT(*) AS n FROM `{T_LOG}` WHERE send_date = @d AND letter_code = @c AND event = 'SEND_STARTED'",
             d=d, c=code)[0]["n"]) != 1:
        log(q, d, code, who, "SEND_REFUSED", {"reasons": ["TWO_STARTS_AT_ONCE"], "nothing_sent": True})
        return 3
    campaign_id = list_id = None
    try:
        tag = f"{code} {d}" + (" TESTS" if test_only else "")
        f = fill_list(q, d, code, who, f"ENGINE EDU {tag} {check_run[-6:]}", [a["email"] for a in aud])
        list_id, ok, fail_pct = f["list_id"], f["ok"], f["not_added_pct"]
        if f["extra"]:
            raise RuntimeError(f"LIST_HOLDS_ADDRESSES_NOBODY_ASKED_FOR n={len(f['extra'])}")
        if not f["counter_settled"]:
            raise RuntimeError(f"LIST_COUNTER_NOT_SETTLED counter={f['counter']} members={len(ok)} after {f['settle_seconds']} s")
        if not ok or fail_pct > ADD_FAIL_PCT or (test_only and ok != [TEST_RECIPIENT]):
            raise RuntimeError(f"LIST_NOT_FILLED added={len(ok)} of {len(aud)} ({fail_pct:.1f}% missing, limit {ADD_FAIL_PCT}%)")
        excl = exclusion_lists(test_only)
        created = _brevo("POST", "/emailCampaigns", campaign_payload(source, f"ENGINE · EDU · {tag}", list_id, excl,
                                                                     params))
        campaign_id = int(created["id"])
        mine = _brevo("GET", f"/emailCampaigns/{campaign_id}")
        rec = mine.get("recipients") or {}
        fm = _facts(mine)
        problems = content_refusals(L, mine, fm["placeholders"], fm["unsubscribe_links"])
        if sorted(rec.get("lists") or []) != [list_id] or sorted(rec.get("exclusionLists") or []) != sorted(excl):
            problems.append(f"RECIPIENTS_READ_BACK_DIFFER lists={rec.get('lists')} excl={rec.get('exclusionLists')}")
        for k in ("header", "footer", "mirrorActive", "replyTo"):         # what Brevo adds around the HTML
            if mine.get(k) != source.get(k):
                problems.append(f"COPY_DIFFERS_IN_{k} engine={str(mine.get(k))[:40]} source={str(source.get(k))[:40]}")
        if (mine.get("sender") or {}).get("id") != (source.get("sender") or {}).get("id"):
            problems.append("COPY_DIFFERS_IN_sender")
        if mine.get("status") != "draft":
            problems.append(f"ENGINE_CAMPAIGN_STATUS={mine.get('status')}")
        if stops(q, d, code):                                              # the last look before the one call
            problems.append("STOPPED")
        if problems:
            raise RuntimeError("; ".join(problems))
    except Exception as e:  # noqa: BLE001 - before sendNow: nothing went out; the engine draft is removed
        removed = None
        if campaign_id:
            try:
                _brevo("DELETE", f"/emailCampaigns/{campaign_id}")
                removed = True
            except Exception:  # noqa: BLE001
                removed = False
        log(q, d, code, who, "SEND_REFUSED", {"reasons": [str(e)[:400]], "nothing_sent": True, "list_id": list_id,
                                              "engine_campaign_id": campaign_id, "engine_draft_removed": removed})
        print("EDU_SEND " + json.dumps({"date": d, "letter": code, "sent": False, "reasons": [str(e)[:400]]}))
        return 3
    _brevo("POST", f"/emailCampaigns/{campaign_id}/sendNow")              # THE ONE SEND CALL
    try:
        log(q, d, code, who, "SENT", {"campaign_id": campaign_id, "list_id": list_id, "people": len(ok),
                                      "exclusion_lists": excl, "source_campaign_id": L["source_campaign_id"]})
        q(f"INSERT INTO `{T_SENT}` (send_date, letter_code, campaign_id, source_campaign_id, brevo_list_id, master_key, "
          f"email, sent_at, check_run, test_only) SELECT @d, @c, @cid, @sid, @lid, master_key, email, "
          f"CURRENT_TIMESTAMP(), @cr, @t FROM `{T_AUD}` WHERE send_date = @d AND letter_code = @c AND check_run = @cr "
          f"AND email IN UNNEST(@ok)", d=d, c=code, cid=campaign_id, sid=L["source_campaign_id"], lid=list_id,
          cr=check_run, t=test_only, ok=ok)
        n = int(q(f"SELECT COUNT(*) AS n FROM `{T_SENT}` WHERE send_date = @d AND letter_code = @c", d=d, c=code)[0]["n"])
        after = _brevo("GET", f"/emailCampaigns/{campaign_id}")
        log(q, d, code, who, "RECORDED", {"edu_sent_rows": n, "brevo_status": after.get("status")})
        print("EDU_SEND " + json.dumps({"date": d, "letter": code, "sent": True, "campaign_id": campaign_id,
                                        "list_id": list_id, "people": len(ok), "edu_sent_rows": n,
                                        "brevo_status": after.get("status")}))
        return 0
    except Exception as e:  # noqa: BLE001 - the letter IS out; only the record is incomplete
        print("EDU_SEND " + json.dumps({"date": d, "letter": code, "sent": True, "campaign_id": campaign_id,
                                        "RECORD_INCOMPLETE": f"{type(e).__name__}: {e}"[:300]}))
        return 5


def main(argv) -> int:
    mode = (argv[0] if argv else os.environ.get("EDU_MODE") or "").strip()
    args = dict(a.lstrip("-").split("=", 1) for a in argv[1:] if "=" in a)
    d = (args.get("date") or os.environ.get("EDU_DATE") or "").strip()
    code = (args.get("letter") or os.environ.get("EDU_LETTER") or "").strip()
    if mode in ("setup", "prepare", "inputs", "coldreg", "history"):
        return {"setup": setup, "prepare": prepare, "inputs": inputs, "coldreg": coldreg, "history": history}[mode](query)
    if mode == "rawkeys":
        return rawkeys([x for x in (os.environ.get("EDU_IDS") or args.get("ids") or "").split(",") if x])
    if mode == "pick":                                                     # dry: the Tuesday selection for Thursday d
        dt.date.fromisoformat(d)
        mix = tuple(x for x in (args.get("mix") or "").split(",") if x) or EDU_MIX
        if any(x not in ("own", "other") for x in mix):
            print("mix is a comma list of own|other"); return 2
        return pick(query, d, mix, args.get("fallback", "0") == "1")
    if mode == "snapshot":                                                 # GET only: Brevo lists -> edu_brevo_list
        print("EDU_SNAPSHOT " + json.dumps(snapshot_lists(query, [int(x) for x in os.environ["EDU_SNAPSHOT_LISTS"].split(",")])))
        return 0
    if mode not in ("check", "stop", "status", "verify", "send", "rehearse") or not code:
        print("usage: edu.py setup | prepare | check|stop|status --date=YYYY-MM-DD --letter=CODE [--by=.. --why=..] ; "
              "job: EDU_MODE=verify|send EDU_DATE EDU_LETTER [EDU_CHECK_RUN]")
        return 2
    dt.date.fromisoformat(d)                                              # a bad date never reaches a query
    if mode == "check":
        return check(query, d, code, dash=args.get("dash", "1") != "0")
    if mode == "stop":
        if not args.get("by") or not args.get("why"):
            print("stop needs --by= and --why="); return 2
        return stop(query, d, code, args["by"], args["why"])
    if mode == "status":
        return status(query, d, code)
    if mode == "rehearse":
        return rehearse(query, d, code, os.environ.get("EDU_CHECK_RUN") or args.get("check_run") or "",
                        os.environ.get("EDU_LIST_NAME") or args.get("name") or "")
    if mode == "verify":
        return verify(query, d, code, os.environ.get("EDU_CHECK_RUN") or args.get("check_run") or "manual")
    return send(query, d, code)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
