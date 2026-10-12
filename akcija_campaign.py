"""Weekly akcija 236 as ONE Brevo campaign per week - interface AKCIJA ATTRIBUTES v1 (MAIN 11.10 11:05, I1-I7).
PREPARATION ONLY (MAIN 11.10 11:35 task (b)): until "Vēstuļu šabloni 7b" delivers the 236 frame template id this module
builds the two pieces that do not depend on the frame, and nothing else:

  region   I5: the week's SHARED maker blocks (the same for every recipient - I1), rendered as HTML for the frame's
           placeholder <!--MAKER_BLOCKS-->. Blocks = akcija_assembly's pools/rules for the week's maker
           (mkt_control.akcija_week + akcija_week_blocks, DEFAULT auto_cat:1..4), WITHOUT the per-contact WA6 drop.
           fill_frame() puts it into a frame and refuses a frame without exactly one placeholder.
  audience I6: Brevo list 3, LV contacts, minus suppressed, minus NO_PROMO (manual + all B2B, flow gate list 96).
           Measured from the Brevo list snapshot (mkt_control.edu_brevo_list) and BigQuery; the campaign itself will
           carry listIds [3] and exclusionListIds [4, 46, 75, 96] so Brevo applies the same rule at send time.

No Brevo write, no attribute write, no campaign: --shadow prints the gate + funnel and writes the campaign HTML to GCS.
v1.2 (MAIN 17:45): the shared maker region is rendered by the writer into mkt_control.ak_maker_html; region_html()
below stays only as a reference renderer, the campaign uses the writer's html.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import sys

import akcija_assembly as A

P = "jaunais-za-aizv04022026"
M = f"{P}.mkt_control"
PLACEHOLDER = "<!--MAKER_BLOCKS-->"
LIST_ALL, LIST_SUPPRESSION, LIST_EN, LIST_EE, LIST_NO_PROMO = 3, 4, 46, 75, 96
LIST_EXCL = 98                    # AKCIJA IZSLĒGT: info slot (a) + reorder_1 (b) + cold B2B leads (c) + quiet never-buyers (d)
CAMPAIGN_LISTS = {"listIds": [LIST_ALL], "exclusionListIds": [LIST_SUPPRESSION, LIST_EN, LIST_EE, LIST_NO_PROMO, LIST_EXCL]}
REGION_GCS = "gs://jaunais-za-aizv04022026-brevo-history/_akcija/region_{week}.html"
GREEN = "#12603f"


# ------------------------------------------------------------------------------------------------------- region
def _card(g) -> str:
    name, url, img = html.escape(g["name"] or ""), html.escape(g["url"] or "", quote=True), html.escape(g["img"] or "", quote=True)
    sale, std = html.escape(A.price(g["sale"])), html.escape(A.price(g["std"]))
    return (f'<td width="50%" valign="top" style="padding:8px;">'
            f'<a href="{url}" style="text-decoration:none;color:#222;">'
            f'<img src="{img}" alt="{name}" width="240" style="display:block;width:100%;max-width:240px;height:auto;border:0;margin:0 auto 8px;">'
            f'<span style="display:block;font-size:14px;line-height:1.4;">{name}</span></a>'
            f'<span style="display:block;margin-top:4px;font-size:16px;font-weight:bold;color:{GREEN};">{sale}</span>'
            f'<span style="display:block;font-size:13px;color:#8a8a8a;text-decoration:line-through;">{std}</span></td>')


def region_html(blocks) -> str:
    """blocks = A.pick_blocks(...) output (shared). Two cards per row; an odd last card gets an empty cell."""
    out = []
    for b in blocks:
        items = b["items"]
        rows = []
        for i in range(0, len(items), 2):
            pair = [_card(g) for g in items[i:i + 2]]
            if len(pair) == 1:
                pair.append('<td width="50%"></td>')
            rows.append("<tr>" + "".join(pair) + "</tr>")
        title = html.escape(b.get("title") or "")
        out.append((f'<h2 style="margin:24px 0 8px;font-size:18px;color:#222;">{title}</h2>' if title else "")
                   + '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
                   + "".join(rows) + "</table>")
    return "\n".join(out)


def fill_frame(frame: str, region: str) -> str:
    """I5: the engine fills the region when it creates the campaign. Exactly one placeholder, a non-empty region."""
    n = frame.count(PLACEHOLDER)
    if n != 1:
        raise ValueError(f"frame must hold exactly one {PLACEHOLDER}, it holds {n}")
    if not region.strip():
        raise ValueError("empty maker region - no campaign without the week's goods")
    return frame.replace(PLACEHOLDER, region)


def shared_blocks(query, day: dt.date) -> dict:
    """The week's maker and its shared blocks, from the same tables and rules as the per-contact assembly."""
    tue, week_id, end = A.week_of(day)
    wk = query(f"SELECT maker FROM `{A.T_WEEK}` WHERE week_id = '{week_id}'")
    if len(wk) != 1:
        return {"week_id": week_id, "send_date": tue.isoformat(), "status": "NO_WEEK_ROW", "rows": len(wk)}
    maker = wk[0]["maker"]
    brows = query(f"SELECT block_no, title, selector, top_n, max_per_kind, max_per_category FROM `{A.T_BLOCKS}` "
                  f"WHERE maker = '{maker}' ORDER BY block_no")
    rules = A.parse_rules(brows or A.DEFAULT_RULES)
    goods = [g for g in query(A.sql_goods(maker)) if A._b(g["sellable"]) and g["url"] and g["img"]]
    cats = query(A.sql_cat_order(maker))
    blocks = A.pick_blocks(rules, A.build_pools(rules, goods, [c["category"] for c in cats]), set(),
                           {c["category"]: c["name"] for c in cats})
    return {"week_id": week_id, "send_date": tue.isoformat(), "lidz": A.lidz(end), "h1": A.h1(maker), "maker": maker,
            "rules": "akcija_week_blocks" if brows else "DEFAULT", "week_goods": len(goods), "status": "OK",
            "blocks": blocks}


