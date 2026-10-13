import json, subprocess, sys, datetime as dt
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import s5_card_gate as G
def q(sql):
    r = subprocess.run(["bq","query","--nouse_legacy_sql","--format=json","--max_rows=1000000",sql],capture_output=True,text=True)
    if r.returncode: raise SystemExit(r.stderr[-2000:]+r.stdout[-2000:])
    out = r.stdout.strip()
    return json.loads(out) if out else []
d = dt.date(2026,10,13)
aud = [r["email"] for r in q(sys.argv[1] if len(sys.argv) > 1 else "SELECT DISTINCT LOWER(TRIM(email)) email FROM mkt_control.send_log WHERE campaign_id IN (330, 332)")]
res = G.evaluate(q, d, aud)
print("audience", len(aud), "order_hold", res["order_hold"], "checked", res["checked"], "cards", res["cards"], "holds", len(res["holds"]), "stats", res["stats"])
print(json.dumps(res["facts"]))
import collections
for e,w in list(res["holds"].items())[:6]: print(e[:3]+"***", w[:300])
