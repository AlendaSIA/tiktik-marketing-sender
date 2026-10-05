"""Proof for WRITER OUTPUT v1 (contract 7ca13f671703): for 5 real planned letters of today, what the engine reads
and would carry (sequence_job.LF_SQL -> send_path.letter_params) vs an independent read of the same contact's
mkt_control.letter_fields row - canonical JSON, sha256, byte comparison. Plus one in-memory absence: the same real
contact without a row today -> the gate. Read-only. Run in the ops shell: python3 tools/lf_proof.py"""
import hashlib
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import presend as G  # noqa: E402
import send_path as SP  # noqa: E402
import sequence_job as J  # noqa: E402

P = "jaunais-za-aizv04022026"


def bq(sql):
    out = subprocess.run(["bq", "--project_id", P, "query", "--nouse_legacy_sql", "--format=json", "--max_rows=100",
                          sql], capture_output=True, text=True, timeout=120)
    return json.loads(out.stdout[out.stdout.index("["):])


canon = lambda d: json.dumps(d, ensure_ascii=False, sort_keys=True)  # noqa: E731
plan = bq(f"""SELECT email_type, LOWER(TRIM(email)) email, master_key, run_id, CAST(offer_valid_until AS STRING) ovu,
  CAST(xsell_valid_until AS STRING) xvu, trigger_order_nr, presend_gates, would_deliver, template_id
FROM `{P}.mkt_control.shadow_send_plan` WHERE plan_date = CURRENT_DATE('Europe/Riga') AND would_send
QUALIFY ROW_NUMBER() OVER (PARTITION BY email_type ORDER BY email) = 1 ORDER BY email_type""")
emails = ", ".join("'%s'" % r["email"] for r in plan)
engine = {r["email"]: r for r in bq(f"SELECT * FROM ({J.LF_SQL}) WHERE email IN ({emails})")}
cols = [k for k in next(iter(engine.values())) if k.isupper()]
table = {r["email"]: r for r in bq(f"SELECT email, plan_run_id, run_id AS lf_run, {', '.join(cols)} "
                                   f"FROM `{P}.mkt_control.letter_fields` "
                                   f"WHERE plan_date = CURRENT_DATE('Europe/Riga') AND email IN ({emails})")}
for r in plan:
    e, t = SP.letter_params(engine[r["email"]]), {k: table[r["email"]][k] for k in cols}
    a, b = canon(e).encode(), canon(t).encode()
    print(json.dumps({
        "email_type": r["email_type"], "template_id": r["template_id"], "master_key": r["master_key"],
        "fields": len(e), "bytes": len(a), "engine_sha": hashlib.sha256(a).hexdigest()[:12],
        "table_sha": hashlib.sha256(b).hexdigest()[:12], "byte_equal": a == b,
        "P1": [e["P1_NAME"], e["P1_PRICE"], e["P1_REF_PRICE"]], "R1": [e["R1_NAME"], e["R1_PRICE"], e["R1_REF_PRICE"]],
        "OFFER_VALID_UNTIL": e["OFFER_VALID_UNTIL"], "plan.offer_valid_until": r["ovu"],
        "XSELL_VALID_UNTIL": e["XSELL_VALID_UNTIL"], "plan.xsell_valid_until": r["xvu"],
        "ORDER_NR": e["ORDER_NR"], "plan.trigger_order_nr": r["trigger_order_nr"], "ANKETA_URL": e["ANKETA_URL"][:60],
        "plan_run": r["run_id"], "letter_fields.plan_run_id": table[r["email"]]["plan_run_id"],
        "plan.presend_gates": r["presend_gates"]}, ensure_ascii=False))
r = plan[0]
rows = {k: v for k, v in engine.items() if k != r["email"]}                     # the same real contact, row absent
w = J.letter_row(rows, r["email"], r["email_type"])
print("NO ROW TODAY:", r["master_key"], r["email_type"], "letter_row ->", w, "gates ->",
      G.gates(r["email_type"], 0, G.Ctx(template_id=int(r["template_id"]), template_approved=True,
                                        letter_fields=w is not None)))
