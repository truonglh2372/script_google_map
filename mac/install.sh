#!/usr/bin/env bash
# aqd one-command installer for macOS - the Mac counterpart of windows/install.ps1.
# Messages are ASCII only on purpose (they render the same in any Terminal locale).
#
# FRESH install (paste ONE line in Terminal; the repo is public, no token needed):
#   curl -fsSL https://raw.githubusercontent.com/truonglh2372/script_google_map/main/mac/install.sh | bash
#
# CLEAN reinstall of a Mac that already ran an older copy (stops what runs, reports, backs up,
# wipes the conflicting state, then installs again) - still ONE line:
#   curl -fsSL https://raw.githubusercontent.com/truonglh2372/script_google_map/main/mac/install.sh | AQD_CLEAN=1 bash
#
# Print every action without changing anything on the machine (recommended first, especially on a Mac
# that is already running a hand-patched copy):
#   curl -fsSL <the same url> | AQD_DRY_RUN=1 bash
#
# Install somewhere else (tests):   AQD_ROOT="$HOME/aqd_test" curl -fsSL <the same url> | bash
# Install the payload of another branch/tag (test before merging):
#   AQD_REPO_REF=feature/11-mac-one-command-installer curl -fsSL <the same url> | bash
# Local checkout, no network (offline harness):  AQD_SRC_DIR=/path/to/checkout bash mac/install.sh
#
# Flags when run as a file: --root DIR  --clean  --no-start  --no-chrome  --skip-heavy  --yes  --dry-run  --help
#
# Answers are read from /dev/tty when there is one, so `curl ... | bash` still prompts properly.
# Every prompt has a default: press Enter. Only AQD_ADMIN_EMAIL and AQD_ID_SUFFIXES are REQUIRED.
set -u

DEFAULT_BASE="https://anuongquanhday.com"
DEFAULT_LIST_PAGES="20"
DEFAULT_FETCH_LIMIT="0"
DEFAULT_CDP_PORT=9222
RAW_BASE="https://raw.githubusercontent.com/truonglh2372/script_google_map/main"
REPO="truonglh2372/script_google_map"
REPO_GIT="https://github.com/${REPO}.git"
REPO_ZIP="https://codeload.github.com/${REPO}/zip/refs/heads/main"
REQUIRED_PKGS="requests pillow playwright openlocationcode"

DRY_RUN=0
CLEAN=0
NO_START=0
NO_CHROME=0
SKIP_HEAVY=0
YES=0
CLI_ROOT=""

R=""
REF="main"
SRC=""
SRC_DIR="${AQD_SRC_DIR:-}"
TTY_SRC="${AQD_TTY-/dev/tty}"
WORK=""
TS="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR=""
PY_BASE=""
PY_VER=""
PY=""
CHROME_BIN=""
SUFFIXES_OUT=""
PARITY_OUT=""
LIST_PAGES_OUT="$DEFAULT_LIST_PAGES"
FETCH_LIMIT_OUT="$DEFAULT_FETCH_LIMIT"
CDP_PORT_OUT="$DEFAULT_CDP_PORT"
ANS=""
LOG_TAIL_FROM=0

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_CY=$'\033[36m'; C_GR=$'\033[32m'; C_YE=$'\033[33m'; C_RE=$'\033[31m'; C_0=$'\033[0m'
else
  C_CY=""; C_GR=""; C_YE=""; C_RE=""; C_0=""
fi

say() { printf '%s\n' "$*"; }
hdr() { say ""; say "${C_CY}==== $*${C_0}"; }
ok() { say "  ${C_GR}[ok]${C_0} $*"; }
done_ok() { if [ "$DRY_RUN" = 1 ]; then say "  [dry-run] (planned) $*"; else ok "$*"; fi; }
note() { say "  -  $*"; }
warn() { say "  ${C_YE}[!]${C_0} $*"; }
bad() { say "  ${C_RE}[x]${C_0} $*"; }

cleanup() { if [ -n "$WORK" ] && [ -d "$WORK" ]; then rm -rf "$WORK" 2>/dev/null || true; fi; }
trap cleanup EXIT

die() {
  say ""
  bad "INSTALL STOPPED: $*"
  note "Fix the cause and paste the same single command again - the installer is re-runnable."
  exit 1
}

usage() {
  say "aqd installer for macOS (mac/install.sh)"
  say ""
  say "One line:  curl -fsSL $RAW_BASE/mac/install.sh | bash"
  say "Clean   :  curl -fsSL $RAW_BASE/mac/install.sh | AQD_CLEAN=1 bash"
  say "Dry run :  curl -fsSL $RAW_BASE/mac/install.sh | AQD_DRY_RUN=1 bash"
  say ""
  say "Env: AQD_ROOT (default \$HOME/aqd), AQD_REPO_REF (default main), AQD_CLEAN, AQD_DRY_RUN,"
  say "     AQD_SRC_DIR (offline checkout), AQD_TTY (answers source; default /dev/tty), AQD_YES."
  say "Flags: --root DIR --clean --no-start --no-chrome --skip-heavy --yes --dry-run --help"
}

env_truthy() {
  case "$1" in
    ""|0|false|FALSE|no|NO|off|OFF) return 1 ;;
    *) return 0 ;;
  esac
}

fmt_secs() {
  local s="$1"
  if [ "$s" -ge 60 ]; then printf '%sm %ss' $((s / 60)) $((s % 60)); else printf '%ss' "$s"; fi
}

is_digits() { case "$1" in ""|*[!0-9]*) return 1 ;; esac; return 0; }
is_zeros() { case "$1" in ""|*[!0]*) return 1 ;; esac; return 0; }

count_lines() { if [ -z "$1" ]; then printf 0; else printf '%s\n' "$1" | wc -l | tr -d ' '; fi; }
count_words() { printf '%s' "$1" | wc -w | tr -d ' '; }

read_file() { cat "$1"; }

clean_path() { printf '%s' "$1" | tr '\\' '/' | sed -e 's#/*$##'; }
norm_path() { printf '%s' "$1" | tr '\\' '/' | tr 'A-Z' 'a-z' | sed -e 's#/*$##'; }

xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
sed_val() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }

file_sha256() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" 2>/dev/null | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" 2>/dev/null | awk '{print $1}'
  else printf 'unavailable'; fi
}

file_lines() { awk 'END{print NR}' "$1" 2>/dev/null; }

act() {
  if [ "$DRY_RUN" = 1 ]; then say "  [dry-run] $*"; return 1; fi
  return 0
}

mkdir_p() { if act "mkdir -p $1"; then mkdir -p "$1" || die "mkdir failed: $1"; fi; }
rm_rf() { if act "rm -rf $1"; then rm -rf "$1" 2>/dev/null || true; fi; }
rm_f() { if act "rm -f $1"; then rm -f "$1" 2>/dev/null || true; fi; }
cp_f() { if act "cp -f $1 -> $2"; then mkdir -p "$(dirname "$2")" 2>/dev/null || true; cp -f "$1" "$2" || die "cp failed: $1 -> $2"; fi; }
touch_f() { if act "touch $1"; then : >"$1" 2>/dev/null || true; fi; }
write_file() { if act "write $1"; then printf '%s' "$2" >"$1" || die "write failed: $1"; fi; }
kill_pid() { if act "kill -9 $1"; then kill -9 "$1" 2>/dev/null || true; fi; }

copy_dir_into() {
  if act "copy dir $1 -> $2/"; then
    if command -v rsync >/dev/null 2>&1; then
      rsync -a "$1/" "$2/$(basename "$1")/" || warn "rsync $1 failed"
    else
      cp -R "$1" "$2/" 2>/dev/null || warn "cp -R $1 failed"
    fi
  fi
}

