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
raivis@alenda.lv ONLY. The content is the letter_fields row of ONE real member of that type's plan of today who passes
every member gate (L5 L7 L8 L9) - copied onto raivis@alenda.lv's Brevo attributes (his previous values are recorded in
mkt_control.send_day_proof and put back by `--proof-restore`). The list holds that one address and nothing else
(checked before the campaign is created). send_log and send_state_advance carry master_key 'TEST:raivis@alenda.lv',
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

import pd_record
import presend as G
import send_path as SP
import sequence as S

P = "jaunais-za-aizv04022026"
M = f"{P}.mkt_control"
T_PLAN, T_LF = f"{M}.shadow_send_plan", f"{M}.letter_fields"
T_CAP, T_AUD, T_RUN = f"{M}.send_cap", f"{M}.send_day_audience", f"{M}.send_day_run"
T_TRACK, T_APPROVAL = f"{M}.track_enabled", f"{M}.template_approval"
T_LOG, T_ADV, T_STATE = f"{M}.send_log", f"{M}.send_state_advance", f"{M}.contact_sequence_state"
T_PDW, T_SUPP = f"{M}.shadow_pd_writes", f"{P}.business_marts.email_suppression_all"
RIGA = zoneinfo.ZoneInfo("Europe/Riga")
APPROVER = "Raivis"
TUESDAY, THURSDAY = 1, 3                     # date.weekday(): Monday = 0
EDUCATIONAL_TYPES = frozenset()              # the new plan holds no educational letter today (Thursday = block 2)
SUPPRESSION_LIST_ID = 4
LIST_FOLDER_ID = 1
ADD_CHUNK = 150
DUE_LOOKBACK_DAYS = 90
TEST_RECIPIENT = "raivis@alenda.lv"
TEST_KEY = "TEST:" + TEST_RECIPIENT
T_PROOF = f"{M}.send_day_proof"

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
  restored_at TIMESTAMP)"""]


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
    def member_refusal_fn(self, et, rung_of, data_date, cands, l11=True):
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
            if not l11:
                return None
            attrs = self.contact_attrs(c["email"])
            if attrs is None:
                return "L11_CONTACT_UNREADABLE"
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
    def run(self, send_date: dt.date, mode="auto", caps_override=None, only=None) -> dict:
        live = mode != "dry" and l1_open(self.env)
        if caps_override is not None and live:
            raise SP.SendLocked([("D3", "a caps override exists only for a DRY rehearsal")])
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
        for t in self.types_of(plan_run, send_date):
            et = t["email_type"]
            if only and et not in only:
                continue
            res = self._one(t, send_date, plan_date, plan_run, live, cap_rows, track_rows, appr_rows, caps_override)
            result["types"].append(res)
        return result

    def _one(self, t, d, plan_date, plan_run, live, cap_rows, track_rows, appr_rows, caps_override):
        et, tid, track = t["email_type"], _i(t["template_id"]), t["track"]
        res = {"email_type": et, "template_id": tid, "track": track, "candidates": 0, "chosen": 0, "refused": {},
               "type_refusal": None, "cap": None, "would_get": []}
        if not is_send_day(et, d):
            res["type_refusal"] = f"NOT_A_SEND_DAY ({d:%a}; {et} goes on weekday {send_weekday(et)})"
            return self._record(res, d, live, [], {})
        cap = int((caps_override or {}).get(et, 0)) if caps_override is not None else cap_of(cap_rows, et, d)
        res["cap"] = cap
        d1 = standing_approval(et, track, tid, track_rows, appr_rows, self.live_hash)
        cands = [dict(r) for r in self.q(candidates_sql(plan_run, d, et))]
        res["candidates"] = len(cands)
        if d1:
            res["type_refusal"] = "D1 " + "; ".join(d1)
            if live and cap > 0:
                self.dash("KĻŪDA", f"Sūtīšanas diena {d}: {et} atteikts - " + "; ".join(d1) + " - nekas nav sūtīts")
        elif cap <= 0:
            res["type_refusal"] = "D3 NO_CAP (0)"
        refuse = self.member_refusal_fn(et, lambda c: int(c.get("rung") or 0), plan_date, cands) if cands \
            else (lambda c: None)
        # the member selection is evaluated and recorded in DRY even for a refused type (who WOULD get it); LIVE sends
        # nothing for a refused type and reads no contact for it
        chosen, decisions = select_audience(cands, 0 if (live and res["type_refusal"]) else cap, refuse)
        rungs = {int(c.get("rung") or 0) for c in chosen}
        if len(rungs) > 1 and not res["type_refusal"]:
            res["type_refusal"] = f"MIXED_RUNG {sorted(rungs)} - one campaign carries one rung"
        res["chosen"] = len(chosen)
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
            res.update(self._send(et, tid, track, d, chosen, plan_run))
        return self._record(res, d, live, cands, decisions)

    # -- LIVE: list, campaign, dispatch
    def _send(self, et, tid, track, d, chosen, plan_run, alias=None):
        batch = f"{self.run_id}-{et}"
        emails = [c["email"] for c in chosen]
        lst = self.fill_list(f"SD {d} {et} {batch}", emails)
        if alias is not None and self.list_members(lst["list_id"]) != {TEST_RECIPIENT}:
            raise SP.SendLocked([("PROOF", f"list {lst['list_id']} does not hold exactly {TEST_RECIPIENT}")])
        if sorted(lst["ok"]) != sorted(emails) or not lst["settled"]:
            raise SP.SendLocked([("LIST", f"list {lst['list_id']} holds {len(lst['ok'])}/{len(emails)}, "
                                          f"settled={lst['settled']} - refused")])
        camp = self.create_campaign(et, tid, d, lst["list_id"], batch)
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
                          offered_sink=None, now=self.now)
        return {"batch_id": batch, "list_id": lst["list_id"], "campaign_id": camp, "sent": out["sent"],
                "pd_writes": out["pd_writes"], "pd_activity_ids": list(self.pd_ids)}

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

    def proof(self, et, d, pd_person, wait_s=600) -> dict:
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
        src = next((c for c in cands if gate(c) is None), None)
        if src is None:
            raise SP.SendLocked([("PROOF", f"no member of {et} passes the member gates today")])
        frow = self.full_lf_rows(plan_date, [src["email"]]).get(src["email"])
        if not frow:
            raise SP.SendLocked([("PROOF", f"no letter_fields row for the source member of {et}")])
        want = SP.letter_params(frow)
        types = self.attr_types()
        before = {k: v for k, v in (self.contact_attrs(TEST_RECIPIENT) or {}).items() if k in want}
        payload = self.typed(want, types)
        self.brevo("PUT", f"/contacts/{TEST_RECIPIENT}", {"attributes": payload})
        self.q(f"INSERT INTO `{T_PROOF}` (run_id, email_type, source_master_key, source_email, raivis_before, written, "
               f"created_at) VALUES ({_s(self.run_id)}, {_s(et)}, {_s(src['master_key'])}, {_s(src['email'])}, "
               f"{_s(json.dumps(before, default=str))}, {_s(json.dumps(payload, default=str))}, CURRENT_TIMESTAMP())")
        time.sleep(5)
        diff = attrs_equal({k: v for k, v in want.items() if k in types}, self.contact_attrs(TEST_RECIPIENT) or {})
        if diff:
            raise SP.SendLocked([("L11", f"{TEST_RECIPIENT} attributes differ after the write: {diff[:8]}")])
        me = {"master_key": TEST_KEY, "email": TEST_RECIPIENT, "person_id": int(pd_person), "rung": src.get("rung"),
              "reason": f"PROOF of {et} with the letter of {src['master_key']}"}
        out = self._send(et, tid, track, d, [me], plan_run, alias={TEST_KEY: src["master_key"]})
        t0, camp = time.time(), {}
        while time.time() - t0 < wait_s:
            camp = self.brevo("GET", f"/emailCampaigns/{out['campaign_id']}", None) or {}
            if camp.get("status") == "sent":
                break
            time.sleep(15)
        self.q(f"UPDATE `{T_PROOF}` SET campaign_id = {int(out['campaign_id'])}, list_id = {int(out['list_id'])} "
               f"WHERE run_id = {_s(self.run_id)}")
        html = camp.get("htmlContent") or ""
        attrs = self.contact_attrs(TEST_RECIPIENT) or {}
        return {**out, "email_type": et, "template_id": tid, "source_master_key": src["master_key"],
                "brevo_status": camp.get("status"), "subject": camp.get("subject"),
                "unfilled": unfilled_marks(html + " " + (camp.get("subject") or ""), attrs),
                "stats": ((camp.get("statistics") or {}).get("globalStats") or {})}

    def proof_restore(self, run_ids):
        """Put raivis@alenda.lv's attributes back to what they were before the FIRST proof of these runs."""
        rows = self.q(f"SELECT run_id, raivis_before, written FROM `{T_PROOF}` WHERE run_id IN "
                      f"({','.join(_s(r) for r in run_ids)}) ORDER BY created_at")
        before, keys = {}, set()
        for r in rows:
            for k, v in json.loads(r["raivis_before"] or "{}").items():
                before.setdefault(k, v)
            keys |= set(json.loads(r["written"] or "{}"))
        types = self.attr_types()
        restore = {k: before.get(k, "" if types.get(k) == "text" else None) for k in keys}
        restore = {k: v for k, v in restore.items() if v is not None}
        self.brevo("PUT", f"/contacts/{TEST_RECIPIENT}", {"attributes": restore})
        self.q(f"UPDATE `{T_PROOF}` SET restored_at = CURRENT_TIMESTAMP() WHERE run_id IN "
               f"({','.join(_s(r) for r in run_ids)})")
        return {"restored": sorted(restore), "left_numeric_or_boolean": sorted(keys - set(restore))}

    def fill_list(self, name, emails, rounds=3, pause=6, settle_s=300):
        lid = int(self.brevo("POST", "/contacts/lists", {"name": name[:120], "folderId": LIST_FOLDER_ID})["id"])
        todo, members = list(emails), set()
        for _ in range(rounds):
            for i in range(0, len(todo), ADD_CHUNK):
                self.brevo("POST", f"/contacts/lists/{lid}/contacts/add", {"emails": todo[i:i + ADD_CHUNK]})
            time.sleep(pause)
            members = self.list_members(lid)
            todo = [e for e in emails if e not in members]
            if not todo:
                break
        t0 = time.time()
        while True:
            n = (self.brevo("GET", f"/contacts/lists/{lid}", None) or {}).get("uniqueSubscribers") or 0
            if n >= len(emails) or time.time() - t0 > settle_s:
                break
            time.sleep(10)
        extra = members - set(emails)
        return {"list_id": lid, "ok": [e for e in emails if e in members], "settled": n >= len(emails) and not extra}

    def list_members(self, lid):
        out, off = set(), 0
        while True:
            r = self.brevo("GET", f"/contacts/lists/{lid}/contacts?limit=500&offset={off}", None) or {}
            page = [(c.get("email") or "").strip().lower() for c in r.get("contacts") or []]
            out |= set(page)
            off += 500
            if len(page) < 500:
                return out

    def create_campaign(self, et, tid, d, list_id, batch):
        import campaign as CAMP
        tpl = self.brevo("GET", f"/smtp/templates/{int(tid)}", None) or {}
        week = f"{d.isocalendar()[0]}-w{d.isocalendar()[1]:02d}"
        html = tpl.get("htmlContent") or ""
        if CAMP.UTM_WEEK_MARKER in html:              # the UTM seam where the template carries it (its own refusals)
            html, _pairs = CAMP.apply_utm_week(html, week)
        # else: byte for byte - the approved letters 179 / 180 / 244 carry no utm link at all (measured 2026-10-09);
        # their D1 hash is of exactly these bytes
        payload = {"name": f"SD {d} {et} ({batch})"[:200], "subject": tpl.get("subject") or "",
                   "sender": {"id": CAMP.SENDER_ID}, "replyTo": "info@tiktik.lv", "htmlContent": html,
                   "recipients": {"listIds": [int(list_id)], "exclusionListIds": [SUPPRESSION_LIST_ID]},
                   "inlineImageActivation": False}
        return int(self.brevo("POST", "/emailCampaigns", payload)["id"])

    # -- writers
    def write_send_log(self, rows):
        if not rows:
            return
        vals = ",".join(
            "(" + ",".join([_s(r["master_key"]), _s(r["email"]), str(int(r["campaign_id"])),
                            _n(r.get("brevo_list_id")), _s(r["email_type"]), _n(r.get("template_id")),
                            _s(r.get("track")), "NULL", _n(r.get("rung")), f"TIMESTAMP {_s(r['sent_at'])}",
                            _s(r["source"]), _s(r["run_id"]), "NULL", _s(r.get("utm_campaign"))]) + ")" for r in rows)
        self.q(f"INSERT INTO `{T_LOG}` (master_key, email, campaign_id, brevo_list_id, email_type, template_id, track, "
               f"step, rung, sent_at, source, run_id, brevo_message_id, utm_campaign) VALUES {vals}")

    def advance(self, mk, et, on, rung, batch, camp):
        self.q(f"INSERT INTO `{T_ADV}` VALUES ({_s(mk)}, {_s(et)}, DATE '{on}', {int(rung or 0)}, {_s(batch)}, "
               f"{int(camp)}, CURRENT_TIMESTAMP())")
        self.q(f"UPDATE `{T_STATE}` SET last_email_type = {_s(et)}, last_sent_on = DATE '{on}', step = IFNULL(step, 0) + 1, "
               f"last_send_source = 'engine_live' WHERE master_key = {_s(mk)}")

    def _record(self, res, d, live, cands, decisions):
        mode = "live" if live else "dry"
        self.q(f"INSERT INTO `{T_RUN}` VALUES ({_s(self.run_id)}, DATE '{d}', {_s(mode)}, {_s(res['email_type'])}, "
               f"{_n(res['template_id'])}, {_s(res['track'])}, {_n(res.get('cap'))}, {int(res['candidates'])}, "
               f"{int(res['chosen'])}, {_s(json.dumps(res['refused'], sort_keys=True))}, {_s(res['type_refusal'])}, "
               f"{_s(res.get('batch_id'))}, {_n(res.get('list_id'))}, {_n(res.get('campaign_id'))}, "
               f"{_n(res.get('sent'))}, {_n(res.get('pd_writes'))}, NULL, CURRENT_TIMESTAMP())")
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
        if method != "GET" and not l1_open(os.environ):
            raise SP.SendLocked([("L1", f"Brevo {method} {path} refused while L1 is closed")])
        return CAMP._call(method, path, payload)

    def pd_post(record):
        import urllib.request
        import gsecret
        tok = gsecret.read("PIPEDRIVE_API_TOKEN")
        body = {"subject": record["subject"], "type": record["type_key"], "due_date": record["due_date"],
                "done": 1 if record["done"] else 0, "note": record["note"]}
        if record.get("target_person_id"):
            body["person_id"] = int(record["target_person_id"])
        req = urllib.request.Request(f"https://api.pipedrive.com/v1/activities?api_token={tok}",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()).get("data", {}).get("id")

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
    if args.get("proof"):
        q, wh, brevo, pd_post, dash = _prod()
        day = Day(q, wh, brevo, pd_post, dash, os.environ)
        try:
            res = day.proof(args["proof"], d, int(args["pd-person"]))
        except SP.SendLocked as e:
            print("PROOF_REFUSED " + str(e)[:800])
            return 3
        print("PROOF " + json.dumps({"run_id": day.run_id, **res}, ensure_ascii=False, default=str)[:20000])
        return 0
    only = set(args["only"].split(",")) if args.get("only") else None
    q, wh, brevo, pd_post, dash = _prod()
    day = Day(q, wh, brevo, pd_post, dash, os.environ)
    try:
        res = day.run(d, mode=mode, caps_override=caps, only=only)
    except SP.SendLocked as e:
        print("SEND_DAY_REFUSED " + str(e)[:800])
        dash("KĻŪDA", f"Sūtīšanas diena {d}: atteikts - {str(e)[:250]} - nekas nav sūtīts")
        return 3
    print("SEND_DAY " + json.dumps(res, ensure_ascii=False, default=str)[:20000])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
