"""Offline self-test for the 2026-10-24 audit fixes (an_uong_quanh_day#1083 #1084 #1085 #1087).
No network: safe_run talks to a fake Guard, Chrome / disk / sign-in are monkeypatched.

Run: python tools/test_audit_gaps.py [repo-root]

  #1083 reviews : AQD_MAX_REVIEWS (default 50), no listugcposts left (DOM scrolling: tools/test_reviews_dom.py),
                  push cap + 422 -> 20 fallback
  #1084 photos  : picked-but-0-sent is an error with backoff retry (photo_retry.json), push --photo-retry, give-up,
                  replay-photos dry run / --apply (skips places that already have server photos)
  #1085 disk    : AQD_MIN_FREE_GB guard in safe_run (exit 4 under AQD_ALT) and in both alt.py loops
  thin          : signed-out Chrome -> run refuses; thin place -> not pushed / not validated / retryable
"""
import ast
import contextlib
import io
import json
import os
import sys
import tempfile
import time

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILURES = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        FAILURES.append(label)


def check_true(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + (f" ({detail})" if detail else ""))
    if not cond:
        FAILURES.append(label)


def fresh_import(root, **env):
    for name in ("safe_run", "aqd_api", "maps_fetch", "fast_pass", "olcdec", "google_extras", "heartbeat"):
        sys.modules.pop(name, None)
    if os.path.join(ROOT, "common") not in sys.path:
        sys.path.insert(0, os.path.join(ROOT, "common"))
    session = os.path.join(root, ".session"); os.makedirs(session, exist_ok=True)
    for k in ("AQD_MAX_REVIEWS", "AQD_GOOGLE_REVIEWS_PUSH_MAX", "AQD_ALT", "AQD_CDP", "AQD_ALLOW_THIN",
              "AQD_INGEST_TOKEN", "AQD_MIN_FREE_GB"):
        os.environ.pop(k, None)
    os.environ.update(AQD_ROOT=root, AQD_BASE="http://127.0.0.1:1", AQD_SESSION_DIR=session,
                      AQD_ADMIN_EMAIL="mock@example.com", AQD_ADMIN_PASSWORD="mock",
                      AQD_LOG_FILE=os.path.join(root, "api.log"), **env)
    import safe_run
    safe_run.GAP = 0.001
    safe_run.LOG = os.path.join(root, "safe_run_log.md")
    safe_run.PROG = os.path.join(root, "aqd_progress.md")
    safe_run.DATA_DIR = os.path.join(root, "aqd_data")
    safe_run.HB = None
    return safe_run


class APIErr(Exception):
    def __init__(self, status, code="x"):
        super().__init__(f"{status} {code}"); self.status = status; self.code = code


class FakeG:
    """Stands in for safe_run.Guard: records every request, answers like the admin API."""

    def __init__(self, photos_mode="ok", server_photos=None):
        self.calls, self.photos_mode, self.server_photos = [], photos_mode, server_photos or {}
        self.slow = None; self.n = 0; self.tt = 0.0; self.max = 0.0

    def req(self, method, path, json_body=None, **kw):
        self.calls.append((method, path, json_body)); self.n += 1
        parts = path.split("?")[0].split("/")
        if method == "GET" and path.endswith("photos?page=1&per_page=1"):
            return {"photos": self.server_photos.get(int(parts[4]), [])}
        if method == "GET":
            return {"restaurant": {"id": int(parts[4]), "status": "draft", "name": ""}}
        if method == "PATCH":
            r = {"id": int(parts[4]), "status": "draft"}; r.update(json_body or {}); return {"restaurant": r}
        if method == "POST" and parts[-1] == "photos":
            urls = [x["url"] if isinstance(x, dict) else x for x in json_body["image_urls"]]
            if self.photos_mode == "none":
                return {"photos": [], "errors": []}
            if self.photos_mode == "errors":
                return {"photos": [], "errors": [{"url": u, "error": "download_failed"} for u in urls]}
            return {"photos": [{"id": 1000 + i, "status": "pending"} for i, _ in enumerate(urls)], "errors": []}
        return {}

    def count(self, method, frag=""):
        return sum(1 for m, p, _ in self.calls if m == method and frag in p)


