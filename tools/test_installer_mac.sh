#!/usr/bin/env bash
# Offline simulation harness for mac/install.sh. There is no Mac on this machine, so the macOS-only tools are
# stubbed (launchctl, ps, curl, git, python3, unzip, shasum, sha256sum, caffeinate, open, pgrep, pkill, plutil)
# and the installer runs the whole flow against a temporary root. It proves the prompt order, that Enter alone
# is enough, the flat layout, aqd_config.json, the suffix/parity warning, the root-scoped guard, the Chrome-per-
# root rule, the rendered launchd plist, the dry run, and the hand-patch safety (backup + sha256 + default No).
#
# Run: bash tools/test_installer_mac.sh         (needs a POSIX bash: Linux, macOS or WSL)
#      AQD_SIM_SHOW=none bash tools/test_installer_mac.sh   (assertions only, no transcripts)
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
INSTALL="$REPO/mac/install.sh"
REAL_PY="$(command -v python3 || command -v python)"
REAL_SHASUM="$(command -v shasum || true)"
REAL_SHA256SUM="$(command -v sha256sum || true)"
FAILED=0
SHOW="${AQD_SIM_SHOW:-a,c1,d}"

assert_eq() {
  local label="$1" got="$2" want="$3"
  if [ "$got" = "$want" ]; then echo "  ok   $label: $got"
  else echo "  FAIL $label: got [$got] want [$want]"; FAILED=$((FAILED + 1)); fi
}
assert() {
  local label="$1" cond="$2"
  if [ "$cond" = 0 ]; then echo "  ok   $label"
  else echo "  FAIL $label"; FAILED=$((FAILED + 1)); fi
}
is0() { if [ "$1" = 0 ]; then echo 0; else echo 1; fi; }

[ -n "$REAL_PY" ] || { echo "python3 is required to build the payload zip"; exit 2; }
[ -f "$INSTALL" ] || { echo "missing $INSTALL"; exit 2; }

