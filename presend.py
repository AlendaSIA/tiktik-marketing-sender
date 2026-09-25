"""PRE-SEND LIST, machine part: BLOCKING checks that must pass before ANY customer send of an engine letter.

The whole list, including the items no machine can check, is docs/pre_send_checklist.md. This file checks
the letters themselves, read live from Brevo (GET /smtp/templates/{id} only - nothing is written):

  FRESH_LINE_UNGATED - MAIN, command 5, 2026-09-25, verbatim: "the 'jauna partija' line in 232/233/234 is
      today inside {% if contact.P1_NAME %} only. It must be wrapped in the v2.8 fresh flag before any send -
      add this as a blocking check to your pre-send list so it cannot be forgotten." Every "partij..." word
      must stand inside {% if contact.<FRESH_FLAG_FIELD> %} (its true branch, at any depth). The field name
      comes with contract v2.8; until it is written below, EVERY such line is ungated and blocks.
  PLACEHOLDER_LEFT - any U+27E6 bracket left in subject, preheader or HTML (the same rule campaign.send_now
      enforces since F7, listed here so the pre-send list shows it before a send is even attempted).

Run: python presend.py [--templates 229,230,...]   -> ONE JSON report; exit 0 only if nothing blocks.
"""
import argparse
import json
import re
import sys

import campaign as C

FRESH_FLAG_FIELD = None          # contract v2.8 names it; None = not in the contract yet = every fresh line blocks
DEFAULT_TEMPLATES = (229, 230, 231, 232, 233, 234, 235, 236)
_FRESH = re.compile(r"partij", re.I)                         # partija / partiju / partijas / partijai
_TAG = re.compile(r"\{%\s*(if\s+(.+?)|else|endif)\s*%\}", re.S)


def fresh_line_blockers(html, flag=FRESH_FLAG_FIELD):
    """Every "partij" occurrence that is NOT inside the true branch of {% if contact.<flag> %}."""
    html = html or ""
    events = [(m.start(), "tag", m) for m in _TAG.finditer(html)] + \
             [(m.start(), "word", m) for m in _FRESH.finditer(html)]
    stack, out = [], []                     # stack of [condition, in_else]
    for pos, kind, m in sorted(events, key=lambda e: e[0]):
        if kind == "tag":
            word = m.group(1).split()[0]
            if word == "if":
                stack.append([m.group(2).strip(), False])
            elif word == "else" and stack:
                stack[-1][1] = True
            elif word == "endif" and stack:
                stack.pop()
            continue
        gated = flag is not None and any(c == "contact." + flag and not e for c, e in stack)
        if not gated:
            ctx = " ".join(html[max(0, pos - 60): pos + 60].split())
            out.append({"at": pos, "context": ctx,
                        "why": "no fresh flag in the contract yet" if flag is None
                        else "not inside {% if contact." + flag + " %}"})
    return out


def check_template(tid):
    t = C.template(tid)
    html, subject = t.get("htmlContent") or "", t.get("subject") or ""
    blockers = []
    fresh = fresh_line_blockers(html)
    if fresh:
        blockers.append({"check": "FRESH_LINE_UNGATED", "count": len(fresh), "first": fresh[0]["context"],
                         "why": fresh[0]["why"]})
    hits = C.placeholder_hits(subject, "", html)
    if hits:
        blockers.append({"check": "PLACEHOLDER_LEFT", "count": len(hits),
                         "tokens": sorted({h["token"] for h in hits})[:6]})
    return {"id": tid, "subject": subject, "active": t.get("isActive"), "blocked": bool(blockers),
            "blockers": blockers}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--templates", default=",".join(str(t) for t in DEFAULT_TEMPLATES))
    a = ap.parse_args(argv)
    ids = [int(x) for x in re.split(r"[,+]", a.templates) if x.strip()]
    rows = [check_template(t) for t in ids]
    report = {"pre_send_list": "docs/pre_send_checklist.md", "fresh_flag_field": FRESH_FLAG_FIELD,
              "templates": rows, "blocked": [r["id"] for r in rows if r["blocked"]],
              "may_send": not any(r["blocked"] for r in rows)}
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["may_send"] else 1


if __name__ == "__main__":
    sys.exit(main())
