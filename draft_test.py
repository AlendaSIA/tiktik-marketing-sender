"""Draft test: render ONE Brevo template as N real contacts and (optionally) mail the result to Raivis.

Order: MAIN 2026-09-24 to Vestulu sabloni (blk-tiktik-marketing-sender, PD 175), step 2.
Contract: ATTRIBUTE CONTRACT v2.7, sha256[:12] 2154106c033c.

WHAT IT DOES, per contact:
  1. reads the contact's LIVE Brevo attributes (campaign.contact_attributes);
  2. renders the template as that contact (campaign.render_for_contact + a filter-aware
     substitute for `| default` / `| lower`, which campaign.substitute does not resolve);
  3. runs the checks below and prints ONE JSON report;
  4. only with --send AND every check green AND the seam open: sends the rendered letter to
     raivis@alenda.lv through the transactional API. Nobody else, ever - see TEST_RECIPIENT.

WHY THE LINK CHECK KEYS ON A BODY MARKER AND NEVER ON THE STATUS CODE. Measured 2026-09-24 with
campaign.py's own UA, no redirect following:
  plani kabinets.php, valid token     200, 4 529 B, "Laipni lūdzam kabinetā!"   (access choice page)
  plani kabinets.php, invalid token   200, 4 275 B, "Atsūtīt man saiti"         (login page)
  shop category that does not exist   200, 89 279 B, title "Tiktik - Veikals", 0 product links
  shop item that does not exist       404
Two of the three dead-link shapes answer 200. A check that trusts 200 passes them.
An UNKNOWN body is a FAIL (fail-closed): the first false negative is visible and costs one line
in MARKERS; a false positive would reach a customer.
Re-measured 2026-09-25: a valid KABINETS_URL now answers a small LOADER page that boots the cabinet
through its own link (+ &boot=1). The facts, and the one follow they allow, are at MARKERS.

V2 (MAIN 2026-09-24, command "all letters A to Z"): 1 to 3 contacts; --variant and --seq put
"[TESTS <id> · <variant> · N/M]" in the subject so Raivis can take the letters in order; --send-limit K sends
only the first K rendered letters (the order says ONE letter per template); the report carries the live
template's sha256 (proof that Brevo holds the reviewed file byte for byte) and every visible price-slot
placeholder (⟦…⟧). A placeholder is REPORTED, not failed: the price slot is known to be outside contract v2.7.

THE SEAM (rule 11 + MAIN's order): the send refuses unless, on EVERY contact, KABINETS_HAS_PRODUCTS
is a real boolean and KABINETS_URL carries utm_campaign=<week>-. Both come from Nakts
sinhronizacija D1+D2. The refusal is here so the seam holds even if someone runs --send early.

V3 (MAIN 2026-09-25). F1: the cabinet LOADER is followed through its own boot link, once, and passes
only as a booted cabinet (link_verdict). F6: a template that carries campaign.UTM_WEEK_MARKER is
checked AS THE SEND PATH SENDS IT - campaign.apply_utm_week(template, week) first, the call
campaign.create_draft makes, then rendering and every check (send_path_html). A template without the
marker is rendered as stored: the engine templates carry none, their UTM rides on the attribute values
(rule 10). static_checks accepts the seam form __UTM_WEEK__-<base> and still flags a written-in week.
"""
import argparse
import hashlib
import html as _html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import campaign as C

# THE ONLY RECIPIENT. Not a parameter. Widening it is a code change with a commit message.
TEST_RECIPIENT = "raivis@alenda.lv"
UA = "tiktik-campaign-linkcheck/1.0"
# Visible placeholders for slots the contract does not carry yet (e.g. the winback/lost price slot).
PLACEHOLDER = re.compile(r"⟦[^⟧]*⟧")

# Contract v2.7, THE FIELDS (47) + KABINETS_URL (rule 10). Nothing else may be rendered.
CONTRACT_FIELDS = frozenset(
    [f"P{i}_{s}" for i in range(1, 9) for s in ("NAME", "IMG", "PRICE")]
    + [f"R{i}_{s}" for i in range(1, 5) for s in ("NAME", "IMG", "PRICE")]
    + [f"D{i}_{s}" for i in range(1, 5) for s in ("NAME", "URL")]
    + ["KABINETS_HAS_PRODUCTS", "VARDS", "UZRUNA", "KABINETS_URL"])

