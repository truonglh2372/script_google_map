#!/usr/bin/env python3
"""Browser-free Google Maps place fetch (plain HTTP, no Chrome).

  maps_fetch.py <place_id> [--raw FILE] [--rid N]

Requests (the same two a normal page load makes, nothing else):
  1. GET https://www.google.com/maps/place/?q=place_id:<id>&hl=vi        (HTML shell)
  2. GET the /maps/preview/place?...pb=... URL embedded in that HTML     (place JSON, ")]}'" prefix)
Prints JSON shaped like aqd_data/<id>/info.json plus photo_urls ([{url, role?, kind}]) and _meta.
Signed-out view: the photo set is what the preview embeds (a few per category), NOT the full gallery.
Polite: >=1.1 s between requests in a process; stops on HTTP 429 / 'sorry' / consent pages.
Exit codes: 0 ok, 2 error, 3 no place block, 4 blocked. _meta.thin=true -> reduced payload (types/photos missing).
Nothing is written anywhere (stdout only)."""
import argparse, html, json, os, re, sys, time
import requests

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
MIN_GAP = 1.1
_last = [0.0]
_s = None

CAFE = {"cafe", "coffee_shop", "tea_house", "bubble_tea_store", "juice_shop", "dessert_shop", "ice_cream_shop",
        "cafeteria", "coffee_stand"}
RESTAURANT_HINT = ("restaurant", "steak_house", "buffet", "barbecue", "seafood", "hot_pot", "brewpub", "pub", "bar",
                   "beer", "bistro", "diner", "food_court")
EATERY = ("noodle", "pho", "rice", "snack", "meal_takeaway", "takeout", "bakery", "sandwich", "food",
          "vegetarian", "eatery", "breakfast", "lunch", "chicken", "pizza", "fast_food", "deli")


class Blocked(Exception):
    pass


def _get(url, **kw):
    global _s
    if _s is None:
        _s = requests.Session()
        _s.headers.update({"User-Agent": UA, "Accept-Language": "vi,en;q=0.8"})
    wait = MIN_GAP - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    r = _s.get(url, timeout=30, **kw)
    _last[0] = time.time()
    if r.status_code == 429 or "/sorry/" in r.url or "consent.google" in r.url:
        raise Blocked(f"HTTP {r.status_code} {r.url[:80]}")
    r.raise_for_status()
    return r


def g(o, *ix):
    for i in ix:
        try:
            o = o[i]
        except (IndexError, TypeError, KeyError):
            return None
    return o


def walk(o):
    if isinstance(o, list):
        yield o
        for x in o:
            yield from walk(x)


def fetch_place_json(place_id):
    r = _get(f"https://www.google.com/maps/place/?q=place_id:{place_id}&hl=vi")
    m = re.search(r'(/maps/preview/place\?[^"\'\s]+)', r.text)
    if not m:
        raise Blocked("no preview/place URL in page (layout change or block)")
    r2 = _get("https://www.google.com" + html.unescape(m.group(1)), headers={"Referer": r.url})
    t = r2.text
    return json.loads(t[t.index("\n") + 1:] if t.startswith(")]}'") else t)


def _base(url):
    return url.split("=")[0] if "googleusercontent.com" in url else url


def photos(p):
    """Distinct photos embedded in the preview. Photo node: [..., 6:[url, label, [w,h]], ..., 21:[..19:[kind, ref]]]."""
    by_ref, order = {}, []
    cat_of = {}
    for c in g(p, 171, 0) or []:
        for ph in g(c, 3) or []:
            ref = g(ph, 21, 19, 1)
            if ref:
                cat_of.setdefault(ref, set()).add(g(c, 2))
    for node in walk(p):
        url, kind, ref = g(node, 6, 0), g(node, 21, 19, 0), g(node, 21, 19, 1)
        if not (isinstance(url, str) and isinstance(ref, str) and isinstance(kind, str)):
            continue
        if kind != "UGCS_REFERENCE" or "googleusercontent.com" not in url:
            continue                                   # skip Street View thumbnails
        good = "/gps-cs-s/" in url or "/p/" in url     # direct photo URL; grass-cs is a proxy copy
        if ref not in by_ref:
            order.append(ref)
            by_ref[ref] = url
        elif good and "/grass-cs/" in by_ref[ref]:
            by_ref[ref] = url
    return [{"url": _base(by_ref[r]), "ref": r.rstrip("|"), "categories": sorted(x for x in cat_of.get(r, ()) if x)}
            for r in order]


