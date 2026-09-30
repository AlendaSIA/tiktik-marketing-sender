"""A-Z review round (MAIN COMMAND 1 to 'Vestulu sabloni 2', 2026-09-28): ONE prepared template rendered as ONE real
contact the engine plans, with today's shadow values laid over the contact's live Brevo attributes, every pre-send
check run, and - only with --send and every check green - ONE test letter to raivis@alenda.lv. Nobody else, ever.

    python draft_test_az.py --template 180 --commit <40 hex> --week 2026-w40 --contact <email> \
        --variant winback_1 --seq 5/10 --overlay-b64 <b64 json> --note-b64 <b64 text> [--send]
    python draft_test_az.py --summary-b64 <b64 html> --send          # the one summary letter

1. The template is the FILE the manifest names at --commit (blob sha + sha256 checked), never the live Brevo template:
   the v2.9.1 / L4 versions have not reached Brevo (MAIN: no template_put until Raivis' ratings).
2. attrs = live Brevo attributes of --contact, then the overlay (shadow_brevo_price_attrs of today's plan_date:
   P<n>_PRICE, P<n>_REF_PRICE, P1_FRESH, OFFER_VALID_UNTIL, OFFER_RUNG, GREETING).
3. Checks: draft_test.static_checks; draft_test.contact_checks on the letter AS THE CUSTOMER WOULD GET IT (the
   customer's own cabinet link must answer as a booted cabinet); draft_test_v28.display_checks (P2, P4, P6);
   G2 greeting line = GREETING (default 'Sveiki!'); L4 words; reorder letters carry no discount words.
4. The copy that goes to Raivis: every link to plani.tiktik.lv (the customer's cabinet, token inside) is replaced by
   the cabinet link of OWN_CONTACT (Raivis' own address, rule-11 TEST_CONTACT); if that link does not pass the same
   link check, the links are disabled (href="#"). A yellow banner says which, with the contact masked.
"""
import argparse
import base64
import hashlib
import html as _html
import json
import re
import sys

import campaign as C
import draft_test as D
import draft_test_v28 as V
import template_put as T

TEST_RECIPIENT = D.TEST_RECIPIENT            # raivis@alenda.lv - the only recipient
OWN_CONTACT = "alenda.jurmala@gmail.com"     # Raivis' own contact (contract v2.7 rule 11)
ROUND_TAG = "az-2026-09-30c"                  # MAIN COMMAND 2 round
REF_126 = "126 „tavs personīgais piedāvājums” (15.07., 4,67 % klikšķu) + preču kartītes no 222 „Mercator nedēļa”"
STYLE_REF = {236: "222 „Mercator nedēļa” (22.09.) — mūsu nedēļas akcijas formāts"}
PRICE_LETTERS = {180, 232, 233, 234, 9180, 9232, 9233}   # LADDER POLICY L1 + CADENCE v1 E2 (provisional ids)
REORDER_LETTERS = {179, 230, 231}            # L1 / P5: no ladder, no discount words
L4_FORBIDDEN = ["vēl lētāk", "atkal", "šoreiz", "pakāp", "solis lētāk", "nākamreiz lētāk", "vēl zemāk"]
DISCOUNT_WORDS = ["atlaid", "lētāk", "zemāk par"]
_PLANI = re.compile(r"^https://plani\.tiktik\.lv/")


def mask(email):
    local, _, dom = (email or "").partition("@")
    return (local[:2] + "***@" + dom) if dom else "***"


def greeting_check(rendered, attrs):
    """G2: the greeting is the letter's first line - its own block (126 frame) or the start of the first paragraph
    (222 weekly frame, 236)."""
    want = str(attrs.get("GREETING") or "") or "Sveiki!"
    ok = any(re.search(">" + re.escape(w) + r"(</p>|</div>| )", rendered)
             for w in (want, _html.escape(want, quote=False)))
    return {"expected": want, "ok": ok}


def words_check(template_id, rendered, subject, attrs):
    text = (subject or "") + "\n" + "\n".join(V.visible_lines(rendered)) + "\n" + (V.preheader(rendered) or "")
    low = text.lower()
    out = {"l4_forbidden_found": [], "reorder_discount_found": []}
    if template_id in PRICE_LETTERS:
        out["l4_forbidden_found"] = [w for w in L4_FORBIDDEN if w in low]
    if template_id in REORDER_LETTERS:
        found = [w for w in DISCOUNT_WORDS if w in low]
        # a price in force (P5) may show its REF + TAVA CENA; words of a discount offer may not
        out["reorder_discount_found"] = found
    out["ok"] = not out["l4_forbidden_found"] and not out["reorder_discount_found"]
    return out