def make_place(sr, rid, n_photos=3, thin=False):
    d = os.path.join(sr.DATA_DIR, str(rid)); os.makedirs(d, exist_ok=True)
    info = {"name": f"Quan {rid}", "address": "1 Le Loi", "category": "eatery", "latitude": "10.7", "longitude": "106.7",
            "photo_urls": [{"url": f"https://lh3.googleusercontent.com/p/AF{rid}x{i}=s1600", "kind": "Tất cả",
                            **({"role": "cover"} if i == 0 else {})} for i in range(n_photos)],
            "_extra": {}, "_maps": {"via": "chrome", "thin": thin}}
    json.dump(info, open(os.path.join(d, "info.json"), "w", encoding="utf-8"))
    open(os.path.join(d, "SAFE_PREP"), "w").write("x")
    return info


class A:  # argparse stand-in
    def __init__(self, **kw):
        self.ids = None; self.force = False; self.limit = 0; self.ignore_peak = True; self.photo_retry = False
        self.ids_file = ""; self.apply = False; self.check_server = False
        self.__dict__.update(kw)


def quiet(fn, *a, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


# ------------------------------------------------------------------ #1083 reviews
def review_node(i, ts_us=None):
    return [f"rv{i}", [None, None, ts_us or (1_700_000_000_000_000 + i), None, [None] * 5 + [[f"User {i}"]], None, "1 tuần trước"],
            [[5 - i % 5]] + [None] * 13 + [["vi"]] + [[[f"text {i}"]]]]


def test_reviews():
    print("=== #1083 reviews: AQD_MAX_REVIEWS + Reviews-panel scrolling wired in (no listugcposts)")
    root = tempfile.mkdtemp(prefix="aqd-rv-")
    sr = fresh_import(root); mf = sr.mf; ge = sr.google_extras
    check("maps_fetch.REVIEW_CAP default", mf.REVIEW_CAP, 50)
    check("google_extras.MAX_REVIEWS default", ge.MAX_REVIEWS, 50)
    sr = fresh_import(root, AQD_MAX_REVIEWS="30"); mf = sr.mf
    check("AQD_MAX_REVIEWS=30 -> maps cap", mf.REVIEW_CAP, 30)
    check("AQD_MAX_REVIEWS=30 -> push cap", sr.google_extras.PUSH_MAX, 30)
    sr = fresh_import(root, AQD_MAX_REVIEWS="nonsense"); mf = sr.mf
    check("bad AQD_MAX_REVIEWS -> 50", mf.REVIEW_CAP, 50)

    # the dead listugcposts pager (always 403) is gone; reviews come from scrolling the panel (reviews_dom.py)
    for name in ("ugc_path", "parse_ugc", "more_reviews", "UGC_PAGE", "UGC_MAX_PAGES"):
        check_true(f"maps_fetch.{name} removed", not hasattr(mf, name))
    check_true("safe_run._browser_get removed", not hasattr(sr, "_browser_get"))
    for f in ("common/safe_run.py", "common/maps_fetch.py"):
        check_true(f"no listugcposts / batchexecute in {f}",
                   not any(w in open(os.path.join(ROOT, f), encoding="utf-8").read() for w in ("listugcposts", "batchexecute")))

    sr = fresh_import(root); mf = sr.mf
    import reviews_dom as rd
    preview = [mf.review_row(review_node(i)) for i in range(1, 6)]
    seen = {}

    class FakeDriver:
        def __init__(self, pg, url):
            seen["pg"], seen["url"] = pg, url
    orig = (rd.PageDriver, rd.page_reviews)
    rd.PageDriver = FakeDriver
    rd.page_reviews = lambda drv, have, cap, want=None: (have + [{"id": "dom1"}], f"dom+1/1s cap={cap} want={want}")
    sr._PW["pg"] = "PAGE"
    info = {"_meta": {"place_id": "ChIJtest", "hex_id": "0xa:0xb", "extra": {"reviews": list(preview), "google_review_count": 12}}}
    try:
        quiet(sr._more_reviews, info)
        ex = info["_meta"]["extra"]
        check("_more_reviews uses the crawler's own tab", seen.get("pg"), "PAGE")
        check("_more_reviews opens the place by place_id", seen.get("url"), "https://www.google.com/maps/place/?q=place_id:ChIJtest&hl=vi")
        check("_more_reviews passes cap + Google count", ex["reviews_paging"], "dom+1/1s cap=50 want=12")
        check("_more_reviews stores rows", len(ex["reviews"]), 6)

        def boom(*a, **k):
            raise RuntimeError("x")
        rd.page_reviews = boom
        info["_meta"]["extra"]["reviews"] = list(preview)
        quiet(sr._more_reviews, info)
        check("driver error keeps the preview", (len(ex["reviews"]), ex["reviews_paging"]), (5, "dom_err:RuntimeError"))
    finally:
        rd.PageDriver, rd.page_reviews = orig
        sr._PW.pop("pg", None)

    # push: 50 rows go out; a server still at 20 answers 422 -> one retry with the newest 20, then 20 directly
    sr = fresh_import(root); ge = sr.google_extras
    ex = {"google_rating": 4.5, "reviews": [{"id": f"r{i}", "rating": 5, "text": "x"} for i in range(60)]}
    check("payload capped at 50", len(ge.reviews_payload(ex)["reviews"]), 50)
    sent = []

    def old_server(method, path, json_body=None):
        if path.endswith("/google_reviews"):
            sent.append(len(json_body["reviews"]))
            if len(json_body["reviews"]) > 20:
                raise APIErr(422, "google_reviews_invalid")
            return {"reviews": json_body["reviews"]}
        return {}
    res = ge.push(old_server, 7, ex, api_error=APIErr)
    check("old server: 50 -> 422 -> 20", sent, [50, 20])
    check_true("log says reviews=20/20", "reviews=20/20" in res, res)
    sent.clear(); ge.push(old_server, 8, ex, api_error=APIErr)
    check("next place sends 20 directly", sent, [20])
    sr = fresh_import(root); ge = sr.google_extras; sent.clear()

    def new_server(method, path, json_body=None):
        if path.endswith("/google_reviews"):
            sent.append(len(json_body["reviews"])); return {"reviews": json_body["reviews"]}
        return {}
    ge.push(new_server, 9, ex, api_error=APIErr)
    check("new server (cap 50): one PUT of 50", sent, [50])
    sent.clear()

    def bad_server(method, path, json_body=None):
        if path.endswith("/google_reviews"):
            sent.append(len(json_body["reviews"])); raise APIErr(422, "google_reviews_invalid")
        return {}
    res = ge.push(bad_server, 10, {"reviews": ex["reviews"][:10]}, api_error=APIErr)
    check("422 on <=20 rows is not retried", (sent, "reviews_fail=422" in res), ([10], True))


# ------------------------------------------------------------------ #1084 photos
def test_photo_retry():
    print("=== #1084 photos picked but 0 sent -> error + retry")
    root = tempfile.mkdtemp(prefix="aqd-ph-")
    sr = fresh_import(root)
    sr.disk_ok = lambda where="": True
    make_place(sr, 501)
    G = FakeG("none")
    try:
        sr.push_one(G, 501); raised = False
    except sr.PhotosUnsent:
        raised = True
    check_true("push_one raises PhotosUnsent when the server returns no photo", raised)
    check("no validate PATCH after unsent photos", sum(1 for m, p, b in G.calls if m == "PATCH" and b and b.get("status") == "validated"), 0)

    G = FakeG("none"); sr.Guard = lambda: G
    quiet(sr.cmd_push, A(ids=[501], force=True))
    check("no SAFE_DONE", os.path.exists(os.path.join(sr.DATA_DIR, "501", "SAFE_DONE")), False)
    e = json.load(open(sr.PHOTO_RETRY))["501"]
    check("photo_retry entry", (e["tries"], e["state"]), (1, "retry"))
    check_true("first retry after 15 min", 14.9 * 60 < e["next_at"] - time.time() <= 15 * 60 + 1)
    check_true("logged as ERROR PHOTOS_UNSENT", "ERROR PHOTOS_UNSENT" in open(sr.LOG, encoding="utf-8").read())
    check("not due yet", sr.photo_retry_due(), {})
    check("due later", list(sr.photo_retry_due(now=time.time() + 16 * 60)), [501])
    e2 = sr.photo_retry_fail(777, "x", now=0); e3 = sr.photo_retry_fail(777, "x", now=0); e4 = sr.photo_retry_fail(777, "x", now=0)
    check("backoff 15/30/60 min", [e2["next_at"], e3["next_at"], e4["next_at"]], [900.0, 1800.0, 3600.0])
    for _ in range(6): e5 = sr.photo_retry_fail(777, "x", now=0)
    check("backoff capped at 6 h", e5["next_at"], 21600.0)

    # retry now succeeds -> SAFE_DONE + done
    d = sr._pr_load(); d["501"]["next_at"] = 0; sr._pr_save(d)
    G = FakeG("ok"); sr.Guard = lambda: G
    quiet(sr.cmd_push, A(photo_retry=True))
    check("retry pushes the due place", G.count("POST", "/photos"), 1)
    check("SAFE_DONE after a good retry", os.path.exists(os.path.join(sr.DATA_DIR, "501", "SAFE_DONE")), True)
    check("entry done", sr._pr_load()["501"]["state"], "done")
    check("validated PATCH sent", sum(1 for m, p, b in G.calls if m == "PATCH" and b and b.get("status") == "validated"), 1)

    # errors for every URL, 3 retries -> the 4th push finishes without photos (gave_up)
    make_place(sr, 502)
    G = FakeG("errors"); sr.Guard = lambda: G
    quiet(sr.cmd_push, A(ids=[502], force=True))
    for _ in range(sr.PHOTO_RETRY_MAX - 1):
        d = sr._pr_load(); d["502"]["next_at"] = 0; sr._pr_save(d)
        quiet(sr.cmd_push, A(photo_retry=True))
    check("still not done after tries 1..3", (os.path.exists(os.path.join(sr.DATA_DIR, "502", "SAFE_DONE")), sr._pr_load()["502"]["tries"]), (False, 3))
    d = sr._pr_load(); d["502"]["next_at"] = 0; sr._pr_save(d)
    quiet(sr.cmd_push, A(photo_retry=True))
    check("last retry finishes the place", os.path.exists(os.path.join(sr.DATA_DIR, "502", "SAFE_DONE")), True)
    check("entry gave_up", sr._pr_load()["502"]["state"], "gave_up")
    check_true("warn says photos_unsent_gave_up", "photos_unsent_gave_up" in open(sr.LOG, encoding="utf-8").read())

    # a place without picked photos is not an error
    make_place(sr, 503, n_photos=0)
    G = FakeG("none"); sr.Guard = lambda: G
    quiet(sr.cmd_push, A(ids=[503], force=True))
    check("0 photos picked -> done as before", os.path.exists(os.path.join(sr.DATA_DIR, "503", "SAFE_DONE")), True)


def test_replay():
    print("=== #1084 replay-photos (dry run default, --apply skips places with server photos)")
    root = tempfile.mkdtemp(prefix="aqd-rp-")
    sr = fresh_import(root)
    sr.disk_ok = lambda where="": True
    for rid in (601, 602, 603): make_place(sr, rid)
    make_place(sr, 604, n_photos=0)
    json.dump({"urls": {"h": {"photo_id": 9}}}, open(os.path.join(sr.DATA_DIR, "603", "applied.json"), "w"))
    make_place(sr, 605, thin=True)
    ids = os.path.join(root, "ids.tsv")
    open(ids, "w").write("# header\n601\t3\n602 3\n603\n604\n605\n606\n")
    check("plan 601", sr.replay_plan(601)[1], "replay")
    check("plan 603 already sent", sr.replay_plan(603)[1], "already_sent")
    check("plan 604 nothing picked", sr.replay_plan(604)[1], "no_photos_picked")
    check("plan 605 thin", sr.replay_plan(605)[1], "thin_info")
    check("plan 606 no info", sr.replay_plan(606)[1], "no_info")
    G = FakeG("ok", server_photos={602: [{"id": 1}]}); sr.Guard = lambda: G
    tally = quiet(sr.cmd_replay_photos, A(ids_file=ids))
    check("dry run: no request at all", len(G.calls), 0)
    check("dry run tally", tally.get("replay"), 2)
    res = quiet(sr.cmd_replay_photos, A(ids_file=ids, check_server=True))
    check("check-server: GET only", (G.count("POST"), G.count("GET"), res), (0, 2, {"server_has_none": 1, "server_has_photos": 1}))
    G.calls.clear()
    res = quiet(sr.cmd_replay_photos, A(ids_file=ids, apply=True))
    check("apply: 601 sent, 602 skipped (server has photos)", res, {"sent": 1, "server_has_photos": 1})
    check("apply: only photo POSTs, no PATCH/PUT", (G.count("POST", "/photos"), G.count("PATCH"), G.count("PUT")), (1, 0, 0))
    G.calls.clear()
    res = quiet(sr.cmd_replay_photos, A(ids_file=ids, apply=True))
    check("re-run skips the done list", (len(G.calls), res), (0, {}))


# ------------------------------------------------------------------ #1085 disk
def test_disk():
    print("=== #1085 free-disk guard")
    root = tempfile.mkdtemp(prefix="aqd-dk-")
    sr = fresh_import(root)
    check("AQD_MIN_FREE_GB default", sr.MIN_FREE_GB, 5.0)
    sr.free_gb = lambda path=None: 2.0
    check("2 GB free -> not ok", quiet(sr.disk_ok, "x"), False)
    os.environ["AQD_ALT"] = "1"
    try:
        quiet(sr.disk_guard, "x"); code = None
    except SystemExit as e:
        code = e.code
    os.environ.pop("AQD_ALT", None)
    check("AQD_ALT: exit 4", code, 4)
    sr.free_gb = lambda path=None: 50.0
    check("50 GB free -> ok", sr.disk_ok("x"), True)
    sr.free_gb = lambda path=None: None
    check("unknown free space never blocks", sr.disk_ok("x"), True)
    sr.free_gb = lambda path=None: 2.0; sr.MIN_FREE_GB = 0
    check("AQD_MIN_FREE_GB=0 disables", sr.disk_ok("x"), True)
    sr.MIN_FREE_GB = 5.0
    make_place(sr, 701)
    G = FakeG("ok"); sr.Guard = lambda: G
    os.environ["AQD_ALT"] = "1"
    try:
        quiet(sr.cmd_push, A(ids=[701], force=True)); code = None
    except SystemExit as e:
        code = e.code
    os.environ.pop("AQD_ALT", None)
    check("push on a full disk: exit 4 before any request", (code, len(G.calls)), (4, 0))


WANTED = {"env_num", "disk_free_gb", "disk_low", "photo_retry_due", "read_json"}


def test_alt(rel):
    print(f"=== {rel}: disk guard + photo retry helpers")
    src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    tree = ast.parse(src)
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in WANTED]
    check("helpers present", {n.name for n in funcs}, WANTED)
    ns = {"os": os, "sys": sys, "json": json, "time": time, "R": tempfile.mkdtemp(prefix="aqd-alt-"),
          "MIN_FREE_GB": 5.0}
    exec("\n\n".join(ast.get_source_segment(src, n) for n in funcs), ns)
    check("disk_low(2)", ns["disk_low"](2.0), True)
    check("disk_low(9)", ns["disk_low"](9.0), False)
    check("disk_low(None)", ns["disk_low"](None), False)
    check("disk_low guard off", ns["disk_low"](2.0, 0), False)
    p = os.path.join(ns["R"], "photo_retry.json")
    json.dump({"1": {"state": "retry", "next_at": 0}, "2": {"state": "retry", "next_at": 9e12},
               "3": {"state": "gave_up", "next_at": 0}, "4": {"state": "done", "next_at": 0}}, open(p, "w"))
    check("due ids", ns["photo_retry_due"](p), [1])
    check("missing file", ns["photo_retry_due"](p + ".none"), [])
    check_true("loop handles exit 4 / 5", "EXIT_DISK" in src and "EXIT_SIGNED_OUT" in src and '"--photo-retry"' in src)


