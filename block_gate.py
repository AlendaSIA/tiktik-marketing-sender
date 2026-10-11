"""INTERFACE BLOCKS v1 (Raivis D31, MAIN 11.10 17:05) + AK-H1 - the engine's gates on rendered HTML blocks.

  1. A letter whose template reads a block field ({{ contact.AK_HTML|safe }} / {{ contact.FLOW_HTML|safe }}) is NOT
     sent to a contact whose field is "" (or missing): member reason `block_empty <FIELD>` (send_outcome).
  2. Such a letter goes only when today's push of that block is logged LIVE / OK / 0 errors in
     mkt_control.block_push_log (writer: Nakts sinhronizācija; columns of AK-H1 + `block`). No table / no row = HOLD.
  3. 236 (AK_HTML) keeps AK-H1: a row in mkt_control.ak_push_log OR block_push_log (block 'AK_HTML') with
     run_date = send date, week_id = that week, mode LIVE, status OK, errors 0 and assembled_built_at =
     INFORMATION_SCHEMA.PARTITIONS.last_modified_time of that date's akcija_assembled partition (AK-H1 amendment).
Read-only. Nothing here writes anything.
"""
from __future__ import annotations

import re

P = "jaunais-za-aizv04022026"
M = f"{P}.mkt_control"
T_BLOCK_LOG, T_AK_LOG = f"{M}.block_push_log", f"{M}.ak_push_log"
BLOCKS = ("AK_HTML", "FLOW_HTML")
_REF = re.compile(r"\{\{\s*contact\.(AK_HTML|FLOW_HTML)\b")


def fields_in(html: str) -> list:
    """The block fields a template reads, in a fixed order."""
    found = set(_REF.findall(html or ""))
    return [b for b in BLOCKS if b in found]


def empty_blocks(attrs: dict, fields) -> list:
    """Fields that are "" / missing for the contact -> the letter is not sent to them."""
    return [f for f in fields if not str((attrs or {}).get(f) or "").strip()]


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
    """AK-H1 (+ amendment) for 236. None = may go; else why it is held."""
    built = q(assembled_built_at_sql(send_date))
    if not built or built[0].get("t") is None:
        return f"HOLD AK-H1: no akcija_assembled partition for {send_date}"
    cond = (f"run_date = DATE '{send_date}' AND week_id = '{week_id}' AND mode = 'LIVE' AND status = 'OK' "
            f"AND IFNULL(errors, 1) = 0 AND TIMESTAMP_TRUNC(assembled_built_at, MILLISECOND) = "
            f"TIMESTAMP_TRUNC(({assembled_built_at_sql(send_date)}), MILLISECOND)")
    sources = []
    if _exists(q, T_AK_LOG):
        sources.append(f"SELECT COUNT(*) AS n FROM `{T_AK_LOG}` WHERE {cond}")
    if _exists(q, T_BLOCK_LOG):
        sources.append(f"SELECT COUNT(*) AS n FROM `{T_BLOCK_LOG}` WHERE block = 'AK_HTML' AND {cond}")
    if not sources:
        return "HOLD AK-H1: neither ak_push_log nor block_push_log exists"
    if any(int((q(s) or [{"n": 0}])[0]["n"]) > 0 for s in sources):
        return None
    return f"HOLD AK-H1: no LIVE/OK/0-error push row matching the {send_date} assembly ({week_id})"