def neutralise(rendered, own_url):
    """Every plani.tiktik.lv href -> own_url (or '#'). Returns (html, replaced_count, left_customer_hrefs)."""
    n = [0]

    def sub(m):
        q, url = m.group(1), m.group(2)
        if _PLANI.match(_html.unescape(url)):
            n[0] += 1
            return 'href=' + q + _html.escape(own_url or "#", quote=True) + q
        return m.group(0)
    out = re.sub(r'href=(["\'])(.*?)\1', sub, rendered, flags=re.S)
    left = [h for h in re.findall(r'href=["\'](.*?)["\']', out, re.S)
            if _PLANI.match(_html.unescape(h)) and _html.unescape(h) != (own_url or "#")]
    return out, n[0], left


def banner(text):
    return ('<div style="background:#fff3cd;border-bottom:2px solid #e0b000;padding:10px 14px;'
            'font:13px/1.5 Arial,sans-serif;color:#5c4400;">' + text + '</div>')


def send(subject, html_body, tag):
    payload = {"sender": {"id": C.SENDER_ID}, "to": [{"email": TEST_RECIPIENT}], "subject": subject,
               "htmlContent": html_body, "tags": ["draft-test", ROUND_TAG, tag]}
    assert payload["to"] == [{"email": TEST_RECIPIENT}], "recipient guard"
    return C._call("POST", "/smtp/email", payload)


