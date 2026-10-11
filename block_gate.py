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
Read-only. Nothing here writes anything.
"""
from __future__ import annotations

import re

P = "jaunais-za-aizv04022026"
M = f"{P}.mkt_control"
T_BLOCK_LOG = f"{M}.block_push_log"
T_MAKER = f"{M}.ak_maker_html"
BLOCKS = ("AK_HTML", "FLOW_HTML")                          # family = its FIRST field = the block_push_log `block`
FAMILY = {"AK_HTML": "AK_HTML", "AK2_HTML": "AK_HTML", "FLOW_HTML": "FLOW_HTML", "FLOW2_HTML": "FLOW_HTML"}
EMPTY_CHECKED = ("FLOW_HTML",)                              # v1.2: AK_HTML may be "" (own goods only)
_REF = re.compile(r"\{\{\s*contact\.(AK2?_HTML|FLOW2?_HTML)\b")


def fields_in(html: str) -> list:
    """The block FAMILIES a template reads (by their first field), in a fixed order. A template that reads only a
    2nd field still needs its family's first field and push row."""
    found = {FAMILY[f] for f in _REF.findall(html or "")}
    return [b for b in BLOCKS if b in found]


def empty_blocks(attrs: dict, fields) -> list:
    """FIRST fields that are "" / missing for the contact -> the letter is not sent to them (2nd fields not checked).
    v1.2: only the FLOW family is checked; AK_HTML may legitimately be ""."""
    return [f for f in fields if f in EMPTY_CHECKED and not str((attrs or {}).get(f) or "").strip()]


def _exists(q, table) -> bool:
    name = table.split(".")[-1]
    return bool(q(f"SELECT 1 AS x FROM `{M}.INFORMATION_SCHEMA.TABLES` WHERE table_name = '{name}'"))


def push_ok(q, send_date: str, block: str) -> str | None:
    """None when today's push of `block` is logged LIVE / OK / 0 errors, else why it is held."""
    if not _exists(q, T_BLOCK_LOG):
        return f"HOLD {block}: {T_BLOCK_LOG} does not exist yet"
    r = q(f"SELECT COUNT(*) AS n FROM `{T_BLOCK_LOG}` WHERE run_date = DATE '{send_date}' AND block = '{block}' "
          f"AND mode = 'LIVE' AND status = 'OK' AND IFNULL(errors, 1) = 0")
    return None if r and int(r[0]["n"]) > 0 else f"HOLD {block}: no LIVE/OK/0-error push row for {send_date}"


def assembled_built_at_sql(send_date: str) -> str:
    return (f"SELECT last_modified_time AS t FROM `{M}.INFORMATION_SCHEMA.PARTITIONS` "
            f"WHERE table_name = 'akcija_assembled' AND partition_id = '{send_date.replace('-', '')}'")


def ak_h1(q, send_date: str, week_id: str) -> str | None:
    """236 gate (v1.2): AK-H1 on block_push_log only + the week's ak_maker_html row. None = may go; else why it is held."""
    built = q(assembled_built_at_sql(send_date))
    if not built or built[0].get("t") is None:
        return f"HOLD AK-H1: no akcija_assembled partition for {send_date}"
    cond = (f"run_date = DATE '{send_date}' AND week_id = '{week_id}' AND mode = 'LIVE' AND status = 'OK' "
            f"AND IFNULL(errors, 1) = 0 AND TIMESTAMP_TRUNC(assembled_built_at, MILLISECOND) = "
            f"TIMESTAMP_TRUNC(({assembled_built_at_sql(send_date)}), MILLISECOND)")
    if not _exists(q, T_BLOCK_LOG):
        return f"HOLD AK-H1: {T_BLOCK_LOG} does not exist"
    r = q(f"SELECT COUNT(*) AS n FROM `{T_BLOCK_LOG}` WHERE block = 'AK_HTML' AND {cond}")
    if not (r and int(r[0]["n"]) > 0):
        return f"HOLD AK-H1: no LIVE/OK/0-error push row matching the {send_date} assembly ({week_id})"
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