read_line_to_var() {
  local prompt="$1" line=""
  if [ -n "$TTY_SRC" ] && [ -r "$TTY_SRC" ]; then
    printf '%s ' "$prompt"
    IFS= read -r line <"$TTY_SRC" || line=""
  else
    printf '%s\n' "$prompt"
    IFS= read -r line || line=""
  fi
  ANS="$line"
}

read_secret_to_var() {
  local prompt="$1" line=""
  if [ -n "$TTY_SRC" ] && [ -c "$TTY_SRC" ]; then
    printf '%s ' "$prompt"
    IFS= read -rs line <"$TTY_SRC" || line=""
    printf '\n'
  else
    printf '%s\n' "$prompt"
    note "stdin is not a real console: the value you type WILL be shown here in clear text - in Terminal it stays hidden"
    IFS= read -r line || line=""
  fi
  ANS="$line"
}

ask() {
  local q="$1" def="$2" req="${3:-}" p envv=""
  if [ "$YES" = 1 ]; then case "$q" in [A-Z_]*) envv="$(printenv "$q" 2>/dev/null || true)" ;; esac; fi
  if [ -n "$envv" ]; then note "$q = $envv (from env)"; ANS="$envv"; return; fi
  if [ "$YES" = 1 ] && [ -n "$def" ]; then note "$q -> $def (--yes default)"; ANS="$def"; return; fi
  if [ -n "$def" ]; then p="$q [$def]"
  elif [ "$req" = required ]; then p="$q (REQUIRED - no default)"
  else p="$q"; fi
  read_line_to_var "$p"
  if [ -z "$ANS" ] && [ -n "$def" ]; then ANS="$def"; fi
}

ask_secret() {
  local q="$1" def="$2" p envv
  envv=""; if [ "$YES" = 1 ]; then envv="$(printenv "${q%% *}" 2>/dev/null || true)"; fi
  if [ -n "$envv" ]; then note "${q%% *} taken from env (not printed)"; ANS="$envv"; return; fi
  if [ -n "$def" ]; then p="$q [Enter = keep the existing value]"; else p="$q (REQUIRED - no default)"; fi
  read_secret_to_var "$p"
  if [ -z "$ANS" ] && [ -n "$def" ]; then ANS="$def"; fi
}

confirm() {
  local q="$1" def="$2" hint
  if [ "$def" = yes ]; then hint="[Y/n]"; else hint="[y/N]"; fi
  if [ "$YES" = 1 ]; then note "$q $hint -> $def"; [ "$def" = yes ]; return; fi
  read_line_to_var "$q $hint"
  if [ -z "$ANS" ]; then [ "$def" = yes ]; return; fi
  case "$ANS" in [Yy]|[Yy][Ee][Ss]) return 0 ;; *) return 1 ;; esac
}

list_aqd_procs() {
  ps -Ao pid=,command= 2>/dev/null | while read -r pid cmd; do
    [ -n "$pid" ] || continue
    case "$cmd" in *alt.py*|*safe_run.py*|*menu_worker.py*) : ;; *) continue ;; esac
    printf '%s\t%s\n' "$pid" "$cmd"
  done
}

aqd_procs_in_root() {
  local needle
  needle="$(norm_path "$1")/"
  list_aqd_procs | while IFS="$(printf '\t')" read -r pid cmd; do
    case "$(norm_path "$cmd")" in *"$needle"*) printf '%s\t%s\n' "$pid" "$cmd" ;; esac
  done
}

aqd_procs_other_root() {
  local needle
  needle="$(norm_path "$1")/"
  list_aqd_procs | while IFS="$(printf '\t')" read -r pid cmd; do
    case "$(norm_path "$cmd")" in *"$needle"*) : ;; *) printf '%s\t%s\n' "$pid" "$cmd" ;; esac
  done
}

print_proc_list() {
  printf '%s\n' "$1" | while IFS="$(printf '\t')" read -r pid cmd; do
    [ -n "$pid" ] || continue
    note "pid=$pid $cmd"
  done
}

owner_from_cmd() {
  local c="$1" rest
  case "$c" in *--user-data-dir=*) : ;; *) printf ''; return ;; esac
  rest="${c#*--user-data-dir=}"
  case "$rest" in
    \"*) rest="${rest#\"}"; printf '%s' "${rest%%\"*}" ;;
    *) printf '%s' "${rest%% *}" ;;
  esac
}

chrome_owner_profile() {
  local port="$1" tmp cmd
  tmp="${TMPDIR:-/tmp}/.aqd_chrome_line.$$"
  ps -Ao pid=,command= 2>/dev/null | while IFS= read -r line; do
    case "$line" in *"--remote-debugging-port=$port"*) : ;; *) continue ;; esac
    case "$line" in *Chrome*|*chrome*) printf '%s\n' "$line" ;; esac
  done | head -n 1 >"$tmp" 2>/dev/null
  cmd="$(cat "$tmp" 2>/dev/null)"
  rm -f "$tmp" 2>/dev/null || true
  owner_from_cmd "$cmd"
}

decide_chrome() {
  if [ "$1" = no ]; then printf 'free'; return; fi
  if [ -n "$2" ] && [ "$(norm_path "$2")" = "$(norm_path "$3")" ]; then printf 'reuse'; else printf 'foreign'; fi
}

cdp_up() { if [ "$DRY_RUN" = 1 ]; then return 1; fi; curl -s --max-time 3 "http://127.0.0.1:$1/json/version" >/dev/null 2>&1; }

resolve_cdp_port() {
  local myprof="$1/chrome-profile" p="$2" end up owner d
  end=$(($2 + 50))
  while [ "$p" -lt "$end" ]; do
    up=no
    if cdp_up "$p"; then up=yes; fi
    owner=""
    if [ "$up" = yes ]; then owner="$(chrome_owner_profile "$p")"; fi
    d="$(decide_chrome "$up" "$owner" "$myprof")"
    case "$d" in
      reuse) ok "CDP $p is open with this root's profile - reusing it"; CDP_PORT_OUT="$p"; return ;;
      free) CDP_PORT_OUT="$p"; return ;;
      foreign) warn "CDP $p belongs to another root's Chrome (profile '$owner') - not reusing it, trying the next port" ;;
    esac
    p=$((p + 1))
  done
  warn "no free CDP port near $2 - falling back to $2"
  CDP_PORT_OUT="$2"
}

parity_overlap_warnings() {
  local norm digits d hit par name pd
  norm="$(printf '%s' "$1" | tr ',' ' ' | tr -s ' ' ' ' | sed -e 's/^ *//' -e 's/ *$//')"
  [ -n "$norm" ] || return 0
  digits=""
  for d in $norm; do case "$d" in [0-9]) digits="$digits $d" ;; esac; done
  [ -n "$digits" ] || return 0
  digits="$(printf '%s\n' $digits | sort -n -u | tr '\n' ' ' | sed -e 's/ *$//')"
  for par in even:0,2,4,6,8 odd:1,3,5,7,9; do
    name="${par%%:*}"
    pd="${par#*:}"
    hit=""
    for d in $digits; do
      case ",$pd," in *",$d,"*) hit="$hit $d" ;; esac
    done
    [ -n "$hit" ] || continue
    hit="$(printf '%s' "$hit" | sed -e 's/^ *//' | tr ' ' ',')"
    if [ "$(printf '%s' "$hit" | tr -cd ',' | wc -c | tr -d ' ')" = 0 ]; then
      printf 'suffix %s is INSIDE parity=%s (%s) - if another machine runs parity=%s you will both fetch id%%10=%s. Set the other machine to a disjoint set.\n' "$hit" "$name" "$pd" "$name" "$hit"
    else
      printf 'suffixes %s are INSIDE parity=%s (%s) - if another machine runs parity=%s you will both fetch id%%10=%s. Set the other machine to a disjoint set.\n' "$hit" "$name" "$pd" "$name" "$hit"
    fi
  done
}

