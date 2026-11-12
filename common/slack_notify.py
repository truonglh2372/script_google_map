#!/usr/bin/env python3
"""Crawler -> Slack status reports (truonglh2372/an_uong_quanh_day#1388). Stdlib only; the same file lives in
script_foody/common and script_google_map/common (keep them identical).

Each crawler posts straight to ONE shared Slack channel through an Incoming Webhook (no server side):
  - a report every AQD_REPORT_EVERY_H hours (default 4) from a timer thread in the long-running process,
  - a message when it starts and when it stops normally,
  - an immediate alert on a severe error (captcha, sign-in needed, disk < AQD_DISK_MIN_GB, crash,
    AQD_EMPTY_PAGES_ALERT empty list pages in a row) and when nothing new came in for AQD_STUCK_HOURS (default 3),
  - at most one alert per crawler / host / error code every AQD_ALERT_EVERY_H hours (default 4), kept in
    <state dir>/slack_state.json so it holds across restarts and across short-lived processes.

Config (never committed, never logged):
  SLACK_CRAWLER_WEBHOOK_URL   the webhook; empty -> the file SLACK_CRAWLER_WEBHOOK_FILE, else ~/aqd/crawler.env
                              (Windows C:\\aqd\\crawler.env) when it exists, else
                              ~/.config/aqd/slack_webhook (Mac/Linux) or %APPDATA%\\aqd\\slack_webhook (Windows).
                              Missing -> one warning in the log, the crawler keeps running, nothing is sent.
  SLACK_ALERT_MENTION         a Slack user id (U.../W...) mentioned on red alerts; empty -> no mention.
  AQD_HOST_LABEL              machine name shown in the messages (default: hostname).
  AQD_SLACK=0                 turn everything off.
A failed send (network, timeout, HTTP error) is logged without the URL and swallowed: it never stops the crawler.
"""
import json, os, re, shutil, socket, subprocess, sys, threading, time, traceback, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone

ICT = timezone(timedelta(hours=7))
CODES = ("google_cutoff", "captcha", "signin_required", "rate_limited", "foody_empty_pages", "http_503", "disk_low",
         "upload_fail", "crash", "stuck")
SEVERE = ("captcha", "signin_required", "disk_low", "crash", "foody_empty_pages")
COUNTS = ("places", "photos", "reviews", "dishes", "upload_ok", "upload_fail", "errors")
PROGRESS = ("places", "photos", "reviews", "dishes")
TITLES = {"crash": "process thoát do exception", "captcha": "gặp captcha", "signin_required": "cần đăng nhập",
          "disk_low": "ổ đĩa sắp đầy", "foody_empty_pages": "trang rỗng liên tiếp"}
ACTIONS = {"captcha": "giải captcha trên {host}", "signin_required": "cần đăng nhập Google trên {host}",
           "disk_low": "dọn ổ đĩa trên {host}",
           "foody_empty_pages": "kiểm tra Foody có chặn IP {host} không / đổi parity hoặc tạm dừng",
           "crash": "watchdog sẽ khởi động lại trong ≤15 phút; nếu lặp lại, xem log trên {host}",
           "google_cutoff": "Google đang chặn {host}; chờ hết lượt chặn hoặc đổi mạng",
           "stuck": "xem log; khởi động lại crawler trên {host}"}
USER_ID_RE = re.compile(r"^<?@?([UW][A-Z0-9]{2,30})>?$")


def log(msg):
    try:
        print(f"[slack] {msg}", flush=True)
    except Exception:
        pass


def _float_env(env, name, default):
    try:
        value = float(env.get(name, "") or default)
        return value if value > 0 else default
    except ValueError:
        return default


def crawler_env_file(env=os.environ):
    """The per-machine editable file (an_uong_quanh_day#1388): ~/aqd/crawler.env, C:\\aqd\\crawler.env on Windows.
    KEY=VALUE lines, # comments. AQD_CRAWLER_ENV overrides the path."""
    if env.get("AQD_CRAWLER_ENV"):
        return env["AQD_CRAWLER_ENV"]
    return r"C:\aqd\crawler.env" if os.name == "nt" else os.path.join(os.path.expanduser("~"), "aqd", "crawler.env")


