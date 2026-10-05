"""The send path (Sūtīšanas dzinējs, D4) - BUILT, and LOCKED.

campaign.send_now() runs its content block (⟦…⟧) and Raivis' approval gate first, then hands the
campaign to dispatch() below. dispatch() is the only place a campaign can be sent, and it refuses
unless EVERY lock is open. ABSOLUTE RULE (Raivis 2026-09-25): nothing is sent to any customer until
Raivis himself says so. Today every lock is closed by design:

  L1  config.ALLOW_SEND is True and config.DRY_RUN is False        (env; both default closed)
  L2  SEND_UNLOCKED_BY == "RAIVIS-<today, Europe/Riga>"              (a human-dated word, expires daily)
  L3  the variant's track is enabled in mkt_control.track_enabled    (Raivis' switch; 0/9 today)
  L4  the template has an approved row in mkt_control.template_approval
  L5  the frozen audience for (batch_id, build_id) is non-empty and has 0 suppressed addresses
  L6  pd_record type configured (PD_ACTIVITY_TYPE_KEY) - no customer send without its PD record
  L7  G15 (contract G15.1/G15.2): a PRICE letter needs that send date's goods run
      (mkt_control.shadow_rung_goods_slots, latest run_id of plan_date = send date); no run -> refuse;
      any audience member with 0 priced slots (NO_PRICED_SLOTS) or no row / a row for another rung
      (NO_SLOT_ROW) -> refuse. Never a fallback. Non-price letters skip L7.
  L8  PRE-SEND DATA GATES (MAIN 2026-10-05 16:10 item 3): presend.gates() - the SAME function the daily plan
      stores as presend_gate - is evaluated again for every audience member on that moment's data
      (OFFER_VALID_UNTIL, XS4 R1_REF_PRICE + XSELL_VALID_UNTIL, ANKETA_URL, R-CAB, PP3 no-repeat, template
      approval / provisional id). Any member with a gate -> the campaign is refused (a frozen audience is clean
      or nothing goes, as L5).
  L9  PERSON RE-CHECK at send time: B2B_FLOW / LEAD_FLOW (Pipedrive field 309 or the flow classification) and
      G-EN (EN_PENDING). Any member blocked -> refuse.

  L10 DATES AND SEND WINDOW v1 (contract 9d7c6584cc16, DW3 / DW4): the send is inside 09:00-23:00 Europe/Riga
      of the send date, mkt_control.letter_fields_log has an OK run for the send date, and that run read the
      LATEST plan run of the date (a plan rebuilt after the writer -> refuse until the writer runs again).

Only when ALL pass does it call Brevo sendNow (one call), then, per recipient of the frozen
audience: one mkt_control.send_log row (source='engine_live'), one Pipedrive write through
pd_record.write(shadow=False) - the SAME record the shadow plan stored - and the sequence state is
advanced with sequence.record_sent. Every dependency is injected, so the refusals are proven
without a warehouse, without Brevo and without Pipedrive (tests/test_send_path.py).
"""
from __future__ import annotations

import datetime as dt
import os

import pd_record
import presend
import sequence

RIGA = dt.timezone(dt.timedelta(hours=3))   # EEST; only the DATE is compared, DST edge = 1 h


try:  # a refusal here is a campaign.SendRefused, so every existing caller that catches it still does
    from campaign import SendRefused as _Base
except Exception:  # noqa: BLE001 - campaign needs its own deps; the lock must not depend on them
    _Base = RuntimeError


class SendLocked(_Base):
    """A lock is closed. Carries the name of the FIRST closed lock and all closed ones."""

    def __init__(self, closed):
        self.closed = closed
        super().__init__("SEND PATH LOCKED - " + "; ".join(f"{k}: {v}" for k, v in closed) +
                         ". Raivis' condition of 2026-09-09/25 stands: 'visam japaliek dry run kamer nav "
                         "viss lidz galam gatavs'; opening a lock is his call, not a code change.")


