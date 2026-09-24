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

THE SEAM (rule 11 + MAIN's order): the send refuses unless, on EVERY contact, KABINETS_HAS_PRODUCTS
is a real boolean and KABINETS_URL carries utm_campaign=<week>-. Both come from Nakts
sinhronizacija D1+D2. The refusal is here so the seam holds even if someone runs --send early.
"""
import argparse
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

# Contract v2.7, THE FIELDS (47) + KABINETS_URL (rule 10). Nothing else may be rendered.
CONTRACT_FIELDS = frozenset(
    [f"P{i}_{s}" for i in range(1, 9) for s in ("NAME", "IMG", "PRICE")]
    + [f"R{i}_{s}" for i in range(1, 5) for s in ("NAME", "IMG", "PRICE")]
    + [f"D{i}_{s}" for i in range(1, 5) for s in ("NAME", "URL")]
    + ["KABINETS_HAS_PRODUCTS", "VARDS", "UZRUNA", "KABINETS_URL"])

# Rule 2: comma decimals, two decimals, U+00A0 thousands, U+00A0 before the euro sign.
# Rule 6: an R row with spread reads "no <price>".
_PRICE_OK = re.compile(r"^(no )?\d{1,3}( \d{3})*,\d{2} €$")

# Body markers, keyed by URL shape. (must_have_any, must_not_have_any, min_item_links)
MARKERS = [
    (re.compile(r"^https://plani\.tiktik\.lv/kabinets\.php\?"),
     ("Laipni lūdzam kabinetā",),            # UNVERIFIED: the in-cabinet page (a customer who
                                             # already chose) has its own marker, not measured yet
     ("Atsūtīt man saiti", "Ieej savā kabinetā"), 0),
    (re.compile(r"^https://www\.tiktik\.lv/veikals/item/"),
     ('itemprop="price"',), ("<title>Tiktik - Veikals</title>",), 0),
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
            items = len(set(re.findall(r'href="[^"]*/veikals/item/[^"]*"', text)))
            if items < min_items:
                return {"url": url, "ok": False, "why": f"{items} product links < {min_items}"}
            return {"url": url, "ok": True, "bytes": len(body)}
    return {"url": url, "ok": False, "why": "UNKNOWN_URL_SHAPE: no body marker defined, fail-closed"}


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
    # UTM must not be written into the template (rule 10) except via the contact's own URL value.
    out["template_side_utm"] = sorted(set(re.findall(r"utm_campaign=([^&\"'\s]+)", h)))
    visible = re.sub(r"(?s)<(style|script)[^>]*>.*?</\1>|<[^>]+>", " ", h)
    out["percent_in_text"] = re.findall(r"[^\s]{0,20}%[^\s]{0,20}", _html.unescape(visible))
    return out


_COMPANY = re.compile(r"(^|[\s\"'«])(SIA|IK|AS|Z/S|ZS)([\s\"'»]|$)", re.I)


def uzruna_valid(uz, attrs):
    """Rule 7: VALID = a single token, no company designation, not equal to the full name."""
    full = str(attrs.get("FIRSTNAME") or "").strip()
    return (len(uz.split()) == 1 and not _COMPANY.search(uz)
            and uz.casefold() != full.casefold())


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
    ku = str(attrs.get("KABINETS_URL") or "")
    if f"utm_campaign={week}-" not in ku:
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


def send_to_raivis(template_id, idx, n, res):
    banner = ("<div style=\"background:#fff3cd;padding:10px 14px;font:13px Arial;color:#5c4400;\">"
              f"TESTS · šablons {template_id} · {idx}/{n} · renderēts kā {_html.escape(res['contact'])}. "
              "Klientam NAV sūtīts. Kabineta saite ir klienta — neizvēlies tur paroli/bez paroles.</div>")
    body = re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + banner, res["html"], count=1)
    payload = {"sender": {"id": C.SENDER_ID}, "to": [{"email": TEST_RECIPIENT}],
               "subject": f"[TESTS {template_id} · {idx}/{n}] {res['subject']}",
               "htmlContent": body, "tags": ["draft-test", f"tpl-{template_id}"]}
    assert payload["to"] == [{"email": TEST_RECIPIENT}], "recipient guard"
    return C._call("POST", "/smtp/email", payload)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", type=int, required=True)
    ap.add_argument("--contacts", required=True, help="comma-separated, exactly 3")
    ap.add_argument("--week", required=True, help="utm.py form, e.g. 2026-w39")
    ap.add_argument("--send", action="store_true")
    a = ap.parse_args(argv)
    contacts = [c.strip() for c in a.contacts.split(",") if c.strip()]
    if len(contacts) != 3:
        sys.exit("exactly 3 contacts")
    if not re.fullmatch(r"\d{4}-w\d{2}", a.week):
        sys.exit("week must be YYYY-Www")
    t = C.template(a.template)
    tpl, subject = t.get("htmlContent") or "", t.get("subject") or ""
    st = static_checks(tpl, subject)
    static_bad = (not st["complete_html"]) or st["outside_contract"] or st["nested_double_quote_hrefs"] \
        or st["template_side_utm"] or st["percent_in_text"]
    results = [contact_checks(tpl, subject, e, a.week) for e in contacts]
    report = {"template": a.template, "template_name": t.get("name"), "week": a.week,
              "static": st, "static_ok": not static_bad,
              "contacts": [{k: v for k, v in r.items() if k != "html"} for r in results],
              "all_ok": (not static_bad) and all(r["ok"] for r in results), "sent": []}
    if a.send:
        if not report["all_ok"]:
            report["send_refused"] = "not all checks green - nothing sent"
        else:
            for i, r in enumerate(results, 1):
                report["sent"].append(send_to_raivis(a.template, i, len(results), r))
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    return 0 if report["all_ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