def default_secret_file(env=os.environ):
    """SLACK_CRAWLER_WEBHOOK_FILE, else crawler.env when it exists, else the old ~/.config/aqd/slack_webhook.
    Read again by every Notifier (crawler start, every watchdog run)."""
    if env.get("SLACK_CRAWLER_WEBHOOK_FILE"):
        return env["SLACK_CRAWLER_WEBHOOK_FILE"]
    if os.path.isfile(crawler_env_file(env)):
        return crawler_env_file(env)
    if os.name == "nt":
        return os.path.join(env.get("APPDATA") or os.path.expanduser("~"), "aqd", "slack_webhook")
    return os.path.join(os.path.expanduser("~"), ".config", "aqd", "slack_webhook")


FILE_KEYS = ("SLACK_CRAWLER_WEBHOOK_URL", "SLACK_ALERT_MENTION", "AQD_HOST_LABEL")


def read_secret_file(env=os.environ):
    """The local secret file: either just the webhook URL, or KEY=VALUE lines (SLACK_CRAWLER_WEBHOOK_URL,
    SLACK_ALERT_MENTION, AQD_HOST_LABEL) so Task Scheduler / launchd jobs need no environment. {} if unreadable."""
    try:
        with open(default_secret_file(env), encoding="utf-8-sig") as f:
            lines = [x.strip() for x in f.read().splitlines() if x.strip() and not x.strip().startswith("#")]
    except OSError:
        return {}
    out = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip() in FILE_KEYS:
            out[key.strip()] = value.strip().strip('"').strip("'")
        elif not sep or re.match(r"^https?://", line):
            out.setdefault("SLACK_CRAWLER_WEBHOOK_URL", line)
    return out


def settings(env=os.environ):
    """env wins over the secret file, key by key."""
    merged = dict(read_secret_file(env))
    for key in FILE_KEYS:
        if (env.get(key) or "").strip():
            merged[key] = env[key].strip()
    for key, value in env.items():
        merged.setdefault(key, value)
    return merged


def webhook_url(env=os.environ):
    """The webhook URL from env, else the local secret file; '' when none or not http(s)."""
    url = (settings(env).get("SLACK_CRAWLER_WEBHOOK_URL") or "").strip()
    return url if re.match(r"^https?://\S+$", url) else ""


def mention(env=os.environ):
    m = USER_ID_RE.match((env.get("SLACK_ALERT_MENTION") or "").strip())
    return f"<@{m.group(1)}> " if m else ""


def hhmm(ts):
    return datetime.fromtimestamp(ts, ICT).strftime("%H:%M")


def ddmm_hhmm(ts):
    return datetime.fromtimestamp(ts, ICT).strftime("%d/%m %H:%M")


def ddmm(ts):
    return datetime.fromtimestamp(ts, ICT).strftime("%d/%m")


def num(n):
    """Vietnamese thousands separator: 37208 -> 37.208."""
    return f"{int(n):,}".replace(",", ".")


def gb(value):
    return f"{value:.1f}".replace(".", ",") + " GB"