def config_locks(*, allow_send, dry_run, unlocked_by, today) -> list:
    """L1, L2, L6 - checked FIRST, before any lookup or network call."""
    closed = []
    if not allow_send or dry_run:
        closed.append(("L1", f"ALLOW_SEND={allow_send} DRY_RUN={dry_run}"))
    if unlocked_by != f"RAIVIS-{today.isoformat()}":
        closed.append(("L2", f"SEND_UNLOCKED_BY={unlocked_by!r} (needs RAIVIS-{today.isoformat()})"))
    if not pd_record.PD_ACTIVITY_TYPE_KEY:
        closed.append(("L6", "PD_ACTIVITY_TYPE_KEY unresolved"))
    return closed


def data_locks(*, campaign, track_enabled, template_approved, audience, suppressed_count) -> list:
    """L3, L4, L5 - need the warehouse; checked only after the config locks are open."""
    closed = []
    if not track_enabled:
        closed.append(("L3", f"track {campaign.get('track')!r} not enabled"))
    if not template_approved:
        closed.append(("L4", f"template {campaign.get('template_id')} not approved"))
    if not audience:
        closed.append(("L5", "frozen audience empty"))
    elif suppressed_count:
        closed.append(("L5", f"{suppressed_count} suppressed address(es) in the audience"))
    return closed


def g15_lock(*, campaign, send_date, audience, goods_run, goods) -> list:
    """L7. goods_run(send_date) -> run_id | None; goods(run_id, master_keys) -> {mk: (rung, zero_priced)}."""
    rung = campaign.get("rung") or 0
    if not sequence.is_price_letter(campaign.get("email_type"), rung):
        return []
    run_id = goods_run(send_date)
    if not run_id:
        return [("L7", f"G15: no goods run for send date {send_date} - nothing price-related is sent")]
    g = goods(run_id, [a["master_key"] for a in audience])
    bad = {}
    for a in audience:
        h = sequence.goods_hold(campaign["email_type"], rung, None, g.get(a["master_key"]))
        if h:
            bad[h] = bad.get(h, 0) + 1
    return [("L7", f"G15 run {run_id}: " + ", ".join(f"{k} {v}" for k, v in sorted(bad.items())))] if bad else []


def presend_lock(*, campaign, send_date, audience, presend_ctx) -> list:
    """L8. presend_ctx(campaign, send_date, master_keys) -> {master_key: presend.Ctx}; no Ctx = every gate that
    needs data refuses (an empty Ctx), never a pass."""
    ctx = presend_ctx(campaign, send_date, [a["master_key"] for a in audience])
    bad = {}
    for a in audience:
        for g in presend.gates(campaign.get("email_type"), campaign.get("rung") or 0,
                               ctx.get(a["master_key"]) or presend.Ctx())[:1]:
            bad[g] = bad.get(g, 0) + 1
    return [("L8", "pre-send gates: " + ", ".join(f"{k} {v}" for k, v in sorted(bad.items())))] if bad else []


def person_lock(*, send_date, audience, person_blocks) -> list:
    """L9. person_blocks(send_date, master_keys) -> {master_key: 'B2B_FLOW' | 'LEAD_FLOW' | 'EN_PENDING'}."""
    blocked = person_blocks(send_date, [a["master_key"] for a in audience])
    bad = {}
    for a in audience:
        r = blocked.get(a["master_key"])
        if r:
            bad[r] = bad.get(r, 0) + 1
    return [("L9", "person re-check: " + ", ".join(f"{k} {v}" for k, v in sorted(bad.items())))] if bad else []


SEND_WINDOW = (dt.time(9, 0), dt.time(23, 0))          # DW3, Europe/Riga wall clock


def riga(now: dt.datetime) -> dt.datetime:
    import zoneinfo
    return now.astimezone(zoneinfo.ZoneInfo("Europe/Riga"))


