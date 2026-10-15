"""Signed-in Chrome gallery collector. Chrome must run with --user-data-dir=<aqd>/chrome-profile --remote-debugging-port=9222.
Ids: aqd_photo_backfill.txt (not in .photos_done). Per place >=30s+jitter, slow scroll, captcha -> 2h pause. Stop: STOP_PHOTOS.
Pushes only new URLs (applied.json) via safe_run.Guard: 1 request at a time, >=3s, <=5 urls/request.

Gallery policy: collect ONLY from relevant Maps category tabs (food/drink, menu, vibe/interior,
storefront/exterior, by-owner). Never the "All" tab. Optional CLIP filter is applied in safe_run.
"""
import json, os, random, re, sys, time
R = "C:/aqd"
sys.path.insert(0, R)
for l in open(R + "/creds.env", encoding="utf-8"):
    l = l.strip()
    if l and not l.startswith("#") and "=" in l:
        k, v = l.split("=", 1); os.environ[k.strip()] = v.strip().strip('"').strip("'")
import safe_run as S
from playwright.sync_api import sync_playwright
STOP = R + "/STOP_PHOTOS"; DONE = R + "/aqd_photo_backfill.txt.photos_done"; LOG = R + "/photos_log.md"

# Tabs we KEEP (vi + en). Do NOT include "Tất cả" / "All".
KEEP_TAB = re.compile(
    r"Thực phẩm|đồ uống|Đồ ăn|thức uống|Đồ ăn và thức uống|Food\s*&\s*drink|Food and drink|"
    r"Thực đơn|Menu|"
    r"Không khí|Vibe|Atmosphere|Ambiente|"
    r"Bên ngoài|Mặt tiền|Exterior|Outside|Storefront|"
    r"Của chủ sở hữu|chủ sở hữu|By owner|Owner|"
    r"Bên trong|Interior|Inside",
    re.I,
)
# Tabs we explicitly skip even if matched loosely
SKIP_TAB = re.compile(
    r"^Tất cả|^All\b|Street View|360|Nhật ký|Latest|Mới nhất|Videos?|Video",
    re.I,
)


def log(s):
    line = f"- {time.strftime('%Y-%m-%d %H:%M:%S')} {s}"
    open(LOG, "a", encoding="utf-8").write(line + "\n"); print(line, flush=True)


JS_URLS = """()=>{const s=new Set();document.querySelectorAll('[style*="googleusercontent"],img[src*="googleusercontent"]').forEach(e=>{
 const m=(e.getAttribute('style')||'').match(/url\\(["']?(https:[^"')]+)/);const u=m?m[1]:e.src;
 if(u&&/\\/(gps-cs-s|p)\\//.test(u))s.add(u.split('=')[0])});return [...s]}"""


def nap(sec):
    end = time.time() + sec
    while time.time() < end:
        if os.path.exists(STOP): return False
        time.sleep(min(20, max(0, end - time.time())))
    return True


def blocked(pg):
    t = (pg.url or "")
    if "/sorry/" in t: return True
    try:
        body = pg.inner_text("body", timeout=3000)[:5000]
    except Exception:
        return False
    return bool(re.search(r"unusual traffic|lưu lượng truy cập bất thường|not a robot|không phải là robot", body, re.I))


def signed_in(pg):
    return pg.locator('a[href*="ServiceLogin"]').count() == 0 and "chế độ bị hạn chế" not in pg.inner_text("body")[:20000]


def scroll_collect(pg, rounds=60):
    got = set(pg.evaluate(JS_URLS)); still = 0
    for _ in range(rounds):
        pg.mouse.move(220, 500); pg.mouse.wheel(0, 800); pg.wait_for_timeout(1300 + random.randint(0, 900))
        n = set(pg.evaluate(JS_URLS))
        still = still + 1 if n <= got else 0
        got |= n
        if still >= 4: break
    return got


LAST_LINKS = {}
SKIP_REASON = {}


def _norm(t):
    import unicodedata
    t = unicodedata.normalize("NFC", t or "").casefold()
    return re.sub(r"[\W_]+", "", t)


def _scoped_urls(pg, label):
    return pg.evaluate("""(lab)=>{const m=[...document.querySelectorAll('[role=main]')].find(e=>e.getAttribute('aria-label')===lab);
      if(!m) return null; const s=new Set();
      m.querySelectorAll('[style*="googleusercontent"],img[src*="googleusercontent"]').forEach(x=>{
        const mm=(x.getAttribute('style')||'').match(/url\\(["']?(https:[^"')]+)/);const u=mm?mm[1]:x.src;
        if(u&&/\\/(gps-cs-s|p)\\//.test(u))s.add(u.split('=')[0])});return [...s]}""", label)