def duration(seconds):
    m = int(max(0, seconds) // 60)
    return f"{m // 60}h{m % 60:02d}" if m >= 60 else f"{m} phút"


def _clean(text, limit=300):
    """One line, no webhook-ish URLs, cut to limit."""
    text = re.sub(r"https://hooks\.slack\.com/\S+", "[webhook]", str(text or ""))
    text = " ".join(text.split())
    return text[:limit]


# Result of the last post_json: "ok", "http <code>", "URLError(<reason>)" or the exception class name.
# Never the URL or the payload.
LAST_RESULT = ""
URLERROR_RETRY_WAIT = 5.0  # one retry after a connect-level failure (DNS / refused / connect timeout)


def _reason(e):
    """Short, URL-free description of a URLError: reason class + errno/message, e.g. URLError(gaierror 11001)."""
    r = getattr(e, "reason", None)
    if r is None:
        return type(e).__name__
    errno = getattr(r, "errno", None)
    detail = str(errno) if errno is not None else (_clean(str(r), 80) if isinstance(r, str) else "")
    return f"{type(e).__name__}({type(r).__name__}{' ' + detail if detail else ''})"


def post_json(url, payload, timeout=10, sleep=time.sleep):
    """POST the payload; True on 2xx. Never raises. Sets LAST_RESULT.

    A URLError means the request never got an HTTP answer (DNS, refused, connect timeout, TLS), so it is retried
    once after URLERROR_RETRY_WAIT seconds; HTTP errors and read timeouts are not retried (no duplicate posts)."""
    global LAST_RESULT
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                code = getattr(r, "status", None) or r.getcode()
            if 200 <= int(code) < 300:
                LAST_RESULT = "ok" if attempt == 1 else "ok (lần 2)"
                return True
            LAST_RESULT = f"http {code}"
        except urllib.error.HTTPError as e:
            LAST_RESULT = f"http {e.code}"
        except urllib.error.URLError as e:
            LAST_RESULT = _reason(e)
            if attempt == 1:
                log(f"gửi Slack lỗi: {LAST_RESULT} -> thử lại sau {URLERROR_RETRY_WAIT:g}s")
                sleep(URLERROR_RETRY_WAIT)
                continue
        except Exception as e:  # noqa: BLE001 - Slack must never break the crawler
            LAST_RESULT = type(e).__name__
        break
    log(f"gửi Slack lỗi: {LAST_RESULT}")
    return False


class _FileLock:
    """Tiny cross-process lock (O_EXCL lock file, stale after 30 s). Gives up waiting after 5 s and goes on."""

    def __init__(self, path):
        self.path, self.fd = path, None

    def __enter__(self):
        deadline = time.time() + 5
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > 30:
                        os.remove(self.path)
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    return self
                time.sleep(0.05)
            except OSError:
                return self

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                os.close(self.fd)
                os.remove(self.path)
            except OSError:
                pass
            self.fd = None


class Notifier:
    """One per crawler process. Counters and alert times live in <state_dir>/slack_state.json, so short-lived
    processes (gmaps safe_run passes) add up and the long-running one (timer=True) reports them."""

    def __init__(self, crawler, state_dir, env=None, now=time.time, sender=None, disk_dir=None, labels=None,
                 version=None, scheduler="launchd" if sys.platform == "darwin" else "Task Scheduler"):
        self.env = settings(os.environ if env is None else env)
        self.crawler = crawler
        self.state_dir = state_dir
        self.state_file = os.path.join(state_dir, "slack_state.json")
        self.lock = threading.RLock()
        self.now = now
        self.host = (self.env.get("AQD_HOST_LABEL") or "").strip() or socket.gethostname()
        self.url = webhook_url(self.env)
        self.enabled = (self.env.get("AQD_SLACK", "1").strip() != "0") and bool(self.url)
        self.sender = sender or post_json
        self.last_result = None
        self.disk_dir = disk_dir or state_dir
        self.every = _float_env(self.env, "AQD_REPORT_EVERY_H", 4) * 3600
        self.alert_every = _float_env(self.env, "AQD_ALERT_EVERY_H", 4) * 3600
        self.stuck_after = _float_env(self.env, "AQD_STUCK_HOURS", 3) * 3600
        self.empty_alert = int(_float_env(self.env, "AQD_EMPTY_PAGES_ALERT", 10))
        self.disk_min = _float_env(self.env, "AQD_DISK_MIN_GB", 5)
        self.labels = {"places": "quán", "photos": "ảnh", "reviews": "review"}
        self.labels.update(labels or {})
        self.version = version
        self.scheduler = scheduler
        self.sources = []
        self.thread = None
        self.stopping = threading.Event()
        self.sent = []  # (kind, text) of every message handed to the sender, for logs/tests
        if not self.url and self.env.get("AQD_SLACK", "1").strip() != "0":
            log("chưa cấu hình SLACK_CRAWLER_WEBHOOK_URL / file secret -> không gửi Slack, crawler vẫn chạy")

    # ---- state ---------------------------------------------------------------------------------------------------
    def _load(self):
        try:
            with open(self.state_file, encoding="utf-8") as f:
                st = json.load(f)
            return st if isinstance(st, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, st):
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = f"{self.state_file}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False)
            os.replace(tmp, self.state_file)
        except OSError:
            pass

    def _update(self, fn):
        with self.lock, _FileLock(self.state_file + ".lock"):
            st = self._load()
            out = fn(st)
            self._save(st)
            return out

    # ---- message plumbing ----------------------------------------------------------------------------------------
    def _title(self):
        return f"*{self.crawler} @ {self.host}*"

    def _send(self, kind, text):
        global LAST_RESULT
        self.sent.append((kind, text))
        if not self.enabled:
            self.last_result = "tắt (AQD_SLACK=0)" if self.url else "chưa cấu hình webhook"
            return False
        LAST_RESULT = ""
        try:
            ok = bool(self.sender(self.url, {"text": text, "unfurl_links": False, "unfurl_media": False}))
            # last_result: "ok" / "http <code>" / exception name - safe to log (never the URL).
            self.last_result = "ok" if ok else (LAST_RESULT or "lỗi")
            return ok
        except Exception as e:  # noqa: BLE001
            self.last_result = type(e).__name__
            log(f"gửi Slack lỗi: {type(e).__name__}")
            return False

    def _allow(self, st, code):
        """Anti-spam: one alert per crawler/host/code every alert_every seconds."""
        alerts = st.setdefault("alerts", {})
        last = float(alerts.get(code) or 0)
        if self.now() - last < self.alert_every:
            return False
        alerts[code] = self.now()
        return True

    def _counts_text(self, counts):
        c = {k: int((counts or {}).get(k, 0)) for k in COUNTS}
        parts = [f"{self.labels['places']} +{num(c['places'])}", f"{self.labels['photos']} +{num(c['photos'])}",
                 f"{self.labels['reviews']} +{num(c['reviews'])}"]
        if "dishes" in self.labels:
            parts.append(f"{self.labels['dishes']} +{num(c['dishes'])}")
        return " · ".join(parts) + f" · upload ok {num(c['upload_ok'])} / fail {num(c['upload_fail'])}"

    def disk_free_gb(self):
        try:
            return shutil.disk_usage(self.disk_dir).free / 1024 ** 3
        except OSError:
            return None

    def _action(self, code, action=None):
        text = action or ACTIONS.get(code)
        return text.format(host=self.host) if text else None

    # ---- recording (any process) ---------------------------------------------------------------------------------
    def count(self, **delta):
        delta = {k: int(v) for k, v in delta.items() if k in COUNTS and int(v or 0) > 0}
        if not delta:
            return

        def fn(st):
            for bucket in ("win", "run"):
                counts = st.setdefault(bucket, {"start": self.now()}).setdefault("counts", {})
                for k, v in delta.items():
                    counts[k] = int(counts.get(k, 0)) + v
            if any(k in PROGRESS for k in delta):
                st["last_progress"] = st["last_success"] = self.now()
                st.pop("stuck_since", None)
            elif delta.get("upload_ok"):
                st["last_success"] = self.now()
        self._update(fn)

    def record_place(self, ok, photos=0, reviews=0, dishes=0):
        if ok:
            self.count(places=1, photos=photos, reviews=reviews, dishes=dishes)
        else:
            self.count(errors=1)

    def position(self, text):
        text = _clean(text, 150)
        self._update(lambda st: st.__setitem__("position", text))

    def track_totals(self, fn):
        """fn() -> cumulative {count name: total}; the timer adds what grew since the last look."""
        self.sources.append([fn, None])

    def sync_totals(self):
        for src in self.sources:
            try:
                cur = {k: int(v) for k, v in (src[0]() or {}).items() if k in COUNTS}
            except Exception:  # noqa: BLE001
                continue
            if src[1] is not None:
                self.count(**{k: v - src[1].get(k, 0) for k, v in cur.items() if v > src[1].get(k, 0)})
            src[1] = cur

    def empty_page(self, label=""):
        """One list page given up as empty. AQD_EMPTY_PAGES_ALERT in a row -> severe foody_empty_pages."""
        def fn(st):
            st["empty_streak"] = int(st.get("empty_streak") or 0) + 1
            if st["empty_streak"] == 1:
                st["empty_first"] = label
            return st["empty_streak"], st.get("empty_first") or label
        streak, first = self._update(fn)
        if streak >= self.empty_alert:
            span = f" ({first}→{label})" if first and label else ""
            self.error("foody_empty_pages", f"{streak} trang list liên tiếp trả \"0 quán\"{span}")

    def page_ok(self):
        self._update(lambda st: (st.pop("empty_streak", None), st.pop("empty_first", None)))

    def error(self, code, detail="", action=None, severe=None):
        """Remember the last error; severe ones (or severe=True) alert right away, deduped per code."""
        severe = code in SEVERE if severe is None else severe
        detail = _clean(detail)
        act = self._action(code, action)

        def fn(st):
            st["last_error"] = {"code": code, "detail": detail, "at": self.now(), "action": act, "severe": severe}
            return severe and self._allow(st, code)
        if not self._update(fn):
            return False
        st = self._load()
        if code == "foody_empty_pages":
            body = [f"• {detail}"]
        else:
            body = [f"• Lỗi: `{code}` — {detail}" if detail else f"• Lỗi: `{code}`"]
            pos = st.get("position")
            last = st.get("last_success")
            body.append("• Vị trí: " + (pos or "không rõ")
                        + (f" · thành công cuối {hhmm(last)} (UTC+7)" if last else ""))
        body.append(f"• Việc cần làm: *{act}*" if act else "• Việc cần làm: không")
        title = f"`{code}`" if code in ("foody_empty_pages",) else TITLES.get(code, f"`{code}`")
        text = (f"🔴 {mention(self.env)}{self._title()} — LỖI: {title}\n" + "\n".join(body)
                + f"\n_{ddmm_hhmm(self.now())} (UTC+7) · cảnh báo `{code}` tiếp theo cho {self.crawler}@{self.host} "
                  f"sớm nhất {hhmm(self.now() + self.alert_every)}_")
        return self._send("severe", text)

    def crash(self, exc=None):
        if exc is None:
            exc = sys.exc_info()[1]
        where = ""
        tb = getattr(exc, "__traceback__", None)
        if tb is not None:
            frame = traceback.extract_tb(tb)[-1]
            where = f" tại {os.path.basename(frame.filename)}:{frame.lineno}"
        detail = f"`{type(exc).__name__}: {_clean(exc, 160)}`{where}" if exc else "không rõ"
        return self.error("crash", detail)

    def cleanup(self, freed_bytes, files, free_gb=None, trigger="định kỳ"):
        """Một dòng sau mỗi lượt tự dọn ổ (common/disk_cleanup.py). Không chống spam: job tự giới hạn tần suất."""
        free = self.disk_free_gb() if free_gb is None else free_gb
        text = (f"🧹 {self._title()} — tự dọn ổ ({trigger}): giải phóng {gb(max(0, freed_bytes) / 1024 ** 3)}"
                f" ({num(files)} file)" + (f" · còn trống {gb(free)}" if free is not None else "")
                + f" · {ddmm_hhmm(self.now())} (UTC+7)")
        return self._send("cleanup", text)

    def disk_low_nothing(self, free_gb=None, top=""):
        """disk_low: ổ thấp nhưng thư mục crawler không còn gì để dọn -> nói rõ cái gì đang chiếm ổ / RAM
        (common/host_usage.py). Job dọn tự giới hạn 1 lần / AQD_CLEANUP_ALERT_H giờ."""
        free = self.disk_free_gb() if free_gb is None else free_gb
        text = (f"⚠️ {mention(self.env)}{self._title()} — `disk_low`: ổ thấp" + (f" (còn {gb(free)})" if free is not None else "")
                + ", tự dọn không giải phóng được gì (0 byte) — thư mục crawler không còn gì để dọn.\n"
                + "• Chỗ đang chiếm: " + (_clean(top, 1500) or "không đo được") + "\n"
                + f"• Việc cần làm: *{ACTIONS.get('disk_low', 'dọn ổ đĩa').format(host=self.host)}*\n"
                + f"_{ddmm_hhmm(self.now())} (UTC+7)_")
        return self._send("cleanup", text)

    # ---- lifecycle (long-running process) ------------------------------------------------------------------------
    def start(self, command="", extra=None):
        def fn(st):
            st["run"] = {"start": self.now(), "pid": os.getpid(), "counts": {}}
            st.setdefault("win", {"start": self.now(), "counts": {}})
            st["last_progress"] = max(float(st.get("last_progress") or 0), self.now())
            st["next_report"] = self.now() + self.every
            st["alive_at"] = self.now()
            st.pop("stopped", None)
            return st.get("position")
        pos = self._update(fn)
        free = self.disk_free_gb()
        line = "• Lệnh: " + (f"`{_clean(command, 120)}`" if command else "không rõ")
        if self.version:
            line += f" · bản {self.version}"
        tail = [x for x in [f"Tiếp tục từ: {pos}" if pos else None, extra,
                            f"ổ đĩa trống {gb(free)}" if free is not None else None] if x]
        text = (f"▶️ {self._title()} — bắt đầu chạy\n{line}\n" + (f"• {' · '.join(tail)}\n" if tail else "")
                + f"_{ddmm_hhmm(self.now())} (UTC+7) · báo cáo {int(self.every // 3600)}h đầu tiên lúc "
                  f"~{hhmm(self.now() + self.every)}_")
        return self._send("start", text)

    def stop(self, reason="hết việc"):
        self.stopping.set()
        self.sync_totals()

        def fn(st):
            run = st.get("run") or {}
            st["stopped"] = self.now()
            return run, st.get("position")
        run, pos = self._update(fn)
        started = float(run.get("start") or self.now())
        text = (f"⚫ {self._title()} — đã dừng ({_clean(reason, 80)})\n"
                f"• Lần chạy này ({hhmm(started)}–{hhmm(self.now())}, {duration(self.now() - started)}): "
                f"{self._counts_text(run.get('counts'))}\n"
                + (f"• Dừng tại: {pos}\n" if pos else "")
                + f"_{ddmm_hhmm(self.now())} (UTC+7)_")
        return self._send("stop", text)

    def status(self, st):
        err = st.get("last_error") or {}
        if err.get("severe") and float(err.get("at") or 0) > float(st.get("last_progress") or 0) \
                and self.now() - float(err.get("at") or 0) < self.every:
            return "🔴", "lỗi"
        if self.now() - float(st.get("last_progress") or self.now()) >= self.stuck_after:
            return "🟡", "kẹt"
        return "🟢", "đang chạy"

    def report(self):
        """The periodic report; resets the window counters."""
        self.sync_totals()

        def fn(st):
            win = st.get("win") or {"start": self.now(), "counts": {}}
            st["win"] = {"start": self.now(), "counts": {}}
            st["last_report"] = self.now()
            st["next_report"] = self.now() + self.every
            return dict(st), win
        st, win = self._update(fn)
        emoji, word = self.status(st)
        err = st.get("last_error") or {}
        last = st.get("last_success")
        if err:
            err_line = f"`{err['code']}` {hhmm(err['at'])}" + (f" — {err['detail']}" if err.get("detail") else "")
        else:
            err_line = "không"
        act = err.get("action") if err and (emoji != "🟢" or err.get("severe")) else None
        if emoji == "🟡" and not act:
            act = self._action("stuck")
        start = float(win.get("start") or self.now())
        text = (f"{emoji} {self._title()} — {word}\n"
                f"• Vị trí: {st.get('position') or 'không rõ'}\n"
                f"• {int(round((self.now() - start) / 3600)) or 1} giờ qua ({hhmm(start)}–{hhmm(self.now())}): "
                f"{self._counts_text(win.get('counts'))}\n"
                f"• Thành công cuối: {ddmm_hhmm(last) + ' (UTC+7)' if last else 'chưa có'}\n"
                f"• Lỗi gần nhất: {err_line}\n"
                + (f"• Việc cần làm: *{act}*\n" if act else "• Việc cần làm: không\n")
                + f"_Báo cáo lúc {ddmm_hhmm(self.now())} (UTC+7) · tin kế tiếp ~{hhmm(self.now() + self.every)}_")
        return self._send("report", text)

    def check_stuck(self):
        def fn(st):
            since = float(st.get("last_progress") or self.now())
            if self.now() - since < self.stuck_after:
                return None
            if not self._allow(st, "stuck"):
                return None
            st["stuck_since"] = since
            return dict(st)
        st = self._update(fn)
        if not st:
            return False
        since = float(st["last_progress"])
        run = st.get("run") or {}
        err = st.get("last_error") or {}
        hours = self.stuck_after / 3600
        hours_txt = f"{hours:g}"
        pid_txt = f"PID {run.get('pid')}" + (f", từ {ddmm(run['start'])}" if run.get("start") else "") if run else ""
        text = (f"🟡 {self._title()} — KẸT: {hours_txt} giờ không có {self.labels['places']}/"
                f"{self.labels['reviews']} mới\n"
                f"• Process vẫn chạy ({pid_txt}) nhưng 0 item mới từ {hhmm(since)} (UTC+7)\n"
                f"• Đang làm: {st.get('position') or 'không rõ'}\n"
                + (f"• Lỗi gần nhất: `{err['code']}` {hhmm(err['at'])}\n" if err else "• Lỗi gần nhất: không\n")
                + f"• Việc cần làm: *{self._action('stuck')}*\n"
                f"_{ddmm_hhmm(self.now())} (UTC+7) · ngưỡng {hours_txt}h (AQD_STUCK_HOURS) · nhắc lại sớm nhất "
                f"{hhmm(self.now() + self.alert_every)}_")
        return self._send("stuck", text)

    def check_disk(self):
        free = self.disk_free_gb()
        if free is not None and free < self.disk_min:
            return self.error("disk_low", f"Ổ đĩa còn {gb(free)} (< {self.disk_min:g} GB)")
        return False

    def tick(self):
        """One timer step: totals, alive mark, disk, stuck, and the report when due. Never raises."""
        try:
            self.sync_totals()
            st = self._update(lambda s: (s.__setitem__("alive_at", self.now()), dict(s))[1])
            self.check_disk()
            self.check_stuck()
            if self.now() >= float(st.get("next_report") or 0):
                self.report()
        except Exception as e:  # noqa: BLE001
            log(f"tick lỗi: {type(e).__name__}")

    def start_timer(self, every=60):
        def loop():
            while not self.stopping.wait(every):
                self.tick()
        self.thread = threading.Thread(target=loop, name="slack-notify", daemon=True)
        self.thread.start()
        return self.thread

    def install_crash_hook(self):
        """A crash that ends the process sends the red crash alert (then the normal excepthook prints it)."""
        prev = sys.excepthook

        def hook(kind, exc, tb):
            if not issubclass(kind, (KeyboardInterrupt, SystemExit)):
                try:
                    self.crash(exc)
                except Exception:  # noqa: BLE001
                    pass
            prev(kind, exc, tb)
        sys.excepthook = hook


def git_version(path):
    try:
        out = subprocess.run(["git", "-C", path, "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                             timeout=5)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None
