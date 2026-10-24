#!/usr/bin/env python3
"""SAFE single-pipeline runner (after the 2026-10-09 server crash).
  prep : list unnamed drafts (sequential reads, 3s gap) + browser-free Maps fetch -> aqd_data/<id>/info.json (NO server writes)
  push : strictly sequential writes; 1 in-flight request; >=3s gap; photo URLs in chunks of <=PHOTO_CHUNK;
         any request >20s / 5xx / 429 / timeout -> wait 5 min, retry the place once; second failure -> stop.
  list : one page window of the unnamed drafts (--start-page/--max-pages/--per-page/--unnamed/--id-suffix, with a
         probe + fallback to the old 24/page listing) + a <queue>.meta.json sidecar the caller uses to continue.
  run  : list order, one place at a time; --limit caps how many places this pass handles.
Run: bash -c 'set -a; . C:/aqd/.aqd_session/creds.env; set +a; python3 C:/aqd/safe_run.py prep|push [--limit N]'"""
import argparse, json, os, random, re, sys, time
ROOT = os.environ.get("AQD_ROOT", "C:/aqd").replace("\\", "/")
sys.path.insert(0, ROOT)
import maps_fetch as mf
import google_extras
import aqd_api
from aqd_api import AQD, AQDError, APIError, API, DATA_DIR, VERIFY, maps_place_id, normalize_fields, _url_hash, _upsize_google
from fast_pass import classify, INTENTIONAL
from olcdec import decode as olc_decode

LOG = ROOT + "/safe_run_log.md"
PROG = ROOT + "/aqd_progress.md"
QUEUE = ROOT + "/queue.json"
GAP, SLOW, HTTP_TO, PHOTO_CHUNK, COOLDOWN = 3.0, 20.0, 45, 5, 300
LIST_PAGE_MAX, LIST_META = 500, ".meta.json"
STOPFILE = ROOT + "/STOP"



def _img_body(chunk, roles=None):
    """#704: image_urls items may be {url, role}; the server stores pending_role and sets cover/logo itself when the
    async download finishes (PUT /cover on a still-pending photo answers 422 invalid_image). roles: {url: role}."""
    roles = roles or {}
    out, used = [], set()
    for c in chunk:
        r = roles.get(c["url"])
        if r in ("cover", "logo") and r not in used:
            used.add(r); out.append({"url": _upsize_google(c["url"]), "role": r})
        else:
            out.append(_upsize_google(c["url"]))
    return {"image_urls": out}


def _pick_roles(todo, rec, cover_key="role", logo_key="role2", want_cover=True, want_logo=True):
    """Choose which new URL carries cover/logo (marked one first, else 1st/2nd new URL)."""
    roles = {}
    if want_cover and todo:
        roles[next((c for c in todo if c.get(cover_key) == "cover"), todo[0])["url"]] = "cover"
    if want_logo:
        rest = [c for c in todo if c["url"] not in roles]
        hit = next((c for c in rest if c.get(logo_key) == "logo"), rest[0] if rest else None)
        if hit: roles[hit["url"]] = "logo"
    return roles


def _rec_photo(c, ph, roles):
    d = {"url": c["url"], "photo_id": int(ph["id"]) if ph else None}
    if ph:
        d["status"] = ph.get("status") or "succeeded"
        if ph.get("pending_role"): d["role"] = ph["pending_role"]
        elif roles.get(c["url"]) and d["status"] == "pending": d["role"] = roles[c["url"]]
    return d


def _server_pending(G, rid):
    """Ids of this place's URL photos the server has not downloaded yet (GET photos?status=pending, #704)."""
    try:
        return {int(p["id"]) for p in (G.req("GET", f"{API}/{rid}/photos?status=pending").get("photos") or [])}
    except Overload:
        raise
    except Exception:
        return None   # unknown -> callers must not PUT


def _put_role(G, rid, role, pid):
    """PUT cover/logo; 422 (photo pending/failed on the server) is skipped, not a place-level error that re-queues it."""
    try:
        G.req("PUT", f"{API}/{rid}/{role}", json_body={"photo_id": pid}); return True
    except APIError as e:
        if e.status == 422: return False
        raise


def _role_queued(rec, role):
    """A pending photo already carries pending_role=role -> the server will assign it; no PUT."""
    return next((v["photo_id"] for v in rec["urls"].values() if v.get("role") == role and v.get("photo_id")), None)

def log(s):
    line = f"- {time.strftime('%Y-%m-%d %H:%M:%S')} {s}"
    open(LOG, "a", encoding="utf-8").write(line + "\n"); print(line, flush=True)


try:  # crawler heartbeat for the daily Slack ops report (an_uong_quanh_day#1021); no AQD_INGEST_TOKEN -> no-op
    import heartbeat as _heartbeat
    HB = _heartbeat.Heartbeat("script_google_map", ROOT)
except Exception:  # an old install without heartbeat.py keeps working
    HB = None


def hb(method, *args, **kwargs):
    """Best effort: a heartbeat problem never stops a pass and is never logged with the token."""
    try:
        return getattr(HB, method)(*args, **kwargs) if HB else None
    except Exception:
        return None


class Overload(Exception):
    pass


class Guard:
    def __init__(self):
        self.a = AQD(); self.last = 0.0; self.slow = None; self.n = 0; self.tt = 0.0; self.max = 0.0

    def req(self, method, path, **kw):
        w = GAP - (time.time() - self.last)
        if w > 0: time.sleep(w)
        t = time.time()
        try:
            try:
                return self.a.call(method, path, retries=0, timeout=HTTP_TO, retry_status=(), retry_network=False, **kw)
            except AQDError as e0:
                if isinstance(e0, APIError) or method not in ("GET", "PUT"): raise
                time.sleep(10)
                return self.a.call(method, path, retries=0, timeout=HTTP_TO, retry_status=(), retry_network=False, **kw)
        except APIError as e:
            if e.status >= 500 or e.status == 429:
                raise Overload(f"{method} {path} HTTP {e.status}")
            raise
        except AQDError as e:
            if not isinstance(e, APIError):
                raise Overload(f"{method} {path} {e}")
            raise
        finally:
            dt = time.time() - t; self.last = time.time(); self.n += 1; self.tt += dt; self.max = max(self.max, dt)
            if dt > SLOW:
                self.slow = f"{method} {path} took {dt:.1f}s"


