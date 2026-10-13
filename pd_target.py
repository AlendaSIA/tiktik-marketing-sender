"""Who a lifecycle letter's Pipedrive activity lands on (MAIN 2026-09-28 COMMAND 4, P-A..P-E). Pure.

The PD target is resolved by the SEND ADDRESS at plan time from channel_raw.pipedrive_persons, never
from customer_lifecycle.person_id (that one is "a person of the org the frozen client->org map
points at" - measured 28.09: 456 of 1 384 had none, 53 pointed at the wrong person).

Classes (report 3) -> outcome:
  C1  exactly one person holds the address        -> kind person       (that person + its org)
  C2a several holders, one name, one org (dups)    -> HELD pd_duplicate_person (merge first; never a 2nd)
  C2b several holders = shared mailbox, one org    -> kind org_participants (org + every holder;
                                                      primary = holder whose PRIMARY email is the
                                                      address, else newest update_time, else lowest id)
  C2c shared mailbox across several orgs           -> HELD pd_ambiguous_org, unless a human override row
                                                      (mkt_control.pd_target_override) names the target
  C3  nobody holds it, a person holds another address of the same master_key
                                                   -> that person (the address is NOT added to it)
  C4  the address sits only on an organisation     -> kind org_only
  C5  nowhere in PD                                -> HELD pd_no_person (creating a person = Raivis)
  persons snapshot older than 26 h                 -> HELD pd_persons_stale (never guessed)
Nothing here writes anything; the caller renders pd_record and, in shadow, stores it.
"""
from __future__ import annotations

import dataclasses as dc
import datetime as dt

PERSONS_MAX_AGE_H = 26

HOLD_STALE = "pd_persons_stale"
HOLD_DUP = "pd_duplicate_person"
HOLD_MULTI_ORG = "pd_ambiguous_org"
HOLD_NO_PERSON = "pd_no_person"


@dc.dataclass(frozen=True)
class Person:
    id: int
    org_id: int | None
    name: str | None
    email_primary: str | None
    update_time: dt.datetime | None = None


@dc.dataclass(frozen=True)
class Target:
    kind: str                         # person | org_participants | org_only | held
    cls: str                          # C1 | C2a | C2b | C2c | C3 | C4 | C5 | stale | override
    person_id: int | None = None      # primary participant (None for org_only / held)
    org_id: int | None = None
    participants: tuple = ()          # all participant person ids, primary first
    hold_reason: str | None = None


def norm(email: str | None) -> str:
    return (email or "").strip().lower()


def _name(p: Person) -> str:
    return " ".join((p.name or "").lower().split())


def _primary(holders, address) -> Person:
    ts_min = dt.datetime.min.replace(tzinfo=dt.timezone.utc)

    def key(p):
        ut = p.update_time or ts_min
        if ut.tzinfo is None:
            ut = ut.replace(tzinfo=dt.timezone.utc)
        return (norm(p.email_primary) == address, ut, -p.id)
    return max(holders, key=key)


def _from_holders(holders, address, override, cls_one) -> Target:
    uniq = {p.id: p for p in holders}
    holders = sorted(uniq.values(), key=lambda p: p.id)
    if len(holders) == 1:
        p = holders[0]
        return Target("person", cls_one, p.id, p.org_id, (p.id,))
    orgs = {p.org_id for p in holders if p.org_id is not None}
    if len({_name(p) for p in holders}) == 1 and len(orgs) <= 1:
        return Target("held", "C2a", hold_reason=HOLD_DUP)
    if len(orgs) > 1:
        if override is not None:
            return override
        return Target("held", "C2c", hold_reason=HOLD_MULTI_ORG)
    prim = _primary(holders, address)
    parts = (prim.id,) + tuple(p.id for p in holders if p.id != prim.id)
    return Target("org_participants", "C2b", prim.id, next(iter(orgs), None), parts)


def resolve(address: str, *, by_address: dict, master_other_addresses=(), org_by_address: dict | None = None,
            overrides: dict | None = None, persons_age_h: float | None = None) -> Target:
    """by_address: {address: [Person]} from the PD snapshot; org_by_address: {address: {org_id}};
    overrides: {address: (person_id|None, org_id|None)}; persons_age_h: age of the snapshot."""
    if persons_age_h is None or persons_age_h > PERSONS_MAX_AGE_H:
        return Target("held", "stale", hold_reason=HOLD_STALE)
    a = norm(address)
    ov = (overrides or {}).get(a)
    override = None
    if ov is not None:
        pid, oid = ov
        override = Target("person" if pid else "org_only", "override", pid, oid, (pid,) if pid else ())
    holders = by_address.get(a) or []
    if holders:
        return _from_holders(holders, a, override, "C1")
    other = [p for x in master_other_addresses if norm(x) != a for p in (by_address.get(norm(x)) or [])]
    if other:
        t = _from_holders(other, a, override, "C3")
        return dc.replace(t, cls="C3") if t.kind == "person" else t
    orgs = (org_by_address or {}).get(a) or set()
    if len(orgs) == 1:
        return Target("org_only", "C4", None, next(iter(orgs)), ())
    if len(orgs) > 1:
        return override or Target("held", "C2c", hold_reason=HOLD_MULTI_ORG)
    return override or Target("held", "C5", hold_reason=HOLD_NO_PERSON)