read_cred_value() {
  local path="$1" key="$2" line k v
  [ -f "$path" ] || { printf ''; return; }
  while IFS= read -r line || [ -n "$line" ]; do
    line="$(printf '%s' "$line" | sed -e 's/^ *//' -e 's/ *$//')"
    [ -z "$line" ] && continue
    case "$line" in \#*) continue ;; esac
    case "$line" in *=*) : ;; *) continue ;; esac
    k="$(printf '%s' "${line%%=*}" | sed -e 's/^ *//' -e 's/ *$//')"
    v="${line#*=}"
    if [ "$k" = "$key" ]; then
      printf '%s' "$v" | sed -e 's/^ *//' -e 's/ *$//' -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
      return
    fi
  done <"$path"
  printf ''
}

count_cred_keys() {
  local path="$R/creds.env" n=0 line
  [ -f "$path" ] || { printf 0; return; }
  while IFS= read -r line || [ -n "$line" ]; do
    line="$(printf '%s' "$line" | sed -e 's/^ *//' -e 's/ *$//')"
    [ -z "$line" ] && continue
    case "$line" in \#*) continue ;; esac
    case "$line" in *=) continue ;; *=*) : ;; *) continue ;; esac
    n=$((n + 1))
  done <"$path"
  printf '%s' "$n"
}

show_new_lines() {
  local f="$1" keep="${2:-4}" total n start
  [ -f "$f" ] || return 0
  total="$(wc -l <"$f" 2>/dev/null | tr -d ' ')"
  [ -n "$total" ] || return 0
  [ "$total" -le "$LOG_TAIL_FROM" ] && return 0
  n=$((total - LOG_TAIL_FROM))
  if [ "$n" -gt "$keep" ]; then say "  | ... $((n - keep)) more line(s)"; fi
  start=$((total - keep + 1))
  [ "$start" -lt 1 ] && start=1
  [ "$start" -le "$LOG_TAIL_FROM" ] && start=$((LOG_TAIL_FROM + 1))
  sed -n "${start},${total}p" "$f" | while IFS= read -r l; do say "  | $l"; done
  LOG_TAIL_FROM=$total
}

run_streamed() {
  local label="$1" log="$2" pid rc t0 now beat
  shift 2
  if [ "$DRY_RUN" = 1 ]; then say "  [dry-run] $label: $* (log $log)"; return 0; fi
  : >"$log" 2>/dev/null || true
  LOG_TAIL_FROM=0
  note "$label - live log: $log"
  "$@" >"$log" 2>&1 &
  pid=$!
  t0=$(date +%s)
  beat=$t0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 2
    show_new_lines "$log" 4
    now=$(date +%s)
    if [ $((now - beat)) -ge 15 ]; then
      beat=$now
      note "$label still running after $(fmt_secs $((now - t0))) - it is NOT frozen, full log: $log"
    fi
  done
  wait "$pid"
  rc=$?
  show_new_lines "$log" 4
  return $rc
}

run_bounded() {
  local secs="$1" log="$2" pid i=0
  shift 2
  "$@" >"$log" 2>&1 &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$i" -ge "$secs" ]; then
      kill -9 "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      return 124
    fi
    sleep 1
    i=$((i + 1))
  done
  wait "$pid"
  return $?
}

find_python() {
  local c path cands="" best="" best_rank=9 rank v
  for c in python3.12 python3.11 python3.10 python3.13 python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if command -v "$c" >/dev/null 2>&1; then
      path="$(command -v "$c")"
      case "$cands" in *" $path "*) continue ;; esac
      cands="$cands $path "
    fi
  done
  for path in $cands; do
    v="$("$path" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)"
    [ -n "$v" ] || continue
    case "$v" in 3.*) : ;; *) continue ;; esac
    rank=9
    case "$v" in
      3.12.*) rank=0 ;;
      3.11.*) rank=1 ;;
      3.10.*) rank=2 ;;
      3.13.*) rank=3 ;;
      3.14.*) rank=4 ;;
      3.9.*) rank=5; warn "python $v at $path is older than 3.10 - accepted as a last resort (macOS system python; the payload parses on 3.9)" ;;
    esac
    if [ "$rank" = 9 ]; then
      warn "ignoring $path ($v): this payload needs Python 3.10 or newer"
      continue
    fi
    note "python candidate: $v at $path"
    if [ "$rank" = 0 ]; then PY_BASE="$path"; PY_VER="$v"; return 0; fi
    if [ "$rank" -lt "$best_rank" ]; then best="$path"; best_rank="$rank"; PY_VER="$v"; fi
  done
  if [ -n "$best" ]; then PY_BASE="$best"; return 0; fi
  PY_BASE=""
  return 1
}

ensure_venv() {
  if [ -x "$R/venv/bin/python" ]; then
    ok "reusing the existing virtualenv: $R/venv"
    return
  fi
  note "creating a virtualenv at $R/venv (from python $PY_VER)"
  if act "$PY_BASE -m venv $R/venv"; then
    "$PY_BASE" -m venv "$R/venv" || die "python -m venv failed (is ensurepip available for this python?)"
  fi
}

probe_torch_wheel() {
  local log txt
  log="$WORK/torch-probe.log"
  run_bounded 120 "$log" "$PY" -m pip index versions torch --disable-pip-version-check --timeout 60 --retries 2
  txt="$(cat "$log" 2>/dev/null)"
  case "$txt" in *"No matching distribution found"*|*"no matching distribution found"*) printf 'no'; return 0 ;; esac
  case "$txt" in *LATEST:*|*"Available versions"*) printf 'yes'; return 0 ;; esac
  printf 'unknown'
}

get_source() {
  local dir zip url
  SRC=""
  if [ -n "$SRC_DIR" ]; then
    [ -d "$SRC_DIR/common" ] || die "AQD_SRC_DIR=$SRC_DIR has no common/ (not a payload checkout)"
    note "using the local checkout: $SRC_DIR"
    SRC="$SRC_DIR"
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    note "dry run reads the payload into a temp dir (read-only) so the plan can compare this Mac's alt.py with the repo's"
  fi
  if command -v git >/dev/null 2>&1; then
    dir="$WORK/repo"
    note "git clone --depth 1 --branch $REF $REPO_GIT"
    if git clone --quiet --depth 1 --branch "$REF" "$REPO_GIT" "$dir" >"$WORK/git.log" 2>&1 && [ -d "$dir/common" ]; then
      SRC="$dir"
      return 0
    fi
    warn "git clone failed; falling back to the codeload zip"
    show_new_lines "$WORK/git.log" 5
  else
    note "no git; using the codeload zip"
  fi
  zip="$WORK/src.zip"
  if [ "$REF" = main ]; then url="$REPO_ZIP"; else url="https://codeload.github.com/${REPO}/zip/refs/heads/${REF}"; fi
  note "codeload zip: $url"
  curl -fsSL "$url" -o "$zip" || die "could not download $url"
  mkdir -p "$WORK/unzip"
  if command -v unzip >/dev/null 2>&1; then
    unzip -q "$zip" -d "$WORK/unzip" || die "unzip failed"
  else
    tar -xf "$zip" -C "$WORK/unzip" || die "zip extract failed"
  fi
  dir="$(find "$WORK/unzip" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | head -n 1)"
  [ -n "$dir" ] || die "the zip has no top folder"
  SRC="$dir"
}

