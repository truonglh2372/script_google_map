import re, sys
p = sys.argv[1] + "/photos.py"; s = open(p, encoding="utf-8").read()
a = s.index("def gallery(pg, pid):"); b = s.index("def main():")
new = r'''def _norm(t):
    import unicodedata
    t = unicodedata.normalize("NFC", t or "").casefold()
    return re.sub(r"[\W_]+", "", t)


SKIP_REASON = {}


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
        if n is None: return None          # gallery panel vanished -> unsure
        n = set(n); still = still + 1 if n <= got else 0
        got |= n
        if still >= 4: break
    return got


def gallery(pg, pid, expect_name=None, hexid=None):
    """Returns set of photo URLs (may be empty), None on captcha, or "SKIP" if the page could not be verified
    as exactly this place. Collects only inside the gallery panel [role=main][aria-label="Ảnh của <name>"]."""
    SKIP_REASON.clear(); LAST_LINKS.clear()
    pg.goto("about:blank")                                    # reset: no stale panel from the previous place
    pg.goto(f"https://www.google.com/maps/place/?q=place_id:{pid}&hl=vi", wait_until="domcontentloaded")
    title = ""
    for _ in range(30):                                       # wait up to ~15 s for the place panel
        pg.wait_for_timeout(500)
        if blocked(pg): return None
        h = pg.eval_on_selector_all('[role=main] h1', "els=>els.map(e=>e.innerText.trim()).filter(Boolean)")
        if h and "/maps/place/" in pg.url: title = h[0]; break
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
    if not btn.count(): return set()                         # place has no photo button -> no photos
    btn.first.click()
    ok = False
    for _ in range(16):
        pg.wait_for_timeout(500)
        if blocked(pg): return None
        if _scoped_urls(pg, label) is not None: ok = True; break
    if not ok:
        return set()                                          # hero opened Street View etc. -> no gallery photos
    tabs = pg.locator(f'[role=main][aria-label="{label}"] [role="tab"]')
    labels = tabs.evaluate_all("els=>els.map(e=>(e.getAttribute('aria-label')||e.innerText||'').trim())")
    allx = next((i for i, l in enumerate(labels) if l.startswith("Tất cả")), None)
    if allx is not None:
        tabs.nth(allx).click(); pg.wait_for_timeout(2500)
    urls = _scroll_scoped(pg, label)
    if urls is None:
        SKIP_REASON["r"] = "gallery panel vanished while scrolling"; return "SKIP"
    for i, l in enumerate(labels):
        if re.search(r"Thực đơn|Menu", l):
            tabs.nth(i).click(); pg.wait_for_timeout(2500)
            more = _scroll_scoped(pg, label, 15)
            if more is None: SKIP_REASON["r"] = "menu tab panel vanished"; return "SKIP"
            urls |= more
    return urls


'''
s = s[:a] + new + s[b:]
# backfill caller: pass expected name, handle SKIP
s = s.replace("                urls = gallery(pg, m.group(1))", "                urls = gallery(pg, m.group(1), expect_name=info.get('name'))")
s = s.replace("""            if urls is None:""", """            if urls == "SKIP":
                log(f"#{rid} SKIP unsure: {SKIP_REASON.get('r')}"); open(DONE, "a", encoding="utf-8").write(f"{rid} skip-unsure {time.strftime('%F %H:%M')}\\n"); continue
            if urls is None:""")
open(p, "w", encoding="utf-8").write(s)
# one-pass caller
p = sys.argv[1] + "/safe_run.py"; s = open(p, encoding="utf-8").read()
s = s.replace('''            gal = PH.gallery(_PW["pg"], pid)
            if gal is None:''', '''            _p6 = mf.g(d, 6)
            gal = PH.gallery(_PW["pg"], pid, expect_name=mf.g(_p6, 11), hexid=mf.g(_p6, 10))
            if gal == "SKIP":
                print(f"gallery skipped (unsure): {PH.SKIP_REASON.get('r')}", flush=True); gal = set()
            if gal is None:''')
s = s.replace('    r = _PW["pg"].evaluate(_JS, pid)', '    if "google.com" not in (_PW["pg"].url or ""):\n        _PW["pg"].goto("https://www.google.com/maps?hl=vi"); _PW["pg"].wait_for_timeout(2500)\n    r = _PW["pg"].evaluate(_JS, pid)')
s = s.replace(" or pg >= 100: break", " or pg >= 400: break")
open(p, "w", encoding="utf-8").write(s)
print("ok", "SKIP_REASON" in open(sys.argv[1] + "/photos.py", encoding="utf-8").read(), "expect_name=mf.g" in s)