# Rule 2: comma decimals, two decimals, U+00A0 thousands, U+00A0 before the euro sign.
# Rule 6: an R row with spread reads "no <price>".
_PRICE_OK = re.compile(r"^(no )?\d{1,3}( \d{3})*,\d{2} €$")

# THE CABINET, measured live 2026-09-25 on a real customer's KABINETS_URL (execution
# tiktik-draft-test-pbh8f; the customer stays anonymous here), this file's UA, no redirect following:
#   letter URL      kabinets.php?k=<32 hex>&tab=preces&utm_source=brevo&utm_medium=email
#                   &utm_campaign=2026-w39-kabinets
#                   -> 200, 1 771-1 782 B, <title>tiktik.lv dokumentu kabinets</title>, h1 "Uzgaidi,
#                   ielādējam tavu kabinetu", text "Sameklējam tavus pasūtījumus un preču dokumentus — tas
#                   aizņem pāris sekundes." A LOADER. Its one link, "Turpināt", is the letter URL + &boot=1
#                   (&amp;-escaped); its script fetch()es the same URL and document.write()s the answer.
#                   4 refetches over 30 s: the same loader every time.
#   letter + &boot=1 -> 200, 23 839 B, <title>Mans kabinets</title>, 0.4 s: the booted cabinet. It holds
#                   neither "Laipni lūdzam kabinetā" nor a login marker.
#   invalid token   -> 200, 4 275 B, the login page ("Atsūtīt man saiti"): DEAD, as on 2026-09-24.
# So the loader is a KNOWN body, but it passes only through its own boot link, followed ONCE and checked
# against the letter URL (link_verdict). A "Mans kabinets" page reached DIRECTLY was not measured and
# stays UNKNOWN_BODY. The 2026-09-24 access-choice page ("Laipni lūdzam kabinetā") still passes as is.
KABINETS_LOADER = "Uzgaidi, ielādējam tavu kabinetu"
KABINETS_BOOTED = "<title>Mans kabinets</title>"
_KABINETS = re.compile(r"^https://plani\.tiktik\.lv/kabinets\.php\?")

