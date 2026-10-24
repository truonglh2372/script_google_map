"""Google Maps reviews by scrolling the Reviews panel of the place page (an_uong_quanh_day#1108).

Replaces the dead listugcposts pager (#1083): Google no longer serves /maps/rpc/listugcposts to the page (403 even
signed in); the Reviews tab now pages through a WizUi batchexecute RPC that needs page-minted tokens. We do NOT build
that request by hand. Instead the crawler's own signed-in tab does what a person does:

  open the place -> Reviews tab -> sort "Mới nhất" -> scroll the panel (1.5-3 s jittered waits, <= MAX_SCROLLS)
  -> click "Xem thêm"/"Thêm" -> read the cards -> de-dupe by review id with the preview rows.

Stop at the cap (AQD_MAX_REVIEWS, default 50), at Google's review count, or when a scroll loads nothing new
(after one extra patient wait). Preview rows keep their exact published_at; DOM-only rows carry relative_date only.
Parsing is pure Python over the cards' outerHTML so it is testable with saved fixtures (tools/fixtures/reviews_dom).
"""
from __future__ import annotations

import random
import re
import unicodedata
from html.parser import HTMLParser

MAX_SCROLLS = 8
WAIT = (1.5, 3.0)
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_STARS = re.compile(r"(\d)(?:[.,]\d)?\s*(?:sao|stars?)\b", re.I)
_URL = re.compile(r"url\((?:&quot;|[\"'])?(https://[^\"')&]+)")
_MORE = re.compile(r"Thêm|Xem thêm|More", re.I)


