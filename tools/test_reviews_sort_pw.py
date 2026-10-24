"""Offline browser test of the Reviews sort (an_uong_quanh_day#1110): runs reviews_dom's real page JS + PageDriver
in a throw-away headless browser against local pages built from the saved fixtures. No Google traffic, no live profile.

Scenarios mirror what differs between the Mac and Admin panels: decoy popup buttons before the sort button
(Admin), a sort menu that renders late (> the old fixed 0.8-1.6 s wait), no role=menu wrapper, NFD labels,
English / unknown-language labels, a menu that ignores synthetic DOM clicks, and a sort that never applies.

Skips (exit 0, prints SKIP) when Playwright or a Chromium/Chrome is not installed.
Run: python tools/test_reviews_sort_pw.py [repo-root]
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unicodedata

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
        return re.sub(r"<!--[\s\S]*?-->", "", f.read())


CARDS = re.findall(r'<div class="jftiEf[^"]*"[^>]*data-review-id=.*?(?=<div class="jftiEf|\Z)', fx("cards_initial_collapsed.html"), re.S)


_SCRIPT = """
const PANEL = __PANEL__, STALE = __STALE__, SWAP = __SWAP__, DELAY = __DELAY__, TRUSTED = __TRUSTED__, APPLIES = __APPLIES__;
let SORTBTN = null; window.OPENS = 0; window.PICKED = null; window.DECOY = 0;
document.addEventListener('click', e => { const b = e.target.closest('button[aria-haspopup]');
  if (b && b !== SORTBTN && !b.closest('#menu')) window.DECOY++; }, true);
function wire() {
  SORTBTN = [...document.querySelectorAll('#hdr button[aria-haspopup=true]')].pop() || null;   // the last popup = sort
  if (!SORTBTN) return;
  SORTBTN.addEventListener('click', e => { if (TRUSTED && !e.isTrusted) return; window.OPENS++;
    setTimeout(() => { document.getElementById('menu').style.display = 'block' }, DELAY); });
}
document.querySelectorAll('#menu [role=menuitemradio]').forEach(i => i.addEventListener('click', e => {
  if (TRUSTED && !e.isTrusted) return;
  window.PICKED = i.getAttribute('data-index') || i.textContent.trim(); document.getElementById('menu').style.display = 'none';
  if (APPLIES && SORTBTN) { const t = i.textContent.trim(); SORTBTN.setAttribute('aria-label', t); SORTBTN.firstElementChild.textContent = t; } }));
document.addEventListener('keydown', e => { if (e.key === 'Escape') document.getElementById('menu').style.display = 'none'; });
if (STALE) setTimeout(() => { document.getElementById('panel').innerHTML = PANEL; wire(); }, SWAP); else wire();
"""


def page(header, menu, delay_ms=0, trusted_only=False, applies=True, hdr_after=False, stale=None, swap_ms=0):
    """header = HTML holding the sort button (its last aria-haspopup=true button is wired as the sort button),
    menu = the menu HTML (hidden until the sort button is clicked). hdr_after: the header is rendered AFTER the review
    cards. stale: HTML shown first (the old Overview with its preview cards, as observed on Admin) and replaced by the
    real panel after swap_ms."""
    hdr = f"<div id=hdr>{header}</div>"
    feed = f"<div id=feed style=\"height:300px;overflow-y:auto\">{''.join(CARDS)}</div>"
    panel = feed + hdr if hdr_after else hdr + feed
    js = (_SCRIPT.replace("__PANEL__", json.dumps(panel)).replace("__STALE__", "true" if stale else "false")
          .replace("__SWAP__", str(int(swap_ms))).replace("__DELAY__", str(int(delay_ms)))
          .replace("__TRUSTED__", str(trusted_only).lower()).replace("__APPLIES__", str(applies).lower()))
    return f"""<html lang="vi-RU"><body style="width:929px">
