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
# A survey link that already carries a query, utm AND a fragment: the hardest case for "append nothing".
URL = "https://www.tiktik.lv/anketa.php?o=abc123&utm_source=brevo&utm_medium=email&utm_campaign=2026-w41-atsauksme#anketa"


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
