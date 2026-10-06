"""Put the reviewed engine templates into their EXISTING Brevo ids (Vestulu sabloni, MAIN 2026-09-24).

MAIN's command "all letters, A to Z": every new engine letter is written from the style sheet + contract v2.7,
reviewed, and then tested with draft_test.py. This script is the only writer of those templates' content.

Runs ONLY under tiktik-campaign-sa (the identity that holds the Brevo key); the key never passes a
conversation. It reads templates_manifest.json (pinned by the job wrapper), fetches every HTML file from
raw.githubusercontent.com/AlendaSIA/tiktik-marketing-sender/<--commit>/<file>, checks its git blob sha and
sha256 against the manifest, and - only with --apply - PUTs htmlContent, subject and templateName into the
template id the manifest names. It never creates, deletes, activates or sends anything.

Guards, all hard:
  - only ids in ALLOWED (the eight templates created for this command, 229-236, and 244 from the manifest
    section "post_purchase", read only with --section post_purchase);
  - the file must match the manifest byte for byte (git blob sha AND sha256), else refuse that row;
  - the live template must exist and be INACTIVE before, and stay inactive after; an active template is
    never edited from here;
  - the PRE-PUT HTML is printed in full (base64) with its sha256 BEFORE the PUT, so Cloud Logging holds the
    copy even if everything after fails (restore = base64-decode and PUT it back);
  - after the PUT the stored htmlContent must equal the file byte for byte and the subject must be equal;
  - a row whose live html, subject and name already equal the file is skipped (idempotent re-runs).
"""
import argparse
import base64
import hashlib
import json
import re
import sys
import urllib.request

import campaign as C

REPO_RAW = "https://raw.githubusercontent.com/AlendaSIA/tiktik-marketing-sender/{commit}/{path}"
# + 244 post_purchase_feedback (MAIN 2026-10-06 11:06); + 179 reorder_1, 180 winback_1 (MAIN 2026-10-06 17:28, TC3:
# Brevo held the 24-25.09 files for the approved templates - the approved file is put, isActive stays false).
ALLOWED = frozenset(range(229, 237)) | {244} | {179, 180}
# Manifest sections this script may read. "templates" stays the default, so a bare run never reaches 244:
# it is put only by naming its section (--section post_purchase); 179 / 180 only with --section live_mapped.
SECTIONS = ("templates", "post_purchase", "live_mapped", "episode_e2")
# E2 (MAIN 2026-10-06 17:53): the three real, INACTIVE Brevo templates created for the approved E2 files. The manifest
# keeps its provisional ids (9180 / 9232 / 9233) and no id mapping is changed anywhere: an episode_e2 row is put only
# with --target-id, one variant per run, and only into a template whose stored name already carries that variant.
E2_TARGETS = frozenset({255, 256, 257})


def blob_sha(b: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(b) + b).hexdigest()


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def fetch(commit: str, path: str) -> bytes:
    return urllib.request.urlopen(REPO_RAW.format(commit=commit, path=path), timeout=30).read()


def put_one(row, commit, apply, target_id=None):
    res = {"n": row.get("n"), "variant": row["variant"], "id": row["id"]}
    if target_id is not None:
        if not row.get("provisional_id") or target_id not in E2_TARGETS:
            res["refused"] = f"--target-id {target_id}: only a provisional row into {sorted(E2_TARGETS)}"
            return res, 2
        res["manifest_id"] = row["id"]
        row = dict(row, id=target_id)
        res["id"] = target_id
    elif row.get("provisional_id"):
        res["refused"] = "provisional id - name the real template with --target-id"
        return res, 2
    elif row["id"] not in ALLOWED:
        res["refused"] = f"id {row['id']} not in {sorted(ALLOWED)}"
        return res, 2
    raw = fetch(commit, row["file"])
    if blob_sha(raw) != row["git_blob"] or hashlib.sha256(raw).hexdigest() != row["sha256"]:
        res["refused"] = f"file {row['file']} at {commit[:12]} does not match the manifest"
        return res, 2
    html = raw.decode("utf-8")
    t = C.template(row["id"])
    live = t.get("htmlContent") or ""
    res.update(pre_sha256=sha256(live), file_sha256=row["sha256"], pre_active=t.get("isActive"))
    if t.get("isActive"):
        res["refused"] = "template is ACTIVE - this script never edits an active template"
        return res, 2
    if target_id is not None and not (t.get("name") or "").startswith(f"money:{row['variant']} "):
        res["refused"] = f"template {target_id} is not named for {row['variant']}: {t.get('name')!r}"
        return res, 2
    if live == html and t.get("subject") == row["subject"] and t.get("name") == row["name"]:
        res["already_equal"] = True
        res["applied"] = False
        return res, 0
    print("PRE_PUT_B64", row["id"], sha256(live), base64.b64encode(live.encode("utf-8")).decode(), flush=True)
    rc = 0
    if apply:
        C._call("PUT", f"/smtp/templates/{row['id']}",
                {"htmlContent": html, "subject": row["subject"], "templateName": row["name"], "isActive": False})
        t2 = C.template(row["id"])
        post = t2.get("htmlContent") or ""
        res.update(post_sha256=sha256(post), post_equals_file=post == html,
                   post_subject_ok=t2.get("subject") == row["subject"], post_active=t2.get("isActive"))
        if not (post == html and t2.get("subject") == row["subject"] and not t2.get("isActive")):
            rc = 3
    res["applied"] = bool(apply)
    return res, rc


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", required=True, help="full sha of the commit the files are read from")
    ap.add_argument("--manifest", default="templates_manifest.json")
    ap.add_argument("--only", default=None, help="comma-separated variants (default: every row)")
    ap.add_argument("--section", default="templates", choices=SECTIONS, help="manifest section to read")
    ap.add_argument("--target-id", type=int, default=None, help="real template id for ONE episode_e2 row")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", a.commit):
        sys.exit("commit must be a full 40-character sha")
    rows = json.load(open(a.manifest, encoding="utf-8"))[a.section]
    if a.only:
        keep = {v.strip() for v in a.only.split(",") if v.strip()}
        rows = [r for r in rows if r["variant"] in keep]
    if (a.section == "episode_e2") != (a.target_id is not None) or (a.target_id is not None and len(rows) != 1):
        sys.exit("episode_e2 is put one variant per run: --section episode_e2 --only <variant> --target-id <id>")
    out, rc = [], 0
    for r in rows:
        res, code = put_one(r, a.commit, a.apply, a.target_id)
        out.append(res)
        rc = max(rc, code)
    print(json.dumps({"commit": a.commit, "apply": a.apply, "results": out}, ensure_ascii=False, indent=1))
    return rc


if __name__ == "__main__":
    sys.exit(main())