payload_py_names() {
  local src="$1"
  {
    for f in "$src"/common/*.py; do [ -f "$f" ] && basename "$f"; done
    [ -f "$src/mac/alt.py" ] && printf 'alt.py\n'
  } | sort -u
}

pre_install_patch_check() {
  local f p sum lines any=0 sum_mac sum_repo lines_mac lines_repo ref_alt="$SRC/mac/alt.py" oldq
  hdr "6a/8 Before overwriting: the patched code on this Mac"
  mkdir_p "$BACKUP_DIR/preinstall_py"
  for f in alt.py safe_run.py; do
    p="$R/$f"
    [ -f "$p" ] || continue
    any=1
    sum="$(file_sha256 "$p")"
    lines="$(file_lines "$p")"
    note "existing $f: sha256=$sum lines=$lines"
    cp_f "$p" "$BACKUP_DIR/preinstall_py/$f"
  done
  if [ "$any" = 0 ]; then
    note "no existing alt.py / safe_run.py at $R - nothing to compare or preserve"
  else
    done_ok "backed up the existing alt.py / safe_run.py -> $BACKUP_DIR/preinstall_py/ (keep the sha256 above to compare)"
  fi
  if [ -f "$R/alt.py" ] && [ -f "$ref_alt" ]; then
    if cmp -s "$R/alt.py" "$ref_alt"; then
      ok "this Mac's alt.py is IDENTICAL to the repo's mac/alt.py"
    else
      sum_mac="$(file_sha256 "$R/alt.py")"
      lines_mac="$(file_lines "$R/alt.py")"
      sum_repo="$(file_sha256 "$ref_alt")"
      lines_repo="$(file_lines "$ref_alt")"
      warn "this Mac's alt.py DIFFERS from the repo's mac/alt.py - installing the repo version OVERWRITES a different build"
      note "  this Mac : $R/alt.py   sha256=$sum_mac lines=$lines_mac"
      note "  repo     : mac/alt.py   sha256=$sum_repo lines=$lines_repo"
      note "  this Mac's copy is kept at $BACKUP_DIR/preinstall_py/alt.py"
      note "  see what differs before deciding:  diff -u $BACKUP_DIR/preinstall_py/alt.py $R/alt.py"
      if ! confirm "Overwrite this Mac's alt.py with the repo version?" "$(if [ "$YES" = 1 ]; then printf yes; else printf no; fi)"; then
        bad "stopped on purpose: this Mac's alt.py was NOT overwritten."
        note "backup of the current alt.py: $BACKUP_DIR/preinstall_py/alt.py"
        note "merge anything you need from it into the repo, then run the same single command again."
        exit 3
      fi
      warn "overwriting this Mac's alt.py with the repo version (backup kept above)"
    fi
  fi
  for oldq in queue6.json queue8.json queue6.json.state.json queue8.json.state.json alt_step.txt; do
    if [ -e "$R/$oldq" ]; then
      warn "old generation marker found: $oldq (the new generation uses queue_all_unnamed.json / queue_ids_*.json)"
      cp_f "$R/$oldq" "$BACKUP_DIR/old_queue/$oldq"
      rm_f "$R/$oldq"
      note "backed up $oldq -> $BACKUP_DIR/old_queue/ and removed it so it cannot shadow the new queue files"
    fi
  done
}

config_json_value() {
  local f="$R/aqd_config.json"
  [ -f "$f" ] || return 0
  sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" "$f" | head -1
}

root_procs_loose() {
  # this root's alt/safe_run/menu_worker + a menu_worker started with a relative path or from an old python
  { aqd_procs_in_root "$R"; list_aqd_procs | while IFS="$(printf '\t')" read -r pid cmd; do
      case "$cmd" in *"$R"/menu_worker.py*|*" menu_worker.py"*) printf '%s\t%s\n' "$pid" "$cmd" ;; esac
    done; } | sort -u
}

stop_graceful() {
  local left i=0 n
  left="$(root_procs_loose)"
  if [ -z "$left" ]; then note "nothing running under $R"; return; fi
  print_proc_list "$left"
  touch_f "$R/STOP_ALL"
  ok "created STOP_ALL - waiting up to 10 min for alt.py/safe_run.py/menu_worker.py to stop at a place boundary"
  if [ "$DRY_RUN" = 1 ]; then return; fi
  while [ "$i" -lt 600 ]; do
    [ -z "$(root_procs_loose)" ] && { ok "stopped cleanly after ~${i}s"; break; }
    sleep 5; i=$((i + 5))
  done
  for n in aqd.alt aqd.menu; do launchctl bootout "gui/$(id -u)/$n" 2>/dev/null || launchctl remove "$n" 2>/dev/null || true; done
  left="$(root_procs_loose)"
  if [ -n "$left" ]; then
    warn "still running after 10 min - stopping them (done markers keep progress)"
    printf '%s\n' "$left" | while IFS="$(printf '\t')" read -r p cmd; do [ -n "$p" ] && kill_pid "$p"; done
  fi
}

ask_suffixes() {
  say ""
  note "ID suffixes = the LAST DIGIT of the id this machine handles; keep the set DISJOINT from every other machine."
  note "Examples: '1 3 5 7' or '2 4'. Also accepts 'even' or 'odd' to fall back to the old AQD_PARITY (the Mac default is odd)."
  note "This one is REQUIRED: there is no safe default, because two machines on the same digits duplicate the work."
  local raw out bad p saved
  if [ "$YES" = 1 ] && [ -z "${AQD_ID_SUFFIXES:-}" ]; then
    saved="$(config_json_value id_suffixes)"
    if [ -n "$saved" ]; then note "AQD_ID_SUFFIXES = $saved (saved aqd_config.json)"; SUFFIXES_OUT="$saved"; PARITY_OUT=""; return; fi
    saved="$(config_json_value parity)"
    if [ -n "$saved" ]; then note "AQD_PARITY = $saved (saved aqd_config.json)"; SUFFIXES_OUT=""; PARITY_OUT="$saved"; return; fi
    PARITY_OUT="${AQD_PARITY:-odd}"; SUFFIXES_OUT=""
    note "--yes and no saved split: AQD_PARITY = $PARITY_OUT (Mac default odd; Windows default even)"
    return
  fi
  while :; do
    ask "AQD_ID_SUFFIXES" "" required
    raw="$(printf '%s' "$ANS" | tr ',' ' ' | tr -s ' ' ' ' | sed -e 's/^ *//' -e 's/ *$//')"
    case "$raw" in
      [Ee][Vv][Ee][Nn]) SUFFIXES_OUT=""; PARITY_OUT="even"; return ;;
      [Oo][Dd][Dd]) SUFFIXES_OUT=""; PARITY_OUT="odd"; return ;;
    esac
    if [ -z "$raw" ]; then
      warn "AQD_ID_SUFFIXES is required - type digits 0-9 (e.g. '1 3 5 7'), or 'even'/'odd'"
      continue
    fi
    out=""
    bad=""
    for p in $raw; do
      case "$p" in
        [0-9]) out="$out $p" ;;
        *) bad="$bad $p" ;;
      esac
    done
    if [ -n "$(printf '%s' "$bad" | tr -d ' ')" ]; then
      warn "invalid:$bad - only single digits 0-9 are accepted"
      continue
    fi
    SUFFIXES_OUT="$(printf '%s\n' $out | sort -n -u | tr '\n' ' ' | sed -e 's/ *$//')"
    PARITY_OUT=""
    return
  done
}

