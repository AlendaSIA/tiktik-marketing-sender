"""INTERFACE BLOCKS v1 (Raivis D31, MAIN 11.10 17:05) + AK-H1 - the engine's gates on rendered HTML blocks.

  v1.1 (MAIN 17:20/17:30, file sha 89a9dd0cee70): TWO fields per family - AK_HTML + AK2_HTML, FLOW_HTML + FLOW2_HTML.
  1. A letter whose template reads a family ({{ contact.AK_HTML|safe }} / {{ contact.FLOW_HTML|safe }}, and/or the
     2nd fields) is NOT sent to a contact whose FIRST field is "" (or missing): member reason `block_empty <FIELD>`
     (send_outcome). The 2nd field may be "" - that is normal.
  2. Such a letter goes only when today's push of that family is logged LIVE / OK / 0 errors in
     mkt_control.block_push_log (writer: Nakts sinhronizācija; columns of AK-H1 + `block`). No table / no row = HOLD.
  3. 236 (family AK_HTML) - AK-H1 on block_push_log ONLY (MAIN 17:30: ak_push_log is out of the gate): a row with
     block = 'AK_HTML', run_date = send date, week_id = that week, mode LIVE, status OK, errors 0 and
     assembled_built_at = INFORMATION_SCHEMA.PARTITIONS.last_modified_time of that date's akcija_assembled partition.
  v1.2 (MAIN 17:45, file sha c7de2af1d099): 236's shared maker goods are NOT in contact fields - the writer renders them
     once a week into mkt_control.ak_maker_html (week_id, html, html_sha256, built_at); the engine puts that html at the
     frame marker <!--MAKER_BLOCKS--> when it builds the 236 campaign. AK_HTML / AK2_HTML = the contact's own goods of
     that maker and MAY be "" -> no block_empty check for the AK family. 236 gate = block_push_log (AK_HTML)
     LIVE/OK/0 + assembled_built_at as built (AK-H1) + an ak_maker_html row for the week with a non-empty html_sha256.
     FLOW unchanged (v1.1).
  MAIN 18:50 (timing): the writer tiktik-ak-writer pushes LIVE daily 09:00 Riga (~09:05 on a maker-change day) and
     may log several rows per block per day (ERROR then OK). So: the gate takes the LATEST block_push_log row of that
     block for the day, it is checked at SEND time (not 08:55), and a block letter waits (re-check every 5 min) until
     10:30 Riga, then HOLD + reason. Letters without blocks never wait. 236's 3 sends (09:00 / 09:15 / 09:30, smallest
     list first) keep their 15 min gaps; only the first may slide to the OK row.
  v1.3 (MAIN 19:00, file sha 2d9dc460f678): FLOW family grows to FLOW_HTML(+FLOW2) / FLOWS_HTML(+FLOWS2) / FLOWR_HTML.
     Per letter the engine keeps a config of which block fields it reads and which of them are PRODUCT fields; a letter
     does not go to a contact when ALL its product fields are "" (reason block_empty ...). The push gate covers every
     group the letter reads (latest row per day, same 5 min / 10:30 wait). Source of the config = _FLOW-GROUP-RULES.md
     (7b); it becomes active only when MAIN hands its sha (FLOW_RULES_APPROVED_SHA). Until then: the template's own
     references decide the push gate and only FLOW_HTML is checked for "" (v1.2 behaviour).
Read-only. Nothing here writes anything.
"""
from __future__ import annotations

import datetime as dt
import re
import zoneinfo

