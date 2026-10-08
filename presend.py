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
  TEMPLATE_NOT_APPROVED  TC4: no approved row for (template id, email type), or its approved_sha256 is NULL, or it is
                         not the sha256 of the htmlContent Brevo holds now (an approval is of a content, not an id)
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
  LETTER_DATE_NOT_PLAN_DATE  DW2: the writer's OFFER_VALID_UNTIL / XSELL_VALID_UNTIL is not the plan's date
  WRITER_EXCLUDED        today's letter_fields row of this contact is an EXCLUDED row (the writer's own B2B / suppression
                         view refused the contact): there is no field set to send
  NO_LETTER_FIELDS_ROW   WO1 / WO2 (contract 7ca13f671703): no mkt_control.letter_fields row of this contact and this
                         letter for TODAY's plan_date. Never an older plan_date, never another table.

Sūtīšanas dzinējs 5 (MAIN 2026-10-06, contract 1634b7f07054): the gates that only READ the writer's row - G15
(NO_SLOT_ROW / NO_PRICED_SLOTS), XS4, NO_ANKETA_URL, LETTER_DATE_NOT_PLAN_DATE - are NOT named while the row does not
exist. Before the 08:40 writer the only true statement is NO_LETTER_FIELDS_ROW; naming XS4 for 898 letters at 08:05
was an artefact, and reading G15 from yesterday's goods run was a fallback WO2 forbids. G15 = the row's own
rung + g15_zero_priced. The gate that counts is evaluated at SEND time (send_path L8, send_lookups.py).
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
DATE_MISMATCH = "LETTER_DATE_NOT_PLAN_DATE"
EXCLUDED = "WRITER_EXCLUDED"
# CAB-1 (interface fixed by MAIN 2026-10-08 13:52): the price a letter prints must be what the client's cabinet shows,
# read from mkt_control.cabinet_price_current - only today's run_id 'cab-YYYYMMDD' whose cabinet_price_log row is ok,
# only status 'written'. No row for the person, a priced slot without its row, a row of another price role, or another
# price -> the letter is held. No fallback to block/LQXS or to an older day. xs_intro stays out.
CAB_MISSING = "CABINET_PRICE_MISSING"
CAB_ROLES = {"winback_1": {"r1"}, "lost_quarterly": {"lq1_floor", "lq2_minus13"}, "reorder_1": {"negotiated"}}
_UNSET = object()
ALL = (T_PROVISIONAL, T_NOT_APPROVED, PRICE_STALE, NO_OVU, NO_PRICE, NO_SLOT_ROW, NO_PRICED, XS4, XS_NOTHING_NEW, XS_REPEAT,
       NO_ANKETA, RCAB, DATE_MISMATCH, EXCLUDED, NO_LF, CAB_MISSING)

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
    writer_excluded: bool = False                       # ... and it is an EXCLUDED row: no field set
    row_offer_valid_until: str | None = None            # the writer's OFFER_VALID_UNTIL text (DD.MM.YYYY, "" = none)
    row_xsell_valid_until: str | None = None            # the writer's XSELL_VALID_UNTIL text
    letter_p: tuple = ()                                # CAB-1: the letter's priced P slots ((sku, price text), ...)
    cab: dict | None = None                             # CAB-1: {sku_key: (price_role, gross)} of today's ok run; None = none
    cab_checked: bool = False                           # the caller read CAB-1 (both production callers do)


def _dmy(v) -> str | None:
    """A plan date as the letter prints it (DD.MM.YYYY)."""
    if not v:
        return None
    if isinstance(v, str):
        v = dt.date.fromisoformat(v[:10])
    return v.strftime("%d.%m.%Y")


def row_goods(row):
    """G15 from the writer's row: (rung the goods were built for, zero priced slots) - None when there is no
    usable row (absent or EXCLUDED)."""
    if not row or row.get("letter") == "EXCLUDED":
        return None
    return (row.get("rung"), bool(row.get("g15_zero_priced")))


def letter_priced_slots(row) -> tuple:
    """CAB-1: the letter's priced P slots as ((sku, price text), ...). The SKU of slot i comes from p_audit
    ('1:FM26656M:u 2:77-640:u'), the price from P{i}_PRICE. A priced slot whose SKU is unknown keeps sku None and
    so can never match a cabinet row (held)."""
    if not row:
        return ()
    sku_of = {}
    for part in str(row.get("p_audit") or "").strip().strip('"').split():
        bits = part.split(":")
        if len(bits) >= 2 and bits[0].isdigit():
            sku_of[int(bits[0])] = ":".join(bits[1:-1]) if len(bits) > 2 else bits[1]
    out = []
    for i in range(1, 9):
        price = (row.get(f"P{i}_PRICE") or "").strip()
        if price:
            out.append((sku_of.get(i), price))
    return tuple(out)


