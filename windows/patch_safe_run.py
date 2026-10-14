# -*- coding: utf-8 -*-
import ast, re, shutil, time
from pathlib import Path
R = Path(r"C:/aqd")
ts = time.strftime("%Y%m%d_%H%M%S")
bak = R / "bak_fix" / "safe_run.py"
if bak.exists():
    shutil.copy2(bak, R / "safe_run.py")
    print("restored bak_fix/safe_run.py")
shutil.copy2(R / "safe_run.py", R / "bak_fix" / ("safe_run.py.%s.prefilter" % ts))

sr_path = R / "safe_run.py"
sr = sr_path.read_text(encoding="utf-8")

if "import photo_filter" not in sr:
    sr = sr.replace(
        "from olcdec import decode as olc_decode\n",
        "from olcdec import decode as olc_decode\ntry:\n    import photo_filter as PF\nexcept Exception:\n    PF = None\n",
        1,
    )
    print("import ok")

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
    raise SystemExit("photo block missing")
sr = sr.replace(old, new, 1)
print("photo ok")

lines = sr.splitlines(keepends=True)
found = False
for i, line in enumerate(lines):
    if "_maps" in line and "meta.get" in line and ("photo_categories" in line or "maps_photo_count" in line):
        if "photos_skipped" in line:
            print("maps already", i + 1)
            found = True
            break
        raw = line.rstrip("\r\n")
        nl = line[len(raw):]
        if not raw.endswith("}"):
            continue
        if "'cats'" in raw or "'count'" in raw:
            lines[i] = raw[:-1] + ", 'photos_skipped': meta.get('photos_skipped', 0)}" + nl
        else:
            lines[i] = raw[:-1] + ', "photos_skipped": meta.get("photos_skipped", 0)}' + nl
        print("maps ok", i + 1)
        found = True
        break
if not found:
    raise SystemExit("maps line missing")
sr = "".join(lines)

def add_skip(mo):
    g = mo.group(0)
    if "photos_skipped" in g:
        return g
    if "'" in g:
        return g + " photos_skipped={info['_maps'].get('photos_skipped',0)}"
    return g + ' photos_skipped={info["_maps"].get("photos_skipped",0)}'

sr2, n = re.subn(
    r'gallery=\{info\[[\'"]_maps[\'"]\]\.get\([\'"]gallery[\'"]\)\}',
    add_skip,
    sr,
)
print("gallery subs", n)
if n < 1:
    raise SystemExit("no gallery= to patch")
sr = sr2

sr_path.write_text(sr, encoding="utf-8")
ast.parse(sr)
print("SYNTAX OK")
for i, l in enumerate(sr.splitlines(), 1):
    if "photos_skipped" in l or "photo_filter" in l:
        print("%d:%s" % (i, l[:160]))
