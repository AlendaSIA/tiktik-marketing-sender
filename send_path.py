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

Only when all six pass does it call Brevo sendNow (one call), then, per recipient of the frozen
audience: one mkt_control.send_log row (source='engine_live'), one Pipedrive write through
pd_record.write(shadow=False) - the SAME record the shadow plan stored - and the sequence state is
advanced with sequence.record_sent. Every dependency is injected, so the refusals are proven
without a warehouse, without Brevo and without Pipedrive (tests/test_send_path.py).
"""
from __future__ import annotations

import datetime as dt
import os

import pd_record

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


def dispatch(campaign: dict, *, send_date, batch_id, build_id, config, lookups, brevo_send,
             log_sink, pd_writer, state_advance, now=None) -> dict:
    """campaign: {campaign_id, email_type, track, template_id, rung, utm_campaign, brevo_list_id}.
    lookups: object with track_enabled(track), template_approved(template_id),
             audience(batch_id, build_id) -> [ {master_key,email,person_id,reason} ], suppressed(emails)->int.
    Raises SendLocked before ANY external call when a lock is closed."""
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(RIGA).date()
    closed = config_locks(allow_send=config.ALLOW_SEND, dry_run=config.DRY_RUN,
                          unlocked_by=os.environ.get("SEND_UNLOCKED_BY"), today=today)
    if closed:
        raise SendLocked(closed)                              # no lookup, no network
    audience = lookups.audience(batch_id, build_id)
    closed = data_locks(campaign=campaign, track_enabled=lookups.track_enabled(campaign["track"]),
                        template_approved=lookups.template_approved(campaign["template_id"]),
                        audience=audience,
                        suppressed_count=lookups.suppressed([a["email"] for a in audience]))
    if closed:
        raise SendLocked(closed)

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
    track_enabled = template_approved = audience = suppressed = _no


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
