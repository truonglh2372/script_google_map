#!/bin/bash
set -e
R=/Users/hoangtruongle/aqd
TS=$(date +%Y%m%d_%H%M%S)
mkdir -p "$R/bak"
cp -f "$R/alt.py" "$R/bak/alt.py.bak_chain_$TS"
cp -f "$R/safe_run.py" "$R/bak/safe_run.py.bak_chain_$TS"
cp -f "$R/queue6.json" "$R/bak/queue6.json.bak_chain_$TS" 2>/dev/null || true
cp -f /tmp/queue8.json "$R/queue8.json"
cp -f /tmp/alt_mac.py "$R/alt.py"
# page cap
python3 - <<'PY'
from pathlib import Path
p=Path("/Users/hoangtruongle/aqd/safe_run.py")
t=p.read_text()
for a,b in [("pg >= 100","pg >= 500"),("pg >= 400","pg >= 500")]:
    if a in t:
        t=t.replace(a,b); print("patched",a,b)
p.write_text(t)
print("has500", "pg >= 500" in p.read_text())
PY
echo 0 > "$R/alt_step.txt"
rm -f "$R/STOP"
# restart launchd aqd.alt
uid=$(id -u)
launchctl bootout "gui/$uid/aqd.alt" 2>/dev/null || launchctl remove aqd.alt 2>/dev/null || true
sleep 1
# kill leftover
pkill -f '/Users/hoangtruongle/aqd/alt.py' 2>/dev/null || true
pkill -f '/Users/hoangtruongle/aqd/safe_run.py' 2>/dev/null || true
sleep 2
# ensure chrome
if ! curl -s --max-time 2 http://127.0.0.1:9222/json/version >/dev/null; then
  launchctl bootout "gui/$uid/aqd.chrome" 2>/dev/null || true
  launchctl submit -l aqd.chrome -- /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir="$R/chrome-profile" https://www.google.com/maps
  sleep 5
fi
launchctl submit -l aqd.alt -- /usr/bin/caffeinate -dimsu "$R/venv/bin/python" "$R/alt.py"
sleep 3
launchctl print "gui/$uid/aqd.alt" 2>&1 | head -20
pgrep -fl 'alt.py|safe_run' | head -10
tail -15 "$R/alt.out"
