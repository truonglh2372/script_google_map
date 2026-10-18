"""Backfill: push every local extra.json of THIS machine's parity to the /v1 admin API (google_extras.push).
usage: push_extras_v1.py [--dry] [--parity odd|even] [--force] [rid ...]
--dry prints the payload sizes only. Already pushed places (applied.json extras_v1 newer than extra.json) are skipped
unless --force. Parity defaults to $AQD_PARITY."""
import json, os, sys, time
R = os.environ.get("AQD_ROOT") or os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, R)
import safe_run as S  # noqa: E402
import google_extras as GX  # noqa: E402
from aqd_api import DATA_DIR, API, APIError  # noqa: E402

a = sys.argv[1:]
dry, force = "--dry" in a, "--force" in a
par = os.environ.get("AQD_PARITY", "")
if "--parity" in a: par = a[a.index("--parity") + 1]
ids = [int(x) for x in a if x.isdigit()]
if not ids:
    ids = sorted(int(d) for d in os.listdir(DATA_DIR) if d.isdigit() and os.path.exists(os.path.join(DATA_DIR, d, "extra.json")))
if par in ("odd", "even"):
    ids = [i for i in ids if i % 2 == (1 if par == "odd" else 0)]
G = None if dry else S.Guard()
cnt = {"ok": 0, "skip_done": 0, "fail": 0, "about": 0, "reviews": 0, "hours": 0, "hours_none": 0, "hours_owner": 0}
for rid in ids:
    folder = os.path.join(DATA_DIR, str(rid)); exp = os.path.join(folder, "extra.json"); recp = os.path.join(folder, "applied.json")
    ex = json.load(open(exp, encoding="utf-8"))
    rec = json.load(open(recp, encoding="utf-8")) if os.path.exists(recp) else {}
    done = (rec.get("extras_v1") or {}).get("at", "")
    if done and done >= ex.get("saved_at", "") and not force and "fail" not in rec["extras_v1"].get("result", ""):
        cnt["skip_done"] += 1; continue
    if dry:
        hb = GX.hours_payload(ex)
        print(rid, "DRY about", sum(len(g["items"]) for g in GX.attributes_payload(ex.get("about"))),
              "reviews", len(GX.reviews_payload(ex)["reviews"]), "hours", hb and (len(hb["periods"]), hb["closed_days"], hb["is_24h_days"]), flush=True)
        continue
    try:
        cur = G.req("GET", f"{API}/{rid}")["restaurant"]
        res = GX.push(G.req, rid, ex, current=cur, api_error=APIError)
    except S.Overload as e:
        print(rid, "overload", e, "- sleeping 120s", flush=True); time.sleep(120); cnt["fail"] += 1; continue
    except Exception as e:
        print(rid, "FAIL", type(e).__name__, e, flush=True); cnt["fail"] += 1; continue
    rec["extras_v1"] = {"at": time.strftime("%F %T"), "result": res}
    json.dump(rec, open(recp + ".tmp", "w", encoding="utf-8"), ensure_ascii=False, indent=1); os.replace(recp + ".tmp", recp)
    cnt["ok"] += 1
    cnt["about"] += "about=" in res; cnt["reviews"] += "reviews=" in res
    cnt["hours"] += "hours=google" in res; cnt["hours_none"] += "none_on_google" in res; cnt["hours_owner"] += "owner_confirmed" in res
    if "fail" in res: cnt["fail"] += 1
    print(rid, res, flush=True)
print("SUMMARY", json.dumps(cnt), flush=True)
