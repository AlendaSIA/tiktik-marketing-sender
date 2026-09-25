"""Offline tests for draft_test.py - no network, no Brevo."""
import hashlib
import html as _html
import json
import os
import sys

try:
    import pytest
except ImportError:  # the image runs `python -m unittest discover -s tests` without pytest
    import unittest
    raise unittest.SkipTest("pytest-style module: run it with python -m pytest")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import draft_test as D  # noqa: E402


def test_recipient_is_hardcoded():
    assert D.TEST_RECIPIENT == "raivis@alenda.lv"


def test_render_default_single_and_double_quotes():
    a = {"X": ""}
    assert D.render('{{ contact.X | default : "Sveiki!" }}', a) == "Sveiki!"
    assert D.render("{{ contact.X | default : 'https://a/b' }}", a) == "https://a/b"
    assert D.render('{{ contact.Y | lower }}', {"Y": "ABC"}) == "abc"


def test_unknown_filter_stays_visible():
    out = D.render('{{ contact.Y | upcase }}', {"Y": "a"})
    assert out.startswith("{{")


def test_if_block_uses_campaign_renderer():
    html = "{% if contact.KABINETS_HAS_PRODUCTS %}A{% else %}B{% endif %}"
    assert D.render(html, {"KABINETS_HAS_PRODUCTS": True}) == "A"
    assert D.render(html, {"KABINETS_HAS_PRODUCTS": False}) == "B"


def test_static_flags_contract_and_defects():
    tpl = ('<!DOCTYPE html><html><head></head><body>{{ contact.HERO_PRODUCT_NAME }} 10% '
           '<a href="{{ contact.KABINETS_URL | default : "https://x" }}">a</a>'
           '<a href="https://www.tiktik.lv/veikals?utm_campaign=2026-09-x">b</a></body></html>')
    s = D.static_checks(tpl, "")
    assert s["complete_html"]
    assert s["outside_contract"] == ["HERO_PRODUCT_NAME"]
    assert s["nested_double_quote_hrefs"]
    assert s["template_side_utm"] == ["2026-09-x"]
    assert s["percent_in_text"]


def test_price_format_rule2():
    assert D._PRICE_OK.match("5,49 €")
    assert D._PRICE_OK.match("1 234,00 €")
    assert D._PRICE_OK.match("no 4,20 €")
    assert not D._PRICE_OK.match("19.99")
    assert not D._PRICE_OK.match("5,49 €")  # plain space: the price_text trap


def test_unknown_url_shape_fails_closed(monkeypatch):
    monkeypatch.setattr(D, "fetch", lambda u, timeout=25: (200, "text/html", b"hello"))
    v = D.link_verdict("https://example.com/")
    assert not v["ok"] and "UNKNOWN_URL_SHAPE" in v["why"]


def test_kabinets_login_page_is_dead_even_on_200(monkeypatch):
    monkeypatch.setattr(D, "fetch", lambda u, timeout=25: (200, "text/html", "Atsūtīt man saiti".encode()))
    v = D.link_verdict("https://plani.tiktik.lv/kabinets.php?k=0&tab=preces")
    assert not v["ok"] and "dead-page marker" in v["why"]


def test_empty_category_is_dead_even_on_200(monkeypatch):
    monkeypatch.setattr(D, "fetch", lambda u, timeout=25: (200, "text/html", b"<title>x</title>"))
    v = D.link_verdict("https://www.tiktik.lv/veikals/category/nav/")
    assert not v["ok"]


def test_neutral_greeting_ladder_renders_with_campaign_renderer():
    import template_greeting_edit as G
    assert D.render(G.NEW, {"UZRUNA": "Sandi", "VARDS": "Sandis"}).endswith("Sveiki, Sandi!</p>")
    assert D.render(G.NEW, {"VARDS": "Sandis"}).endswith("Sveiki, Sandis!</p>")
    assert D.render(G.NEW, {}).endswith("Sveiki!</p>")
    assert "{%" not in D.render(G.NEW, {})


def test_uzruna_validity_rule7():
    assert D.uzruna_valid("Sandi", {"FIRSTNAME": "Sandis Masulis"})
    assert not D.uzruna_valid('SIA "BR', {})
    assert not D.uzruna_valid("Sandis Masulis", {"FIRSTNAME": "Sandis Masulis"})
    assert not D.uzruna_valid("Sandis", {"FIRSTNAME": "Sandis"})


def test_liquid_tags_are_not_percent_text():
    s = D.static_checks("<!DOCTYPE html><html><head></head><body>{% if contact.X %}a{% endif %}</body></html>", "")
    assert s["percent_in_text"] == []


