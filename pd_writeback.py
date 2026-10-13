"""PD write-back for one letter at LIVE send (contract v2.9.3 + v2.9.4, sha 3818b716589d). Pure.

v2.9.3 - level = PERSON. Per sent letter:
  * ONE activity, type _e_pasts_automatisks (id 32), done=1, person + org (+ participants for a
    shared mailbox), subject = letter type + offer summary, note = products / prices / OFFER_VALID_UNTIL
    (rendered by pd_record.render - the same bytes in shadow and live);
  * person fields on the recipient person: 185 "MKT Atlaides pakāpiens" (enum 724..727 = rung 0..3),
    184 "MKT Piedāvājums spēkā līdz" (date, empty when no offer), 167 LAST_CAMPAIGN (email type),
    168 LAST_CAMPAIGN_DATE (send date). CAMPAIGN_HISTORY (171) is NOT used.
v2.9.4 - a missing PD person/org is created (Raivis 28.09 "jā lai izveido ja nav viena vai otra"):
  * no person -> person (+ org with the same name when none can be linked);
  * person without org -> org with the same name, then the person is linked;
  * GUARD before any org create: search existing orgs by reg. nr -> exact name -> e-mail domain (never a
    free-mail domain); ONE hit -> link to it; several hits -> held pd_org_ambiguous (no guess, no create).
This module only PLANS the writes. Shadow stores them in mkt_control.shadow_pd_writes; nothing here calls
Pipedrive. Held targets (duplicates, multi-org, stale data) stay in the pending queue, as before.
"""
from __future__ import annotations

import datetime as dt
import re

ACTIVITY_TYPE_KEY = "_e_pasts_automatisks"
ACTIVITY_TYPE_ID = 32
F_RUNG = "20e977e74489c1ff17fcea50c5a65e09692d1d60"          # 185 MKT Atlaides pakāpiens (enum)
RUNG_OPTION = {0: 724, 1: 725, 2: 726, 3: 727}
F_OFFER_UNTIL = "98eb0a33b6563fd78b1a704dfec70a581cfd5dd5"   # 184 MKT Piedāvājums spēkā līdz (date)
F_LAST_CAMPAIGN = "29a3179972cc1079f80eeefdb4aeec5af4837b1e"   # 167 LAST_CAMPAIGN (varchar = email type)
F_LAST_CAMPAIGN_DATE = "a849d7df5eeded7a2fc631a6f86261858454199f"  # 168 LAST_CAMPAIGN_DATE

HOLD_ORG_AMBIGUOUS = "pd_org_ambiguous"

# A domain match links to a COMPANY only when the domain is the company's own. Free-mail domains never match.
FREE_MAIL = {
    "gmail.com", "googlemail.com", "inbox.lv", "mail.ru", "list.ru", "bk.ru", "yandex.ru", "yandex.com",
    "yahoo.com", "hotmail.com", "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "one.lv",
    "apollo.lv", "tvnet.lv", "e-pasts.lv", "gmx.de", "gmx.net", "gmx.com", "protonmail.com", "proton.me",
    "mail.com", "aol.com", "delfi.lv", "navigator.lv", "online.lv", "rambler.ru", "ukr.net", "inbox.ru",
    "hot.ee", "mail.ee", "gmail.lv", "zoho.com", "tutanota.com", "web.de", "wp.pl", "o2.pl", "interia.pl",
}
_LEGAL = re.compile(r'\b(sia|as|ik|z/s|zs|ooo|uab|ou|oü|ltd|llc|gmbh)\b|["\'«»“”„]', re.I)


def norm_name(name: str | None) -> str:
    return " ".join(_LEGAL.sub(" ", (name or "").lower()).split())


def norm_reg(reg: str | None) -> str:
    return re.sub(r"\D", "", reg or "")


def domain(email: str | None) -> str | None:
    e = (email or "").strip().lower()
    return e.rsplit("@", 1)[1] if "@" in e else None


def build_org_index(orgs, persons) -> dict:
    """orgs: [{id, name, reg_number}]; persons: [{org_id, emails}] - domain -> orgs via the persons in them."""
    idx = {"reg": {}, "name": {}, "domain": {}}
    for o in orgs:
        r = norm_reg(o.get("reg_number"))
        if r:
            idx["reg"].setdefault(r, set()).add(o["id"])
        n = norm_name(o.get("name"))
        if n:
            idx["name"].setdefault(n, set()).add(o["id"])
    for p in persons:
        if p.get("org_id") is None:
            continue
        for e in p.get("emails") or ():
            d = domain(e)
            if d and d not in FREE_MAIL:
                idx["domain"].setdefault(d, set()).add(p["org_id"])
    return idx


