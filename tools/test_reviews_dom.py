"""Offline tests for Google reviews read by scrolling the Reviews panel (common/reviews_dom.py, an_uong_quanh_day#1108).
Fixtures in tools/fixtures/reviews_dom are sanitized snapshots of a real panel (ids RV_TEST_xx, synthetic authors /
text / photo ids, no cookies or tokens); card_truncated_*.html is hand-made from the same markup.
No network, no browser: a fake driver serves the snapshots.

Run: python tools/test_reviews_dom.py [repo-root]
"""
import json
import os
import re
import sys

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "common"))
import reviews_dom as rd  # noqa: E402

FX = os.path.join(ROOT, "tools", "fixtures", "reviews_dom")
FAILURES = []


def check(label, got, want):
    print(f"  {'ok  ' if got == want else 'FAIL'} {label}: {got!r}" + ("" if got == want else f", want {want!r}"))
    if got != want:
        FAILURES.append(label)


def fx(name):
    with open(os.path.join(FX, name), encoding="utf-8") as f:
        return f.read()


C10, C30 = fx("cards_initial_collapsed.html"), fx("cards_after_2_scrolls_expanded.html")
T_SHORT, T_FULL = fx("card_truncated_collapsed.html"), fx("card_truncated_expanded.html")
PREVIEW = json.loads(fx("preview_rows.json"))
CARDS = re.findall(r'<div class="jftiEf[^"]*"[^>]*data-review-id=.*?(?=<div class="jftiEf|\Z)', C30, re.S)


def html_of(n):
    """the first n cards of the 30-card snapshot (Google loads 10 per scroll); beyond 30, clones with new ids"""
    extra = [CARDS[k % 30].replace(f"RV_TEST_{k % 30 + 1:02d}", f"RV_GEN_{k:03d}") for k in range(30, n)]
    return "".join(CARDS[:n] + extra)


class FakeDriver:
    """Serves snapshots like the real panel: `sizes[k]` cards after k scrolls."""

    def __init__(self, sizes=(10, 20, 30), tab=True, sort=True, truncated=False, fail_at=None):
        self.sizes, self.tab, self.sort, self.truncated, self.fail_at = list(sizes), tab, sort, truncated, fail_at
        self.scrolls = self.expanded = 0
        self.pauses, self.reads, self.calls = [], 0, []

    def open_reviews(self):
        self.calls.append("open")
        if self.fail_at == "open":
            raise TimeoutError("goto")
        return self.tab

    def sort_newest(self):
        self.calls.append("sort")
        return self.sort      # True, False, or a reason string like the real PageDriver

    def pause(self, s):
        self.pauses.append(s)

    def cards_html(self):
        self.reads += 1
        if self.fail_at == "read" and self.scrolls == 1:
            raise RuntimeError("detached")
        n = self.sizes[min(self.scrolls, len(self.sizes) - 1)]
        extra = (T_FULL if self.expanded else T_SHORT) if self.truncated else ""
        return html_of(n) + extra

    def scroll(self):
        self.calls.append("scroll")
        self.scrolls += 1
        return True

    def expand_more(self):
        self.calls.append("expand")
        self.expanded += 1
        return 1


