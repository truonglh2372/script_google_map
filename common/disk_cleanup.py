"""Tự dọn ổ cho crawler (gmaps / foody) - an_uong_quanh_day#1388, sau cảnh báo disk_low 04/11/2026.

Chạy bởi launchd (aqd.cleanup.<crawler>) / Task Scheduler (aqd_cleanup_<crawler>) mỗi 15 phút; mỗi lần chạy tự quyết:
  - tới hạn định kỳ: AQD_CLEANUP_EVERY_H (mặc định 6) giờ kể từ lần dọn trước, HOẶC
  - ổ còn < AQD_CLEANUP_MIN_FREE_GB (mặc định 8 GB, cao hơn ngưỡng tạm dừng của crawler: gmaps 5, foody 3) thì dọn
    ngay (tối đa 1 lần / 30 phút), log chỉ giữ 3 ngày; cache menu vẫn giữ đủ AQD_CLEANUP_CACHE_DAYS (7).
Crawler không có giờ nghỉ cố định, nên job KHÔNG dừng crawler: nó chỉ xoá file crawler không còn dùng tới (đã đẩy
xong / cache cũ / log cũ). Khi ổ dưới ngưỡng, chính crawler đã tự tạm dừng (alt.py AQD_MIN_FREE_GB, foody
--min-free-gb) và tự chạy lại khi job này giải phóng xong. Không đụng file STOP/STOP_ALL.

CHỈ xoá thứ tạo lại được:
  gmaps : ảnh trong aqd_data/<id>/ đã có SAFE_DONE (> 1 giờ) và id KHÔNG nằm trong photo_retry.json;
          menu_ocr_cache/*.jpg cũ hơn AQD_CLEANUP_CACHE_DAYS (giữ .ocr.json; menu_worker.fetch() tải lại nếu thiếu).
  foody : ảnh còn sót trong foody_data/<fid>/photos/ của quán có trong _cleaned.json (server đã xác nhận succeeded).
  cả hai: log (.log/.out/.err) không ghi thêm trong AQD_CLEANUP_LOG_DAYS (14) ngày -> cắt về rỗng (log đã xoay *.log.N -> xoá); log đang ghi > AQD_CLEANUP_LOG_MAX_MB
          (50) -> cắt còn AQD_CLEANUP_LOG_KEEP_MB (5) cuối; *.tmp/*.part cũ hơn 1 ngày.
  gmaps : cache trình duyệt chrome-profile/Default/{Cache, Code Cache} (danh sách CỐ ĐỊNH CHROME_CACHE_DIRS), CHỈ khi
          Chrome crawler KHÔNG chạy (không process nào có --user-data-dir=<root>/chrome-profile hoặc
          --remote-debugging-port=<cổng CDP>, và cổng CDP đóng; không đọc được danh sách process -> coi như đang chạy,
          bỏ qua). AQD_CLEANUP_CHROME_CACHE: low (mặc định, chỉ khi ổ thấp) / 1 (mọi lượt) / 0 (tắt).
KHÔNG BAO GIỜ xoá: json/jsonl dữ liệu, quán chưa SAFE_DONE / chưa _cleaned, crawler.env, creds.env, *.env, file secret,
phần còn lại của chrome-profile (History, Cookies, Login Data, Local State, ...), profile đăng nhập, venv, .git, bak/.
Mỗi lần dọn thật CÓ giải phóng: một dòng Slack qua slack_notify (#1388). Dọn 0 file -> không gửi; nếu ổ thấp thì gửi
cảnh báo disk_low (tối đa 1 lần / AQD_CLEANUP_ALERT_H (6) giờ) kèm thứ đang chiếm ổ: file hệ thống (pagefile.sys,
hiberfil.sys, swapfile.sys), thư mục lớn nhất (đo bằng os.scandir, chạy được cả Windows, có giới hạn thời gian) và
process ngốn RAM nhất - xem host_usage.py. --dry-run: chỉ liệt kê, không xoá, không gửi Slack.
"""
import argparse, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

