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
  The price drops at most ONCE per calendar month.
  DEFAULTS, UNCONFIRMED (Raivis' answer comes via MAIN): the first price letter = rung 1; each
  following calendar month without a purchase = rung + 1, cap 3; any purchase clears the ladder.
"""
from __future__ import annotations

import dataclasses as dc
import datetime as dt

INTERFACE_V1 = {
    "welcome_1": 229, "reorder_1": 179, "reorder_2": 230, "reorder_3": 231,
    "winback_1": 180, "winback_2": 232, "winback_3": 233, "lost_quarterly": 234,
    "active_xsell": 235, "akcija_weekly": 236,
}
RETIRED = {"rhythm_next": "active_xsell"}

# lifecycle_stage -> (track, ordered letters). Only letters in INTERFACE_V1 may be emitted.
TRACKS = {
    "new": ("welcome_buyer", ["welcome_1"]),
    "reorder_due": ("reorder", ["reorder_1", "reorder_2", "reorder_3"]),
    "winback": ("winback", ["winback_1", "winback_2", "winback_3"]),
    "lost": ("lost_wave", ["lost_quarterly"]),
    "active": ("active", ["active_xsell"]),
}
LADDER_TYPES = {"winback_1", "winback_2", "winback_3", "lost_quarterly"}
NO_LADDER_TYPES = {"reorder_1", "reorder_2", "reorder_3"}
RUNG_CAP = 3

# Offer deadline (MAIN 2026-09-25 17:45 from A-Z amendment 2026-09-02, Raivis): a personal price is
# valid 7 days for reorder/winback rungs, 14 days for the lost wave. Counted INCLUDING the send day,
# so OFFER_VALID_UNTIL = send date + (days - 1). reorder has no rung (P5) -> no deadline from here.
OFFER_VALID_DAYS = {"winback_1": 7, "winback_2": 7, "winback_3": 7, "lost_quarterly": 14}

# Minimum gap between two letters of the same track. UNCONFIRMED where marked.
REORDER_STEP_GAP_DAYS = 14        # UNCONFIRMED - nothing in the tree sets reorder_2/3 spacing
ACTIVE_XSELL_GAP_DAYS = 28        # UNCONFIRMED - one cross-sell a month
LOST_REPEATS = True               # lost_quarterly repeats monthly (A-Z 1.1 "monthly waves")


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


@dc.dataclass
class Facts:
    lifecycle_stage: str | None
    last_order_on: dt.date | None
    first_order_on: dt.date | None
    suppressed: bool = False


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

    # 1. A purchase after the ladder started clears it (UNCONFIRMED default).
    if s.rung is not None and f.last_order_on and s.rung_set_on and f.last_order_on >= s.rung_set_on:
        setf("rung", None); setf("rung_set_on", None); setf("rung_month", None)
        setf("ladder_cleared_on", f.last_order_on)

    # 2. Track from lifecycle stage. A new track resets the step, never the ladder by itself.
    if f.lifecycle_stage in (None, "blocked"):
        setf("track", None)
        return Decision(s, None, None, 0, f"stage={f.lifecycle_stage}", "BLOCKED_OR_UNKNOWN", changes)
    track, letters = TRACKS[f.lifecycle_stage]
    if s.track != track:
        setf("track", track); setf("track_entered_on", today); setf("step", 0)
    # A purchase inside a track restarts it (the person's clock restarted).
    if f.last_order_on and s.track_entered_on and f.last_order_on > s.track_entered_on:
        setf("track_entered_on", f.last_order_on); setf("step", 0)

    if f.suppressed:
        return Decision(s, None, None, 0, f"track={track}", "SUPPRESSED", changes)

    # 3. Which letter is next, and when.
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
        due = today if s.last_sent_on is None or s.step == 0 else \
            max(today, s.last_sent_on + dt.timedelta(days=REORDER_STEP_GAP_DAYS))
    elif track in ("winback", "lost_wave"):
        # one ladder letter per calendar month
        due = today if s.last_sent_on is None or _month(s.last_sent_on) != _month(today) \
            else _first_of_next_month(today)
    else:  # active
        due = today if s.last_sent_on is None else \
            max(today, s.last_sent_on + dt.timedelta(days=ACTIVE_XSELL_GAP_DAYS))

    # 4. Rung for that letter (decided for the letter, stored only when it is SENT - see record_sent).
    if nxt in NO_LADDER_TYPES or nxt not in LADDER_TYPES:
        rung = 0
    elif s.rung is None:
        rung = 1
    elif s.rung_month == _month(due):
        rung = s.rung                                   # never twice in one calendar month
    else:
        rung = min(RUNG_CAP, s.rung + 1)          # next calendar month without purchase
    reason = (f"track={track} step={s.step + 1}/{len(letters)} letter={nxt} rung={rung}"
              f" last_order={f.last_order_on} last_sent={s.last_sent_on}")
    valid = due + dt.timedelta(days=OFFER_VALID_DAYS[nxt] - 1) if rung and nxt in OFFER_VALID_DAYS else None
    return Decision(s, nxt, due, rung, reason, None, changes, valid)


def record_sent(s: State, email_type: str, sent_on: dt.date, rung: int) -> State:
    """Apply one real (or history-reconstructed) send to the state. Never called in shadow."""
    s = dc.replace(s)
    s.step += 1
    s.last_email_type, s.last_sent_on = email_type, sent_on
    if rung:
        assert email_type in LADDER_TYPES, f"rung {rung} on non-ladder letter {email_type}"
        if s.rung != rung:
            assert s.rung_month != _month(sent_on), "price dropped twice in one calendar month"
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
