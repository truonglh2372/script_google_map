#!/usr/bin/env python3
"""Browser-free fast pass for unnamed new drafts: maps_fetch -> category/non-food check -> info.json -> apply.
Run through creds:  bash -c 'set -a; . C:/aqd/.aqd_session/creds.env; set +a; python3 C:/aqd/fast_pass.py'
Options: --dry (no info.json / apply), --limit N.
Ranges are worked bottom-up, round-robin: #3175.., #2638.., #2101.. (other bots go top-down).
Log: C:/aqd/fast_pass_log.md"""
import argparse, json, os, re, sys, time
sys.path.insert(0, "C:/aqd")
import maps_fetch as mf
from aqd_api import AQD, AQDError, DATA_DIR, maps_place_id
from olcdec import decode as olc_decode

LOG = "C:/aqd/fast_pass_log.md"
API_LOG = "C:/aqd/aqd_api_log.md"
INTENTIONAL = {int(x) for x in "1857 1777 1755 1736 1734 1730 1604 1582 1499 1430 1426 1421 1247 802 792 747 734 589 "
               "580 466 464 308 1 2625 2634".split()}
RANGES = [(3175, 3395), (2638, 2974), (2101, 2576)]
PER_PLACE_S = 3.0
BATCH, PAUSE_S, BLOCK_WAIT_S = 30, 120, 15 * 60

CAFE_KW = ["cà phê", "cafe", "café", "coffee", "trà sữa", "trà", "tea", "milk tea", "sinh tố", "nước ép", "juice",
           "giải khát", "kem", "chè", "dessert", "bubble", "matcha", "smoothie", "cacao"]
REST_KW = ["nhà hàng", "lẩu", "nướng", "buffet", "restaurant", "hải sản", "bbq", "barbecue", "hotpot", "hot pot",
           "steak", "beer club", "quán nhậu", "nhậu", "bia", "beer", "pub", "bistro"]
EAT_KW = ["quán", "cơm", "phở", "bún", "bánh", "ăn vặt", "hủ tiếu", "hủ tíu", "mì", "miến", "cháo", "xôi", "bò",
          "gà", "vịt", "ốc", "chay", "food", "ăn", "ẩm thực", "kitchen", "bếp", "snack", "pizza", "burger",
          "sandwich", "bánh mì", "dimsum", "sushi", "gimbap", "tokbokki", "nem", "chả", "bún đậu", "mỳ", "lẩu",
          "eatery", "điểm tâm", "noodle", "rice", "bakery", "tiệm bánh", "đặc sản", "hàu", "dê", "heo", "cua",
          "tào phớ", "sữa chua", "yaourt", "drink", "đồ uống"]
FOOD_TYPES = ["restaurant", "cafe", "coffee", "food", "bakery", "bar", "pub", "tea", "juice", "dessert", "ice_cream",
              "meal", "diner", "bistro", "eatery", "noodle", "brewpub", "barbecue", "buffet", "snack", "deli",
              "pizza", "soup", "sandwich", "steak", "seafood", "vegetarian", "hot_pot", "confectionery", "beer",
              "bubble_tea", "cafeteria", "caterer", "brewery", "winery"]
NONFOOD_KW = ["khách sạn", "hotel", "nhà nghỉ", "homestay", "resort", "motel", "hostel", "công ty", "company",
              "tnhh", "salon", "tóc", "hair", "spa", "nail", "massage", "gara", "garage", "sửa xe", "rửa xe",
              "trường", "school", "mầm non", "bida", "billiard", "bi-a", "karaoke", "studio", "nhà thuốc",
              "pharmacy", "ngân hàng", "bank", "văn phòng", "office", "nội thất", "điện thoại", "điện máy",
              "thời trang", "fashion", "shop quần áo", "giày", "tạp hóa", "siêu thị", "chợ", "phòng khám",
              "clinic", "bệnh viện", "gym", "fitness", "yoga", "nhà xe", "vận tải", "xây dựng", "vật liệu",
              "phân bón", "nông sản", "thú y", "pet", "in ấn", "photocopy", "khu dân cư", "chung cư", "nhà riêng",
              "trạm", "cây xăng", "xăng dầu", "đại lý", "nhà sách", "điện tử", "sân bóng", "sân cầu lông",
              "lodging", "store", "agency", "beauty", "car_", "real_estate", "school", "doctor", "dentist",
              "church", "chùa", "nhà thờ", "lodging", "camp", "farm", "trang trại", "xưởng"]
