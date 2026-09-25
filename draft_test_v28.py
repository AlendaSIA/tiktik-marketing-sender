"""Contract v2.8 draft check (MAIN 2026-09-25 17:05): a PREPARED template rendered as the rule-11 TEST_CONTACT.

MAIN, verbatim: "The fields do not exist in Brevo yet; render the draft check with a hardcoded TEST_CONTACT that
carries them (rule 11)." Contract v2.7 rule 11: the draft check renders as TEST_CONTACT = alenda.jurmala@gmail.com,
and "the negative branch is NEVER tested by deleting a field: an empty field and a boolean false are different
things in Brevo". So the no_ref case WRITES every v2.8 field - empty, false or 0 - and deletes none.

    python draft_test_v28.py --template 232 --commit <40-hex sha> --week 2026-w39 --case with_ref|no_ref

1. The template is the FILE the manifest names (its "templates" rows, 229-236, or its "live_mapped" rows, 179 and
   180), fetched at --commit and refused unless its git blob sha AND its sha256 equal the manifest's
   (template_put.fetch / blob_sha). Never the live Brevo template: v2.8 has not reached Brevo (MAIN: "prepare, do
   not update live Brevo templates").
2. The contact is TEST_CONTACT, read live from Brevo (GET only), with the v2.8 fields of --case laid over it.
3. draft_test's own checks run unchanged (static_checks, contact_checks: unresolved tags, rule-2 price format -
   now including P<n>_REF_PRICE -, every link 200, every image), plus the v2.8 rules this node enforces:
     P2 display  a slot shows its crossed-out reference, and the TAVA CENA label, iff P<n>_REF_PRICE is non-empty;
     P4 display  the "valid until" line (and the preheader that carries it) shows iff OFFER_VALID_UNTIL is non-empty;
                 a letter without such a line never shows one;
     P6          the fresh-batch line sits inside {% if contact.P1_FRESH %} and shows iff P1_FRESH is true (232-234;
                 the other letters have no fresh line);
   no U+27E6 placeholder is left, and without a personal price the body claims none.
Nothing is sent and nothing is written: there is no send path in this file.
"""
import argparse
import hashlib
import html as _html
import json
import re
import sys

import campaign as C
import draft_test as D
import presend as P
import template_put as T

TEST_CONTACT = "alenda.jurmala@gmail.com"  # contract v2.7 rule 11
SLOTS = range(1, 9)
NB, EUR = chr(0xA0), chr(0x20AC)


def price(s):
    """Rule-2 form: "0,89" -> "0,89" + U+00A0 + EUR sign."""
    return s + NB + EUR


V28_FIELDS = [f"P{i}_REF_PRICE" for i in SLOTS] + ["P1_FRESH", "OFFER_VALID_UNTIL", "OFFER_RUNG"]
CASES = {
    # TEST DATA. Models the writer's P2 write for P1 in one run: P1_PRICE = the personal price, P1_REF_PRICE =
    # today's shop price (the TEST_CONTACT's live P1_PRICE read 2026-09-25: 0,99 EUR). 0,89 < 0.95 x 0,99.
    "with_ref": dict({f"P{i}_REF_PRICE": "" for i in SLOTS}, P1_PRICE=price("0,89"), P1_REF_PRICE=price("0,99"),
                     P1_FRESH=True, OFFER_VALID_UNTIL="09.10.2026", OFFER_RUNG=1),
    # No slot shows a personal price (P2), so OFFER_VALID_UNTIL = "" and OFFER_RUNG = 0 (P4). P1_PRICE stays live.
    "no_ref": dict({f"P{i}_REF_PRICE": "" for i in SLOTS}, P1_FRESH=False, OFFER_VALID_UNTIL="", OFFER_RUNG=0),
}

SPEKA_LIDZ = "sp" + chr(0x113) + "k" + chr(0x101) + " l" + chr(0x12B) + "dz"   # the "valid until" words
_PRE = re.compile(r'(?s)<div[^>]*display:none[^>]*>(.*?)</div>')
_H1 = re.compile(r'(?s)<h1[^>]*>(.*?)</h1>')
_STRUCK = re.compile(r'text-decoration:line-through[^"]*">([^<]*)</span> <span[^>]*>([^<]*)</span>')
_LABEL = '>TAVA CENA</div>'
_CLAIM = re.compile(r"(?i)\b(sava|tava|tavu) cen")


def _text(fragment):
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def visible_lines(html_text):
    """The letter's visible text, one line per block. Title, style and the hidden preheader are left out."""
    h = re.sub(r"(?s)<(style|script|title)[^>]*>.*?</\1>", " ", html_text or "")
    h = _PRE.sub(" ", h, count=1)
    h = re.sub(r"<img[^>]*>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</td>|</h1>|</tr>", "\n", h)
    return [t for t in (_text(line) for line in h.split("\n")) if t]


def preheader(html_text):
    m = _PRE.search(html_text or "")
    return _text(m.group(1)) if m else None


