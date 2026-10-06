"""Daily shadow sample (CABINET-FIRST v1 CF5; released by MAIN 2026-10-06, contract 15e761dce94e).

Every morning, after the send-time evaluation, ONE real planned letter per type that is deliverable today is rendered
with that client's own fields and mailed to raivis@alenda.lv ONLY, so the price in the letter and in the client's
cabinet can be compared. Two halves, because no identity holds both rights:
  1. the 08:55 job (ops account, reads every table) picks the letters: send_lookups.sample_pick -> rows in
     mkt_control.shadow_sample (one per type; a skipped type carries its reason);
  2. THIS module, in Cloud Run job tiktik-shadow-sample (tiktik-campaign-sa: the only identity that may read the Brevo
     key; it reads mkt_control only), renders and mails them.

What a sample is made of: the Brevo template as Brevo holds it (GET), the client's live Brevo attributes (GET) with
the writer's mkt_control.letter_fields row of today laid over them - the row wins, field for field, exactly the set
the letter would carry (WO1). Rendering = draft_test.render.

NOTHING ELSE HAPPENS: no send_log row, no sequence state, no Pipedrive write, no Brevo contact or attribute write, no
template put or activated. The only outward call that changes anything is one transactional mail to TEST_RECIPIENT.
Fail closed: a field or block the renderer cannot resolve, a visible placeholder, a missing / EXCLUDED / foreign row
-> that type is NOT mailed and the reason is printed.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html as _html
import json
import os
import re
import sys

import campaign as C
import draft_test as DT

P = "jaunais-za-aizv04022026"
T_QUEUE = f"{P}.mkt_control.shadow_sample"
T_LF = f"{P}.mkt_control.letter_fields"
RECIPIENT = DT.TEST_RECIPIENT                    # raivis@alenda.lv - a constant of draft_test, not a parameter
# FOUND 2026-10-06, before the first sample was mailed: Brevo holds the 25.09 files of 179 / 180 / 234 / 235, not the
# files Raivis approved on 30.09 - 01.10 (234 in Brevo still shows price placeholders). mkt_control.template_approval
# approves an id, not a content. A sample is mailed ONLY when Brevo's htmlContent is byte-equal to the approved file:
# sha256 of templates/<type>.html at the approved commit on feat/v2.8-price-fields. An id that is not listed here is
# never mailed. INTERIM home of these hashes - MAIN decides where the approved hash lives.
APPROVED_SHA256 = {
    179: "035071516cb38839431bbca294349535e016133eca5e0269c9bb34589b89bd53",   # commit faaec26 (mkt_control.template_approval note)
    180: "010b3a30bf3136c5312ce6cabfcbcc98f760085c3b407945150a84923470149b",   # commit aa0ce78 (mkt_control.template_approval note)
    234: "0e5d9ddd04bf9fb8dab0e0f03cad480664817af9bbc8bc586fe2d3911a5949aa",   # commit d4ed418 (mkt_control.template_approval note)
    235: "de294f09076ad824281824508ab6f44bc66dc47a7fd07ef5c018fc03c3ae0011",   # commit 9bde7f6 (mkt_control.template_approval note)
}
_LEFT_FIELD = re.compile(r"\{\{\s*contact\.[A-Za-z0-9_]+[^}]*\}\}")
_LEFT_BLOCK = re.compile(r"\{%[^%]*%\}")


def letter_attrs(brevo_attrs, row) -> dict:
    """The client's Brevo attributes with today's letter_fields row over them (UPPERCASE columns; NULL = empty)."""
    a = dict(brevo_attrs or {})
    for k, v in dict(row).items():
        if k.isupper():
            a[k] = "" if v is None else v
    return a


def banner(day, email_type, master_key) -> str:
    return ("<div style=\"background:#fff3cd;padding:10px 14px;font:13px Arial;color:#5c4400;\">"
            f"PARAUGS {day:%d.%m.%Y} · {_html.escape(email_type)} · šī ir klienta <b>{_html.escape(master_key)}</b> "
            "vēstule ar viņa paša laukiem. Klientam NAV sūtīta. Pogas ved uz ĪSTĀ klienta kabinetu — "
            "pasūtījums vai izmaiņas tur būtu īstas.</div>")


def build(day, pick, row, tpl, brevo_attrs) -> dict:
    """Pure. -> {problems: [...], payload: {...} | None, template_sha256, template_modified}."""
    et, problems = pick["email_type"], []
    html_t, subj_t = tpl.get("htmlContent") or "", tpl.get("subject") or ""
    full = hashlib.sha256(html_t.encode()).hexdigest()
    want = APPROVED_SHA256.get(int(pick["template_id"]))
    info = {"template_sha256": full[:12], "approved_sha256": want and want[:12],
            "template_modified": tpl.get("modifiedAt"), "template_active": tpl.get("isActive")}
    if row is None:
        problems.append("no letter_fields row today")
    elif row.get("email_type") != et:
        problems.append(f"letter_fields row is for {row.get('email_type')}, not {et}")
    elif row.get("letter") == "EXCLUDED":
        problems.append("letter_fields row is EXCLUDED (SG5)")
    if not html_t:
        problems.append("Brevo template has no htmlContent")
    elif want is None:
        problems.append(f"no approved content hash for template {pick['template_id']}")
    elif full != want:
        problems.append(f"Brevo holds another file than the approved one (Brevo {full[:12]}, approved {want[:12]})")
    if C.UTM_WEEK_MARKER in html_t:
        problems.append("template carries the campaign week marker - not an engine template")
    if problems:
        return {"problems": problems, "payload": None, **info}
    attrs = letter_attrs(brevo_attrs, row)
    html, subject = DT.render(html_t, attrs), DT.render(subj_t, attrs)
    left = sorted(set(_LEFT_FIELD.findall(html + subject)))
    if left:
        problems.append("unresolved fields: " + ", ".join(left)[:300])
    if _LEFT_BLOCK.search(html + subject):
        problems.append("unresolved block: " + _LEFT_BLOCK.search(html + subject).group(0)[:120])
    ph = sorted(set(DT.PLACEHOLDER.findall(html + subject)))
    if ph:
        problems.append("visible placeholder: " + ", ".join(ph)[:200])
    b = banner(day, et, pick["master_key"])
    body, n = re.subn(r"(<body[^>]*>)", lambda m: m.group(1) + b, html, count=1)
    payload = {"sender": {"id": C.SENDER_ID}, "to": [{"email": RECIPIENT}],
               "subject": f"PARAUGS {day:%d.%m.%Y} · {et} · {subject}",
               "htmlContent": body if n else b + html, "tags": ["shadow-sample", et]}
    return {"problems": problems, "payload": None if problems else payload, **info}


def send(payload) -> dict:
    assert payload["to"] == [{"email": RECIPIENT}] and RECIPIENT == "raivis@alenda.lv", "recipient guard"
    assert not {"cc", "bcc", "templateId", "params", "messageVersions"} & set(payload), "payload guard"
    return C._call("POST", "/smtp/email", payload)


def main() -> int:
    import zoneinfo
    from google.cloud import bigquery
    bq = bigquery.Client(project=P)
    day = dt.datetime.now(zoneinfo.ZoneInfo("Europe/Riga")).date()
    par = lambda **k: bigquery.QueryJobConfig(query_parameters=[  # noqa: E731
        bigquery.ScalarQueryParameter(n, "DATE" if isinstance(v, dt.date) else "STRING", v) for n, v in k.items()])
    picks = [dict(r) for r in bq.query(
        f"SELECT email_type, template_id, master_key, email, skip_reason FROM `{T_QUEUE}` WHERE plan_date = @d "
        f"ORDER BY email_type", job_config=par(d=day)).result()]
    out = []
    for p in picks:
        res = {"email_type": p["email_type"], "template_id": p["template_id"], "master_key": p["master_key"]}
        if p["skip_reason"]:
            out.append({**res, "sent": False, "why": p["skip_reason"]})
            continue
        try:
            row = next(iter(bq.query(
                f"SELECT * FROM `{T_LF}` WHERE plan_date = @d AND LOWER(TRIM(email)) = @e "
                f"ORDER BY built_at DESC LIMIT 1", job_config=par(d=day, e=p["email"])).result()), None)
            b = build(day, p, None if row is None else dict(row), C.template(int(p["template_id"])),
                      C.contact_attributes(p["email"]))
            res.update({k: b[k] for k in ("template_sha256", "approved_sha256", "template_modified", "template_active")})
            if b["problems"]:
                out.append({**res, "sent": False, "why": "; ".join(b["problems"])})
                continue
            if os.environ.get("SAMPLE_DRY") == "1":            # render and check only; nothing is mailed
                out.append({**res, "sent": False, "why": "SAMPLE_DRY", "subject": b["payload"]["subject"][:160],
                            "html_bytes": len(b["payload"]["htmlContent"].encode()), "lf_run": row["run_id"]})
                continue
            r = send(b["payload"])
            out.append({**res, "sent": True, "messageId": r.get("messageId"), "subject": b["payload"]["subject"][:120],
                        "lf_run": row["run_id"]})
        except Exception as e:  # noqa: BLE001 - one type failing must not stop the others; nothing was mailed for it
            out.append({**res, "sent": False, "why": f"{type(e).__name__}: {e}"[:300]})
    for o in out:
        print("SAMPLE " + json.dumps(o, ensure_ascii=False, default=str))
    print("SAMPLE_DONE " + json.dumps({"day": day.isoformat(), "to": RECIPIENT, "sent": sum(o["sent"] for o in out),
                                       "not_sent": sum(not o["sent"] for o in out)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