<div id=menu style="display:none;position:absolute;top:80px;left:20px;background:#fff">{menu}</div>
<div id=panel>{stale if stale else panel}</div>
<script>{js}</script></body></html>"""


def old_module(rev="804e9db"):
    """reviews_dom.py at an older commit (for the before/after proofs); None if unavailable."""
    try:
        src = subprocess.run(["git", "-C", ROOT, "show", f"{rev}:common/reviews_dom.py"], capture_output=True, text=True,
                             encoding="utf-8", check=True).stdout
    except Exception:
        return None
    m = types.ModuleType("reviews_dom_old"); exec(compile(src, "reviews_dom_old.py", "exec"), m.__dict__)
    return m


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed"); return 0
    nfd = unicodedata.normalize("NFD", fx("sort_menu.html"))
    no_wrap = re.sub(r'\srole="menu"', "", nfd)
    admin_hdr = fx("sort_area_admin.html")
    mac_hdr = fx("sort_button.html")
    en_hdr = admin_hdr.replace("Phù hợp nhất", "Most relevant").replace('class="FNe70d"', 'class="Zz9"')
    en_menu = re.sub(r'data-index="(\d)"', r'data-index="x\1"', unicodedata.normalize("NFC", fx("sort_menu.html"))
                     .replace("Phù hợp nhất", "Most relevant").replace("Mới nhất", "Newest"))
    xx_hdr = admin_hdr.replace("Phù hợp nhất", "Najbardziej trafne").replace('class="FNe70d"', 'class="Zz9"')
    xx_menu = re.sub(r'data-index="(\d)"', r'data-index="x\1"', unicodedata.normalize("NFC", fx("sort_menu.html"))
                     .replace("Phù hợp nhất", "Najbardziej trafne").replace("Mới nhất", "Najnowsze"))
    newest_hdr = mac_hdr.replace("Phù hợp nhất", "Mới nhất")
    stale_ov = fx("overview_stale.html")
    cases = [  # name, html, expected sort_newest(), expected picked item
        ("Mac panel, instant menu", page(mac_hdr, nfd), True, "1"),
        ("Admin panel: decoys first, menu after 2.5 s, no role=menu, NFD", page(admin_hdr, no_wrap, delay_ms=2500), True, "1"),
        ("menu ignores synthetic clicks (needs a real mouse click)", page(admin_hdr, nfd, trusted_only=True), True, "1"),
        ("English labels, renamed classes", page(en_hdr, en_menu), True, "x1"),
        ("unknown language: by position", page(xx_hdr, xx_menu), True, "x1"),
        ("already newest: no click", page(newest_hdr, nfd), True, None),
        ("click does not apply -> not_applied", page(admin_hdr, nfd, applies=False), "not_applied", "1"),
        ("menu never opens -> no_menu", page(admin_hdr, "<div></div>"), "no_menu", None),
        ("no sort button -> no_btn + diagnostics", page('<button class="pUmuib">Tìm hiểu thêm</button>', nfd),
         "no_btn:pop0-above0-below0-incard0-fne0-cards10", None),
        ("sort button rendered AFTER the first review card", page(admin_hdr, nfd, hdr_after=True), True, "1"),
        ("after the cards, English label + renamed class", page(en_hdr, en_menu, hdr_after=True), True, "x1"),
        ("after the cards, unknown language: fallback refuses (never below the cards)",
         page(xx_hdr, xx_menu, hdr_after=True), "no_btn:pop2-above0-below2-incard0-fne0-cards10", None),
        ("Admin race: stale Overview (preview cards, decoys) for 1.2 s, then the panel",
         page(admin_hdr, no_wrap, stale=stale_ov, swap_ms=1200), True, "1"),
    ]
    old = old_module()
    tmp = tempfile.mkdtemp(prefix="aqd_sort_pw_")
    with sync_playwright() as p:
        b = None
        for kw in ({}, {"channel": "chrome"}):
            try:
                b = p.chromium.launch(headless=True, **kw); break
            except Exception:
                continue
        if b is None:
            print("SKIP: no Chromium/Chrome for Playwright"); return 0
        pg = b.new_page(viewport={"width": 929, "height": 891})
        for i, (name, html, want, picked) in enumerate(cases):
            f = os.path.join(tmp, f"case{i}.html")
            open(f, "w", encoding="utf-8").write(html)
            pg.goto("file://" + f)
            d = rd.PageDriver(pg, "file://" + f)
            d.pause = lambda s: pg.wait_for_timeout(int(s * 100))
            got = d.sort_newest()
            check(f"{name}: result", got, want)
            check(f"{name}: picked", pg.evaluate("window.PICKED"), picked)
            check(f"{name}: no decoy button clicked", pg.evaluate("window.DECOY"), 0)
            if want != True:
                check(f"{name}: menu left closed", pg.evaluate("getComputedStyle(document.getElementById('menu')).display"), "none")
        if old is not None:
            print("=== before/after on the Admin-like panel (menu after 2.5 s)")
            f = os.path.join(tmp, "old.html"); open(f, "w", encoding="utf-8").write(page(admin_hdr, no_wrap, delay_ms=2500))
            pg.goto("file://" + f)
            od = old.PageDriver(pg, "file://" + f); od.pause = lambda s: pg.wait_for_timeout(int(s * 1000))
            check("old code (main 804e9db) -> unsorted on the Admin-like panel", bool(od.sort_newest()), False)
            pg.goto("file://" + f)
            nd = rd.PageDriver(pg, "file://" + f); nd.pause = lambda s: pg.wait_for_timeout(int(s * 100))
            check("new code -> sorted on the same panel", nd.sort_newest(), True)
        old22 = old_module("9583dd8")
        if old22 is not None:
            print("=== before/after on the observed Admin race (stale Overview for 1.2 s)")
            f = os.path.join(tmp, "race.html"); open(f, "w", encoding="utf-8").write(page(admin_hdr, no_wrap, stale=stale_ov, swap_ms=1200))
            pg.goto("file://" + f)
            od = old22.PageDriver(pg, "file://" + f); od.pause = lambda s: pg.wait_for_timeout(int(s * 1000))
            check("main 9583dd8 (#22) -> no_btn on the race (what Admin logged)", od.sort_newest(), "no_btn")
            check("... and it clicked no decoy", pg.evaluate("window.DECOY"), 0)
            pg.goto("file://" + f)
            nd = rd.PageDriver(pg, "file://" + f); nd.pause = lambda s: pg.wait_for_timeout(int(s * 100))
            check("new code -> sorted on the same race", nd.sort_newest(), True)
            print("=== full page_reviews through the race")
            d = rd.PageDriver(pg, "file://" + f)
            d.open_reviews = lambda: (pg.goto("file://" + f), pg.wait_for_selector("div.jftiEf[data-review-id]"), True)[2]
            d.pause = lambda s: pg.wait_for_timeout(int(s * 100))
            rows, note = rd.page_reviews(d, [], cap=10, want=10, rating=4.5, wait=(0.1, 0.2))
            check("race: note says sorted", ("unsorted" in note, note.startswith("dom+")), (False, True))
            check("race: panel rows read, no stale overview ids", (len(rows), any(r["id"].startswith("OV_") for r in rows)), (10, False))
        print("=== full page_reviews on the Admin-like panel")
        f = os.path.join(tmp, "full.html"); open(f, "w", encoding="utf-8").write(page(admin_hdr, no_wrap, delay_ms=1200))
        d = rd.PageDriver(pg, "file://" + f)
        d.open_reviews = lambda: (pg.goto("file://" + f), True)[1]
        d.pause = lambda s: pg.wait_for_timeout(int(s * 100))
        rows, note = rd.page_reviews(d, [], cap=10, want=10, rating=4.5, wait=(0.1, 0.2))
        check("note says sorted (no ',unsorted')", ("unsorted" in note, note.startswith("dom+")), (False, True))
        check("rows read", len(rows), 10)
        b.close()
    print("\nALL OK" if not FAILURES else f"\n{len(FAILURES)} FAILED: {FAILURES}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
