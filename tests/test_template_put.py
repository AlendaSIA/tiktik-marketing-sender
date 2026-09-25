"""Offline tests for template_put.py - no network, no Brevo."""
import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import template_put as T  # noqa: E402

HTML = "<!DOCTYPE html><html><head></head><body>Sveiki! ⟦TAVA CENA P1⟧</body></html>"
RAW = HTML.encode("utf-8")
ROW = {"n": 1, "variant": "winback_2", "id": 232, "file": "templates/winback_2.html",
       "git_blob": T.blob_sha(RAW), "sha256": hashlib.sha256(RAW).hexdigest(),
       "subject": "Jauna preču partija — par tavu cenu", "name": "money:winback_2 v1"}
COMMIT = "5ede135870585e1d996f3bac2c061bd811c1c734"


def _brevo(monkeypatch, live, active=False):
    state = {"html": live, "subject": "old", "name": "old", "isActive": active, "puts": []}
    monkeypatch.setattr(T.C, "template", lambda i: {"htmlContent": state["html"], "subject": state["subject"],
                                                     "name": state["name"], "isActive": state["isActive"]})

    def call(method, path, payload):
        state["puts"].append((method, path, payload))
        state["html"], state["subject"], state["name"] = payload["htmlContent"], payload["subject"], payload["templateName"]
        return {}
    monkeypatch.setattr(T.C, "_call", call)
    return state


def test_dry_run_puts_nothing(monkeypatch):
    monkeypatch.setattr(T, "fetch", lambda c, p: RAW)
    st = _brevo(monkeypatch, "old html")
    res, rc = T.put_one(ROW, COMMIT, apply=False)
    assert rc == 0 and st["puts"] == [] and res["applied"] is False


def test_apply_writes_exact_file_and_stays_inactive(monkeypatch):
    monkeypatch.setattr(T, "fetch", lambda c, p: RAW)
    st = _brevo(monkeypatch, "old html")
    res, rc = T.put_one(ROW, COMMIT, apply=True)
    assert rc == 0 and res["post_equals_file"] and res["post_subject_ok"] and not res["post_active"]
    assert st["puts"][0][1] == "/smtp/templates/232" and st["puts"][0][2]["isActive"] is False


def test_refuses_active_template(monkeypatch):
    monkeypatch.setattr(T, "fetch", lambda c, p: RAW)
    st = _brevo(monkeypatch, "old html", active=True)
    res, rc = T.put_one(ROW, COMMIT, apply=True)
    assert rc == 2 and "ACTIVE" in res["refused"] and st["puts"] == []


def test_refuses_file_not_matching_manifest(monkeypatch):
    monkeypatch.setattr(T, "fetch", lambda c, p: RAW + b" ")
    st = _brevo(monkeypatch, "old html")
    res, rc = T.put_one(ROW, COMMIT, apply=True)
    assert rc == 2 and "does not match" in res["refused"] and st["puts"] == []


def test_refuses_id_outside_allowed(monkeypatch):
    row = dict(ROW, id=179)
    res, rc = T.put_one(row, COMMIT, apply=True)
    assert rc == 2 and "not in" in res["refused"]


def test_idempotent_when_equal(monkeypatch):
    monkeypatch.setattr(T, "fetch", lambda c, p: RAW)
    st = _brevo(monkeypatch, HTML)
    st["subject"], st["name"] = ROW["subject"], ROW["name"]
    res, rc = T.put_one(ROW, COMMIT, apply=True)
    assert rc == 0 and res["already_equal"] and st["puts"] == []


def test_manifest_rows_are_allowed_and_complete():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rows = json.load(open(os.path.join(here, "templates_manifest.json"), encoding="utf-8"))["templates"]
    assert [r["n"] for r in rows] == list(range(1, 9))
    assert all(r["id"] in T.ALLOWED for r in rows)
    for r in rows:
        raw = open(os.path.join(here, r["file"]), "rb").read()
        assert T.blob_sha(raw) == r["git_blob"] and hashlib.sha256(raw).hexdigest() == r["sha256"]
        assert "%" not in r["subject"] and "!" not in r["subject"]


def test_commit_must_be_full_sha():
    with pytest.raises(SystemExit):
        T.main(["--commit", "5ede135"])