def summary(a):
    body = base64.b64decode(a.summary_b64).decode("utf-8")
    doc = ('<!DOCTYPE html><html><head><meta charset="utf-8"></head><body style="font:15px/1.6 Arial,sans-serif;'
           'color:#1c2b23;max-width:680px;padding:16px;">' + body + '</body></html>')
    report = {"mode": "summary", "to": TEST_RECIPIENT, "bytes": len(doc), "sent": None}
    if a.send:
        report["sent"] = send("[TESTS · kopsavilkums] 10 vēstules A–Z — atbildi: der N / labot N", doc, "az-summary")
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", type=int)
    ap.add_argument("--commit")
    ap.add_argument("--week")
    ap.add_argument("--contact")
    ap.add_argument("--variant")
    ap.add_argument("--seq")
    ap.add_argument("--overlay-b64", default="")
    ap.add_argument("--note-b64", default="")
    ap.add_argument("--fill-b64", default="", help="236 only: {token: value} for the ⟦…⟧ frame, as the week's builder fills it")
    ap.add_argument("--summary-b64", default="")
    ap.add_argument("--manifest", default="templates_manifest.json")
    ap.add_argument("--send", action="store_true")
    a = ap.parse_args(argv)
    if a.summary_b64:
        return summary(a)
    if not (a.template and a.commit and a.week and a.contact and a.variant and a.seq):
        sys.exit("--template --commit --week --contact --variant --seq are required")
    if not re.fullmatch(r"[0-9a-f]{40}", a.commit):
        sys.exit("commit must be a full 40-hex sha")
    if not re.fullmatch(r"\d{4}-w\d{2}", a.week):
        sys.exit("week must be YYYY-Www")
    if not re.fullmatch(r"[a-z0-9_]{3,40}", a.variant) or not re.fullmatch(r"\d{1,2}/\d{1,2}", a.seq):
        sys.exit("bad variant/seq")
    overlay = json.loads(base64.b64decode(a.overlay_b64).decode("utf-8")) if a.overlay_b64 else {}
    note = base64.b64decode(a.note_b64).decode("utf-8") if a.note_b64 else ""
    m = json.load(open(a.manifest, encoding="utf-8"))
    rows = [r for r in m["templates"] + m.get("live_mapped", []) + m.get("episode_e2", []) if r["id"] == a.template]
    if len(rows) != 1:
        sys.exit("template %d is not in the manifest" % a.template)
    row = rows[0]
    raw = T.fetch(a.commit, row["file"])
    rep = {"template": a.template, "variant": a.variant, "seq": a.seq, "contact": mask(a.contact),
           "file_ok": T.blob_sha(raw) == row["git_blob"] and hashlib.sha256(raw).hexdigest() == row["sha256"],
           "template_sha256": hashlib.sha256(raw).hexdigest()}
    if not rep["file_ok"]:
        rep["refused"] = "the file at this commit does not match the manifest"
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 2
    tpl, subject = raw.decode("utf-8"), row["subject"]
    fill = json.loads(base64.b64decode(a.fill_b64).decode("utf-8")) if a.fill_b64 else {}
    if fill and a.template != 236:
        sys.exit("--fill-b64 is only for the 236 frame")
    for k, v in fill.items():
        tpl, subject = tpl.replace(k, v), subject.replace(k, v)
    rep["filled_tokens"] = sorted(fill)
    live = C.contact_attributes(a.contact)
    attrs = dict(live)
    attrs.update(overlay)
    rep["overlay_keys"] = sorted(overlay)
    # The shadow row places prices by SKU in the v2.7 slot order; NAME and IMG come from live Brevo. Proof that the two
    # agree: a slot without REF must carry the live price, a slot with REF must carry the live price as its reference.
    align = []
    for i in range(1, 9):
        if not live.get(f"P{i}_NAME") or f"P{i}_PRICE" not in overlay:
            continue
        ref = str(overlay.get(f"P{i}_REF_PRICE") or "")
        want = ref if ref else str(overlay.get(f"P{i}_PRICE") or "")
        align.append({"slot": i, "live_price": live.get(f"P{i}_PRICE"), "overlay_shop": want,
                      "ok": str(live.get(f"P{i}_PRICE") or "") == want})
    rep["slot_alignment"] = align
    rep["live_has_greeting"] = "GREETING" in live

    st = D.static_checks(tpl, subject)
    static_ok = st["complete_html"] and not (st["outside_contract"] or st["nested_double_quote_hrefs"]
                                             or st["template_side_utm"] or st["percent_in_text"])
    sent_html, seam = D.send_path_html(tpl, a.week)
    cc = D.contact_checks(sent_html, subject, a.contact, a.week, attrs=attrs) if sent_html is not None else None
    rendered = cc["html"] if cc else ""
    v28 = V.display_checks(tpl, rendered, subject, attrs)
    p4_ok = v28["p4"]["ok"]
    # 236 is the list-campaign frame: its weekly tokens are filled by the campaign layer, reported, not failed.
    left_ok = not v28["placeholders_left"] or a.template == 236
    v28_ok = v28["p2"]["ok"] and p4_ok and v28["p6"]["ok"] and left_ok and v28["claims_ok"]
    g = greeting_check(rendered, attrs)
    w = words_check(a.template, rendered, cc["subject"] if cc else subject, attrs)
    rep.update(static_ok=static_ok, static={k: st[k] for k in st if k != "attributes"}, utm_seam=seam,
               contact_checks={k: v for k, v in (cc or {}).items() if k not in ("html", "contact")},
               v28={"p2": v28["p2"], "p4_ok": p4_ok, "p6": v28["p6"], "placeholders_left": v28["placeholders_left"],
                    "claims_ok": v28["claims_ok"], "h1": v28["h1"]},
               greeting=g, words=w,
               excerpt=[V.preheader(rendered) or ""] + V.visible_lines(rendered)[:30])
    rep["all_ok"] = bool(static_ok and cc and cc["ok"] and v28_ok and g["ok"] and w["ok"]
                         and all(x["ok"] for x in align))

    own = C.contact_attributes(OWN_CONTACT)
    own_url = str(own.get("KABINETS_URL") or "")
    own_ok = bool(own_url) and f"utm_campaign={a.week}-" in own_url and D.link_verdict(own_url)["ok"]
    body, replaced, left = neutralise(rendered, own_url if own_ok else "#")
    rep["links_in_test"] = {"replaced": replaced, "to": "own cabinet" if own_ok else "disabled (#)",
                            "customer_links_left": len(left)}
    if left:
        rep["all_ok"] = False
    rung = attrs.get("OFFER_RUNG", 0)
    btxt = (f"<b>[TESTS {a.seq} · {a.variant} · {a.template}]</b> Renderēts kā {_html.escape(mask(a.contact))}, "
            f"{a.variant}, pakāpiens {rung}. <b>Klientam NAV sūtīts.</b><br>"
            + ("Kabineta saites šajā testā ved uz TAVU kabinetu, ne klienta." if own_ok
               else "Kabineta saites šajā testā ir atslēgtas (#).")
            + "<br>Stils kā mūsu kampaņā " + _html.escape(STYLE_REF.get(a.template, REF_126)) + "."
            + (("<br>" + _html.escape(note)) if note else ""))
    final = re.sub(r"(<body[^>]*>)", lambda mm: mm.group(1) + banner(btxt), body, count=1)
    rep["sent"] = None
    if a.send:
        if not rep["all_ok"]:
            rep["send_refused"] = "not all checks green - nothing sent"
        else:
            subj = f"[TESTS {a.seq} · {a.variant} · {a.template}] {cc['subject']}"
            rep["sent"] = send(subj, final, f"tpl-{a.template}")
    print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    return 0 if rep["all_ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
