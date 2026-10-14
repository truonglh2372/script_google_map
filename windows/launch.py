"""Windows chain: run main queue -> list #2101..#2180 -> run it -> photo backfill. Stop: create C:\\aqd\\STOP"""
import os, subprocess, sys, time
R = "C:/aqd"
for l in open(R + "/creds.env", encoding="utf-8"):
    l = l.strip()
    if l and not l.startswith("#") and "=" in l:
        k, v = l.split("=", 1); os.environ[k.strip()] = v.strip().strip('"').strip("'")
os.environ.update(AQD_THROTTLE="2", AQD_UPLOAD_WORKERS="1", AQD_URL_BATCH_MAX="5", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
py = sys.executable
steps = [["run", "--ignore-peak", "--sleep", "0", "--floor", "2813"],
         ["list", "--floor", "2100", "--ceil", "2180", "--queue", R + "/queue2.json"],
         ["run", "--ignore-peak", "--sleep", "0", "--floor", "2100", "--queue", R + "/queue2.json"],
         ["backfill"]]
out = open(R + "/chain.out", "a", encoding="utf-8")
for st in steps:
    if os.path.exists(R + "/STOP"): break
    out.write(f"{time.strftime('%F %T')} STEP {st}\n"); out.flush()
    rc = subprocess.call([py, R + "/safe_run.py"] + st, stdout=out, stderr=out, cwd=R)
    if rc != 0: out.write(f"step failed rc={rc}\n"); break
out.write(f"{time.strftime('%F %T')} CHAIN DONE\n")
