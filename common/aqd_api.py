#!/usr/bin/env python3
"""Admin client for anuongquanhday.com over the JSON admin API (/v1/admin/...).

Auth: POST /v1/auth/login {email, password} -> access_token (15 min) + refresh_token.
Credentials come ONLY from env AQD_ADMIN_EMAIL / AQD_ADMIN_PASSWORD (loaded by C:/aqd/aqd); they are never
printed, logged or written. Tokens are cached in C:/aqd/.aqd_session/api_tokens.json (0600), shared by all
bots under a file lock; refreshed via /v1/auth/refresh before expiry, re-login only if refresh fails.

Endpoints (repo truonglh2372/an_uong_quanh_day, app/controllers/api/v1/admin/*):
  GET   /v1/admin/restaurants?status=draft&page=N          list (24/page, total_count)
  GET   /v1/admin/restaurants/:id                           {restaurant}
  PATCH /v1/admin/restaurants/:id                           flat JSON, only sent keys change
  GET   /v1/admin/restaurants/:id/photos                    {photos:[{id,url,source,purpose,roles}]}
  POST  /v1/admin/restaurants/:id/photos                    multipart image -> {photo}
                                                            or JSON {image_urls:[<=30]} -> {photos, errors:[{url,error}]}
                                                            (server downloads the URLs itself; since commit 329f670)
  PUT   /v1/admin/restaurants/:id/cover|logo                {photo_id}
  GET   /v1/admin/restaurants/:id/menu                      {rows}
  PUT   /v1/admin/restaurants/:id/menu                      {rows:[...]} -> {created,updated,deleted}
  GET   /v1/admin/restaurants/:id/dish_categories           {dish_categories}
The previous HTML-form client is kept as C:/aqd/aqd_api_html.py (wrapper C:/aqd/aqd_html).
"""
import argparse, concurrent.futures as cf, datetime, glob, hashlib, io, json, os, re, sys, tempfile, \
    threading, time
from urllib.parse import urljoin, urlparse

import requests
try:
    import fcntl
except ImportError:
    fcntl = None
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import origin_pin  # noqa: E402  AQD_ORIGIN_IP: call the origin directly, bypassing Cloudflare

ROOT = os.environ.get("AQD_ROOT", "C:/aqd").replace("\\", "/")
BASE = os.environ.get("AQD_BASE", "https://anuongquanhday.com")
SESSION_DIR = os.environ.get("AQD_SESSION_DIR", ROOT + "/.aqd_session")
TOKEN_FILE = os.path.join(SESSION_DIR, "api_tokens.json")
TOKEN_LOCK = os.path.join(SESSION_DIR, "api_tokens.lock")
ATTEMPTS_FILE = os.path.join(SESSION_DIR, "api_login_attempts.json")
LOG_FILE = os.environ.get("AQD_LOG_FILE", ROOT + "/aqd_api_log.md")
DATA_DIR = ROOT + "/aqd_data"
API = "/v1/admin/restaurants"
REFRESH_MARGIN_S = 90            # refresh when <90 s of the 15 min access token remain
MAX_LOGINS_PER_WINDOW = 5        # across all bots (server allows 10 per 3 min per IP)
LOGIN_WINDOW_S = 3 * 60
MAX_LOGINS_PER_PROCESS = 2
MAX_SIDE = 1600
MAX_BYTES = 5 * 1024 * 1024 - 64 * 1024   # server limit 5 MB
MAX_FETCH_BYTES = 30 * 1024 * 1024
UPLOAD_WORKERS = int(os.environ.get("AQD_UPLOAD_WORKERS", "5"))
# URL photos: "server" = POST {image_urls} so the site downloads them (fallback to local fetch per URL the server
# rejects); "local" = old behaviour (fetch into memory here, shrink, multipart upload).
THROTTLE = float(os.environ.get("AQD_THROTTLE", "0"))
import threading as _thr_mod
_THR_LOCK = _thr_mod.Lock(); _THR = [0.0]
URL_UPLOAD_MODE = os.environ.get("AQD_URL_UPLOAD", "server")
URL_BATCH_MAX = int(os.environ.get("AQD_URL_BATCH_MAX", "6"))                # URLs per server request (server limit 30; it fetches them sequentially)
MENU_CHUNK = 200

INFO_FIELDS = [
    "name", "address", "latitude", "longitude", "phone", "website", "facebook_url", "tiktok_url", "menu_url",
    "opening_hours_text", "business_status", "google_primary_type", "google_primary_type_label",
    "google_types_text", "google_maps_url", "category", "cuisine",
    "short_address", "address_street_number", "address_route", "address_ward", "address_district",
    "address_city", "address_country_code", "address_postal_code", "plus_code_global", "plus_code_compound",
    "name_language_code", "utc_offset_minutes",
    "viewport_south_lat", "viewport_west_lng", "viewport_north_lat", "viewport_east_lng",
]
NUMERIC = {"latitude", "longitude", "viewport_south_lat", "viewport_west_lng", "viewport_north_lat",
           "viewport_east_lng", "utc_offset_minutes"}
CORE = {"name", "address", "latitude", "longitude"}
ALIASES = {"lat": "latitude", "lng": "longitude", "lon": "longitude", "facebook": "facebook_url",
           "tiktok": "tiktok_url", "hours": "opening_hours_text", "opening_hours": "opening_hours_text",
           "google_types": "google_types_text", "maps_url": "google_maps_url", "shop_type": "category",
           "dishes": "cuisine"}
CATEGORIES = {"cafe", "restaurant", "eatery", "other"}
BUSINESS_STATUSES = {"OPERATIONAL", "CLOSED_TEMPORARILY", "CLOSED_PERMANENTLY"}
SOCIAL_HOSTS = {"facebook_url": ("facebook.com", "fb.com", "fb.me"), "tiktok_url": ("tiktok.com",)}
VERIFY = {"status": "validated", "verification_method": "other_non_google", "verification_source": "grok"}
SOURCE_LABELS = {"upload": "Tải lên", "ai": "AI", "menu": "Ảnh menu", "post": "Bài đăng"}
APPLY_SKIP_KEYS = ("cover", "logo", "stay_draft", "note", "photo_urls")