# ------------------------------------------------------------------ thin data
def test_thin():
    print("=== thin data is never validated")
    root = tempfile.mkdtemp(prefix="aqd-th-")
    sr = fresh_import(root, AQD_CDP="1")
    sr.disk_ok = lambda where="": True
    make_place(sr, 801, thin=True)
    G = FakeG("ok")
    check("push_one: thin -> 'thin', no request", (sr.push_one(G, 801)[0], len(G.calls)), ("thin", 0))
    sr.Guard = lambda: G
    quiet(sr.cmd_push, A(ids=[801], force=True))
    check("thin place: no SAFE_DONE", os.path.exists(os.path.join(sr.DATA_DIR, "801", "SAFE_DONE")), False)
    check_true("logged ERROR THIN", "ERROR THIN" in open(sr.LOG, encoding="utf-8").read())

    # signed-out Chrome: run refuses before any request
    q = os.path.join(root, "q.json"); json.dump([{"id": 811}, {"id": 812}], open(q, "w"))
    sr.chrome_signed_in = lambda: False
    G = FakeG("ok"); sr.Guard = lambda: G
    check("run refuses when signed out", quiet(sr.cmd_run, A(queue=q, floor=0, limit=0)), "SIGNED_OUT")
    check("no request", len(G.calls), 0)
    os.environ["AQD_ALT"] = "1"
    try:
        quiet(sr.cmd_run, A(queue=q, floor=0, limit=0)); code = None
    except SystemExit as e:
        code = e.code
    os.environ.pop("AQD_ALT", None)
    check("AQD_ALT: exit 5", code, 5)
    check_true("clear REFUSE log line", "REFUSE TO RUN" in open(sr.LOG, encoding="utf-8").read())
    sr.chrome_signed_in = lambda: None
    check("unknown sign-in (Chrome down) does not refuse", sr.require_signin(), True)

    # signed in, but the place comes back thin: nothing pushed, no info.json, THIN flag, retryable
    sr.chrome_signed_in = lambda: True
    sr.MAPS_MIN_GAP = 0.0; sr.MAPS_JITTER = 0.0
    thin_info = {"name": "Quan", "category": "eatery", "photo_urls": [{"url": "https://lh3.googleusercontent.com/p/x=s1600"}],
                 "_extra": {}, "_maps": {"via": "chrome", "thin": True, "count": "14 ảnh"}}
    sr.build_info = lambda r: (json.loads(json.dumps(thin_info)), None)
    G = FakeG("ok"); sr.Guard = lambda: G
    json.dump([{"id": 811}], open(q, "w"))
    quiet(sr.cmd_run, A(queue=q, floor=0, limit=0))
    d = os.path.join(sr.DATA_DIR, "811")
    check("thin: no info.json / SAFE_PREP / SAFE_DONE",
          [os.path.exists(os.path.join(d, f)) for f in ("info.json", "SAFE_PREP", "SAFE_DONE")], [False, False, False])
    check("thin: THIN flag + info.thin.json", [os.path.exists(os.path.join(d, f)) for f in ("THIN", "info.thin.json")], [True, True])
    check("thin: only the pre-check GET, no write", [m for m, _, _ in G.calls], ["GET"])
    # next pass, full data -> pushed and flag cleared
    full = dict(json.loads(json.dumps(thin_info)), _maps={"via": "chrome", "thin": False, "count": "40 ảnh"},
                latitude="10.7", longitude="106.7")
    sr.build_info = lambda r: (json.loads(json.dumps(full)), None)
    sr.limited_reason = lambda info: (None, 0, 0)
    quiet(sr.cmd_run, A(queue=q, floor=0, limit=0))
    check("full data later: SAFE_DONE, THIN cleared",
          [os.path.exists(os.path.join(d, f)) for f in ("SAFE_DONE", "THIN", "info.thin.json")], [True, False, False])
    # AQD_ALLOW_THIN=1 keeps the old behaviour (manual runs / tests only)
    sr = fresh_import(root, AQD_ALLOW_THIN="1")
    make_place(sr, 802, thin=True)
    check("AQD_ALLOW_THIN=1 pushes", sr.push_one(FakeG("ok"), 802)[0] in ("ok", "warn"), True)


def main():
    test_reviews()
    test_photo_retry()
    test_replay()
    test_disk()
    test_alt("mac/alt.py")
    test_alt("windows/alt.py")
    test_thin()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}: {FAILURES}")
        sys.exit(1)
    print("all audit-gap checks passed")


if __name__ == "__main__":
    main()
