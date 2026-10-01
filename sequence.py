"""Per-contact sequence + rung state (Sūtīšanas dzinējs, D1). Pure: no clock, no client.

ONE WRITER. mkt_control.contact_sequence_state is written only by this module's caller
(sequence_job.py). Step lives ONLY here (MAIN 2026-09-25 17:08, D-2): customer_lifecycle
*_step is ignored; lifecycle supplies stage + dates.

INTERFACE email_type v1 (MAIN 2026-09-25 17:08, identical wording to Vēstuļu šabloni):
  welcome_1=229 · reorder_1=179 · reorder_2=230 · reorder_3=231 · winback_1=180 ·
  winback_2=232 · winback_3=233 · lost_quarterly=234 · active_xsell=235 ·
  akcija_weekly=236 (list campaign, map row stays NULL)
The engine emits exactly these names. "rhythm_next" is retired -> active_xsell.
Template ids are READ from mkt_control.email_template_map at plan time (Vēstuļu šabloni owns
it); INTERFACE_V1 below is only used to report a disagreement, never to choose a template.

LADDER (contract v2.8 P5 + Raivis 24.09 18:04/18:58, 25.09 09:37):
  reorder_* -> OFFER_RUNG 0 always.
  winback_1..3 and lost_quarterly -> rung 1/2/3 (the price itself comes from the PAP engine).
  The price drops at most ONCE per calendar month. Contract v2.8.1 A2: this rule is OWNED HERE, via
  State.rung_month (= contact_sequence_state.rung_month): advance() keeps the rung when rung_month is
  the due month; record_sent() refuses a second rung change in one month and any change that is not
  one step up. reorder_* (P5) never start, advance or clear a rung.
  LADDER POLICY v1 (Raivis 2026-09-28, rules L1-L8, contract sha 326480dce080) replaces the old
  unconfirmed defaults: rung by stage (winback_1/2/3 = 1/2/3), lost keeps the last reached rung, purchase
  -> rung 0, rung_cap 1 for 12 months after a purchase inside a rung-2/3 window, one walk per cycle,
  doubled reorder -> winback_1 gap once reorder has worked. See planned_rung() and ladder_marks().

CADENCE v1 (Raivis 2026-09-30 19:11, contract K1-K7) replaces the reorder_1..3 -> winback_1 weekly chain:
  K1 reorder_due = ONE letter (reorder_1); reorder_2/3 are never planned (names kept for history only).
  K2/K4 after reorder_1 and after every price episode: 4 weeks with no lifecycle letter (akcija only) ->
     the next lifecycle letter is due no earlier than last + 35 d (QUIET_GAP_DAYS).
  K3 a price episode = E1 (winback_r) then E2 (winback_r_e2) exactly 7 d later, same rung, same
     OFFER_VALID_UNTIL = E1 send + 13. An E2 that was not sent on E1+7 is skipped, never sent late.
  K5 next episode = rung + 1 (E1 -> E1 = 42 d); after the rung-3 episode the contact waits for stage lost
     -> lost_quarterly as before (L3 rung). K6 a purchase ends the cycle; L6/L8 unchanged.
  Any two lifecycle letters other than an E1 -> E2 pair are >= MIN_GAP_ANY_DAYS apart (never consecutive weeks).
  E2 templates are not written yet: E2 types have NO template id and are held E2_TEMPLATE_PENDING, never a fallback.
"""
from __future__ import annotations

import dataclasses as dc
import datetime as dt

