"""Push the Google extras kept in extra.json to the /v1 admin API (#679 About, #680 reviews, #685 hours).

  PATCH /v1/admin/restaurants/:id               {google_attributes:[{group,label,items:[{id,label,value:bool}]}], description}
  PUT   /v1/admin/restaurants/:id/google_reviews {google_rating, google_review_count, rating_histogram, reviews[<=20]}
  PUT   /v1/admin/restaurants/:id/opening_hours  {source:"google", periods:[{weekday 0=Mon, open, close}], closed_days,
                                                  is_24h_days, raw_text}; 409 opening_hours_owner_confirmed -> skip
Payloads mirror the server validators (app/services/google_attributes/validate.rb, google_reviews/validate.rb,
opening_hours/replace_week.rb) so a bad item is dropped here instead of failing the whole request."""
import re
from urllib.parse import urlparse

API = "/v1/admin/restaurants"
KEY_RE = re.compile(r"\A[a-z0-9_]{1,64}\Z")
MAX_GROUPS, MAX_ITEMS, MAX_LABEL = 20, 60, 120
MAX_REVIEWS, MAX_PHOTOS = 20, 5


def server_key(raw):
    """extra.about id/group ('/geo/type/establishment_poi/serves_dine_in', 'Has-WiFi') -> server key [a-z0-9_]{1,64}."""
    k = str(raw or "").rsplit("/", 1)[-1].strip().lower()
    k = re.sub(r"[^a-z0-9_]+", "_", k).strip("_")[:64]
    return k if KEY_RE.match(k) else None


def _label(v, fallback):
    s = (v if isinstance(v, str) else "").strip() or (fallback or "")
    return s[:MAX_LABEL] or None


def attributes_payload(about):
    out, n = [], 0
    for grp in about or []:
        if len(out) >= MAX_GROUPS or n >= MAX_ITEMS or not isinstance(grp, dict):
            break
        gk = server_key(grp.get("group"))
        gl = _label(grp.get("label"), gk)
        if not gk or not gl:
            continue
        items, seen = [], set()
        for it in grp.get("items") or []:
            if n + len(items) >= MAX_ITEMS:
                break
            if not isinstance(it, dict) or not isinstance(it.get("value"), bool):
                continue  # booleans only
            ik = server_key(it.get("id"))
            il = _label(it.get("label"), it.get("text") or ik)
            if not ik or not il or ik in seen:
                continue
            seen.add(ik)
            items.append({"id": ik, "label": il, "value": it["value"]})
        if items:
            out.append({"group": gk, "label": gl, "items": items}); n += len(items)
    return out


def _lh3(u):
    try:
        p = urlparse(u)
    except Exception:
        return False
    return p.scheme == "https" and p.hostname == "lh3.googleusercontent.com" and not p.username


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else (int(v) if isinstance(v, str) and v.isdigit() else None)


def reviews_payload(ex):
    body = {}
    r = ex.get("google_rating")
    if isinstance(r, (int, float)) and not isinstance(r, bool) and 1 <= r <= 5:
        body["google_rating"] = r
    c = _int(ex.get("google_review_count"))
    if c is not None and c >= 0:
        body["google_review_count"] = c
    h = ex.get("rating_histogram")
    if isinstance(h, dict) and all(_int(h.get(k)) is not None for k in "12345"):
        body["rating_histogram"] = {k: _int(h[k]) for k in "12345"}
    rows, seen = [], set()
    for rv in ex.get("reviews") or []:
        if len(rows) >= MAX_REVIEWS:
            break
        rid, rating = rv.get("id"), _int(rv.get("rating"))
        if not isinstance(rid, str) or not rid or rid in seen or rating is None or not 1 <= rating <= 5:
            continue
        seen.add(rid)
        photos = []
        for ph in (rv.get("photos") or []) + [{"url": u} for u in rv.get("photo_urls") or [] if isinstance(u, str)]:
            u = ph.get("url") if isinstance(ph, dict) else None
            if not _lh3(u) or any(p["url"] == u for p in photos):
                continue
            p = {"url": u}
            if ph.get("label") in ("food", "drink"):
                p["label"] = ph["label"]
            photos.append(p)
            if len(photos) >= MAX_PHOTOS:
                break
        row = {"id": rid, "rating": rating, "photos": photos}
        for k, mx in (("author", 120), ("text", 5000), ("language", 8)):
            v = rv.get(k)
            if isinstance(v, str) and v.strip():
                row[k] = v[:mx]
        if isinstance(rv.get("published_at"), str):
            row["published_at"] = rv["published_at"]
        if isinstance(rv.get("relative_date"), str) and rv["relative_date"]:
            row["relative_date"] = rv["relative_date"]
        rows.append(row)
    body["reviews"] = rows
    return body