P = "jaunais-za-aizv04022026"
M = f"{P}.mkt_control"
T_BLOCK_LOG = f"{M}.block_push_log"
T_MAKER = f"{M}.ak_maker_html"
T_MIX = f"{M}.week_price_mix16"                             # MAIN 19:00: the ONLY source of a MIX week's 236 goods
BLOCKS = ("AK_HTML", "FLOW_HTML", "FLOWS_HTML", "FLOWR_HTML")  # group = its FIRST field = the block_push_log `block`
FAMILY = {"AK_HTML": "AK_HTML", "AK2_HTML": "AK_HTML", "FLOW_HTML": "FLOW_HTML", "FLOW2_HTML": "FLOW_HTML",
          "FLOWS_HTML": "FLOWS_HTML", "FLOWS2_HTML": "FLOWS_HTML", "FLOWR_HTML": "FLOWR_HTML"}
RIGA = zoneinfo.ZoneInfo("Europe/Riga")
WAIT_UNTIL = dt.time(10, 30)                                # MAIN 18:50: wait for the block push at most until 10:30
POLL_S = 300                                                # re-check every 5 min
SEND_START = dt.time(9, 0)                                  # 236 first send; also the DRY "would start" base
GAP_236 = dt.timedelta(minutes=15)
EMPTY_CHECKED = ("FLOW_HTML",)                              # v1.2: AK_HTML may be "" (own goods only)
_REF = re.compile(r"\{\{\s*contact\.(AK2?_HTML|FLOW2?_HTML|FLOWS2?_HTML|FLOWR_HTML)\b")
# ---- v1.3 per-letter config, transcribed from _FLOW-GROUP-RULES.md (7b, read 11.10 19:15, sha b34fcb1d56f8).
# reads = groups the letter's template reads; product = groups that count for "all product fields empty" (7b note:
# the letter needs FLOW_HTML, the ROWS letters FLOW_HTML or FLOWS_HTML; FLOWR = companion goods, not counted).
FLOW_RULES_SOURCE_SHA = "b34fcb1d56f8"
FLOW_RULES_APPROVED_SHA = "b34fcb1d56f8"                     # MAIN 2 12.10 16:15: approved -> config ACTIVE
# MAIN 2 12.10 16:15 item 2: "Nakts sinhronizācija 11" writes all 5 FLOW* fields inside the FLOW_HTML block push - the
# FLOW_HTML push-log row covers FLOWS_HTML / FLOWR_HTML too (no separate rows are written or awaited).
PUSH_ROW = {"AK_HTML": "AK_HTML", "FLOW_HTML": "FLOW_HTML", "FLOWS_HTML": "FLOW_HTML", "FLOWR_HTML": "FLOW_HTML"}
_GRID = {"reads": ("FLOW_HTML", "FLOWR_HTML"), "product": ("FLOW_HTML",)}
_ROWS = {"reads": ("FLOW_HTML", "FLOWS_HTML", "FLOWR_HTML"), "product": ("FLOW_HTML", "FLOWS_HTML")}
FLOW_RULES = {179: _GRID, 180: _GRID, 255: _GRID, 229: _GRID, 230: _GRID, 235: _GRID,
              231: {"reads": ("FLOW_HTML",), "product": ("FLOW_HTML",)},
              232: _ROWS, 256: _ROWS, 234: _ROWS, 233: _ROWS, 257: _ROWS}


def rules_active() -> bool:
    return FLOW_RULES_APPROVED_SHA is not None and FLOW_RULES_APPROVED_SHA == FLOW_RULES_SOURCE_SHA


def letter_config(template_id, html: str) -> dict:
    """-> {"reads": [groups to push-gate], "product": [groups for the all-empty rule], "source", "mismatch"}.
    Active config: the rules; the template must read exactly those groups, else mismatch (the type is held).
    Inactive: the template's own references; product = FLOW_HTML only (v1.2)."""
    found = fields_in(html)
    rule = FLOW_RULES.get(int(template_id)) if template_id and rules_active() else None
    if rule is None:
        return {"reads": found, "product": [b for b in found if b in EMPTY_CHECKED], "source": "template",
                "mismatch": None}
    want = [b for b in BLOCKS if b in rule["reads"]]
    flows = [b for b in found if b != "AK_HTML"]
    mm = None if flows == want else f"CONFIG_MISMATCH {template_id}: template reads {flows}, rules say {want}"
    return {"reads": want, "product": list(rule["product"]), "source": f"rules {FLOW_RULES_SOURCE_SHA}",
            "mismatch": mm}