def test_parse():
    print("=== parse the real (sanitized) cards")
    rows = rd.parse_cards(C10)
    check("10 cards in the first load", len(rows), 10)
    rows = rd.parse_cards(C30)
    check("30 cards after 2 scrolls", len(rows), 30)
    check("ids in page order", [r["id"] for r in rows[:3]], ["RV_TEST_01", "RV_TEST_02", "RV_TEST_03"])
    check("ids unique", len({r["id"] for r in rows}), 30)
    r = rows[0]
    check("author", r["author"], "Người Dùng 01")
    check("rating from 'N sao'", r["rating"], 1)
    check("every card has a rating 1-5", all(x["rating"] in (1, 2, 3, 4, 5) for x in rows), True)
    check("relative date", r["relative_date"], "5 tháng trước")
    check("language from the text block", r["language"], "vi")
    check("no exact date from the DOM", r["published_at"], None)
    check("text is the review, not the owner reply", r["text"].startswith("Nội dung mẫu RV_TEST_01"), True)
    check("owner replies never leak into text", any("Phản hồi mẫu" in x["text"] for x in rows), False)
    check("photo url without size suffix", r["photo_urls"], ["https://lh3.googleusercontent.com/grass-cs/PHOTO_TEST_017"])
    check("cards with photos", sum(bool(x["photo_urls"]) for x in rows), 25)
    check("edited date kept as shown", [x["relative_date"] for x in rows if "chỉnh sửa" in (x["relative_date"] or "")][:1],
          ["Thời gian chỉnh sửa: một năm trước"])
    check("collapsed snapshot: 'Xem thêm' buttons flagged", sum(x["truncated"] for x in rd.parse_cards(C10)), 6)
    check("expanded snapshot: nothing left to expand", sum(x["truncated"] for x in rows), 0)
    same = {x["id"]: x["text"] for x in rd.parse_cards(C10)}
    check("same text collapsed vs expanded for these cards", all(same[x["id"]] == x["text"] for x in rows[:10]), True)
    check("custom photo base fn", rd.parse_cards(C10, base=lambda u: "B")[0]["photo_urls"], ["B"])
    check("empty / garbage html", (rd.parse_cards(""), rd.parse_cards("<div><span>")), ([], []))
    check("'stars' (English UI) also parsed", rd.parse_cards(C10.replace(" sao\"", " stars\""))[0]["rating"], 1)


def test_expand():
    print("=== 'Thêm' expansion of a truncated review")
    s, f = rd.parse_cards(T_SHORT)[0], rd.parse_cards(T_FULL)[0]
    check("collapsed card is flagged truncated", s["truncated"], True)
    check("collapsed text", s["text"], "Dòng một của bài dài.\nDòng hai bị cắt …")
    check("expanded card not truncated", f["truncated"], False)
    check("expanded text keeps line breaks", f["text"], "Dòng một của bài dài.\nDòng hai bị cắt ở đây nhưng còn tiếp.\nDòng ba hết bài.")
    check("owner reply excluded", "Phản hồi" in f["text"], False)
    check("edited date", f["relative_date"], "Thời gian chỉnh sửa: một năm trước")
    d = FakeDriver(sizes=(10, 20, 30), truncated=True)
    rows, note = rd.page_reviews(d, [], cap=50, want=31, wait=(0, 0), rand=lambda a, b: a)
    check("expand clicked once after scrolling", d.calls.count("expand"), 1)
    check("expanded text read back", next(r["text"] for r in rows if r["id"] == "RV_TEST_99").endswith("Dòng ba hết bài."), True)
    d = FakeDriver(sizes=(10, 20, 30))
    rd.page_reviews(d, [], cap=50, want=30, wait=(0, 0), rand=lambda a, b: a)
    check("no click when nothing is truncated", d.calls.count("expand"), 0)


def test_merge():
    print("=== de-dupe with the preview")
    dom = rd.parse_cards(C30)
    out = rd.merge(PREVIEW, dom, cap=50)
    check("8 preview + 30 DOM -> 30 unique (all preview ids are on the page)", len(out), 30)
    check("unique ids", len({r["id"] for r in out}), 30)
    pre = {p["id"]: p for p in PREVIEW}
    check("preview's exact published_at kept on matching ids",
          all(r["published_at"] == pre[r["id"]]["published_at"] for r in out if r["id"] in pre), True)
    check("DOM-only rows: relative date only", all(r["published_at"] is None for r in out if r["id"] not in pre), True)
    m = next(r for r in out if r["id"] == "RV_TEST_02")
    check("longer DOM text wins over the preview's", m["text"].startswith("Nội dung mẫu RV_TEST_02"), True)
    check("author stays the preview's", m["author"], "Preview 1")
    check("sorted newest: page order", [r["id"] for r in out[:2]], ["RV_TEST_01", "RV_TEST_02"])
    check("no internal 'truncated' key in output", any("truncated" in r for r in out), False)
    check("cap applied", len(rd.merge(PREVIEW, dom, cap=12)), 12)
    extra = [{"id": "PREVIEW_ONLY", "text": "x", "photo_urls": ["p1"], "published_at": "2026-10-01T00:00:00+07:00"}]
    out = rd.merge(extra + PREVIEW, dom, cap=50)
    check("preview-only rows appended, not lost", (len(out), out[-1]["id"]), (31, "PREVIEW_ONLY"))
    out = rd.merge(PREVIEW, dom, cap=50, sorted_newest=False)
    check("unsorted panel: preview order first", [r["id"] for r in out[:8]], [p["id"] for p in PREVIEW])
    p = dict(PREVIEW[0], photo_urls=["https://lh3.googleusercontent.com/grass-cs/OTHER"])
    m = rd.merge([p], dom, cap=50)
    m = next(r for r in m if r["id"] == p["id"])
    check("photos unioned (preview first)", m["photo_urls"][0], "https://lh3.googleusercontent.com/grass-cs/OTHER")
    check("photos unioned (DOM added)", len(m["photo_urls"]) > 1, True)
    check("duplicate DOM ids collapse", len(rd.merge([], dom + dom, cap=99)), 30)