NONFOOD_TYPES = ["lodging", "hotel", "store", "shop", "salon", "spa", "school", "car_", "bank", "pharmacy", "gym",
                 "doctor", "dentist", "hospital", "real_estate", "agency", "corporate", "company", "church",
                 "place_of_worship", "farm", "billiards", "karaoke", "massage", "beauty"]


def kw_in(text, kws):
    t = (text or "").lower()
    return [k for k in kws if re.search(r"(?<!\w)" + re.escape(k) + r"(?!\w)", t)]


STRONG_NONFOOD_NAME = ["bida", "billiard", "billiards", "bi-a", "karaoke", "khách sạn", "hotel", "nhà nghỉ", "motel",
                       "spa", "salon", "gara", "homestay", "resort", "garage", "sửa xe", "rửa xe", "massage", "studio", "nail", "tóc"]
ALWAYS_NONFOOD_NAME = ["bida", "bi da", "bi-a", "billiard", "billiards", "karaoke", "hồ câu", "câu cá"]
REST_TYPES = ("barbecue", "seafood", "buffet", "hot_pot", "steak", "fine_dining", "korean_barbecue", "brewpub")


def log(line):
    with open(LOG, "a") as f:
        f.write(f"- {time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    print(line, flush=True)


def append(path, rid, extra=""):
    with open(path, "a") as f:
        f.write(f"{rid}{(' ' + extra) if extra else ''}\n")


def classify(info, types):
    label = info.get("google_primary_type_label") or ""
    labels = label + " " + (info.get("cuisine") or "")
    name = info.get("name") or ""
    tjoin = " ".join(types).lower()
    primary_t = (types[0] if types else "").lower()
    food_t = any(f in tjoin for f in FOOD_TYPES)
    primary_food_t = any(f in primary_t for f in FOOD_TYPES)
    nonfood_primary = (primary_t and any(n in primary_t for n in NONFOOD_TYPES) and not primary_food_t)
    label_food = bool(kw_in(label, CAFE_KW + REST_KW + EAT_KW))
    label_nonfood = kw_in(label, NONFOOD_KW)
    name_food = kw_in(name, CAFE_KW + REST_KW + EAT_KW)
    name_nonfood = kw_in(name, NONFOOD_KW)
    reason = ""
    strong = kw_in(name, STRONG_NONFOOD_NAME)
    always = kw_in(name, ALWAYS_NONFOOD_NAME)
    if nonfood_primary:
        reason = f"primary type {primary_t}"
    elif always:
        reason = f"name has {always[0]!r}"
    elif strong and not name_food:
        reason = f"name has {strong[0]!r}"
    elif label_nonfood and not label_food:
        reason = f"label '{label}'"
    elif name_nonfood and not name_food and not label_food and not primary_food_t:
        reason = f"name has {name_nonfood[0]!r}"
    elif not (food_t or label_food or name_food):
        reason = f"no food signal (label '{label}', types {types[:3]})"
    elif info.get("_name_is_address") and not (food_t or label_food):
        reason = "name is an address"
    # category (old convention: 'restaurant' only for real restaurants, small quán -> eatery)
    rest_label = [k for k in kw_in(label, REST_KW) if k not in ("nhà hàng", "restaurant")]
    if any(x in tjoin for x in ("cafe", "coffee", "tea", "juice", "dessert", "ice_cream", "bubble")) \
            or kw_in(label, CAFE_KW) or (kw_in(name, CAFE_KW) and not kw_in(name, REST_KW + EAT_KW)):
        cat = "cafe"
    elif kw_in(name, REST_KW) or rest_label or any(x in primary_t for x in REST_TYPES):
        cat = "restaurant"
    elif food_t or label_food or name_food:
        cat = "eatery"
    else:
        cat = "other"
    if cat == "cafe" and (kw_in(name, REST_KW) or rest_label) and not kw_in(name, CAFE_KW):
        cat = "restaurant"
    cuisine = label or ", ".join(name_food[:2])
    return cat, cuisine, reason


def pick_roles(photos):
    if not photos:
        return []
    out = [{"url": p["url"] + "=s1600"} for p in photos]
    kinds = [p.get("kind") or "" for p in photos]
    cover = next((i for i, k in enumerate(kinds) if "Của chủ sở hữu" in k), None)
    if cover is None:
        cover = next((i for i, k in enumerate(kinds) if "Tất cả" in k), 0)
    logo = next((i for i, k in enumerate(kinds) if ("Thực phẩm" in k or "Thực đơn" in k) and i != cover), None)
    if logo is None:
        logo = next((i for i in range(len(photos)) if i != cover), cover)
    out[cover]["role"] = "cover"
    if logo != cover:
        out[logo]["role"] = "logo"
    else:   # one photo: apply assigns the first role only, so add the same URL again as logo
        out.append({"url": out[cover]["url"], "role": "logo"})
    return out


def todo_ids(a):
    logtxt = open(API_LOG).read()
    done = {int(x) for x in re.findall(r"#(\d+) (?:OK|WARN)", logtxt)}
    stay = {int(m) for m in re.findall(r"#(\d+) \w+ .*stay_draft", logtxt)}
    drafts = a.drafts("draft")["restaurants"]
    ids = {r["id"] for r in drafts if not (r.get("name") or "").strip()}
    ids -= done | stay | INTENTIONAL
    per = [sorted(i for i in ids if lo <= i <= hi) for lo, hi in RANGES]
    out = []
    while any(per):
        for q in per:
            if q:
                out.append(q.pop(0))
    return out, {r["id"]: r for r in drafts}


def busy_elsewhere(rid):
    return os.path.exists(os.path.join(DATA_DIR, str(rid), "info.json")) or os.path.exists(f"C:/aqd/menus/f/{rid}.json")


def one(a, rid, row, dry):
    pid = maps_place_id(row)
    if not pid:
        return "skip", "no place_id"
    d = mf.fetch_place_json(pid)            # raises mf.Blocked
    p = mf.g(d, 6)
    if not isinstance(p, list):
        return "skip", "no place block in Maps response"
    info = mf.parse(p, pid)
    meta = info.pop("_meta")
    photos = info.pop("photo_urls")
    types = [t for t in (info.get("google_types_text") or "").split(", ") if t not in ("point_of_interest", "establishment")]
    info["_name_is_address"] = bool(meta.get("name_is_address"))
    cat, cuisine, reason = classify(info, types)
    info.pop("_name_is_address")
    info["category"], info["cuisine"] = cat, cuisine
    if not info.get("google_types_text") or info["google_types_text"] == "point_of_interest, establishment":
        info.pop("google_types_text", None); info.pop("google_primary_type", None)   # thin: don't send empty types
    notes = []
    pc = r"^[23456789CFGHJMPQRVWX]{4,8}\+[23456789CFGHJMPQRVWX]{2,3},\s*"
    for k in ("short_address", "address_route"):
        if re.match(pc, info.get(k) or ""):
            info[k] = re.sub(pc, "", info[k])
    if not info.get("latitude") and info.get("plus_code_global"):
        ll = olc_decode(info["plus_code_global"])
        if ll:
            info["latitude"], info["longitude"] = str(ll[0]), str(ll[1]); notes.append("coords from plus code")
    if not info.get("latitude"):
        reason = reason or "no coordinates and no plus code"
    if info.get("business_status") != "OPERATIONAL":
        notes.append(info["business_status"])
    if not info.get("name"):
        reason = reason or "no name from Maps"
    info["photo_urls"] = pick_roles(photos)
    if reason:
        info["stay_draft"] = True
    info["note"] = f"fast_pass {time.strftime('%H:%M')} thin={meta.get('thin')} photos {meta.get('photos_embedded')}/{meta.get('maps_photo_count_label') or '?'}"
    summary = (f"{info.get('name','')[:40]!r} cat={cat} photos={len(photos)}/{meta.get('maps_photo_count_label') or '?'} "
               f"thin={meta.get('thin')} phone={'y' if info.get('phone') else 'n'} hours={'y' if info.get('opening_hours_text') else 'n'}"
               + (f" STAY_DRAFT: {reason}" if reason else "") + (f" [{'; '.join(notes)}]" if notes else ""))
    if dry:
        return "dry", summary + "\n" + json.dumps(info, ensure_ascii=False)[:600]
    # re-check right before apply
    cur = a.restaurant(rid)
    if (cur.get("name") or "").strip() or cur.get("status") != "draft" or busy_elsewhere(rid):
        return "skip", "taken by another bot before apply"
    folder = os.path.join(DATA_DIR, str(rid))
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, "info.json.fp_tmp")
    with open(tmp, "w") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(folder, "info.json"))
    res = a.apply(rid)
    if reason:
        append("C:/aqd/fast_review.txt", rid, f"| {reason} | {info.get('name','')}")
    append("C:/aqd/review_queue.txt", rid)
    append("C:/aqd/gallery_queue.txt", rid)
    return ("ok" if res["ok"] else "warn"), summary + " | " + res["line"].split(f"#{rid} ", 1)[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ids", type=int, nargs="*")
    args = ap.parse_args()
    a = AQD()
    ids, rows = todo_ids(a)
    if args.ids:
        ids = [i for i in args.ids if i in rows]
    if args.limit:
        ids = ids[:args.limit]
    t_start = time.time()
    log(f"START {len(ids)} ids (dry={args.dry}) ranges {RANGES}")
    stats = {"ok": 0, "warn": 0, "skip": 0, "err": 0, "dry": 0, "stay": 0, "thin": 0, "fetched": 0}
    blocked_once = False
    for n, rid in enumerate(ids):
        if n and n % BATCH == 0:
            log(f"batch pause {PAUSE_S}s after {n} ({stats})")
            time.sleep(PAUSE_S)
        t0 = time.time()
        if busy_elsewhere(rid):
            stats["skip"] += 1; log(f"#{rid} SKIP busy elsewhere (info.json/menus file exists)"); continue
        try:
            cur = a.restaurant(rid)
            if (cur.get("name") or "").strip() or cur.get("status") != "draft":
                stats["skip"] += 1; log(f"#{rid} SKIP no longer an unnamed draft"); continue
            try:
                kind, msg = one(a, rid, cur, args.dry)
            except mf.Blocked as e:
                if blocked_once:
                    log(f"#{rid} BLOCKED again ({e}); stopping"); break
                blocked_once = True
                log(f"#{rid} BLOCKED ({e}); waiting {BLOCK_WAIT_S // 60} min then one retry")
                time.sleep(BLOCK_WAIT_S)
                kind, msg = one(a, rid, a.restaurant(rid), args.dry)
            stats["fetched"] += kind != "skip"
            stats["thin"] += "thin=True" in msg
            stats["stay"] += "STAY_DRAFT" in msg
            stats[kind] = stats.get(kind, 0) + 1
            log(f"#{rid} {kind.upper()} {msg}")
        except mf.Blocked as e:
            log(f"#{rid} BLOCKED on retry ({e}); stopping"); break
        except Exception as e:  # noqa: BLE001
            stats["err"] += 1; log(f"#{rid} ERROR {type(e).__name__}: {str(e)[:200]}")
        dt = time.time() - t0
        if dt < PER_PLACE_S:
            time.sleep(PER_PLACE_S - dt)
    log(f"END {stats} in {round((time.time() - t_start) / 60, 1)} min")


if __name__ == "__main__":
    main()