IMG = (".jpg", ".jpeg", ".png", ".webp")
LOGS = (".log", ".out", ".err")
TMP = (".tmp", ".part")
NEVER_DIRS = {"chrome-profile", "profile", "venv", ".venv", ".git", "bak", "bak_menu", "node_modules"}
NEVER_FILES = {"crawler.env", "creds.env", ".env", "slack_webhook", "STOP", "STOP_ALL", "SAFE_DONE", "SAFE_PREP",
               "MENU_DONE"}
NEVER_EXT = (".env", ".json", ".jsonl", ".py", ".sh", ".ps1", ".cmd", ".plist", ".md", ".txt", ".tsv", ".tgz", ".zip")
SETTLE = 3600          # SAFE_DONE phải cũ hơn 1 giờ
LOW_GAP = 1800         # ổ thấp: tối đa 1 lần dọn / 30 phút
TMP_AGE = 86400
CHROME_PROFILE = "chrome-profile"
# Allowlist: the ONLY folders inside chrome-profile that may be emptied. Everything else in the profile (History,
# Cookies, Login Data, Local State, Preferences, IndexedDB, ...) is never touched.
CHROME_CACHE_DIRS = (("Default", "Cache"), ("Default", "Code Cache"))
DEFAULT_CDP_PORT = 9222


def top_space_users(root, env=None):
    """Văn bản ngắn: thứ đang chiếm ổ/RAM (host_usage.report). Test thay hàm này."""
    import host_usage
    return host_usage.report(root, env)


def env_num(env, name, default):
    try:
        return float(env.get(name, "") or default)
    except ValueError:
        return default


def options(env=None, **over):
    env = os.environ if env is None else env
    o = {"every_h": env_num(env, "AQD_CLEANUP_EVERY_H", 6), "min_free_gb": env_num(env, "AQD_CLEANUP_MIN_FREE_GB", 8),
         "cache_days": env_num(env, "AQD_CLEANUP_CACHE_DAYS", 7), "log_days": env_num(env, "AQD_CLEANUP_LOG_DAYS", 14),
         "log_max_mb": env_num(env, "AQD_CLEANUP_LOG_MAX_MB", 50), "log_keep_mb": env_num(env, "AQD_CLEANUP_LOG_KEEP_MB", 5),
         "alert_h": env_num(env, "AQD_CLEANUP_ALERT_H", 6),
         "chrome_cache": (env.get("AQD_CLEANUP_CHROME_CACHE") or "low").strip().lower()}
    o.update({k: v for k, v in over.items() if v is not None})
    o.setdefault("foody_data", None)
    return o


def is_safe(path, root):
    """Đường dẫn thật nằm trong root, không phải symlink, không thuộc vùng cấm."""
    if os.path.islink(path) or not os.path.isfile(path):
        return False
    real, base = os.path.realpath(path), os.path.realpath(root)
    if os.path.commonpath([real, base]) != base:
        return False
    parts = os.path.relpath(real, base).split(os.sep)
    if any(p in NEVER_DIRS for p in parts[:-1]):
        return False
    name = parts[-1]
    if name in NEVER_FILES or name.startswith("."):
        return False
    low = name.lower()
    if low.endswith(NEVER_EXT) and not low.endswith(LOGS):
        return False
    return True