INTERFACE_V1 = {
    "welcome_1": 229, "reorder_1": 179, "reorder_2": 230, "reorder_3": 231,
    "winback_1": 180, "winback_2": 232, "winback_3": 233, "lost_quarterly": 234,
    "active_xsell": 235, "akcija_weekly": 236,
}
# CADENCE v1 K3/K7: E1 of rung r = winback_r (180/232/233 are the E1 candidates); E2 = winback_r_e2, whose texts
# Vēstuļu šabloni is still writing -> no template id exists; the planner must hold them, never fall back.
E1_BY_RUNG = {1: "winback_1", 2: "winback_2", 3: "winback_3"}
E2_BY_RUNG = {r: f"winback_{r}_e2" for r in (1, 2, 3)}
E1_TYPES, E2_TYPES = set(E1_BY_RUNG.values()), set(E2_BY_RUNG.values())
RUNG_OF = {**{v: k for k, v in E1_BY_RUNG.items()}, **{v: k for k, v in E2_BY_RUNG.items()}}
HOLD_E2_TEMPLATE = "E2_TEMPLATE_PENDING"
# G-EN (Raivis 2026-09-30 17:47): LV first, then EN in bulk. Until EN letters exist, an EN contact never receives an
# LV engine letter. EN contact = Brevo list 46 OR attribute LANGUAGE = en (on any address of the person). The hold
# replaces every "letter planned" outcome; holds that mean "no letter at all" stay as they are. Nothing is sent and
# no state advances (state moves only in record_sent, on a real send).
HOLD_EN = "EN_PENDING"
EN_KEEPS = {"SUPPRESSED", "BLOCKED_OR_UNKNOWN", "LADDER_NO_RESTART", "SEQUENCE_DONE"}


def language_hold(email_type, hold, is_en: bool):
    """G-EN guard: the hold reason after the language check (hold None = would send)."""
    if is_en and email_type and hold not in EN_KEEPS:
        return HOLD_EN
    return hold
CADENCE = "cadence-v1 (Raivis 2026-09-30 19:11, K1-K7)"
QUIET_WEEKS = 4
QUIET_GAP_DAYS = 7 * (QUIET_WEEKS + 1)   # K2/K4: letter in week n -> weeks n+1..n+4 akcija only -> next in n+5
E2_AFTER_DAYS = 7                        # K3: E2 exactly one week after E1
MIN_GAP_ANY_DAYS = 14                    # never two lifecycle letters in consecutive weeks (except E1 -> E2)
QUIET_AFTER = {"reorder_1"} | E2_TYPES | {"lost_quarterly"}
RETIRED = {"rhythm_next": "active_xsell"}

# lifecycle_stage -> (track, ordered letters). Only letters in INTERFACE_V1 may be emitted.
TRACKS = {
    "new": ("welcome_buyer", ["welcome_1"]),
    "reorder_due": ("reorder", ["reorder_1"]),                                  # K1
    "winback": ("winback", ["winback_1", "winback_1_e2", "winback_2", "winback_2_e2",
                            "winback_3", "winback_3_e2"]),                     # K3/K5 (order by last letter)
    "lost": ("lost_wave", ["lost_quarterly"]),
    "active": ("active", ["active_xsell"]),
}
LADDER_TYPES = E1_TYPES | E2_TYPES | {"lost_quarterly"}
NO_LADDER_TYPES = {"reorder_1", "reorder_2", "reorder_3"}
RUNG_CAP = 3

# Offer deadline (MAIN 2026-09-25 17:45 from A-Z amendment 2026-09-02, Raivis): a personal price is
# valid 7 days for reorder/winback rungs, 14 days for the lost wave. Counted INCLUDING the send day,
# so OFFER_VALID_UNTIL = send date + (days - 1). reorder has no rung (P5) -> no deadline from here.
# CADENCE v1 K3: an episode's price holds 14 days from E1 (E1 + 13 = E2 + 6), so E1 = 14 d, E2 = 7 d, lost 14 d.
OFFER_VALID_DAYS = {**{t: 14 for t in E1_TYPES}, **{t: 7 for t in E2_TYPES}, "lost_quarterly": 14}

# LADDER POLICY v1 (Raivis 2026-09-28 15:33/15:35, contract sha 326480dce080, rules L1-L8). Replaces every
# "UNCONFIRMED ladder default". L2 rung by stage; L3 lost keeps the last reached rung (max 3, never deeper);
# L5 purchase -> rung 0 (NULL); L6 rung_cap 1 for 12 months after a purchase inside a rung-2/3 window;
# L7 one walk per cycle, no restart without a purchase; L8 doubled reorder -> winback_1 gap after reorder worked.
STAGE_RUNG = {"winback_1": 1, "winback_2": 2, "winback_3": 3}
LOST_ENTRY_RUNG = 1        # L3 when the contact never reached a rung (e.g. history starts 09.2026) - MAIN to confirm
CAP_RUNG, CAP_MONTHS = 1, 12
# The configured reorder -> winback_1 gap is customer_lifecycle's: reorder_due at last_order + entry_threshold_days,
# winback at + entry_threshold_days + 56 (view SQL, read 28.09). L8 doubles it once: factor 2, fixed.
REORDER_TO_WINBACK1_GAP_DAYS = 56
L8_FACTOR = 2
HOLD_NO_RESTART = "LADDER_NO_RESTART"

