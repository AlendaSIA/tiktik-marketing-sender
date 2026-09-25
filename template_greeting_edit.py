"""One-off: replace the gendered SVEICIENS greeting in 179/180 with the NEUTRAL ladder (contract v2.7, RULE 7).

MAIN decision 2026-09-24 (after the Vestulu sabloni report), Raivis' decision of 16.09:
  UZRUNA if valid, else "Sveiki, <VARDS>!", else "Sveiki!". SVEICIENS leaves the template.

VALIDITY OF UZRUNA IS THE WRITER'S JOB (rule 7: "every run reports the FALLBACK COUNT at each rung" - the
sync run). Brevo Liquid could test for a space but not for a company designation reliably, and campaign.py's
link-check renderer understands only `contact.X` / `"s" in contact.X` conditions. So the template reads
UZRUNA as "valid if present", and draft_test.py re-checks validity on every test contact (fail if not).

Runs ONLY under tiktik-campaign-sa (the identity that holds the Brevo key); the key never passes a
conversation. Without --apply it changes nothing and prints what it would do.

Guards, all hard:
  - the old greeting occurs EXACTLY once, else refuse;
  - after the edit: SVEICIENS count 0, if +2, endif +2, else +2, nothing else changed (checked by re-reading
    Brevo and comparing to old.replace(OLD, NEW) byte for byte);
  - the PRE-EDIT HTML is printed in full (base64) with its sha256 BEFORE the PUT, so Cloud Logging holds
    the copy even if everything after fails.
"""
import argparse
import base64
import hashlib
import json
import sys

import campaign as C

OLD = '<p style="margin:0 0 10px;">{{ contact.SVEICIENS | default : "Sveiki!" }}</p>'
NEW = ('<p style="margin:0 0 10px;">{% if contact.UZRUNA %}Sveiki, {{ contact.UZRUNA }}!'
       '{% else %}{% if contact.VARDS %}Sveiki, {{ contact.VARDS }}!{% else %}Sveiki!{% endif %}{% endif %}</p>')
ALLOWED = {179, 180}


def counts(h):
    return {"if": h.count("{% if "), "endif": h.count("{% endif %}"), "else": h.count("{% else %}"),
            "SVEICIENS": h.count("SVEICIENS")}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", type=int, required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    if a.template not in ALLOWED:
        sys.exit(f"template {a.template} not in {sorted(ALLOWED)}")
    t = C.template(a.template)
    old = t.get("htmlContent") or ""
    sha_old = hashlib.sha256(old.encode()).hexdigest()
    n = old.count(OLD)
    out = {"template": a.template, "name": t.get("name"), "isActive": t.get("isActive"),
           "pre_sha256": sha_old, "pre_bytes": len(old.encode()), "old_greeting_occurrences": n,
           "pre_counts": counts(old)}
    print("PRE_EDIT_B64", a.template, sha_old, base64.b64encode(old.encode()).decode(), flush=True)
    if n != 1:
        out["refused"] = f"old greeting occurs {n} times, expected exactly 1"
        print(json.dumps(out, ensure_ascii=False))
        return 2
    expected = old.replace(OLD, NEW)
    out["expected_sha256"] = hashlib.sha256(expected.encode()).hexdigest()
    out["expected_counts"] = counts(expected)
    d = {k: out["expected_counts"][k] - out["pre_counts"][k] for k in out["pre_counts"]}
    out["delta"] = d
    if d != {"if": 2, "endif": 2, "else": 2, "SVEICIENS": -out["pre_counts"]["SVEICIENS"]} \
            or out["expected_counts"]["SVEICIENS"] != 0:
        out["refused"] = f"unexpected count delta {d}"
        print(json.dumps(out, ensure_ascii=False))
        return 2
    if a.apply:
        C._call("PUT", f"/smtp/templates/{a.template}", {"htmlContent": expected})
        after = C.template(a.template).get("htmlContent") or ""
        out["post_sha256"] = hashlib.sha256(after.encode()).hexdigest()
        out["post_equals_expected"] = after == expected
        out["post_counts"] = counts(after)
        if not out["post_equals_expected"]:
            print(json.dumps(out, ensure_ascii=False))
            return 3
    out["applied"] = bool(a.apply)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
