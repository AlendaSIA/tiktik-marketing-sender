"""Guarded runner for tiktik-draft-test (MAIN 2026-10-06 17:28, STEP 0).
The job is relocked whenever this runner exits, for any reason it can still act on: normal end, exception, SIGTERM,
SIGINT, SIGHUP (try/finally + signal handlers, armed BEFORE the wrapper is opened). For the exits it cannot act on
(SIGKILL, lost container) the OPEN wrapper carries its own expiry: after EXP_S seconds it refuses --send by itself.
The OPEN wrapper is not a stored file any more: it is the LOCKED wrapper with its refusal line replaced by the expiry."""
import datetime, hashlib, json, os, signal, subprocess, sys, time
B = "gs://jaunais-za-aizv04022026-vps-deploy/src-staging"; J = "tiktik-draft-test"; R = "europe-west1"
LOCKED_FILE = B + "/tiktik-draft-test-wrapper-locked-20261006f.py"; EXP_S = 300
REF = ('if "--send" in sys.argv:\n    sys.exit("REFUSED: --send is not allowed in this job until MAIN opens the D1+D2 seam")\n')
def sh(*a): return subprocess.run(a, capture_output=True, text=True)
def log(*a): print(datetime.datetime.utcnow().strftime("%H:%M:%S"), *a, flush=True)
def locked_code():
    c = sh("gsutil", "cat", LOCKED_FILE).stdout
    assert c.startswith("import sys") and REF in c, "locked wrapper not readable"
    return c
def open_code(exp_s):
    exp = int(time.time()) + exp_s; iso = datetime.datetime.utcfromtimestamp(exp).strftime("%Y-%m-%dT%H:%M:%SZ")
    return locked_code().replace(REF, 'import time\nif "--send" in sys.argv and time.time() > %d:\n    sys.exit("REFUSED: the OPEN wrapper expired at %s - relock the job")\n' % (exp, iso)), iso
def apply(code):
    j = json.loads(sh("gcloud", "run", "jobs", "describe", J, "--region", R, "--format=json").stdout)
    j["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["command"] = ["python", "-c", code]
    j.pop("status", None)
    for k in ("generation", "resourceVersion", "uid", "creationTimestamp", "selfLink"): j["metadata"].pop(k, None)
    f = "/tmp/job-%d.json" % os.getpid(); open(f, "w").write(json.dumps(j))
    return sh("gcloud", "run", "jobs", "replace", f, "--region", R).returncode
def state():
    j = json.loads(sh("gcloud", "run", "jobs", "describe", J, "--region", R, "--format=json").stdout)
    c = j["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["command"][2]
    st = {"gen": j["metadata"]["generation"], "wrapper_sha": hashlib.sha256(c.encode()).hexdigest()[:12],
          "LOCKED": REF in c, "expiry_line": "the OPEN wrapper expired" in c}
    log("STATE", json.dumps(st)); return st
def lock():
    for i in range(3):
        if apply(locked_code()) == 0 and state()["LOCKED"]: log("RELOCKED"); return True
        time.sleep(3)
    log("RELOCK FAILED"); return False
class Guard:
    def __init__(self, exp_s=EXP_S): self.exp_s = exp_s
    def __enter__(self):
        def stop(sig, frm): log("SIGNAL", sig); raise SystemExit(128 + sig)
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP): signal.signal(s, stop)
        log("ARMED pid", os.getpid())                      # the relock point is armed BEFORE the wrapper is opened
        code, iso = open_code(self.exp_s); rc = apply(code); log("OPEN rc", rc, "expires", iso); state(); return self
    def __exit__(self, *exc):
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP): signal.signal(s, signal.SIG_IGN)
        lock(); return False
def execute(args):
    r = sh("gcloud", "run", "jobs", "execute", J, "--region", R, "--args", ",".join(args), "--wait", "--format=value(metadata.name)")
    ex = (r.stdout.strip().splitlines() or [""])[-1]
    if not ex.startswith(J): ex = sh("gcloud", "run", "jobs", "executions", "list", "--job", J, "--region", R, "--limit", "1", "--format=value(metadata.name)").stdout.strip()
    log("EXEC", ex, "rc", r.returncode); return ex
if __name__ == "__main__":
    m = sys.argv[1]
    if m == "state": state()
    elif m == "lock": lock()
    elif m == "proof-term":
        with Guard(): log("HOLDING - no execution, no send"); time.sleep(600)
    elif m == "proof-expiry":                               # open with a short expiry, then die WITHOUT the finally
        code, iso = open_code(int(sys.argv[2])); log("OPEN-NO-GUARD rc", apply(code), "expires", iso); state(); os.kill(os.getpid(), 9)
    elif m == "exec": execute(json.loads(sys.argv[2]))      # no --send: runs under the LOCKED wrapper
    elif m == "send":
        with Guard(): execute(json.loads(sys.argv[2]))