def test_stop():
    print("=== stop conditions + human-like waits")
    zero = dict(wait=(0, 0), rand=lambda a, b: a)
    d = FakeDriver(sizes=(10, 20, 30, 40))
    rows, note = rd.page_reviews(d, PREVIEW, cap=20, want=500, **zero)
    check("stops at the cap (AQD_MAX_REVIEWS)", (len(rows), d.scrolls), (20, 1))
    check("note", note, "dom+12/1s")
    d = FakeDriver(sizes=(10, 20, 30, 40))
    rows, note = rd.page_reviews(d, PREVIEW, cap=50, want=15, **zero)
    check("stops scrolling once Google's review count is reached", (d.scrolls, len(rows) >= 15), (1, True))
    d = FakeDriver(sizes=(10, 20, 30, 30, 30))
    rows, note = rd.page_reviews(d, PREVIEW, cap=50, want=200, **zero)
    check("stops when a scroll loads nothing new", (len(rows), d.scrolls), (30, 3))
    check("one patient re-read before giving up", d.reads, 1 + 3 + 1)
    d = FakeDriver(sizes=[10 * i for i in range(1, 20)])
    rows, note = rd.page_reviews(d, [], cap=500, want=None, rating=4.5, max_scrolls=8, **zero)
    check("never more than max_scrolls (8)", (d.scrolls, len(rows)), (8, 90))
    check("default max scrolls is 8", rd.MAX_SCROLLS, 8)
    d = FakeDriver()
    rows, note = rd.page_reviews(d, PREVIEW, cap=50, want=8, **zero)
    check("preview already has Google's count -> page untouched", (len(rows), note, d.calls), (8, "dom_skip", []))
    rows, note = rd.page_reviews(FakeDriver(), PREVIEW, cap=5, **zero)
    check("preview above the cap -> trimmed, untouched", (len(rows), note), (5, "dom_skip"))
    d = FakeDriver(tab=False)
    rows, note = rd.page_reviews(d, PREVIEW, cap=50, want=30, **zero)
    check("no Reviews tab -> preview kept", (len(rows), note, d.scrolls), (8, "dom_no_tab", 0))
    rows, note = rd.page_reviews(FakeDriver(fail_at="open"), PREVIEW, cap=50, want=30, **zero)
    check("navigation error -> preview kept", (len(rows), note), (8, "dom_err:TimeoutError"))
    rows, note = rd.page_reviews(FakeDriver(fail_at="read"), PREVIEW, cap=50, want=30, **zero)
    check("error mid-scroll -> preview kept", (len(rows), note), (8, "dom_err:RuntimeError"))
    d = FakeDriver(sort=False)
    rows, note = rd.page_reviews(d, PREVIEW, cap=50, want=30, **zero)
    check("sort menu missing -> still reads, flagged", (len(rows), note), (30, "dom+22/2s,unsorted"))
    d = FakeDriver()
    rd.page_reviews(d, [], cap=50, want=30, **zero)
    check("order: open, sort, then scrolls", d.calls[:3], ["open", "sort", "scroll"])
    seen = []
    d = FakeDriver(sizes=(10, 20, 30))
    rd.page_reviews(d, [], cap=30, rating=4.2, rand=lambda a, b: (seen.append((a, b)), (a + b) / 2)[1])
    check("waits drawn from 1.5-3 s", set(seen), {(1.5, 3.0)})
    check("a wait after every scroll", len(d.pauses) >= 1 + d.scrolls, True)
    import random as _r
    rnd = _r.Random(7)
    d = FakeDriver(sizes=(10, 20, 30))
    rd.page_reviews(d, [], cap=30, rating=4.2, rand=rnd.uniform)
    check("jittered, in range", (all(1.5 <= p <= 3.0 for p in d.pauses), len(set(d.pauses)) == len(d.pauses)), (True, True))


