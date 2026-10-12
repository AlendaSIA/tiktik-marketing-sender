"""RAIVIS D48 (MAIN 2 12.10 19:25): NOTHING MANUAL - the weekly 236 is launched by the send day itself.

After the Tuesday personal letters finish and list 98 is rebuilt from send_outcome (D46/D47), send_day calls launch():
  frame    = the ONE approved 'akcija_weekly' template (313), its Brevo bytes must equal the approved sha (D1)
  content  = frame with the week's ak_maker_html at <!--MAKER_BLOCKS--> (fill_frame); contact.AK_HTML stays personal
  audience = Brevo list 3 minus exclusion lists 4, 46 (EN), 75 (EE), 96 (NO_PROMO), 98 (personal letters + 98c/98d)
  sender   = the engine's campaign sender (campaign.SENDER_ID, replyTo info@tiktik.lv), same as every engine campaign
  gates    = D44 for 236 (incl. ak_tuesday_log BUILT) + AK-H1 (block_gate.ak_h1) + list 98 rebuilt today from the
             send day's outcome (and pushed to Brevo on LIVE) + approval sha = Brevo.
Any fail -> 236 is not launched, dash KĻŪDA + one alert mail to raivis@alenda.lv (LIVE). DRY builds everything and
measures the final audience, never creates a campaign on list 3. test() runs the SAME path (real campaign, real send)
against a list that holds exactly the two test addresses, subject prefixed "[TESTS 236 auto]"."""
from __future__ import annotations

import datetime as dt
import hashlib

import akcija_assembly as A
import akcija_campaign as AC

ALERT_TO = "raivis@alenda.lv"
TEST_RECIPIENTS = {"alenda.jurmala@gmail.com", "raivis@alenda.lv"}
TEST_PREFIX = "[TESTS 236 auto] "
EXCLUSIONS = [AC.LIST_SUPPRESSION, AC.LIST_EN, AC.LIST_EE, AC.LIST_NO_PROMO]       # + list 98 (looked up by name)


def build(q, brevo, d: dt.date, gate_date=None) -> dict:
    """Everything up to (not including) the campaign: gates, frame, html. -> {"holds": [...], "html", ...}."""
    import block_gate as BG
    tue, week_id, _end = A.week_of(d)
    out = {"week_id": week_id, "send_date": tue.isoformat(), "holds": []}
    frame_row, why = AC.frame_template(q)
    if why:
        out["holds"].append(why)
        return out
    tpl = brevo("GET", f"/smtp/templates/{frame_row['template_id']}", None) or {}
    frame = tpl.get("htmlContent") or ""
    live_sha = hashlib.sha256(frame.encode("utf-8")).hexdigest()
    out.update({"template_id": frame_row["template_id"], "subject": tpl.get("subject") or "",
                "frame_sha": live_sha[:12], "approved_sha": frame_row["approved_sha256"][:12]})
    if live_sha != frame_row["approved_sha256"]:
        out["holds"].append(f"D1 236 frame {frame_row['template_id']} brevo {live_sha[:12]} != approved "
                            f"{frame_row['approved_sha256'][:12]}")
    if not AC.frame_order_ok(frame):
        out["holds"].append("FRAME_ORDER: contact.AK_HTML must come before <!--MAKER_BLOCKS-->")
    gd = str(gate_date or d)
    h1 = BG.ak_h1(q, gd, week_id)
    if h1:
        out["holds"].append(h1)
    row, mwhy = BG.maker_html(q, week_id)
    if mwhy:
        out["holds"].append(mwhy)
    else:
        try:
            html = AC.fill_frame(frame, row["html"])
            out["html"] = html
            out["html_sha"] = hashlib.sha256(html.encode("utf-8")).hexdigest()[:12]
        except ValueError as e:
            out["holds"].append(f"FRAME: {e}")
    if not out["subject"].strip():
        out["holds"].append("SUBJECT: the 236 frame has no subject")
    return out


def create_and_send(brevo, b: dict, list_id: int, exclusions: list, name: str, subject_prefix: str = "") -> dict:
    import campaign as CAMP
    payload = {"name": name[:200], "subject": subject_prefix + b["subject"], "sender": {"id": CAMP.SENDER_ID},
               "replyTo": "info@tiktik.lv", "htmlContent": b["html"],
               "recipients": ({"listIds": [int(list_id)], "exclusionListIds": [int(x) for x in exclusions]}
                              if exclusions else {"listIds": [int(list_id)]}),        # Brevo refuses an empty list
               "inlineImageActivation": False}
    cid = int(brevo("POST", "/emailCampaigns", payload)["id"])
    hits = CAMP.placeholder_hits(payload["subject"], None, b["html"])
    got = brevo("GET", f"/emailCampaigns/{cid}", None) or {}
    rec = got.get("recipients") or {}
    if sorted(rec.get("lists") or []) != [int(list_id)] or \
            sorted(rec.get("exclusionLists") or []) != sorted(int(x) for x in exclusions):
        return {"campaign_id": cid, "sent": False, "refused": f"RECIPIENTS_READ_BACK_DIFFER {rec}"}
    brevo("POST", f"/emailCampaigns/{cid}/sendNow", None)
    return {"campaign_id": cid, "sent": True, "unfilled_marks_in_frame_text": hits[:5]}