def _scroll_scoped(pg, label, rounds=60):
    got = set(_scoped_urls(pg, label) or []); still = 0
    for _ in range(rounds):
        pg.mouse.move(220, 500); pg.mouse.wheel(0, 800); pg.wait_for_timeout(1300 + random.randint(0, 900))
        n = _scoped_urls(pg, label)
        if n is None: return None
        n = set(n); still = still + 1 if n <= got else 0
        got |= n
        if still >= 4: break
    return got


def _tab_keep(label: str) -> bool:
    lab = (label or "").strip()
    if not lab: return False
    if SKIP_TAB.search(lab): return False
    return bool(KEEP_TAB.search(lab))


def gallery(pg, pid, expect_name=None, hexid=None):
    """Returns set of photo URLs (may be empty), None on captcha, or "SKIP" if the page could not be verified
    as exactly this place. Collects only inside the gallery panel, from relevant category tabs only."""
    SKIP_REASON.clear(); LAST_LINKS.clear()
    pg.goto("about:blank")
    pg.goto(f"https://www.google.com/maps/place/?q=place_id:{pid}&hl=vi", wait_until="domcontentloaded")
    title = ""
    for _ in range(30):
        pg.wait_for_timeout(500)
        if blocked(pg): return None
        h = pg.eval_on_selector_all('[role=main] h1', "els=>els.map(e=>e.innerText.trim()).filter(Boolean)")
        if h and "/maps/place/" in pg.url and "!1s0x" in pg.url: title = h[0]; break
    if not title:
        SKIP_REASON["r"] = "place panel not loaded"; return "SKIP"
    if expect_name and _norm(title) != _norm(expect_name):
        SKIP_REASON["r"] = f"title mismatch {title!r} != {expect_name!r}"; return "SKIP"
    if hexid and hexid.lower() not in pg.url.lower():
        SKIP_REASON["r"] = f"url hex id mismatch (want {hexid})"; return "SKIP"
    try:
        for h in pg.eval_on_selector_all('[role=main] a[href]', "els=>els.map(e=>e.href)"):
            if re.search(r"(facebook\.com|fb\.com)/", h) and "facebook" not in LAST_LINKS and "sharer" not in h: LAST_LINKS["facebook"] = h
            if "tiktok.com/@" in h and "tiktok" not in LAST_LINKS: LAST_LINKS["tiktok"] = h
    except Exception:
        pass
    label = "Ảnh của " + title
    btn = pg.locator(f'[role=main] button[aria-label="{label}"]')
    if not btn.count(): return set()
    btn.first.click()
    ok = False
    for _ in range(16):
        pg.wait_for_timeout(500)
        if blocked(pg): return None
        if _scoped_urls(pg, label) is not None: ok = True; break
    if not ok:
        return set()
    tabs = pg.locator(f'[role=main][aria-label="{label}"] [role="tab"]')
    labels = tabs.evaluate_all("els=>els.map(e=>(e.getAttribute('aria-label')||e.innerText||'').trim())")
    keep_idx = [i for i, l in enumerate(labels) if _tab_keep(l)]
    # If Maps has no category tabs (only All / none), fall back to All + CLIP later in safe_run
    if not keep_idx:
        allx = next((i for i, l in enumerate(labels) if re.match(r"Tất cả|^All\b", l or "", re.I)), None)
        if allx is not None:
            tabs.nth(allx).click(); pg.wait_for_timeout(2500)
            urls = _scroll_scoped(pg, label)
            if urls is None:
                SKIP_REASON["r"] = "gallery panel vanished while scrolling"; return "SKIP"
            SKIP_REASON["tabs"] = "fallback_all_no_category_tabs"
            return urls
        # no tabs at all — scroll current (usually All)
        urls = _scroll_scoped(pg, label)
        if urls is None:
            SKIP_REASON["r"] = "gallery panel vanished while scrolling"; return "SKIP"
        SKIP_REASON["tabs"] = "no_tabs"
        return urls
    urls = set()
    used = []
    for i in keep_idx:
        tabs.nth(i).click(); pg.wait_for_timeout(2500)
        more = _scroll_scoped(pg, label, 40 if i == keep_idx[0] else 20)
        if more is None:
            SKIP_REASON["r"] = f"tab panel vanished: {labels[i]!r}"; return "SKIP"
        urls |= more
        used.append(labels[i])
    SKIP_REASON["tabs"] = ",".join(used)
    return urls


