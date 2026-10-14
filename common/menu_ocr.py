#!/usr/bin/env python3
"""Menu photo -> dishes (local OCR, no external API). Used by menu_worker.py.
EasyOCR (Vietnamese) -> row grouping (dish text left of a price on the same line) -> syllable fix-up
against a food vocabulary -> kind (food/drink/side/other) + category -> rows for PUT /menu.
Also: VN phone extraction from OCR text."""
import json, os, re, unicodedata
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
_R = {}


def reader():
    if "r" not in _R:
        import easyocr
        _R["r"] = easyocr.Reader(["vi"], gpu=False, verbose=False)
    return _R["r"]


def ocr(path_or_bytes):
    out = reader().readtext(path_or_bytes, canvas_size=1600)
    toks = []
    for box, s, c in out:
        xs = [p[0] for p in box]; ys = [p[1] for p in box]
        toks.append({"t": s.strip(), "c": float(c), "x0": float(min(xs)), "x1": float(max(xs)), "y0": float(min(ys)), "y1": float(max(ys))})
    return toks


# ---------------- vocabulary fix-up ----------------
def strip_acc(s):
    s = s.replace("đ", "d").replace("Đ", "D")
    return "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn")


def _load_vocab():
    if "v" in _R:
        return _R["v"]
    cnt = Counter()
    p = os.path.join(HERE, "menu_vocab.json")
    if os.path.exists(p):
        cnt.update(json.load(open(p, encoding="utf-8")))
    by_base = {}
    for w, n in cnt.items():
        b = strip_acc(w)
        if n > by_base.get(b, ("", 0))[1]:
            by_base[b] = (w, n)
    _R["v"] = (cnt, by_base)
    return _R["v"]


def _case_like(src, w):
    if src.isupper() and len(src) > 1: return w.upper()
    if src[:1].isupper(): return w[:1].upper() + w[1:]
    return w


def fix_words(text):
    cnt, by_base = _load_vocab()
    out = []
    for w in text.split():
        lw = w.lower()
        if not re.fullmatch(r"[^\W\d_]+", lw):
            out.append(w); continue
        cand = by_base.get(strip_acc(lw))
        if cand and cand[0] != lw and cand[1] >= 3 and cand[1] >= 10 * max(1, cnt.get(lw, 0)) and (cnt.get(lw, 0) < 2 or lw == strip_acc(lw)):
            out.append(_case_like(w, cand[0]))
        else:
            out.append(w)
    return " ".join(out)


def known_ratio(text):
    cnt, _ = _load_vocab()
    ws = [w.lower() for w in text.split() if re.search(r"[^\W\d_]", w)]
    return sum(1 for w in ws if cnt.get(w, 0) >= 2) / max(1, len(ws))


# ---------------- price ----------------
def _num_fix(s):
    s = s.strip().replace(" ", "")
    s = re.sub(r"(?<=\d)[oOQ]|[oOQ](?=\d)", "0", s)
    s = re.sub(r"(?<=\d)[oO](?=[kK])", "0", s)
    s = re.sub(r"^[oO](?=\d|[oO]*[kK])", "0", s)
    s = re.sub(r"(?<=\d)[lI|](?=\d)", "1", s)
    s = re.sub(r"(?<=[kK])l(?=[a-z])", "/", s)            # 10klcon -> 10k/con
    return s


PRICE_RE = re.compile(r"^(\d{1,3}(?:[.,]\d{3})*|\d{1,4})(k|K|đ|d|vnđ|vnd|đồng|000|\.000đ)?(?:/([a-zà-ỹ]{1,6}))?$")


def parse_price(s):
    """-> (value_vnd, unit, label) or None. Handles 25k, 7ok, 1ook, 35.000, 35.000đ, 10k/con, 120k-150k."""
    s0 = _num_fix(s)
    parts = re.split(r"[-~–]", s0)
    vals = []; unit = None
    for p in parts:
        p = p.strip().rstrip(".,")
        m = PRICE_RE.match(p)
        if not m:
            return None
        n = int(re.sub(r"[.,]", "", m.group(1)))
        suf = (m.group(2) or "").lower()
        if suf in ("k",) or (not suf and n < 1000):
            n *= 1000
        elif suf == "000":
            n *= 1000
        unit = unit or m.group(3)
        if not (1000 <= n <= 5_000_000):
            return None
        vals.append(n)
    if not vals:
        return None
    label = re.sub(r"(?<=\d)K", "k", s0) if (len(vals) > 1 or unit) else ""
    return min(vals), unit, label


