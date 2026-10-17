"""Fetch Google extras for already-processed places and push them like the fetcher does.
usage: extra_backfill.py [--dry] [--gap 20] rid [rid ...]
Per place: 2 Maps requests (same as maps_fetch), extra.json written locally, opening periods PUT only if the
server has none (--dry: GET only, no writes on the server)."""
import json, os, sys, time, re
R = os.environ.get("AQD_ROOT") or os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, R)
import maps_fetch as mf  # noqa: E402  (before safe_run, which prepends its own root)
import safe_run as S
from aqd_api import DATA_DIR, API
args = sys.argv[1:]; dry = "--dry" in args; gap = 20.0
if "--gap" in args: gap = float(args[args.index("--gap") + 1]); args.remove(str(args[args.index("--gap") + 1])); args.remove("--gap")
ids = [int(a) for a in args if a.isdigit()]
G = S.Guard()
for n, rid in enumerate(ids):
    folder = os.path.join(DATA_DIR, str(rid)); info = json.load(open(os.path.join(folder, "info.json"), encoding="utf-8"))
    pid = (info.get("_maps") or {}).get("place_id") or re.search(r"query_place_id=([\w-]+)", info.get("google_maps_url", "")).group(1)
    if n: time.sleep(gap)
    p = mf.g(mf.fetch_place_json(pid), 6)
    ex = mf.extras(p); S._review_photo_filter(ex); info["_extra"] = ex
    recp = os.path.join(folder, "applied.json"); rec = json.load(open(recp, encoding="utf-8")) if os.path.exists(recp) else {}
    save = lambda: json.dump(rec, open(recp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if dry:
        cur = G.req("GET", f"{API}/{rid}/opening_periods").get("periods") or []
        print(rid, "DRY", f"rating={ex['google_rating']} n={ex['google_review_count']} reviews={len(ex['reviews'])}",
              f"rv_photos={sum(len(r['photos']) for r in ex['reviews'])} about={sum(len(g['items']) for g in ex['about'])}",
              f"periods_google={len(ex['opening_periods'] or [])} server_has={len(cur)}", flush=True)
        json.dump(ex, open(os.path.join(folder, "extra.dry.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    else:
        print(rid, S.push_extra(G, rid, folder, info, rec, save), flush=True)