class Node:
    __slots__ = ("tag", "attrs", "kids", "parent")

    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.kids, self.parent = tag, dict(attrs), [], parent

    def cls(self):
        return (self.attrs.get("class") or "").split()

    def iter(self):
        for k in self.kids:
            if isinstance(k, Node):
                yield k
                yield from k.iter()

    def find(self, pred):
        return next((n for n in self.iter() if pred(n)), None)

    def text(self, skip=lambda n: False):
        out = []
        for k in self.kids:
            if isinstance(k, str):
                out.append(k)
            elif k.tag == "br":
                out.append("\n")
            elif not skip(k):
                out.append(k.text(skip))
        return "".join(out)


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None); self.cur = self.root

    def handle_starttag(self, tag, attrs):
        attrs = [(k, unicodedata.normalize("NFC", v) if v else v) for k, v in attrs]
        n = Node(tag, attrs, self.cur); self.cur.kids.append(n)
        if tag not in _VOID:
            self.cur = n

    def handle_startendtag(self, tag, attrs):
        attrs = [(k, unicodedata.normalize("NFC", v) if v else v) for k, v in attrs]
        self.cur.kids.append(Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.kids.append(unicodedata.normalize("NFC", data))   # Google mixes NFC/NFD Vietnamese


def parse_html(html):
    t = _Tree(); t.feed(html or ""); t.close()
    return t.root


def _has(cls):
    return lambda n: cls in n.cls()


def _is_more(n):
    return n.tag == "button" and ("w8nwRe" in n.cls() or _MORE.search(n.attrs.get("aria-label") or "")) \
        and n.attrs.get("aria-expanded") != "true"


def card_row(card, base=lambda u: u.split("=")[0]):
    """One review card (div.jftiEf[data-review-id]) -> row (same keys as maps_fetch.review_row) + 'truncated'."""
    rid = card.attrs.get("data-review-id")
    if not rid:
        return None
    a = card.find(_has("d4r55"))
    stars = card.find(lambda n: "kvMYJc" in n.cls() or (n.attrs.get("role") == "img" and _STARS.search(n.attrs.get("aria-label") or "")))
    m = _STARS.search((stars.attrs.get("aria-label") or "") if stars else "")
    if not m:
        alt = card.find(_has("fzvQIb"))   # "4/5" layout of some markets
        m = re.match(r"\s*(\d)", alt.text()) if alt else None
    date = card.find(_has("rsqaWe"))
    body = card.find(_has("MyEned"))
    span = body.find(_has("wiI7pd")) if body else None
    text = span.text().strip() if span else ""
    photos = []
    for b in card.iter():
        if b.tag == "button" and "Tya61d" in b.cls():
            u = _URL.search(b.attrs.get("style") or "")
            if u and "googleusercontent.com" in u.group(1):
                p = base(u.group(1))
                if p not in photos:
                    photos.append(p)
    return {"id": rid, "author": a.text().strip() if a else None,
            "rating": int(m.group(1)) if m else None, "text": text, "published_at": None,
            "relative_date": date.text().strip() if date else None,
            "language": (body.attrs.get("lang") if body else None), "photo_urls": photos,
            "truncated": bool(body and body.find(_is_more))}


def parse_cards(html, base=lambda u: u.split("=")[0]):
    """outerHTML of the cards (or the whole panel) -> rows in page order, unique by id (first wins)."""
    rows, seen = [], set()
    for n in parse_html(html).iter():
        if n.tag == "div" and "jftiEf" in n.cls() and n.attrs.get("data-review-id"):
            r = card_row(n, base)
            if r and r["id"] not in seen:
                seen.add(r["id"]); rows.append(r)
    return rows


def merge(preview, dom, cap, sorted_newest=True):
    """Preview rows + DOM rows, de-duped by id. Same id: keep the preview's exact published_at, take the longer text,
    union the photos, fill missing fields from the DOM. Order: page order when the panel is sorted newest, else the
    preview first. Then cap."""
    by = {}
    for r in dom:
        by.setdefault(r["id"], r)
    out, used = [], set()
    def merged(p):
        d = by.get(p["id"])
        if not d:
            return dict(p)
        m = dict(p)
        if len(d.get("text") or "") > len(p.get("text") or ""):
            m["text"] = d["text"]
        for k in ("author", "rating", "relative_date", "language"):
            if m.get(k) in (None, "") and d.get(k) not in (None, ""):
                m[k] = d[k]
        m["photo_urls"] = list(dict.fromkeys(list(p.get("photo_urls") or []) + list(d.get("photo_urls") or [])))
        return m
    pre = {p["id"]: p for p in preview if p.get("id")}
    if sorted_newest:
        for d in dom:
            if d["id"] in used:
                continue
            used.add(d["id"])
            out.append(merged(pre[d["id"]]) if d["id"] in pre else {k: v for k, v in d.items() if k != "truncated"})
        out += [dict(p) for i, p in pre.items() if i not in used]
    else:
        out = [merged(p) for p in pre.values()]
        used = set(pre)
        for d in dom:
            if d["id"] not in used:
                used.add(d["id"]); out.append({k: v for k, v in d.items() if k != "truncated"})
    for r in out:
        r.pop("truncated", None)
    return out[:cap]


def page_reviews(drv, have, cap, want=None, max_scrolls=MAX_SCROLLS, wait=WAIT, rand=random.uniform):
    """Drive the Reviews panel through `drv` (PageDriver or a test fake) and return (rows, note).

    note: "dom_skip" (preview already enough), "dom_no_tab", "dom+N/Ss" (N new rows, S scrolls; ",unsorted" when the
    sort menu was not found), "dom_err:Type". Never raises: any problem keeps the preview rows."""
    rows = [r for r in (have or []) if r.get("id")][:cap]
    goal = min(cap, want) if isinstance(want, int) and want > 0 else cap
    if len(rows) >= goal:
        return rows, "dom_skip"
    pause = lambda: drv.pause(rand(*wait))
    scrolls = 0
    try:
        if not drv.open_reviews():
            return rows, "dom_no_tab"
        sorted_newest = bool(drv.sort_newest())
        pause()
        dom = parse_cards(drv.cards_html())
        while len(merge(rows, dom, cap, sorted_newest)) < goal and scrolls < max_scrolls:
            drv.scroll(); scrolls += 1; pause()
            more = parse_cards(drv.cards_html())
            if len(more) <= len(dom):
                pause()                       # one patient re-check, no extra scroll
                more = parse_cards(drv.cards_html())
                if len(more) <= len(dom):
                    break                     # nothing new loads -> stop
            dom = more
        if any(r["truncated"] for r in dom) and drv.expand_more():
            drv.pause(rand(0.8, 1.5))
            dom = parse_cards(drv.cards_html()) or dom
    except Exception as e:
        return rows, f"dom_err:{type(e).__name__}"
    out = merge(rows, dom, cap, sorted_newest)
    added = len({r["id"] for r in out} - {r["id"] for r in rows})
    return out, f"dom+{added}/{scrolls}s" + ("" if sorted_newest else ",unsorted")


# ---- the real page (Playwright sync Page of the crawler's signed-in Chrome) ----
# Labels are compared after .normalize('NFC'): the live sort menu sends "Mới nhất" in decomposed (NFD) form.
_JS_CARDS = "()=>[...document.querySelectorAll('div.jftiEf[data-review-id]')].map(e=>e.outerHTML).join('\\n')"
_JS_FEED = """(()=>{const c=document.querySelector('div.jftiEf[data-review-id]'); let e=c;
  while(e&&!(e.scrollHeight>e.clientHeight+50&&getComputedStyle(e).overflowY!='visible')) e=e.parentElement; return e})()"""
_JS_SCROLL = "()=>{const e=" + _JS_FEED + "; if(!e) return false; e.scrollTop=e.scrollHeight; return true}"
_JS_EXPAND = """()=>{let n=0; for(const b of document.querySelectorAll('div.jftiEf button')){
  if(b.getAttribute('aria-expanded')==='true') continue;
  if(b.classList.contains('w8nwRe')||/^(Xem thêm|Thêm|More)$/i.test((b.getAttribute('aria-label')||b.textContent||'').normalize('NFC').trim())){b.click(); n++}} return n}"""
_JS_TAB = """()=>{const t=[...document.querySelectorAll('button[role=tab]')].find(b=>/^(Bài đánh giá|Reviews)/i.test(
  (b.getAttribute('aria-label')||'').normalize('NFC').trim()) || /^(Bài đánh giá|Reviews)$/i.test((b.textContent||'').normalize('NFC').trim()));
  if(!t) return false; if(t.getAttribute('aria-selected')!=='true') t.click(); return true}"""
_JS_SORT_OPEN = """()=>{const b=[...document.querySelectorAll('button[aria-haspopup=true],button[data-value]')].find(x=>
  /Sắp xếp|Phù hợp nhất|Mới nhất|Sort|Most relevant|Newest/i.test(((x.getAttribute('aria-label')||'')+' '+(x.getAttribute('data-value')||'')+' '+x.textContent).normalize('NFC')));
  if(!b) return false; if(/Mới nhất|Newest/i.test(b.textContent.normalize('NFC'))) return 'already'; b.click(); return true}"""
_JS_SORT_PICK = """()=>{const items=[...document.querySelectorAll('[role=menu] [role=menuitemradio],[role=menu] [role=menuitem]')];
  const i=items.find(x=>/^(Mới nhất|Newest)$/i.test(x.textContent.normalize('NFC').trim())) || items.find(x=>x.getAttribute('data-index')==='1');
  if(!i) return false; i.click(); return true}"""


def place_url(info, hexid=None):
    """Same place URL as the gallery walk (photos.py); falls back to the stored Maps link, then the hex id."""
    info = info or {}
    pid = info.get("place_id") or (info.get("_meta") or {}).get("place_id")
    if pid:
        return f"https://www.google.com/maps/place/?q=place_id:{pid}&hl=vi"
    u = info.get("google_maps_url") or (f"https://www.google.com/maps/place/data=!4m2!3m1!1s{hexid}" if hexid else "")
    return (u + ("&" if "?" in u else "?") + "hl=vi") if u and "hl=" not in u else u


class PageDriver:
    """Thin Playwright wrapper: only clicks/scrolls/reads in the page, never builds Google requests."""

    def __init__(self, pg, url):
        self.pg, self.url = pg, url

    def pause(self, sec):
        self.pg.wait_for_timeout(int(sec * 1000))

    def open_reviews(self):
        if not self.url:
            return False
        self.pg.goto(self.url, wait_until="domcontentloaded", timeout=60000)
        self.pause(random.uniform(2.5, 4.0))
        if not self.pg.evaluate(_JS_TAB):
            return False
        try:
            self.pg.wait_for_selector("div.jftiEf[data-review-id]", timeout=10000)
        except Exception:
            return False
        return True

    def sort_newest(self):
        r = self.pg.evaluate(_JS_SORT_OPEN)
        if r == "already":
            return True
        if not r:
            return False
        self.pause(random.uniform(0.8, 1.6))
        if not self.pg.evaluate(_JS_SORT_PICK):
            return False
        self.pause(random.uniform(1.5, 2.5))
        return True

    def cards_html(self):
        return self.pg.evaluate(_JS_CARDS) or ""

    def scroll(self):
        return self.pg.evaluate(_JS_SCROLL)

    def expand_more(self):
        return self.pg.evaluate(_JS_EXPAND) or 0