def _size(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def _mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0


def _read_json(p, default):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _files(d):
    try:
        return [os.path.join(d, n) for n in sorted(os.listdir(d)) if os.path.isfile(os.path.join(d, n))]
    except OSError:
        return []


def plan_logs_tmp(d, now, o):
    out = []
    for p in _files(d):
        low = os.path.basename(p).lower()
        age = now - _mtime(p)
        rotated = any(f"{e}." in low for e in LOGS) and not low.endswith(LOGS)
        if rotated and age > o["log_days"] * 86400:
            out.append(("delete", p, _size(p), "log cũ (đã xoay)"))
        elif low.endswith(LOGS):
            if age > o["log_days"] * 86400:
                if _size(p) > 0:  # cắt về rỗng, không xoá: launchd/Task Scheduler có thể vẫn giữ file mở
                    out.append(("empty", p, _size(p), "log cũ"))
            elif low.endswith(LOGS) and _size(p) > o["log_max_mb"] * 1024 ** 2:
                out.append(("trim", p, _size(p) - int(o["log_keep_mb"] * 1024 ** 2), "log lớn (cắt đuôi)"))
        elif low.endswith(TMP) and age > TMP_AGE:
            out.append(("delete", p, _size(p), "file tạm"))
    return out


def plan_gmaps(root, now, o):
    out = []
    retry = {str(k) for k in (_read_json(os.path.join(root, "photo_retry.json"), {}) or {})}
    data = os.path.join(root, "aqd_data")
    try:
        ids = sorted(os.listdir(data))
    except OSError:
        ids = []
    for i in ids:
        d = os.path.join(data, i)
        done = os.path.join(d, "SAFE_DONE")
        if not i.isdigit() or i in retry or not os.path.isfile(done) or now - _mtime(done) < SETTLE:
            continue
        for a, dirs, fs in os.walk(d):
            dirs[:] = [x for x in dirs if x not in NEVER_DIRS]
            for n in fs:
                if n.lower().endswith(IMG):
                    p = os.path.join(a, n)
                    out.append(("delete", p, _size(p), "ảnh đã đẩy (SAFE_DONE)"))
    cache = os.path.join(root, "menu_ocr_cache")
    for p in _files(cache):
        if p.lower().endswith(IMG) and now - _mtime(p) > o["cache_days"] * 86400:
            out.append(("delete", p, _size(p), "cache ảnh menu"))
    out += plan_logs_tmp(root, now, o)
    return out


def plan_foody(root, now, o):
    out = []
    data = o.get("foody_data") or os.path.join(root, "foody_data")
    cleaned = _read_json(os.path.join(data, "_cleaned.json"), {}) or {}
    for fid in sorted(str(k) for k in cleaned):
        pd = os.path.join(data, fid, "photos")
        for p in _files(pd):
            if p.lower().endswith(IMG):
                out.append(("delete", p, _size(p), "ảnh đã lên server (_cleaned)"))
    out += plan_logs_tmp(data, now, o)
    out += plan_logs_tmp(root, now, o)
    return out


PROFILES = {"gmaps": plan_gmaps, "foody": plan_foody}


def cdp_port(root, env=None):
    """AQD_CDP_PORT > aqd_config.json cdp_port > 9222 (same order as start_fetcher.ps1)."""
    env = os.environ if env is None else env
    for v in (env.get("AQD_CDP_PORT"), (_read_json(os.path.join(root, "aqd_config.json"), {}) or {}).get("cdp_port")):
        try:
            if v not in (None, ""):
                return int(v)
        except (TypeError, ValueError):
            pass
    return DEFAULT_CDP_PORT


def _norm(p):
    return str(p or "").replace("\\", "/").rstrip("/").lower()


def port_open(port, host="127.0.0.1", timeout=1.0):
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def chrome_state(root, port, lister=None, port_check=port_open):
    """'running' / 'stopped' / 'unknown' for THIS root's crawler Chrome. Running = a process whose command line has
    --user-data-dir=<root>/chrome-profile or --remote-debugging-port=<port>, or the CDP port answers.
    'unknown' (process list unreadable) must be treated like 'running' by callers."""
    if port_check(port):
        return "running"
    if lister is None:
        import host_usage
        lister = host_usage.list_commands
    procs = lister()
    if procs is None:
        return "unknown"
    prof = _norm(os.path.join(os.path.abspath(root), CHROME_PROFILE))
    flag = f"--remote-debugging-port={port}"
    for _pid, cmd in procs:
        c = _norm(cmd)
        if flag in c or prof in c:
            return "running"
    return "stopped"


def _cache_dirs(root):
    base = os.path.join(os.path.realpath(root), CHROME_PROFILE)
    return [os.path.join(base, *parts) for parts in CHROME_CACHE_DIRS]


def is_chrome_cache_file(path, root):
    """True only for a regular file (not a symlink) whose real path is inside one of the allowlisted cache folders,
    and the cache folder itself (and chrome-profile) is a real folder, not a link pointing elsewhere."""
    if os.path.islink(path) or not os.path.isfile(path):
        return False
    real = os.path.realpath(path)
    for d in _cache_dirs(root):
        if os.path.islink(d) or os.path.realpath(d) != d:
            continue
        try:
            if os.path.commonpath([real, d]) == d and real != d:
                return True
        except ValueError:  # different drives on Windows
            continue
    return False


def plan_chrome_cache(root):
    out = []
    for d in _cache_dirs(root):
        if not os.path.isdir(d) or os.path.islink(d) or os.path.realpath(d) != d:
            continue
        for a, dirs, fs in os.walk(d, followlinks=False):
            dirs[:] = [x for x in dirs if not os.path.islink(os.path.join(a, x))]
            for n in fs:
                p = os.path.join(a, n)
                out.append(("cache", p, _size(p), "cache Chrome (Chrome crawler đang tắt)"))
    return out


def state_dir_for(crawler, root, env=None):
    env = os.environ if env is None else env
    if crawler == "foody":
        return env.get("AQD_FOODY_DATA") or os.path.join(root, "foody_data")
    return root


def free_gb(path):
    try:
        import shutil
        return shutil.disk_usage(path).free / 1024 ** 3
    except OSError:
        return None


def decide(state, now, free, o, force=False):
    """-> (run?, trigger, low?)"""
    last = float(state.get("last_run") or 0)
    low = free is not None and o["min_free_gb"] > 0 and free < o["min_free_gb"]
    if force:
        return True, "chạy tay", low
    if low and now - last >= LOW_GAP:
        return True, f"ổ còn {free:.1f} GB", True
    if now - last >= o["every_h"] * 3600:
        return True, "định kỳ", low
    return False, "", low


def trim(p, keep):
    with open(p, "rb") as f:
        f.seek(0, 2); n = f.tell(); f.seek(max(0, n - keep)); tail = f.read()
    nl = tail.find(b"\n")
    tail = tail[nl + 1:] if 0 <= nl < len(tail) - 1 else tail
    with open(p, "r+b") as f:
        f.truncate(0); f.seek(0); f.write(b"[disk_cleanup] cat bot log cu\n" + tail)


def execute(plan, root, o, dry_run, say=print):
    freed = files = 0
    for act, p, size, why in plan:
        if not (is_chrome_cache_file(p, root) if act == "cache" else is_safe(p, root)):
            say(f"BỎ QUA (vùng cấm) {p}"); continue
        if dry_run:
            say(f"[dry-run] {act} {p} ({size} B, {why})"); freed += max(0, size); files += 1; continue
        try:
            if act in ("trim", "empty"):
                before = _size(p)
                if act == "trim":
                    trim(p, int(o["log_keep_mb"] * 1024 ** 2))
                else:
                    with open(p, "r+b") as f:
                        f.truncate(0)
                freed += max(0, before - _size(p))
            else:
                os.remove(p); freed += size
            files += 1
        except OSError as e:
            say(f"lỗi {act} {p}: {type(e).__name__}")
    return freed, files


class Lock:
    def __init__(self, path, stale=7200):
        self.path, self.stale, self.ok = path, stale, False

    def __enter__(self):
        if os.path.exists(self.path) and time.time() - _mtime(self.path) > self.stale:
            try: os.remove(self.path)
            except OSError: pass
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY); os.write(fd, str(os.getpid()).encode()); os.close(fd)
            self.ok = True
        except FileExistsError:
            self.ok = False
        return self

    def __exit__(self, *a):
        if self.ok:
            try: os.remove(self.path)
            except OSError: pass