def test_static():
    print("=== no hand-built Google requests")
    src = open(os.path.join(ROOT, "common", "reviews_dom.py"), encoding="utf-8").read()
    code = re.sub(r'"""[\s\S]*?"""', "", src, count=1)          # skip the module docstring that explains why
    for w in ("batchexecute", "listugcposts", "fetch(", "XMLHttpRequest", "import requests", "urllib"):
        check(f"reviews_dom has no {w}", w in code, False)
    check("place url by place id", rd.place_url({"_meta": {"place_id": "ChIJx"}}), "https://www.google.com/maps/place/?q=place_id:ChIJx&hl=vi")
    check("place url fallback to the stored link",
          rd.place_url({"google_maps_url": "https://www.google.com/maps/search/?api=1&query=a"}),
          "https://www.google.com/maps/search/?api=1&query=a&hl=vi")
    check("no url -> empty", rd.place_url({}), "")
    for name in ("tablist.html", "sort_button.html", "sort_menu.html"):
        check(f"fixture {name} present", os.path.getsize(os.path.join(FX, name)) > 0, True)
    check("tab label matches the driver's selector", "Bài đánh giá" in fx("tablist.html"), True)
    import unicodedata
    menu = fx("sort_menu.html")
    check("live menu sends 'Mới nhất' NOT in NFC (why the JS normalizes)", "Mới nhất" in menu, False)
    check("menu has 'Mới nhất' after NFC", "Mới nhất" in unicodedata.normalize("NFC", menu), True)
    check("parser normalizes to NFC", rd.parse_html(menu).text().count("Mới nhất"), 1)
    for js in ("_JS_TAB", "_JS_SORT_BTN", "_JS_PICK", "_JS_APPLIED", "_JS_IS_NEWEST", "_JS_EXPAND"):
        check(f"{js} compares NFC-normalized labels", "normalize('NFC')" in getattr(rd, js), True)
    check("sort pick falls back to data-index=1 ('Mới nhất' in the live menu)",
          'data-index="1"' in menu and "data-index')==='1'" in rd._JS_PICK, True)
    check("sort button label (closed menu, icon glyph after it)", rd.parse_html(fx("sort_button.html")).text().startswith("Phù hợp nhất"), True)
    allfx = "".join(fx(n) for n in os.listdir(FX))
    check("fixtures carry no cookies/tokens", [w for w in ("SAPISID", "__Secure-", "HSID", "SSID=", "NID=", "AIza", "ved=")
                                               if w in allfx], [])


def test_skip0():
    print("=== (b) no reviews -> the panel is not opened")
    zero = dict(wait=(0, 0), rand=lambda a, b: a)
    for label, kw in (("Google count 0", dict(want=0)), ("count '0' as text", dict(want="0")),
                      ("no count, no rating, no preview", dict(want=None, rating=None)),
                      ("count 0 even with a rating", dict(want=0, rating=4.0))):
        d = FakeDriver()
        rows, note = rd.page_reviews(d, [], cap=50, **kw, **zero)
        check(f"{label} -> dom_skip0, page untouched", (rows, note, d.calls), ([], "dom_skip0", []))
    d = FakeDriver()
    rows, note = rd.page_reviews(d, [], cap=50, want=None, rating=4.6, **zero)
    check("no count but a rating -> still reads", (note.startswith("dom+"), d.calls[:2]), (True, ["open", "sort"]))
    d = FakeDriver()
    rows, note = rd.page_reviews(d, PREVIEW[:2], cap=50, want=None, rating=None, **zero)
    check("no count/rating but preview rows -> still reads", d.calls[:1], ["open"])
    for want in (2, 8, "8"):
        d = FakeDriver()
        rows, note = rd.page_reviews(d, PREVIEW[:min(8, int(want))], cap=50, want=want, rating=5, **zero)
        check(f"preview covers Google's count ({want!r}) -> dom_skip", (note, d.calls), ("dom_skip", []))
    d = FakeDriver()
    rows, note = rd.page_reviews(d, PREVIEW, cap=50, want="n/a", rating=4.0, **zero)
    check("unparsable count -> treated as unknown (reads to cap)", d.calls[:1], ["open"])


