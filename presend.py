"""Pre-send gates (Sūtīšanas dzinējs 4, MAIN command 2026-10-05 16:10 item 3; contract sha 682ad015f0ab). Pure.

ONE function decides, for one planned letter of one contact, which DATA gates would refuse it. The daily shadow
planner calls it with the newest data that exists at 08:05 and stores the answer next to the plan row
(shadow_send_plan.presend_gate / presend_gates / would_deliver); the live send path calls the SAME function again
at send time (send_path lock L8) with that moment's data. A gate never falls back to another letter.

Person-level refusals are NOT here: SUPPRESSED, BLOCKED_OR_UNKNOWN, B2B_FLOW, LEAD_FLOW and EN_PENDING are holds
of the planner (sequence.py) and are re-checked at send time by send_path lock L9.

Why two layers (contract G15.2): the planner must not change would_send because a writer run does not exist yet at
08:05 - so would_send stays "the engine wants to send this", and would_deliver = would_send AND no gate.

Gates (each a named reason):
  TEMPLATE_PROVISIONAL   the letter's template id is a provisional one (9180 / 9232 / 9233) - no Brevo template yet
  TEMPLATE_NOT_APPROVED  no approved row in mkt_control.template_approval for (template id, email type)
  PRICE_SOURCE_STALE     a price letter / 235 and the price table is older than 26 h (v2.8.2 A7) - no price today
  NO_OFFER_VALID_UNTIL   a price letter (E1 / E2 of any rung, lost_quarterly) without OFFER_VALID_UNTIL
  NO_PRICE_ROW           a price letter with a date but no price row at its rung in a fresh price table
  NO_SLOT_ROW            G15.1: a price letter with no goods row, or a row built for another rung
  NO_PRICED_SLOTS        G15: a price letter whose goods carry 0 priced slots
  XS4_NO_INTRO_PRICE     235 without R1_REF_PRICE, or without XSELL_VALID_UNTIL (XS4)
  XSELL_NOTHING_NEW      235 whose R goods were ALL offered to this contact in an earlier 235 (PP3) - skip
  XSELL_REPEAT           235 with at least one R product already offered in an earlier 235 (PP3 "never repeat")
  NO_ANKETA_URL          244 with an empty ANKETA_URL (FS8) or without the triggering order number
  RCAB_MISMATCH          a letter with R slots whose R goods are not what the cabinet shows (R-CAB C4)
  NO_LETTER_FIELDS_ROW   WO1 / WO2 (contract 7ca13f671703): no mkt_control.letter_fields row of this contact and this
                         letter for TODAY's plan_date. Never an older plan_date, never another table. Reported LAST,
                         so presend_gate still names the data reason when there is one.
"""
from __future__ import annotations

import dataclasses as dc
import datetime as dt

import sequence as S

T_PROVISIONAL = "TEMPLATE_PROVISIONAL"
T_NOT_APPROVED = "TEMPLATE_NOT_APPROVED"
PRICE_STALE = "PRICE_SOURCE_STALE"
NO_OVU = "NO_OFFER_VALID_UNTIL"
NO_PRICE = "NO_PRICE_ROW"
NO_SLOT_ROW = S.HOLD_NO_SLOT_ROW
NO_PRICED = S.HOLD_NO_PRICED
XS4 = "XS4_NO_INTRO_PRICE"
XS_NOTHING_NEW = "XSELL_NOTHING_NEW"
XS_REPEAT = "XSELL_REPEAT"
NO_ANKETA = "NO_ANKETA_URL"
RCAB = "RCAB_MISMATCH"
NO_LF = "NO_LETTER_FIELDS_ROW"
ALL = (T_PROVISIONAL, T_NOT_APPROVED, PRICE_STALE, NO_OVU, NO_PRICE, NO_SLOT_ROW, NO_PRICED, XS4, XS_NOTHING_NEW, XS_REPEAT,
       NO_ANKETA, RCAB, NO_LF)

# Templates that print R1..R4 (grep "R1_NAME" over templates/ on feat/v2.8-price-fields @ 48dac94, 2026-10-05):
# every lifecycle letter except 244. R-CAB applies to all of them.
R_SLOT_TYPES = {"reorder_1", S.XSELL, "lost_quarterly"} | S.E1_TYPES | S.E2_TYPES


@dc.dataclass
class Ctx:
    """Everything the gates read about ONE planned letter. None / empty = 'not there' -> the gate refuses."""
    template_id: int | None = None
    template_approved: bool = False
    offer_valid_until: dt.date | str | None = None      # engine-owned (A6); the writer copies it
    goods: tuple | None = None                          # (rung, zero_priced) from the goods run, or None
    price_stale: bool = False                           # A7 on the table this letter's price comes from
    has_price: bool | None = None                       # engine: a price row exists at the rung (None = unknown)
    r_handles: tuple = ()                               # the letter's R goods (handles, slot order)
    r_cabinet: tuple = ()                               # what the cabinet shows for this contact (handles)
    r1_ref_price: str | None = None                     # XS5 R1_REF_PRICE ("" / None = no intro price)
    xsell_valid_until: dt.date | str | None = None      # XS6
    xsell_offered: frozenset = frozenset()              # handles offered in earlier 235 letters to this contact
    anketa_url: str | None = None                       # PP4.2 / FS8
    order_nr: str | None = None                         # the order that triggers 244
    letter_fields: bool = False                         # today's letter_fields row of this letter exists (WO2)


def gates(email_type: str | None, offer_rung, c: Ctx) -> list:
    """All gates that refuse this letter, in a fixed order (first = the one reported as presend_gate)."""
    if not email_type:
        return []
    out = []
    if c.template_id in S.PROVISIONAL_TEMPLATE_IDS:
        out.append(T_PROVISIONAL)
    elif not c.template_approved:
        out.append(T_NOT_APPROVED)
    if S.is_price_letter(email_type, offer_rung):
        if not c.offer_valid_until:
            out.append(NO_OVU)
        elif c.has_price is False:                          # DW1: the date no longer says a price exists
            out.append(PRICE_STALE if c.price_stale else NO_PRICE)
        g = S.goods_hold(email_type, offer_rung, None, c.goods)             # G15 / G15.1
        if g:
            out.append(g)
    if email_type == S.XSELL:
        if not (c.r1_ref_price and c.xsell_valid_until):
            out.append(PRICE_STALE if c.price_stale else XS4)
        if c.r_handles and c.xsell_offered:
            again = [h for h in c.r_handles if h in c.xsell_offered]
            if len(again) == len(c.r_handles):
                out.append(XS_NOTHING_NEW)
            elif again:
                out.append(XS_REPEAT)
    if email_type == S.PP1 and not (c.anketa_url and c.order_nr):
        out.append(NO_ANKETA)
    if email_type in R_SLOT_TYPES and set(c.r_handles) != set(c.r_cabinet):
        out.append(RCAB)
    if not c.letter_fields:
        out.append(NO_LF)
    return out