def _money(v):
    t = "".join(ch for ch in str(v if v is not None else "").replace(",", ".") if ch.isdigit() or ch == ".")
    try:
        return round(float(t), 2) if t and t.count(".") <= 1 else None
    except ValueError:
        return None


def sku_key(v) -> str:
    """The key business_marts.mozello_sku_handle and CAB-1 rows are matched on: whitespace -> space, trimmed, upper."""
    return " ".join(str(v or "").replace("\u00a0", " ").split()).upper()


def cab_problem(email_type, letter_p, cab) -> bool:
    """CAB-1, pure. True = the letter must be held."""
    roles = CAB_ROLES.get(email_type)
    if roles is None:
        return False
    if not cab:                      # no ok run today, or no row at all for this person
        return True
    for sku, price in letter_p:
        r = cab.get(sku_key(sku)) if sku else None
        if not r or r[0] not in roles:
            return True
        a, b = _money(price), _money(r[1])
        if a is None or b is None or abs(a - b) > 0.005:
            return True
    return False


def build_ctx(*, email_type, template_id, template_approved, offer_valid_until, xsell_valid_until, has_price,
              price_stale, row, trigger_order_nr, r_handles=(), r_cabinet=(), xsell_offered=frozenset(),
              xs_price_holds=True, cab=_UNSET) -> Ctx:
    """THE way a Ctx is made from a plan row + today's letter_fields row of the same letter (or None). The 08:05
    planner (sequence_job) and the send path (send_lookups) both call this - one builder, two moments."""
    excluded = bool(row) and row.get("letter") == "EXCLUDED"
    use = row if row and not excluded else None
    same_order = bool(use) and bool(trigger_order_nr) and use.get("ORDER_NR") == trigger_order_nr   # WO3
    return Ctx(
        template_id=template_id, template_approved=template_approved, offer_valid_until=offer_valid_until,
        goods=row_goods(use), price_stale=price_stale, has_price=has_price, r_handles=tuple(r_handles),
        r_cabinet=tuple(r_cabinet), r1_ref_price=use.get("R1_REF_PRICE") if use else None,
        xsell_valid_until=xsell_valid_until if xs_price_holds else None, xsell_offered=xsell_offered,
        anketa_url=use.get("ANKETA_URL") if same_order else None,
        order_nr=trigger_order_nr if email_type == S.PP1 else None,
        letter_fields=row is not None, writer_excluded=excluded,
        row_offer_valid_until=use.get("OFFER_VALID_UNTIL") if use else None,
        row_xsell_valid_until=use.get("XSELL_VALID_UNTIL") if use else None,
        letter_p=letter_priced_slots(use), cab=None if cab is _UNSET else cab, cab_checked=cab is not _UNSET)


def gates(email_type: str | None, offer_rung, c: Ctx) -> list:
    """All gates that refuse this letter, in a fixed order (first = the one reported as presend_gate)."""
    if not email_type:
        return []
    out = []
    if c.template_id in S.PROVISIONAL_TEMPLATE_IDS:
        out.append(T_PROVISIONAL)
    elif not c.template_approved:
        out.append(T_NOT_APPROVED)
    row = c.letter_fields and not c.writer_excluded          # the writer's field set of this letter exists
    if S.is_price_letter(email_type, offer_rung):
        if not c.offer_valid_until:
            out.append(NO_OVU)
        elif c.has_price is False:                          # DW1: the date no longer says a price exists
            out.append(PRICE_STALE if c.price_stale else NO_PRICE)
        if row:
            g = S.goods_hold(email_type, offer_rung, None, c.goods)             # G15 / G15.1, from the row
            if g:
                out.append(g)
            if c.row_offer_valid_until and c.offer_valid_until \
                    and c.row_offer_valid_until != _dmy(c.offer_valid_until):
                out.append(DATE_MISMATCH)                                       # DW2: the writer copies the date
    if email_type == S.XSELL:
        if row and not (c.r1_ref_price and c.xsell_valid_until):
            out.append(PRICE_STALE if c.price_stale else XS4)
        if row and c.row_xsell_valid_until and c.xsell_valid_until \
                and c.row_xsell_valid_until != _dmy(c.xsell_valid_until):
            out.append(DATE_MISMATCH)
        if c.r_handles and c.xsell_offered:
            again = [h for h in c.r_handles if h in c.xsell_offered]
            if len(again) == len(c.r_handles):
                out.append(XS_NOTHING_NEW)
            elif again:
                out.append(XS_REPEAT)
    if email_type == S.PP1 and (not c.order_nr or (row and not c.anketa_url)):
        out.append(NO_ANKETA)                               # no order on the plan row, or the row has no link for it
    if email_type in R_SLOT_TYPES and set(c.r_handles) != set(c.r_cabinet):
        out.append(RCAB)
    if c.cab_checked and row and cab_problem(email_type, c.letter_p, c.cab):
        out.append(CAB_MISSING)
    if c.writer_excluded:
        out.append(EXCLUDED)
    elif not c.letter_fields:
        out.append(NO_LF)
    return out
