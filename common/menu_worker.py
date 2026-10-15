#!/usr/bin/env python3
"""Menu worker: reads menu photos of places the fetcher finished and creates dishes + dish categories.

Runs beside the fetcher (separate process; does not touch the fetch queue).
For each aqd_data/<id>/ with SAFE_DONE and no MENU_DONE (newest first):
  1. candidates = photos Maps tagged "Thực đơn" + gallery photos CLIP labels 'menu' (photo_filter)
  2. local OCR (EasyOCR vi) -> rows (menu_ocr.parse_menu); a photo with < MIN_ROWS priced rows = unreadable, logged
  3. GET menu: place already has dishes -> skip (never duplicates, never deletes)
     else PUT /menu {rows}: new_category_name, kind, price_vnd (or price_on_request when unknown)
  4. phone: if the place has no phone, VN mobile numbers found by OCR on menu/sign photos.
     exactly one number, conf >= 0.6, and server phone still empty -> PATCH phone; otherwise logged only.
Pacing: >= 2 s between API calls; 5xx/429/timeouts -> 5 min pause; 3 in a row -> 30 min.
Stop: <root>/STOP_ALL or <root>/STOP_MENU. Log: <root>/menu_worker_log.md
  python menu_worker.py [--once] [--ids 1 2 3] [--dry]"""
import argparse, hashlib, io, json, os, re, sys, time, traceback
ROOT = os.environ.get("AQD_ROOT") or os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
for _cf in (os.path.join(ROOT, "creds.env"), os.path.join(ROOT, ".aqd_session", "creds.env")):
    if os.path.exists(_cf):
        for _l in open(_cf, encoding="utf-8"):
            _l = _l.strip()
            if _l and not _l.startswith("#") and "=" in _l:
                _k, _v = _l.split("=", 1); os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
        break
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("OMP_NUM_THREADS", "2")
import requests
import aqd_api
from aqd_api import AQD, APIError, AQDError, API
import menu_ocr as M
try:
    import photo_filter as PF
except Exception:
    PF = None

DATA = os.environ.get("AQD_DATA_DIR") or os.path.join(ROOT, "aqd_data")
LOG = os.path.join(ROOT, "menu_worker_log.md")
PHONE_LOG = os.path.join(ROOT, "menu_phone_review.txt")
CACHE = os.path.join(ROOT, "menu_ocr_cache")
GAP, MIN_ROWS, MAX_SCAN, MAX_OCR = 2.0, 3, 40, 5
MENU_TAG = "Thực đơn"
START_FLOOR = int(os.environ.get("AQD_MENU_FLOOR", "0"))
CEIL = int(os.environ.get("AQD_MENU_CEIL", "999999999"))


def log(s):
    line = f"- {time.strftime('%Y-%m-%d %H:%M:%S')} {s}"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def stopped():
    return os.path.exists(os.path.join(ROOT, "STOP_ALL")) or os.path.exists(os.path.join(ROOT, "STOP_MENU"))


class Pace:
    def __init__(self):
        self.a = AQD(); self.last = 0.0; self.fails = 0

    def call(self, method, path, **kw):
        while True:
            w = GAP - (time.time() - self.last)
            if w > 0: time.sleep(w)
            try:
                r = self.a.call(method, path, retries=0, timeout=60, **kw)
                self.fails = 0
                return r
            except APIError as e:
                if e.status >= 500 or e.status == 429:
                    self._back(f"{method} {path} HTTP {e.status}"); continue
                raise
            except AQDError as e:
                self._back(f"{method} {path} {e}"); continue
            finally:
                self.last = time.time()

    def _back(self, why):
        self.fails += 1
        m = 30 if self.fails >= 3 else 5
        log(f"API trouble ({why}); pause {m} min")
        end = time.time() + m * 60
        while time.time() < end:
            if stopped(): raise SystemExit(0)
            time.sleep(10)


def fetch(url):
    os.makedirs(CACHE, exist_ok=True)
    u = re.sub(r"=s\d+$", "", url) + "=s1600"
    f = os.path.join(CACHE, hashlib.sha1(u.encode()).hexdigest()[:16] + ".jpg")
    if not os.path.exists(f):
        r = requests.get(u, timeout=40, headers={"User-Agent": "Mozilla/5.0"}); r.raise_for_status()
        open(f, "wb").write(r.content)
    return f


