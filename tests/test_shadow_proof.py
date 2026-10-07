import datetime as dt

import shadow_sample as S


def test_proof_refuses_without_today(monkeypatch, capsys):
    monkeypatch.setenv("PROOF_DAY", "2026-10-06")
    sent = []
    assert S.proof_main(today=dt.date(2026, 10, 7), post=sent.append) == 2 and sent == []


def test_proof_dry_posts_nothing(monkeypatch):
    monkeypatch.setenv("PROOF_DAY", "2026-10-07"); monkeypatch.setenv("SAMPLE_DRY", "1")
    sent = []
    assert S.proof_main(today=dt.date(2026, 10, 7), post=sent.append) == 0 and sent == []


def test_proof_one_mail_to_raivis_only(monkeypatch):
    monkeypatch.setenv("PROOF_DAY", "2026-10-07"); monkeypatch.delenv("SAMPLE_DRY", raising=False)
    sent = []
    assert S.proof_main(today=dt.date(2026, 10, 7), post=lambda p: sent.append(p) or {"messageId": "x"}) == 0
    assert len(sent) == 1 and sent[0]["to"] == [{"email": "raivis@alenda.lv"}]
    assert "templateId" not in sent[0] and "{{ unsubscribe }}" in sent[0]["htmlContent"]
    assert len(sent[0]["params"]["own"]) == 2 and len(sent[0]["params"]["blocks"][0]["items"]) == 2