def main():
    done = {l.split()[0] for l in open(DONE, encoding="utf-8")} if os.path.exists(DONE) else set()
    ids = []
    for l in open(R + "/aqd_photo_backfill.txt", encoding="utf-8"):
        t = l.split()
        if t and t[0].isdigit() and t[0] not in done and int(t[0]) not in ids: ids.append(int(t[0]))
    ids = [i for i in sorted(ids, reverse=True) if os.path.exists(f"{R}/aqd_data/{i}/info.json")]
    if os.environ.get("PHOTOS_IDS"): ids = [int(x) for x in os.environ["PHOTOS_IDS"].split(",")]
    if os.environ.get("PHOTOS_LIMIT"): ids = ids[:int(os.environ["PHOTOS_LIMIT"])]
    G = S.Guard(); hour = []
    with sync_playwright() as p:
        br = p.chromium.connect_over_cdp("http://127.0.0.1:9222"); ctx = br.contexts[0]
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.goto("https://www.google.com/maps?hl=vi"); pg.wait_for_timeout(4000)
        if not signed_in(pg): log(f"NOT SIGNED IN in {R}/chrome-profile window; stop"); sys.exit(5)
        log(f"START {len(ids)} ids")
        for rid in ids:
            if os.path.exists(STOP): log("STOP_PHOTOS; end"); break
            hour = [t for t in hour if time.time() - t < 3600]
            if len(hour) >= 100: time.sleep(3600 - (time.time() - hour[0]))
            t0 = time.time(); hour.append(t0)
            folder = f"{R}/aqd_data/{rid}"; info = json.load(open(folder + "/info.json", encoding="utf-8"))
            m = re.search(r"place_id=([^&]+)", info.get("google_maps_url") or "")
            if not m: continue
            try:
                urls = gallery(pg, m.group(1), expect_name=info.get('name'))
            except Exception as e:
                log(f"#{rid} BROWSER ERROR {type(e).__name__}: {str(e)[:120]}"); urls = set()
            if urls == "SKIP":
                log(f"#{rid} SKIP unsure: {SKIP_REASON.get('r')}"); open(DONE, "a", encoding="utf-8").write(f"{rid} skip-unsure {time.strftime('%F %H:%M')}\n"); continue
            if urls is None:
                log(f"#{rid} CAPTCHA/unusual traffic -> stop photos, back off 2h (not bypassing)")
                open(R + "/photos_backoff.txt", "w", encoding="utf-8").write(str(time.time() + 7200)); sys.exit(4)
            recp = folder + "/applied.json"
            rec = json.load(open(recp, encoding="utf-8")) if os.path.exists(recp) else {}
            rec.setdefault("urls", {})
            have_base = {v["url"].split("=")[0] for v in rec["urls"].values()}
            new = [u + "=s1600" for u in sorted(urls) if u not in have_base]
            added = 0
            try:
                for i in range(0, len(new), S.PHOTO_CHUNK):
                    ch = new[i:i + S.PHOTO_CHUNK]
                    body = G.req("POST", f"{S.API}/{rid}/photos", json_body={"image_urls": ch}, raw_status=(422,))
                    bad = {e.get("url") for e in body.get("errors") or [] if isinstance(e, dict)}; it = iter(body.get("photos") or [])
                    for u in ch:
                        if u in bad: continue
                        ph = next(it, None)
                        if ph: rec["urls"][S._url_hash(u)] = {"url": u, "photo_id": int(ph["id"])}; added += 1
                    json.dump(rec, open(recp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            except S.Overload as e:
                log(f"#{rid} SERVER OVERLOAD {e}; wait 5 min, then stop if again"); nap(300)
                try: G.req("GET", f"{S.API}/{rid}")
                except S.Overload: log("second failure -> STOP"); break
                continue
            open(DONE, "a", encoding="utf-8").write(f"{rid} +{added} gallery {len(urls)} {time.strftime('%F %H:%M')}\n")
            log(f"#{rid} gallery {len(urls)} urls, +{added} new pushed tabs={SKIP_REASON.get('tabs','')}")
            w = 30 + random.uniform(0, 15) - (time.time() - t0)
            if w > 0 and not nap(w): break


if __name__ == "__main__":
    main()