_PW = {}
_CDP = "http://127.0.0.1:" + (os.environ.get("AQD_CDP_PORT") or "9222")
_JS = """async (pid)=>{const r=await fetch('/maps/place/?q=place_id:'+pid+'&hl=vi',{credentials:'include'});const t=await r.text();
const m=t.match(/(\\/maps\\/preview\\/place\\?[^"'\\s]+)/);if(!m)return {err:'nopreview',status:r.status,url:r.url};
const u=m[1].replace(/&amp;/g,'&');const r2=await fetch(u,{credentials:'include'});return {status:r2.status,body:await r2.text()}}"""


def _pw_reset():
    """Tear down the Playwright driver completely. A sync_playwright() left started after a failure keeps its asyncio
    loop registered on this thread, so the NEXT sync_playwright().start() raises "Sync API inside the asyncio loop"
    (2026-10-18: one Page.goto timeout -> every later place fell back to thin HTTP)."""
    _PW.pop("pg", None); b = _PW.pop("b", None); p = _PW.pop("p", None)
    try:
        if b: b.close()
    except Exception: pass
    try:
        if p: p.stop()
    except Exception: pass


def _pw_page():
    if "pg" in _PW:
        try:
            if not _PW["pg"].is_closed(): return _PW["pg"]
        except Exception: pass
        _pw_reset()
    from playwright.sync_api import sync_playwright
    try:
        _PW["p"] = sync_playwright().start()
        b = _PW["b"] = _PW["p"].chromium.connect_over_cdp(__import__("cdp_proxy").endpoint())
        ctx = b.contexts[0]; pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        _PW["pg"] = pg
    except BaseException:
        _pw_reset(); raise
    return pg


def _maps_home(pg):
    # Maps rarely fires "load" within 30s on Chrome 155; DOM ready is all the fetch() below needs.
    pg.goto("https://www.google.com/maps?hl=vi", wait_until="domcontentloaded", timeout=60000); pg.wait_for_timeout(2000)


def browser_place_json(pid):
    """Same two requests as maps_fetch, but made by the signed-in Chrome (CDP port 9222) itself."""
    try:
        pg = _pw_page()
        if "google.com" not in (pg.url or ""): _maps_home(pg)
        try: r = pg.evaluate(_JS, pid)
        except Exception as e:
            if "context was destroyed" not in str(e): raise
            pg.wait_for_timeout(3000); r = pg.evaluate(_JS, pid)   # Maps' own redirect after DOM ready
    except mf.Blocked:
        raise
    except Exception:
        _pw_reset(); raise   # never leave a half-started driver behind
    t = r.get("body") or ""
    if not t or r.get("status") != 200 or "/sorry/" in (r.get("url") or ""):
        raise mf.Blocked(f"browser fetch {r.get('status')} {r.get('err')}")
    return json.loads(t[t.index("\n") + 1:] if t.startswith(")]}'") else t)


def build_info(row):
    pid = maps_place_id(row)
    if not pid:
        return None, "no place_id"
    gal = None; via = "http"
    import photos as PH
    if os.environ.get("AQD_CDP"):
        try:
            d = browser_place_json(pid); via = "chrome"
            _p6 = mf.g(d, 6)
            gal = PH.gallery(_PW["pg"], pid, expect_name=mf.g(_p6, 11), hexid=mf.g(_p6, 10))
            if gal == "SKIP":
                return None, f"gallery unsure, skipped: {PH.SKIP_REASON.get('r')}"
            if gal is None:
                raise mf.Blocked("captcha/unusual traffic in signed-in Chrome")
        except mf.Blocked:
            raise
        except ImportError as e:
            # Missing websockets / cdp_proxy is an install bug — do NOT silently fall back to thin HTTP.
            print(
                f"ERROR: cdp_proxy import failed ({type(e).__name__}: {e}). "
                f"Install websockets (pip install websockets) or re-run the installer. "
                f"Not falling back to plain HTTP.",
                flush=True,
            )
            raise
        except Exception as e:
            print(f"chrome path failed ({type(e).__name__}: {str(e)[:100]}); plain HTTP fallback", flush=True)
            _pw_reset()
            d = mf.fetch_place_json(pid); via = "http"; gal = None
    else:
        d = mf.fetch_place_json(pid)
    p = mf.g(d, 6)
    if not isinstance(p, list):
        return None, "no place block"
    info = mf.parse(p, pid)
    info["_meta"]["via"] = via
    if gal is not None and via == "chrome":
        L = getattr(PH, "LAST_LINKS", {})
        if L.get("facebook") and not info.get("facebook_url"): info["facebook_url"] = L["facebook"]
        if L.get("tiktok") and not info.get("tiktok_url"): info["tiktok_url"] = L["tiktok"]
    if "tiktok.com/@" in (info.get("website") or "") and not info.get("tiktok_url"):
        info["tiktok_url"] = info["website"]; info["website"] = ""
    if gal:
        have = {x["url"].split("=")[0] for x in info["photo_urls"]}
        info["photo_urls"] += [{"url": u, "kind": "gallery"} for u in sorted(gal) if u.split("=")[0] not in have]
        info["_meta"]["gallery"] = len(gal)
    if via == "http" and info["_meta"].get("thin") and os.environ.get("AQD_CDP"):
        try:
            time.sleep(5)
            p2 = mf.g(browser_place_json(pid), 6)
            if isinstance(p2, list):
                i2 = mf.parse(p2, pid)
                i2["_meta"]["via"] = "chrome"
                if not i2["_meta"].get("thin") or len(i2["photo_urls"]) > len(info["photo_urls"]):
                    info = i2
        except Exception as e:
            print(f"browser fallback failed: {e}", flush=True)
    meta = info.pop("_meta"); photos = info.pop("photo_urls")
    extra = meta.pop("extra", None) or {}
    _review_photo_filter(extra)
    info["_extra"] = extra
    types = [t for t in (info.get("google_types_text") or "").split(", ") if t not in ("point_of_interest", "establishment")]
    info["_name_is_address"] = bool(meta.get("name_is_address"))
    cat, cuisine, reason = classify(info, types)
    info.pop("_name_is_address")
    info["category"], info["cuisine"] = cat, cuisine
    if not info.get("google_types_text") or info["google_types_text"] == "point_of_interest, establishment":
        info.pop("google_types_text", None); info.pop("google_primary_type", None)
    pc = r"^[23456789CFGHJMPQRVWX]{4,8}\+[23456789CFGHJMPQRVWX]{2,3},\s*"
    for k in ("short_address", "address_route"):
        if re.match(pc, info.get(k) or ""): info[k] = re.sub(pc, "", info[k])
    if not info.get("latitude") and info.get("plus_code_global"):
        ll = olc_decode(info["plus_code_global"])
        if ll: info["latitude"], info["longitude"] = str(ll[0]), str(ll[1])
    if not info.get("latitude"): reason = reason or "no coordinates"
    if not info.get("name"): reason = reason or "no name from Maps"
    if info.get("business_status") == "CLOSED_PERMANENTLY": reason = reason or "permanently closed"
    # photos: drop videos only; the API classifies every photo (#855, no local model)
    ph = [x for x in photos if "Video" not in (x.get("kind") or "")]
    photos_skipped = 0
    meta["photos_skipped"] = photos_skipped
    kinds = [x.get("kind") or "" for x in ph]
    out = [{"url": x["url"] + "=s1600", "kind": x.get("kind") or ""} for x in ph]
    if out:
        cv = next((i for i, k in enumerate(kinds) if re.search(r"Bên ngoài|Mặt tiền|Ngoại thất|Bảng hiệu", k)), None)
        if cv is None: cv = next((i for i, k in enumerate(kinds) if "Của chủ sở hữu" in k), None)
        if cv is None: cv = next((i for i, k in enumerate(kinds) if "Tất cả" in k), 0)
        lg = next((i for i, k in enumerate(kinds) if ("Thực phẩm" in k or "Đồ uống" in k) and i != cv), None)
        if lg is None: lg = next((i for i in range(len(out)) if i != cv), cv)
        out[cv]["role"] = "cover"; out[lg]["role2"] = "logo"
    info["photo_urls"] = out
    if reason: info["stay_draft"] = True; info["stay_reason"] = reason
    info["has_menu_photos"] = any("Thực đơn" in k for k in kinds)
    info["_maps"] = {"via": meta.get("via", "http"), "gallery": meta.get("gallery"), "place_id": pid, "thin": meta.get("thin"), "count": meta.get("maps_photo_count_label"), "cats": meta.get("photo_categories"), "photos_skipped": meta.get("photos_skipped", 0)}
    return info, reason