# ----------------------------------------------------------------------------------------------------- audience
AUDIENCE_SQL = f"""
WITH bl AS (SELECT DISTINCT LOWER(TRIM(email)) AS email, list_id FROM `{M}.edu_brevo_list`
            WHERE list_id IN ({LIST_ALL}, {LIST_SUPPRESSION}, {LIST_EN}, {LIST_EE})),
x AS (SELECT email, LOGICAL_OR(list_id = {LIST_ALL}) AS l3, LOGICAL_OR(list_id = {LIST_SUPPRESSION}) AS l4,
             LOGICAL_OR(list_id = {LIST_EN}) AS l46, LOGICAL_OR(list_id = {LIST_EE}) AS l75 FROM bl GROUP BY 1),
s AS (SELECT DISTINCT LOWER(TRIM(email)) AS email FROM `{P}.business_marts.email_suppression_all`),
bs AS (SELECT LOWER(TRIM(email)) AS email, LOGICAL_OR(email_blocklisted OR NOT email_subscribed) AS blocked
       FROM `{P}.business_marts.brevo_contacts_snapshot` GROUP BY 1),
sg AS (SELECT email, MIN(language) AS language FROM `{M}.edu_segment` GROUP BY 1),
np AS (SELECT DISTINCT email FROM `{M}.flow_gate_list_members` WHERE list_name = 'NO_PROMO'),
ld AS (SELECT DISTINCT email FROM `{M}.b2b_lead_email` WHERE is_lead),
qn AS (SELECT DISTINCT email FROM `{M}.quiet_never_buyer`)
SELECT x.email,
  CASE WHEN x.l4 THEN 'LIST_4_SUPPRESSION' WHEN x.l46 THEN 'EN_LIST' WHEN x.l75 THEN 'EE_LIST'
       WHEN s.email IS NOT NULL THEN 'SUPPRESSED' WHEN IFNULL(bs.blocked, FALSE) THEN 'BREVO_BLOCKLISTED'
       WHEN IFNULL(sg.language, 'lv') != 'lv' THEN 'NOT_LV'
       WHEN np.email IS NOT NULL THEN 'NO_PROMO'
       WHEN ld.email IS NOT NULL THEN 'COLD_B2B_LEAD'
       WHEN qn.email IS NOT NULL THEN 'QUIET_NEVER_BUYER'
       ELSE 'IN' END AS gate
FROM x LEFT JOIN s USING (email) LEFT JOIN bs USING (email) LEFT JOIN sg USING (email) LEFT JOIN np USING (email)
  LEFT JOIN ld USING (email) LEFT JOIN qn USING (email)
WHERE x.l3"""


TEMPLATE_ID = 236                  # legacy id of the weekly letter; the CAMPAIGN frame is never taken from it
EMAIL_TYPE = "akcija_weekly"       # MAIN 11.10 19:55 (C) via MAIN 2 12.10: frame = the approval row of this type
T_APPROVAL = f"{M}.template_approval"


def frame_template(query):
    """The 236 campaign frame id from the approval rows: exactly ONE approved row (by Raivis) of email_type
    'akcija_weekly' with a sha. -> ({"template_id", "approved_sha256"}, None) or (None, "HOLD FRAME_ID ...")."""
    rows = query(f"SELECT template_id, approved_sha256 FROM `{T_APPROVAL}` WHERE email_type = '{EMAIL_TYPE}' "
                 f"AND approved AND approved_by = 'Raivis' AND approved_sha256 IS NOT NULL")
    if len(rows) != 1:
        return None, f"HOLD FRAME_ID: {len(rows)} approved rows for {EMAIL_TYPE} (exactly one needed)"
    r = rows[0]
    return {"template_id": int(r["template_id"]), "approved_sha256": str(r["approved_sha256"]).strip().lower()}, None
CAMPAIGN_GCS = "gs://jaunais-za-aizv04022026-brevo-history/_akcija/campaign_{week}.html"


def frame_order_ok(frame: str) -> bool:
    """v1.2 frame order: own goods ({{ contact.AK_HTML }} / AK2) come BEFORE <!--MAKER_BLOCKS-->."""
    i = frame.find(PLACEHOLDER)
    return i > 0 and "contact.AK_HTML" in frame[:i]