def fields_in(html: str) -> list:
    """The block FAMILIES a template reads (by their first field), in a fixed order. A template that reads only a
    2nd field still needs its family's first field and push row."""
    found = {FAMILY[f] for f in _REF.findall(html or "")}
    return [b for b in BLOCKS if b in found]


def all_empty(attrs: dict, product) -> list:
    """v1.3: the product groups when ALL of them are "" for the contact (-> no letter), else []."""
    product = list(product or [])
    empty = [f for f in product if not str((attrs or {}).get(f) or "").strip()]
    return empty if product and len(empty) == len(product) else []


def empty_blocks(attrs: dict, fields) -> list:
    """FIRST fields that are "" / missing for the contact -> the letter is not sent to them (2nd fields not checked).
    v1.2: only the FLOW family is checked; AK_HTML may legitimately be ""."""
    return [f for f in fields if f in EMPTY_CHECKED and not str((attrs or {}).get(f) or "").strip()]


def _exists(q, table) -> bool:
    name = table.split(".")[-1]
    return bool(q(f"SELECT 1 AS x FROM `{M}.INFORMATION_SCHEMA.TABLES` WHERE table_name = '{name}'"))


def latest_push(q, send_date: str, block: str) -> dict | None:
    """The LATEST block_push_log row of `block` for the day (several per day are possible: ERROR then OK)."""
    r = q(f"SELECT mode, status, errors, week_id, finished_at, assembled_built_at FROM `{T_BLOCK_LOG}` "
          f"WHERE run_date = DATE '{send_date}' AND block = '{block}' ORDER BY finished_at DESC LIMIT 1")
    return dict(r[0]) if r else None


def row_ok(r) -> bool:
    return bool(r) and r.get("mode") == "LIVE" and r.get("status") == "OK" and r.get("errors") is not None \
        and int(r["errors"]) == 0


def push_ok(q, send_date: str, block: str) -> str | None:
    """None when the LATEST push row of `block` today is LIVE / OK / 0 errors, else why it is held."""
    if not _exists(q, T_BLOCK_LOG):
        return f"HOLD {block}: {T_BLOCK_LOG} does not exist yet"
    r = latest_push(q, send_date, PUSH_ROW.get(block, block))
    if not r:
        return f"HOLD {block}: no push row for {send_date}"
    if not row_ok(r):
        return (f"HOLD {block}: latest push row for {send_date} is {r.get('mode')}/{r.get('status')}/"
                f"{r.get('errors')} errors at {r.get('finished_at')}")
    return None


def wait_for(check, now_fn, sleep, until: dt.datetime, poll_s: int = POLL_S):
    """Re-run check() every poll_s until it returns None or `until` passes. -> (released_at | None, last reason).
    check() -> None (OK) | reason. now_fn() -> aware datetime. sleep(seconds)."""
    while True:
        why = check()
        t = now_fn()
        if why is None:
            return t, None
        if t + dt.timedelta(seconds=poll_s) > until:
            return None, f"{why} - waited until {until.astimezone(RIGA):%H:%M} Riga"
        sleep(poll_s)


def deadline(send_date) -> dt.datetime:
    d = dt.date.fromisoformat(str(send_date))
    return dt.datetime.combine(d, WAIT_UNTIL, tzinfo=RIGA)


