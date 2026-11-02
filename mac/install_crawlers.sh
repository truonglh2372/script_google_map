#!/usr/bin/env bash
# One command per Mac for the crawler Slack reports (an_uong_quanh_day#1388). Idempotent: run it again any time.
#   curl -fsSL https://raw.githubusercontent.com/truonglh2372/script_google_map/main/mac/install_crawlers.sh | bash
# It asks for the Slack webhook, the machine name and (optional) the Slack user ID to tag; Enter keeps what
# ~/aqd/crawler.env already has. Then: latest code for the crawlers on this Mac (~/script_foody git pull, ~/aqd payload
# with backup), watchdogs (launchd, every 15 min, restart + Slack), restart of the crawlers (unless their STOP /
# STOP_ALL file exists), one Slack test message. The webhook is never printed.
set -u
WEBHOOK_FILE="${AQD_WEBHOOK_FILE:-}"; NAME="${AQD_SET_NAME:-}"; REF="${AQD_REPO_REF:-main}"
RESTART="${AQD_RESTART:-1}"; TEST="${AQD_SLACK_TEST:-1}"; WEBHOOK=""
if [ -n "$WEBHOOK_FILE" ]; then
  [ -f "$WEBHOOK_FILE" ] && WEBHOOK="$(tr -d '\r\n' < "$WEBHOOK_FILE")"; rm -f "$WEBHOOK_FILE"
fi
AQD="${AQD_ROOT:-$HOME/aqd}"; FOODY="${AQD_FOODY_ROOT:-$HOME/script_foody}"; U=$(id -u); TS=$(date +%Y%m%d_%H%M%S)
ENVF="$AQD/crawler.env"
say() { printf '[aqd] %s\n' "$*"; }

# 1. crawler.env
mkdir -p "$AQD"; umask 077
if [ ! -f "$ENVF" ]; then
  cat > "$ENVF" <<'TPL'
# Cấu hình crawler cho MÁY NÀY (an_uong_quanh_day#1388). Mẫu - file thật KHÔNG commit.
# File thật: Mac ~/aqd/crawler.env | Windows C:\aqd\crawler.env (chỉ chủ máy đọc được).
# Sửa bằng TextEdit/Notepad rồi lưu; crawler đọc lại khi khởi động, watchdog đọc lại mỗi 15 phút.
# Hoặc chạy lại lệnh cài: nó hỏi lại từng giá trị, Enter = giữ nguyên.

# Webhook Slack chung của kênh crawler: https://hooks.slack.com/services/...
SLACK_CRAWLER_WEBHOOK_URL=

# Tên máy hiện trong tin Slack, ví dụ: Admin, Mac mini, TRUONGLE
AQD_HOST_LABEL=

# Slack user ID (U... hoặc W...) được tag khi lỗi nặng. Để trống = không tag ai.
SLACK_ALERT_MENTION=
TPL
  say "created $ENVF"
fi
setkey() { # setkey KEY VALUE  (value via env, never on a command line)
  K="$1" V="$2" /usr/bin/python3 - "$ENVF" <<'PY'
import os, sys
p, k, v = sys.argv[1], os.environ["K"], os.environ["V"]
lines = open(p, encoding="utf-8-sig").read().splitlines()
for i, l in enumerate(lines):
    if l.split("=", 1)[0].strip() == k and not l.lstrip().startswith("#"):
        lines[i] = f"{k}={v}"; break
else:
    lines.append(f"{k}={v}")
open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY
}
getkey() { /usr/bin/sed -n "s/^$1=//p" "$ENVF" | tail -1 | tr -d '\r'; }
mask() { [ -z "$1" ] && { echo "(trống)"; return; }; printf '%s...%s' "$(printf %s "$1" | cut -c1-24)" "$(printf %s "$1" | rev | cut -c1-4 | rev)"; }
TTY=""; [ -z "$WEBHOOK_FILE" ] && { : < /dev/tty; } 2>/dev/null && TTY=/dev/tty
ask() { # ask PROMPT CURRENT_SHOWN -> REPLY
  REPLY=""; [ -n "$TTY" ] || return 0; printf '%s [%s]: ' "$1" "$2" > /dev/tty; IFS= read -r REPLY < /dev/tty || REPLY=""
}
CUR_W="$(getkey SLACK_CRAWLER_WEBHOOK_URL)"; CUR_N="$(getkey AQD_HOST_LABEL)"; CUR_M="$(getkey SLACK_ALERT_MENTION)"
[ -z "$CUR_N" ] && CUR_N="$(scutil --get ComputerName 2>/dev/null || hostname -s)"
if [ -z "$WEBHOOK" ]; then
  while :; do
    ask "Webhook Slack (https://hooks.slack.com/services/...), Enter = giữ" "$(mask "$CUR_W")"
    case "$REPLY" in ""|https://hooks.slack.com/*) WEBHOOK="$REPLY"; break;; *) echo "  phải bắt đầu bằng https://hooks.slack.com/" > /dev/tty;; esac
  done