def test_v2_prefix_forms():
    assert D.subject_prefix(179, 1, 3) == "[TESTS 179 · 1/3]"
    assert D.subject_prefix(231, 1, 1, "winback_2", "4/8") == "[TESTS 231 · winback_2 · 4/8]"


def test_module_has_nothing_pytest_would_collect():
    # draft_test.py matches *_test.py, so pytest collects it: a test_* function there is an ERROR.
    assert not [n for n in dir(D) if n.startswith("test")]


def test_v2_placeholders_are_found_not_failed():
    tpl = ('<!DOCTYPE html><html><head></head><body><p>⟦TAVA CENA P1⟧ līdz ⟦LĪDZ DATUMAM⟧</p>'
           '<a href="{{ contact.KABINETS_URL }}">x</a></body></html>')
    assert sorted(set(D.PLACEHOLDER.findall(tpl))) == ["⟦LĪDZ DATUMAM⟧", "⟦TAVA CENA P1⟧"]
    s = D.static_checks(tpl, "Tēma")
    assert not s["outside_contract"] and not s["percent_in_text"] and not s["template_side_utm"]


def test_v2_send_limit_and_recipient(monkeypatch):
    calls = []
    monkeypatch.setattr(D.C, "template", lambda i: {"name": "t", "subject": "S",
                        "htmlContent": "<!DOCTYPE html><html><head></head><body>x</body></html>", "isActive": False})
    monkeypatch.setattr(D, "contact_checks", lambda t, s, e, w: {"contact": e, "problems": [], "ok": True,
                                                                "html": "<body>x</body>", "subject": "S"})
    monkeypatch.setattr(D.C, "_call", lambda m, p, payload: calls.append(payload) or {"messageId": "m"})
    rc = D.main(["--template", "9", "--contacts", "a@x.lv,b@x.lv", "--week", "2026-w39", "--send",
                 "--send-limit", "1", "--variant", "reorder_2", "--seq", "2/8"])
    assert rc == 0 and len(calls) == 1
    assert calls[0]["to"] == [{"email": "raivis@alenda.lv"}]
    assert calls[0]["subject"] == "[TESTS 9 · reorder_2 · 2/8] S"


def test_v2_contact_count_bounds():
    import pytest
    with pytest.raises(SystemExit):
        D.main(["--template", "9", "--contacts", "a,b,c,d", "--week", "2026-w39"])


# --- F1 (MAIN 2026-09-25): the cabinet LOADER, measured live that day. Token anonymised. ------------------
TOKEN = "0123456789abcdef0123456789abcdef"
LETTER = ("https://plani.tiktik.lv/kabinets.php?k=" + TOKEN + "&tab=preces&utm_source=brevo"
          "&utm_medium=email&utm_campaign=2026-w39-kabinets")
BOOT = LETTER + "&boot=1"
REL_BOOT = "kabinets.php?" + BOOT.split("?", 1)[1]          # the loader's link is relative
BOOTED = ('<!DOCTYPE html><html lang="lv"><head><title>Mans kabinets</title></head><body>'
          + "<p>pasūtījumi</p>" * 50 + "</body></html>").encode("utf-8")
LOGIN = "<h1>Ieej savā kabinetā</h1><button>Atsūtīt man saiti</button>".encode("utf-8")
ACCESS = "<h1>Laipni lūdzam kabinetā!</h1><a href=\"?x\">Ar paroli</a>".encode("utf-8")


def _loader(href=REL_BOOT, link=True, extra=""):
    """The loader's shape as measured: title, h1, one line of text, the "Turpināt" link (&amp;-escaped),
    and the script that fetches the boot URL. The script keeps the good URL even when the link is bad."""
    a = f'<p><a class="btn" href="{_html.escape(href)}">Turpināt</a></p>' if link else ""
    return ('<!DOCTYPE html><html lang="lv"><head><meta charset="utf-8">'
            "<title>tiktik.lv dokumentu kabinets</title></head><body>"
            "<h1>Uzgaidi, ielādējam tavu kabinetu</h1>"
            "<p>Sameklējam tavus pasūtījumus un preču dokumentus — tas aizņem pāris sekundes.</p>" + a + extra +
            '<script>var u="' + REL_BOOT + '";fetch(u,{credentials:"same-origin"}).then(function(r){'
            "return r.text()}).then(function(h){document.open();document.write(h);document.close()});"
            "</script></body></html>").encode("utf-8")