def is_price_tok(t):
    s = t["t"].strip()
    if not re.search(r"\d", _num_fix(s)): return False
    pp = parse_price(s)
    if not pp: return False
    bare = re.fullmatch(r"\d{1,3}", _num_fix(s))
    return pp, bool(bare)


# ---------------- kind / category ----------------
DRINK_BRANDS = r"(coca|cocacola|pepsi|7 ?up|sting|red ?bull|bò húc|tiger|heineken|sài gòn|saigon|333|larue|huda|hà nội beer|halida|budweiser|strongbow|somersby|sapporo|carlsberg|bivina|aquafina|lavie|revive|c2|number ?1|warrior|monster|mirinda|fanta|sprite|schweppes|lipton|twister|o ?long|oolong|nutri ?boost|yakult|vinamilk|th true|trà xanh 0|ô long)"
DRINK_RE = re.compile(r"\b(trà|chè (xanh|thái)|cà ?phê|cafe|café|coffee|capuchino|cappuccino|latte|espresso|americano|bạc xỉu|cacao|ca cao|sinh tố|ép|nước|bia|beer|rượu|vang|sữa(?! chua)|soda|sođa|mojito|smoothie|juice|milk ?tea|matcha|yaourt đá|sữa chua (đá|lắc|uống)|đá me|chanh (dây|muối|leo)|cocktail|whisky|vodka|kombucha|trân châu đường đen|lục trà|hồng trà|hồng xiêm|frappe|freeze|đá xay|nước suối|nước ngọt|nước dừa|dừa tươi|cam vắt|rau má|sâm|mía|tắc|xí muội|nước sấu|nước mơ|sữa đậu)\b|" + DRINK_BRANDS, re.I)
NOT_DRINK = re.compile(r"\bnước (chấm|mắm|dùng|lèo|sốt|tương|màu|cốt)\b|\b(lẩu|cơm|bún|phở|mì|mỳ|miến|cháo|hủ tiếu|hủ tíu|bánh|gà|bò|heo|lợn|cá|tôm|mực|vịt|dê|ếch|sườn|ốc|nem|chả|xôi|thịt|lòng|ngan|cua|ghẹ|lươn|chim|trứng|đậu|rau|canh|gỏi|nộm|kem|chè|sữa chua|xúc xích|khoai|nấm|hàu|sò|nghêu|ngao|hến|bạch tuộc|bồ câu|ba ba|cháo)\b", re.I)
STRONG_DRINK = re.compile(r"\b(cà fê|cafê|hồng trà|lục trà|ô long|oolong|trà đào|trà vải|trà chanh|trà tắc|trà xanh|latte|trà sữa|cà ?phê|cafe|café|coffee|sinh tố|nước ép|soda|matcha|cacao|ca cao|bạc xỉu|smoothie|frappe|đá xay|cappuccino|espresso|americano|milk ?tea)\b|" + DRINK_BRANDS, re.I)
OTHER_RE = re.compile(r"^(khăn( lạnh| ướt)?|đá( viên| lạnh)?|hộp( xốp)?|túi|ly nhựa|bao bì|phí .*|thuốc lá.*|hộp mang về|đồ mang về)$", re.I)
SIDE_RE = re.compile(r"(\bthêm\b|topping|^trân châu|^thạch\b|^pudding|^quẩy|^bánh phở thêm|^cơm trắng$|^cơm thêm|^rau thêm|^trứng (thêm|chần|ốp)?$|^bánh mì thêm|^mì tôm thêm|^phô mai thêm|^kem cheese|^kem muối)", re.I)


def kind_of(name, header=""):
    n = name.lower().strip()
    if OTHER_RE.match(n): return "other"
    if SIDE_RE.search(n): return "side"
    drink = bool(DRINK_RE.search(n))
    if re.match(r"^sữa chua(?! (uống|lắc|đá))", n) and not STRONG_DRINK.search(n): return "food"
    if STRONG_DRINK.search(n): return "drink"
    if drink and NOT_DRINK.search(n) and not re.match(r"^(nước|trà|cà phê|sinh tố|sữa|bia|rượu)\b", n):
        drink = False
    if re.match(r"^nước (chấm|mắm|dùng|lèo|sốt)", n): drink = False
    if False and not drink and header:
        drink = bool(re.match(r"^[^\d]{2,40}$", n)) and len(n.split()) <= 6 and not re.search(r"\b(chiên|xào|nướng|luộc|hấp|kho|rang)\b", n)
    return "drink" if drink else "food"