def chrome_cache_step(root, o, low, dry_run, env, say, state_fn=None):
    """(freed, files) from emptying the allowlisted Chrome cache folders - only when allowed by
    AQD_CLEANUP_CHROME_CACHE and the crawler Chrome is NOT running (checked right before deleting)."""
    mode = o.get("chrome_cache", "low")
    if mode in ("0", "off", "no") or (mode == "low" and not low):
        return 0, 0
    if not any(os.path.isdir(d) for d in _cache_dirs(root)):
        return 0, 0
    state_fn = state_fn or (lambda: chrome_state(root, cdp_port(root, env)))
    state = state_fn()
    if state != "stopped":
        say(f"cache Chrome: bỏ qua (Chrome crawler {'đang chạy' if state == 'running' else 'không rõ trạng thái'})")
        return 0, 0
    return execute(plan_chrome_cache(root), root, o, dry_run, say)


def run(crawler, root, dry_run=False, force=False, now=None, notifier=None, env=None, say=print,
        chrome_state_fn=None, **over):
    now = time.time() if now is None else now
    o = options(env, **over)
    sd = state_dir_for(crawler, root, env)
    o["foody_data"] = sd if crawler == "foody" else None
    os.makedirs(sd, exist_ok=True)
    stp = os.path.join(sd, "cleanup_state.json")
    st = _read_json(stp, {}) or {}
    free = free_gb(sd)
    go, trigger, low = decide(st, now, free, o, force or dry_run)
    if not go:
        return {"ran": False}
    if low:
        o["log_days"] = min(o["log_days"], 3)  # cache menu giữ nguyên AQD_CLEANUP_CACHE_DAYS (7), không hạ khi ổ thấp
    with Lock(os.path.join(sd, "cleanup.lock")) as lk:
        if not lk.ok:
            say("đang có lượt dọn khác -> bỏ qua"); return {"ran": False}
        plan = PROFILES[crawler](root, now, o)
        freed, files = execute(plan, root, o, dry_run, say)
        if crawler == "gmaps":
            cf, cn = chrome_cache_step(root, o, low, dry_run, os.environ if env is None else env, say, chrome_state_fn)
            freed += cf; files += cn
    after = free_gb(sd)
    say(f"{'[dry-run] ' if dry_run else ''}{crawler}: {trigger}; {files} file, {freed / 1024 ** 3:.2f} GB"
        + (f"; còn trống {after:.1f} GB" if after is not None else ""))
    if not dry_run:
        st.update(last_run=now, last_freed=freed, last_files=files)
        tmp = stp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f: json.dump(st, f)
        os.replace(tmp, stp)
        nothing = freed <= 0  # 0 byte giải phóng (vd chỉ cắt log đã rỗng) = không có gì để báo
        send_alert = nothing and low and now - float(st.get("last_alert") or 0) >= o["alert_h"] * 3600
        if nothing and not send_alert:
            say("slack: bỏ qua (không giải phóng gì)")
            return {"ran": True, "freed": freed, "files": files, "trigger": trigger, "plan": plan}
        try:
            if notifier is None:
                import slack_notify
                notifier = slack_notify.Notifier(crawler, sd)
            if send_alert:
                st["last_alert"] = now
                with open(tmp, "w", encoding="utf-8") as f: json.dump(st, f)
                os.replace(tmp, stp)
                top = top_space_users(root, os.environ if env is None else env)
                say(f"ổ thấp, không còn gì để dọn; chỗ đang chiếm: {top}")
                ok = notifier.disk_low_nothing(after, top)
            else:
                ok = notifier.cleanup(freed, files, after, trigger)
            res = getattr(notifier, "last_result", None) or ("ok" if ok else "lỗi")
            say(f"slack: {res}")  # ok / http <code> / lỗi; never the webhook URL
        except Exception as e:  # noqa: BLE001 - Slack never breaks the cleanup
            say(f"slack: lỗi {type(e).__name__}")
    return {"ran": True, "freed": freed, "files": files, "trigger": trigger, "plan": plan}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--crawler", choices=sorted(PROFILES), required=True)
    ap.add_argument("--root", required=True, help="gmaps: ~/aqd | C:\\aqd ; foody: ~/script_foody | C:\\script_foody")
    ap.add_argument("--dry-run", action="store_true", help="chỉ liệt kê, không xoá, không gửi Slack")
    ap.add_argument("--force", action="store_true", help="dọn ngay, bỏ qua lịch")
    ap.add_argument("--log", help="ghi log vào file này")
    a = ap.parse_args(argv)
    # Windows console (cp1252/cp437) không in được tiếng Việt -> --dry-run trên Admin chết UnicodeEncodeError.
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    out = open(a.log, "a", encoding="utf-8") if a.log else None
    def say(s):
        line = f"{time.strftime('%F %T')} {s}"
        (out.write(line + "\n") if out else print(line, flush=True))
        if out: out.flush()
    run(a.crawler, os.path.abspath(a.root), dry_run=a.dry_run, force=a.force, say=say)
    return 0


if __name__ == "__main__":
    sys.exit(main())
