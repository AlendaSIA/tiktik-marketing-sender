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
# TEMPLATE CONTENT v1 (contract bce3bccbc46b, TC4): a sample is mailed ONLY when the sha256 of Brevo's htmlContent,
# read at this moment, equals mkt_control.template_approval.approved_sha256 of that (template id, email type). NULL or
# a mismatch = not approved = not mailed. The hashes live in the table (TC2: written by the templates conversation).
T_APPROVAL = f"{P}.mkt_control.template_approval"
T_BREVO_HASH = f"{P}.mkt_control.brevo_template_content"
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


def content_hash(html: str) -> str:
    """TC1: sha256, full lowercase hex, of the UTF-8 bytes of htmlContent exactly as Brevo returns it."""
    return hashlib.sha256((html or "").encode("utf-8")).hexdigest()


def build(day, pick, row, tpl, brevo_attrs, approved_sha256=None) -> dict:
    """Pure. -> {problems: [...], payload: {...} | None, template_sha256, template_modified}."""
    et, problems = pick["email_type"], []
    html_t, subj_t = tpl.get("htmlContent") or "", tpl.get("subject") or ""
    full = content_hash(html_t)
    want = (approved_sha256 or "").strip().lower() or None
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
        problems.append(f"TEMPLATE_NOT_APPROVED: approved_sha256 is NULL for template {pick['template_id']} (TC4)")
    elif full != want:
        problems.append(f"TEMPLATE_NOT_APPROVED: Brevo holds another file than the approved one "
                        f"(Brevo {full[:12]}, approved {want[:12]})")
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


def hash_main() -> int:
    """Hash mode (TC4): rewrite the mirror mkt_control.brevo_template_content from what Brevo holds now. GET only."""
    from google.cloud import bigquery
    bq = bigquery.Client(project=P)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    ids = [r["template_id"] for r in bq.query(
        f"SELECT DISTINCT template_id FROM `{T_APPROVAL}` WHERE template_id < 9000 ORDER BY 1").result()]
    rows = []
    for tid in ids:
        base = {"template_id": tid, "html_sha256": None, "html_bytes": None, "modified_at": None, "is_active": None,
                "error": None, "checked_at": now}
        try:
            t = C.template(int(tid))
            html = t.get("htmlContent") or ""
            rows.append({**base, "html_sha256": content_hash(html) if html else None,
                         "html_bytes": len(html.encode("utf-8")), "modified_at": t.get("modifiedAt"),
                         "is_active": t.get("isActive"), "error": None if html else "no htmlContent"})
        except Exception as e:  # noqa: BLE001 - an unreadable template is a row with an error: the gate stays closed
            rows.append({**base, "error": f"{type(e).__name__}: {e}"[:300]})
    if not rows:
        print("HASH_DONE " + json.dumps({"templates": 0, "written": False}))
        return 1
    bq.load_table_from_json(rows, T_BREVO_HASH, job_config=bigquery.LoadJobConfig(
        write_disposition="WRITE_TRUNCATE", schema=bq.get_table(T_BREVO_HASH).schema)).result()
    for r in rows:
        print("HASH " + json.dumps({k: (v[:12] if k == "html_sha256" and v else v) for k, v in r.items()}))
    print("HASH_DONE " + json.dumps({"templates": len(rows), "written": True,
                                     "errors": sum(bool(r["error"]) for r in rows)}))
    return 0


def main() -> int:
    if os.environ.get("SAMPLE_MODE") == "hash":
        return hash_main()
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
            ap = [r["approved_sha256"] for r in bq.query(
                f"SELECT approved_sha256 FROM `{T_APPROVAL}` WHERE approved AND template_id = @t AND email_type = @e",
                job_config=bigquery.QueryJobConfig(query_parameters=[
                    bigquery.ScalarQueryParameter("t", "INT64", int(p["template_id"])),
                    bigquery.ScalarQueryParameter("e", "STRING", p["email_type"])])).result()]
            b = build(day, p, None if row is None else dict(row), C.template(int(p["template_id"])),
                      C.contact_attributes(p["email"]), ap[0] if len(ap) == 1 else None)
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