def menu_candidates(info):
    tagged, rest = [], []
    for p in info.get("photo_urls") or []:
        (tagged if MENU_TAG in (p.get("kind") or "") else rest).append(p["url"])
    cands = list(dict.fromkeys(tagged))
    if PF is not None and os.environ.get("AQD_CLIP", "1") not in ("0", "no", "false"):
        for u in rest[:MAX_SCAN]:
            if len(cands) >= MAX_OCR + 3: break
            try:
                r = PF.classify_url(re.sub(r"=s\d+$", "", u) + "=s512")
                sc = r.get("scores") or {}
                if r.get("label") == "menu" or sc.get("menu", 0) >= 0.35:
                    cands.append(u)
            except Exception:
                pass
    return cands, tagged


def sign_candidates(info):
    out = []
    for p in info.get("photo_urls") or []:
        k = p.get("kind") or ""
        if p.get("role") == "cover" or re.search(r"Bên ngoài|Mặt tiền|Ngoại thất|Bảng hiệu", k):
            out.append(p["url"])
    return out[:3]


def process(P, rid, dry=False):
    d = os.path.join(DATA, str(rid))
    info = json.load(open(os.path.join(d, "info.json"), encoding="utf-8"))
    t0 = time.time()
    cands, tagged = menu_candidates(info)
    need_phone = not (info.get("phone") or "").strip()
    rows_all, used, unread, phone_hits = [], [], [], {}
    for u in cands[:MAX_OCR]:
        try:
            f = fetch(u)
            from PIL import Image
            W = Image.open(f).size[0]
            cj = f[:-4] + ".ocr.json"
            if os.path.exists(cj): toks = json.load(open(cj, encoding="utf-8"))
            else:
                toks = M.ocr(f); json.dump(toks, open(cj, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception as e:
            unread.append(f"{u[-20:]}:{type(e).__name__}"); continue
        for ph, c in M.phones(toks).items(): phone_hits[ph] = max(phone_hits.get(ph, 0), c)
        r = M.parse_menu(toks, W)
        if len(r["rows"]) < MIN_ROWS:
            unread.append(f"...{u[-16:]} rows={len(r['rows'])} toks={r.get('n_tokens', 0)}"); continue
        used.append(u); rows_all += r["rows"]
    if need_phone and not phone_hits:
        for u in sign_candidates(info):
            try:
                for ph, c in M.phones(M.ocr(fetch(u))).items(): phone_hits[ph] = max(phone_hits.get(ph, 0), c)
            except Exception:
                pass
    # dedupe across photos
    seen, rows = set(), []
    for r in rows_all:
        k = (r["name"].lower(), r["price_vnd"])
        if k in seen: continue
        seen.add(k); rows.append(r)
    rows = M.contextualize(rows, info.get("category") or "")
    q_ok, q_avg, q_good = M.quality(rows)
    parts = [f"cands={len(cands)}(tag {len(tagged)}) read={len(used)} unreadable={len(unread)}"]
    res = {"rid": rid, "rows": rows, "unreadable": unread, "phones": phone_hits}
    if rows and not q_ok:
        parts.append(f"menu=LOW_QUALITY({len(rows)} rows avg_conf={q_avg} good={q_good}; not written, see menu_ocr.json)")
    elif rows:
        cur = P.call("GET", f"{API}/{int(rid)}/menu")["rows"]
        if cur:
            parts.append(f"menu=SKIP(existing {len(cur)} dishes; parsed {len(rows)})")
        elif dry:
            parts.append(f"menu=DRY {len(rows)} rows")
        else:
            body = {"rows": []}
            for r in rows:
                row = {"name": r["name"][:120], "description": r["description"][:200], "new_category_name": r["category"],
                       "kind": r["kind"], "available": True}
                if r.get("price_vnd"): row["price_vnd"] = int(r["price_vnd"])
                else: row["price_vnd"] = None; row["price_on_request"] = True
                body["rows"].append(row)
            out = P.call("PUT", f"{API}/{int(rid)}/menu", json_body=body)
            k = {x: sum(1 for r in rows if r["kind"] == x) for x in ("food", "drink", "side", "other")}
            parts.append(f"menu=+{out.get('created', '?')} ({', '.join(f'{a}={b}' for a, b in k.items() if b)}; cats={len({r['category'] for r in rows})})")
    else:
        parts.append("menu=none" + (" (UNREADABLE)" if cands else " (no menu photo)"))
    if need_phone and phone_hits:
        good = {p: c for p, c in phone_hits.items() if c >= 0.6}
        if len(phone_hits) == 1 and len(good) == 1 and not dry:
            ph = next(iter(good))
            cur = P.call("GET", f"{API}/{int(rid)}")["restaurant"]
            if not (cur.get("phone") or "").strip():
                P.call("PATCH", f"{API}/{int(rid)}", json_body={"phone": M.fmt_phone(ph)})
                parts.append(f"phone=SET {M.fmt_phone(ph)} (ocr c={good[ph]:.2f})")
            else:
                parts.append("phone=server already has one")
        else:
            with open(PHONE_LOG, "a", encoding="utf-8") as f:
                f.write(f"{rid}\t{info.get('name','')}\t" + ", ".join(f"{p}({c:.2f})" for p, c in phone_hits.items()) + f"\t{time.strftime('%F %H:%M')}\n")
            parts.append(f"phone=REVIEW {len(phone_hits)} candidate(s)")
    log(f"#{rid} " + " ".join(parts) + f" {time.time()-t0:.0f}s")
    json.dump(res, open(os.path.join(d, "menu_ocr.dry.json" if dry else "menu_ocr.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if not dry:
        mark_done(rid, info, parts[-1])
    return res


def photo_sig(info):
    return len(info.get("photo_urls") or [])


def mark_done(rid, info, outcome):
    json.dump({"at": time.strftime("%F %T"), "photos": photo_sig(info), "outcome": outcome[:200]},
              open(os.path.join(DATA, str(rid), "MENU_DONE"), "w", encoding="utf-8"), ensure_ascii=False)


def is_done(d, info):
    f = os.path.join(d, "MENU_DONE")
    if not os.path.exists(f): return False
    try:
        m = json.load(open(f, encoding="utf-8"))
        return m.get("photos", 0) >= photo_sig(info)       # revisit only if new photos arrived
    except Exception:
        return True                                          # old plain-text marker = done


def todo():
    ids = []
    for x in os.listdir(DATA):
        if not x.isdigit() or int(x) <= START_FLOOR or int(x) > CEIL: continue
        d = os.path.join(DATA, x)
        if os.path.exists(os.path.join(d, "SAFE_DONE")):
            try:
                info = json.load(open(os.path.join(d, "info.json"), encoding="utf-8"))
            except Exception:
                continue
            if is_done(d, info): continue
            if info.get("stay_draft") or not (info.get("has_menu_photos") or not (info.get("phone") or "").strip()
                                              or len(info.get("photo_urls") or []) >= 3):
                mark_done(int(x), info, "not eligible"); continue
            ids.append(int(x))
    def mt(i):
        try: return os.path.getmtime(os.path.join(DATA, str(i), "SAFE_DONE"))
        except OSError: return 0
    return sorted(ids, key=lambda i: (-mt(i), -i))


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--once", action="store_true"); ap.add_argument("--dry", action="store_true")
    ap.add_argument("--ids", type=int, nargs="*")
    a = ap.parse_args()
    try:
        import torch; torch.set_num_threads(2)
    except Exception:
        pass
    if not a.dry and not a.ids:
        global _LOCKF
        _LOCKF = open(os.path.join(ROOT, "menu_worker.lock"), "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt; _LOCKF.seek(0); msvcrt.locking(_LOCKF.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl; fcntl.flock(_LOCKF, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("another menu_worker holds the lock; exit"); return
    P = Pace()
    log(f"menu worker start (pid {os.getpid()}) (dry={a.dry}, floor={START_FLOOR})")
    while not stopped():
        ids = a.ids if a.ids else todo()
        if not ids:
            if a.once: break
            time.sleep(120); continue
        for n_, rid in enumerate(ids):
            if stopped(): break
            if not a.ids and n_ and n_ % 10 == 0: break      # re-scan so freshly fetched places go first
            try:
                process(P, rid, dry=a.dry)
            except SystemExit:
                raise
            except Exception as e:
                log(f"#{rid} ERROR {type(e).__name__}: {str(e)[:200]}")
                try: mark_done(rid, json.load(open(os.path.join(DATA, str(rid), "info.json"), encoding="utf-8")), f"error {type(e).__name__}")
                except Exception: pass
        if a.once or a.ids: break
    log("menu worker end")


if __name__ == "__main__":
    main()
