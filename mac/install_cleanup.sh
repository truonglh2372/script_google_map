#!/bin/sh
# Tự dọn ổ cho crawler gmaps (an_uong_quanh_day#1388, sau disk_low 04/11): launchd chạy disk_cleanup.py mỗi 15 phút;
# script tự quyết: dọn định kỳ mỗi AQD_CLEANUP_EVERY_H giờ (6) hoặc ngay khi ổ < AQD_CLEANUP_MIN_FREE_GB (8).
# Chỉ xoá thứ tạo lại được (xem đầu disk_cleanup.py); mỗi lần dọn gửi 1 dòng Slack. Log: $ROOT/cleanup.log
#   sh mac/install_cleanup.sh               # cài / cài lại
#   sh mac/install_cleanup.sh --uninstall   # gỡ
#   sh mac/install_cleanup.sh --dry-run     # chỉ liệt kê những gì sẽ xoá, không cài, không xoá
set -eu
ROOT="${AQD_ROOT:-$HOME/aqd}"
LABEL=aqd.cleanup.gmaps
PL="$HOME/Library/LaunchAgents/$LABEL.plist"
U=$(id -u)
if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$U/$LABEL" 2>/dev/null || true; rm -f "$PL"; echo "Đã gỡ $LABEL"; exit 0
fi
MOD="$ROOT/disk_cleanup.py"
[ -f "$MOD" ] || { echo "Không thấy $MOD - cài crawler trước" >&2; exit 1; }
PY="$ROOT/venv/bin/python"; [ -x "$PY" ] || PY=/usr/bin/python3
if [ "${1:-}" = "--dry-run" ]; then exec "$PY" "$MOD" --crawler gmaps --root "$ROOT" --dry-run; fi
mkdir -p "$(dirname "$PL")"
cat > "$PL" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>$MOD</string>
    <string>--crawler</string><string>gmaps</string>
    <string>--root</string><string>$ROOT</string>
    <string>--log</string><string>$ROOT/cleanup.log</string>
  </array>
  <key>StartInterval</key><integer>900</integer>
  <key>LowPriorityIO</key><true/><key>Nice</key><integer>10</integer>
  <key>WorkingDirectory</key><string>$ROOT</string>
</dict></plist>
PLIST
launchctl bootout "gui/$U/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$U" "$PL"
echo "Đã cài $LABEL (kiểm tra mỗi 15 phút; dọn mỗi ${AQD_CLEANUP_EVERY_H:-6}h hoặc khi ổ < ${AQD_CLEANUP_MIN_FREE_GB:-8} GB). Log: $ROOT/cleanup.log | Gỡ: sh mac/install_cleanup.sh --uninstall"
