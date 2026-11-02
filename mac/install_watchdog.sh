#!/bin/sh
# Watchdog cho crawler Google Maps (an_uong_quanh_day#1388): launchd chạy <root>/watchdog.py mỗi 15 phút.
# alt.py chết (và không có <root>/STOP_ALL) -> launchctl kickstart/bootstrap aqd.alt rồi báo Slack.
#   sh mac/install_watchdog.sh              # cài / cài lại   (AQD_ROOT=... nếu root không phải ~/aqd)
#   sh mac/install_watchdog.sh --uninstall  # gỡ
set -eu
ROOT="${AQD_ROOT:-$HOME/aqd}"
LABEL=aqd.watchdog.gmaps
PL="$HOME/Library/LaunchAgents/$LABEL.plist"
U=$(id -u)
if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$U/$LABEL" 2>/dev/null || true; rm -f "$PL"; echo "Đã gỡ $LABEL"; exit 0
fi
[ -f "$ROOT/watchdog.py" ] || { echo "Không thấy $ROOT/watchdog.py - chạy mac/install.sh trước" >&2; exit 1; }
PY="$ROOT/venv/bin/python"; [ -x "$PY" ] || PY=/usr/bin/python3
CRAWLER_PL="$HOME/Library/LaunchAgents/aqd.alt.plist"
RESTART="launchctl kickstart gui/$U/aqd.alt || launchctl bootstrap gui/$U $CRAWLER_PL"
mkdir -p "$(dirname "$PL")"
cat > "$PL" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>$ROOT/watchdog.py</string>
    <string>--crawler</string><string>gmaps</string>
    <string>--state-dir</string><string>$ROOT</string>
    <string>--match</string><string>alt\.py</string>
    <string>--restart</string><string>$RESTART</string>
    <string>--stop-file</string><string>$ROOT/STOP_ALL</string>
    <string>--log</string><string>$ROOT/watchdog.log</string>
  </array>
  <key>StartInterval</key><integer>900</integer>
  <key>WorkingDirectory</key><string>$ROOT</string>
</dict></plist>
PLIST
launchctl bootout "gui/$U/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$U" "$PL"
echo "Đã cài $LABEL (mỗi 15 phút). Log: $ROOT/watchdog.log | Gỡ: sh mac/install_watchdog.sh --uninstall"