# Minimum gap between two letters of the same track. UNCONFIRMED where marked.
ACTIVE_XSELL_GAP_DAYS = 28        # UNCONFIRMED - one cross-sell a month
LOST_REPEATS = True               # lost_quarterly repeats monthly (A-Z 1.1 "monthly waves")

# GATE 232/233 (MAIN 2026-09-28 COMMAND 1 item 3, Vēstuļu šabloni pre-send row 13): winback_2 and
# winback_3 are planned only for a contact whose letter will carry a non-empty OFFER_VALID_UNTIL.
# Until Nakts sinhronizācija writes OFFER_VALID_UNTIL, the engine expresses it as "this contact HAS a
# rung price": at least one PAP transport row (contract v2.8.1 A4) whose price at the planned rung is
# strictly more than 5 % below the shop (P2 / A4 "0.05 STRICT"), from a table not older than 26 h
# (v2.8.2 A7), and - on the send date - PAP valid_until >= our OFFER_VALID_UNTIL (v2.8.2 A6).
# Facts.rung_price_valid_until carries {rung: latest valid_until among such rows}; the job fills it
# (sequence_job.RUNG_PRICE_SQL). Missing / empty -> hold 'no_offer_valid_until' (fail closed).
PRICE_GATED_TYPES = {"winback_2", "winback_3"} | E2_TYPES   # an E2 without the E1 price is meaningless
HOLD_NO_OFFER = "no_offer_valid_until"


@dc.dataclass
class State:
    master_key: str
    track: str | None = None
    track_entered_on: dt.date | None = None
    step: int = 0                      # letters of this track already SENT (0 = none yet)
    rung: int | None = None            # NULL | 1 | 2 | 3
    rung_set_on: dt.date | None = None
    rung_month: str | None = None      # YYYY-MM of the last rung change
    ladder_cleared_on: dt.date | None = None
    last_email_type: str | None = None
    last_sent_on: dt.date | None = None
    rung_cap: int | None = None            # L6: 1 for 12 months after a purchase inside a rung-2/3 window
    rung_cap_until: dt.date | None = None
    reorder_worked_at: dt.date | None = None   # L8: last paid order after a reorder letter, before any price letter


@dc.dataclass
class Facts:
    lifecycle_stage: str | None
    last_order_on: dt.date | None
    first_order_on: dt.date | None
    suppressed: bool = False
    rung_price_valid_until: dict | None = None   # {rung: date}; see PRICE_GATED_TYPES
    entry_threshold_days: int | None = None      # customer_lifecycle: reorder_due starts at last_order + this


@dc.dataclass
class Decision:
    state: State
    next_email_type: str | None
    next_due_on: dt.date | None
    offer_rung: int                    # contract v2.8 OFFER_RUNG for the next letter
    reason: str
    hold_reason: str | None
    changes: list                       # [(field, before, after)] for contact_sequence_log
    offer_valid_until: dt.date | None = None   # contract v2.8 OFFER_VALID_UNTIL (None -> "")