def category_for(kind, header):
    h = (header or "").strip()
    hdrink = bool(h and DRINK_RE.search(h.lower()) and not NOT_DRINK.search(h.lower()))
    if kind == "drink":
        if not h or re.match(r"^(đồ uống|thức uống|nước uống|giải khát|drinks?|beverages?)$", h, re.I): return "Đồ uống"
        if h.lower().startswith("đồ uống"): return h
        return "Đồ uống - " + h if hdrink else "Đồ uống"
    if kind == "side": return "Món thêm"
    if kind == "other": return "Khác"
    if h and not hdrink and not re.match(r"^(menu|thực đơn|bảng giá)$", h, re.I): return h
    return "Món ăn"


# ---------------- layout ----------------
def _yc(t): return (t["y0"] + t["y1"]) / 2
def _h(t): return t["y1"] - t["y0"]


def clean_name(s):
    s = " ".join(w.replace("l", "I") if (w.upper() == w.replace("l", "L") and re.search(r"[A-ZÀ-Ỹ]", w)) else w for w in s.split())
    s = re.sub(r"^\s*(\d{1,2}|[a-zA-Z])\s*[.)/:-]\s*", "", s)
    s = re.sub(r"[.…_·\-:]{2,}.*$", "", s)
    s = re.sub(r"\s*[:\-–.]+\s*$", "", s)
    s = re.sub(r"\b\d+([.,]\d+)?\s*[kK]\b|\b\d{1,3}[.,]\d{3}\b(/\d{1,3}[.,]\d{3})?|\b[iI]\d+k\b", " ", s)
    s = re.sub(r"\((s/m|m/l|s|m|l|ly/set)\)", " ", s, flags=re.I)
    s = re.sub(r"^[#*•\-+~^]+\s*", "", s.strip())
    ws = s.split()
    while ws and (re.fullmatch(r"[\W\d_]{1,4}|[a-zA-Z]|[\d.,]+|/\w{0,2}", ws[-1])):
        ws.pop()
    while ws and re.fullmatch(r"[\W\d_]{1,4}|[\d.,]+", ws[0]):
        ws.pop(0)
    s = " ".join(ws)
    s = re.sub(r"\s+", " ", s).strip(" ,;:-/")
    return s