class AQDError(Exception):
    pass


class LoginError(AQDError):
    pass


class APIError(AQDError):
    def __init__(self, status, code, details=None, path=""):
        self.status, self.code, self.details = status, code, details
        super().__init__(f"HTTP {status} {code} {path}" + (f" {json.dumps(details, ensure_ascii=False)[:600]}"
                                                           if details else ""))


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def _ensure_session_dir():
    os.makedirs(SESSION_DIR, mode=0o700, exist_ok=True)
    os.chmod(SESSION_DIR, 0o700)


def _write_private(path, data):
    _ensure_session_dir()
    fd, tmp = tempfile.mkstemp(dir=SESSION_DIR)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def _host_ok(url, domains):
    try:
        u = urlparse(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and any(host == d or host.endswith("." + d) for d in domains)


def normalize_fields(fields):
    """Map aliases, drop unknown keys, coerce types and pre-check the model validations."""
    out, warnings = {}, []
    for k, v in fields.items():
        k = ALIASES.get(k, k)
        if k not in INFO_FIELDS:
            continue
        if k == "google_types_text" and isinstance(v, (list, tuple)):
            v = ", ".join(map(str, v))
        if v is None:
            v = ""
        v = str(v) if not isinstance(v, str) else v
        v = v.strip() if k != "opening_hours_text" else v.strip().replace("\r\n", "\n")
        if k in ("latitude", "longitude") and v == "":
            warnings.append(f"blank_{k}_not_sent")
            continue
        out[k] = v
    if out.get("category") and out["category"] not in CATEGORIES:
        raise AQDError(f"category must be one of {sorted(CATEGORIES)}, got {out['category']!r}")
    if out.get("business_status") and out["business_status"] not in BUSINESS_STATUSES:
        raise AQDError(f"business_status must be one of {sorted(BUSINESS_STATUSES)}, got {out['business_status']!r}")
    for k, domains in SOCIAL_HOSTS.items():
        if out.get(k) and not _host_ok(out[k], domains):
            raise AQDError(f"{k} must be an http(s) URL on {domains}, got {out[k]!r}")
    for k in ("latitude", "longitude"):
        if out.get(k):
            float(out[k])
    return out, warnings


# ---------------------------------------------------------------- auth (shared token cache)
class TokenStore:
    def __init__(self, base, ua):
        self.base, self.ua = base, ua
        self.logins = 0
        self._mem = None
        self._lock = threading.Lock()

    def _post(self, path, body):
        return requests.post(urljoin(self.base, path), json=body, timeout=30,
                             headers={"User-Agent": self.ua, "Accept": "application/json"})

    @staticmethod
    def _fresh(tok):
        if not tok or not tok.get("access_token"):
            return False
        try:
            exp = datetime.datetime.fromisoformat(tok["access_token_expires_at"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            return False
        return exp - time.time() > REFRESH_MARGIN_S

    def access(self, force=False):
        with self._lock:
            if not force and self._fresh(self._mem):
                return self._mem["access_token"]
            stale = self._mem.get("access_token") if (force and self._mem) else None
            tok = _read_json(TOKEN_FILE, None)
            if self._fresh(tok) and tok.get("access_token") != stale:
                self._mem = tok
                return tok["access_token"]
            _ensure_session_dir()
            with os.fdopen(os.open(TOKEN_LOCK, os.O_RDWR | os.O_CREAT, 0o600), "w", encoding="utf-8") as lk:
                if fcntl: fcntl.flock(lk, fcntl.LOCK_EX)
                tok = _read_json(TOKEN_FILE, None)          # another bot may have refreshed meanwhile
                if self._fresh(tok) and tok.get("access_token") != stale:
                    self._mem = tok
                    return tok["access_token"]
                new = None
                if tok and tok.get("refresh_token"):
                    r = self._post("/v1/auth/refresh", {"refresh_token": tok["refresh_token"]})
                    if r.status_code == 200:
                        new = r.json()
                if new is None:
                    new = self._login()
                new = {k: new.get(k) for k in ("access_token", "access_token_expires_at", "refresh_token")}
                _write_private(TOKEN_FILE, new)
                self._mem = new
                return new["access_token"]

    def _login(self):
        email = os.environ.get("AQD_ADMIN_EMAIL", "")
        password = os.environ.get("AQD_ADMIN_PASSWORD", "")
        if not email or not password:
            raise LoginError("AQD_ADMIN_EMAIL / AQD_ADMIN_PASSWORD not set (run through C:/aqd/aqd)")
        if self.logins >= MAX_LOGINS_PER_PROCESS:
            raise LoginError(f"stopped after {self.logins} logins in this process")
        now = time.time()
        ts = [t for t in _read_json(ATTEMPTS_FILE, []) if now - t < LOGIN_WINDOW_S]
        if len(ts) >= MAX_LOGINS_PER_WINDOW:
            raise LoginError(f"login refused locally: {len(ts)} logins in the last {LOGIN_WINDOW_S // 60} min")
        _write_private(ATTEMPTS_FILE, ts + [now])
        self.logins += 1
        r = self._post("/v1/auth/login", {"email": email, "password": password})
        del email, password
        if r.status_code == 200:
            data = r.json()
            if not (data.get("user") or {}).get("role", "admin") == "admin":
                pass  # role checked server-side on each admin call (403 forbidden)
            return data
        try:
            code = r.json().get("error", {}).get("code")
        except ValueError:
            code = None
        if r.status_code == 429:
            raise LoginError("server login rate limit hit (10 per 3 min); wait and retry")
        raise LoginError(f"login failed: HTTP {r.status_code} {code or ''} (not retrying)")


# ---------------------------------------------------------------- client
class AQD:
    def __init__(self, base=BASE, verbose=False):
        self.base = base
        origin_pin.install(base)  # no-op unless AQD_ORIGIN_IP is set; TLS still verified against the host name
        self.verbose = verbose
        self.pending_roles = {}   # photo_id -> pending_role for URL photos still downloading (#704)
        self.ua = "aqd-api/2.0 (+grok-bot)"
        self.tokens = TokenStore(base, self.ua)
        self._tl = threading.local()

    # one requests.Session per thread (Session is not guaranteed thread-safe)
    @property
    def s(self):
        s = getattr(self._tl, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"User-Agent": self.ua, "Accept": "application/json", "Accept-Language": "vi"})
            ad = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8)
            s.mount("https://", ad); s.mount("http://", ad)
            self._tl.s = s
        return s

    def _url(self, path):
        return urljoin(self.base, path)

    def login(self):
        self.tokens.access()

    def call(self, method, path, json_body=None, files=None, data=None, retries=4, timeout=60, raw_status=(),
             retry_status=(429, 500, 502, 503, 504), retry_network=True):
        """Authenticated JSON call with token refresh on 401 and backoff on 429/5xx/network errors.
        raw_status: HTTP codes whose JSON body is returned (with "_status") instead of raising."""
        forced = False
        if THROTTLE:
            retries = 0
        for attempt in range(retries + 1):
            if THROTTLE:
                with _THR_LOCK:
                    w = _THR[0] + THROTTLE - time.time()
                    if w > 0: time.sleep(w)
                    _THR[0] = time.time()
            hdr = {"Authorization": "Bearer " + self.tokens.access()}
            try:
                if files is not None:
                    for f in files.values():
                        try:
                            f[1].seek(0)
                        except Exception:
                            pass
                r = self.s.request(method, self._url(path), json=json_body, files=files, data=data,
                                   headers=hdr, timeout=timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt >= retries or not retry_network:
                    raise AQDError(f"{method} {path}: {type(e).__name__}") from e
                time.sleep(min(2 ** attempt, 20)); continue
            if r.status_code == 401 and not forced:
                forced = True
                self.tokens.access(force=True)
                continue
            if r.status_code in retry_status and attempt < retries:
                ra = r.headers.get("Retry-After")
                time.sleep(float(ra) if ra and ra.isdigit() else min(2 ** attempt, 20)); continue
            if r.status_code == 204:
                return {}
            try:
                body = r.json()
            except ValueError:
                body = None
            if r.status_code in raw_status and isinstance(body, dict):
                body["_status"] = r.status_code
                return body
            if r.status_code >= 400:
                err = (body or {}).get("error", {}) if isinstance(body, dict) else {}
                raise APIError(r.status_code, err.get("code") or r.reason, err.get("details"), f"{method} {path}")
            return body

    # ---------- read ----------
    def restaurant(self, rid):
        return self.call("GET", f"{API}/{int(rid)}")["restaurant"]

    def get(self, rid):
        r = self.restaurant(rid)
        return {"id": r["id"], "title": r.get("name"), "fields": r, "status": r.get("status"),
                "verification_method": r.get("verification_method"),
                "verification_source": r.get("verification_source"), "verified_at": r.get("verified_at"),
                "details": {}, "errors": []}

    def photos(self, rid):
        out = []
        for p in self.call("GET", f"{API}/{int(rid)}/photos")["photos"]:
            url = self._url(p["url"]) if p.get("url") else None
            out.append({"id": p["id"], "roles": p.get("roles", []), "source": p.get("source"),
                        "source_label": SOURCE_LABELS.get(p.get("source"), p.get("source")),
                        "purpose": p.get("purpose"), "dish_ids": p.get("dish_ids", []),
                        "has_image": bool(url), "url": url, "thumb": url, "full": url,
                        "created_at": p.get("created_at")})
        return out

    def dishes(self, rid):
        rows = self.call("GET", f"{API}/{int(rid)}/menu")["rows"]
        if not rows:
            return []
        cats = {c["id"]: c.get("name") for c in
                self.call("GET", f"{API}/{int(rid)}/dish_categories").get("dish_categories", [])}
        return [{"id": str(d["id"]), "name": d["name"] or "",
                 "price_vnd": "" if d.get("price_vnd") is None else str(d["price_vnd"]),
                 "description": d.get("description") or "", "category_id": d.get("category_id"),
                 "category_name": cats.get(d.get("category_id"), ""), "available": d.get("available"),
                 "photo_id": d.get("photo_id")} for d in rows]

    def drafts(self, status="draft", q=None):
        def page(n):
            params = f"?status={status}&page={n}" + (f"&q={requests.utils.quote(q)}" if q else "")
            return self.call("GET", f"{API}{params}")
        first = page(1)
        total = first.get("total_count", 0)
        pages = max(1, -(-total // 24))
        res = list(first["restaurants"])
        if pages > 1:
            with cf.ThreadPoolExecutor(4) as ex:
                for body in ex.map(page, range(2, pages + 1)):
                    res += body["restaurants"]
        seen, out = set(), []
        for r in res:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            out.append(r)
        return {"total_count": total, "restaurants": sorted(out, key=lambda r: -r["id"])}

    # ---------- write ----------
    def patch(self, rid, body):
        return self.call("PATCH", f"{API}/{int(rid)}", json_body=body)["restaurant"]

    def update_info(self, rid, fields, before=None, with_verify=False):
        """PATCH only the given info fields (absent keys untouched). with_verify adds status validated +
        method/source in the SAME request (only safe for a first verification of a never-verified draft)."""
        clean, warnings = normalize_fields(fields)
        body = dict(clean)
        if with_verify:
            body.update(VERIFY)
        if not body:
            return {"ok": True, "sent": 0, "warnings": warnings, "status": None}
        before = before or self.restaurant(rid)
        if all(k in before and _same(k, v, before[k]) for k, v in body.items()):
            return {"ok": True, "sent": 0, "unchanged": len(clean), "mismatched": [], "warnings": warnings,
                    "status": before.get("status"), "restaurant": before}
        after = self.patch(rid, body)
        if before.get("status") != "draft" and after.get("status") == "draft":
            warnings.append("server reset verification to draft (name/address/coords changed on a verified place)")
        mismatched = [k for k, v in clean.items() if k in after and not _same(k, v, after[k])]
        return {"ok": not mismatched, "sent": len(clean), "mismatched": mismatched, "warnings": warnings,
                "status": after.get("status"), "restaurant": after}

    def validate(self, rid):
        after = self.patch(rid, dict(VERIFY))
        ok = all(after.get(k) == v for k, v in VERIFY.items())
        return {"ok": ok, "status": after.get("status"), "verification_method": after.get("verification_method"),
                "verification_source": after.get("verification_source"), "verified_at": after.get("verified_at")}

    def upload_bytes(self, rid, data, filename="photo.jpg"):
        files = {"image": (filename, io.BytesIO(data), "image/jpeg")}
        return self.call("POST", f"{API}/{int(rid)}/photos", files=files, timeout=180)["photo"]["id"]

    def _parallel(self, items, fn, workers=None):
        """Run fn(item) concurrently; returns [(item, result|None, error|None)] in input order."""
        workers = workers or UPLOAD_WORKERS
        out = [None] * len(items)
        with cf.ThreadPoolExecutor(max(1, min(workers, len(items) or 1))) as ex:
            futs = {ex.submit(fn, it): i for i, it in enumerate(items)}
            for f in cf.as_completed(futs):
                i = futs[f]
                try:
                    out[i] = (items[i], f.result(), None)
                except Exception as e:      # noqa: BLE001 - reported per item
                    out[i] = (items[i], None, f"{type(e).__name__}: {e}")
        return out

    def upload_photos(self, rid, paths):
        """Shrink to <=1600px in memory, POST each concurrently. Returns [(path, photo_id|None)] in order."""
        def one(p):
            return self.upload_bytes(rid, prepare_image(p), os.path.splitext(os.path.basename(p))[0] + ".jpg")
        res = self._parallel(list(paths), one)
        if self.verbose:
            for p, pid, err in res:
                if err:
                    print(f"  upload {p}: {err}", file=sys.stderr)
        return [(p, pid) for p, pid, _ in res]

    def upload_url_local(self, rid, url):
        """Old path: fetch the URL into memory here, shrink, multipart POST (nothing touches disk)."""
        return self.upload_bytes(rid, fetch_image(url), _url_filename(url))

    def _server_batch(self, rid, urls, roles=None):
        """One POST {image_urls}. Returns {url: (photo_id|None, error|None)}. Raises on transport/whole-request
        errors. Server answers 201 if >=1 created, 422 if none; photos come back in request order."""
        sent = {}
        for u in urls:
            sent.setdefault(_upsize_google(u), []).append(u)
        # no blind retry on timeouts/5xx (the server may have created the photos); 429 is safe to retry
        # #704: {url, role} items -> server keeps pending_role and sets cover/logo itself after the async download
        roles, used, items = roles or {}, set(), []
        for su, origs in sent.items():
            r = next((roles.get(o) for o in origs if roles.get(o) in ("cover", "logo")), None)
            if r and r not in used:
                used.add(r); items.append({"url": su, "role": r})
            else:
                items.append(su)
        body = self.call("POST", f"{API}/{int(rid)}/photos", json_body={"image_urls": items},
                         timeout=60 + 20 * len(sent), raw_status=(422,), retries=3, retry_status=(429,),
                         retry_network=False)
        photos, errors = body.get("photos"), body.get("errors")
        if not isinstance(photos, list) or not isinstance(errors, list):
            err = (body.get("error") or {}).get("code") if isinstance(body.get("error"), dict) else body.get("error")
            raise APIError(body.get("_status", 0), err or "unexpected_response", None, "POST image_urls")
        failed = {e.get("url"): e.get("error") for e in errors if isinstance(e, dict)}
        if len(photos) + len(failed) != len(sent):
            raise APIError(body.get("_status", 0), "batch_count_mismatch",
                           {"sent": len(sent), "photos": len(photos), "errors": len(failed)}, "POST image_urls")
        res, it = {}, iter(photos)
        for su, origs in sent.items():
            if su in failed:
                val = (None, failed[su] or "error")
            else:
                ph = next(it)
                val = (int(ph["id"]), None)
                if ph.get("status") == "pending":
                    self.pending_roles[int(ph["id"])] = ph.get("pending_role")
            for orig in origs:
                res[orig] = val
        return res

    def upload_urls(self, rid, urls, roles=None):
        """Upload image URLs (deduped, order kept). Server-side download in parallel batches; any URL the
        server rejects (or a failed batch) falls back to the local in-memory fetch+upload.
        Returns [(url, photo_id|None, error|None, via)] with via in server|local."""
        urls = list(dict.fromkeys(urls))
        if not urls:
            return []
        done = {}
        if URL_UPLOAD_MODE == "server":
            n = max(1, min(URL_BATCH_MAX, -(-len(urls) // max(1, UPLOAD_WORKERS))))
            chunks = [urls[i:i + n] for i in range(0, len(urls), n)]
            for chunk, res, err in self._parallel(chunks, lambda c: self._server_batch(rid, c, roles)):
                if err:
                    for u in chunk:
                        done[u] = (None, "batch:" + err)
                else:
                    done.update(res)
        retry = [u for u in urls if not (done.get(u) or (None,))[0]]
        server_err = {u: (done.get(u) or (None, None))[1] for u in retry}
        local = {}
        for u, pid, err in self._parallel(retry, lambda u: self.upload_url_local(rid, u)):
            se = server_err.get(u)
            local[u] = (pid, (f"server:{se}; local:{err}" if se else err) if err else None,
                        se)
        out = []
        for u in urls:
            if u in local:
                pid, err, se = local[u]
                out.append((u, pid, err, "local" + (f"(server:{se})" if se else "")))
            else:
                out.append((u, done[u][0], None, "server"))
        if self.verbose:
            for u, pid, err, via in out:
                print(f"  url {u[:70]} -> {pid} via {via}" + (f" ERR {err}" if err else ""), file=sys.stderr)
        return out

    def push_urls(self, rid, urls):
        """Upload image URLs (server downloads them; local fetch fallback). urls: list of str or
        {"url", "role"}. Returns [{"url","photo_id","role","error","via"}]."""
        items = [_url_item(u) for u in urls]
        res = {u: (pid, err, via) for u, pid, err, via in self.upload_urls(rid, [it["url"] for it in items],
                                                                         {it["url"]: it.get("role") for it in items})}
        out = []
        for it in items:
            pid, err, via = res[it["url"]]
            out.append({"url": it["url"], "role": it.get("role"), "photo_id": pid, "error": err, "via": via})
        return out

    def push_urls_recorded(self, rid, urls, folder=None):
        """push_urls + idempotency via <folder>/applied.json["urls"] (sha1 of URL -> photo_id), then assign roles."""
        folder = folder or os.path.join(DATA_DIR, str(rid))
        os.makedirs(folder, exist_ok=True)
        rec_path = os.path.join(folder, "applied.json")
        items = [_url_item(u) for u in urls]
        album = {p["id"]: p for p in self.photos(rid)}
        rec = _read_json(rec_path, {})
        rec.setdefault("urls", {})
        todo, out = [], []
        for it in items:
            prev = rec["urls"].get(_url_hash(it["url"]))
            if prev and prev.get("photo_id") in album:
                out.append({"url": it["url"], "role": it["role"], "photo_id": prev["photo_id"], "skipped": True})
            else:
                todo.append(it)
        for r in self.push_urls(rid, todo):
            if r.get("photo_id"):
                r["skipped"] = False
            out.append(r)
        rec = _read_json(rec_path, {})            # re-read: another step may have written meanwhile
        rec.setdefault("urls", {})
        for r in out:
            if r.get("photo_id") and not r.get("skipped"):
                rec["urls"][_url_hash(r["url"])] = {"url": r["url"], "photo_id": r["photo_id"]}
        rec["updated_at"] = _now()
        with open(rec_path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        os.replace(rec_path + ".tmp", rec_path)
        for role in ("cover", "logo"):
            hit = next((r for r in out if r["role"] == role and r.get("photo_id")), None)
            if hit and hit["photo_id"] in self.pending_roles:
                hit["assigned"] = self.pending_roles[hit["photo_id"]] == role   # server assigns on download
                continue
            if hit and role not in album.get(hit["photo_id"], {}).get("roles", []):
                self.assign(rid, hit["photo_id"], role)
                hit["assigned"] = True
        return out

    def assign(self, rid, photo_id, role):
        if role not in ("cover", "logo"):
            raise AQDError("role must be cover or logo")
        r = self.call("PUT", f"{API}/{int(rid)}/{role}", json_body={"photo_id": int(photo_id)})["restaurant"]
        if not r.get(f"{role}_url"):
            raise AQDError(f"#{rid} {role} not set after PUT")
        return {"ok": True, "role": role, "photo_id": int(photo_id)}

    def upload_role(self, rid, path, role):
        pid = self.upload_bytes(rid, prepare_image(path), os.path.splitext(os.path.basename(path))[0] + ".jpg")
        self.assign(rid, pid, role)
        return {"ok": True, "role": role, "photo_id": pid}

    def save_menu(self, rid, rows, if_existing="skip", current=None):
        """rows: list of dicts name/price_vnd/description/category (or a TSV path). PUT /menu.
        if_existing: 'skip' -> nothing when the place already has dishes; 'missing' -> only rows whose
        (name, price) are not on the menu yet."""
        if isinstance(rows, str):
            rows = read_menu_tsv(rows)
        if current is None:
            current = self.call("GET", f"{API}/{int(rid)}/menu")["rows"]
        if current and if_existing == "skip":
            return {"ok": True, "skipped": True, "existing": len(current), "created": 0}
        have = {(_norm(d.get("name")), "" if d.get("price_vnd") is None else str(d["price_vnd"])) for d in current}
        todo, seen = [], set()
        for r in rows:
            key = (_norm(r["name"]), r["price_vnd"])
            if not r["name"] or key in have or key in seen:
                continue
            seen.add(key)
            todo.append(r)
        created = 0
        for i in range(0, len(todo), MENU_CHUNK):
            chunk = todo[i:i + MENU_CHUNK]
            body = {"rows": [{"name": r["name"], "price_vnd": r["price_vnd"], "description": r["description"],
                              "new_category_name": r["category"], "available": True} for r in chunk]}
            try:
                res = self.call("PUT", f"{API}/{int(rid)}/menu", json_body=body, timeout=180, retries=1)
            except APIError as e:
                raise AQDError(f"#{rid} menu chunk refused ({created} created before): {e}") from e
            created += int(res.get("created", 0))
        return {"ok": True, "skipped": False, "existing": len(current), "created": created, "submitted": len(todo)}

    # ---------- one-shot ----------
    def apply(self, rid, folder=None):
        t0 = time.time()
        folder = folder or os.path.join(DATA_DIR, str(rid))
        with open(os.path.join(folder, "info.json"), encoding="utf-8") as f:
            info = json.load(f)
        rec_path = os.path.join(folder, "applied.json")
        rec = _read_json(rec_path, {})
        rec.setdefault("photos", {})
        rec.setdefault("urls", {})
        parts, warn = [], []

        def save_rec():
            rec["updated_at"] = _now()
            with open(rec_path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=1)
            os.replace(rec_path + ".tmp", rec_path)

        # read current state once (restaurant + album + menu) in parallel
        with cf.ThreadPoolExecutor(3) as ex:
            f_r = ex.submit(self.restaurant, rid)
            f_p = ex.submit(self.photos, rid)
            menu_path = os.path.join(folder, "menu.tsv")
            f_m = ex.submit(self.call, "GET", f"{API}/{int(rid)}/menu") if os.path.exists(menu_path) else None
            cur = f_r.result(); album = {p["id"]: p for p in f_p.result()}
            cur_menu = f_m.result()["rows"] if f_m else None

        # 1) info (+ status in the same PATCH for a first verification)
        fields = {k: v for k, v in info.items() if k not in APPLY_SKIP_KEYS}
        first_verify = (not info.get("stay_draft") and cur.get("status") == "draft"
                        and not cur.get("verified_at") and not cur.get("verified_by_id"))
        try:
            res = self.update_info(rid, fields, before=cur, with_verify=first_verify)
        except APIError as e:
            if not first_verify:
                raise
            warn.append(f"one_patch_refused:{e.code}")
            first_verify = False
            res = self.update_info(rid, fields, before=cur)
        warn += res.get("warnings", [])
        if res.get("mismatched"):
            warn.append("mismatch:" + ",".join(res["mismatched"]))
        parts.append(f"info={res['sent']}f" + ("+v" if first_verify else "") +
                     (f"(={res['unchanged']})" if res.get("unchanged") else ""))
        rec["info"] = {"at": _now(), "sent": res["sent"]}
        status_now = res.get("status") or cur.get("status")

        # 2) photos from files + URLs, all concurrently (idempotent by sha1 / url hash + still in album)
        files = sorted(p for p in glob.glob(os.path.join(folder, "photos", "*"))
                       if p.lower().endswith((".jpg", ".jpeg", ".png", ".webp")))
        jobs, skipped = [], 0
        for p in files:
            h = _sha1(p)
            prev = rec["photos"].get(os.path.basename(p))
            if prev and prev.get("sha1") == h and prev.get("photo_id") in album:
                skipped += 1
                continue
            jobs.append(("file", p, h))
        url_items = [_url_item(u) for u in info.get("photo_urls") or []]
        for it in url_items:
            h = _url_hash(it["url"])
            prev = rec["urls"].get(h)
            if prev and prev.get("photo_id") in album:
                skipped += 1
                continue
            jobs.append(("url", it["url"], h))

        def up(job):
            kind, src, _h = job
            return self.upload_bytes(rid, prepare_image(src), os.path.splitext(os.path.basename(src))[0] + ".jpg")
        file_jobs = [j for j in jobs if j[0] == "file"]
        url_jobs = [j for j in jobs if j[0] == "url"]
        results, via_n = [], {}
        with cf.ThreadPoolExecutor(2) as ex:
            f_url = ex.submit(self.upload_urls, rid, [j[1] for j in url_jobs],
                              {it["url"]: it.get("role") for it in url_items}) if url_jobs else None
            results += self._parallel(file_jobs, up) if file_jobs else []
            if f_url:
                ures = {u: (pid, err, via) for u, pid, err, via in f_url.result()}
                for j in url_jobs:
                    pid, err, via = ures[j[1]]
                    via_n[via.split("(")[0]] = via_n.get(via.split("(")[0], 0) + (1 if pid else 0)
                    results.append((j, pid, err))
        added = 0
        for (kind, src, h), pid, err in results:
            if pid:
                added += 1
                album[pid] = {"id": pid, "roles": []}
                if kind == "file":
                    rec["photos"][os.path.basename(src)] = {"sha1": h, "photo_id": pid}
                else:
                    rec["urls"][h] = {"url": src, "photo_id": pid}
            else:
                warn.append(f"upload_failed:{os.path.basename(src) if kind == 'file' else src[:60]}:{err}")
        save_rec()
        parts.append(f"photos=+{added}/skip{skipped}" + (f"(urls {len(url_items)}" + (
            "".join(f" {k}{v}" for k, v in sorted(via_n.items())) if via_n else "") + ")" if url_items else ""))

        # 3) cover / logo  (info.json cover/logo = file name; photo_urls items may carry role)
        role_pid = {}
        for role in ("cover", "logo"):
            name = info.get(role)
            if name:
                pid = rec["photos"].get(os.path.basename(name), {}).get("photo_id")
                if pid:
                    role_pid[role] = pid
                else:
                    warn.append(f"{role}_file_not_uploaded:{name}")
        for it in url_items:
            if it.get("role") in ("cover", "logo") and it["role"] not in role_pid:
                pid = rec["urls"].get(_url_hash(it["url"]), {}).get("photo_id")
                if pid:
                    role_pid[it["role"]] = pid
                else:
                    warn.append(f"{it['role']}_url_not_uploaded")
        todo_roles = {}
        for role, pid in role_pid.items():
            if pid in self.pending_roles:   # #704: still downloading; pending_role (if sent) is applied server-side
                parts.append(f"{role}={pid}(pending_role)" if self.pending_roles[pid] == role else f"{role}=pending")
                continue
            if role in album.get(pid, {}).get("roles", []):
                parts.append(f"{role}=ok")
            else:
                todo_roles[role] = pid
        if todo_roles:
            for role, pid, err in self._parallel(list(todo_roles.items()),
                                                 lambda rp: self.assign(rid, rp[1], rp[0]), workers=2):
                role, pid = role
                if err:
                    warn.append(f"{role}_assign_failed:{err}")
                else:
                    rec[role] = pid
                    parts.append(f"{role}={pid}")
        save_rec()

        # 4) menu (skip if the place already has dishes, unless we created them in an earlier run)
        if cur_menu is not None:
            if cur_menu and not rec.get("menu"):
                parts.append(f"menu=skip(existing {len(cur_menu)})")
                warn.append("menu_not_saved_place_already_has_dishes")
            else:
                res = self.save_menu(rid, menu_path, if_existing="missing" if rec.get("menu") else "skip",
                                     current=cur_menu)
                if res["skipped"]:
                    parts.append(f"menu=skip(existing {res['existing']})")
                else:
                    parts.append(f"menu=+{res['created']}")
                    rec["menu"] = {"at": _now(), "created": rec.get("menu", {}).get("created", 0) + res["created"]}
            save_rec()

        # 5) status
        if info.get("stay_draft"):
            parts.append(f"status={status_now}(stay_draft)")
            if status_now != "draft":
                warn.append("stay_draft_but_place_is_" + str(status_now))
        else:
            r_after = res_info = None
            now_r = res.get("restaurant") or cur
            if (first_verify and status_now == "validated") or \
                    (status_now in ("validated", "published") and now_r.get("verified_at")
                     and all(now_r.get(k) == v for k, v in VERIFY.items() if k != "status")):
                r_after = {"ok": True, "status": status_now}
            else:
                r_after = self.validate(rid)
            parts.append(f"status={r_after['status']}")
            if not r_after["ok"]:
                warn.append(f"validate_failed:{r_after['status']}/{r_after.get('verification_method')}/"
                            f"{r_after.get('verification_source')}")
            rec["validated"] = r_after["ok"]
        save_rec()
        ok = not any(w.startswith(("validate_failed", "upload_failed", "mismatch", "server reset",
                                   "cover_assign", "logo_assign")) for w in warn)
        parts.append(f"{time.time() - t0:.1f}s")
        line = f"- {_now()} #{rid} {'OK' if ok else 'WARN'} " + " ".join(parts) + (" | " + "; ".join(warn) if warn else "")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line)
        return {"ok": ok, "line": line}


# ---------------------------------------------------------------- helpers
def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _same(key, sent, got):
    if key in NUMERIC:
        try:
            if (sent in ("", None)) and got in ("", None):
                return True
            return abs(float(sent) - float(got)) < 1e-6
        except (TypeError, ValueError):
            return str(sent or "") == str(got or "")
    norm = lambda v: ("" if v is None else str(v)).strip().replace("\r\n", "\n")
    return norm(sent) == norm(got)


def _sha1(path):
    with open(path, "rb") as f:
        return hashlib.sha1(f.read()).hexdigest()


def _url_item(u):
    if isinstance(u, dict):
        it = {"url": str(u["url"]).strip(), "role": u.get("role")}
    else:
        u = str(u).strip()
        m = re.match(r"^(cover|logo)=(https?://.+)$", u)
        it = {"url": m.group(2), "role": m.group(1)} if m else {"url": u, "role": None}
    if not re.match(r"^https?://", it["url"]):
        raise AQDError(f"not an http(s) URL: {it['url'][:80]!r}")
    if it["role"] not in (None, "", "cover", "logo"):
        raise AQDError(f"role must be cover or logo, got {it['role']!r}")
    return it


def _url_hash(url):
    return hashlib.sha1(url.strip().encode()).hexdigest()


def _url_filename(url):
    return "url-" + _url_hash(url)[:12] + ".jpg"


def _upsize_google(url):
    """Ask googleusercontent for ~1600 px: replace a trailing '=w408-h306-k-no' / '=s680' style suffix with
    '=s1600', or add '=s1600' when there is none. Old-style URLs whose last segment is a file name (has a dot)
    and every other host are returned unchanged. Dedup keys stay on the ORIGINAL URL (callers hash that)."""
    try:
        u = urlparse(url)
    except ValueError:
        return url
    if not (u.hostname or "").endswith("googleusercontent.com") or u.query or u.fragment:
        return url
    head, _, last = u.path.rpartition("/")
    if not last or not head:
        return url
    if "=" in last:
        base = last[:last.index("=")]
    elif "." in last:
        return url
    else:
        base = last
    return url[:len(url) - len(last)] + base + "=s1600" if url.endswith(last) else url


_fetch_local = threading.local()


def fetch_image(url):
    """Download an image into memory (size-capped) and return shrunk JPEG bytes. Nothing touches disk."""
    s = getattr(_fetch_local, "s", None)
    if s is None:
        s = _fetch_local.s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0 (aqd-api image fetch)"})
    last = None
    for attempt in range(3):
        try:
            with s.get(_upsize_google(url), stream=True, timeout=(10, 40), allow_redirects=True) as r:
                if r.status_code in (429, 500, 502, 503, 504):
                    last = f"HTTP {r.status_code}"; time.sleep(1 + 2 * attempt); continue
                if r.status_code != 200:
                    raise AQDError(f"fetch HTTP {r.status_code}")
                ctype = r.headers.get("Content-Type", "")
                if ctype and not ctype.startswith("image/") and "octet-stream" not in ctype:
                    raise AQDError(f"not an image ({ctype})")
                buf = io.BytesIO()
                for chunk in r.iter_content(256 * 1024):
                    buf.write(chunk)
                    if buf.tell() > MAX_FETCH_BYTES:
                        raise AQDError("image too large")
            return prepare_image(buf)
        except (requests.ConnectionError, requests.Timeout) as e:
            last = type(e).__name__; time.sleep(1 + 2 * attempt)
    raise AQDError(f"fetch failed: {last}")


def prepare_image(src):
    """Thumbnail to 1600px, RGB, JPEG q85 (<=5 MB). src: path, bytes or file-like. Never edits a file."""
    if isinstance(src, (bytes, bytearray)):
        src = io.BytesIO(src)
    with Image.open(src) as im:
        im.draft("RGB", (MAX_SIDE, MAX_SIDE))      # fast JPEG downscale on decode
        im = im.convert("RGB")
    if max(im.size) > MAX_SIDE:
        im.thumbnail((MAX_SIDE, MAX_SIDE))
    q = 85
    while True:
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=q)
        if buf.tell() <= MAX_BYTES or q <= 50:
            return buf.getvalue()
        q -= 10


def read_menu_tsv(path):
    """TSV, no header: name <TAB> price_vnd (digits or blank) <TAB> description <TAB> category (blank -> default)."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n").rstrip("\r")
            if not line.strip():
                continue
            cols = (line.split("\t") + ["", "", "", ""])[:4]
            name, price, desc, cat = (c.strip() for c in cols)
            price = re.sub(r"[^\d]", "", price)
            if price and int(price) <= 0:
                price = ""          # server rejects 0; blank is allowed
            rows.append({"name": name, "price_vnd": price, "description": desc, "category": cat})
    return rows


def place_id_from(url):
    m = re.search(r"(ChIJ[\w-]{20,})", url or "")
    return m.group(1) if m else ""


def maps_link(r):
    """Maps link from the admin JSON (google_maps_link since commit 329f670; older servers: google_maps_url)."""
    return r.get("google_maps_link") or r.get("google_maps_url") or ""


def maps_place_id(r):
    return r.get("google_place_id") or place_id_from(maps_link(r))


def main(argv=None):
    ap = argparse.ArgumentParser(description="anuongquanhday.com admin client (JSON admin API)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    p = sub.add_parser("get"); p.add_argument("id", type=int)
    p = sub.add_parser("photos"); p.add_argument("id", type=int)
    p = sub.add_parser("dishes"); p.add_argument("id", type=int)
    p = sub.add_parser("update-info"); p.add_argument("id", type=int); p.add_argument("json", help="JSON object or @file.json")
    p = sub.add_parser("validate"); p.add_argument("id", type=int)
    p = sub.add_parser("upload-photos"); p.add_argument("id", type=int); p.add_argument("paths", nargs="+")
    p = sub.add_parser("push-urls", help="fetch image URLs into memory and upload (prefix cover= / logo= to assign)")
    p.add_argument("id", type=int); p.add_argument("urls", nargs="+"); p.add_argument("--dir", help="applied.json folder (default C:/aqd/aqd_data/<id>)")
    p = sub.add_parser("assign"); p.add_argument("id", type=int); p.add_argument("photo_id", type=int); p.add_argument("role", choices=["cover", "logo"])
    p = sub.add_parser("upload-role"); p.add_argument("id", type=int); p.add_argument("path"); p.add_argument("role", choices=["cover", "logo"])
    p = sub.add_parser("save-menu"); p.add_argument("id", type=int); p.add_argument("tsv"); p.add_argument("--if-existing", choices=["skip", "missing"], default="skip")
    p = sub.add_parser("apply"); p.add_argument("ids", type=int, nargs="+"); p.add_argument("--dir", help="folder (default C:/aqd/aqd_data/<id>)")
    p = sub.add_parser("drafts", help="list drafts: id <TAB> name <TAB> google_maps_link (API; = google_maps_url when stored)")
    p.add_argument("--status", default="draft", choices=["draft", "validated", "published"]); p.add_argument("--q"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("survey-photos"); p.add_argument("lo", type=int); p.add_argument("hi", type=int); p.add_argument("--out", default="C:/aqd/aiw/survey")
    p = sub.add_parser("fetch"); p.add_argument("id", type=int); p.add_argument("outdir"); p.add_argument("photo_ids", type=int, nargs="*")
    a = ap.parse_args(argv)
    c = AQD(verbose=a.verbose)
    try:
        if a.cmd == "login":
            c.login(); out = {"ok": True}
        elif a.cmd == "get":
            out = c.get(a.id)
        elif a.cmd == "photos":
            out = c.photos(a.id)
        elif a.cmd == "dishes":
            out = c.dishes(a.id)
        elif a.cmd == "update-info":
            src = open(a.json[1:], encoding="utf-8").read() if a.json.startswith("@") else a.json
            out = c.update_info(a.id, json.loads(src)); out.pop("restaurant", None)
        elif a.cmd == "validate":
            out = c.validate(a.id)
        elif a.cmd == "upload-photos":
            out = c.upload_photos(a.id, a.paths)
        elif a.cmd == "push-urls":
            out = c.push_urls_recorded(a.id, a.urls, a.dir)
        elif a.cmd == "assign":
            out = c.assign(a.id, a.photo_id, a.role)
        elif a.cmd == "upload-role":
            out = c.upload_role(a.id, a.path, a.role)
        elif a.cmd == "save-menu":
            out = c.save_menu(a.id, a.tsv, if_existing=a.if_existing)
        elif a.cmd == "drafts":
            d = c.drafts(a.status, a.q)
            if a.json:
                out = [{"id": r["id"], "name": r.get("name"), "google_maps_url": r.get("google_maps_url"),
                        "google_maps_link": maps_link(r), "google_place_id": maps_place_id(r)}
                       for r in d["restaurants"]]
            else:
                for r in d["restaurants"]:
                    print(f"{r['id']}\t{r.get('name') or ''}\t{maps_link(r)}")
                print(f"# {len(d['restaurants'])} {a.status} (total_count {d['total_count']})", file=sys.stderr)
                return 0
        elif a.cmd == "survey-photos":
            os.makedirs(a.out, exist_ok=True)
            for rid in range(a.lo, a.hi + 1):
                fn = os.path.join(a.out, f"{rid}.json")
                if os.path.exists(fn):
                    continue
                try:
                    data = {"id": rid, "photos": c.photos(rid)}
                except AQDError as e:
                    data = {"id": rid, "error": str(e)}
                with open(fn + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(fn + ".tmp", fn)
                print(rid, flush=True)
            out = {"ok": True}
        elif a.cmd == "fetch":
            os.makedirs(a.outdir, exist_ok=True)
            got = []
            for ph in c.photos(a.id):
                if a.photo_ids and ph["id"] not in a.photo_ids:
                    continue
                if not ph["url"]:
                    continue
                r = requests.get(ph["url"], timeout=60)
                if r.status_code != 200:
                    got.append({"id": ph["id"], "error": r.status_code}); continue
                ext = os.path.splitext(urlparse(r.url).path)[1] or ".jpg"
                fn = os.path.join(a.outdir, f"{ph['id']}{ext}")
                with open(fn, "wb") as f:
                    f.write(r.content)
                got.append({"id": ph["id"], "file": fn, "bytes": len(r.content)})
            out = got
        elif a.cmd == "apply":
            rc = 0
            for rid in a.ids:
                try:
                    if not c.apply(rid, a.dir if len(a.ids) == 1 else None)["ok"]:
                        rc = 1
                except LoginError:
                    raise
                except Exception as e:
                    line = f"- {_now()} #{rid} ERROR {type(e).__name__}: {e}"
                    with open(LOG_FILE, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
                    print(line); rc = 1
            return rc
        print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
        return 0
    except LoginError as e:
        print(f"LOGIN ERROR: {e}", file=sys.stderr); return 3
    except AQDError as e:
        print(f"ERROR: {e}", file=sys.stderr); return 2


if __name__ == "__main__":
    sys.exit(main())