def final_audience(q, brevo, d: dt.date, excl98_emails: set, list98_id) -> dict:
    """The 236 audience as Brevo will count it: list 3 members not blacklisted, minus lists 4/46/75/96 and list 98.
    DRY passes the FORECAST list 98 (send-day outcome table) because a DRY day never pushes 98 to Brevo."""
    import flow_gate as FG

    def members(lid, with_bl=False):
        out, off = {}, 0
        while True:
            r = brevo("GET", f"/contacts/lists/{lid}/contacts?limit=500&offset={off}", None) or {}
            page = r.get("contacts") or []
            for c in page:
                out[(c.get("email") or "").strip().lower()] = bool(c.get("emailBlacklisted"))
            off += 500
            if len(page) < 500:
                return out
    l3 = members(AC.LIST_ALL)
    ex = set()
    per = {}
    for lid in EXCLUSIONS:
        m = set(members(lid))
        per[lid] = len(m & set(l3))
        ex |= m
    bl = {e for e, b in l3.items() if b}
    final = set(l3) - bl - ex - excl98_emails
    return {"list3": len(l3), "blacklisted": len(bl), "excluded_by_list": per,
            "excluded_by_98": len((set(l3) - bl - ex) & excl98_emails), "final": len(final)}


def launch(q, brevo, d: dt.date, live: bool, refill: dict, d44: dict | None, dash, gate_date=None, pace=None,
           extra_holds=()) -> dict:
    import flow_gate as FG
    b = build(q, brevo, d, gate_date)
    holds = list(b["holds"]) + list(extra_holds)
    holds += [c for c in (d44 or {}).get("akcija_236", [])]
    if not refill or refill.get("error"):
        holds.append(f"LIST_98_NOT_REBUILT {(refill or {}).get('error') or 'no refill'}")
    elif live and not (refill.get("brevo") or {}).get("list_id"):
        holds.append("LIST_98_NOT_PUSHED_TO_BREVO")
    excl = {r["email"] for r in q(f"SELECT email FROM `{FG.T_EXCL}` WHERE send_date = DATE '{d}'")}
    lid98 = (refill or {}).get("brevo", {}).get("list_id") if refill and refill.get("brevo") else None
    out = {k: v for k, v in b.items() if k != "html"}
    out["audience"] = final_audience(q, brevo, d, excl, lid98)
    out["holds"] = holds
    if holds:
        out["launched"] = False
        if live:
            text = f"236 {d} NAV palaists: " + "; ".join(holds)
            dash("KĻŪDA", text[:400])
            brevo("POST", "/smtp/email", {"sender": {"id": 2}, "to": [{"email": ALERT_TO}],
                                          "subject": f"236 {d} nav palaists - {len(holds)} iemesli",
                                          "textContent": text, "tags": ["d48-236"]})
        return out
    if not live:
        out["launched"] = False
        out["dry"] = "built and measured; DRY never creates a campaign on list 3"
        return out
    lid98 = refill["brevo"]["list_id"]
    if pace:
        pace()                                   # D49: 236 last, >= 10 min after the last personal campaign
    out.update(create_and_send(brevo, b, AC.LIST_ALL, EXCLUSIONS + [int(lid98)], f"236 {d} {b['week_id']} (engine)"))
    out["launched"] = out.get("sent", False)
    return out


def test(q, brevo, d: dt.date, gate_date=None) -> dict:
    """D48 test: the SAME build + create + send path on a list holding exactly the two test addresses."""
    import flow_gate as FG
    b = build(q, brevo, d, gate_date)
    out = {k: v for k, v in b.items() if k != "html"}
    if "html" not in b or any(h.startswith(("D1", "FRAME", "SUBJECT", "HOLD MAKER")) for h in b["holds"]):
        out["refused"] = "content not buildable: " + "; ".join(b["holds"])
        return out
    lid = FG.list_ids(q, brevo, {"TEST_236_AUTO": "TEST 236 auto - tikai alenda.jurmala + raivis"})["TEST_236_AUTO"]
    FG.sync_one(brevo, lid, set(TEST_RECIPIENTS))
    now = FG.list_members(brevo, lid)
    if now != TEST_RECIPIENTS:
        out["refused"] = f"test list {lid} holds {sorted(now)}"
        return out
    out["test_list_id"] = lid
    out.update(create_and_send(brevo, b, lid, [], f"TEST 236 auto {d} {b['week_id']}", TEST_PREFIX))
    return out