def cmd_prep(args):
    G = Guard()
    rows = []
    for pg in range(1, args.pages + 1):
        b = G.req("GET", f"{API}?status=draft&page={pg}")
        if pg == 1 or pg % 20 == 0: log(f"LIST page {pg} (total {b.get('total_count')}) ...")
        rows += [r for r in b["restaurants"] if not (r.get("name") or "").strip()]
        if not b["restaurants"]: break
    rows = sorted({r["id"]: r for r in rows}.values(), key=lambda r: -r["id"])
    json.dump(rows, open(args.queue, "w", encoding="utf-8"), ensure_ascii=False)
    log(f"PREP listed {len(rows)} unnamed drafts, top {rows[0]['id'] if rows else None}; reads={G.n} avg={G.tt/max(1,G.n):.2f}s max={G.max:.1f}s")
    done = 0; blocked = False
    for r in rows:
        rid = r["id"]
        if rid in INTENTIONAL: continue
        f = os.path.join(DATA_DIR, str(rid), "info.json")
        if os.path.exists(f): continue
        if args.limit and done >= args.limit: break
        try:
            info, reason = build_info(r)
        except mf.Blocked as e:
            if blocked: log(f"#{rid} Maps BLOCKED again: {e}; stop prep"); break
            blocked = True; log(f"#{rid} Maps BLOCKED: {e}; wait 15 min"); time.sleep(900); continue
        except Exception as e:
            log(f"#{rid} PREP ERROR {type(e).__name__}: {str(e)[:150]}"); continue
        if info is None:
            log(f"#{rid} PREP SKIP {reason}"); continue
        os.makedirs(os.path.dirname(f), exist_ok=True)
        json.dump(info, open(f + ".tmp", "w", encoding="utf-8"), ensure_ascii=False, indent=1); os.replace(f + ".tmp", f)
        open(os.path.join(DATA_DIR, str(rid), "SAFE_PREP"), "w", encoding="utf-8").write(time.strftime("%F %T"))
        done += 1
        log(f"#{rid} PREP {info.get('name','')[:40]!r} cat={info['category']} photos={len(info['photo_urls'])}/{info['_maps']['count']} "
            f"phone={'y' if info.get('phone') else 'n'} hours={'y' if info.get('opening_hours_text') else 'n'} menu_ph={info['has_menu_photos']}"
            + (f" STAY_DRAFT:{reason}" if reason else ""))
        time.sleep(args.sleep)
    log(f"PREP END prepared {done}")


SKIP_KEYS = ("_extra", "cover", "logo", "stay_draft", "stay_reason", "note", "photo_urls", "has_menu_photos", "_maps")


REVIEW_PHOTO_CAP = int(os.environ.get("AQD_REVIEW_PHOTO_CAP", "12"))


def _review_photo_filter(extra):
    """Keep review photos up to the cap (no local model; #855); nothing is uploaded, only stored in extra.json."""
    n = 0
    for rv in extra.get("reviews") or []:
        keep = []
        for u in rv.pop("photo_urls", []) or []:
            if n >= REVIEW_PHOTO_CAP: break
            n += 1
            keep.append({"url": u})
        rv["photos"] = keep