SIM="$(mktemp -d "${AQD_SIM_BASE:-/tmp}/aqd-mac-sim.XXXXXX")"
case "$SIM" in /*) : ;; *) echo "temp dir is not absolute: $SIM" >&2; exit 2 ;; esac
cleanup() { if [ "${AQD_SIM_KEEP:-0}" = 1 ]; then echo "kept simulation dir: $SIM"; return 0; fi; rm -rf "$SIM" 2>/dev/null || true; }
trap cleanup EXIT
mkdir -p "$SIM/bin" "$SIM/home" "$SIM/tmp" "$SIM/cdp"

# ---------------------------------------------------------------- stubs
stub() { mkdir -p "$SIM/bin"; cat >"$SIM/bin/$1"; chmod +x "$SIM/bin/$1"; }

stub git <<'EOF'
#!/usr/bin/env bash
echo "git-stub: git is not available in this simulation" >&2
exit 1
EOF

stub curl <<'EOF'
#!/usr/bin/env bash
out=""
url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="${2:-}"; shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
case "$url" in
  *127.0.0.1:*)
    p="${url#*127.0.0.1:}"
    p="${p%%/*}"
    if [ -f "${AQD_SIM_CDP_DIR:-/nonexistent}/$p" ]; then echo '{"Browser":"Chrome/120.0"}'; exit 0; fi
    exit 7
    ;;
esac
echo "curl-stub $url -> $out" >>"${AQD_SIM_LOG:-/dev/null}"
[ -n "$out" ] || { echo "curl-stub: no -o target" >&2; exit 2; }
cp "${AQD_SIM_ZIP}" "$out" || exit 22
exit 0
EOF

stub unzip <<'EOF'
#!/usr/bin/env bash
src=""
dest=""
while [ $# -gt 0 ]; do
  case "$1" in
    -d) dest="${2:-}"; shift 2 ;;
    -*) shift ;;
    *) src="$1"; shift ;;
  esac
done
[ -n "$src" ] && [ -n "$dest" ] || { echo "unzip-stub: need src and -d dest" >&2; exit 2; }
"${AQD_SIM_REAL_PY}" -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "$src" "$dest"
EOF

stub ps <<'EOF'
#!/usr/bin/env bash
if [ -n "${AQD_SIM_PS_LIST:-}" ] && [ -f "$AQD_SIM_PS_LIST" ]; then cat "$AQD_SIM_PS_LIST"; fi
exit 0
EOF

stub launchctl <<'EOF'
#!/usr/bin/env bash
echo "launchctl $*" >>"${AQD_SIM_LOG:-/dev/null}"
exit 0
EOF

stub shasum <<'EOF'
#!/usr/bin/env bash
echo "shasum $*" >>"${AQD_SIM_LOG:-/dev/null}"
if [ -n "${AQD_SIM_REAL_SHASUM:-}" ]; then exec "$AQD_SIM_REAL_SHASUM" "$@"; fi
if [ -n "${AQD_SIM_REAL_SHA256SUM:-}" ]; then exec "$AQD_SIM_REAL_SHA256SUM" "$@"; fi
exit 1
EOF

stub sha256sum <<'EOF'
#!/usr/bin/env bash
echo "sha256sum $*" >>"${AQD_SIM_LOG:-/dev/null}"
if [ -n "${AQD_SIM_REAL_SHA256SUM:-}" ]; then exec "$AQD_SIM_REAL_SHA256SUM" "$@"; fi
exit 1
EOF

for n in caffeinate open plutil; do
  stub "$n" <<'EOF'
#!/usr/bin/env bash
echo "$(basename "$0") $*" >>"${AQD_SIM_LOG:-/dev/null}"
exit 0
EOF
done

for n in pgrep pkill; do
  stub "$n" <<'EOF'
#!/usr/bin/env bash
echo "$(basename "$0") $*" >>"${AQD_SIM_LOG:-/dev/null}"
exit 1
EOF
done

stub_python() {
  stub "$1" <<'EOF'
#!/usr/bin/env bash
SELF="$0"
case "$SELF" in /*) : ;; *) SELF="$(cd "$(dirname "$SELF")" && pwd)/$(basename "$SELF")" ;; esac
case "${1:-}" in
  -c)
    case "${2:-}" in
      *version_info*) echo "3.12.10" ;;
      *) : ;;
    esac
    exit 0
    ;;
  -m)
    case "${2:-}" in
      venv)
        v="${3:-}"
        [ -n "$v" ] || exit 2
        mkdir -p "$v/bin"
        for n in python python3 pip pip3; do cp "$SELF" "$v/bin/$n"; chmod +x "$v/bin/$n"; done
        echo "python-stub: created venv $v" >>"${AQD_SIM_LOG:-/dev/null}"
        exit 0
        ;;
      pip)
        echo "python-stub: pip ${*:3}" >>"${AQD_SIM_LOG:-/dev/null}"
        case "$*" in
          *"index versions torch"*)
            echo "torch (2.4.1)"
            echo "Available versions: 2.4.1, 2.4.0, 2.3.1"
            exit 0
            ;;
        esac
        echo "Successfully installed (simulated, no download)"
        exit 0
        ;;
    esac
    exit 0
    ;;
esac
echo "python-stub $*" >>"${AQD_SIM_LOG:-/dev/null}"
exit 0
EOF
}
for n in python3 python3.12 python3.11 python3.10 python3.13; do stub_python "$n"; done

# ---------------------------------------------------------------- payload zip (GitHub-like: one top folder)
"$REAL_PY" - "$REPO" "$SIM/src.zip" <<'PY'
import os, sys, zipfile
repo, out = sys.argv[1], sys.argv[2]
top = "script_google_map-main"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for sub in ("common", "mac"):
        base = os.path.join(repo, sub)
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in sorted(files):
                if f.endswith(".pyc"):
                    continue
                p = os.path.join(root, f)
                rel = os.path.relpath(p, repo).replace(os.sep, "/")
                z.write(p, top + "/" + rel)
PY

run_install() { # name answers-file [args...]
  local name="$1" answers="$2"
  shift 2
  local out="$SIM/case_$name.log" rc
  (
    cd "$REPO" || exit 99
    export HOME="$SIM/home" TMPDIR="$SIM/tmp" PATH="$SIM/bin:$PATH"
    export AQD_TTY= AQD_SIM_PS_LIST="$SIM/pslist.txt" AQD_SIM_CDP_DIR="$SIM/cdp"
    export AQD_SIM_ZIP="$SIM/src.zip" AQD_SIM_LOG="$SIM/stub.log"
    export AQD_SIM_REAL_PY="$REAL_PY" AQD_SIM_REAL_SHASUM="$REAL_SHASUM" AQD_SIM_REAL_SHA256SUM="$REAL_SHA256SUM"
    bash "$INSTALL" "$@"
  ) <"$answers" >"$out" 2>&1
  rc=$?
  printf '%s' "$rc" >"$SIM/case_$name.rc"
  return 0
}

rc_of() { cat "$SIM/case_$1.rc" 2>/dev/null; }
log_of() { cat "$SIM/case_$1.log" 2>/dev/null; }
has() { grep -qF "$2" "$SIM/case_$1.log" 2>/dev/null; }
has_re() { grep -qE "$2" "$SIM/case_$1.log" 2>/dev/null; }
has0() { if has "$@"; then echo 0; else echo 1; fi; }
has_re0() { if has_re "$@"; then echo 0; else echo 1; fi; }
hasnt0() { if has "$@"; then echo 1; else echo 0; fi; }
hasnt_re0() { if has_re "$@"; then echo 1; else echo 0; fi; }

prompt_seq() {
  grep -aE '^(AQD_[A-Z_]+|Install CLIP|Install OCR|Register auto-start|Overwrite creds\.env|Overwrite this Mac|Back up creds\.env|Also delete chrome-profile|Kill -9 the)' "$SIM/case_$1.log" 2>/dev/null |
    sed -e 's/^\(AQD_[A-Z_]*\).*/\1/' \
        -e 's/^Install CLIP.*/InstallCLIP/' \
        -e 's/^Install OCR.*/InstallOCR/' \
        -e 's/^Register auto-start.*/Register/' \
        -e 's/^Overwrite creds\.env.*/OverwriteCreds/' \
        -e "s/^Overwrite this Mac.*/OverwriteAltPy/" \
        -e 's/^Back up creds\.env.*/Backup/' \
        -e 's/^Also delete chrome-profile.*/DeleteProfile/' \
        -e 's/^Kill -9 the.*/KillConfirm/' |
    tr '\n' ',' | sed -e 's/,$//'
}