def shadow(query, put, day=None, frame_fn=None, gate_date=None) -> dict:
    """v1.2 DRY: gate (AK-H1 + ak_maker_html), the campaign HTML = 236 frame with the writer's maker html at
    <!--MAKER_BLOCKS--> (written to GCS only), and the audience funnel. No Brevo write, no campaign."""
    import block_gate as BG
    day = day or dt.date.today()
    tue, week_id, _end = A.week_of(day)
    res = {"week_id": week_id, "send_date": tue.isoformat()}
    frame_row, frame_why = frame_template(query)
    res["template_id"] = frame_row["template_id"] if frame_row else None
    gd = str(gate_date or res["send_date"])      # MAIN 19:05: DRY may test the gate against another day's push rows
    if gd != res["send_date"]:
        res["gate_date"] = gd
    res["gate"] = BG.ak_h1(query, gd, week_id) or "OK"                    # 236 may go only on "OK"
    wk = query(f"SELECT maker FROM `{A.T_WEEK}` WHERE week_id = '{week_id}'")
    res["maker_of_week"] = wk[0]["maker"] if len(wk) == 1 else None
    if res["maker_of_week"] == A.MIX:                                     # MAIN 19:00: MIX list from week_price_mix16
        mrows, why = BG.mix_rows(query, week_id)
        res["mix_goods"] = len(mrows)
        if why and res["gate"] == "OK":
            res["gate"] = why
    # MAIN 18:50: checked at send time; the first of 3 sends (09:00/09:15/09:30) may slide to the OK row (until 10:30)
    last = BG.latest_push(query, gd, "AK_HTML") if BG._exists(query, BG.T_BLOCK_LOG) else None
    res["ak_push_latest"] = last and {k: str(last.get(k)) for k in ("mode", "status", "errors", "week_id", "finished_at")}
    ok_at = BG._aware(last["finished_at"]) if res["gate"] == "OK" and last else None
    res["sends_riga"] = [t.astimezone(BG.RIGA).strftime("%H:%M") for t in BG.sends_236(gd, ok_at)]
    row, why = BG.maker_html(query, week_id)
    if why:
        res["maker"] = why
    else:
        h = row["html"]
        full = hashlib.sha256(h.encode("utf-8")).hexdigest()
        res["maker"] = {"html_bytes": len(h.encode("utf-8")), "html_sha256": str(row["html_sha256"])[:12],
                        "sha_matches_html": full.startswith(str(row["html_sha256"]).strip().lower()),
                        "built_at": str(row.get("built_at"))}
        frame = frame_fn(frame_row["template_id"]) if (frame_fn and frame_row) else ""
        live_sha = hashlib.sha256((frame or "").encode("utf-8")).hexdigest()
        try:
            if frame_why:
                raise ValueError(frame_why[len("HOLD FRAME: "):] if frame_why.startswith("HOLD FRAME: ") else frame_why)
            if live_sha != frame_row["approved_sha256"]:
                raise ValueError(f"template {frame_row['template_id']} brevo {live_sha[:12]} != approved "
                                 f"{frame_row['approved_sha256'][:12]}")
            out = fill_frame(frame or "", h)
            res["campaign"] = {"frame_order_ok": frame_order_ok(frame), "bytes": len(out.encode("utf-8")),
                               "sha256": hashlib.sha256(out.encode("utf-8")).hexdigest()[:12],
                               "gcs": put(CAMPAIGN_GCS.format(week=week_id), out)}
        except ValueError as e:
            res["campaign"] = f"HOLD FRAME: {e}"
    funnel = {}
    for r in query(AUDIENCE_SQL):
        funnel[r["gate"]] = funnel.get(r["gate"], 0) + 1
    res.update({"audience_in": funnel.get("IN", 0), "audience_funnel": funnel, "campaign_lists": CAMPAIGN_LISTS})
    return res


def _prod():
    import subprocess
    import tempfile
    from google.cloud import bigquery
    c = bigquery.Client(project=P)

    def q(s):
        return [dict(r) for r in c.query(s).result()]

    def put(path, text):
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
            f.write(text)
        subprocess.run(["gsutil", "-q", "cp", f.name, path], check=True)
        return path
    return q, put


def main(argv) -> int:
    if "--shadow" not in argv:
        print(__doc__)
        return 2
    args = dict(a.lstrip("-").split("=", 1) for a in argv if "=" in a)
    day = dt.date.fromisoformat(args["date"]) if args.get("date") else None
    gd = dt.date.fromisoformat(args["gate-date"]) if args.get("gate-date") else None
    q, put = _prod()
    import campaign as CAMP

    def frame_fn(tid):                                   # read-only Brevo GET of the 236 frame
        return (CAMP._call("GET", f"/smtp/templates/{int(tid)}", None) or {}).get("htmlContent") or ""
    print("AKCIJA_CAMPAIGN " + json.dumps(shadow(q, put, day, frame_fn=frame_fn, gate_date=gd), ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