def find_org(idx: dict, *, reg_nr=None, name=None, email=None) -> tuple:
    """-> (org_id | None, how | None, ambiguous: bool). Order: reg. nr, exact name, own e-mail domain."""
    for how, key in (("reg_nr", norm_reg(reg_nr)), ("exact_name", norm_name(name)), ("domain", domain(email))):
        if not key or (how == "domain" and key in FREE_MAIL):
            continue
        hits = idx[{"reg_nr": "reg", "exact_name": "name", "domain": "domain"}[how]].get(key) or set()
        if len(hits) == 1:
            return next(iter(hits)), how, False
        if len(hits) > 1:
            return None, how, True
    return None, None, False


def field_writes(person_ref, *, email_type: str, send_date: dt.date, offer_rung: int, offer_valid_until) -> list:
    """The four v2.9.3 person-field writes for the recipient person (person_ref = id or 'new:person')."""
    ovu = offer_valid_until.isoformat() if isinstance(offer_valid_until, dt.date) else (offer_valid_until or "")
    sd = send_date.isoformat() if isinstance(send_date, dt.date) else send_date
    opt = RUNG_OPTION.get(offer_rung or 0)
    rung_write = ({"object": "person_field", "person_ref": person_ref, "field_key": F_RUNG, "field_value": str(opt)}
                  if opt is not None else   # LQ7 rung 4: field 185 has no option -> never a guessed value; counted
                  {"object": "person_field_skipped", "person_ref": person_ref, "field_key": F_RUNG,
                   "field_value": None, "reason": f"field 185 has no option for rung {offer_rung}"})
    return [
        rung_write,
        {"object": "person_field", "person_ref": person_ref, "field_key": F_OFFER_UNTIL, "field_value": ovu},
        {"object": "person_field", "person_ref": person_ref, "field_key": F_LAST_CAMPAIGN, "field_value": email_type},
        {"object": "person_field", "person_ref": person_ref, "field_key": F_LAST_CAMPAIGN_DATE, "field_value": sd},
    ]


def plan(target, *, email: str, person_name: str | None, org_name: str | None, reg_nr: str | None,
         org_idx: dict) -> dict:
    """What the live path would create/link BEFORE the activity + field writes. Returns
    {person_ref, org_ref, creates: [rows], hold_reason}. target = pd_target.Target."""
    creates = []
    if target.kind == "held" and target.cls != "C5":
        return {"person_ref": None, "org_ref": None, "creates": [], "hold_reason": target.hold_reason}

    person_ref, org_ref = target.person_id, target.org_id
    need_person = target.cls == "C5" or target.kind == "org_only"
    need_org = org_ref is None
    if need_org:
        oname = org_name or person_name or email
        oid, how, amb = find_org(org_idx, reg_nr=reg_nr, name=oname, email=email)
        if amb:
            return {"person_ref": None, "org_ref": None, "creates": [], "hold_reason": HOLD_ORG_AMBIGUOUS}
        if oid is not None:
            org_ref = oid
            creates.append({"object": "org_link", "org_ref": oid, "match_how": how, "name": oname})
        else:
            org_ref = "new:org"
            creates.append({"object": "org_create", "org_ref": "new:org", "name": oname, "reg_nr": norm_reg(reg_nr) or None})
    if need_person:
        person_ref = "new:person"
        creates.append({"object": "person_create", "person_ref": "new:person", "name": person_name or email,
                        "email": email, "org_ref": org_ref})
    elif need_org and person_ref is not None:
        creates.append({"object": "person_org_link", "person_ref": person_ref, "org_ref": org_ref})
    return {"person_ref": person_ref, "org_ref": org_ref, "creates": creates, "hold_reason": None}


def offer_summary(slots: list) -> tuple:
    """slots: [{name, price, ref}] = P1..P8 of today's mkt_control.letter_fields row, texts as the letter carries
    them -> (subject tail, note lines)."""
    shown = [s for s in slots if s.get("name") and s.get("price")]
    priced = [s for s in shown if s.get("ref")]
    tail = f"{len(priced)} personīgas cenas" if priced else "bez personīgas cenas"
    lines = [f"{s['name']}: {s['price']}" + (f" (veikalā {s['ref']})" if s.get("ref") else "") for s in shown]
    return tail, lines