def would_start(send_date, ok_at, start: dt.time = SEND_START, poll_s: int = POLL_S):
    """DRY: when the send would have started - the planned start, or the first 5-min re-check at/after the OK row;
    None when the OK row came after the 10:30 deadline."""
    d = dt.date.fromisoformat(str(send_date))
    t0 = dt.datetime.combine(d, start, tzinfo=RIGA)
    if ok_at is None:
        return None
    ok_at = _aware(ok_at)
    if ok_at <= t0:
        return t0
    n = -(-int((ok_at - t0).total_seconds()) // poll_s)
    t = t0 + dt.timedelta(seconds=n * poll_s)
    return t if t <= deadline(send_date) else None


def sends_236(send_date, ok_at, n: int = 3):
    """236's 3 sends: 09:00 / 09:15 / 09:30, the first may slide to the OK row, gaps stay 15 min."""
    first = would_start(send_date, ok_at)
    return [] if first is None else [first + i * GAP_236 for i in range(n)]


def _aware(t):
    if isinstance(t, str):
        t = dt.datetime.fromisoformat(t.replace(" UTC", "+00:00").replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def assembled_built_at_sql(send_date: str) -> str:
    return (f"SELECT last_modified_time AS t FROM `{M}.INFORMATION_SCHEMA.PARTITIONS` "
            f"WHERE table_name = 'akcija_assembled' AND partition_id = '{send_date.replace('-', '')}'")


def ak_h1(q, send_date: str, week_id: str) -> str | None:
    """236 gate (v1.2): AK-H1 on block_push_log only + the week's ak_maker_html row. None = may go; else why it is held."""
    built = q(assembled_built_at_sql(send_date))
    if not built or built[0].get("t") is None:
        return f"HOLD AK-H1: no akcija_assembled partition for {send_date}"
    if not _exists(q, T_BLOCK_LOG):
        return f"HOLD AK-H1: {T_BLOCK_LOG} does not exist"
    r = q(f"SELECT mode, status, errors, week_id, finished_at, TIMESTAMP_TRUNC(assembled_built_at, MILLISECOND) = "
          f"TIMESTAMP_TRUNC(({assembled_built_at_sql(send_date)}), MILLISECOND) AS built_match "
          f"FROM `{T_BLOCK_LOG}` WHERE run_date = DATE '{send_date}' AND block = 'AK_HTML' "
          f"ORDER BY finished_at DESC LIMIT 1")
    r = dict(r[0]) if r else None
    if not (row_ok(r) and r.get("week_id") == week_id and r.get("built_match") is True):
        what = "no row" if not r else (f"latest {r.get('mode')}/{r.get('status')}/{r.get('errors')} errors, week "
                                       f"{r.get('week_id')}, built_match {r.get('built_match')}")
        return f"HOLD AK-H1: no LIVE/OK/0-error push row matching the {send_date} assembly ({week_id}): {what}"
    return maker_html(q, week_id)[1]                       # v1.2: + the week's shared maker html


def maker_html(q, week_id: str):
    """v1.2: the week's shared maker html. -> (row, None) or (None, "HOLD ...")."""
    if not _exists(q, T_MAKER):
        return None, f"HOLD MAKER: {T_MAKER} does not exist"
    rows = q(f"SELECT week_id, html, html_sha256, built_at FROM `{T_MAKER}` WHERE week_id = '{week_id}' "
             f"ORDER BY built_at DESC LIMIT 1")
    if not rows or not str(rows[0].get("html_sha256") or "").strip() or not str(rows[0].get("html") or "").strip():
        return None, f"HOLD MAKER: no ak_maker_html row with html + html_sha256 for {week_id}"
    return rows[0], None


def mix_rows(q, week_id: str):
    """MAIN 19:00 (WA7d): a MIX week's goods = mkt_control.week_price_mix16 rows of that week_id, nothing computed here.
    -> (rows, None) or ([], "HOLD MIX ...")."""
    if not _exists(q, T_MIX):
        return [], f"HOLD MIX: {T_MIX} does not exist"
    rows = q(f"SELECT product_handle, category, rank, units_30d FROM `{T_MIX}` WHERE week_id = '{week_id}' "
             f"ORDER BY category, rank")
    return (rows, None) if rows else ([], f"HOLD MIX: no week_price_mix16 rows for {week_id}")