ask_list_options() {
  say ""
  note "First start lists unnamed drafts from the server (1 request every 3s) BEFORE the first place is fetched."
  note "AQD_LIST_MAX_PAGES = pages listed per round: 20 pages = under a minute, then fetching starts."
  note "Press Enter for 20. Type 0 for the old behaviour (list the whole list first, about 12 minutes)."
  while :; do
    ask "AQD_LIST_MAX_PAGES" "$DEFAULT_LIST_PAGES"
    if is_digits "$ANS"; then
      if is_zeros "$ANS"; then LIST_PAGES_OUT="0"; return; fi
      LIST_PAGES_OUT="$ANS"
      return
    fi
    warn "AQD_LIST_MAX_PAGES must be a whole number of pages (0 = list everything first)"
  done
}

ask_fetch_limit() {
  say ""
  note "AQD_FETCH_LIMIT = how many places this machine fetches per round (Enter = 0 = no limit)."
  note "A weak or test machine can use e.g. 10 to do a few places, then list again."
  while :; do
    ask "AQD_FETCH_LIMIT" "$DEFAULT_FETCH_LIMIT"
    if is_digits "$ANS"; then FETCH_LIMIT_OUT="$ANS"; return; fi
    warn "AQD_FETCH_LIMIT must be a whole number of places (0 = no limit)"
  done
}

write_creds() {
  local path="$R/creds.env" existing_pw email pw base
  existing_pw="$(read_cred_value "$path" AQD_ADMIN_PASSWORD)"
  if [ -f "$path" ]; then
    note "creds.env already exists"
    if ! confirm "Overwrite creds.env with new values?" no; then
      note "keeping the existing creds.env"
      return
    fi
  fi
  say ""
  note "One prompt per environment variable; values go straight into $path (gitignored)."
  note "AQD_ADMIN_EMAIL is REQUIRED (no default). AQD_ADMIN_PASSWORD: Enter keeps the existing value when there is one."
  email=""
  while [ -z "$email" ]; do
    ask "AQD_ADMIN_EMAIL" "" required
    email="$(printf '%s' "$ANS" | sed -e 's/^ *//' -e 's/ *$//')"
    if [ -z "$email" ]; then
      warn "AQD_ADMIN_EMAIL is required - the admin API login needs it"
      continue
    fi
    case "$email" in *@*) : ;; *) warn "AQD_ADMIN_EMAIL does not look like an email address"; email="" ;; esac
  done
  ask_secret "AQD_ADMIN_PASSWORD (hidden while typing)" "$existing_pw"
  pw="$ANS"
  [ -n "$pw" ] || die "AQD_ADMIN_PASSWORD must not be empty"
  ask "AQD_BASE" "$DEFAULT_BASE"
  base="$ANS"
  write_file "$path" "AQD_ADMIN_EMAIL=$email
AQD_ADMIN_PASSWORD=$pw
AQD_BASE=$base
"
  done_ok "wrote creds.env (AQD_ADMIN_EMAIL, AQD_ADMIN_PASSWORD, AQD_BASE - the password is never printed)"
}

show_report() {
  local data="$R/aqd_data" dirs=0 done_=0 prep=0 lg kb cp
  if [ -d "$data" ]; then
    dirs="$(find "$data" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"
    done_="$(find "$data" -name SAFE_DONE -type f 2>/dev/null | wc -l | tr -d ' ')"
    prep="$(find "$data" -name SAFE_PREP -type f 2>/dev/null | wc -l | tr -d ' ')"
  fi
  note "aqd_data: $dirs places | SAFE_DONE: $done_ | SAFE_PREP: $prep"
  lg="$R/safe_run_log.md"
  if [ -f "$lg" ]; then note "safe_run_log.md: $(wc -l <"$lg" | tr -d ' ') lines"; else note "safe_run_log.md: none"; fi
  if [ -f "$R/fetch_backoff.txt" ]; then note "fetch_backoff.txt: PRESENT (backing off)"; else note "fetch_backoff.txt: none"; fi
  cp="$R/chrome-profile"
  if [ -d "$cp" ]; then
    kb="$(du -sk "$cp" 2>/dev/null | awk '{print $1}')"
    [ -n "$kb" ] || kb=0
    if [ "$kb" -ge 1024 ]; then note "chrome-profile: $((kb / 1024)) MB (Google login session - losing it means logging in again)"
    else note "chrome-profile: ${kb} KB (Google login session - losing it means logging in again)"; fi
  else
    note "chrome-profile: none"
  fi
  if [ -f "$R/creds.env" ]; then note "creds.env: present"; else note "creds.env: none"; fi
  note "queue*.json: $(ls -1 "$R"/queue*.json 2>/dev/null | wc -l | tr -d ' ') file(s)"
}

backup_state() {
  local d
  mkdir_p "$BACKUP_DIR"
  if [ -f "$R/creds.env" ]; then cp_f "$R/creds.env" "$BACKUP_DIR/creds.env"; done_ok "backed up creds.env"; fi
  for d in chrome-profile aqd_data; do
    [ -d "$R/$d" ] || continue
    copy_dir_into "$R/$d" "$BACKUP_DIR"
    done_ok "backed up $d -> $BACKUP_DIR/$d"
  done
}

stop_old() {
  local other left n i p cmd
  other="$(aqd_procs_other_root "$R")"
  if [ -n "$other" ]; then
    warn "another install is running and will NOT be touched (its processes are not under $R):"
    print_proc_list "$other"
  fi
  touch_f "$R/STOP_ALL"
  touch_f "$R/STOP"
  ok "created STOP_ALL + STOP in $R - waiting up to 60s for its alt.py/safe_run.py to stop at a place boundary"
  if [ "$DRY_RUN" = 1 ]; then
    note "[dry-run] would wait up to 60s for the $R processes to exit, then list what is left"
    return
  fi
  i=0
  while [ "$i" -lt 60 ]; do
    if [ -z "$(aqd_procs_in_root "$R")" ]; then ok "no $R aqd process left"; return; fi
    sleep 1
    i=$((i + 1))
  done
  left="$(aqd_procs_in_root "$R")"
  if [ -z "$left" ]; then ok "exited cleanly"; return; fi
  n="$(count_lines "$left")"
  warn "$n process(es) under $R still running after 60s:"
  print_proc_list "$left"
  if confirm "Kill -9 the $n process(es) under $R (and only those)?" no; then
    printf '%s\n' "$left" | while IFS="$(printf '\t')" read -r p cmd; do
      [ -n "$p" ] && kill_pid "$p"
    done
    sleep 2
    ok "killed; left under $R: $(count_lines "$(aqd_procs_in_root "$R")")"
  else
    warn "old processes under $R left alone - the reinstall may fight over files"
  fi
}

clear_state() {
  local remove_profile="$1" f q
  for f in STOP_ALL STOP STOP_MENU parity_tries.json fetch_backoff.txt alt.out alt.nohup safe_run_log.md aqd_progress.md aqd_api_log.md start_fetcher.log menu_worker.lock menu_worker.out menu_worker.err photos_backoff.txt; do
    if [ -e "$R/$f" ]; then rm_f "$R/$f"; note "deleted $f"; fi
  done
  for q in "$R"/queue*.json; do
    [ -e "$q" ] || continue
    rm_f "$q"
    note "deleted $(basename "$q")"
  done
  if [ -d "$R/__pycache__" ]; then rm_rf "$R/__pycache__"; note "deleted __pycache__/"; fi
  if [ -d "$R/.aqd_session" ]; then rm_rf "$R/.aqd_session"; note "deleted .aqd_session/"; fi
  if [ "$remove_profile" = yes ]; then
    if [ -d "$R/chrome-profile" ]; then rm_rf "$R/chrome-profile"; warn "deleted chrome-profile/ - you MUST log in to Google again"; fi
  else
    note "kept chrome-profile/"
  fi
  note "kept creds.env and aqd_data/"
}