def test_sort_pick():
    print("=== (a) sort button / newest item found by structure, any language")
    mac = rd.parse_html(fx("sort_button.html") + C10)
    b = rd.pick_sort_button(mac)
    check("Mac panel: sort button", (b is not None and "FNe70d" in b.cls()), True)
    adm = rd.parse_html(fx("sort_area_admin.html") + C10)
    b = rd.pick_sort_button(adm)
    check("Admin panel (info + write-review popups first): picks FNe70d, not the info button", b.cls(), ["FNe70d"])
    en = fx("sort_area_admin.html").replace("Phù hợp nhất", "Most relevant").replace('class="FNe70d"', 'class="Zz9"')
    check("English labels, renamed class -> by label", rd.pick_sort_button(rd.parse_html(en + C10)).cls(), ["Zz9"])
    xx = fx("sort_area_admin.html").replace("Phù hợp nhất", "Najbardziej trafne").replace('class="FNe70d"', 'class="Zz9"')
    check("unknown language + renamed class -> nearest popup button above the cards",
          rd.pick_sort_button(rd.parse_html(xx + C10)).cls(), ["Zz9"])
    inside = '<div class="jftiEf" data-review-id="RV_X"><button aria-haspopup="true" aria-expanded="false" aria-label="Thao tác">.</button></div>'
    check("popup buttons inside / below review cards are ignored", rd.pick_sort_button(rd.parse_html(C10 + inside)), None)
    menu = fx("sort_menu.html")
    i = rd.pick_newest_item(rd.parse_html(menu))
    check("Mac menu (NFD 'Mới nhất'): newest item", (i.attrs.get("data-index"), rd._nfc(i.text())), ("1", "Mới nhất"))
    bare = re.sub(r'\srole="menu"', "", menu)
    check("menu without a role=menu wrapper still works", rd.pick_newest_item(rd.parse_html(bare)).attrs.get("data-index"), "1")
    import unicodedata
    en = unicodedata.normalize("NFC", menu).replace("Phù hợp nhất", "Most relevant").replace("Mới nhất", "Newest")
    en = re.sub(r'data-index="(\d)"', r'data-index="x\1"', en)     # no data-index help: text only
    check("English menu: by text", rd._nfc(rd.pick_newest_item(rd.parse_html(en)).text()), "Newest")
    ru = re.sub(r'data-index="(\d)"', r'data-index="x\1"', unicodedata.normalize("NFC", menu)).replace("Mới nhất", "Сначала новые")
    check("Russian menu: by text", rd._nfc(rd.pick_newest_item(rd.parse_html(ru)).text()), "Сначала новые")
    pos = re.sub(r'data-index="(\d)"', r'data-index="x\1"', unicodedata.normalize("NFC", menu)).replace("Mới nhất", "Najnowsze")
    check("unknown language, no data-index -> 2nd radio item", rd._nfc(rd.pick_newest_item(rd.parse_html(pos)).text()), "Najnowsze")
    share = '<div role="menu"><div role="menuitem">Sao chép</div><div role="menuitem">Chia sẻ</div></div>'
    check("a non-sort menu (< 3 items) is never clicked", rd.pick_newest_item(rd.parse_html(share)), None)
    check("labels: one list shared by Python and JS", all(json.dumps(rd._SORT_RE.pattern) in getattr(rd, j)
          for j in ("_JS_SORT_BTN", "_JS_PICK", "_JS_APPLIED")), True)
    check("every newest label is also a sort label", all(rd._SORT_RE.search(x) for x in rd.NEWEST_LABELS), True)
    zero = dict(wait=(0, 0), rand=lambda a, b: a)
    for why in ("no_btn", "no_menu", "no_item", "not_applied"):
        rows, note = rd.page_reviews(FakeDriver(sort=why), PREVIEW, cap=50, want=30, **zero)
        check(f"sort miss '{why}' -> reason in the note, still reads", note, f"dom+22/2s,unsorted:{why}")
    rows, note = rd.page_reviews(FakeDriver(sort=True), PREVIEW, cap=50, want=30, **zero)
    check("verified sort -> no unsorted flag", note, "dom+22/2s")


def main():
    test_parse(); test_expand(); test_merge(); test_stop(); test_skip0(); test_sort_pick(); test_static()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL OK")


if __name__ == "__main__":
    main()
