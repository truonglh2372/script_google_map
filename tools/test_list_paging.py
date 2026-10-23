"""Offline self-test for the partial listing (AQD_LIST_MAX_PAGES / AQD_FETCH_LIMIT) and for the
AQD_FETCH_LIMIT -> safe_run `run --limit` wiring. No network, no GB downloads.

Run: python tools/test_list_paging.py [repo-root]

It extracts the real helpers out of windows/alt.py and mac/alt.py (so the shipped code is what is tested),
simulates the listing loop against a fake server, and runs safe_run's real cmd_list/cmd_run against a
localhost mock of the admin API (the new paging params, and a server that ignores them).
"""
import argparse
import ast
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILURES = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        FAILURES.append(label)


def check_true(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}" + (f" ({detail})" if detail else ""))
    else:
        print(f"  FAIL {label}" + (f" ({detail})" if detail else ""))
        FAILURES.append(label)


# ---------------------------------------------------------------- alt.py helpers (real source)
WANTED = {"fail", "id_suffixes", "list_max_pages", "fetch_limit", "page_window", "advance", "plan_round"}


def load_helpers(rel):
    path = os.path.join(ROOT, rel)
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src, path)
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in WANTED]
    found = {n.name for n in funcs}
    if found != WANTED:
        raise AssertionError(f"{rel}: missing helpers {sorted(WANTED - found)}")
    ns = {"os": os, "sys": sys, "json": json, "time": __import__("time"),
          "R": tempfile.mkdtemp(prefix="aqd-alth-")}
    exec("\n\n".join(ast.get_source_segment(src, n) for n in funcs), ns)
    return src, {name: ns[name] for name in WANTED}


