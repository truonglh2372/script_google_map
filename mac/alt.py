"""Parity fetcher loop (2026-10-14): Admin = EVEN ids, Mac = ODD ids.
Forever: list all unnamed drafts from the server -> keep my parity -> ascending -> safe_run run (pre-check skips
places already named/taken, SAFE_DONE markers kept) -> when none left sleep 10 min and list again.
Stop: <root>/STOP_ALL (clean exit at a place boundary). Google limit -> honours fetch_backoff.txt like before.
Config by env or defaults: AQD_ROOT (dir of this file), AQD_PARITY (even|odd)."""
import json, os, subprocess, sys, time
R = os.environ.get("AQD_ROOT") or os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
PARITY = os.environ.get("AQD_PARITY") or ("even" if os.name == "nt" else "odd")
for l in open(R + "/creds.env", encoding="utf-8"):
    l = l.strip()
    if l and not l.startswith("#") and "=" in l:
        k, v = l.split("=", 1); os.environ[k.strip()] = v.strip().strip('"').strip("'")
for k in ("PHOTOS_IDS", "PHOTOS_LIMIT", "AQD_MEET_STOP"): os.environ.pop(k, None)
os.environ.update(AQD_THROTTLE="2", AQD_UPLOAD_WORKERS="1", AQD_URL_BATCH_MAX="5", PYTHONIOENCODING="utf-8",
                  PYTHONUTF8="1", AQD_ALT="1", AQD_CDP="1", AQD_CLIP="1")
if sys.platform == "darwin": os.environ.setdefault("AQD_CLIP_DEVICE", "mps")
py = sys.executable; out = open(R + "/alt.out", "a", encoding="utf-8")
ALL, MINE = R + "/queue_all_unnamed.json", R + f"/queue_{PARITY}.json"
IDLE = 600


def say(s):
    out.write(f"{time.strftime('%F %T')} {s}\n"); out.flush()


def stopall(): return os.path.exists(R + "/STOP_ALL")


def until(f): return float(open(f).read()) if os.path.exists(f) else 0


def nap(sec):
    end = time.time() + sec
    while time.time() < end and not stopall(): time.sleep(10)


def run(args):
    if os.path.exists(R + "/STOP"): os.remove(R + "/STOP")
    pr = subprocess.Popen([py, R + "/safe_run.py"] + args, stdout=out, stderr=out, cwd=R)
    while pr.poll() is None:
        if stopall(): open(R + "/STOP", "w").close()
        time.sleep(5)
    if os.path.exists(R + "/STOP"): os.remove(R + "/STOP")
    return pr.returncode


say(f"parity loop start: {PARITY} ids, ascending")
while not stopall():
    if time.time() < until(R + "/fetch_backoff.txt"):
        say(f"fetch backoff; sleep {int(until(R + '/fetch_backoff.txt') - time.time())}s"); nap(until(R + "/fetch_backoff.txt") - time.time()); continue
    rc = run(["list", "--floor", "0", "--ceil", "999999999", "--queue", ALL])
    if rc != 0 or not os.path.exists(ALL):
        say(f"list failed rc={rc}; retry in 5 min"); nap(300); continue
    rows = [r for r in json.load(open(ALL, encoding="utf-8")) if r["id"] % 2 == (0 if PARITY == "even" else 1)]
    rows.sort(key=lambda r: r["id"])
    rows = [r for r in rows if not os.path.exists(f"{R}/aqd_data/{r['id']}/SAFE_DONE")]
    tf = R + "/parity_tries.json"; tries = json.load(open(tf)) if os.path.exists(tf) else {}
    skipped = [r["id"] for r in rows if tries.get(str(r["id"]), 0) >= 3]
    rows = [r for r in rows if tries.get(str(r["id"]), 0) < 3]          # failed 3 passes -> leave for manual check
    for r in rows: tries[str(r["id"])] = tries.get(str(r["id"]), 0) + 1
    json.dump(tries, open(tf, "w"))
    if skipped: say(f"{len(skipped)} {PARITY} ids skipped after 3 failed passes (parity_tries.json)")
    json.dump(rows, open(MINE, "w", encoding="utf-8"), ensure_ascii=False)
    if not rows:
        say(f"no unnamed {PARITY} drafts left; sleep {IDLE//60} min"); nap(IDLE); continue
    say(f"FETCH {len(rows)} {PARITY} drafts #{rows[0]['id']}..#{rows[-1]['id']}")
    rc = run(["run", "--ignore-peak", "--sleep", "0", "--floor", "0", "--queue", MINE])
    if stopall(): break
    if rc == 3: say("Google limited -> backoff (kept)")
    elif rc != 0: say(f"fetcher rc={rc}; retry in 60s"); nap(60)
    else: say("pass done; re-list")
say("parity loop stopped (STOP_ALL)")