reset_ps() { : >"$SIM/pslist.txt"; : >"$SIM/stub.log"; rm -f "$SIM"/cdp/*; rm -rf "$SIM"/home/aqd_backup_*; }
ps_line() { printf '%s\n' "$1" >>"$SIM/pslist.txt"; }

show_case() { # name
  local n="$1"
  [ "$SHOW" = none ] && return 0
  case ",$SHOW," in *,all,*|*,"$n",*) : ;; *) return 0 ;; esac
  echo ""
  echo "-------- transcript: case $n (rc=$(rc_of "$n")) --------"
  cat "$SIM/case_$n.log"
  echo "-------- end case $n --------"
}

PLANT_ALT='# hand patch kept on the Mac (NOT the repo alt.py)
print("HAND PATCH")
'
PLANT_SAFE='# hand patched safe_run (pg >= 500)
x = 1
'
printf '%s' "$PLANT_ALT" >"$SIM/plant_alt.ref"
printf '%s' "$PLANT_SAFE" >"$SIM/plant_safe.ref"

echo "==================================================================="
echo "(A) fresh install: Enter everywhere except the suffixes + the email"
echo "==================================================================="
reset_ps
ps_line "70001 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9222 --user-data-dir=$SIM/root_a/chrome-profile"
: >"$SIM/cdp/9222"
ROOT_A="$SIM/root_a"
cat >"$SIM/ans_a" <<'EOF'
1 3 5


admin@example.com
secretpass


EOF
printf '\n' >>"$SIM/ans_a"
run_install a "$SIM/ans_a" --root "$ROOT_A"
assert_eq "exit code" "$(rc_of a)" 0
assert_eq "prompt order" "$(prompt_seq a)" "AQD_ID_SUFFIXES,AQD_LIST_MAX_PAGES,AQD_FETCH_LIMIT,AQD_ADMIN_EMAIL,AQD_ADMIN_PASSWORD,AQD_BASE,InstallCLIP,InstallOCR,Register"
for f in alt.py safe_run.py photos.py photos_new.py menu_vocab.json aqd_api.py; do
  assert "flat layout: $f directly in the root" "$([ -f "$ROOT_A/$f" ] && echo 0 || echo 1)"
done
assert "no common/ subfolder" "$([ ! -d "$ROOT_A/common" ] && echo 0 || echo 1)"
assert "photos.py is the AQD_ROOT-aware build" "$(grep -q 'AQD_ROOT' "$ROOT_A/photos.py" && echo 0 || echo 1)"
assert "venv created" "$([ -x "$ROOT_A/venv/bin/python" ] && echo 0 || echo 1)"
assert_eq "aqd_config id_suffixes" "$(sed -n 's/.*"id_suffixes": "\(.*\)",/\1/p' "$ROOT_A/aqd_config.json")" "1 3 5"
assert_eq "aqd_config parity is empty" "$(sed -n 's/.*"parity": "\(.*\)",/\1/p' "$ROOT_A/aqd_config.json")" ""
assert_eq "aqd_config list_max_pages" "$(sed -n 's/.*"list_max_pages": "\(.*\)",/\1/p' "$ROOT_A/aqd_config.json")" "20"
assert_eq "aqd_config fetch_limit" "$(sed -n 's/.*"fetch_limit": "\(.*\)",/\1/p' "$ROOT_A/aqd_config.json")" "0"
assert_eq "aqd_config cdp_port" "$(sed -n 's/.*"cdp_port": \([0-9]*\).*/\1/p' "$ROOT_A/aqd_config.json")" "9222"
assert_eq "creds email" "$(sed -n 's/^AQD_ADMIN_EMAIL=//p' "$ROOT_A/creds.env")" "admin@example.com"
assert_eq "creds base (Enter default)" "$(sed -n 's/^AQD_BASE=//p' "$ROOT_A/creds.env")" "https://anuongquanhday.com"
assert "creds has the password line" "$(grep -q '^AQD_ADMIN_PASSWORD=secretpass$' "$ROOT_A/creds.env" && echo 0 || echo 1)"
assert "defaults summary printed" "$(has0 a 'Enter = defaults: base=https://anuongquanhday.com')"
assert "required list printed" "$(has0 a 'REQUIRED (no default, asked until valid)')"
assert "torch wheel probe is announced" "$(has0 a 'checking whether torch has a wheel for this Python')"
assert "detects the torch wheel (no source build)" "$(has0 a 'torch has a wheel for Python 3.12.10 - no source build needed')"
assert "flat layout message" "$(has0 a '(FLAT layout: every .py sits directly in')"
assert "photos.py override explained" "$(has0 a 'hardcodes C:/aqd')"
assert "reused the CDP 9222 with our own profile" "$(has0 a 'already open with this root')"
assert "google login reminder" "$(has0 a 'REMEMBER: log in to Google')"
assert "alt.py started" "$(has0 a 'started alt.py')"
assert "DONE block: watch" "$(has0 a "watch it : tail -f $ROOT_A/alt.out")"
assert "DONE block: stop" "$(has0 a "stop   : touch $ROOT_A/STOP_ALL")"
assert "DONE block: re-run" "$(has0 a 're-run : curl -fsSL')"
assert "no dry-run lines" "$(hasnt_re0 a '\[dry-run\]')"
assert "no launchd plist when startup=no" "$([ ! -e "$SIM/home/Library/LaunchAgents/aqd.alt.plist" ] && echo 0 || echo 1)"
show_case a

echo ""
echo "==================================================================="
echo "(A2) re-run on the same root: idempotent, creds.env kept"
echo "==================================================================="
reset_ps
ps_line "70001 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9222 --user-data-dir=$ROOT_A/chrome-profile"
: >"$SIM/cdp/9222"
cat >"$SIM/ans_a2" <<'EOF'
1 3 5


EOF
printf '\n\n\n' >>"$SIM/ans_a2"
run_install a2 "$SIM/ans_a2" --root "$ROOT_A"
assert_eq "exit code" "$(rc_of a2)" 0
assert_eq "prompt order (creds kept, no email prompt)" "$(prompt_seq a2)" "AQD_ID_SUFFIXES,AQD_LIST_MAX_PAGES,AQD_FETCH_LIMIT,OverwriteCreds,InstallCLIP,InstallOCR,Register"
assert "keeps the existing creds.env" "$(has0 a2 'keeping the existing creds.env')"
assert_eq "creds email unchanged" "$(sed -n 's/^AQD_ADMIN_EMAIL=//p' "$ROOT_A/creds.env")" "admin@example.com"
assert_eq "creds password unchanged" "$(sed -n 's/^AQD_ADMIN_PASSWORD=//p' "$ROOT_A/creds.env")" "secretpass"
show_case a2

echo ""
echo "==================================================================="
echo "(B) suffix 9 vs parity=odd: the overlap warning"
echo "==================================================================="
reset_ps
ROOT_B="$SIM/root_b"
cat >"$SIM/ans_b" <<'EOF'
9


b@example.com
pw


EOF
printf '\n' >>"$SIM/ans_b"
run_install b "$SIM/ans_b" --root "$ROOT_B" --skip-heavy --no-chrome
assert_eq "exit code" "$(rc_of b)" 0
assert "prints the odd overlap warning" "$(has0 b 'suffix 9 is INSIDE parity=odd (1,3,5,7,9) - if another machine runs parity=odd you will both fetch id%10=9. Set the other machine to a disjoint set.')"
assert "explains suffix wins over parity" "$(has0 b 'AQD_ID_SUFFIXES wins over AQD_PARITY')"
assert_eq "aqd_config parity empty when suffixes set" "$(sed -n 's/.*"parity": "\(.*\)",/\1/p' "$ROOT_B/aqd_config.json")" ""
assert_eq "aqd_config id_suffixes" "$(sed -n 's/.*"id_suffixes": "\(.*\)",/\1/p' "$ROOT_B/aqd_config.json")" "9"
show_case b

echo ""
echo "==================================================================="
echo "(C1) hand-patched alt.py, Enter (= default No): STOP, never overwrite"
echo "==================================================================="
reset_ps
ROOT_C1="$SIM/root_c1"
mkdir -p "$ROOT_C1"
printf '%s' "$PLANT_ALT" >"$ROOT_C1/alt.py"
printf '%s' "$PLANT_SAFE" >"$ROOT_C1/safe_run.py"
printf '\n' >"$SIM/ans_c1"
run_install c1 "$SIM/ans_c1" --root "$ROOT_C1" --skip-heavy --no-chrome
assert_eq "exit code (stopped on purpose)" "$(rc_of c1)" 3
assert "warns the Mac alt.py differs" "$(has0 c1 "this Mac's alt.py DIFFERS from the repo's mac/alt.py")"
assert "prints mac sha256" "$(has_re0 c1 'this Mac : .*/alt\.py   sha256=[0-9a-f]{64} lines=[0-9]+')"
assert "prints repo sha256" "$(has_re0 c1 'repo     : mac/alt\.py   sha256=[0-9a-f]{64} lines=[0-9]+')"
assert "names the backup path" "$(has0 c1 'preinstall_py/alt.py')"
assert "says it stopped" "$(has0 c1 'stopped on purpose')"
assert "alt.py untouched" "$(cmp -s "$ROOT_C1/alt.py" "$SIM/plant_alt.ref" && echo 0 || echo 1)"
BAK_C1="$(ls -d "$SIM"/home/aqd_backup_* 2>/dev/null | head -n 1)"
assert "backup dir made" "$([ -n "$BAK_C1" ] && echo 0 || echo 1)"
assert "backup holds the hand-patched alt.py" "$(cmp -s "$BAK_C1/preinstall_py/alt.py" "$SIM/plant_alt.ref" && echo 0 || echo 1)"
assert "backup holds safe_run.py" "$(cmp -s "$BAK_C1/preinstall_py/safe_run.py" "$SIM/plant_safe.ref" && echo 0 || echo 1)"
rm -rf "$SIM"/home/aqd_backup_*
show_case c1

echo ""
echo "==================================================================="
echo "(C2) hand-patched alt.py, answer y: overwrite + old queue generation"
echo "==================================================================="
reset_ps
ROOT_C2="$SIM/root_c2"
mkdir -p "$ROOT_C2/__pycache__"
printf '%s' "$PLANT_ALT" >"$ROOT_C2/alt.py"
printf '%s' "$PLANT_SAFE" >"$ROOT_C2/safe_run.py"
printf 'x' >"$ROOT_C2/legacy_extra.py"
printf 'junk' >"$ROOT_C2/__pycache__/junk.pyc"
printf '[]' >"$ROOT_C2/queue6.json"
printf '[]' >"$ROOT_C2/queue8.json"
printf '0' >"$ROOT_C2/alt_step.txt"
printf 'AQD_ADMIN_EMAIL=old@x.y\nAQD_ADMIN_PASSWORD=oldpw\nAQD_BASE=https://old\n' >"$ROOT_C2/creds.env"
cat >"$SIM/ans_c2" <<'EOF'
y
2 4


EOF
printf '\n\n' >>"$SIM/ans_c2"
run_install c2 "$SIM/ans_c2" --root "$ROOT_C2" --skip-heavy --no-chrome
assert_eq "exit code" "$(rc_of c2)" 0
assert "alt.py now equals the repo version" "$(cmp -s "$ROOT_C2/alt.py" "$REPO/mac/alt.py" && echo 0 || echo 1)"
assert "queue6.json gone" "$([ ! -e "$ROOT_C2/queue6.json" ] && echo 0 || echo 1)"
assert "queue8.json gone" "$([ ! -e "$ROOT_C2/queue8.json" ] && echo 0 || echo 1)"
assert "alt_step.txt gone" "$([ ! -e "$ROOT_C2/alt_step.txt" ] && echo 0 || echo 1)"
assert "stray .py gone" "$([ ! -e "$ROOT_C2/legacy_extra.py" ] && echo 0 || echo 1)"
assert "__pycache__ gone" "$([ ! -d "$ROOT_C2/__pycache__" ] && echo 0 || echo 1)"
assert "creds.env kept" "$(grep -q '^AQD_ADMIN_PASSWORD=oldpw$' "$ROOT_C2/creds.env" && echo 0 || echo 1)"
assert_eq "config rewritten with the new suffixes" "$(sed -n 's/.*"id_suffixes": "\(.*\)",/\1/p' "$ROOT_C2/aqd_config.json")" "2 4"
BAK_C2="$(ls -d "$SIM"/home/aqd_backup_* 2>/dev/null | head -n 1)"
assert "old generation backed up" "$([ -f "$BAK_C2/old_queue/queue6.json" ] && [ -f "$BAK_C2/old_queue/alt_step.txt" ] && echo 0 || echo 1)"
assert "stray .py backed up" "$([ -f "$BAK_C2/stale_py/legacy_extra.py" ] && echo 0 || echo 1)"
assert "says old generation" "$(has0 c2 'old generation marker found: queue6.json')"
assert "lists the stray .py" "$(has0 c2 'stray .py at the root (not in the new payload): legacy_extra.py')"
rm -rf "$SIM"/home/aqd_backup_*
show_case c2

echo ""
echo "==================================================================="
echo "(D) CLEAN reinstall: stop -> report -> backup -> wipe -> install"
echo "==================================================================="
reset_ps
ROOT_D="$SIM/root_d"
mkdir -p "$ROOT_D/chrome-profile/Cache" "$ROOT_D/aqd_data/123" "$ROOT_D/aqd_data/456" "$ROOT_D/__pycache__"
printf '%s' "$PLANT_ALT" >"$ROOT_D/alt.py"
printf '%s' "$PLANT_SAFE" >"$ROOT_D/safe_run.py"
printf 'x' >"$ROOT_D/legacy_extra.py"
printf 'junk' >"$ROOT_D/__pycache__/junk.pyc"
printf 'cookie' >"$ROOT_D/chrome-profile/Cache/c"
printf '{"id":123}' >"$ROOT_D/aqd_data/123/info.json"
printf 'done' >"$ROOT_D/aqd_data/123/SAFE_DONE"
printf 'prep' >"$ROOT_D/aqd_data/456/SAFE_PREP"
printf 'l1\nl2\nl3\n' >"$ROOT_D/safe_run_log.md"
printf '[]' >"$ROOT_D/queue_even.json"
printf '{}' >"$ROOT_D/parity_tries.json"
printf 'log' >"$ROOT_D/alt.out"
printf 'stop' >"$ROOT_D/STOP_ALL"
printf 'AQD_ADMIN_EMAIL=keep@x.y\nAQD_ADMIN_PASSWORD=keeppw\nAQD_BASE=https://keep\n' >"$ROOT_D/creds.env"
printf '{\n  "id_suffixes": "0 2",\n  "parity": "",\n  "list_max_pages": "20",\n  "fetch_limit": "0",\n  "cdp_port": 9222\n}\n' >"$ROOT_D/aqd_config.json"
cat >"$SIM/ans_d" <<'EOF'


y
6 8



EOF
printf '\n' >>"$SIM/ans_d"
run_install d "$SIM/ans_d" --root "$ROOT_D" --skip-heavy --no-chrome --clean
assert_eq "exit code" "$(rc_of d)" 0
assert_eq "prompt order (CLEAN)" "$(prompt_seq d)" "Backup,DeleteProfile,OverwriteAltPy,AQD_ID_SUFFIXES,AQD_LIST_MAX_PAGES,AQD_FETCH_LIMIT,OverwriteCreds,Register"
h2="$(grep -n '2/8 Stop old processes' "$SIM/case_d.log" | head -n1 | cut -d: -f1)"
h3="$(grep -n '3/8 Report before wiping' "$SIM/case_d.log" | head -n1 | cut -d: -f1)"
h4="$(grep -n '4/8 Rescue backup' "$SIM/case_d.log" | head -n1 | cut -d: -f1)"
h5="$(grep -n '5/8 Wipe conflicting state' "$SIM/case_d.log" | head -n1 | cut -d: -f1)"
h6="$(grep -n '6/8 Fetch source + install flat' "$SIM/case_d.log" | head -n1 | cut -d: -f1)"
assert "order stop<report<backup<wipe<install" "$([ -n "$h2" ] && [ "$h2" -lt "$h3" ] && [ "$h3" -lt "$h4" ] && [ "$h4" -lt "$h5" ] && [ "$h5" -lt "$h6" ] && echo 0 || echo 1)"
assert "reports the state before wiping" "$(has0 d 'aqd_data: 2 places | SAFE_DONE: 1 | SAFE_PREP: 1')"
assert "reports safe_run_log.md lines" "$(has0 d 'safe_run_log.md: 3 lines')"
assert "creates STOP_ALL" "$(has0 d 'created STOP_ALL + STOP')"
assert "no process of this root left" "$(has0 d "no $ROOT_D aqd process left")"
BAK_D="$(ls -d "$SIM"/home/aqd_backup_* 2>/dev/null | head -n 1)"
assert "rescue backup: creds.env" "$([ -f "$BAK_D/creds.env" ] && echo 0 || echo 1)"
assert "rescue backup: chrome-profile" "$([ -f "$BAK_D/chrome-profile/Cache/c" ] && echo 0 || echo 1)"
assert "rescue backup: aqd_data" "$([ -f "$BAK_D/aqd_data/123/info.json" ] && echo 0 || echo 1)"
assert "STOP_ALL removed at the end" "$([ ! -e "$ROOT_D/STOP_ALL" ] && echo 0 || echo 1)"
assert "queue_even.json wiped" "$([ ! -e "$ROOT_D/queue_even.json" ] && echo 0 || echo 1)"
assert "parity_tries.json wiped" "$([ ! -e "$ROOT_D/parity_tries.json" ] && echo 0 || echo 1)"
assert "alt.out wiped" "$([ ! -e "$ROOT_D/alt.out" ] && echo 0 || echo 1)"
assert "chrome-profile kept" "$([ -d "$ROOT_D/chrome-profile" ] && echo 0 || echo 1)"
assert "says chrome-profile kept" "$(has0 d 'kept chrome-profile/')"
assert "aqd_data kept" "$([ -f "$ROOT_D/aqd_data/123/SAFE_DONE" ] && echo 0 || echo 1)"
assert "creds.env kept with the old password" "$(grep -q '^AQD_ADMIN_PASSWORD=keeppw$' "$ROOT_D/creds.env" && echo 0 || echo 1)"
assert "stray .py backed up in CLEAN too" "$([ -f "$BAK_D/stale_py/legacy_extra.py" ] && echo 0 || echo 1)"
rm -rf "$SIM"/home/aqd_backup_*
show_case d

echo ""
echo "==================================================================="
echo "(E) guards: another root's process is warned, never killed;"
echo "    another root's Chrome profile is not reused (port moves on)"
echo "==================================================================="
reset_ps
ROOT_E="$SIM/root_e"
mkdir -p "$ROOT_E"
printf 'AQD_ADMIN_EMAIL=z@x.y\nAQD_ADMIN_PASSWORD=zz\n' >"$ROOT_E/creds.env"
ps_line "40708 /usr/bin/python3 $SIM/other_root/aqd/alt.py"
ps_line "12252 /usr/bin/python3 $SIM/other_root/aqd/safe_run.py"
ps_line "90003 /usr/bin/python3 $SIM/other_root/aqd/menu_worker.py"
ps_line "99999 /usr/bin/python3 -m http.server"
ps_line "70002 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9222 --user-data-dir=$SIM/other_root/aqd/chrome-profile"
: >"$SIM/cdp/9222"
cat >"$SIM/ans_e" <<'EOF'


1 3


EOF
printf '\n\n\n' >>"$SIM/ans_e"
run_install e "$SIM/ans_e" --root "$ROOT_E" --skip-heavy --no-chrome --clean
assert_eq "exit code" "$(rc_of e)" 0
assert "warns about the other install" "$(has0 e 'another install is running and will NOT be touched')"
assert "lists the foreign pid" "$(has0 e 'pid=40708')"
assert "never kills the foreign pid" "$(hasnt_re0 e 'kill -9 40708')"
assert "no kill prompt for this root" "$(hasnt0 e 'Kill -9 the')"
assert "this root has no process left" "$(has0 e "no $ROOT_E aqd process left")"
assert "detects the foreign Chrome profile" "$(has0 e "belongs to another root's Chrome")"
assert "moves to the next CDP port" "$(has0 e 'trying the next port')"
assert_eq "cdp_port moved to 9223" "$(sed -n 's/.*"cdp_port": \([0-9]*\).*/\1/p' "$ROOT_E/aqd_config.json")" "9223"
assert "the http.server pid is ignored too" "$(hasnt0 e 'pid=99999')"
rm -rf "$SIM"/home/aqd_backup_*
show_case e

echo ""
echo "==================================================================="
echo "(F) AQD_DRY_RUN=1: prints the plan, changes nothing"
echo "==================================================================="
reset_ps
ROOT_F="$SIM/root_f"
cat >"$SIM/ans_f" <<'EOF'
1 3 5


f@example.com
pw


EOF
printf '\n' >>"$SIM/ans_f"
(AQD_DRY_RUN=1; export AQD_DRY_RUN; run_install f "$SIM/ans_f" --root "$ROOT_F" --no-chrome)
assert_eq "exit code" "$(rc_of f)" 0
assert "dry run announced" "$(has0 f 'DRY RUN: every action is printed instead of run')"
assert "read-only note" "$(has0 f 'dry run reads the payload into a temp dir (read-only)')"
assert "prints [dry-run] actions" "$(has0 f '[dry-run] cp -f')"
assert "the root was NOT created" "$([ ! -d "$ROOT_F" ] && echo 0 || echo 1)"
assert "no backup dir was created" "$([ -z "$(ls -d "$SIM"/home/aqd_backup_* 2>/dev/null)" ] && echo 0 || echo 1)"
assert "still prints the DONE block" "$(has0 f "stop   : touch $ROOT_F/STOP_ALL")"
show_case f

echo ""
echo "==================================================================="
echo "(G) auto-start: the launchd plist is rendered per root with EnvironmentVariables"
echo "==================================================================="
reset_ps
ROOT_G="$SIM/home/aqd"
cat >"$SIM/ans_g" <<'EOF'
1 3 5


g@example.com
pw


EOF
printf '\n' >>"$SIM/ans_g"
run_install g "$SIM/ans_g" --root "$ROOT_G" --skip-heavy --no-chrome --yes
assert_eq "exit code" "$(rc_of g)" 0
PLIST="$SIM/home/Library/LaunchAgents/aqd.alt.plist"
assert "plist installed in ~/Library/LaunchAgents" "$([ -f "$PLIST" ] && echo 0 || echo 1)"
assert "rendered copy kept in the root" "$([ -f "$ROOT_G/launchd/aqd.alt.plist" ] && echo 0 || echo 1)"
for k in AQD_ROOT AQD_ID_SUFFIXES AQD_PARITY AQD_LIST_MAX_PAGES AQD_FETCH_LIMIT AQD_CDP_PORT; do
  assert "plist EnvironmentVariables: $k" "$(grep -q "<key>$k</key>" "$PLIST" && echo 0 || echo 1)"
done
assert "plist has EnvironmentVariables block" "$(grep -q '<key>EnvironmentVariables</key>' "$PLIST" && echo 0 || echo 1)"
assert "no hardcoded user in the plist" "$(hasnt0 g 'hoangtruongle')"
assert "no leftover placeholder" "$(hasnt0 g '__AQD_')"
assert "plist points at this root's venv python" "$(grep -q "$ROOT_G/venv/bin/python" "$PLIST" && echo 0 || echo 1)"
assert "plist points at this root" "$(grep -q "$ROOT_G/alt.py" "$PLIST" && echo 0 || echo 1)"
assert "launchctl bootstrap was called" "$(grep -q "launchctl bootstrap gui/" "$SIM/stub.log" && echo 0 || echo 1)"
assert "menu plist also rendered" "$(grep -q 'menu_worker.py' "$ROOT_G/launchd/aqd.menu.plist" && echo 0 || echo 1)"
show_case g

echo ""
echo "==================================================================="
if [ "$FAILED" = 0 ]; then
  echo "ALL OK"
  exit 0
else
  echo "$FAILED CHECK(S) FAILED"
  exit 1
fi
