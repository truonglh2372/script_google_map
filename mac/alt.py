"""Fetcher loop: pick which unnamed drafts this machine handles.

Selection priority: AQD_ID_SUFFIXES (the LAST DIGIT of the id, e.g. "1 3 5 7 9", also "1,3,5")
wins; when it is empty, fall back to AQD_PARITY (even|odd) exactly as before. So a machine can take
1,3,5,7,9 and another 2,4 without any even/odd meaning.
Queue file: queue_ids_<digits>.json for suffixes (digits ascending), queue_<parity>.json for parity.
Forever: list one page window of the unnamed drafts (AQD_LIST_MAX_PAGES pages per round, newest first, the next
round continues at the following pages and wraps to the top when the list ends) -> keep mine -> ascending ->
safe_run run (pre-check skips places already named/taken, SAFE_DONE markers kept, AQD_FETCH_LIMIT places per
round) -> when none left sleep 10 min and list again.
Stop: <root>/STOP_ALL (clean exit at a place boundary). Google limit -> honours fetch_backoff.txt like before.
Config by env or defaults: AQD_ROOT (dir of this file), AQD_ID_SUFFIXES, AQD_PARITY (even|odd),
AQD_LIST_MAX_PAGES (default 20; 0 or empty lists the whole list before the first fetch, the old behaviour),
AQD_FETCH_LIMIT (default 0 = every listed draft this round)."""
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
STATE = MINE + ".state.json"
SUFFIX_ARG = ["--id-suffix", ",".join(str(d) for d in SUFFIXES)] if SUFFIXES else []


def list_max_pages(raw):
    """AQD_LIST_MAX_PAGES: unset -> 20 pages per round; empty or 0 -> 0 = list the whole list first."""
    if raw is None: return 20
    raw = str(raw).strip()
    if not raw: return 0
    if not raw.isdigit(): fail(f"AQD_LIST_MAX_PAGES: {raw!r} is not a whole number of pages (0 or empty = all)")
    return int(raw)


def fetch_limit(raw):
    """AQD_FETCH_LIMIT: unset/empty/0 -> 0 = no per-round limit on how many places are fetched."""
    if raw is None: return 0
    raw = str(raw).strip()
    if not raw: return 0
    if not raw.isdigit(): fail(f"AQD_FETCH_LIMIT: {raw!r} is not a whole number of places (0 or empty = all)")
    return int(raw)


def page_window(state, max_pages):
    """(first_page, last_page|None) for one listing round."""
    first = max(1, int((state or {}).get("start_page") or 1))
    return (first, first + max_pages - 1) if max_pages else (first, None)


def advance(state, meta):
    """Next listing state. The server lists newest first, so a round that hit the page cap continues at the
    following pages, while a round that reached the end of the list starts the next one again from the top."""
    pages = int((meta or {}).get("pages") or 0)
    if not (meta or {}).get("capped") or pages <= 0: return {"start_page": 1}
    return {"start_page": max(1, int((state or {}).get("start_page") or 1)) + pages}


def plan_round(rows, state, meta, limit):
    """(queue, next_state, left) for one round: at most `limit` places, and the same page window is listed
    again until every id it holds has been tried once, so a small limit never starves the rest of a window."""
    tried = set((state or {}).get("tried") or [])
    fresh = [r for r in rows if r["id"] not in tried]
    queue = fresh[:limit] if limit else fresh
    left = len(fresh) - len(queue)
    if queue and left:
        return queue, {"start_page": max(1, int((state or {}).get("start_page") or 1)),
                       "tried": sorted(tried | {r["id"] for r in queue})}, left
    return queue, advance(state, meta), left


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f) or default
    except (OSError, ValueError): return default


def write_json(path, data):
    with open(path + ".tmp", "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)

CREDS = R + "/creds.env"
if not os.path.exists(CREDS): fail(f"missing {CREDS} (run mac/install_mac.sh first)")
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
MAX_PAGES = list_max_pages(os.environ.get("AQD_LIST_MAX_PAGES"))
FETCH_LIMIT = fetch_limit(os.environ.get("AQD_FETCH_LIMIT"))
say(f"config: queue={os.path.basename(MINE)} list_max_pages={MAX_PAGES or 'all'} fetch_limit={FETCH_LIMIT or 'none'}")
while not stopall():
    if time.time() < until(R + "/fetch_backoff.txt"):
        say(f"fetch backoff; sleep {int(until(R + '/fetch_backoff.txt') - time.time())}s"); nap(until(R + "/fetch_backoff.txt") - time.time()); continue
    state = read_json(STATE, {"start_page": 1})
    first, last = page_window(state, MAX_PAGES)
    span = f"pages {first}..{last}" if last else "pages from %d to the end of the list" % first
    say(f"listing {span} of unnamed {LABEL} drafts ({MAX_PAGES or 'all'} pages this round"
        + (f", ~{MAX_PAGES * 3 // 60} min of listing" if MAX_PAGES else "") + ") - fetching starts right after")
    rc = run(["list", "--floor", "0", "--ceil", "999999999", "--queue", ALL, "--start-page", str(first),
              "--max-pages", str(MAX_PAGES), "--per-page", "50", "--unnamed"] + SUFFIX_ARG)
    if rc != 0 or not os.path.exists(ALL):
        say(f"list failed rc={rc}; retry in 5 min"); nap(300); continue
    meta = read_json(ALL + ".meta.json", {})
    rows = [r for r in json.load(open(ALL, encoding="utf-8")) if keep(r)]
    rows.sort(key=lambda r: r["id"])
    rows = [r for r in rows if not os.path.exists(f"{R}/aqd_data/{r['id']}/SAFE_DONE")]
    tried = set(state.get("tried") or [])
    fresh = [r for r in rows if r["id"] not in tried]
    tf = R + "/parity_tries.json"; tries = json.load(open(tf, encoding="utf-8")) if os.path.exists(tf) else {}
    skipped = [r["id"] for r in fresh if tries.get(str(r["id"]), 0) >= 3]
    queue, next_state, left = plan_round([r for r in fresh if tries.get(str(r["id"]), 0) < 3], state, meta, FETCH_LIMIT)
    for r in queue: tries[str(r["id"])] = tries.get(str(r["id"]), 0) + 1
    json.dump(tries, open(tf, "w", encoding="utf-8"))
    if skipped: say(f"{len(skipped)} {LABEL} ids skipped after 3 failed passes (parity_tries.json)")
    json.dump(queue, open(MINE, "w", encoding="utf-8"), ensure_ascii=False)
    if left:
        say(f"{left} of {left + len(queue)} {LABEL} drafts in this window left for the next round (fetch limit {FETCH_LIMIT})")
    write_json(STATE, next_state)
    if not queue:
        if int(next_state.get("start_page") or 1) <= 1:
            say(f"no unnamed {LABEL} drafts left in pages {first}..{last or 'end'}; sleep {IDLE//60} min"); nap(IDLE)
        continue
    say(f"FETCH {len(queue)} {LABEL} drafts #{queue[0]['id']}..#{queue[-1]['id']}")
    rc = run(["run", "--ignore-peak", "--sleep", "0", "--floor", "0", "--queue", MINE, "--limit", str(FETCH_LIMIT)])
    if stopall(): break
    if rc == 3: say("Google limited -> backoff (kept)")
    elif rc != 0: say(f"fetcher rc={rc}; retry in 60s"); nap(60)
    else: say("pass done; re-list")
say("fetcher loop stopped (STOP_ALL)")