def letter_params(row) -> dict:
    """WO1 (contract 7ca13f671703): what a letter carries = the Brevo-named (UPPERCASE) columns of the contact's
    mkt_control.letter_fields row of the send date, value for value. Nothing is added, renamed or recomputed."""
    return {k: v for k, v in dict(row).items() if k.isupper()}


def window_lock(*, send_date, now, letter_fields, plan_run) -> list:
    """L10. letter_fields(send_date) -> {'status', 'plan_run_id', 'run_id'} | None (latest writer run of the date);
    plan_run(send_date) -> run_id of the latest plan run of the date | None."""
    closed = []
    local = riga(now)
    if str(send_date) != local.date().isoformat():
        closed.append(("L10", f"send date {send_date} is not today in Riga ({local.date()})"))
    elif not (SEND_WINDOW[0] <= local.time() < SEND_WINDOW[1]):
        closed.append(("L10", f"outside the send window 09:00-23:00 Riga (now {local.strftime('%H:%M')})"))
    lf, plan = letter_fields(send_date), plan_run(send_date)
    if plan is None:
        closed.append(("L10", f"no plan run for send date {send_date}"))
    if lf is None or lf.get("status") != "OK":
        closed.append(("L10", f"letter_fields_log is not OK for send date {send_date}"))
    elif plan is not None and lf.get("plan_run_id") != plan:
        closed.append(("L10", f"the plan was rebuilt after the letter writer ran (writer read {lf.get('plan_run_id')}, "
                              f"latest plan {plan}) - the writer must run again"))
    return closed


def dispatch(campaign: dict, *, send_date, batch_id, build_id, config, lookups, brevo_send,
             log_sink, pd_writer, state_advance, offered_sink=None, now=None) -> dict:
    """campaign: {campaign_id, email_type, track, template_id, rung, utm_campaign, brevo_list_id}.
    lookups: object with track_enabled(track), template_approved(template_id),
             audience(batch_id, build_id) -> [ {master_key,email,person_id,reason} ], suppressed(emails)->int,
             goods_run(send_date) -> run_id|None, goods(run_id, master_keys) -> {mk: (rung, zero_priced)},
             presend_ctx(campaign, send_date, master_keys) -> {mk: presend.Ctx},
             person_blocks(send_date, master_keys) -> {mk: reason}.
    offered_sink(rows): REQUIRED for 235 - one row per (recipient, R product) into mkt_control.xsell_offered, so
    the next 235 never repeats an R product (PP3).
    Raises SendLocked before ANY external call when a lock is closed."""
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(RIGA).date()
    closed = config_locks(allow_send=config.ALLOW_SEND, dry_run=config.DRY_RUN,
                          unlocked_by=os.environ.get("SEND_UNLOCKED_BY"), today=today)
    if closed:
        raise SendLocked(closed)                              # no lookup, no network
    closed = window_lock(send_date=send_date, now=now, letter_fields=lookups.letter_fields,
                         plan_run=lookups.plan_run)
    if closed:
        raise SendLocked(closed)                              # before the audience is even read
    audience = lookups.audience(batch_id, build_id)
    closed = data_locks(campaign=campaign, track_enabled=lookups.track_enabled(campaign["track"]),
                        template_approved=lookups.template_approved(campaign["template_id"]),
                        audience=audience,
                        suppressed_count=lookups.suppressed([a["email"] for a in audience]))
    closed += g15_lock(campaign=campaign, send_date=send_date, audience=audience,
                       goods_run=lookups.goods_run, goods=lookups.goods)
    if audience:
        closed += presend_lock(campaign=campaign, send_date=send_date, audience=audience,
                               presend_ctx=lookups.presend_ctx)
        closed += person_lock(send_date=send_date, audience=audience, person_blocks=lookups.person_blocks)
    if campaign.get("email_type") == sequence.XSELL and offered_sink is None:
        closed.append(("WIRE", "235 needs offered_sink (mkt_control.xsell_offered) - PP3 no-repeat"))
    if closed:
        raise SendLocked(closed)
    offered_ctx = lookups.presend_ctx(campaign, send_date, [a["master_key"] for a in audience]) \
        if campaign.get("email_type") == sequence.XSELL else {}

    brevo_send(campaign["campaign_id"])                       # the ONE send call
    sent_at = now.isoformat()
    rows, pd_results = [], []
    for a in audience:
        rows.append({"master_key": a["master_key"], "email": a["email"],
                     "campaign_id": campaign["campaign_id"], "brevo_list_id": campaign.get("brevo_list_id"),
                     "email_type": campaign["email_type"], "template_id": campaign["template_id"],
                     "track": campaign["track"], "step": None, "rung": campaign.get("rung") or 0,
                     "sent_at": sent_at, "source": "engine_live", "run_id": batch_id,
                     "brevo_message_id": None, "utm_campaign": campaign.get("utm_campaign")})
        rec = pd_record.render(person_id=a.get("person_id"), org_id=None, master_key=a["master_key"],
                               email=a["email"], email_type=campaign["email_type"],
                               template_id=campaign["template_id"], send_date=send_date,
                               offer_rung=campaign.get("rung") or 0, reason=a.get("reason", ""),
                               campaign_ref=campaign.get("utm_campaign") or campaign["campaign_id"])
        if a.get("person_id") is not None:
            pd_results.append(pd_record.write(rec, shadow=False, pd_writer=pd_writer, shadow_sink=None))
        state_advance(a["master_key"], campaign["email_type"], today, campaign.get("rung") or 0)
    log_sink(rows)
    if offered_ctx:
        offered_sink([{"master_key": a["master_key"], "email": a["email"], "handle": h, "sent_at": sent_at,
                       "campaign_id": campaign["campaign_id"], "run_id": batch_id}
                      for a in audience for h in (offered_ctx.get(a["master_key"]) or presend.Ctx()).r_handles])
    return {"sent": len(rows), "pd_writes": len(pd_results), "no_person": len(rows) - len(pd_results)}