def _month(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _months_between(a: dt.date, b: dt.date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def _first_of_next_month(d: dt.date) -> dt.date:
    return dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def advance(prev: State, f: Facts, today: dt.date) -> Decision:
    """The whole rule set in one function, so the shadow plan and the live path share it."""
    s = dc.replace(prev)
    changes = []

    def setf(name, value):
        before = getattr(s, name)
        if before != value:
            changes.append((name, before, value))
            setattr(s, name, value)

    # 1. L5: a purchase after the ladder started clears it (rung 0 = NULL).
    if s.rung is not None and f.last_order_on and s.rung_set_on and f.last_order_on >= s.rung_set_on:
        setf("rung", None); setf("rung_set_on", None); setf("rung_month", None)
        setf("ladder_cleared_on", f.last_order_on)

    # 2. Track from lifecycle stage. A new track resets the step, never the ladder by itself.
    if f.lifecycle_stage in (None, "blocked"):
        setf("track", None)
        return Decision(s, None, None, 0, f"stage={f.lifecycle_stage}", "BLOCKED_OR_UNKNOWN", changes)
    track, letters = TRACKS[f.lifecycle_stage]
    # L7: a ladder is walked once per cycle - back from lost to winback only after a purchase.
    if track == "winback" and s.track == "lost_wave" and not (
            f.last_order_on and s.track_entered_on and f.last_order_on > s.track_entered_on):
        return Decision(s, None, None, 0, "lost -> winback without a purchase", HOLD_NO_RESTART, changes)
    if s.track != track:
        setf("track", track); setf("track_entered_on", today); setf("step", 0)
    # A purchase inside a track restarts it (the person's clock restarted).
    if f.last_order_on and s.track_entered_on and f.last_order_on > s.track_entered_on:
        setf("track_entered_on", f.last_order_on); setf("step", 0)

    if f.suppressed:
        return Decision(s, None, None, 0, f"track={track}", "SUPPRESSED", changes)

    # 3. Which letter is next, and when (CADENCE v1).
    floor = cadence_floor(s, f)
    e2 = pending_e2(s, f, today) if track in ("winback", "lost_wave") else None
    if e2:                                                    # K3: E2 exactly E1 + 7, even if stage moved to lost
        nxt, due = e2
    elif track == "winback":
        nxt = next_episode_letter(s, f)
        if nxt is None:                                       # K5: rung-3 episode done -> wait for stage lost
            return Decision(s, None, None, 0, f"track={track} rung-3 episode done", "SEQUENCE_DONE", changes)
        due = max(today, floor) if floor else today
        # L8: reorder worked in an earlier cycle -> winback_1 no earlier than 2 x the configured gap
        # ("later cycle" = the cycle that the worked order itself started, or any after it)
        if nxt == "winback_1" and s.reorder_worked_at and f.last_order_on and f.entry_threshold_days is not None \
                and f.last_order_on >= s.reorder_worked_at:
            earliest = f.last_order_on + dt.timedelta(days=f.entry_threshold_days
                                                      + L8_FACTOR * REORDER_TO_WINBACK1_GAP_DAYS)
            due = max(due, earliest)
    else:
        if s.step < len(letters):
            nxt = letters[s.step]
        elif track in ("lost_wave", "active") and (LOST_REPEATS or track == "active"):
            nxt = letters[-1]
        else:
            return Decision(s, None, None, 0, f"track={track} step={s.step}/{len(letters)} done",
                            "SEQUENCE_DONE", changes)
        if track == "welcome_buyer":
            base = f.first_order_on or today
            due = max(today, base + dt.timedelta(days=3))
        elif track == "reorder":
            due = today                                        # K1: one letter; step >= 1 -> SEQUENCE_DONE above
        elif track == "lost_wave":
            # one lost letter per calendar month, and (K4) never inside the quiet weeks after an episode
            due = today if s.last_sent_on is None or _month(s.last_sent_on) != _month(today) \
                else _first_of_next_month(today)
        else:  # active
            due = today if s.last_sent_on is None else \
                max(today, s.last_sent_on + dt.timedelta(days=ACTIVE_XSELL_GAP_DAYS))
        if floor:
            due = max(due, floor)

    # 4. Rung for that letter (decided for the letter, stored only when it is SENT - see record_sent).
    rung = planned_rung(s, nxt, due)
    reason = (f"track={track} step={s.step + 1}/{len(letters)} letter={nxt} rung={rung}"
              f" last_order={f.last_order_on} last_sent={s.last_sent_on}")
    # A6 (v2.8.2) + K3: the ONE customer-facing date, owned here. E1 / lost = send + 13; E2 = its E1 + 13.
    ovu = (s.last_sent_on + dt.timedelta(days=13) if nxt in E2_TYPES else offer_valid_until(nxt, due)) if rung else None
    has = has_rung_price(f, rung, due, ovu, today)
    if nxt in PRICE_GATED_TYPES and not has:
        return Decision(s, nxt, due, rung, reason + " no rung price", HOLD_NO_OFFER, changes, None)
    # OFFER_VALID_UNTIL only when the letter carries a rung price (OFFER_RUNG > 0 AND a price exists)
    return Decision(s, nxt, due, rung, reason, None, changes, ovu if has else None)


def cadence_floor(s: State, f: Facts):
    """K2/K4 + no consecutive weeks: the earliest date of the next lifecycle letter (None = no letter yet).
    A purchase after the last letter ends the cycle (K6): only the 14-day minimum is left."""
    if s.last_sent_on is None:
        return None
    purchased = f.last_order_on is not None and f.last_order_on >= s.last_sent_on
    t = s.last_email_type
    if not purchased and t in E1_TYPES:          # E2 not sent (or not yet): the episode ends on E1 + 7
        return s.last_sent_on + dt.timedelta(days=E2_AFTER_DAYS + QUIET_GAP_DAYS)
    if not purchased and t in QUIET_AFTER:
        return s.last_sent_on + dt.timedelta(days=QUIET_GAP_DAYS)
    return s.last_sent_on + dt.timedelta(days=MIN_GAP_ANY_DAYS)


def pending_e2(s: State, f: Facts, today: dt.date):
    """K3: (E2 type, E1 + 7) when the last letter is an E1, no purchase since, and E1 + 7 is not past.
    A missed E2 is skipped - its text says "7 more days" and the OFFER_VALID_UNTIL is fixed by E1."""
    if s.last_email_type not in E1_TYPES or s.last_sent_on is None:
        return None
    if f.last_order_on is not None and f.last_order_on >= s.last_sent_on:
        return None                                                # K6
    due = s.last_sent_on + dt.timedelta(days=E2_AFTER_DAYS)
    if today > due:
        return None
    return E2_BY_RUNG[RUNG_OF[s.last_email_type]], due


def next_episode_letter(s: State, f: Facts):
    """K5: the E1 of the next episode from the last winback letter of THIS cycle (None = rung 3 done).
    The cycle is bounded by the last PURCHASE (K6/L7), not by track_entered_on: the first engine run set
    track_entered_on 25.09 AFTER campaign 221 (22.09) was applied from history (measured 30.09, 72 contacts)."""
    purchased = f.last_order_on is not None and s.last_sent_on is not None and f.last_order_on >= s.last_sent_on
    last = s.last_email_type if (s.last_sent_on and not purchased) else None
    if last not in RUNG_OF:
        return E1_BY_RUNG[1]
    r = RUNG_OF[last]
    return E1_BY_RUNG.get(r + 1)


def planned_rung(s: State, nxt: str, due: dt.date) -> int:
    """L1-L3, L6, A2: the rung of the next letter = MIN(stage rung, rung_cap). E2 = its E1's rung (K3)."""
    if nxt in NO_LADDER_TYPES or nxt not in LADDER_TYPES:
        return 0                                                  # L1
    if nxt in E2_TYPES:
        return s.rung or 0                                        # K3: same price as the E1
    stage = STAGE_RUNG.get(nxt)                                   # L2
    if stage is None:                                             # lost_quarterly - L3
        stage = min(RUNG_CAP, s.rung) if s.rung else LOST_ENTRY_RUNG
    if s.rung_cap and s.rung_cap_until and s.rung_cap_until >= due:
        stage = min(stage, s.rung_cap)                            # L6
    if s.rung is not None and stage != s.rung and s.rung_month == _month(due):
        return s.rung                                             # A2: never twice in one calendar month
    return stage


def _add_months(d: dt.date, n: int) -> dt.date:
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    last = [31, 29 if (y % 4 == 0 and (y % 100 or y % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return dt.date(y, m, min(d.day, last))


def ladder_marks(sends, orders) -> tuple:
    """L6 + L8 from the full history of one contact. sends: [{email_type, rung, sent_on}] (counted sends,
    any order); orders: [date] of PAID orders. Returns (rung_cap, rung_cap_until, reorder_worked_at)."""
    sends = sorted(sends, key=lambda h: h["sent_on"])
    orders = sorted(set(orders))
    cap_until = None
    for h in sends:                                               # L6
        if (h.get("rung") or 0) >= 2 and h["email_type"] in OFFER_VALID_DAYS:
            end = h["sent_on"] + dt.timedelta(days=OFFER_VALID_DAYS[h["email_type"]] - 1)
            for o in orders:
                if h["sent_on"] <= o <= end:
                    u = _add_months(o, CAP_MONTHS)
                    cap_until = u if cap_until is None or u > cap_until else cap_until
    worked = None
    prev = None
    for o in orders:                                              # L8: cycle = sends after the previous order, before o
        cyc = [h for h in sends if (prev is None or h["sent_on"] > prev) and h["sent_on"] <= o]
        if any(h["email_type"] in NO_LADDER_TYPES for h in cyc):
            first_reorder = min(h["sent_on"] for h in cyc if h["email_type"] in NO_LADDER_TYPES)
            priced = any(h["email_type"] in LADDER_TYPES and (h.get("rung") or 0) > 0
                         and h["sent_on"] >= first_reorder for h in cyc)
            if not priced:
                worked = o
        prev = o
    return (CAP_RUNG if cap_until else None), cap_until, worked


def offer_valid_until(email_type: str, send_date: dt.date) -> dt.date | None:
    """Contract v2.8.2 A6: send date + (days - 1); None for letters without a deadline."""
    days = OFFER_VALID_DAYS.get(email_type)
    return send_date + dt.timedelta(days=days - 1) if days else None


def has_rung_price(f: Facts, rung: int, due: dt.date, ovu: dt.date | None, today: dt.date) -> bool:
    """Engine-side "OFFER_VALID_UNTIL will be non-empty" (gate 232/233, contract v2.8.2 A6/A7).
    A price at this rung must exist in the (fresh, A7) PAP table. PAP valid_until is only a CONDITION,
    evaluated on the SEND DATE against that night's table: valid_until >= OFFER_VALID_UNTIL. For a
    letter due later, today's table cannot answer it (PAP refreshes nightly) - the run on the send
    date re-evaluates, so only existence counts until then."""
    vu = (f.rung_price_valid_until or {}).get(rung)
    if not rung or vu is None or ovu is None:
        return False
    return vu >= ovu if due <= today else True


def record_sent(s: State, email_type: str, sent_on: dt.date, rung: int) -> State:
    """Apply one real (or history-reconstructed) send to the state. Never called in shadow."""
    s = dc.replace(s)
    s.step += 1
    s.last_email_type, s.last_sent_on = email_type, sent_on
    if rung:
        assert email_type in LADDER_TYPES, f"rung {rung} on non-ladder letter {email_type}"
        if s.rung != rung:
            assert s.rung_month != _month(sent_on), "price dropped twice in one calendar month"
            # A2: the rung only climbs, one step at a time (start at 1, +1, cap RUNG_CAP)
            assert rung == (1 if s.rung is None else min(RUNG_CAP, s.rung + 1)), \
                f"rung {s.rung} -> {rung} is not one step up"
            s.rung, s.rung_set_on, s.rung_month = rung, sent_on, _month(sent_on)
    return s


def apply_history(s: State, rows) -> tuple:
    """Apply counted sends (dicts with track, email_type, rung, sent_on, source) newer than
    s.last_sent_on, in order. Returns (state, source of the last applied row or None)."""
    src = None
    for h in rows:
        if s.last_sent_on is not None and h["sent_on"] <= s.last_sent_on:
            continue
        if s.track != h["track"]:
            s = dc.replace(s, track=h["track"], track_entered_on=h["sent_on"], step=0)
        s = record_sent(s, h["email_type"], h["sent_on"], h["rung"] or 0)
        src = h["source"]
    return s, src


def template_disagreements(template_map: dict) -> list:
    """INTERFACE_V1 vs mkt_control.email_template_map (email_type -> template_id)."""
    out = []
    for et, tid in INTERFACE_V1.items():
        if et == "akcija_weekly":
            continue
        if template_map.get(et) != tid:
            out.append((et, tid, template_map.get(et)))
    return out
