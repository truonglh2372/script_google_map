# -*- coding: utf-8 -*-
import ast, re, shutil, time, os
from pathlib import Path
R = Path(os.path.expanduser("~/aqd"))
ts = time.strftime("%Y%m%d_%H%M%S")
bakdir = R / "bak"
bakdir.mkdir(exist_ok=True)
for name in ("photos.py", "safe_run.py"):
    p = R / name
    if p.exists():
        shutil.copy2(p, bakdir / f"{name}.{ts}")
        print("bak", name)

# install photos + filter from Desktop-equivalent: same folder as this script OR R
src = Path(__file__).resolve().parent
for src_name, dst_name in (("photo_filter.py", "photo_filter.py"), ("photos_mac.py", "photos.py"), ("photos_new.py", "photos.py")):
    sp = src / src_name
    if sp.exists() and dst_name:
        if src_name.startswith("photos"):
            text = sp.read_text(encoding="utf-8")
            text = text.replace(
                'R = os.environ.get("AQD_ROOT") or ("C:/aqd" if sys.platform.startswith("win") else os.path.expanduser("~/aqd"))',
                'R = os.path.expanduser("~/aqd")',
            )
            text = text.replace('R = "C:/aqd"', 'R = os.path.expanduser("~/aqd")')
            (R / "photos.py").write_text(text, encoding="utf-8")
            print("wrote photos.py from", src_name)
            break
sp = src / "photo_filter.py"
if sp.exists():
    shutil.copy2(sp, R / "photo_filter.py")
    print("wrote photo_filter.py")

sr_path = R / "safe_run.py"
sr = sr_path.read_text(encoding="utf-8")
if "import photo_filter" not in sr:
    # Mac may use different import block
    inserted = False
    for anchor in (
        "from olcdec import decode as olc_decode\n",
        "from olcdec import decode as olc_decode\r\n",
        "import maps_fetch as mf\n",
    ):
        if anchor in sr:
            sr = sr.replace(anchor, anchor + "try:\n    import photo_filter as PF\nexcept Exception:\n    PF = None\n", 1)
            inserted = True
            print("import ok via", repr(anchor[:40]))
            break
    if not inserted:
        sr = "try:\n    import photo_filter as PF\nexcept Exception:\n    PF = None\n" + sr
        print("import prepended")

old = (
    "    # photos: drop videos; roles: storefront/owner -> cover, food -> logo\n"
    '    ph = [x for x in photos if "Video" not in (x.get("kind") or "")]\n'
    '    kinds = [x.get("kind") or "" for x in ph]'
)
new = (
    "    # photos: drop videos; CLIP filter (food/drink/menu/venue only); roles\n"
    '    ph = [x for x in photos if "Video" not in (x.get("kind") or "")]\n'
    "    photos_skipped = 0\n"
    '    if PF is not None and ph and __import__("os").environ.get("AQD_CLIP", "1") not in ("0", "false", "no"):\n'
    "        try:\n"
    "            kept, photos_skipped, _det = PF.filter_urls(ph, return_details=True)\n"
    "            ph = kept\n"
    "        except Exception as e:\n"
    '            print(f"photo_filter failed: {type(e).__name__}: {e}; keeping all", flush=True)\n'
    "            photos_skipped = 0\n"
    '    meta["photos_skipped"] = photos_skipped\n'
    '    kinds = [x.get("kind") or "" for x in ph]'
)
if old not in sr:
    raise SystemExit("photo block missing on Mac safe_run")
sr = sr.replace(old, new, 1)
print("photo ok")

lines = sr.splitlines(keepends=True)
found = False
for i, line in enumerate(lines):
    if "_maps" in line and "meta.get" in line and ("photo_categories" in line or "maps_photo_count" in line):
        if "photos_skipped" in line:
            print("maps already"); found = True; break
        raw = line.rstrip("\r\n"); nl = line[len(raw):]
        if not raw.endswith("}"):
            continue
        if "'cats'" in raw or "'count'" in raw:
            lines[i] = raw[:-1] + ", 'photos_skipped': meta.get('photos_skipped', 0)}" + nl
        else:
            lines[i] = raw[:-1] + ', "photos_skipped": meta.get("photos_skipped", 0)}' + nl
        print("maps ok"); found = True; break
if not found:
    raise SystemExit("maps missing")
sr = "".join(lines)

def add_skip(mo):
    g = mo.group(0)
    if "photos_skipped" in g:
        return g
    if "'" in g:
        return g + " photos_skipped={info['_maps'].get('photos_skipped',0)}"
    return g + ' photos_skipped={info["_maps"].get("photos_skipped",0)}'

sr2, n = re.subn(r'gallery=\{info\[[\'"]_maps[\'"]\]\.get\([\'"]gallery[\'"]\)\}', add_skip, sr)
print("gallery subs", n)
if n < 1:
    raise SystemExit("gallery missing")
sr = sr2
sr_path.write_text(sr, encoding="utf-8")
ast.parse(sr)
ast.parse((R / "photos.py").read_text(encoding="utf-8"))
ast.parse((R / "photo_filter.py").read_text(encoding="utf-8"))
print("MAC INSTALL OK", R)
