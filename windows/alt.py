"""Fetcher loop: pick which unnamed drafts this machine handles.

Selection priority: AQD_ID_SUFFIXES (the LAST DIGIT of the id, e.g. "1 3 5 7 9", also "1,3,5")
wins; when it is empty, fall back to AQD_PARITY (even|odd) exactly as before. So a machine can take
1,3,5,7,9 and another 2,4 without any even/odd meaning.
Queue file: queue_ids_<digits>.json for suffixes (digits ascending), queue_<parity>.json for parity.
Forever: list all unnamed drafts from the server -> keep mine -> ascending -> safe_run run (pre-check skips
places already named/taken, SAFE_DONE markers kept) -> when none left sleep 10 min and list again.
Stop: <root>/STOP_ALL (clean exit at a place boundary). Google limit -> honours fetch_backoff.txt like before.
Config by env or defaults: AQD_ROOT (dir of this file), AQD_ID_SUFFIXES, AQD_PARITY (even|odd)."""
import json, os, subprocess, sys, time
R = (os.environ.get("AQD_ROOT") or os.path.dirname(os.path.abspath(__file__))).replace("\\", "/")
PARITY = os.environ.get("AQD_PARITY") or ("even" if os.name == "nt" else "odd")
os.environ.setdefault("AQD_ROOT", R)


def fail(msg):
    try: open(R + "/alt.out", "a", encoding="utf-8").write(f"{time.strftime('%F %T')} ERROR {msg}\n")
    except OSError: pass
    sys.exit("aqd alt: " + msg)


def id_suffixes(raw):
    out = []
    for part in (raw or "").replace(",", " ").split():
        if len(part) != 1 or not part.isdigit(): fail(f"AQD_ID_SUFFIXES: {part!r} is not a single digit 0-9 (got {raw!r})")
        if int(part) not in out: out.append(int(part))
    return sorted(out)


SUFFIXES = id_suffixes(os.environ.get("AQD_ID_SUFFIXES"))
LABEL = ",".join(str(d) for d in SUFFIXES) if SUFFIXES else PARITY
if SUFFIXES:
    MINE = R + "/queue_ids_%s.json" % "".join(str(d) for d in SUFFIXES)
else:
    MINE = R + f"/queue_{PARITY}.json"
CREDS = R + "/creds.env"
if not os.path.exists(CREDS): fail(f"missing {CREDS} (run windows/install.ps1 first)")
for l in open(CREDS, encoding="utf-8"):
    l = l.strip()
    if l and not l.startswith("#") and "=" in l:
        k, v = l.split("=", 1); os.environ[k.strip()] = v.strip().strip('"').strip("'")
for k in ("PHOTOS_IDS", "PHOTOS_LIMIT", "AQD_MEET_STOP"): os.environ.pop(k, None)
os.environ.update(AQD_THROTTLE="2", AQD_UPLOAD_WORKERS="1", AQD_URL_BATCH_MAX="5", PYTHONIOENCODING="utf-8",
                  PYTHONUTF8="1", AQD_ALT="1", AQD_CDP="1", AQD_CLIP="1")
if sys.platform == "darwin": os.environ.setdefault("AQD_CLIP_DEVICE", "mps")
py = sys.executable; out = open(R + "/alt.out", "a", encoding="utf-8")
ALL = R + "/queue_all_unnamed.json"
IDLE = 600


def say(s):
    out.write(f"{time.strftime('%F %T')} {s}\n"); out.flush()


def stopall(): return os.path.exists(R + "/STOP_ALL")


def until(f): return float(open(f, encoding="utf-8").read()) if os.path.exists(f) else 0


def nap(sec):
    end = time.time() + sec
    while time.time() < end and not stopall(): time.sleep(10)


def run(args):
    if os.path.exists(R + "/STOP"): os.remove(R + "/STOP")
    pr = subprocess.Popen([py, R + "/safe_run.py"] + args, stdout=out, stderr=out, cwd=R)
    while pr.poll() is None:
        if stopall(): open(R + "/STOP", "w", encoding="utf-8").close()
        time.sleep(5)
    if os.path.exists(R + "/STOP"): os.remove(R + "/STOP")
    return pr.returncode


if SUFFIXES:
    keep = lambda r: r["id"] % 10 in SUFFIXES
    say(f"id suffixes: {LABEL} -> {os.path.basename(MINE)}")
else:
    keep = lambda r: r["id"] % 2 == (0 if PARITY == "even" else 1)
    say(f"parity loop start: {PARITY} ids, ascending")
while not stopall():
    if time.time() < until(R + "/fetch_backoff.txt"):
        say(f"fetch backoff; sleep {int(until(R + '/fetch_backoff.txt') - time.time())}s"); nap(until(R + "/fetch_backoff.txt") - time.time()); continue
    rc = run(["list", "--floor", "0", "--ceil", "999999999", "--queue", ALL])
    if rc != 0 or not os.path.exists(ALL):
        say(f"list failed rc={rc}; retry in 5 min"); nap(300); continue
    rows = [r for r in json.load(open(ALL, encoding="utf-8")) if keep(r)]
    rows.sort(key=lambda r: r["id"])
    rows = [r for r in rows if not os.path.exists(f"{R}/aqd_data/{r['id']}/SAFE_DONE")]
    json.dump(rows, open(MINE, "w", encoding="utf-8"), ensure_ascii=False)
    if not rows:
        say(f"no unnamed {LABEL} drafts left; sleep {IDLE//60} min"); nap(IDLE); continue
    say(f"FETCH {len(rows)} {LABEL} drafts #{rows[0]['id']}..#{rows[-1]['id']}")
    rc = run(["run", "--ignore-peak", "--sleep", "0", "--floor", "0", "--queue", MINE])
    if stopall(): break
    if rc == 3: say("Google limited -> backoff (kept)")
    elif rc != 0: say(f"fetcher rc={rc}; retry in 60s"); nap(60)
    else: say("pass done; re-list")
say("fetcher loop stopped (STOP_ALL)")