def _site(monkeypatch, pages):
    """D.fetch answers from `pages`; the letter's logo is an image; anything else is a 404."""
    calls = []

    def fake(u, timeout=25):
        if "/logobox/" in u:
            return 200, "image/png", b"png"
        calls.append(u)
        return pages.get(u, (404, "text/html", b""))
    monkeypatch.setattr(D, "fetch", fake)
    return calls


def test_loader_is_followed_once_to_the_booted_cabinet(monkeypatch):
    calls = _site(monkeypatch, {LETTER: (200, "text/html", _loader()), BOOT: (200, "text/html", BOOTED)})
    v = D.link_verdict(LETTER)
    assert v["ok"] and v["via"] == "loader>boot" and v["bytes"] == len(BOOTED)
    assert calls == [LETTER, BOOT]


@pytest.mark.parametrize("quote", ["'", ""])
def test_loader_link_in_single_quotes_or_unquoted_is_still_the_boot_link(monkeypatch, quote):
    esc = _html.escape(REL_BOOT).encode("utf-8")
    body = _loader().replace(b'href="' + esc + b'"', b"href=" + quote.encode() + esc + quote.encode())
    assert body != _loader()
    calls = _site(monkeypatch, {LETTER: (200, "text/html", body), BOOT: (200, "text/html", BOOTED)})
    v = D.link_verdict(LETTER)
    assert v["ok"] and v["via"] == "loader>boot" and calls == [LETTER, BOOT]


def test_loader_then_login_page_is_dead(monkeypatch):
    _site(monkeypatch, {LETTER: (200, "text/html", _loader()), BOOT: (200, "text/html", LOGIN)})
    v = D.link_verdict(LETTER)
    assert not v["ok"] and v["why"].startswith("LOADER>BOOT: dead-page marker 'Atsūtīt man saiti'")


def test_loader_then_unknown_body_fails(monkeypatch):
    calls = _site(monkeypatch, {LETTER: (200, "text/html", _loader()),
                                BOOT: (200, "text/html", b"<title>Something else</title>")})
    v = D.link_verdict(LETTER)
    assert not v["ok"] and "UNKNOWN_BODY" in v["why"] and "Mans kabinets" in v["why"]
    assert calls == [LETTER, BOOT]


def test_loader_answering_the_loader_again_is_not_followed_twice(monkeypatch):
    calls = _site(monkeypatch, {LETTER: (200, "text/html", _loader()), BOOT: (200, "text/html", _loader())})
    v = D.link_verdict(LETTER)
    assert not v["ok"] and "UNKNOWN_BODY" in v["why"] and calls == [LETTER, BOOT]


@pytest.mark.parametrize("href, code", [
    ("https://evil.example/kabinets.php?" + BOOT.split("?", 1)[1], "LOADER_BOOT_LINK_ELSEWHERE"),
    ("//plani.tiktik.lv.evil.example/kabinets.php?" + BOOT.split("?", 1)[1], "LOADER_BOOT_LINK_ELSEWHERE"),
    ("kabinets2.php?" + BOOT.split("?", 1)[1], "LOADER_BOOT_LINK_ELSEWHERE"),
    (REL_BOOT.replace(TOKEN, "f" * 32), "LOADER_BOOT_LINK_MISMATCH: it changes the letter's query (k)"),
    (REL_BOOT.replace("&boot=1", "&boot=0"), "LOADER_BOOT_LINK_MISMATCH: its query does not end in boot=1"),
    (REL_BOOT + "&x=1", "LOADER_BOOT_LINK_MISMATCH: its query does not end in boot=1"),
    (REL_BOOT.replace("&tab=preces", ""), "LOADER_BOOT_LINK_MISMATCH: it changes the letter's query (tab)"),
    (REL_BOOT + "#x", "LOADER_BOOT_LINK_MISMATCH: it carries a #fragment"),
    (REL_BOOT.replace("&boot=1", ""), "LOADER_NO_BOOT_LINK"),
])
def test_loader_with_a_wrong_boot_link_fails_closed_and_fetches_nothing_more(monkeypatch, href, code):
    calls = _site(monkeypatch, {LETTER: (200, "text/html", _loader(href)), BOOT: (200, "text/html", BOOTED)})
    v = D.link_verdict(LETTER)
    assert not v["ok"] and v["why"].startswith(code), v["why"]
    assert calls == [LETTER]


