"""Template 244 (post_purchase_feedback) - POST-PURCHASE v1.1 PP1.1/PP1.2/PP4.2."""
import hashlib
import json
import os
import re

import campaign as C
import draft_test as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROW = json.load(open(os.path.join(ROOT, "templates_manifest.json"), encoding="utf-8"))["post_purchase"][0]
RAW = open(os.path.join(ROOT, ROW["file"]), "rb").read()
TPL = RAW.decode("utf-8")
# The survey link in the shape the writer mints it (letter_fields_244): query, NO utm (MAIN 2026-10-06).
URL = "https://plani.tiktik.lv/atsauksme.php?o=M-860325-34895&t=abcDEF_123-xyz"


def _attrs(greeting, **kw):
    a = {"GREETING": greeting, "ANKETA_URL": URL, "ORDER_NR": "M-860325-34895", "KABINETS_HAS_PRODUCTS": True}
    a.update(kw)
    return a


def test_manifest_row_matches_file():
    assert ROW["id"] == 244 and ROW["variant"] == "post_purchase_feedback"
    assert hashlib.sha1(b"blob %d\0" % len(RAW) + RAW).hexdigest() == ROW["git_blob"]
    assert hashlib.sha256(RAW).hexdigest() == ROW["sha256"]
    assert "%" not in ROW["subject"].replace("{{", "").replace("}}", "") and "!" not in ROW["subject"]
    assert ROW["preheader"] in TPL


def test_static_checks_clean_and_fields():
    st = D.static_checks(TPL, ROW["subject"])
    assert st["complete_html"] and not st["outside_contract"] and not st["nested_double_quote_hrefs"]
    assert not st["template_side_utm"] and not st["percent_in_text"]
    assert sorted(st["attributes"]) == ["ANKETA_URL", "GREETING", "ORDER_NR", "UZRUNA"]
    assert "KABINETS_URL" not in TPL and "elif" not in TPL


def test_three_gender_branches_render_fully():
    for greeting, want, other in (("Sveika, Anna!", "neesi paspējusi notestēt", "paspējis"),
                                  ("Sveiks, Jāni!", "neesi paspējis notestēt", "paspējusi"),
                                  ("Sveiki!", "vēl nav sanācis notestēt", "neesi pasp"),
                                  ("", "vēl nav sanācis notestēt", "neesi pasp")):
        body = D.render(TPL, _attrs(greeting))
        left = [x for x in re.findall(r"\{\{.*?\}\}|\{%.*?%\}", body, re.S) if not C.is_brevo_system_link(x)]
        assert left == [], left
        assert want in body and other not in body
        assert (greeting or "Sveiki!") in body
    body = D.render(TPL, _attrs("Sveika, Anna!"))
    assert "neesi apmierināta ar" in body and "par šo pasūtījumu M-860325-34895 –" in body
    assert "par šo pasūtījumu –" in D.render(TPL, _attrs("Sveiki!", ORDER_NR=""))


def test_nothing_is_appended_to_anketa_url():
    body = D.render(TPL, _attrs("Sveiki!"))
    hrefs = [h for h in C.hrefs_in(body) if not C.is_brevo_system_link(h)]
    assert hrefs == [URL]
    # the send path (campaign.create_draft -> apply_utm_week) is not entered: the template has no marker
    after, seam = D.send_path_html(TPL, "2026-w41")
    assert after == TPL and seam == {"marker": False}


def test_any_other_wording_and_en_fall_to_the_neutral_branch():
    """MAIN 2026-10-06: the gender branch stays on the GREETING substring ("Sveika," / "Sveiks,"); no contract field.
    Anything that is not exactly that wording - other LV wording, a missing comma, other case, EN - reads neutral."""
    for greeting in ("Labdien, Anna!", "Sveicināta, Anna!", "Sveika Anna!", "Sveiks Jāni!", "sveika, Anna!",
                     "SVEIKS, JĀNI!", "Čau, Jāni!", "Hello, Anna!", "Hi John,", "Dear Anna,", "Welcome, John!",
                     "Sveiki, Anna!", "Sveiki!", ""):
        body = D.render(TPL, _attrs(greeting))
        assert "vēl nav sanācis notestēt" in body, greeting
        for gendered in ("paspējusi", "paspējis", "apmierināta ar", "apmierināts ar"):
            assert gendered not in body, (greeting, gendered)
        assert not [x for x in re.findall(r"\{\{.*?\}\}|\{%.*?%\}", body, re.S) if not C.is_brevo_system_link(x)]