# Body markers, keyed by URL shape. (must_have_any, must_not_have_any, min_item_links)
MARKERS = [
    (_KABINETS,
     ("Laipni lūdzam kabinetā",              # access-choice page, 2026-09-24
      KABINETS_LOADER),                      # loader, 2026-09-25 - passes only via its boot link
     ("Atsūtīt man saiti", "Ieej savā kabinetā"), 0),
    (re.compile(r"^https://www\.tiktik\.lv/veikals/item/"),
     ('itemprop="price"',), ("<title>Tiktik - Veikals</title>",), 0),
    # Also the shop's featured page (rule 14 fallback; akcija_weekly's no-cabinet box, F6). Measured
    # 2026-09-25 with the week's utm query: 200, 180 565 B, 61 distinct /veikals/item/ links - passes as is.
    (re.compile(r"^https://www\.tiktik\.lv/veikals/(category|params/category)/"),
     ("/veikals/item/",), ("<title>Tiktik - Veikals</title>",), 1),
]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"user-agent": UA})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, r.headers.get("content-type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("content-type", "") if e.headers else "", b""
    except Exception as e:  # noqa: BLE001
        return None, repr(e), b""


def link_verdict(url):
    status, ctype, body = fetch(url)
    if status != 200:
        return {"url": url, "ok": False, "why": f"status {status} {ctype}"[:160]}
    text = body.decode("utf-8", "replace")
    for rx, must, must_not, min_items in MARKERS:
        if rx.match(url):
            bad = [m for m in must_not if m in text]
            if bad:
                return {"url": url, "ok": False, "why": f"dead-page marker {bad[0]!r} (status was 200)"}
            if not any(m in text for m in must):
                return {"url": url, "ok": False, "why": f"UNKNOWN_BODY: none of {list(must)}"}
            if rx is _KABINETS and KABINETS_LOADER in text:
                # Checked before the access-choice marker on purpose: a body carrying both takes the
                # stricter road.
                return _loader_verdict(url, text, len(body), must_not)
            items = len(set(re.findall(r'href="[^"]*/veikals/item/[^"]*"', text)))
            if items < min_items:
                return {"url": url, "ok": False, "why": f"{items} product links < {min_items}"}
            return {"url": url, "ok": True, "bytes": len(body)}
    return {"url": url, "ok": False, "why": "UNKNOWN_URL_SHAPE: no body marker defined, fail-closed"}


_A_HREF = re.compile(r"""<a\b[^>]*?\shref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.I | re.S)


def _boot_link(url, loader_text):
    """(boot_url, None) or (None, why). The boot link is taken from the loader's own <a href> (never
    from its script), html-unescaped and resolved against the letter URL. It must keep the letter
    URL's scheme, host and path, and its query must be exactly the letter's query + &boot=1."""
    found = set()
    for m in _A_HREF.finditer(loader_text):
        href = _html.unescape(next(g for g in m.groups() if g is not None)).strip()
        query = urllib.parse.urlsplit(href).query
        if any(k == "boot" for k, _ in urllib.parse.parse_qsl(query, keep_blank_values=True)):
            found.add(urllib.parse.urljoin(url, href))
    if not found:
        return None, "LOADER_NO_BOOT_LINK: the loader page has no <a href> carrying boot="
    if len(found) > 1:
        return None, f"LOADER_AMBIGUOUS: {len(found)} different boot links in the loader page"
    boot = found.pop()
    L, B = urllib.parse.urlsplit(url), urllib.parse.urlsplit(boot)
    if (B.scheme, B.netloc, B.path) != (L.scheme, L.netloc, L.path):
        return None, (f"LOADER_BOOT_LINK_ELSEWHERE: the boot link goes to {B.scheme}://{B.netloc}{B.path}, "
                      f"not to the letter's {L.scheme}://{L.netloc}{L.path}")
    want = f"{L.query}&boot=1" if L.query else "boot=1"
    if B.query == want and not B.fragment:
        return boot, None
    lq = urllib.parse.parse_qsl(L.query, keep_blank_values=True)
    bq = urllib.parse.parse_qsl(B.query, keep_blank_values=True)
    if B.fragment:
        why = "it carries a #fragment"
    elif bq[-1:] != [("boot", "1")]:
        why = "its query does not end in boot=1"
    elif bq[:-1] != lq:
        why = ("it changes the letter's query (" +
               (", ".join(sorted({k for k, _ in set(lq) ^ set(bq[:-1])})) or "parameter order") + ")")
    else:
        why = "its query is encoded differently from the letter's (byte-exact match required)"
    return None, f"LOADER_BOOT_LINK_MISMATCH: {why}; it must be the letter URL + &boot=1"


def _loader_verdict(url, loader_text, loader_bytes, must_not):
    """The loader's boot link, followed ONCE with fetch() (no redirects). Passes only as a booted cabinet:
    200, no dead-page marker, <title>Mans kabinets</title>. Everything else fails with its own why."""
    boot, why = _boot_link(url, loader_text)
    if why:
        return {"url": url, "ok": False, "why": why}
    status, ctype, body = fetch(boot)
    if status != 200:
        return {"url": url, "ok": False, "why": f"LOADER>BOOT: status {status} {ctype}"[:160]}
    text = body.decode("utf-8", "replace")
    bad = [m for m in must_not if m in text]
    if bad:
        return {"url": url, "ok": False, "why": f"LOADER>BOOT: dead-page marker {bad[0]!r} (status was 200)"}
    if KABINETS_BOOTED not in text:
        return {"url": url, "ok": False, "why": f"LOADER>BOOT: UNKNOWN_BODY: no {KABINETS_BOOTED}"}
    return {"url": url, "ok": True, "via": "loader>boot", "bytes": len(body), "loader_bytes": loader_bytes}


_FILTERED = re.compile(r"\{\{\s*contact\.([A-Za-z0-9_]+)((?:\s*\|\s*[a-z_]+(?:\s*:\s*(?:\"[^\"]*\"|'[^']*'))?)*)\s*\}\}")
_FILTER = re.compile(r"\|\s*([a-z_]+)(?:\s*:\s*(?:\"([^\"]*)\"|'([^']*)'))?")


def render(text, attrs):
    """campaign.render_for_contact for blocks, then every {{ contact.X | filters }} resolved.
    An unknown filter leaves the tag untouched, so it stays visible and fails the check."""
    text = C.render_for_contact(text or "", attrs)

    def one(m):
        val = attrs.get(m.group(1))
        for f in _FILTER.finditer(m.group(2) or ""):
            name, arg = f.group(1), f.group(2) if f.group(2) is not None else f.group(3)
            if name == "default":
                val = val if val not in (None, "") else arg
            elif name == "lower":
                val = None if val is None else str(val).lower()
            else:
                return m.group(0)
        return m.group(0) if val is None else str(val)
    return _FILTERED.sub(one, text)


# The seam value as a template writes it: the marker, "-", and a utm.py-style base (lowercase, digits,
# single hyphens - the customer-safe slug form, e.g. papildinam, tava-cena, akcija).
_SEAM_VALUE = re.compile(re.escape(C.UTM_WEEK_MARKER) + r"-[a-z0-9]+(?:-[a-z0-9]+)*")


def static_checks(tpl_html, subject):
    """Checks on the TEMPLATE itself, independent of any contact."""
    out = {}
    h = tpl_html or ""
    out["complete_html"] = all(s in h.lower() for s in ("<!doctype html", "<html", "<head", "<body", "</body>", "</html>"))
    rendered_attrs = C.attributes_in(h + " " + (subject or ""))
    out["attributes"] = rendered_attrs
    out["outside_contract"] = [a for a in rendered_attrs if a not in CONTRACT_FIELDS]
    # DEFECT 2026-09-17: a double quote inside a Liquid tag inside a double-quoted href.
    out["nested_double_quote_hrefs"] = re.findall(r'href="\{\{[^}]*"[^}]*\}\}', h)
    # UTM must not be written into the template (rule 10) except via the contact's own URL value - or in
    # the UTM SEAM v1 form __UTM_WEEK__-<base>, which carries no week until campaign.apply_utm_week puts
    # it in (F6, 2026-09-25). A written-in week is still flagged, and so is a marker without a base.
    out["template_side_utm"] = sorted(set(v for v in re.findall(r"utm_campaign=([^&\"'\s]+)", h)
                                          if not _SEAM_VALUE.fullmatch(v)))
    visible = re.sub(r"(?s)\{%.*?%\}|\{\{.*?\}\}", " ", h)  # Liquid tags are not text
    visible = re.sub(r"(?s)<(style|script)[^>]*>.*?</\1>|<[^>]+>", " ", visible)
    out["percent_in_text"] = re.findall(r"[^\s]{0,20}%[^\s]{0,20}", _html.unescape(visible))
    return out


_COMPANY = re.compile(r"(^|[\s\"'«])(SIA|IK|AS|Z/S|ZS)([\s\"'»]|$)", re.I)


def uzruna_valid(uz, attrs):
    """Rule 7: VALID = a single token, no company designation, not equal to the full name."""
    full = str(attrs.get("FIRSTNAME") or "").strip()
    return (len(uz.split()) == 1 and not _COMPANY.search(uz)
            and uz.casefold() != full.casefold())


def links_to(tpl_html, attrs, field):
    """Does the letter AS THIS CONTACT SEES IT link to contact.<field>? Blocks are resolved with the
    campaign renderer first, so a link inside a branch this contact does not see does not count."""
    blocks = C.render_for_contact(tpl_html or "", attrs)
    return any(re.search(r"\{\{\s*contact\." + field + r"\b", h) for h in C.hrefs_in(blocks))


def contact_checks(tpl_html, subject, email, week):
    attrs = C.contact_attributes(email)
    body = render(tpl_html, attrs)
    subj = render(subject, attrs)
    r = {"contact": email, "problems": []}
    p = r["problems"]
    leftover = [x for x in re.findall(r"\{\{.*?\}\}|\{%.*?%\}", body + subj, re.S)
                if not C.is_brevo_system_link(x)]
    if leftover:
        p.append(f"unresolved: {leftover[:5]}")
    hp = attrs.get("KABINETS_HAS_PRODUCTS")
    r["has_products"] = hp
    if not isinstance(hp, bool):
        p.append(f"SEAM: KABINETS_HAS_PRODUCTS is {type(hp).__name__}, not a Brevo boolean (rule 1)")
    # KABINETS_URL is demanded only where THIS contact's letter links to it (MAIN 2026-09-25, command 4,
    # item 4). Measured that day: a contact without cabinet products carries no KABINETS_URL at all
    # (3 of 3 read from Brevo), and akcija_weekly v2 gives that contact the featured-page box instead of
    # the cabinet box - demanding the attribute there made a correct letter red (execution smdp7).
    # Where the rendered letter DOES link to it, the rule stands exactly as before.
    r["kabinets_linked"] = links_to(tpl_html, attrs, "KABINETS_URL")
    ku = str(attrs.get("KABINETS_URL") or "")
    if r["kabinets_linked"] and f"utm_campaign={week}-" not in ku:
        p.append(f"SEAM: KABINETS_URL has no utm_campaign={week}- (rule 10 / D1)")
    uz = str(attrs.get("UZRUNA") or "").strip()
    if uz and not uzruna_valid(uz, attrs):
        p.append(f"UZRUNA {uz!r} is not VALID (rule 7) - the writer must blank it so the ladder falls back")
    for k, v in attrs.items():
        if re.fullmatch(r"[PR][1-8]_PRICE", k) and v not in (None, "") and not _PRICE_OK.match(str(v)):
            p.append(f"{k}={v!r} breaks rule 2 format")
    links = []
    for href in sorted(set(C.hrefs_in(body))):
        u = _html.unescape(href).strip()
        if C.is_brevo_system_link(u):
            continue
        if u.count("?") > 1:
            p.append(f"two '?' in {u}")
        if "utm_campaign=" not in u or f"utm_campaign={week}-" not in u:
            p.append(f"no utm_campaign={week}- on {u}")
        links.append(link_verdict(u))
    r["links"] = links
    p += [f"link: {l['why']} :: {l['url']}" for l in links if not l["ok"]]
    if not [l for l in links if l["ok"]]:
        p.append("no_real_links")
    imgs = []
    for src in sorted(set(re.findall(r'<img[^>]+src="([^"]+)"', body))):
        st, ct, _ = fetch(_html.unescape(src))
        slot = "img.php" in src and "f=jpg" in src
        imgs.append({"src": src[:120], "status": st, "type": ct, "img_php_jpg": slot})
        if st != 200 or not str(ct).startswith("image/"):
            p.append(f"image {st} {ct} :: {src[:100]}")
    r["images"] = imgs
    r["subject"] = subj
    r["html"] = body
    r["ok"] = not p
    return r


def subject_prefix(template_id, idx, n, variant=None, seq=None):
    """[TESTS 179 · 1/3] (v1 form) or, with a variant and a sequence, [TESTS 231 · winback_2 · 4/8].
    Not named test_*: pytest collects this file (it matches *_test.py) and took it for a test."""
    if variant and seq:
        return f"[TESTS {template_id} · {variant} · {seq}]"
    return f"[TESTS {template_id} · {idx}/{n}]"


def send_path_html(tpl_html, week):
    """(html, seam) - the HTML the send path turns into the letter, and what happened on the UTM seam.

    campaign.create_draft runs campaign.apply_utm_week on the template and builds the campaign from
    THAT html, so a template carrying campaign.UTM_WEEK_MARKER is rendered and checked after the same
    call. Without the marker the template is returned as stored and apply_utm_week is never called: the
    engine templates carry none, their UTM rides on the attribute values (rule 10). A seam refusal is
    returned, not raised (html None), so the report can say why.
    """
    if C.UTM_WEEK_MARKER not in (tpl_html or ""):
        return tpl_html, {"marker": False}
    try:
        after, pairs = C.apply_utm_week(tpl_html, week)
    except C.UtmSeamRefused as e:
        return None, {"marker": True, "refused": str(e)}
    return after, {"marker": True, "pairs": pairs}


def send_to_raivis(template_id, idx, n, res, variant=None, seq=None):
    tag = subject_prefix(template_id, idx, n, variant, seq)[1:-1]
    banner = ("<div style=\"background:#fff3cd;padding:10px 14px;font:13px Arial;color:#5c4400;\">"
              f"{_html.escape(tag)} · renderēts kā {_html.escape(res['contact'])}. "
              "Klientam NAV sūtīts. Kabineta saite ir klienta — neizvēlies tur paroli/bez paroles.</div>")
    body = re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + banner, res["html"], count=1)
    payload = {"sender": {"id": C.SENDER_ID}, "to": [{"email": TEST_RECIPIENT}],
               "subject": f"{subject_prefix(template_id, idx, n, variant, seq)} {res['subject']}",
               "htmlContent": body, "tags": ["draft-test", f"tpl-{template_id}"]}
    assert payload["to"] == [{"email": TEST_RECIPIENT}], "recipient guard"
    return C._call("POST", "/smtp/email", payload)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", type=int, required=True)
    ap.add_argument("--contacts", required=True, help="comma-separated, 1 to 3")
    ap.add_argument("--week", required=True, help="utm.py form, e.g. 2026-w39")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--variant", default=None, help="email_type, printed in the test subject")
    ap.add_argument("--seq", default=None, help="N/M, printed in the test subject")
    ap.add_argument("--send-limit", type=int, default=0, help="send only the first K letters (0 = all)")
    a = ap.parse_args(argv)
    contacts = [c.strip() for c in a.contacts.split(",") if c.strip()]
    if not 1 <= len(contacts) <= 3:
        sys.exit("1 to 3 contacts")
    if not re.fullmatch(r"\d{4}-w\d{2}", a.week):
        sys.exit("week must be YYYY-Www")
    if a.variant is not None and not re.fullmatch(r"[a-z0-9_]{3,40}", a.variant):
        sys.exit("variant must be an email_type")
    if a.seq is not None and not re.fullmatch(r"\d{1,2}/\d{1,2}", a.seq):
        sys.exit("seq must be N/M")
    if a.send_limit < 0:
        sys.exit("send-limit must be >= 0")
    t = C.template(a.template)
    tpl, subject = t.get("htmlContent") or "", t.get("subject") or ""
    st = static_checks(tpl, subject)
    static_bad = (not st["complete_html"]) or st["outside_contract"] or st["nested_double_quote_hrefs"] \
        or st["template_side_utm"] or st["percent_in_text"]
    sent_html, seam = send_path_html(tpl, a.week)
    if "refused" in seam:
        static_bad = True  # create_draft would refuse this template: nothing to render
    results = [] if sent_html is None else [contact_checks(sent_html, subject, e, a.week) for e in contacts]
    report = {"template": a.template, "template_name": t.get("name"), "week": a.week,
              "variant": a.variant, "seq": a.seq, "template_subject": subject,
              "template_sha256": hashlib.sha256(tpl.encode("utf-8")).hexdigest(),
              "template_active": t.get("isActive"),
              "placeholders": sorted(set(PLACEHOLDER.findall(tpl + " " + subject))),
              "utm_seam": seam,
              "static": st, "static_ok": not static_bad,
              "contacts": [{k: v for k, v in r.items() if k != "html"} for r in results],
              "all_ok": (not static_bad) and all(r["ok"] for r in results), "sent": []}
    if a.send:
        if not report["all_ok"]:
            report["send_refused"] = "not all checks green - nothing sent"
        else:
            chosen = results[:a.send_limit] if a.send_limit else results
            for i, r in enumerate(chosen, 1):
                report["sent"].append(send_to_raivis(a.template, i, len(chosen), r, a.variant, a.seq))
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    return 0 if report["all_ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