def simulate(hp, ids, pages_per_round, limit, page_size, taken=()):
    """Run the shipped page_window/plan_round/advance loop against a fake newest-first unnamed list.

    A queued place leaves the list once it is done (it got a name / SAFE_DONE) unless it is in `taken`
    (already done by the other machine, so it stays unnamed and gets re-listed every sweep).
    Returns (processed ids, rounds, idle wraps).
    """
    drafts = sorted(ids, reverse=True)
    removable = {i for i in ids if i not in taken}
    done, state, rounds, idle = set(), {"start_page": 1}, 0, 0
    while done < removable and rounds < 500:
        rounds += 1
        first, last = hp["page_window"](state, pages_per_round)
        off = (first - 1) * page_size
        end = len(drafts) if last is None else last * page_size
        pages = max(0, -(-(min(end, len(drafts)) - off) // page_size))
        rows = sorted(({"id": i} for i in drafts[off:end]), key=lambda r: r["id"])
        queue, state, _left = hp["plan_round"](rows, state,
                                               {"pages": pages, "capped": end < len(drafts)}, limit)
        if not queue and int(state.get("start_page") or 1) <= 1:
            idle += 1
            if idle > 3:
                break
        for r in queue:
            if r["id"] in taken:
                continue
            done.add(r["id"])
            drafts.remove(r["id"])
    return done, rounds, idle


# ---------------------------------------------------------------- mock admin API
def draft_rows(n, start_id=10000, named_every=0, name="Quan"):
    rows, rid = [], start_id + n - 1
    for i in range(n):
        named = named_every and i % named_every == 0
        rows.append({"id": rid, "name": f"{name} {rid}" if named else "", "status": "draft"})
        rid -= 1
    return rows


class MockAPI(BaseHTTPRequestHandler):
    mode = "modern"          # modern | legacy | modern_no_filters
    drafts = []
    queries = []

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.startswith(("/v1/auth/login", "/v1/auth/refresh")):
            self._send({"access_token": "tok", "access_token_expires_at": "2030-01-01T00:00:00Z",
                        "refresh_token": "ref", "user": {"role": "admin", "email": "x@y.z"}})
            return
        self._send({"error": {"code": "not_found"}}, 404)

    def do_GET(self):
        u = urlparse(self.path)
        type(self).queries.append(self.path)
        m = re.match(r"^/v1/admin/restaurants/(\d+)$", u.path)
        if m:
            row = next((r for r in self.drafts if r["id"] == int(m.group(1))), None)
            self._send({"restaurant": dict(row)} if row else {"error": {"code": "not_found"}}, 200 if row else 404)
            return
        if u.path != "/v1/admin/restaurants":
            self._send({"error": {"code": "not_found"}}, 404)
            return
        q = parse_qs(u.query)
        rows, per, summary, paged = list(self.drafts), 24, False, self.mode != "legacy"
        if paged:
            per = max(1, min(50, int(q.get("per_page", ["24"])[0])))
            summary = q.get("summary", ["0"])[0] in ("1", "true")
        if self.mode == "modern":
            if q.get("unnamed", ["0"])[0] in ("1", "true"):
                rows = [r for r in rows if not (r.get("name") or "").strip()]
            sfx = q.get("id_suffix", [""])[0]
            if sfx:
                keep = {int(x) for x in re.split(r"[,\s]+", sfx) if x}
                rows = [r for r in rows if r["id"] % 10 in keep]
        page = max(1, int(q.get("page", ["1"])[0]))
        chunk = rows[(page - 1) * per: page * per]
        if summary:
            chunk = [{"id": c["id"], "name": c["name"], "status": c["status"], "google_place_id": None} for c in chunk]
        body = {"restaurants": chunk, "total_count": len(rows)}
        if paged:
            body.update({"per_page": per, "has_more": page * per < len(rows)})
        self._send(body)


@contextlib.contextmanager
def mock_server(mode, drafts):
    handler = type("H", (MockAPI,), {"mode": mode, "drafts": drafts, "queries": []})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1], handler.queries
    finally:
        srv.shutdown()
        srv.server_close()


def load_safe_run(base, root):
    for name in ("safe_run", "aqd_api", "maps_fetch", "fast_pass", "olcdec"):
        sys.modules.pop(name, None)
    if os.path.join(ROOT, "common") not in sys.path:
        sys.path.insert(0, os.path.join(ROOT, "common"))
    session = os.path.join(root, ".session")
    os.makedirs(session, exist_ok=True)
    with open(os.path.join(session, "api_tokens.json"), "w", encoding="utf-8") as f:
        # pre-seeded: the listing is what is under test, so skip the login request entirely
        json.dump({"access_token": "mock", "access_token_expires_at": "2030-01-01T00:00:00Z",
                   "refresh_token": "mock"}, f)
    os.environ.update(AQD_ROOT=root, AQD_BASE=base, AQD_SESSION_DIR=session,
                      AQD_ADMIN_EMAIL="mock@example.com", AQD_ADMIN_PASSWORD="mock",
                      AQD_LOG_FILE=os.path.join(root, "api.log"))
    import safe_run
    safe_run.GAP = 0.001
    safe_run.MAPS_MIN_GAP = 0.0
    safe_run.MAPS_JITTER = 0.0
    safe_run.LOG = os.path.join(root, "safe_run_log.md")
    safe_run.DATA_DIR = os.path.join(root, "aqd_data")
    return safe_run


def list_args(queue, **kw):
    d = dict(floor=0, ceil=10 ** 9, queue=queue, per_page=50, unnamed=True, id_suffix="",
             start_page=1, max_pages=20)
    d.update(kw)
    return argparse.Namespace(**d)


def call_list(safe_run, queue, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        meta = safe_run.cmd_list(list_args(queue, **kw))
    return meta, json.load(open(queue, encoding="utf-8")), buf.getvalue()


def sweep_all(safe_run, queue, **kw):
    """Walk every page window the way alt.py does; return the union of the listed ids."""
    seen, start = set(), 1
    for _ in range(300):
        meta, rows, _o = call_list(safe_run, queue, start_page=start, **kw)
        seen |= {r["id"] for r in rows}
        if not meta["capped"]:
            return seen, meta
        start += meta["pages"]
    raise AssertionError("sweep did not finish")


# ---------------------------------------------------------------- tests
def test_eta(root):
    sr = load_safe_run("http://127.0.0.1:1", root)
    print("=== ETA line")
    check("all pages", sr.eta_line(5763, 24, 0, 3.0),
          "listing 241 pages (~12 min at 3s/page) - fetching starts right after")
    check("capped window", sr.eta_line(5763, 24, 20, 3.0),
          "listing 241 pages (~12 min at 3s/page) - listing only the first 20/241 pages now (~1 min),"
          " fetching starts right after")
    check("cap larger than the list", sr.eta_line(100, 50, 20, 3.0),
          "listing 2 pages (~0 min at 3s/page) - fetching starts right after")
    check("per_page 50 shortens the list", sr.eta_line(5763, 50, 20, 3.0),
          "listing 116 pages (~6 min at 3s/page) - listing only the first 20/116 pages now (~1 min),"
          " fetching starts right after")


def test_alt_helpers(rel):
    src, hp = load_helpers(rel)
    print(f"=== {rel} helpers")

    def boom(fn, *a):
        try:
            fn(*a)
        except SystemExit:
            return True
        return False

    check("list_max_pages(None) -> default 20", hp["list_max_pages"](None), 20)
    check("list_max_pages('') -> 0 (whole list, old behaviour)", hp["list_max_pages"](""), 0)
    check("list_max_pages('0') -> 0", hp["list_max_pages"]("0"), 0)
    check("list_max_pages(' 25 ') -> 25", hp["list_max_pages"](" 25 "), 25)
    check("list_max_pages('abc') rejected", boom(hp["list_max_pages"], "abc"), True)
    check("fetch_limit(None) -> 0", hp["fetch_limit"](None), 0)
    check("fetch_limit('') -> 0", hp["fetch_limit"](""), 0)
    check("fetch_limit('12') -> 12", hp["fetch_limit"]("12"), 12)
    check("fetch_limit('-1') rejected", boom(hp["fetch_limit"], "-1"), True)
    check("page_window({}, 20)", hp["page_window"]({}, 20), (1, 20))
    check("page_window({}, 0) -> to the end", hp["page_window"]({}, 0), (1, None))
    check("page_window({'start_page': 21}, 20)", hp["page_window"]({"start_page": 21}, 20), (21, 40))
    check("advance capped -> next window", hp["advance"]({"start_page": 1}, {"pages": 20, "capped": True}),
          {"start_page": 21})
    check("advance not capped -> wrap to the top",
          hp["advance"]({"start_page": 21}, {"pages": 3, "capped": False}), {"start_page": 1})
    check("advance no pages -> wrap", hp["advance"]({"start_page": 21}, {"pages": 0, "capped": True}),
          {"start_page": 1})
    rows = [{"id": i} for i in (5, 4, 3, 2, 1)]
    q, st, left = hp["plan_round"](rows, {"start_page": 7}, {"pages": 20, "capped": True}, 2)
    check("plan_round keeps the first 2", [r["id"] for r in q], [5, 4])
    check("plan_round holds the same window", (st, left), ({"start_page": 7, "tried": [4, 5]}, 3))
    q, st, left = hp["plan_round"]([{"id": i} for i in (3, 2, 1)], {"start_page": 7, "tried": [5, 4]},
                                   {"pages": 20, "capped": True}, 2)
    check("plan_round still holding with 1 left", ([r["id"] for r in q], st, left),
          ([3, 2], {"start_page": 7, "tried": [2, 3, 4, 5]}, 1))
    q, st, left = hp["plan_round"]([{"id": i} for i in (3, 2, 1)], {"start_page": 7, "tried": [5, 4]},
                                   {"pages": 20, "capped": True}, 3)
    check("plan_round finished window -> advance", ([r["id"] for r in q], st, left),
          ([3, 2, 1], {"start_page": 27}, 0))
    q, st, left = hp["plan_round"](rows, {"start_page": 1}, {"pages": 20, "capped": False}, 0)
    check("plan_round without a limit -> whole window, wrap", ([r["id"] for r in q], st, left),
          ([5, 4, 3, 2, 1], {"start_page": 1}, 0))
    check("source keeps the suffix filter", 'r["id"] % 10 in SUFFIXES' in src, True)
    check("source keeps the parity fallback", 'r["id"] % 2 == (0 if PARITY == "even" else 1)' in src, True)
    check("source keeps the queue names",
          '"/queue_ids_%s.json" % "".join(str(d) for d in SUFFIXES)' in src and 'MINE = R + f"/queue_{PARITY}.json"' in src, True)
    check("source lists one page window", '"--start-page"' in src and '"--per-page", "50"' in src, True)
    check("source sends the suffix to the server", 'SUFFIX_ARG = ["--id-suffix"' in src, True)
    check("source passes AQD_FETCH_LIMIT to run --limit", '"--limit", str(FETCH_LIMIT)' in src, True)
    check("source keeps the state file next to the queue", 'STATE = MINE + ".state.json"' in src, True)
    check_true("source prints the listing plan before listing", "- fetching starts right after" in src)
    return hp


def test_sweeps(hp, label):
    ids = list(range(1000, 1600))
    for limit in (0, 7):
        done, rounds, _ = simulate(hp, ids, 4, limit, 50)
        check(f"{label}: limit={limit} processes every draft over the sweeps", sorted(done), sorted(ids))
        print(f"       limit={limit}: {rounds} rounds")
    taken = set(ids[:30])
    done, rounds, _ = simulate(hp, ids, 4, 5, 50, taken=taken)
    check(f"{label}: limit=5 with 30 ids taken elsewhere still processes the rest",
          sorted(done), sorted(set(ids) - taken))
    print(f"       limit=5 + 30 taken: {rounds} rounds")
    done, _, _ = simulate(hp, ids, 1, 0, 24)
    check(f"{label}: 24/page windows process every draft", sorted(done), sorted(ids))
    done, _, _ = simulate(hp, ids, 0, 0, 50)
    check(f"{label}: AQD_LIST_MAX_PAGES=0 (whole list) processes every draft", sorted(done), sorted(ids))


def test_safe_run_modern(root, drafts):
    with mock_server("modern", drafts) as (port, queries):
        sr = load_safe_run(f"http://127.0.0.1:{port}", root)
        q = os.path.join(root, "queue.json")
        print("=== safe_run list, server WITH the new paging params")
        meta, rows, out = call_list(sr, q, start_page=1, max_pages=4, per_page=50, id_suffix="1,3,5")
        check("probe picked the new paging", meta["mode"], "paged")
        check("per_page honoured", meta["per_page"], 50)
        check("capped after 4 pages", (meta["pages"], meta["capped"]), (4, True))
        check("summary=1 requested", any("summary=1" in p for p in queries), True)
        check("id_suffix sent", any("id_suffix=1,3,5" in p or "id_suffix=1%2C3%2C5" in p for p in queries), True)
        check("unnamed=1 sent", any("unnamed=1" in p for p in queries), True)
        check_true("the ETA line is printed before the pages",
                   "listing" in out and "fetching starts right after" in out)
        check_true("only unnamed rows with my suffix listed",
                   rows and all(not r["name"].strip() and r["id"] % 10 in (1, 3, 5) for r in rows), f"{len(rows)} rows")
        want = {d["id"] for d in drafts if not d["name"].strip() and d["id"] % 10 in (1, 3, 5)}
        seen, last = sweep_all(sr, q, max_pages=4, per_page=50, id_suffix="1,3,5")
        check("sweep lists every draft of mine", sorted(seen), sorted(want))
        check("sweep ends on a non-capped window", last["capped"], False)
        meta, rows, _o = call_list(sr, q, start_page=1, max_pages=0, per_page=50, id_suffix="")
        check("max_pages 0 = whole list in one round (capped False)", (meta["capped"], meta["pages"] > 20), (False, True))
        check("that round listed every unnamed draft", len(rows), len([d for d in drafts if not d["name"].strip()]))


def test_safe_run_legacy(root, drafts):
    with mock_server("legacy", drafts) as (port, _q):
        sr = load_safe_run(f"http://127.0.0.1:{port}", root)
        q = os.path.join(root, "queue.json")
        print("=== safe_run list, server that IGNORES the new params")
        meta, rows, out = call_list(sr, q, start_page=1, max_pages=4, per_page=50, id_suffix="1,3,5")
        check("falls back to the old listing", meta["mode"], "legacy")
        check("24 rows per page", meta["per_page"], 24)
        check("capped after 4 pages", (meta["pages"], meta["capped"]), (4, True))
        check_true("no crash, rows listed", len(rows) > 0, f"{len(rows)} rows")
        check_true("the ETA line is still printed", "fetching starts right after" in out)
        want = {d["id"] for d in drafts if not d["name"].strip()}
        seen, _last = sweep_all(sr, q, max_pages=4, per_page=50, id_suffix="1,3,5")
        check("sweep lists every unnamed draft (the suffix filter is alt.py's job in this mode)",
              sorted(seen), sorted(want))


def test_safe_run_modern_no_filters(root, drafts):
    with mock_server("modern_no_filters", drafts) as (port, _q):
        sr = load_safe_run(f"http://127.0.0.1:{port}", root)
        q = os.path.join(root, "queue.json")
        print("=== safe_run list, paging honoured but unnamed/id_suffix ignored")
        meta, rows, _o = call_list(sr, q, start_page=1, max_pages=2, per_page=50, id_suffix="1,3")
        check("paging still detected", meta["mode"], "paged")
        check("two pages of 50", (meta["pages"], meta["per_page"]), (2, 50))
        check_true("client still keeps only unnamed rows",
                   rows and all(not r["name"].strip() for r in rows), f"{len(rows)} rows")
        check("client keeps every id (suffix is the caller's filter)", len(rows), 100 - len(
            [d for d in drafts[:100] if d["name"].strip()]))


def test_run_limit(root):
    ids = [9106, 9105, 9104, 9103, 9102, 9101]
    drafts = [{"id": i, "name": "", "status": "draft"} for i in ids]
    with mock_server("modern", drafts) as (port, _q):
        sr = load_safe_run(f"http://127.0.0.1:{port}", root)
        print("=== safe_run run --limit")
        q = os.path.join(root, "queue_limit.json")
        json.dump([{"id": i} for i in ids], open(q, "w", encoding="utf-8"))
        sr.build_info = lambda row: ({"name": "", "category": "cafe", "photo_urls": [],
                                      "_maps": {"count": "0", "thin": False, "cats": [], "via": "chrome"}}, None)
        pushed = []
        sr.cmd_push = lambda a: pushed.append(a.ids[0]) or "ok"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sr.cmd_run(argparse.Namespace(floor=0, queue=q, limit=3, ignore_peak=True, ids=None, force=False))
        check("only 3 places fetched", len(pushed), 3)
        check("the first queue rows were fetched", pushed, ids[:3])
        check_true("the stop is logged", "run limit 3 reached" in buf.getvalue())


def main():
    print(f"root: {ROOT}")
    root = tempfile.mkdtemp(prefix="aqd-listpaging-")
    drafts = draft_rows(1500, named_every=5)
    test_eta(root)
    helpers = [(rel, test_alt_helpers(rel)) for rel in ("windows/alt.py", "mac/alt.py")]
    for rel, hp in helpers:
        test_sweeps(hp, rel)
    test_safe_run_modern(root, drafts)
    test_safe_run_legacy(root, drafts)
    test_safe_run_modern_no_filters(root, drafts)
    test_run_limit(root)
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("\nALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