def display_checks(tpl_html, rendered, subject, attrs):
    """The v2.8 rules this node enforces, on one rendered letter. Pure: no network, unit-tested."""
    shown = [n for n in SLOTS if attrs.get(f"P{n}_NAME")] if attrs.get("KABINETS_HAS_PRODUCTS") else []
    want = sorted((str(attrs[f"P{n}_REF_PRICE"]), str(attrs.get(f"P{n}_PRICE")))
                  for n in shown if attrs.get(f"P{n}_REF_PRICE"))
    got = sorted(_STRUCK.findall(rendered))
    labels = rendered.count(_LABEL)
    p2 = {"slots_shown": shown, "struck_expected": want, "struck_rendered": got, "labels": labels,
          "ok": got == want and labels == len(want)}

    until = str(attrs.get("OFFER_VALID_UNTIL") or "")
    lines = visible_lines(rendered)
    body = "\n".join(lines)
    pre = preheader(rendered) or ""
    line_shown = SPEKA_LIDZ in body
    # Only 232-234 carry a valid-until line (and a preheader that depends on it). A letter without one must
    # never show one; its preheader is its own and is not judged here.
    has_until = "contact.OFFER_VALID_UNTIL" in (tpl_html or "")
    if has_until:
        p4_ok = ((SPEKA_LIDZ + " " + until) in body and bool(pre)) if until else (not line_shown and pre == "")
    else:
        p4_ok = not line_shown
    p4 = {"template_has_line": has_until, "offer_valid_until": until, "valid_until_line": line_shown,
          "preheader": pre, "ok": p4_ok}

    has_fresh = bool(P.fresh_line_blockers(tpl_html, None))  # the letter has a fresh-batch line at all
    gate = P.fresh_line_blockers(tpl_html, "P1_FRESH")
    fresh_shown = "partij" in body.lower()
    fresh_want = (has_fresh and bool(attrs.get("KABINETS_HAS_PRODUCTS")) and bool(attrs.get("P1_NAME"))
                  and attrs.get("P1_FRESH") is True)
    p6 = {"template_has_line": has_fresh, "ungated_fresh_lines": len(gate), "fresh_line_shown": fresh_shown,
          "fresh_line_expected": fresh_want, "ok": not gate and fresh_shown == fresh_want}

    h1 = _text((_H1.search(rendered) or [None, ""])[1])
    claims = [ln for ln in lines if _CLAIM.search(ln) and ln != h1]
    left = sorted(set(re.findall(chr(0x27E6) + "[^" + chr(0x27E7) + "]*" + chr(0x27E7),
                                 (tpl_html or "") + (subject or "") + rendered)))
    head_claims = bool(_CLAIM.search(subject or "") or _CLAIM.search(h1))
    return {"p2": p2, "p4": p4, "p6": p6, "placeholders_left": left,
            "price_claims_in_body": claims, "claims_ok": bool(until) or not claims,
            "subject": subject, "h1": h1, "head_claims_a_personal_price": head_claims,
            "ok": p2["ok"] and p4["ok"] and p6["ok"] and not left and (bool(until) or not claims)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", type=int, required=True)
    ap.add_argument("--commit", required=True, help="the branch commit the manifest was written for")
    ap.add_argument("--week", required=True, help="utm.py form, e.g. 2026-w39")
    ap.add_argument("--case", choices=sorted(CASES), required=True)
    ap.add_argument("--manifest", default="templates_manifest.json")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", a.commit):
        sys.exit("commit must be a full 40-hex sha")
    if not re.fullmatch(r"\d{4}-w\d{2}", a.week):
        sys.exit("week must be YYYY-Www")
    m = json.load(open(a.manifest, encoding="utf-8"))
    rows = [r for r in m["templates"] + m.get("live_mapped", []) if r["id"] == a.template]
    if len(rows) != 1:
        sys.exit("template %d is not in the manifest" % a.template)
    row = rows[0]
    raw = T.fetch(a.commit, row["file"])
    report = {"template": a.template, "variant": row["variant"], "file": row["file"], "commit": a.commit,
              "case": a.case, "test_contact": TEST_CONTACT,
              "file_ok": T.blob_sha(raw) == row["git_blob"] and hashlib.sha256(raw).hexdigest() == row["sha256"]}
    if not report["file_ok"]:
        report["refused"] = "the file at this commit does not match the manifest"
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 2
    tpl, subject = raw.decode("utf-8"), row["subject"]
    live = C.contact_attributes(TEST_CONTACT)
    attrs = dict(live)
    attrs.update(CASES[a.case])
    report["overlay"] = CASES[a.case]
    report["live_P1_PRICE"] = live.get("P1_PRICE")
    report["v28_fields_already_in_brevo"] = sorted(f for f in V28_FIELDS if f in live)

    st = D.static_checks(tpl, subject)
    static_ok = st["complete_html"] and not (st["outside_contract"] or st["nested_double_quote_hrefs"]
                                             or st["template_side_utm"] or st["percent_in_text"])
    sent_html, seam = D.send_path_html(tpl, a.week)
    cc = D.contact_checks(sent_html, subject, TEST_CONTACT, a.week, attrs=attrs) if sent_html is not None else None
    rendered = cc["html"] if cc else ""
    v28 = display_checks(tpl, rendered, subject, attrs)
    report.update(static=st, static_ok=static_ok, utm_seam=seam,
                  contact={k: v for k, v in (cc or {}).items() if k != "html"}, v28=v28,
                  excerpt=[preheader(rendered) or ""] + visible_lines(rendered)[:24])
    report["gate_note"] = ("subject/h1 promise a personal price and cannot switch in the template: send this letter "
                           "only when OFFER_VALID_UNTIL is non-empty") if v28["head_claims_a_personal_price"] else ""
    report["all_ok"] = bool(static_ok and cc and cc["ok"] and v28["ok"])
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    return 0 if report["all_ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