def _checks(monkeypatch, url, extra_href=""):
    tpl = TPL.replace("</body>", extra_href + "</body>")
    monkeypatch.setattr(D, "link_verdict", lambda u: {"url": u, "ok": True})
    monkeypatch.setattr(D, "fetch", lambda u, timeout=25: (200, "image/png", b""))
    return D.contact_checks(tpl, ROW["subject"], "x@example.com", "2026-w41", attrs=_attrs("Sveiki!", ANKETA_URL=url))


def test_survey_link_is_exempt_from_the_utm_check(monkeypatch):
    r = _checks(monkeypatch, URL)
    assert r["ok"], r["problems"]
    assert r["survey_link_utm_exempt"] is True and "utm_campaign" not in URL


def test_exemption_covers_only_the_anketa_url_value(monkeypatch):
    other = '<a href="https://www.tiktik.lv/veikals/item/x/">x</a>'
    r = _checks(monkeypatch, URL, other)
    assert not r["ok"]
    assert r["problems"] == ["no utm_campaign=2026-w41- on https://www.tiktik.lv/veikals/item/x/"]


def test_survey_page_markers(monkeypatch):
    good = '<title>Tavs vērtējums — tiktik.lv</title><p class="order">Pasūtījums <b>M-1</b></p>'
    dead = '<title>Tavs vērtējums — tiktik.lv</title><p class="note">Šī saite nav derīga. Vari atbildēt</p>'
    for page, ok in ((good, True), (dead, False), ("<title>Tavs vērtējums — tiktik.lv</title>", False)):
        monkeypatch.setattr(D, "fetch", lambda u, timeout=25, page=page: (200, "text/html", page.encode("utf-8")))
        assert D.link_verdict(URL)["ok"] is ok


def test_utm_theme_and_put_allowlist():
    import template_put as T
    import utm
    assert utm.THEMES["post_purchase_feedback"] == "atsauksme"
    assert utm.slug("2026-10-05", "post_purchase_feedback", "lv") == "2026-w41-atsauksme"
    assert 244 in T.ALLOWED and T.ALLOWED == frozenset(range(229, 237)) | {244}
    assert T.SECTIONS == ("templates", "post_purchase") and ROW["id"] in T.ALLOWED


def test_az_overlay_is_read_from_letter_fields_and_never_the_retired_table():
    import draft_test_az as A
    src = open(os.path.join(ROOT, "draft_test_az.py"), encoding="utf-8").read()
    assert "FROM {T_LETTER_FIELDS} " in src and "FROM shadow_brevo_price_attrs" not in src
    assert A.T_LETTER_FIELDS.endswith(".mkt_control.letter_fields`")
    lf = {"plan_date": "2026-10-06", "email": "a@b.lv", "template_id": 244, "run_id": "r1", "GREETING": "Sveika, Anna!",
          "ANKETA_URL": URL, "ORDER_NR": "M-860325-34895", "P1_PRICE": None, "n_slots": 0, "would_send": True}
    seen = []

    def rows(sql, email, plan_date, template_id=None, r244=None):
        seen.append(sql)
        if "letter_fields_244" in sql:
            return r244 if r244 is not None else [{"ORDER_NR": "M-860325-34895", "ANKETA_URL": URL, "source": "s"}]
        return [lf]
    ov, info = A.overlay_from_letter_fields("a@b.lv", 244, "2026-10-06", rows=rows)
    assert ov == {"GREETING": "Sveika, Anna!", "ANKETA_URL": URL, "ORDER_NR": "M-860325-34895"}
    assert "refused" not in info and info["letter_fields_244"]["equal"] and len(seen) == 2
    # a different minted link, no 244 row, or no letter_fields row: refused, never guessed
    for r244 in ([{"ORDER_NR": "M-860325-34895", "ANKETA_URL": URL + "x"}], []):
        _, info = A.overlay_from_letter_fields("a@b.lv", 244, "2026-10-06",
                                               rows=lambda *a, r244=r244, **k: rows(*a, r244=r244, **k))
        assert "refused" in info
    _, info = A.overlay_from_letter_fields("a@b.lv", 244, "2026-10-06", rows=lambda *a, **k: [])
    assert "refused" in info and info["rows"] == 0


def test_az_test_copy_never_carries_the_customers_survey_link():
    import draft_test_az as A
    body, replaced, left = A.neutralise(D.render(TPL, _attrs("Sveiki!")), "#")
    assert replaced == 1 and left == [] and URL.split("?")[0] not in body and 244 in A.SURVEY_LETTERS
