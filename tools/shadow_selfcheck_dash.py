#!/usr/bin/env python3
"""ONE dash row per day, ONLY when the shadow plan's self-check failed (Sūtīšanas dzinējs 4, MAIN 2026-10-05 16:10).

Runs after the daily shadow plan (08:05 Europe/Riga) as Cloud Run job tiktik-shadow-selfcheck-dash on the ops
service account (the planner's own account has no Drive access). Reads mkt_control.shadow_run_report and
mkt_control.shadow_selfcheck of today:
  - no run report row today            -> KĻŪDA "ēnas plāns šodien nav izpildīts"
  - any level='hard' check with ok=false -> KĻŪDA + the check names and values
  - otherwise                          -> nothing (no row, no noise)
Never twice a day: the posted row is remembered in mkt_control.shadow_selfcheck (check_name '_dash_row_posted').
It sends nothing to anyone and touches nothing but that marker row and the dash file. DRY=1 prints the row only.
"""
import datetime as dt
import json
import os
import subprocess
import sys
import zoneinfo

P = "jaunais-za-aizv04022026"
DASH = "Company-Alenda-SIA/shared-platforms/_agent-sessions.md"
AGENT = "tiktik.lv › Marketing · Sūtīšanas dzinējs (ēnas plāna paškontrole)"


def bq(sql):
    out = subprocess.check_output(["bq", "--project_id", P, "query", "--nouse_legacy_sql", "--format=json",
                                   "--max_rows=200", sql], stderr=subprocess.DEVNULL).decode()
    return json.loads(out) if out.strip().startswith("[") else []


def main():
    runs = bq(f"SELECT run_id FROM `{P}.mkt_control.shadow_run_report` WHERE plan_date = CURRENT_DATE() "
              f"ORDER BY finished_at DESC LIMIT 1")
    if bq(f"SELECT 1 FROM `{P}.mkt_control.shadow_selfcheck` WHERE plan_date = CURRENT_DATE() "
          f"AND check_name = '_dash_row_posted' LIMIT 1"):
        print("already posted today"); return 0
    if not runs:
        what, run_id = "Ēnas plāns šodien nav izpildīts (nav shadow_run_report rindas) — nekas nav sūtīts", "none"
    else:
        run_id = runs[0]["run_id"]
        bad = bq(f"SELECT check_name, value FROM `{P}.mkt_control.shadow_selfcheck` WHERE plan_date = CURRENT_DATE() "
                 f"AND run_id = '{run_id}' AND level = 'hard' AND NOT ok ORDER BY check_name")
        n = bq(f"SELECT COUNT(*) AS n FROM `{P}.mkt_control.shadow_selfcheck` WHERE plan_date = CURRENT_DATE() "
               f"AND run_id = '{run_id}'")
        if not int(n[0]["n"]):
            what = "Ēnas plānam šodien nav paškontroles rindu — nekas nav sūtīts"
        elif bad:
            what = (f"Ēnas plāna paškontrole: {len(bad)} neizturētas — "
                    + "; ".join(f"{b['check_name']}={b['value']}" for b in bad)[:300]
                    + " — nekas nav sūtīts; detaļas mkt_control.shadow_selfcheck")
        else:
            print("self-check clean - no dash row"); return 0
    now = dt.datetime.now(zoneinfo.ZoneInfo("Europe/Riga")).strftime("%Y-%m-%d %H:%M")
    row = f"| {now} | {AGENT} | KĻŪDA | {what.replace('|', '/')} | nav zināma |\n"
    if os.environ.get("DRY") == "1":
        print("DRY " + row); return 0
    r = subprocess.run([sys.executable, "/root/iso_drive.py", "append", DASH], input=row.encode(),
                       capture_output=True)
    out = r.stdout.decode() + r.stderr.decode()
    print(out[:400])
    if "VERIFIED ok=True" not in out:
        return 1
    safe = what.replace("\\", " ").replace("'", " ").replace("\n", " ")
    bq(f"INSERT INTO `{P}.mkt_control.shadow_selfcheck` (plan_date, run_id, check_name, level, ok, value, detail, "
       f"checked_at) VALUES (CURRENT_DATE(), '{run_id}', '_dash_row_posted', 'info', TRUE, '1', '{safe}', "
       f"CURRENT_TIMESTAMP())")
    return 0


if __name__ == "__main__":
    sys.exit(main())
