"""Patch safe_run.py: meet-in-the-middle stop. When AQD_MEET_STOP=N and N places in a row are already taken
(named / not draft on the server, i.e. done by the other machine), the run ends cleanly (rc 0)."""
import sys, re, shutil, time
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
if "AQD_MEET_STOP" in s:
    print("already patched"); sys.exit(0)
shutil.copy(p, p + ".bak_pool_" + time.strftime("%Y%m%d%H%M"))
old = """                log(f"#{rid} SKIP pre-check: taken (status={cur.get('status')} name={cur.get('name')!r})"); continue"""
assert old in s, "skip line not found"
new = """                log(f"#{rid} SKIP pre-check: taken (status={cur.get('status')} name={cur.get('name')!r})")
                _taken = globals().get("_TAKEN", 0) + 1; globals()["_TAKEN"] = _taken
                if int(os.environ.get("AQD_MEET_STOP", "0") or 0) and _taken >= int(os.environ["AQD_MEET_STOP"]):
                    log(f"MEET: {_taken} places in a row already done by the other machine; stop"); break
                continue
            globals()["_TAKEN"] = 0"""
s = s.replace(old, new)
open(p, "w", encoding="utf-8").write(s)
print("patched")
