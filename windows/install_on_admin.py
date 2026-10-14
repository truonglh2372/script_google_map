# -*- coding: utf-8 -*-
import base64, pathlib, shutil, time, ast, sys
R = pathlib.Path(r"C:/aqd")
(R / "bak_fix").mkdir(exist_ok=True)
ts = time.strftime("%Y%m%d_%H%M%S")
for name in ("photos.py", "safe_run.py"):
    p = R / name
    shutil.copy2(p, R / "bak_fix" / f"{name}.{ts}")
    shutil.copy2(p, R / "bak_fix" / name)
    print("bak", name)

(R / "photo_filter.py").write_bytes(base64.b64decode((R / "_pf.b64").read_text().strip()))
text = base64.b64decode((R / "_ph.b64").read_text().strip()).decode("utf-8")
needle = 'R = os.environ.get("AQD_ROOT") or ("C:/aqd" if sys.platform.startswith("win") else os.path.expanduser("~/aqd"))'
text = text.replace(needle, 'R = "C:/aqd"')
(R / "photos.py").write_text(text, encoding="utf-8")
print("wrote photo_filter + photos.py")

sr_path = R / "safe_run.py"
sr = sr_path.read_text(encoding="utf-8")
if "import photo_filter" not in sr:
    sr = sr.replace(
        "from olcdec import decode as olc_decode\n",
        "from olcdec import decode as olc_decode\ntry:\n    import photo_filter as PF\nexcept Exception:\n    PF = None\n",
        1,
    )
    print("import added")

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
print("photo block patched")

old_maps = 'info["_maps"] = {"via": meta.get("via", "http"), "gallery": meta.get("gallery"), "place_id": pid, "thin": meta.get("thin"), "count": meta.get("maps_photo_count_label"), "cats": meta.get("photo_categories")}'
new_maps = 'info["_maps"] = {"via": meta.get("via", "http"), "gallery": meta.get("gallery"), "place_id": pid, "thin": meta.get("thin"), "count": meta.get("maps_photo_count_label"), "cats": meta.get("photo_categories"), "photos_skipped": meta.get("photos_skipped", 0)}'
if old_maps not in sr:
    raise SystemExit("maps block missing")
sr = sr.replace(old_maps, new_maps, 1)
print("maps patched")

old_prep = 'gallery={info["_maps"].get("gallery")}" + (f" STAY_DRAFT:{reason}" if reason else ""))'
new_prep = 'gallery={info["_maps"].get("gallery")} photos_skipped={info["_maps"].get("photos_skipped",0)}" + (f" STAY_DRAFT:{reason}" if reason else ""))'
if old_prep not in sr:
    hits = [l for l in sr.splitlines() if "gallery=" in l]
    raise SystemExit("prep log missing: " + repr(hits[:3]))
sr = sr.replace(old_prep, new_prep, 1)
print("prep log patched")

sr_path.write_text(sr, encoding="utf-8")
for p in (R / "photo_filter.py", R / "photos.py", R / "safe_run.py"):
    ast.parse(p.read_text(encoding="utf-8"))
    print("syntax ok", p.name)
print("INSTALL OK")