def push_extra(G, rid, folder, info, rec, save):
    """extra.json locally (always), then About / Google reviews / weekly hours to the /v1 admin API
    (google_extras.push: PATCH google_attributes+description, PUT google_reviews, PUT opening_hours source=google;
    409 owner_confirmed = skip). Returns the log part."""
    ex = dict(info.get("_extra") or {}); ex["saved_at"] = time.strftime("%F %T")
    json.dump(ex, open(os.path.join(folder, "extra.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    head = f"extra(r={ex.get('google_rating')},rv={len(ex.get('reviews') or [])},about={sum(len(x['items']) for x in ex.get('about') or [])})"
    if os.environ.get("AQD_PUSH_EXTRAS", "1") in ("0", "false", "no"):
        return head
    try:
        cur = G.req("GET", f"{API}/{rid}")["restaurant"] if (ex.get("description") or "").strip() else None
        res = google_extras.push(G.req, rid, ex, current=cur, api_error=APIError)
        rec["extras_v1"] = {"at": time.strftime("%F %T"), "result": res}; save()
        return f"{head} {res}"
    except Overload:
        raise
    except Exception as e:
        return f"{head} extras_failed:{type(e).__name__}:{e}"[:300]


def push_one(G, rid):
    folder = os.path.join(DATA_DIR, str(rid))
    info = json.load(open(os.path.join(folder, "info.json"), encoding="utf-8"))
    recp = os.path.join(folder, "applied.json")
    rec = json.load(open(recp, encoding="utf-8")) if os.path.exists(recp) else {}
    rec.setdefault("urls", {})
    save = lambda: (json.dump(rec, open(recp + ".tmp", "w", encoding="utf-8"), ensure_ascii=False, indent=1), os.replace(recp + ".tmp", recp))
    t0 = time.time(); parts = []; warn = []
    cur = G.req("GET", f"{API}/{rid}")["restaurant"]
    if cur.get("status") != "draft" or ((cur.get("name") or "").strip() and not rec.get("info")):
        return "skip", f"not an unnamed draft any more (status={cur.get('status')} name={cur.get('name')!r})"
    # 1) info PATCH (no status)
    fields, w = normalize_fields({k: v for k, v in info.items() if k not in SKIP_KEYS})
    warn += w
    after = G.req("PATCH", f"{API}/{rid}", json_body=fields)["restaurant"]
    rec["info"] = {"at": time.strftime("%F %T"), "sent": len(fields)}; save()
    parts.append(f"info={len(fields)}f")
    parts.append(push_extra(G, rid, folder, info, rec, save))
    # 2) photos, chunks of <=5, sequential
    items = info.get("photo_urls") or []
    todo = [it for it in items if _url_hash(it["url"]) not in rec["urls"]]
    added = failed = 0
    roles = _pick_roles(todo, rec, want_cover=not rec.get("cover"), want_logo=not rec.get("logo"))
    for i in range(0, len(todo), PHOTO_CHUNK):
        chunk = todo[i:i + PHOTO_CHUNK]
        body = G.req("POST", f"{API}/{rid}/photos", json_body=_img_body(chunk, roles), raw_status=(422,))
        photos, errors = body.get("photos") or [], body.get("errors") or []
        bad = {e.get("url") for e in errors if isinstance(e, dict)}
        it = iter(photos)
        for c in chunk:
            if _upsize_google(c["url"]) in bad: failed += 1; continue
            p = next(it, None)
            if p: rec["urls"][_url_hash(c["url"])] = _rec_photo(c, p, roles); added += 1
        save()
    parts.append(f"photos=+{added}" + (f"/fail{failed}" if failed else "") + f"/of{len(items)}")
    # No Street View fallback (#681): Google terms forbid re-hosting Street View imagery, so a place with no photo keeps no cover.
    # 3) cover / logo
    for role, key in (("cover", "role"), ("logo", "role2")):
        hit = next((c for c in items if c.get(key) == role and _url_hash(c["url"]) in rec["urls"]), None)
        if not hit:  # fallback: first/second uploaded
            up = [c for c in items if (rec["urls"].get(_url_hash(c["url"])) or {}).get("photo_id")]
            hit = (up[0] if role == "cover" else (up[1] if len(up) > 1 else (up[0] if up else None))) if up else None
        q = _role_queued(rec, role)
        if q and not rec.get(role):
            rec[role] = q; save(); parts.append(f"{role}={q}(pending_role)"); continue
        if hit and rec.get(role) != rec["urls"][_url_hash(hit["url"])]["photo_id"]:
            pid = rec["urls"][_url_hash(hit["url"])]["photo_id"]
            pend = _server_pending(G, rid)
            if pend is None or pid in pend: continue
            if not _put_role(G, rid, role, pid): parts.append(f"{role}_skip={pid}"); continue
            rec[role] = pid; save()
            parts.append(f"{role}={pid}")
    # 4) status
    if info.get("stay_draft"):
        parts.append(f"status=draft(stay:{info.get('stay_reason')})")
    else:
        r = G.req("PATCH", f"{API}/{rid}", json_body=dict(VERIFY))["restaurant"]
        ok = all(r.get(k) == v for k, v in VERIFY.items())
        parts.append(f"status={r.get('status')}"); rec["validated"] = ok; save()
        if not ok: warn.append("validate_failed")
    if info.get("has_menu_photos"): parts.append("menu_photos=yes(todo)")
    parts.append(f"{time.time()-t0:.1f}s")
    return ("ok" if not warn else "warn"), " ".join(parts) + (" | " + "; ".join(warn) if warn else "")


def peak():
    hm = int(time.strftime("%H")) * 60 + int(time.strftime("%M"))
    return 630 <= hm < 810 or 1020 <= hm < 1230


def cmd_push(args):
    G = Guard()
    ids = sorted((int(d) for d in os.listdir(DATA_DIR) if d.isdigit() and os.path.exists(os.path.join(DATA_DIR, d, "SAFE_PREP"))
                  and not os.path.exists(os.path.join(DATA_DIR, d, "SAFE_DONE"))), reverse=True)
    if args.ids: ids = [i for i in args.ids if i in ids] if not args.force else args.ids
    if args.limit: ids = ids[:args.limit]
    if len(ids) > 1: log(f"PUSH START {len(ids)} ids: {ids[:5]}...")
    n = 0
    for rid in ids:
        if os.path.exists(STOPFILE): log("STOP file present; stopping"); break
        while peak() and not args.ignore_peak:
            log("peak hours; sleeping 10 min"); time.sleep(600)
        tries = 0
        while True:
            try:
                G.slow = None
                kind, msg = push_one(G, rid)
                if G.slow:
                    raise Overload("slow: " + G.slow)
                break
            except Overload as e:
                tries += 1
                log(f"#{rid} OVERLOAD ({e}) try {tries}")
                if tries >= 2:
                    hb("record_place", False); hb("note", "other", f"server overload: {str(e)[:150]}")
                    hb("beat", f"#{rid} STOP: server overload", status="error", force=True)
                    log(f"#{rid} second failure -> STOP ENTIRELY"); open(PROG, "a", encoding="utf-8").write(f"{rid} | safe | STOPPED overload: {e}\n"); open(STOPFILE, "w", encoding="utf-8").write(str(e)); return "STOP"
                time.sleep(COOLDOWN)
            except Exception as e:
                kind, msg = "error", f"{type(e).__name__}: {str(e)[:200]}"; break
        n += 1
        if kind in ("ok", "warn"):
            open(os.path.join(DATA_DIR, str(rid), "SAFE_DONE"), "w", encoding="utf-8").write(time.strftime("%F %T"))
        info = json.load(open(os.path.join(DATA_DIR, str(rid), "info.json"), encoding="utf-8"))
        line = f"{rid} | safe | {info.get('name','')} | {info.get('category')} | {kind.upper()} {msg} - {time.strftime('%F %H:%M')}"
        open(PROG, "a", encoding="utf-8").write(line + "\n"); log(f"#{rid} {kind.upper()} {msg}")
        hb("record_result", kind, msg); hb("beat", f"#{rid} {kind}")
        if n % 20 == 0:
            log(f"health: reqs={G.n} avg={G.tt/max(1,G.n):.2f}s max={G.max:.1f}s"); time.sleep(30)
    if len(ids) > 1: log(f"PUSH END {n} places; reqs={G.n} avg={G.tt/max(1,G.n):.2f}s max={G.max:.1f}s")


BACKFILL = ROOT + "/aqd_photo_backfill.txt"
MAPS_MIN_GAP, MAPS_JITTER = 30.0, 15.0
BACKOFF_MIN = [30, 60, 120]


def limited_reason(info):
    m = info.get("_maps") or {}
    cnt = int(re.sub(r"\D", "", m.get("count") or "") or 0)
    emb = len(info.get("photo_urls") or [])
    why = []
    if m.get("thin"): why.append("thin(no types/photo tabs)")
    if not info.get("google_types_text") or info.get("google_types_text") == "point_of_interest, establishment":
        why.append("no types")
    if cnt >= 6 and emb < min(cnt, 10) * 0.6: why.append(f"photos {emb}/{cnt}")
    return ", ".join(dict.fromkeys(why)), cnt, emb


def nap(sec):
    end = time.time() + sec
    while time.time() < end:
        if os.path.exists(STOPFILE): return False
        time.sleep(min(30, max(0, end - time.time())))
    return True


def _suffix_param(raw):
    return ",".join(d for d in re.split(r"[,\s]+", raw or "") if d)


def eta_line(total, per_page, max_pages, gap=GAP):
    """The one line printed right before the listing starts, so nobody thinks it hung."""
    pages = max(1, -(-int(total or 0) // max(1, int(per_page or 24))))
    head = f"listing {pages} pages (~{pages * gap / 60.0:.0f} min at {gap:.0f}s/page)"
    if max_pages and pages > max_pages:
        return (head + f" - listing only the first {max_pages}/{pages} pages now"
                       f" (~{max_pages * gap / 60.0:.0f} min), fetching starts right after")
    return head + " - fetching starts right after"


def list_query(args, per_page, pg, mode):
    if mode == "paged":
        q = f"?status=draft&page={pg}&per_page={per_page}&summary=1"
        if args.unnamed:
            q += "&unnamed=1"
        if args.id_suffix:
            q += "&id_suffix=" + _suffix_param(args.id_suffix)
        return q
    return f"?status=draft&page={pg}"


def list_page(G, args, per_page, pg, mode):
    return G.req("GET", API + list_query(args, per_page, pg, mode))


def open_listing(G, args, requested, start):
    """Probe the new paging params on the first page of the window; never fail when this server has none."""
    try:
        body = list_page(G, args, requested, start, "paged")
    except APIError as e:
        log(f"LIST new paging params refused ({e}); using the old 24/page listing")
        return "legacy", list_page(G, args, requested, start, "legacy")
    if int(body.get("per_page") or 0) == requested:
        return "paged", body
    log("LIST server ignores per_page/has_more; using the old 24/page listing")
    return "legacy", body


def cmd_list(args):
    G = Guard()
    requested = max(1, min(50, args.per_page or 24))
    first = max(1, int(args.start_page or 1))
    window = max(0, int(args.max_pages or 0))
    mode, body = open_listing(G, args, requested, first)
    per_page = requested if mode == "paged" else 24
    last = first + window - 1 if window else LIST_PAGE_MAX
    log(f"LIST mode={mode} per_page={per_page} start_page={first} max_pages={window or 'all'}"
        + (f" id_suffix={_suffix_param(args.id_suffix)}" if args.id_suffix else ""))
    rows, pg, seen_min, scanned, capped, total = [], first - 1, None, 0, False, None
    while True:
        pg += 1
        batch = (body or {}).get("restaurants") or []
        if not batch:
            break
        scanned += 1
        ids = [r["id"] for r in batch]
        seen_min = min(ids) if seen_min is None else min(seen_min, min(ids))
        if total is None:
            total = body.get("total_count")
        rows += [r for r in batch if not (r.get("name") or "").strip() and args.floor < r["id"] <= args.ceil]
        if pg == first:
            log(eta_line(total, per_page, window, GAP))
        if pg == first or pg % 20 == 0:
            log(f"LIST page {pg}/{max(1, (int(total or 0) // per_page) + 1)} (total {total}) ...")
        if min(ids) <= args.floor or pg >= LIST_PAGE_MAX:
            break
        if mode == "paged" and body.get("has_more") is False:
            break
        if window and pg >= last:
            capped = True
            break
        body = list_page(G, args, per_page, pg + 1, mode)
    rows = sorted({r["id"]: r for r in rows}.values(), key=lambda r: -r["id"])
    json.dump(rows, open(args.queue, "w", encoding="utf-8"), ensure_ascii=False)
    meta = {"mode": mode, "per_page": per_page, "start_page": first, "max_pages": window, "pages": scanned,
            "min_id": seen_min, "capped": capped, "total_count": total}
    json.dump(meta, open(args.queue + LIST_META, "w", encoding="utf-8"), ensure_ascii=False)
    log(f"LIST {len(rows)} unnamed drafts > {args.floor}: {rows[0]['id'] if rows else None}..{rows[-1]['id'] if rows else None}; "
        f"pages={scanned} capped={capped} avg={G.tt/max(1,G.n):.2f}s max={G.max:.1f}s")
    return meta


def cmd_run(args):
    rows = json.load(open(args.queue, encoding="utf-8")); GR = Guard()
    last_maps = 0.0; streak = 0; level = 0; done = 0; limit = max(0, int(args.limit or 0))
    for r in rows:
        if r["id"] <= args.floor: log(f"reached floor {args.floor}; stop"); break
        if os.path.exists(STOPFILE): log("STOP file; end run"); break
        rid = r["id"]
        if rid in INTENTIONAL or os.path.exists(os.path.join(DATA_DIR, str(rid), "SAFE_DONE")): continue
        fetched = False
        if not os.path.exists(os.path.join(DATA_DIR, str(rid), "SAFE_PREP")):
            fetched = True
            if os.path.exists(os.path.join(DATA_DIR, str(rid), "info.json")): continue   # someone else's
            try:
                cur = GR.req("GET", f"{API}/{rid}")["restaurant"]
            except Overload as e:
                log(f"#{rid} OVERLOAD on pre-check ({e}); wait 5 min"); time.sleep(COOLDOWN)
                try: cur = GR.req("GET", f"{API}/{rid}")["restaurant"]
                except Overload as e2:
                    log(f"#{rid} second failure -> STOP ENTIRELY"); open(STOPFILE, "w", encoding="utf-8").write(str(e2)); return
            if (cur.get("name") or "").strip() or cur.get("status") != "draft":
                log(f"#{rid} SKIP pre-check: taken (status={cur.get('status')} name={cur.get('name')!r})")
                _taken = globals().get("_TAKEN", 0) + 1; globals()["_TAKEN"] = _taken
                if int(os.environ.get("AQD_MEET_STOP", "0") or 0) and _taken >= int(os.environ["AQD_MEET_STOP"]):
                    log(f"MEET: {_taken} places in a row already done by the other machine; stop"); break
                continue
            globals()["_TAKEN"] = 0
            w = MAPS_MIN_GAP + random.uniform(0, MAPS_JITTER) - (time.time() - last_maps)
            if w > 0 and not nap(w): log("STOP file; end run"); break
            last_maps = time.time()
            try:
                info, reason = build_info(r)
            except mf.Blocked as e:
                log(f"#{rid} Maps BLOCKED {e}; back off 2h")
                hb("note", "ip_blocked", f"Maps BLOCKED: {str(e)[:150]}")
                hb("beat", "Maps BLOCKED, back off 2h", status="blocked", force=True)
                if os.environ.get("AQD_ALT"):
                    open(ROOT + "/fetch_backoff.txt", "w", encoding="utf-8").write(str(time.time() + 7200)); sys.exit(3)
                time.sleep(7200); continue
            except Exception as e:
                log(f"#{rid} PREP ERROR {type(e).__name__}: {str(e)[:150]}"); continue
            if info is None:
                log(f"#{rid} PREP SKIP {reason}")
                # an empty page (no place block) is the only non-exception block signal; 429/sorry/consent/captcha raise mf.Blocked above
                if reason == "no place block":
                    streak += 1
                    if streak >= 3:
                        m = BACKOFF_MIN[min(level, 2)]; level += 1
                        log(f"Maps empty page {streak} in a row (last #{rid}); pause Maps {m} min (level {level})")
                        hb("note", "rate_limited", f"Maps empty page {streak} in a row; pause {m} min")
                        hb("beat", f"Maps paused {m} min", status="blocked", force=True)
                        if os.environ.get("AQD_ALT"):
                            open(ROOT + "/fetch_backoff.txt", "w", encoding="utf-8").write(str(time.time() + m * 60)); sys.exit(3)
                        if not nap(m * 60): log("STOP file; end run"); break
                continue
            lim, cnt, emb = limited_reason(info)
            m_ = info.get("_maps") or {}
            notypes = not info.get("google_types_text") or info.get("google_types_text") == "point_of_interest, establishment"
            # A thin/sparse payload is NOT a block: checked 2026-10-18, #23701/#23703/#23705 were "thin" over plain HTTP
            # yet loaded fully in Chrome with category + tabs, no captcha. Only captcha / unusual traffic / 429 (mf.Blocked)
            # or an empty page count toward the backoff streak.
            truly = False
            if truly: streak += 1
            elif streak or level:
                if level: log(f"Maps full data back at #{rid}; normal pace")
                streak = 0; level = 0
            if lim:
                open(BACKFILL, "a", encoding="utf-8").write(f"{rid} {cnt} {emb} # safe_run limited: {lim} {time.strftime('%F %H:%M')}\n")

            f = os.path.join(DATA_DIR, str(rid), "info.json"); os.makedirs(os.path.dirname(f), exist_ok=True)
            json.dump(info, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            open(os.path.join(DATA_DIR, str(rid), "SAFE_PREP"), "w", encoding="utf-8").write(time.strftime("%F %T"))
            log(f"#{rid} PREP {info.get('name','')[:40]!r} cat={info['category']} photos={len(info['photo_urls'])}/{info['_maps']['count']} thin={info['_maps']['thin']} via={info['_maps']['via']} gallery={info['_maps'].get('gallery')} photos_skipped={info['_maps'].get('photos_skipped',0)}" + (f" STAY_DRAFT:{reason}" if reason else ""))
        args.ids = [rid]; args.force = True; args.limit = 0
        if cmd_push(args) == "STOP": return
        if fetched and streak >= 3:
            m = BACKOFF_MIN[min(level, 2)]; level += 1
            log(f"Maps truly limited {streak} in a row (last #{rid}: {lim}); pause Maps {m} min (level {level})")
            hb("note", "rate_limited", f"Maps limited {streak} in a row; pause {m} min")
            hb("beat", f"Maps paused {m} min", status="blocked", force=True)
            if os.environ.get("AQD_ALT"):
                open(ROOT + "/fetch_backoff.txt", "w", encoding="utf-8").write(str(time.time() + m * 60)); sys.exit(3)
            if not nap(m * 60): log("STOP file; end run"); break
        if fetched:
            done += 1
            if limit and done >= limit:
                log(f"run limit {limit} reached ({done} places this pass); re-list to continue")
                break


def cmd_backfill(args):
    """Re-fetch Maps for ids in BACKFILL; push only photo URLs not uploaded yet (per applied.json). No info/status change."""
    donef = BACKFILL + ".done"
    done = {l.split()[0] for l in open(donef, encoding="utf-8")} if os.path.exists(donef) else set()
    ids = []
    for l in open(BACKFILL, encoding="utf-8"):
        t = l.split()
        if t and t[0].isdigit() and t[0] not in done and int(t[0]) not in ids: ids.append(int(t[0]))
    ids.sort(reverse=True)
    G = Guard(); last_maps = 0.0; streak = 0; level = 0
    log(f"BACKFILL START {len(ids)} ids")
    for rid in ids:
        if os.path.exists(STOPFILE): log("STOP file; end backfill"); break
        recp = os.path.join(DATA_DIR, str(rid), "applied.json")
        if not os.path.exists(recp): continue
        rec = json.load(open(recp, encoding="utf-8")); rec.setdefault("urls", {})
        w = MAPS_MIN_GAP + random.uniform(0, MAPS_JITTER) - (time.time() - last_maps)
        if w > 0 and not nap(w): break
        try:
            cur = G.req("GET", f"{API}/{rid}")["restaurant"]
            last_maps = time.time()
            info, reason = build_info(cur)
        except Overload as e:
            log(f"#{rid} BACKFILL OVERLOAD {e}; wait 5 min"); nap(COOLDOWN); continue
        except Exception as e:
            log(f"#{rid} BACKFILL ERROR {type(e).__name__}: {str(e)[:120]}"); continue
        if info is None: continue
        m_ = info["_maps"]
        if m_.get("thin"):
            streak += 1; log(f"#{rid} BACKFILL still limited")
            if streak >= 3:
                m = BACKOFF_MIN[min(level, 2)]; level += 1; log(f"backfill: limited x{streak}; pause {m} min")
                if not nap(m * 60): break
            continue
        streak = level = 0
        new = [it for it in info["photo_urls"] if _url_hash(it["url"]) not in rec["urls"]]
        added = 0
        try:
            for i in range(0, len(new), PHOTO_CHUNK):
                chunk = new[i:i + PHOTO_CHUNK]
                body = G.req("POST", f"{API}/{rid}/photos", json_body=_img_body(chunk), raw_status=(422,))
                bad = {e.get("url") for e in body.get("errors") or [] if isinstance(e, dict)}; it_ = iter(body.get("photos") or [])
                for c in chunk:
                    if _upsize_google(c["url"]) in bad: continue
                    ph = next(it_, None)
                    if ph: rec["urls"][_url_hash(c["url"])] = _rec_photo(c, ph, {}); added += 1
                json.dump(rec, open(recp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        except Overload as e:
            log(f"#{rid} BACKFILL OVERLOAD {e}; wait 5 min"); nap(COOLDOWN); continue
        open(donef, "a", encoding="utf-8").write(f"{rid} +{added} of {len(info['photo_urls'])} {time.strftime('%F %H:%M')}\n")
        log(f"#{rid} BACKFILL +{added} new photos (maps {len(info['photo_urls'])}/{m_.get('count')})")


PRIORITY = ROOT + "/refetch_priority.txt"
REFRESH_KEYS = ("category", "cuisine", "google_types_text", "google_primary_type", "google_primary_type_label")


def _pbase(u):
    return (u or "").split("=")[0]


def priority_pending():
    """Ids in refetch_priority.txt (first token per line) not yet in .done and tried < 3 times."""
    if not os.path.exists(PRIORITY): return []
    done = {l.split()[0] for l in open(PRIORITY + ".done", encoding="utf-8")} if os.path.exists(PRIORITY + ".done") else set()
    tries = json.load(open(PRIORITY + ".tries.json", encoding="utf-8")) if os.path.exists(PRIORITY + ".tries.json") else {}
    ids = []
    for l in open(PRIORITY, encoding="utf-8"):
        t = l.split()
        if t and t[0].isdigit() and t[0] not in done and tries.get(t[0], 0) < 3 and int(t[0]) not in ids: ids.append(int(t[0]))
    return ids


def _dhash(url, size=8):
    import io, requests as _rq
    from PIL import Image
    r = _rq.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"}); r.raise_for_status()
    im = Image.open(io.BytesIO(r.content)).convert("L").resize((size + 1, size))
    px = list(im.getdata()); bits = 0
    for y in range(size):
        for x in range(size):
            bits = (bits << 1) | (px[y * (size + 1) + x] > px[y * (size + 1) + x + 1])
    return bits


def _server_hashes(G, rid):
    """dHash of every photo already on the server (Google grass-cs URLs change per session, so URLs can't dedupe)."""
    hs, page = [], 1
    while page < 20:
        b = G.req("GET", f"{API}/{rid}/photos?page={page}&per_page=50")
        ps = b.get("photos") or []
        for p in ps:
            u = p.get("thumb_url") or p.get("url")
            try: hs.append(_dhash(u))
            except Exception: pass
        if not ps or not b.get("has_more"): break
        page += 1
    return hs


def _near(h, hs, bits=10):
    return any(bin(h ^ x).count("1") <= bits for x in hs)


def cmd_refetch(args):
    """FULL re-fetch (Chrome) of the ids in refetch_priority.txt that were fetched thin, for places already pushed.
    - info: fills fields the server has empty; category/cuisine/types only when the server still holds the old thin value
      (never overrides a human edit); status and flags are never touched.
    - photos: same as run (videos dropped, no local filter); skips any photo whose base URL is already uploaded (applied.json) -> no duplicates.
    - extra.json (reviews/About/hours) rewritten; opening periods only when the server has none.
    - menu: MENU_DONE removed when menu photos newly appear, so menu_worker reads them.
    Parity is enforced (AQD_PARITY). A result still via http/thin is not marked done (max 3 tries)."""
    par = os.environ.get("AQD_PARITY") or ("even" if os.name == "nt" else "odd")
    ids = [i for i in priority_pending() if i % 2 == (0 if par == "even" else 1)]
    if args.limit: ids = ids[:args.limit]
    if not ids: return
    tf = PRIORITY + ".tries.json"; tries = json.load(open(tf, encoding="utf-8")) if os.path.exists(tf) else {}
    G = Guard(); last_maps = 0.0
    log(f"REFETCH START {len(ids)} ids #{ids[0]}..#{ids[-1]}")
    for rid in ids:
        if os.path.exists(STOPFILE): log("STOP file; end refetch"); break
        folder = os.path.join(DATA_DIR, str(rid)); recp = os.path.join(folder, "applied.json")
        tries[str(rid)] = tries.get(str(rid), 0) + 1; json.dump(tries, open(tf, "w", encoding="utf-8"))
        w = MAPS_MIN_GAP + random.uniform(0, MAPS_JITTER) - (time.time() - last_maps)
        if w > 0 and not nap(w): break
        try:
            cur = G.req("GET", f"{API}/{rid}")["restaurant"]
            last_maps = time.time()
            info, reason = build_info(cur)
        except mf.Blocked as e:
            log(f"#{rid} REFETCH Maps BLOCKED {e}; back off 2h")
            tries[str(rid)] -= 1; json.dump(tries, open(tf, "w", encoding="utf-8"))
            open(ROOT + "/fetch_backoff.txt", "w", encoding="utf-8").write(str(time.time() + 7200)); sys.exit(3)
        except Overload as e:
            log(f"#{rid} REFETCH OVERLOAD {e}; wait 5 min"); nap(COOLDOWN); continue
        except Exception as e:
            log(f"#{rid} REFETCH ERROR {type(e).__name__}: {str(e)[:150]}"); continue
        if info is None:
            log(f"#{rid} REFETCH SKIP {reason}"); continue
        m_ = info["_maps"]
        if m_.get("via") != "chrome" or m_.get("thin"):
            log(f"#{rid} REFETCH still thin (via={m_.get('via')} thin={m_.get('thin')}); not done, try {tries[str(rid)]}/3"); continue
        os.makedirs(folder, exist_ok=True)
        f = os.path.join(folder, "info.json")
        old = json.load(open(f, encoding="utf-8")) if os.path.exists(f) else {}
        if old and not os.path.exists(f + ".thin"):
            json.dump(old, open(f + ".thin", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        rec = json.load(open(recp, encoding="utf-8")) if os.path.exists(recp) else {}
        rec.setdefault("urls", {})
        save = lambda: (json.dump(rec, open(recp + ".tmp", "w", encoding="utf-8"), ensure_ascii=False, indent=1), os.replace(recp + ".tmp", recp))
        parts = []
        try:
            fields, _w = normalize_fields({k: v for k, v in info.items() if k not in SKIP_KEYS})
            ofields, _ = normalize_fields({k: v for k, v in old.items() if k not in SKIP_KEYS}) if old else ({}, None)
            empty = lambda v: v is None or (isinstance(v, str) and not v.strip()) or v == []
            send = {}
            for k, v in fields.items():
                if empty(v): continue
                sv = cur.get(k)
                if empty(sv) or (k in REFRESH_KEYS and sv == ofields.get(k) and sv != v):
                    send[k] = v
            if send: G.req("PATCH", f"{API}/{rid}", json_body=send)
            parts.append(f"info={len(send)}f[{','.join(sorted(send))[:80]}]")
            parts.append(push_extra(G, rid, folder, info, rec, save))
            # POST {image_urls} is async now (202, photos: []), so applied.json got no ids since 2026-10-12: every URL the
            # thin run posted is on the server already -> treat the old info.json photo URLs as uploaded too.
            have = {_pbase(x.get("url")) for x in rec["urls"].values()} | {_pbase(x.get("url")) for x in old.get("photo_urls") or []}
            items = info.get("photo_urls") or []
            todo = [it for it in items if _pbase(it["url"]) not in have and _url_hash(it["url"]) not in rec["urls"]]
            if todo:
                hs = _server_hashes(G, rid); keep = []; dup = 0
                for it in todo:
                    try: h = _dhash(it["url"].split("=")[0] + "=s256")
                    except Exception: dup += 1; continue   # unreadable -> do not risk a duplicate
                    if _near(h, hs): dup += 1; continue
                    hs.append(h); keep.append(it)
                todo = keep; parts.append(f"dup_skipped={dup}/srv{len(hs) - len(keep)}")
            added = failed = 0
            roles = _pick_roles(todo, rec, cover_key="role", want_cover=(not rec.get("cover") or rec.get("cover") == rec.get("streetview")), want_logo=not rec.get("logo"))
            for i in range(0, len(todo), PHOTO_CHUNK):
                chunk = todo[i:i + PHOTO_CHUNK]
                body = G.req("POST", f"{API}/{rid}/photos", json_body=_img_body(chunk, roles), raw_status=(422,))
                bad = {e.get("url") for e in body.get("errors") or [] if isinstance(e, dict)}; it_ = iter(body.get("photos") or [])
                for c in chunk:
                    if _upsize_google(c["url"]) in bad: failed += 1; continue
                    ph = next(it_, None)
                    # queued (async) photos are recorded without an id so a later pass never re-posts them
                    rec["urls"][_url_hash(c["url"])] = _rec_photo(c, ph, roles); added += 1
                save()
            parts.append(f"photos=+{added}" + (f"/fail{failed}" if failed else "") + f"/of{len(items)}")
            # cover: only replace the script's own streetview / missing cover; logo only if none set by the script
            # #704: pending photos carry pending_role (server assigns on download); PUT only already-downloaded ones
            for role in ("cover", "logo"):
                q = _role_queued(rec, role)
                if q and rec.get(role) in (None, rec.get("streetview")) and q != rec.get(role):
                    rec[role] = q; save(); parts.append(f"{role}={q}(pending_role)")
            pend = _server_pending(G, rid)
            up = [c for c in items if (rec["urls"].get(_url_hash(c["url"])) or {}).get("photo_id")
                  and pend is not None and rec["urls"][_url_hash(c["url"])]["photo_id"] not in pend]
            if pend: parts.append(f"srv_pending={len(pend)}")
            if up and (not rec.get("cover") or rec.get("cover") == rec.get("streetview")):
                hit = next((c for c in up if c.get("role") == "cover"), up[0]); pid = rec["urls"][_url_hash(hit["url"])]["photo_id"]
                if _put_role(G, rid, "cover", pid): rec["cover"] = pid; save(); parts.append(f"cover={pid}")
                else: parts.append(f"cover_skip={pid}")
            if len(up) > 1 and not rec.get("logo"):
                hit = next((c for c in up if c.get("role2") == "logo"), up[1]); pid = rec["urls"][_url_hash(hit["url"])]["photo_id"]
                if _put_role(G, rid, "logo", pid): rec["logo"] = pid; save(); parts.append(f"logo={pid}")
                else: parts.append(f"logo_skip={pid}")
        except Overload as e:
            log(f"#{rid} REFETCH OVERLOAD {e}; wait 5 min"); nap(COOLDOWN); continue
        except Exception as e:
            log(f"#{rid} REFETCH PUSH ERROR {type(e).__name__}: {str(e)[:150]}"); continue
        json.dump(info, open(f + ".tmp", "w", encoding="utf-8"), ensure_ascii=False, indent=1); os.replace(f + ".tmp", f)
        if info.get("has_menu_photos") and not old.get("has_menu_photos") and os.path.exists(os.path.join(folder, "MENU_DONE")):
            os.replace(os.path.join(folder, "MENU_DONE"), os.path.join(folder, "MENU_DONE.thin")); parts.append("menu=requeued")
        elif info.get("has_menu_photos"): parts.append("menu_photos=yes")
        open(PRIORITY + ".done", "a", encoding="utf-8").write(f"{rid} {' '.join(parts)} {time.strftime('%F %H:%M')}\n")
        log(f"#{rid} REFETCH OK {info.get('name','')[:40]!r} cat={info.get('category')} types={(info.get('google_types_text') or '')[:50]!r} "
            f"gallery={m_.get('gallery')} {' '.join(parts)}")
    log(f"REFETCH END; {len(priority_pending())} priority ids left")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("cmd"); ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--pages", type=int, default=12); ap.add_argument("--ids", type=int, nargs="*")
    ap.add_argument("--floor", type=int, default=2813); ap.add_argument("--ceil", type=int, default=10**9); ap.add_argument("--queue", default=QUEUE); ap.add_argument("--sleep", type=float, default=20); ap.add_argument("--force", action="store_true"); ap.add_argument("--ignore-peak", action="store_true")
    ap.add_argument("--start-page", type=int, default=1); ap.add_argument("--max-pages", type=int, default=0)
    ap.add_argument("--per-page", type=int, default=0); ap.add_argument("--unnamed", action="store_true")
    ap.add_argument("--id-suffix", default="")
    a = ap.parse_args()
    {"prep": cmd_prep, "push": cmd_push, "run": cmd_run, "list": cmd_list, "backfill": cmd_backfill, "refetch": cmd_refetch}[a.cmd](a)