clear_stale_py() {
  local keep="$1" bdir="$2" f base stale=""
  if [ -z "$keep" ]; then warn "no payload .py list was given - refusing to delete any .py"; return; fi
  for f in "$R"/*.py; do
    [ -e "$f" ] || continue
    base="$(basename "$f")"
    if printf '%s\n' "$keep" | grep -Fxq "$base"; then continue; fi
    stale="$stale $base"
  done
  if [ -z "$stale" ] && [ ! -d "$R/__pycache__" ]; then
    note "no stray .py at the root and no __pycache__ - nothing to clean"
    return
  fi
  if [ -n "$stale" ]; then
    note "stray .py at the root (not in the new payload): $(printf '%s' "$stale" | sed -e 's/^ *//' | tr ' ' ',')"
    if [ -n "$bdir" ]; then
      mkdir_p "$bdir"
      for base in $stale; do cp_f "$R/$base" "$bdir/$base"; done
      done_ok "backed up $(count_words "$stale") stray .py -> $bdir"
    else
      warn "no backup dir given - the stray .py are deleted without a copy"
    fi
    for base in $stale; do
      rm_f "$R/$base"
      if [ "$DRY_RUN" = 0 ] && [ -e "$R/$base" ]; then warn "could not delete $base (in use?)"; else note "deleted $base"; fi
    done
  fi
  if [ -d "$R/__pycache__" ]; then rm_rf "$R/__pycache__"; note "deleted __pycache__/"; fi
}

