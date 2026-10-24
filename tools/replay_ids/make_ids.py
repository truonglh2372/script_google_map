# read-only: list GMaps places with picked photos but none sent (an_uong_quanh_day#1084)
import json, os, sys, time, hashlib
root = sys.argv[1]; out = sys.argv[2]; label = sys.argv[3]
D = os.path.join(root, "aqd_data"); now = time.time(); rows = []
for d in os.listdir(D):
    if not d.isdigit(): continue
    f = os.path.join(D, d)
    try: info = json.load(open(os.path.join(f, "info.json"), encoding="utf-8"))
    except Exception: continue
    items = info.get("photo_urls") or []
    if not items: continue
    try: rec = json.load(open(os.path.join(f, "applied.json"), encoding="utf-8"))
    except Exception: rec = {}
    if any((v or {}).get("photo_id") for v in (rec.get("urls") or {}).values()): continue
    done = os.path.join(f, "SAFE_DONE")
    if not os.path.exists(done): continue
    t = os.path.getmtime(done)
    thin = bool((info.get("_maps") or {}).get("thin"))
    rows.append((int(d), len(items), time.strftime("%Y-%m-%d %H:%M", time.localtime(t)), "thin" if thin else "", now - t <= 7 * 86400))
rows.sort(reverse=True)
for suffix, sel in (("all", rows), ("7d", [r for r in rows if r[4]])):
    p = f"{out}/gmaps_{label}_{suffix}.tsv"
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"# an_uong_quanh_day#1084 replay ids ({label}, {suffix}): photos picked but none sent; generated {time.strftime('%Y-%m-%d %H:%M')} ICT\n")
        fh.write("# id\tpicked\tsafe_done\tnote\n")
        for r in sel: fh.write(f"{r[0]}\t{r[1]}\t{r[2]}\t{r[3]}\n")
    print(suffix, len(sel), "thin", sum(1 for r in sel if r[3]))