def test_loader_without_a_boot_link_fails_even_if_its_script_has_one(monkeypatch):
    calls = _site(monkeypatch, {LETTER: (200, "text/html", _loader(link=False)), BOOT: (200, "text/html", BOOTED)})
    v = D.link_verdict(LETTER)
    assert not v["ok"] and v["why"].startswith("LOADER_NO_BOOT_LINK") and calls == [LETTER]


def test_loader_with_two_different_boot_links_fails(monkeypatch):
    other = f'<a href="{_html.escape(REL_BOOT.replace(TOKEN, "f" * 32))}">cits</a>'
    _site(monkeypatch, {LETTER: (200, "text/html", _loader(extra=other)), BOOT: (200, "text/html", BOOTED)})
    v = D.link_verdict(LETTER)
    assert not v["ok"] and v["why"].startswith("LOADER_AMBIGUOUS")


@pytest.mark.parametrize("status", [302, 404, 500, None])
def test_loader_boot_not_200_fails(monkeypatch, status):
    _site(monkeypatch, {LETTER: (200, "text/html", _loader()), BOOT: (status, "text/html", BOOTED)})
    v = D.link_verdict(LETTER)
    assert not v["ok"] and v["why"].startswith(f"LOADER>BOOT: status {status}")


def test_access_choice_page_still_passes_without_a_follow(monkeypatch):
    calls = _site(monkeypatch, {LETTER: (200, "text/html", ACCESS)})
    v = D.link_verdict(LETTER)
    assert v["ok"] and "via" not in v and calls == [LETTER]


def test_login_page_on_the_letter_url_is_still_dead(monkeypatch):
    calls = _site(monkeypatch, {LETTER: (200, "text/html", LOGIN), BOOT: (200, "text/html", BOOTED)})
    v = D.link_verdict(LETTER)
    assert not v["ok"] and "dead-page marker 'Atsūtīt man saiti'" in v["why"] and calls == [LETTER]


def test_booted_cabinet_reached_directly_stays_unknown(monkeypatch):
    # Not measured: a "Mans kabinets" page on the letter URL itself is NOT a pass.
    _site(monkeypatch, {LETTER: (200, "text/html", BOOTED)})
    v = D.link_verdict(LETTER)
    assert not v["ok"] and v["why"].startswith("UNKNOWN_BODY")


# --- F6 (MAIN 2026-09-25): akcija_weekly (236) without cabinet products -> the featured page ---------------
AKCIJA = open(os.path.join(ROOT, "templates", "akcija_weekly.html"), encoding="utf-8").read()
FEATURED = "https://www.tiktik.lv/veikals/params/category/featured/"
FEATURED_W39 = FEATURED + "?utm_source=brevo&utm_medium=email&utm_campaign=2026-w39-akcija"


def _real_links(body):
    return [_html.unescape(h).strip() for h in D.C.hrefs_in(body) if not D.C.is_brevo_system_link(h.strip())]


def test_akcija_weekly_passes_the_campaign_layer_utm_seam():
    after, pairs = D.C.apply_utm_week(AKCIJA, "2026-w39")
    assert pairs == [("2026-w39-akcija", None)]
    assert D.C.UTM_WEEK_MARKER not in after


def test_akcija_weekly_contact_without_cabinet_products_gets_the_featured_page():
    after, _ = D.C.apply_utm_week(AKCIJA, "2026-w39")
    body = D.render(after, {"KABINETS_HAS_PRODUCTS": False, "VARDS": "", "UZRUNA": ""})
    assert _real_links(body) == [FEATURED_W39]
    assert "Skatīt nedēļas akcijas &rarr;" in body and "Atvērt savu kabinetu" not in body


def test_akcija_weekly_contact_with_cabinet_products_keeps_the_cabinet_box():
    body = D.render(D.C.apply_utm_week(AKCIJA, "2026-w39")[0], {"KABINETS_HAS_PRODUCTS": True, "KABINETS_URL": LETTER})
    assert _real_links(body) == [LETTER]
    assert "Atvērt savu kabinetu &rarr;" in body and FEATURED not in body


def test_static_utm_accepts_the_seam_form_and_still_flags_a_written_week():
    page = "<!DOCTYPE html><html><head></head><body>{}</body></html>"
    seam = '<a href="https://t.lv/?utm_source=brevo&amp;utm_campaign=__UTM_WEEK__-akcija">a</a>'
    hard = '<a href="https://t.lv/?utm_campaign=2026-w39-akcija">b</a>'
    bare = '<a href="https://t.lv/?utm_campaign=__UTM_WEEK__">c</a>'
    assert D.static_checks(page.format(seam), "")["template_side_utm"] == []
    assert D.static_checks(page.format(seam + hard), "")["template_side_utm"] == ["2026-w39-akcija"]
    assert D.static_checks(page.format(bare), "")["template_side_utm"] == ["__UTM_WEEK__"]
    s = D.static_checks(AKCIJA, "⟦NEDĒĻAS TĒMA⟧ nedēļa: lētāk nekā parasti")
    assert s["complete_html"] and not (s["outside_contract"] or s["nested_double_quote_hrefs"]
                                       or s["template_side_utm"] or s["percent_in_text"])