install_flat() {
  local n=0 f
  mkdir_p "$R"
  for f in "$SRC"/common/*; do
    [ -f "$f" ] || continue
    cp_f "$f" "$R/$(basename "$f")"
    n=$((n + 1))
  done
  if [ -f "$SRC/mac/alt.py" ]; then
    cp_f "$SRC/mac/alt.py" "$R/alt.py"
    n=$((n + 1))
  else
    warn "missing mac/alt.py in the payload"
  fi
  if [ -f "$SRC/common/photos_new.py" ]; then
    cp_f "$SRC/common/photos_new.py" "$R/photos.py"
    n=$((n + 1))
    note "photos.py: the shared common/photos.py hardcodes C:/aqd, so on macOS the AQD_ROOT-aware photos_new.py is installed as photos.py"
  fi
  done_ok "copied $n files into $R (FLAT layout: every .py sits directly in $R)"
}

render_plist() {
  local tpl="$1" dst="$2" out
  out="$(read_file "$tpl")"
  out="$(printf '%s' "$out" | sed \
    -e "s|__AQD_ROOT__|$(sed_val "$(xml_escape "$R")")|g" \
    -e "s|__AQD_PY__|$(sed_val "$(xml_escape "$PY")")|g" \
    -e "s|__AQD_CHROME__|$(sed_val "$(xml_escape "$CHROME_BIN")")|g" \
    -e "s|__AQD_ID_SUFFIXES__|$(sed_val "$(xml_escape "$SUFFIXES_OUT")")|g" \
    -e "s|__AQD_PARITY__|$(sed_val "$(xml_escape "$PARITY_OUT")")|g" \
    -e "s|__AQD_LIST_MAX_PAGES__|$(sed_val "$(xml_escape "$LIST_PAGES_OUT")")|g" \
    -e "s|__AQD_FETCH_LIMIT__|$(sed_val "$(xml_escape "$FETCH_LIMIT_OUT")")|g" \
    -e "s|__AQD_CDP_PORT__|$(sed_val "$(xml_escape "$CDP_PORT_OUT")")|g")"
  write_file "$dst" "$out"
}

write_plists() {
  local out="$R/launchd" la="$HOME/Library/LaunchAgents" n
  mkdir_p "$out"
  mkdir_p "$la"
  for n in aqd.alt aqd.chrome aqd.menu; do
    if [ -f "$SRC/mac/launchd/$n.plist" ]; then
      render_plist "$SRC/mac/launchd/$n.plist" "$out/$n.plist"
      cp_f "$out/$n.plist" "$la/$n.plist"
      done_ok "installed $la/$n.plist (EnvironmentVariables AQD_ROOT/AQD_ID_SUFFIXES/AQD_PARITY/AQD_LIST_MAX_PAGES/AQD_FETCH_LIMIT/AQD_CDP_PORT)"
    else
      warn "missing mac/launchd/$n.plist in the payload"
    fi
  done
}

launchd_command() { command -v launchctl >/dev/null 2>&1; }

launchd_plist_is_ours() {
  local f="$1"
  [ -f "$f" ] || return 2
  case "$(cat "$f" 2>/dev/null)" in *"$R"*) return 0 ;; *) return 1 ;; esac
}

launchd_bootout() {
  local label="$1" plist="$HOME/Library/LaunchAgents/$1.plist" state
  launchd_plist_is_ours "$plist"
  state=$?
  if [ "$state" = 1 ]; then
    warn "launchd label $label already points at a DIFFERENT root (not $R) - leaving it alone"
    return
  fi
  if act "launchctl bootout gui/$(id -u)/$label"; then
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || launchctl remove "$label" 2>/dev/null || true
  fi
}

launchd_bootstrap() {
  local label="$1"
  local plist="$HOME/Library/LaunchAgents/$label.plist"
  [ -f "$plist" ] || { warn "no $plist to bootstrap"; return; }
  if act "launchctl bootstrap gui/$(id -u) $plist"; then
    launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null || launchctl load -w "$plist" 2>/dev/null || warn "launchctl bootstrap failed for $label"
  fi
}

startup_install() {
  local labels="" l
  write_plists
  if [ "$NO_CHROME" = 0 ]; then labels="$labels aqd.chrome"; fi
  if [ "$NO_START" = 0 ]; then labels="$labels aqd.alt aqd.menu"; fi
  if ! launchd_command; then
    warn "launchctl not found - the plists were written but not loaded; load them yourself or run alt.py by hand"
    return
  fi
  for l in $labels; do
    launchd_bootout "$l"
    launchd_bootstrap "$l"
  done
  if [ "$NO_START" = 0 ]; then ok "launchd aqd.alt bootstrapped (it sleeps 45s, then starts alt.py)"; fi
}

find_chrome() {
  local c
  for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" "$HOME/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"; do
    if [ -x "$c" ]; then printf '%s' "$c"; return; fi
  done
  printf ''
}

wait_for_cdp() {
  local port="$1" i=0
  if [ "$DRY_RUN" = 1 ]; then return; fi
  while [ "$i" -lt 30 ]; do
    if cdp_up "$port"; then return; fi
    sleep 2
    i=$((i + 1))
  done
}

start_chrome() {
  local port="$1" myprof="$R/chrome-profile" owner
  if [ "$DRY_RUN" = 0 ] && cdp_up "$port"; then
    owner="$(chrome_owner_profile "$port")"
    case "$(decide_chrome yes "$owner" "$myprof")" in
      reuse) ok "Chrome CDP $port already open with this root's profile - reusing it, not opening another"; return ;;
      *) warn "Chrome CDP $port is open with profile '$owner', NOT this root's '$myprof' - refusing to reuse it"; return ;;
    esac
  fi
  if [ -z "$CHROME_BIN" ]; then
    warn "Google Chrome not found - open it yourself with --remote-debugging-port=$port --user-data-dir=$myprof"
    return
  fi
  note "opening Chrome (CDP $port, profile $myprof)"
  if act "start \"$CHROME_BIN\" --remote-debugging-port=$port --user-data-dir=$myprof"; then
    nohup "$CHROME_BIN" --no-first-run --remote-debugging-address=127.0.0.1 "--remote-debugging-port=$port" "--user-data-dir=$myprof" --restore-last-session >/dev/null 2>&1 &
  fi
  wait_for_cdp "$port"
  if [ "$DRY_RUN" = 1 ]; then return; fi
  if cdp_up "$port"; then ok "Chrome CDP $port is up"; else warn "Chrome CDP $port not up after 60s"; fi
}

start_alt() {
  if [ "$NO_START" = 1 ]; then warn "alt.py not started (--no-start)"; return; fi
  if [ -n "$(aqd_procs_in_root "$R")" ]; then
    warn "alt.py/safe_run.py for $R already running - not starting another"
    return
  fi
  note "starting alt.py (log: $R/alt.out , startup errors: $R/alt.nohup)"
  if act "start $R/venv/bin/python $R/alt.py (root=$R)"; then
    (
      cd "$R" || exit 1
      nohup env AQD_ROOT="$R" AQD_ID_SUFFIXES="$SUFFIXES_OUT" AQD_PARITY="$PARITY_OUT" \
        AQD_LIST_MAX_PAGES="$LIST_PAGES_OUT" AQD_FETCH_LIMIT="$FETCH_LIMIT_OUT" AQD_CDP_PORT="$CDP_PORT_OUT" \
        "$PY" "$R/alt.py" >"$R/alt.nohup" 2>&1 &
    )
  fi
  done_ok "started alt.py (log: $R/alt.out)"
}

install_deps() {
  local pip_log="$R/pip-required.log" rc wheel gname glog gpkgs gmb gwhat
  hdr "7/8 Install dependencies"
  note "pip network timeout 60s, 5 retries (a stalled download gives up with a clear error instead of hanging)"
  note "output streams live below, and the full log stays in $R/pip-*.log"
  ok "required: $REQUIRED_PKGS"
  run_streamed "pip required ($REQUIRED_PKGS)" "$pip_log" "$PY" -m pip install --disable-pip-version-check --no-input --timeout 60 --retries 5 $REQUIRED_PKGS
  rc=$?
  if [ "$rc" = 0 ]; then
    ok "required packages installed (log: $pip_log)"
  else
    warn "pip rc=$rc - see $pip_log"
    show_new_lines "$pip_log" 12
  fi

  if [ "$SKIP_HEAVY" = 1 ]; then
    note "heavy groups skipped (--skip-heavy): CLIP/OCR filtering stays weak, everything else works"
    return
  fi
  if [ "$DRY_RUN" = 1 ]; then
    note "[dry-run] would probe the torch wheel and offer CLIP (~156 MB) / OCR (~242 MB), both default No"
    return
  fi
  note "checking whether torch has a wheel for this Python (pip index versions torch, max 2 min)"
  wheel="$(probe_torch_wheel)"
  case "$wheel" in
    yes) ok "torch has a wheel for Python $PY_VER - no source build needed" ;;
    no) warn "torch has NO wheel for Python $PY_VER: pip would build it from source, which can take hours"
        if ! confirm "Install the heavy groups anyway?" no; then
          warn "heavy groups skipped - install Python 3.12 (brew install python@3.12) and re-run to get them"
          return
        fi ;;
    *) warn "could not tell whether torch has a wheel for Python $PY_VER - continuing" ;;
  esac
  note "The heavy groups download a few hundred MB and can take 10-30 minutes on a slow line."
  note "CLIP is about 156 MB (torch + open_clip_torch); OCR is about 242 MB (~124 MB more once CLIP is in)."
  note "Both default to NO: press Enter to skip. Do NOT close this window; progress prints every 15s."
  local gspec
  for gspec in \
    "CLIP|torch open_clip_torch|156|filters the photo gallery: keeps food/drink/menu photos, drops the rest" \
    "OCR|easyocr|242|reads menu photos and shop signs (adds about 124 MB once CLIP is in: torch is shared)"; do
    IFS='|' read -r gname gpkgs gmb gwhat <<<"$gspec"
    if ! confirm "Install $gname? ~$gmb MB, 5-20 min. $gwhat" "$(if [ "$YES" = 1 ]; then printf yes; else printf no; fi)"; then
      note "skipped $gname (Enter = default no)"
      continue
    fi
    glog="$R/pip-heavy-${gpkgs%% *}.log"
    run_streamed "pip $gname ($gpkgs)" "$glog" "$PY" -m pip install --disable-pip-version-check --no-input --timeout 60 --retries 5 $gpkgs
    if [ "$?" = 0 ]; then ok "$gname installed"; else warn "$gname pip failed - still runs, only this filtering is weaker (log: $glog)"; fi
  done
}

main() {
  local arg existed=0 payload w use_launchd=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --root) CLI_ROOT="${2:-}"; shift 2 ;;
      --root=*) CLI_ROOT="${1#*=}"; shift ;;
      --clean|-Clean) CLEAN=1; shift ;;
      --no-start|-NoStart) NO_START=1; shift ;;
      --no-chrome|-NoChrome) NO_CHROME=1; shift ;;
      --skip-heavy|-SkipHeavy) SKIP_HEAVY=1; shift ;;
      --yes|-Yes) YES=1; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      -h|--help) usage; exit 0 ;;
      "") shift ;;
      *) warn "unknown argument: $1 (see --help)"; shift ;;
    esac
  done

  if [ "$CLEAN" = 0 ] && [ -n "${AQD_CLEAN:-}" ] && env_truthy "$AQD_CLEAN"; then CLEAN=1; fi
  if [ "$DRY_RUN" = 0 ] && [ -n "${AQD_DRY_RUN:-}" ] && env_truthy "$AQD_DRY_RUN"; then DRY_RUN=1; fi
  if [ "$YES" = 0 ] && [ -n "${AQD_YES:-}" ] && env_truthy "$AQD_YES"; then YES=1; fi

  if [ -n "$CLI_ROOT" ]; then R="$CLI_ROOT"
  elif [ -n "${AQD_ROOT:-}" ]; then R="$AQD_ROOT"
  else R="$HOME/aqd"; fi
  case "$R" in /*) : ;; *) die "AQD_ROOT must be an absolute path (got '$R')" ;; esac
  R="$(clean_path "$R")"
  [ -n "$R" ] || die "AQD_ROOT resolved to an empty path"
  if [ -n "${AQD_REPO_REF:-}" ]; then REF="$AQD_REPO_REF"; fi
  if [ -n "$TTY_SRC" ] && [ ! -r "$TTY_SRC" ]; then
    note "answers are read from stdin ($TTY_SRC is not readable - normal when the installer is piped)"
  fi

  WORK="$(mktemp -d "${TMPDIR:-/tmp}/aqd-install.XXXXXX")" || die "could not create a temp dir"
  BACKUP_DIR="$HOME/aqd_backup_$TS"

  hdr "aqd installer - one command (macOS)"
  note "root   : $R"
  note "mode   : $(if [ "$CLEAN" = 1 ]; then printf 'CLEAN reinstall'; else printf 'fresh install / update'; fi)"
  note "ref    : $REF"
  note "payload: $REPO_GIT"
  note "Enter = defaults: base=$DEFAULT_BASE  CLIP=no  OCR=no  id_suffixes=<required>  list_max_pages=$DEFAULT_LIST_PAGES  fetch_limit=$DEFAULT_FETCH_LIMIT  startup=$(if [ "$R" = "$HOME/aqd" ]; then printf 'yes'; else printf 'no'; fi)"
  note "REQUIRED (no default, asked until valid): AQD_ID_SUFFIXES (digits 0-9), AQD_ADMIN_EMAIL. Everything else is Enter."
  note "AQD_ADMIN_PASSWORD: Enter keeps the value already in creds.env when there is one."
  if [ "$DRY_RUN" = 1 ]; then
    note "DRY RUN: every action is printed instead of run. The only thing it does is read (it fetches the source to a"
    note "         temp dir so it can compare), so nothing on this machine changes. Re-run without AQD_DRY_RUN to install."
  fi

  hdr "1/8 Python"
  if ! find_python; then
    die "no real Python 3.10+ found. Install it then re-run: brew install python@3.12"
  fi
  ok "python: $PY_BASE ($PY_VER)"
  case "$PY_VER" in
    3.12.*) : ;;
    *) warn "this payload is written for Python 3.12; $PY_VER works but the torch/easyocr wheels are less certain for it"
       warn "if you hit an odd heavy-install failure, install 3.12: brew install python@3.12" ;;
  esac

  if [ -d "$R" ]; then existed=1; else mkdir_p "$R"; done_ok "created $R"; fi

  if [ "$CLEAN" = 1 ] && [ "$existed" = 1 ]; then
    hdr "2/8 Stop old processes"
    stop_old
    hdr "3/8 Report before wiping"
    show_report
    hdr "4/8 Rescue backup"
    if confirm "Back up creds.env + chrome-profile + aqd_data to $BACKUP_DIR?" yes; then
      backup_state
      done_ok "backup: $BACKUP_DIR"
    else
      warn "NO backup. Losing chrome-profile means you MUST log in to Google again."
    fi
    hdr "5/8 Wipe conflicting state"
    if confirm "Also delete chrome-profile (Google login session)?" no; then
      warn "chrome-profile will be deleted - you must log in to Google again"
      clear_state yes
    else
      clear_state no
    fi
  else
    hdr "2/8 Stop old processes"
    if [ "$CLEAN" = 0 ] && [ "$existed" = 1 ]; then stop_graceful; else note "skipped: $R does not exist yet"; fi
    hdr "3/8 Report before wiping"
    note "skipped"
    hdr "4/8 Rescue backup"
    note "skipped"
    hdr "5/8 Wipe conflicting state"
    note "skipped: update source only, keep creds.env / chrome-profile / aqd_data"
  fi

  hdr "6/8 Fetch source + install flat"
  get_source
  ok "source: $SRC"
  pre_install_patch_check
  payload="$(payload_py_names "$SRC")"
  clear_stale_py "$payload" "$BACKUP_DIR/stale_py"
  ask_suffixes
  ask_list_options
  ask_fetch_limit
  parity_overlap_warnings "$SUFFIXES_OUT" | while IFS= read -r w; do
    [ -n "$w" ] && warn "$w"
  done
  if [ -n "$SUFFIXES_OUT" ]; then
    note "AQD_ID_SUFFIXES wins over AQD_PARITY; the installer writes AQD_PARITY='' so there is no ambiguity - keep the other machine on a disjoint set."
  fi
  resolve_cdp_port "$R" "$DEFAULT_CDP_PORT"
  write_file "$R/aqd_config.json" "$(printf '{\n  "id_suffixes": "%s",\n  "parity": "%s",\n  "list_max_pages": "%s",\n  "fetch_limit": "%s",\n  "cdp_port": %s\n}\n' "$SUFFIXES_OUT" "$PARITY_OUT" "$LIST_PAGES_OUT" "$FETCH_LIMIT_OUT" "$CDP_PORT_OUT")"
  if [ -n "$SUFFIXES_OUT" ]; then ok "aqd_config.json: AQD_ID_SUFFIXES='$SUFFIXES_OUT'"; else ok "aqd_config.json: AQD_PARITY='$PARITY_OUT' (no suffixes)"; fi
  ok "aqd_config.json: AQD_LIST_MAX_PAGES='$LIST_PAGES_OUT' AQD_FETCH_LIMIT='$FETCH_LIMIT_OUT' cdp_port=$CDP_PORT_OUT"
  install_flat
  write_creds
  if [ "$DRY_RUN" = 0 ] && [ "$(count_cred_keys)" -lt 2 ]; then warn "creds.env is missing keys - alt.py will fail when it starts"; fi

  ensure_venv
  PY="$R/venv/bin/python"
  install_deps

  hdr "8/8 Chrome + start"
  CHROME_BIN="$(find_chrome)"
  if [ "$NO_CHROME" = 1 ]; then warn "Chrome skipped (--no-chrome); the CDP port for this root is $CDP_PORT_OUT"; fi
  for f in STOP_ALL STOP; do if [ -e "$R/$f" ]; then rm_f "$R/$f"; ok "removed $f"; fi; done
  if confirm "Register auto-start at macOS login (launchd)?" "$(if [ "$R" = "$HOME/aqd" ]; then printf yes; else printf no; fi)"; then use_launchd=1; fi
  if [ "$use_launchd" = 1 ]; then
    startup_install
    if [ "$NO_CHROME" = 0 ]; then
      wait_for_cdp "$CDP_PORT_OUT"
      warn "REMEMBER: log in to Google in that Chrome window (the script cannot do it)"
    fi
    if [ "$NO_START" = 0 ]; then ok "alt.py starts from launchd in about 45s"; fi
  else
    if [ "$NO_CHROME" = 0 ]; then
      start_chrome "$CDP_PORT_OUT"
      warn "REMEMBER: log in to Google in that Chrome window (the script cannot do it)"
    fi
    start_alt
    if [ "$NO_START" = 0 ] && [ -f "$R/menu_worker.py" ]; then
      if act "start menu_worker.py"; then (cd "$R" && nohup env AQD_ROOT="$R" "$PY" "$R/menu_worker.py" >"$R/menu_worker.out" 2>&1 &); fi
      done_ok "started menu_worker.py (log: $R/menu_worker_log.md)"
    fi
  fi

  say ""
  say "${C_GR}================ DONE ================${C_0}"
  note "root   : $R"
  note "python : $PY ($PY_VER)"
  note "id pick: $(if [ -n "$SUFFIXES_OUT" ]; then printf 'suffixes %s' "$SUFFIXES_OUT"; else printf 'parity=%s' "$PARITY_OUT"; fi)"
  if [ "$LIST_PAGES_OUT" = 0 ]; then
    note "first start: lists EVERY page before the first place (about 12 minutes, old behaviour)"
  else
    note "first start: lists $LIST_PAGES_OUT pages (under a minute at 3s/page), then the first place is fetched"
  fi
  note "per round: $(if [ "$FETCH_LIMIT_OUT" = 0 ]; then printf 'every listed place'; else printf '%s place(s), then it lists again' "$FETCH_LIMIT_OUT"; fi)"
  note "watch it : tail -f $R/alt.out   (and $R/safe_run_log.md for each place)"
  note "creds  : $R/creds.env"
  note "config : $R/aqd_config.json"
  note "cdp    : port $CDP_PORT_OUT (Chrome profile $R/chrome-profile - never shared with another root)"
  note "logs   : $R/alt.out , $R/alt.nohup , $R/safe_run_log.md , $R/pip-*.log"
  note "stop   : touch $R/STOP_ALL"
  note "re-run : curl -fsSL $RAW_BASE/mac/install.sh | bash"
  say "${C_GR}=====================================${C_0}"
}

main "$@"