def production_dispatch(campaign_id, send_date, batch_id, build_id):
    """What campaign.send_now() calls once its content block and approval gate have passed. The
    config locks fire here before anything is imported that could reach BigQuery or Brevo."""
    import config as C
    dispatch({"campaign_id": campaign_id, "track": None, "template_id": None, "email_type": None},
             send_date=send_date, batch_id=batch_id, build_id=build_id, config=C,
             lookups=_ProductionLookupsNotWired(), brevo_send=_unwired, log_sink=_unwired,
             pd_writer=_unwired, state_advance=_unwired)


class _ProductionLookupsNotWired:
    """Deliberately unwired: the warehouse lookups and the Brevo/PD writers are connected in the
    unlock change that Raivis orders, together with the campaign metadata. Until then an open
    config lock still cannot send, because every data lock reads here and refuses."""
    def _no(self, *a):
        raise SendLocked([("L3-L5", "production lookups are not wired - the unlock change wires them")])
    track_enabled = template_approved = audience = suppressed = goods_run = goods = _no
    presend_ctx = person_blocks = letter_fields = plan_run = _no


def _unwired(*a, **k):
    raise SendLocked([("WIRE", "a send-side writer is not wired")])


def _main(argv):
    """`python send_path.py --send --campaign N` - the refusal proof. Exit 3 = refused (expected)."""
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--campaign", type=int, default=0)
    a = ap.parse_args(argv)
    if not a.send:
        print("nothing to do without --send"); return 2
    try:
        production_dispatch(a.campaign, dt.date.today().isoformat(), "refusal-proof", "refusal-proof")
    except SendLocked as e:
        print("SEND_REFUSED " + json.dumps({"campaign": a.campaign, "closed": e.closed}, ensure_ascii=False))
        return 3
    print("NOT_REFUSED - this must never print"); return 1


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