def parse_menu(toks, img_w=None):
    """-> dict(rows=[...], headers=[...], stats)"""
    toks = [t for t in toks if t["t"]]
    if not toks:
        return {"rows": [], "why": "no text"}
    W = img_w or max(t["x1"] for t in toks)
    prices, texts = [], []
    for t in toks:
        r = is_price_tok(t)
        if r:
            t["pp"], t["bare"] = r; prices.append(t)
        else:
            texts.append(t)
    # bare numbers ("35") count as prices only when the menu mostly has bare numbers in a column
    if prices:
        nb = sum(1 for p in prices if p["bare"])
        if nb < 3 and nb < len(prices) / 2:
            texts += [p for p in prices if p["bare"]]; prices = [p for p in prices if not p["bare"]]
    hs = sorted(_h(t) for t in texts) or [10]
    med_h = hs[len(hs) // 2]
    ph = sorted(_h(p) for p in prices) or [med_h]
    big = max(ph[len(ph) // 2], med_h) * 1.6
    assign = {}
    for i, t in enumerate(texts):
        if _h(t) > big: continue          # titles / section headers never belong to a dish row
        best = None
        for j, p in enumerate(prices):
            if abs(_yc(t) - _yc(p)) > 0.55 * max(_h(t), _h(p)): continue
            if t["x0"] >= p["x0"]: continue
            if p["x0"] - t["x1"] > 0.6 * W: continue
            d = p["x0"] - t["x1"]
            if best is None or d < best[0]: best = (d, j)
        if best: assign.setdefault(best[1], []).append(t)
    # headers: unassigned big tokens. Every one is a section boundary; only clean ones give a category name.
    headers = []
    for t in texts:
        if any(t in v for v in assign.values()): continue
        if _h(t) < 1.25 * med_h: continue
        s = fix_words(clean_name(t["t"]))
        s0 = clean_name(t["t"])
        ok = (t["c"] >= 0.8 and s == s0 and re.fullmatch(r"[^\W\d_][^\d]{1,30}", s) is not None
              and len(s.split()) <= 5 and known_ratio(s) >= 0.99)
        if ok and s.isupper(): s = s[:1] + s[1:].lower()
        headers.append({"t": s if ok else "", "x0": t["x0"], "x1": t["x1"], "y": _yc(t)})
    rows, seen, dropped = [], set(), []
    keeps = {}
    for j, p in enumerate(prices):
        parts = sorted(assign.get(j, []), key=lambda t: t["x0"])
        keep = []
        for t in reversed(parts):
            if keep and keep[0]["x0"] - t["x1"] > 2.2 * max(med_h, _h(t)): break
            if not keep and p["x0"] - t["x1"] > 0.45 * W: break
            keep.insert(0, t)
        if keep: keeps[j] = keep
    used = {id(x) for v in keeps.values() for x in v}
    for j, p in enumerate(prices):
        keep = keeps.get(j)
        if not keep: continue
        if keep[0]["t"][:1].islower():
            above = [t for t in texts if id(t) not in used and _h(t) < 1.25 * med_h
                     and abs(t["x0"] - keep[0]["x0"]) < med_h and 0 < keep[0]["y0"] - t["y1"] < 1.2 * med_h]
            if len(above) == 1: keep.insert(0, above[0])
        raw = " ".join(t["t"] for t in keep)
        conf = sum(t["c"] for t in keep) / len(keep)
        name = fix_words(clean_name(raw))
        UNIT = r"(đĩa|dĩa|chén|tô|thố|phần|suất|ly|lon|chai|con|kg|lạng|cái|cây|bát|nồi|xiên|vỉ)"
        um = re.search(r"(\s+" + UNIT + r")+$", name, re.I)
        unit_txt = um.group(0).strip().split()[-1].lower() if um else ""
        if um: name = name[:um.start()].strip()
        if re.fullmatch(UNIT, name, re.I) or not name:
            dropped.append(f"{raw}|unit only"); continue
        letters = re.sub(r"[^\w]|\d|_", "", name)
        if len(letters) < 2 or len(name) > 70 or conf < 0.35:
            dropped.append(f"{raw}|{p['t']}|c={conf:.2f}"); continue
        kr = known_ratio(name)
        ws_ = name.lower().split()
        if len(ws_) >= 4 and " ".join(ws_[:2]) in " ".join(ws_[2:]):
            dropped.append(f"{raw}|merged two dishes"); continue
        if conf < 0.45 or (kr < (0.75 if conf < 0.7 else 0.5) and not re.search(DRINK_BRANDS + r"|7 ?up", name, re.I)):
            dropped.append(f"{raw}->{name}|{p['t']}|unknown words"); continue
        val, unit, label = p["pp"]
        # header: nearest above, same column
        x0 = keep[0]["x0"]; hdr = ""
        cand = [h for h in headers if h["y"] < _yc(p) and x0 - 0.05 * W <= (h["x0"] + h["x1"]) / 2 <= p["x1"]]
        if cand: hdr = max(cand, key=lambda h: h["y"])["t"]
        nm = name[:1].upper() + name[1:]
        ups = sum(1 for ch in nm if ch.isupper()); los = sum(1 for ch in nm if ch.islower())
        if ups > los: nm = nm[:1] + nm[1:].lower()
        if conf < 0.55:
            dropped.append(f"{raw}|low conf {conf:.2f}"); continue
        names = refine(nm)
        if not names:
            dropped.append(f"{raw}|junk"); continue
        nm = names[-1]                        # the part next to the price owns it; earlier parts lost their price
        desc = label if label else (f"/{unit_txt}" if unit_txt else "")
        for extra in names[:-1]:
            ke = kind_of(extra, hdr)
            if (extra.lower(), None) not in seen:
                seen.add((extra.lower(), None))
                rows.append({"name": extra, "price_vnd": None, "description": "", "category": category_for(ke, hdr),
                             "kind": ke, "conf": round(conf, 2), "header": hdr})
        k = kind_of(nm, hdr)
        if not price_ok(k, val, desc):
            dropped.append(f"{raw}|implausible price {val} for {k}"); continue
        key = (nm.lower(), val)
        if key in seen: continue
        seen.add(key)
        rows.append({"name": nm, "price_vnd": val, "description": desc, "category": category_for(k, hdr),
                     "kind": k, "conf": round(conf, 2), "header": hdr})
    return {"rows": rows, "headers": [h["t"] for h in headers], "dropped": dropped,
            "n_prices": len(prices), "n_tokens": len(toks)}


# ---------------- phone ----------------
PHONE_RE = re.compile(r"(?<!\d)(0[35789]\d{8})(?!\d)")


def phones(toks):
    found = {}
    for t in toks:
        s = re.sub(r"(?<=\d)[oO]|[oO](?=\d)", "0", t["t"])
        s = re.sub(r"[\s.\-·,]", "", s)
        s = re.sub(r"^\+?84", "0", s)
        for m in PHONE_RE.findall(s):
            found[m] = max(found.get(m, 0), t["c"])
    return found


def fmt_phone(p):
    return f"+84 {p[1:4]} {p[4:7]} {p[7:]}"


FOOD_STRONG = re.compile(r"\b(kho|canh|rang|rau|gỏi|súp|nấu|hấp|luộc|om|quay|rim|kiến|tộ|nướng|cơm|bún|phở|mì|mỳ|miến|cháo|bánh|gà|bò|heo|lợn|cá|tôm|mực|chiên|xào|nướng|lẩu|xôi|khoai|hủ tiếu|hủ tíu|nem|chả|kem|chè|sữa chua|ốc|vịt|xúc xích|trứng|thịt|sườn|gỏi|nộm|bắp|ngô|hạt|snack|bim|que|pizza|burger|salad|súp|soup|toast|sandwich|đậu)\b", re.I)


def contextualize(rows, place_cat=""):
    """Second pass with the whole menu: drink shops -> plain fruit names (Cam, Bơ, Thơm, Yaourt...) are drinks;
    rows under a Topping/Thêm header are sides."""
    if not rows: return rows
    nd = sum(1 for r in rows if r["kind"] == "drink")
    drinkshop = place_cat == "cafe" or nd >= 0.5 * len(rows)
    for r in rows:
        h = (r.get("header") or "").lower()
        if r["kind"] == "food" and re.search(r"topping|thêm", h):
            r["kind"] = "side"; r["category"] = category_for("side", "")
        elif r["kind"] == "food" and drinkshop and not FOOD_STRONG.search(r["name"]) and len(r["name"].split()) <= 5:
            r["kind"] = "drink"; r["category"] = category_for("drink", r.get("header", ""))
        elif r["kind"] == "food" and drinkshop and re.match(r"^(yaourt|sữa chua)", r["name"], re.I) and not re.search(r"(hủ|hũ|nếp cẩm|mít|trái cây)", r["name"], re.I):
            r["kind"] = "drink"; r["category"] = category_for("drink", r.get("header", ""))
    return rows


def quality(rows):
    """place-level gate: (ok, avg_conf, share_good)"""
    if not rows: return False, 0, 0
    avg = sum(r["conf"] for r in rows) / len(rows)
    good = sum(1 for r in rows if r["conf"] >= 0.6 and known_ratio(r["name"]) >= 0.8) / len(rows)
    return (avg >= 0.65 and good >= 0.6), round(avg, 2), round(good, 2)


# ---------------- refinement (2026-10-13 11:00) ----------------
PHRASE_FIX = [
    (r"_", " "),
    (r"\bsinh tố bò\b", "sinh tố bơ"), (r"\bép dừa hấu\b", "ép dưa hấu"), (r"\bdừa hấu\b", "dưa hấu"),
    (r"\bsđa chua\b", "sữa chua"), (r"\bsửa chua\b", "sữa chua"), (r"\bchanh lác\b", "chanh lắc"),
    (r"\btrà vai\b", "trà vải"), (r"\bcà cao\b", "ca cao"), (r"\bviệt quốc\b", "việt quất"),
    (r"\btrân chêu\b", "trân châu"), (r"\bdương đen\b", "đường đen"), (r"\bcơm chiên đường châu\b", "cơm chiên dương châu"),
    (r"\bdâu khuôn\b", "đậu khuôn"), (r"\bthập cam\b", "thập cẩm"), (r"\bgà popcorn ran\b", "gà popcorn rán"),
    (r"\bsò ?véo\b", "sò điệp"), (r"\bcdm\b", "cơm"), (r"\bphỏ\b", "phở"), (r"_", " "), (r"\s-(?=\w)", " "), (r"\btrợ sữa\b", "trà sữa"), (r"\bsapo\b", "sapoche"),
]
STARTS = r"(trà sữa|trà|sinh tố|sữa chua|cà phê|cafe|ca cao|cacao|nước ép|ép|soda|bánh|cơm|bún|phở|mì|mỳ|miến|lẩu|gà|bò|cá|tôm|mực|lipton|matcha|yaourt)"


def _keep_case(src, new):
    return new[:1].upper() + new[1:] if src[:1].isupper() else new


def refine(name):
    name = re.sub(r"(?<=[a-zà-ỹ])[lI](?=\s?[A-ZĐÀ-Ỹ])", "/", name)       # 'NóngIĐá' -> 'Nóng/Đá'
    name = re.sub(r"^CF\b", "Cà phê", name)
    """-> list of names (usually 1). [] = junk. Splits 'A: B' and two-column merges 'Trà X Trà Y' -> [A, B]."""
    n = re.sub(r"^\s*(\d+\s*[xX]|[xX]\s*\d+|\d+[.)]?)\s+", "", name.strip())     # '15x Cà cao' -> 'Cà cao'
    n = re.sub(r"^(đặc biệt|món|các món)\s*:\s*", "", n, flags=re.I)
    low = n.lower()
    for a, b in PHRASE_FIX:
        low2 = re.sub(a, b, low)
        if low2 != low:
            n = _keep_case(n, low2) if n[:1].isupper() else low2
            low = low2
    parts = [x.strip(" -,.;") for x in re.split(r"\s*[:;|]\s*", n) if x.strip(" -,.;")]
    out = []
    for p_ in parts:
        # two dishes from adjacent columns: a dish-start word appears again later
        m = list(re.finditer(r"(?<=\s)" + STARTS + r"\b", p_, re.I))
        cut = [x.start() for x in m if re.match(STARTS + r"\b", p_, re.I) and x.group(1).lower().split()[0] == p_.lower().split()[0]]
        ws = p_.split()
        if not cut and len(ws) >= 5:
            for L in (3, 2):
                for i in range(1, len(ws) - L):
                    seg = " ".join(ws[i:i + L]).lower()
                    rest = " ".join(ws[i + L:]).lower()
                    if seg in rest and len(seg) >= 5:
                        j = i + L
                        cut = [len(" ".join(ws[:j])) + 1]; break
                if cut: break
        if cut and len(p_.split()) >= 3:
            l_, r_ = p_[:cut[0]].strip(), p_[cut[0]:].strip()
            if l_.lower().endswith(r_.lower()) or len(r_.split()) < 2: out.append(l_)     # OCR duplicate tail
            else: out += [l_, r_]
        else:
            out.append(p_)
    cnt, _ = _load_vocab()
    fixed = []
    for x in out:
        ws = x.split()
        while len(ws) > 1 and len(ws[-1]) <= 2 and cnt.get(ws[-1].lower(), 0) < 2 and not re.fullmatch(r"\d+|[A-Z0-9]{1,2}", ws[-1]):
            ws.pop()                                         # trailing OCR crumbs: 'Phở miến trộn fa'
        x = " ".join(ws)
        x = re.sub(r"^ts\b", "Trà sữa", x, flags=re.I)
        x = re.sub(r"(?<=[^\W\d_])\d+$", "", x).strip()
        x = re.sub(r"\bdương (đen|den)\b", "đường đen", x, flags=re.I)
        fixed.append(x)
    out = [x[:1].upper() + x[1:] for x in fixed if x]
    if len(out) > 1 and any(len(x.split()) < 2 or known_ratio(x) < 0.5 for x in out):
        return []                                            # split left a fragment like 'Nắng' -> whole row is junk
    if len(out) == 1 and len(re.sub(r"[\W\d_]", "", out[0])) < 2:
        return []
    return out


MIN_PRICE = {"food": 8000, "drink": 5000, "side": 2000, "other": 1000}


def price_ok(kind, price, desc=""):
    """per-piece prices ('3k/cây') and ranges are fine; otherwise a floor per kind; 4-digit mash-ups like 2530 are rejected"""
    if price is None or price == "": return True
    p = int(price)
    if desc and re.search(r"/|-", desc): return p >= 1000
    return p >= MIN_PRICE.get(kind, 8000)
