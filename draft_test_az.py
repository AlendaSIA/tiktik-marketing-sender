"""A-Z review round (MAIN COMMAND 1 to 'Vestulu sabloni 2', 2026-09-28): ONE prepared template rendered as ONE real
contact the engine plans, with today's shadow values laid over the contact's live Brevo attributes, every pre-send
check run, and - only with --send and every check green - ONE test letter to raivis@alenda.lv. Nobody else, ever.

    python draft_test_az.py --template 180 --commit <40 hex> --week 2026-w40 --contact <email> \
        --variant winback_1 --seq 5/10 --overlay-b64 <b64 json> --note-b64 <b64 text> [--send]
    python draft_test_az.py --summary-b64 <b64 html> --send          # the one summary letter

1. The template is the FILE the manifest names at --commit (blob sha + sha256 checked), never the live Brevo template:
   the v2.9.1 / L4 versions have not reached Brevo (MAIN: no template_put until Raivis' ratings).
2. attrs = live Brevo attributes of --contact, then the overlay. With --plan-date the overlay is READ HERE from
   mkt_control.letter_fields (the contact's row of that plan_date and this template: every contract field the row
   carries). shadow_brevo_price_attrs is retired (MAIN 2026-10-06) and is not read anywhere. --overlay-b64 remains
   for a hand-made overlay; the two cannot be combined.
   244: the row's ANKETA_URL / ORDER_NR must equal the contact's mkt_control.letter_fields_244 row of the same day.
3. Checks: draft_test.static_checks; draft_test.contact_checks on the letter AS THE CUSTOMER WOULD GET IT (the
   customer's own cabinet link must answer as a booted cabinet); draft_test_v28.display_checks (P2, P4, P6);
   G2 greeting line = GREETING (default 'Sveiki!'); L4 words; reorder letters carry no discount words.
4. The copy that goes to Raivis: every link to plani.tiktik.lv (the customer's cabinet, token inside) is replaced by
   the cabinet link of OWN_CONTACT (Raivis' own address, rule-11 TEST_CONTACT); if that link does not pass the same
   link check, the links are disabled (href="#"). A yellow banner says which, with the contact masked.
   244: the survey link is the customer's own order survey - checked live as the customer gets it, then DISABLED
   (href="#") in the copy to Raivis, so a test click can never file an answer on a customer's order.
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
SURVEY_LETTERS = {244}                       # the only plani link is the customer's per-order survey
T_LETTER_FIELDS = "`jaunais-za-aizv04022026.mkt_control.letter_fields`"
T_LETTER_FIELDS_244 = "`jaunais-za-aizv04022026.mkt_control.letter_fields_244`"
_PLANI = re.compile(r"^https://plani\.tiktik\.lv/")


def _bq_rows(sql, email, plan_date, template_id=None):
    """Read-only, parameterised. bq is imported here so that the offline tests never need google-cloud."""
    import bq as B
    from google.cloud import bigquery
    params = [bigquery.ScalarQueryParameter("email", "STRING", email.strip().lower()),
              bigquery.ScalarQueryParameter("d", "DATE", plan_date)]
    if template_id is not None:
        params.append(bigquery.ScalarQueryParameter("tid", "INT64", template_id))
    return [dict(r.items()) for r in B.query(sql, params)]


def overlay_from_letter_fields(email, template_id, plan_date, rows=_bq_rows):
    """(overlay, info). The ONE mkt_control.letter_fields row of (plan_date, email, template_id); the overlay is
    every CONTRACT field that row carries (NULL = not carried). No row or more than one row is a refusal, not a
    guess. For a survey letter the row must agree with letter_fields_244 (the table the writer mints links into)."""
    got = rows(f"SELECT * FROM {T_LETTER_FIELDS} WHERE plan_date = @d AND LOWER(email) = @email AND template_id = @tid",
               email, plan_date, template_id)
    info = {"source": "mkt_control.letter_fields", "plan_date": plan_date, "rows": len(got)}
    if len(got) != 1:
        info["refused"] = f"{len(got)} letter_fields rows for this contact, template and plan_date (need exactly 1)"
        return {}, info
    row = got[0]
    overlay = {k: v for k, v in row.items() if k in D.CONTRACT_FIELDS and v is not None}
    info.update(run_id=row.get("run_id"), contract_pin=row.get("contract_pin"), email_type=row.get("email_type"),
                would_send=row.get("would_send"), hold_reason=row.get("hold_reason"))
    if template_id in SURVEY_LETTERS:
        r244 = rows(f"SELECT ORDER_NR, ANKETA_URL, source, is_proof FROM {T_LETTER_FIELDS_244} "
                    "WHERE plan_date = @d AND LOWER(email) = @email", email, plan_date)
        same = len(r244) == 1 and all(str(r244[0].get(k) or "") == str(overlay.get(k) or "") != ""
                                      for k in ("ANKETA_URL", "ORDER_NR"))
        info["letter_fields_244"] = {"rows": len(r244), "equal": same,
                                     "source": r244[0].get("source") if len(r244) == 1 else None}
        if not same:
            info["refused"] = "letter_fields ANKETA_URL/ORDER_NR do not equal the one letter_fields_244 row of that day"
    return overlay, info


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
    ap.add_argument("--plan-date", default="", help="YYYY-MM-DD: read the overlay from mkt_control.letter_fields")
    ap.add_argument("--note-b64", default="")
    ap.add_argument("--fill-b64", default="", help="236 only: {token: value} for the ⟦…⟧ frame, as the week's builder fills it")
    ap.add_argument("--summary-b64", default="")
    ap.add_argument("--manifest", default="templates_manifest.json")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--clean", action="store_true", help="Raivis 30.09: the copy to raivis@alenda.lv as the customer sees it - no banner, subject without [TESTS] (still only to raivis@alenda.lv, tag draft-test)")
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
    if a.plan_date and a.overlay_b64:
        sys.exit("--plan-date and --overlay-b64 cannot be combined")
    if a.plan_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.plan_date):
        sys.exit("plan-date must be YYYY-MM-DD")
    overlay = json.loads(base64.b64decode(a.overlay_b64).decode("utf-8")) if a.overlay_b64 else {}
    overlay_info = {"source": "--overlay-b64" if a.overlay_b64 else "none"}
    if a.plan_date:
        overlay, overlay_info = overlay_from_letter_fields(a.contact, a.template, a.plan_date)
    note = base64.b64decode(a.note_b64).decode("utf-8") if a.note_b64 else ""
    m = json.load(open(a.manifest, encoding="utf-8"))
    rows = [r for r in m["templates"] + m.get("live_mapped", []) + m.get("episode_e2", []) + m.get("post_purchase", []) if r["id"] == a.template]
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
    rep["overlay"] = overlay_info
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
                         and all(x["ok"] for x in align) and "refused" not in overlay_info)

    survey_letter = a.template in SURVEY_LETTERS
    if survey_letter:
        own_url, own_ok = "", False   # no cabinet link in this letter; the customer's survey link is never handed to the test copy
    else:
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
            + ("Anketas poga šajā testā ir atslēgta (#): tā ir klienta pasūtījuma anketa. Klienta saite pārbaudīta "
               "dzīvā — atveras ar pasūtījuma numuru." if survey_letter
               else "Kabineta saites šajā testā ved uz TAVU kabinetu, ne klienta." if own_ok
               else "Kabineta saites šajā testā ir atslēgtas (#).")
            + ("" if survey_letter else
               "<br>Stils kā mūsu kampaņā " + _html.escape(STYLE_REF.get(a.template, REF_126)) + ".")
            + (("<br>" + _html.escape(note)) if note else ""))
    final = body if a.clean else re.sub(r"(<body[^>]*>)", lambda mm: mm.group(1) + banner(btxt), body, count=1)
    rep["sent"] = None
    if a.send:
        if not rep["all_ok"]:
            rep["send_refused"] = "not all checks green - nothing sent"
        else:
            subj = cc["subject"] if a.clean else f"[TESTS {a.seq} · {a.variant} · {a.template}] {cc['subject']}"
            rep["sent"] = send(subj, final, f"tpl-{a.template}")
    print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    return 0 if rep["all_ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