def hours_payload(ex):
    """extra.opening_periods (weekday 1=Mon..7) -> server week (0=Mon). None when Google lists no hours."""
    per, by_day = ex.get("opening_periods"), ex.get("hours_by_weekday") or {}
    if not per and not by_day:
        return None
    periods, full = [], set()
    for p in per or []:
        wd = _int(p.get("weekday"))
        if wd is None or not 1 <= wd <= 7 or not p.get("opens_at"):
            continue
        if p["opens_at"] == "00:00" and not p.get("closes_at"):
            full.add(wd - 1); continue
        close = p.get("closes_at") or "24:00"
        if close == p["opens_at"]:
            continue
        periods.append({"weekday": wd - 1, "open": p["opens_at"], "close": close})
    with_rows = {p["weekday"] for p in periods} | full
    closed = sorted(int(k) - 1 for k, v in by_day.items() if str(k).isdigit() and 1 <= int(k) <= 7
                    and (int(k) - 1) not in with_rows and "đóng cửa" in (v or "").lower())
    if not periods and not full and not closed:
        return None
    return {"source": "google", "periods": periods, "closed_days": closed, "is_24h_days": sorted(full),
            "raw_text": {str(int(k) - 1): v for k, v in by_day.items() if str(k).isdigit() and 1 <= int(k) <= 7}}


def push(req, rid, ex, current=None, api_error=Exception):
    """req(method, path, json_body=...) -> dict. Returns a short log string; never raises for a 4xx."""
    parts = []

    def code(e):
        return f"{getattr(e, 'status', '?')}:{getattr(e, 'code', type(e).__name__)}"

    attrs = attributes_payload(ex.get("about"))
    body = {}
    if attrs:
        body["google_attributes"] = attrs
    desc = (ex.get("description") or "").strip()
    if desc and not ((current or {}).get("description") or "").strip():
        body["description"] = desc[:1000]
    if body:
        try:
            req("PATCH", f"{API}/{rid}", json_body=body)
            parts.append(f"about={sum(len(g['items']) for g in attrs)}" + ("+desc" if "description" in body else ""))
        except api_error as e:
            if getattr(e, "status", 500) >= 500 or getattr(e, "status", 0) == 429: raise
            parts.append(f"about_fail={code(e)}")
    rb = reviews_payload(ex)
    if rb.get("reviews") or "google_rating" in rb:
        try:
            got = req("PUT", f"{API}/{rid}/google_reviews", json_body=rb)
            parts.append(f"reviews={len((got or {}).get('reviews') or [])}/{len(rb['reviews'])}")
        except api_error as e:
            if getattr(e, "status", 500) >= 500 or getattr(e, "status", 0) == 429: raise
            parts.append(f"reviews_fail={code(e)}")
    hb = hours_payload(ex)
    if hb is None:
        parts.append("hours=none_on_google")
    else:
        try:
            got = req("PUT", f"{API}/{rid}/opening_hours", json_body=hb)
            parts.append(f"hours={(got or {}).get('opening_hours', {}).get('source')}")
        except api_error as e:
            if getattr(e, "status", 0) == 409:
                parts.append("hours=skip_owner_confirmed")
            elif getattr(e, "status", 500) >= 500 or getattr(e, "status", 0) == 429:
                raise
            else:
                parts.append(f"hours_fail={code(e)}")
    return " ".join(parts)