fi
[ -n "$WEBHOOK" ] && setkey SLACK_CRAWLER_WEBHOOK_URL "$WEBHOOK" && say "webhook saved (hidden)"
if [ -z "$NAME" ]; then ask "Tên máy hiện trên Slack" "$CUR_N"; NAME="${REPLY:-$CUR_N}"; fi
setkey AQD_HOST_LABEL "$NAME"
ask "Slack user ID để tag khi lỗi nặng (U.../W..., '-' = xoá), Enter = giữ" "${CUR_M:-trống}"
case "$REPLY" in "") ;; -) setkey SLACK_ALERT_MENTION "";; *) setkey SLACK_ALERT_MENTION "$REPLY";; esac
chmod 600 "$ENVF"
[ -n "$(getkey SLACK_CRAWLER_WEBHOOK_URL)" ] || say "WARNING: SLACK_CRAWLER_WEBHOOK_URL is empty in $ENVF - nothing will be sent"
say "host label: $(getkey AQD_HOST_LABEL)"
# Older code reads ~/.config/aqd/slack_webhook: point it at crawler.env (one file to edit).
OLD="$HOME/.config/aqd/slack_webhook"; mkdir -p "$(dirname "$OLD")"
if [ -L "$OLD" ] || [ ! -e "$OLD" ]; then ln -sfn "$ENVF" "$OLD"; else mv "$OLD" "$OLD.bak_$TS" && ln -sfn "$ENVF" "$OLD" && say "old $OLD kept as .bak_$TS"; fi

# 2. code
SRC=$(mktemp -d); trap 'rm -rf "$SRC"' EXIT
if [ -d "$FOODY/.git" ]; then
  if [ -z "$(git -C "$FOODY" status --porcelain --untracked-files=no)" ]; then
    git -C "$FOODY" fetch -q origin "$REF" && git -C "$FOODY" checkout -q "$REF" 2>/dev/null; git -C "$FOODY" pull -q --ff-only origin "$REF" && say "script_foody: $(git -C "$FOODY" log --oneline -1)"
  else say "script_foody has local edits - not updated"; fi
  "$FOODY/.venv/bin/pip" install -q -r "$FOODY/requirements.txt" >/dev/null 2>&1 || true
fi
if [ -f "$AQD/alt.py" ]; then
  if curl -fsSL "https://codeload.github.com/truonglh2372/script_google_map/tar.gz/$REF" | tar -xz -C "$SRC" --strip-components 1; then
    B="$AQD/bak/pre1388_$TS"; mkdir -p "$B"
    for f in "$SRC"/common/*.py "$SRC/mac/alt.py"; do
      n=$(basename "$f"); if ! cmp -s "$f" "$AQD/$n"; then [ -f "$AQD/$n" ] && cp -p "$AQD/$n" "$B/"; cp -f "$f" "$AQD/$n"; say "google maps: updated $n"; fi
    done
    rmdir "$B" 2>/dev/null || say "backup: $B"
  else say "WARNING: could not download script_google_map@$REF - ~/aqd not updated"; fi
fi

# 3. watchdogs (repo scripts)
[ -f "$FOODY/common/watchdog.py" ] && [ -f "$HOME/Library/LaunchAgents/aqd.foody.plist" ] && AQD_FOODY_ROOT="$FOODY" sh "$FOODY/mac/install_watchdog.sh"
if [ -f "$AQD/watchdog.py" ] && [ -f "$HOME/Library/LaunchAgents/aqd.alt.plist" ]; then
  if [ -f "$SRC/mac/install_watchdog.sh" ]; then AQD_ROOT="$AQD" sh "$SRC/mac/install_watchdog.sh"; fi
fi

# 4. restart
if [ "$RESTART" = 1 ]; then
  if [ -f "$HOME/Library/LaunchAgents/aqd.foody.plist" ]; then
    if [ -e "$FOODY/foody_data/STOP" ]; then say "foody: STOP file present - not restarted"
    else launchctl kickstart -k "gui/$U/aqd.foody" 2>/dev/null || launchctl bootstrap "gui/$U" "$HOME/Library/LaunchAgents/aqd.foody.plist"; say "foody restarted"; fi
  fi
  if [ -f "$HOME/Library/LaunchAgents/aqd.alt.plist" ]; then
    if [ -e "$AQD/STOP_ALL" ]; then say "google maps: STOP_ALL present - not restarted"
    else launchctl kickstart -k "gui/$U/aqd.alt" 2>/dev/null || launchctl bootstrap "gui/$U" "$HOME/Library/LaunchAgents/aqd.alt.plist"; say "google maps restarted"; fi
  fi
fi

# 5. Slack test
if [ "$TEST" = 1 ]; then
  AQD_CRAWLER_ENV="$ENVF" /usr/bin/python3 - <<'PY'
import json, os, urllib.request
conf = {}
for l in open(os.environ["AQD_CRAWLER_ENV"], encoding="utf-8-sig"):
    l = l.strip()
    if l and not l.startswith("#") and "=" in l:
        k, v = l.split("=", 1); conf[k.strip()] = v.strip().strip('"').strip("'")
url, host = conf.get("SLACK_CRAWLER_WEBHOOK_URL", ""), conf.get("AQD_HOST_LABEL", "")
if not url.startswith("https://"):
    print("[aqd] slack test: SKIPPED (no webhook)"); raise SystemExit
req = urllib.request.Request(url, json.dumps({"text": f"✅ *crawler @ {host}*: đã cài báo cáo Slack + watchdog (test, #1388)"}).encode(),
                             {"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=15) as r: print(f"[aqd] slack test: HTTP {r.status}")
except Exception as e:
    print(f"[aqd] slack test: FAILED {getattr(e, 'code', type(e).__name__)}")
PY
fi
say "done. Edit $ENVF any time."
