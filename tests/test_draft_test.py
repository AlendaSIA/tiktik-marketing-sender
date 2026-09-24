"""Offline tests for draft_test.py - no network, no Brevo."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
    assert D.test_prefix(179, 1, 3) == "[TESTS 179 · 1/3]"
    assert D.test_prefix(231, 1, 1, "winback_2", "4/8") == "[TESTS 231 · winback_2 · 4/8]"


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