def hours_text(p):
    days = g(p, 203, 0) or []
    order = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]
    val = {}
    for d in days:
        name, slots = g(d, 0), g(d, 3) or []
        txt = ", ".join(s[0] for s in slots if isinstance(g(s, 0), str))
        if txt == "Mở cửa cả ngày":
            txt = "Mở cửa 24 giờ"
        val[name] = txt or "Đóng cửa"
    if not val:
        return ""
    groups = []
    for k in order:
        if k not in val:
            continue
        if groups and groups[-1][2] == val[k] and order.index(groups[-1][1]) == order.index(k) - 1:
            groups[-1][1] = k
        else:
            groups.append([k, k, val[k]])
    return "; ".join((a if a == b else f"{a}–{b}") + ": " + v for a, b, v in groups)


# ---- extras (2026-10-17): Google rating/reviews sample, weekly opening periods, About attributes ----
def _env_int(name, default, lo=1):
    try:
        return max(lo, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


# an_uong_quanh_day#1083: keep up to AQD_MAX_REVIEWS (default 50) reviews per place. The preview embeds only 5-8, the
# rest come from paging listugcposts in the signed-in Chrome (more_reviews); signed out, Google answers 403.
REVIEW_CAP = _env_int("AQD_MAX_REVIEWS", 50)
UGC_PAGE = 20
UGC_MAX_PAGES = 6
_WD = {"Thứ Hai": 1, "Thứ Ba": 2, "Thứ Tư": 3, "Thứ Năm": 4, "Thứ Sáu": 5, "Thứ Bảy": 6, "Chủ Nhật": 7}


def _hm(x):
    if not isinstance(x, list) or not x or not isinstance(x[0], int):
        return None
    h, m = x[0], (x[1] if len(x) > 1 and isinstance(x[1], int) else 0)
    return f"{h % 24:02d}:{m:02d}"


def opening_periods(p):
    """[{weekday 1=Mon..7=Sun, opens_at 'HH:MM', closes_at 'HH:MM'|None}] + per-day text.
    24h -> opens 00:00, closes None (server: open until end of day). Closed day -> no period. Split shifts -> 2 rows."""
    periods, by_day, ok = [], {}, False
    for d in g(p, 203, 0) or []:
        name = g(d, 0); wd = g(d, 1) if isinstance(g(d, 1), int) else _WD.get(name)
        if not isinstance(wd, int) or not 1 <= wd <= 7:
            continue
        slots = g(d, 3) or []
        texts = [s[0] for s in slots if isinstance(g(s, 0), str)]
        by_day[wd] = ", ".join(texts) or "Đóng cửa"; ok = True
        for s in slots:
            t = g(s, 0) or ""
            if "cả ngày" in t or "24 giờ" in t:
                periods.append({"weekday": wd, "opens_at": "00:00", "closes_at": None}); continue
            o, c = _hm(g(s, 1, 0)), _hm(g(s, 1, 1))
            if o is None:
                continue
            if c == o:  # "00:00–24:00"
                c = None
            periods.append({"weekday": wd, "opens_at": o, "closes_at": c})
    periods.sort(key=lambda r: (r["weekday"], r["opens_at"]))
    return (periods if ok else None), {str(k): v for k, v in sorted(by_day.items())}


def _ts(us):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S+07:00", time.gmtime(int(us) / 1e6 + 7 * 3600))
    except (TypeError, ValueError):
        return None


def review_row(n):
    """One review node (the same shape in the preview and in listugcposts) -> row, or None."""
    if not isinstance(n, list):
        return None
    meta, body = g(n, 1) or [], g(n, 2) or []
    rating = g(body, 0, 0)
    text = g(body, 15, 0, 0)
    photos = []
    for ph in g(body, 2) or []:
        u = g(ph, 1, 6, 0)
        if isinstance(u, str) and "googleusercontent.com" in u:
            photos.append(_base(u))
    return {"id": g(n, 0), "author": g(meta, 4, 5, 0), "rating": rating if isinstance(rating, int) else None,
            "text": text if isinstance(text, str) else "", "published_at": _ts(g(meta, 2)),
            "relative_date": g(meta, 6), "language": g(body, 14, 0), "photo_urls": photos}


def _newest_first(rows):
    rows.sort(key=lambda x: x.get("published_at") or "", reverse=True)
    return rows


def reviews(p, cap=None):
    """Reviews embedded in the place preview (no extra request). Author display name only (no profile id/url/avatar)."""
    cap = REVIEW_CAP if cap is None else cap
    out = []
    for r in g(p, 175, 9, 0, 0) or []:
        row = review_row(g(r, 0))
        if row is None:
            continue
        out.append(row)
        if len(out) >= cap:
            break
    return _newest_first(out)


def ugc_path(hexid, token="", n=UGC_PAGE, sid="aqd"):
    """Same-origin path of the Maps reviews pager (what the Reviews tab calls while scrolling), newest first."""
    return ("/maps/rpc/listugcposts?authuser=0&hl=vi&gl=vn&pb="
            f"!1m6!1s{hexid}!6m4!4m1!1e1!4m1!1e3!2m2!1i{int(n)}!2s{token or ''}!5m2!1s{sid}!7e81"
            "!8m9!2b1!3b1!5b1!7b1!12m4!1b1!2b1!4m1!1e1!11m0!13m1!1e1")


def parse_ugc(text):
    """listugcposts body (")]}'" + JSON) -> (rows, next_token|None). Raises ValueError on a non-JSON body."""
    t = (text or "").lstrip()
    if t.startswith(")]}'"):
        t = t[4:]
    d = json.loads(t)
    rows = [x for x in (review_row(g(r, 0)) for r in (g(d, 2) or []) if isinstance(r, list)) if x and x.get("id")]
    tok = g(d, 1)
    return rows, (tok if isinstance(tok, str) and tok else None)


def more_reviews(fetch, hexid, have, cap=None, want=None, page=UGC_PAGE, max_pages=UGC_MAX_PAGES):
    """Page the reviews of `hexid` until `cap` rows (or `want`, Google's review count) or the source runs out.

    fetch(path) -> (http_status, body_text): a same-origin GET in the signed-in Chrome. `have` (the preview rows)
    keeps its place; new ids are appended. Best effort: any problem keeps what we have.
    Returns (rows newest first, note) with note like "ugc+32/2p", "ugc_http403", "ugc_err:ValueError"."""
    cap = REVIEW_CAP if cap is None else cap
    goal = min(cap, want) if isinstance(want, int) and want > 0 else cap
    rows = [r for r in (have or []) if r.get("id")][:cap]
    seen = {r["id"] for r in rows}
    if not hexid or len(rows) >= goal:
        return _newest_first(rows), "ugc_skip"
    tok, pages, added, note = "", 0, 0, None
    while len(rows) < goal and pages < max_pages:
        try:
            status, body = fetch(ugc_path(hexid, tok, page))
        except Exception as e:
            note = f"ugc_err:{type(e).__name__}"; break
        pages += 1
        if status != 200:
            note = f"ugc_http{status}"; break
        try:
            got, tok = parse_ugc(body)
        except (ValueError, TypeError) as e:
            note = f"ugc_err:{type(e).__name__}"; break
        new = [r for r in got if r["id"] not in seen]
        for r in new:
            if len(rows) >= cap:
                break
            rows.append(r); seen.add(r["id"]); added += 1
        if not new or not tok:
            break
    return _newest_first(rows), note or f"ugc+{added}/{pages}p"


def about(p):
    """About tab: [{group, label, items:[{id, label, value(bool)}]}]."""
    groups = []
    for grp in g(p, 100, 1) or []:
        items = []
        for it in g(grp, 2) or []:
            flag = g(it, 2, 0)
            items.append({"id": (g(it, 0) or "").rsplit("/", 1)[-1], "label": g(it, 1),
                          "value": bool(flag == 1), "text": g(it, 2, 2, 3) or g(it, 1)})
        if items:
            groups.append({"group": g(grp, 0), "label": g(grp, 1), "items": items})
    return groups


def description(p):
    for path in ((32, 1, 1), (32, 0, 1), (154, 0, 0)):
        v = g(p, *path)
        if isinstance(v, str) and len(v) > 10:
            return v.strip()
    return ""


def extras(p):
    periods, by_day = opening_periods(p)
    return {"google_rating": g(p, 4, 7), "google_review_count": g(p, 4, 8),
            "rating_histogram": dict(zip(["1", "2", "3", "4", "5"], g(p, 175, 3) or [])) or None,
            "reviews": reviews(p), "opening_periods": periods, "hours_by_weekday": by_day,
            "about": about(p), "description": description(p),
            # Google lists no hours at all (the panel shows "Thêm giờ"): empty hours_by_weekday is real, not a parse miss
            "hours_listed": bool(by_day)}


def category(types, label):
    t = " ".join(types).lower()
    if any(x in types for x in CAFE) or "cà phê" in (label or "").lower():
        return "cafe"
    if any(h in t for h in RESTAURANT_HINT) and "nhà hàng" in (label or "").lower():
        return "restaurant"
    if any(h in t for h in RESTAURANT_HINT + EATERY):
        return "eatery"
    return "other"


def _coord(x):
    return str(round(float(x), 7)) if isinstance(x, (int, float)) else ""


def parse(p, place_id):
    name = g(p, 11) or ""
    addr = g(p, 39) or ""
    comp = g(p, 183, 1) or []
    street = g(comp, 1) or ""
    m = re.match(r"^((?:Hẻm |Kiệt |Số )?[\d/A-Za-z-]*\d[\d/A-Za-z-]*)\s+(.*)$", street)
    num, route = (m.group(1), m.group(2)) if m else ("", street)
    types = [t[0] for t in (g(p, 76) or []) if g(t, 0)]
    labels = [x for x in (g(p, 13) or []) if isinstance(x, str)]
    phone_local = g(p, 178, 0, 1, 0, 0) or ""
    phone_intl = g(p, 178, 0, 0) or ""
    strings = [x for node in walk(p) for x in node if isinstance(x, str)]
    status = "OPERATIONAL"
    if any("Đóng cửa vĩnh viễn" in s for s in strings):
        status = "CLOSED_PERMANENTLY"
    elif any(re.search(r"Tạm (thời )?đóng cửa", s) for s in strings):
        status = "CLOSED_TEMPORARILY"
    web = g(p, 7, 0) or ""
    fb = web if re.search(r"(facebook\.com|fb\.com|fb\.me)", web) else ""
    ward_district = [x.strip() for x in (g(comp, 0) or "").split(",")]
    ph = photos(p)
    count_label = next((s for s in strings if re.fullmatch(r"[\d.]+ ảnh", s)), "")
    info = {
        "name": name, "address": addr,
        "latitude": _coord(g(p, 9, 2)), "longitude": _coord(g(p, 9, 3)),
        "phone": phone_intl or phone_local, "website": "" if fb else web, "facebook_url": fb,
        "opening_hours_text": hours_text(p),
        "short_address": street, "address_street_number": num, "address_route": route,
        "address_ward": "",
        "address_district": (g(comp, 5) or "").split(",")[0].strip() or ward_district[-1],
        "address_city": g(comp, 3) or "", "address_country_code": g(comp, 6) or "VN",
        "address_postal_code": g(comp, 4) or "",
        "plus_code_global": g(p, 183, 2, 1, 0) or "", "plus_code_compound": g(p, 183, 2, 2, 0) or "",
        "google_primary_type": types[0] if types else "", "google_primary_type_label": labels[0] if labels else "",
        "google_types_text": ", ".join(dict.fromkeys(types + ["point_of_interest", "establishment"])),
        "google_maps_url": f"https://www.google.com/maps/search/?api=1&query={place_id}&query_place_id={place_id}",
        "name_language_code": "vi", "utc_offset_minutes": "420" if g(p, 30) in ("Asia/Saigon", "Asia/Ho_Chi_Minh") else "",
        "cuisine": labels[0] if labels else "", "category": category(types, labels[0] if labels else ""),
        "business_status": status,
        "photo_urls": [{"url": x["url"], "kind": ",".join(x["categories"])} for x in ph],
        "_meta": {"place_id": g(p, 78) or place_id, "maps_photo_count_label": count_label,
                  "photos_embedded": len(ph), "phone_local": phone_local, "open_now_text": g(p, 203, 1, 4, 0) or "",
                  "photo_categories": [g(c, 2) for c in (g(p, 171, 0) or [])], "hex_id": g(p, 10),
                  # Google sometimes serves a reduced payload (no types / photo tabs / fewer photos);
                  # seen after ~35-40 places in ~15 min from this IP. Treat as "retry later", not as data.
                  "thin": g(p, 76) is None and g(p, 171) is None},
    }
    try:
        info["_meta"]["extra"] = extras(p)
    except Exception as e:  # noqa: BLE001
        info["_meta"]["extra"] = {"error": f"{type(e).__name__}: {e}"}
    # an address-only "name" (e.g. "62 Nguyễn Đình Chiểu") is flagged, not fixed
    if re.match(r"^\d", name) and name.split(",")[0] == street:
        info["_meta"]["name_is_address"] = True
    return info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("place_id")
    ap.add_argument("--raw", help="also save the raw place JSON here (debug)")
    a = ap.parse_args()
    t0 = time.time()
    try:
        d = fetch_place_json(a.place_id)
    except Blocked as e:
        print(json.dumps({"error": "blocked", "detail": str(e)}), file=sys.stderr); return 4
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": type(e).__name__, "detail": str(e)[:200]}), file=sys.stderr); return 2
    p = g(d, 6)
    if not isinstance(p, list):
        print(json.dumps({"error": "no_place", "detail": "preview JSON has no place block"}), file=sys.stderr); return 3
    if a.raw:
        with open(a.raw, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    info = parse(p, a.place_id)
    info["_meta"]["seconds"] = round(time.time() - t0, 2)
    print(json.dumps(info, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