def _run_main(monkeypatch, capsys, html):
    seen = []
    monkeypatch.setattr(D.C, "template", lambda i: {"name": "t", "subject": "S", "htmlContent": html,
                                                    "isActive": False})
    monkeypatch.setattr(D, "contact_checks", lambda t, s, e, w: seen.append(t) or {
        "contact": e, "problems": [], "ok": True, "html": t, "subject": s})
    rc = D.main(["--template", "236", "--contacts", "a@x.lv", "--week", "2026-w39"])
    return rc, json.loads(capsys.readouterr().out), seen


def test_main_checks_what_the_send_path_sends_when_the_marker_is_there(monkeypatch, capsys):
    rc, rep, seen = _run_main(monkeypatch, capsys, AKCIJA)
    assert rc == 0 and rep["all_ok"]
    assert seen == [D.C.apply_utm_week(AKCIJA, "2026-w39")[0]]
    assert rep["utm_seam"] == {"marker": True, "pairs": [["2026-w39-akcija", None]]}
    assert rep["template_sha256"] == hashlib.sha256(AKCIJA.encode("utf-8")).hexdigest()  # the stored file


def test_main_never_applies_the_seam_without_the_marker(monkeypatch, capsys):
    def refuse(*a, **k):
        raise AssertionError("apply_utm_week called on a template without the marker")
    monkeypatch.setattr(D.C, "apply_utm_week", refuse)
    html = '<!DOCTYPE html><html><head></head><body><a href="{{ contact.KABINETS_URL }}">x</a></body></html>'
    rc, rep, seen = _run_main(monkeypatch, capsys, html)
    assert rc == 0 and seen == [html] and rep["utm_seam"] == {"marker": False}


def test_main_reports_a_seam_refusal_and_renders_nothing(monkeypatch, capsys):
    html = AKCIJA.replace("</body>", '<a href="https://t.lv/?utm_campaign=2026-w38-akcija">x</a></body>')
    rc, rep, seen = _run_main(monkeypatch, capsys, html)
    assert rc == 2 and not rep["all_ok"] and not rep["static_ok"] and seen == []
    assert "2026-w38-akcija" in rep["utm_seam"]["refused"]


def test_akcija_no_cabinet_contact_passes_the_draft_check_end_to_end(monkeypatch):
    # The featured page as measured 2026-09-25 (61 distinct item links), the logo, nothing else.
    page = ("<title>medēļās akcijas</title>"
            + "".join(f'<a href="/veikals/item/c/p{i}/">p</a><a href="/veikals/item/c/p{i}/">p</a>'
                      for i in range(61))).encode("utf-8")
    monkeypatch.setattr(D.C, "contact_attributes", lambda e: {
        "KABINETS_HAS_PRODUCTS": False, "VARDS": "", "UZRUNA": "", "KABINETS_URL": LETTER})
    calls = _site(monkeypatch, {FEATURED_W39: (200, "text/html; charset=UTF-8", page)})
    html, seam = D.send_path_html(AKCIJA, "2026-w39")
    r = D.contact_checks(html, "S", "x@y.lv", "2026-w39")
    assert r["ok"], r["problems"]
    assert [(l["url"], l["ok"]) for l in r["links"]] == [(FEATURED_W39, True)] and calls == [FEATURED_W39]


def test_akcija_cabinet_contact_passes_through_the_loader_end_to_end(monkeypatch):
    monkeypatch.setattr(D.C, "contact_attributes", lambda e: {
        "KABINETS_HAS_PRODUCTS": True, "VARDS": "Līga", "UZRUNA": "", "KABINETS_URL": LETTER})
    _site(monkeypatch, {LETTER: (200, "text/html", _loader()), BOOT: (200, "text/html", BOOTED)})
    r = D.contact_checks(D.send_path_html(AKCIJA, "2026-w39")[0], "S", "x@y.lv", "2026-w39")
    assert r["ok"], r["problems"]
    assert [(l["url"], l.get("via")) for l in r["links"]] == [(LETTER, "loader>boot")]
