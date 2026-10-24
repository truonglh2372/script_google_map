#!/usr/bin/env python3
"""Crawler heartbeat for the daily Slack ops report (truonglh2372/an_uong_quanh_day#1021).

Contract: the heartbeat table merged in web #1024 (`crawler_heartbeats`) and the endpoint of web #1036
(docs/runbooks/daily-ops-report.md):

    POST {AQD_BASE}/v1/ingest/heartbeats
    Authorization: Bearer $AQD_INGEST_TOKEN   (an ingest client token, Admin -> Ingest clients)
    Idempotency-Key: <fresh per heartbeat>    (a retry with the same key and body returns the first answer)
    {"crawler": "script_foody", "host": "mac-mini", "status": "running", "activity": "...", "range_label": "...",
     "window_seconds": 300, "processed_count": 12, "photo_count": 30, "dish_count": 8, "error_count": 1,
     "rate_limited_count": 0, "disk_free_mb": 51200, "note": "one timeout"}

- status is one of running / idle / blocked / error. Counters are the window since the previous accepted heartbeat.
  Text is cut to the column limits here (activity 200, range_label 100, host 100, note 500).
- No token (AQD_INGEST_TOKEN unset/empty) or AQD_HEARTBEAT=0 -> every call is a no-op.
- Best effort: any failure returns False and is swallowed; it never raises into the crawler and never logs the token.
- Counts accumulate in <state dir>/heartbeat_state.json, so several short-lived processes (one per pass) add up, and a
  failed send keeps them (and the same body with the same Idempotency-Key) for the next try, so a retry is never
  counted twice. New counts start a new heartbeat. A 409/422 answer drops that body (it would be refused forever).
- At most one heartbeat every AQD_HEARTBEAT_EVERY_MIN minutes (default 15) unless force=True. Send one when idle too:
  the report calls a crawler silent after OPS_REPORT_CRAWLER_SILENT_HOURS without a heartbeat.
"""
import json, os, re, shutil, socket, sys, time, urllib.error, urllib.request, uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import origin_pin  # noqa: E402

PATH = "/v1/ingest/heartbeats"
STATUSES = ("running", "idle", "blocked", "error")
# Problems a crawler may note. They go into the note text; rate_limited also counts in rate_limited_count and the
# blocking ones make the status "blocked" unless the caller gives one.
ISSUES = ("rate_limited", "ip_blocked", "captcha", "login_required", "disk_full", "repeated_errors", "network", "other")
BLOCKING = ("ip_blocked", "captcha", "login_required")
COUNTS = ("processed", "photos", "dishes", "errors", "rate_limited")
LIMITS = {"host": 100, "activity": 200, "range_label": 100, "note": 500}
SLUG_RE = re.compile(r"^[a-z0-9_]{2,50}$")
PHOTOS_RE = re.compile(r"photos=\+(\d+)")


def _int_env(name, default):
    try:
        value = int(os.environ.get(name, "") or default)
        return value if value > 0 else default
    except ValueError:
        return default


def _read_env_file(path):
    """KEY=VALUE lines of a creds.env-style file (only AQD_BASE / AQD_INGEST_TOKEN / AQD_ORIGIN_IP are used); {} if
    unreadable."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    if key.strip() in ("AQD_BASE", "AQD_INGEST_TOKEN", "AQD_ORIGIN_IP"):
                        out[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _cut(value, limit):
    value = (value or "").strip()
    return value[:limit] or None


class Heartbeat:
    def __init__(self, crawler, state_dir, base=None, token=None, opener=None, now=time.time, disk_dir=None,
                 env_file=None):
        if not SLUG_RE.match(crawler or ""):
            raise ValueError("crawler must be a slug [a-z0-9_]{2,50}")
        self.crawler = crawler
        self.state_file = os.path.join(state_dir, "heartbeat_state.json")
        file_env = _read_env_file(env_file) if env_file else {}
        base = base if base is not None else os.environ.get("AQD_BASE") or file_env.get("AQD_BASE")
        self.base = (base or "https://anuongquanhday.com").rstrip("/")
        if token is None:
            token = os.environ.get("AQD_INGEST_TOKEN") or file_env.get("AQD_INGEST_TOKEN") or ""
        self.token = token.strip()
        self.enabled = bool(self.token) and os.environ.get("AQD_HEARTBEAT", "1").strip() != "0"
        self.pin_error = None
        if self.enabled:
            # AQD_ORIGIN_IP (environment, else creds.env): straight to the origin, TLS still checked against the host.
            # A bad value turns the heartbeat off instead of breaking the crawler.
            try:
                origin_pin.install(self.base, os.environ.get("AQD_ORIGIN_IP") or file_env.get("AQD_ORIGIN_IP") or "")
            except origin_pin.OriginPinError as e:
                self.enabled, self.pin_error = False, str(e)
        self.every = _int_env("AQD_HEARTBEAT_EVERY_MIN", 15) * 60
        self.opener = opener or urllib.request.urlopen
        self.now = now
        self.disk_dir = disk_dir or state_dir
        self.last_error = None

    # ---- tally -----------------------------------------------------------------------------------------------------
    def _load(self):
        try:
            with open(self.state_file, encoding="utf-8") as f:
                state = json.load(f)
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, state):
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp, self.state_file)
        except OSError:
            pass

    def _touch(self, state):
        state.setdefault("since", self.now())  # start of the window of the next heartbeat
        state.pop("pending", None)  # the content changed: the next send is a new heartbeat

    def _bump(self, **delta):
        if not self.enabled:
            return
        state = self._load()
        counts = state.setdefault("counts", {})
        for name, value in delta.items():
            counts[name] = int(counts.get(name, 0)) + int(value)
        self._touch(state)
        self._save(state)

    def record_place(self, ok, photos=0, dishes=0):
        """One restaurant handled: ok -> processed+1 (plus its photos/dishes), otherwise errors+1."""
        if ok:
            self._bump(processed=1, photos=photos, dishes=dishes)
        else:
            self._bump(errors=1)

    def record_result(self, kind, msg=""):
        """safe_run style result: kind ok/warn counts as processed, error as an error, skip as nothing."""
        if kind in ("ok", "warn"):
            match = PHOTOS_RE.search(msg or "")
            self.record_place(True, photos=int(match.group(1)) if match else 0)
        elif kind == "error":
            self.record_place(False)
            self.note(None, f"{kind}: {msg}")

    def note(self, issue=None, summary=None):
        """Remember a problem (issue from ISSUES, short summary without secrets) for the next heartbeat."""
        if not self.enabled:
            return
        state = self._load()
        if issue in ISSUES:
            if issue not in state.setdefault("issues", []):
                state["issues"].append(issue)
            if issue == "rate_limited":
                counts = state.setdefault("counts", {})
                counts["rate_limited"] = int(counts.get("rate_limited", 0)) + 1
        if summary:
            state["summary"] = str(summary)[:LIMITS["note"]]
        self._touch(state)
        self._save(state)

    # ---- send ------------------------------------------------------------------------------------------------------
    def disk_free_mb(self):
        try:
            return int(shutil.disk_usage(self.disk_dir).free // (1024 * 1024))
        except OSError:
            return None

    def body(self, state, activity, status, range_label=None):
        counts = {name: max(0, int((state.get("counts") or {}).get(name, 0))) for name in COUNTS}
        issues = [i for i in state.get("issues") or [] if i in ISSUES]
        if status not in STATUSES:
            if any(i in BLOCKING for i in issues):
                status = "blocked"
            elif counts["errors"] and not counts["processed"]:
                status = "error"
            else:
                status = "running"
        started = state.get("since") or state.get("sent_at")
        window = int(max(0, self.now() - float(started))) if started else 0
        note = "; ".join(filter(None, [",".join(issues) or None, state.get("summary")]))
        body = {"crawler": self.crawler,
                "host": _cut(os.environ.get("CRAWLER_HOSTNAME") or socket.gethostname(), LIMITS["host"]) or "unknown",
                "status": status,
                "activity": _cut(activity, LIMITS["activity"]),
                "range_label": _cut(range_label, LIMITS["range_label"]),
                "window_seconds": window,
                "processed_count": counts["processed"], "photo_count": counts["photos"], "dish_count": counts["dishes"],
                "error_count": counts["errors"], "rate_limited_count": counts["rate_limited"],
                "disk_free_mb": self.disk_free_mb(),
                "note": _cut(note, LIMITS["note"])}
        return {k: v for k, v in body.items() if v is not None}

    def beat(self, activity=None, status=None, force=False, range_label=None):
        """Send the accumulated counts if due. status: running/idle/blocked/error (default from the tally).
        True when the server stored it."""
        if not self.enabled:
            return False
        try:
            state = self._load()
            if not force and self.now() - float(state.get("sent_at") or 0) < self.every:
                return False
            pending = state.get("pending") or {}
            if not (pending.get("key") and pending.get("body")):
                # a retry resends the very same body with the same key, so the server stores it once
                pending = {"key": f"{self.crawler}-hb-{uuid.uuid4()}",
                           "body": self.body(state, activity, status, range_label)}
                state["pending"] = pending
                self._save(state)
            key, body = pending["key"], pending["body"]
            request = urllib.request.Request(
                self.base + PATH, data=json.dumps(body).encode("utf-8"), method="POST",
                headers={"Authorization": "Bearer " + self.token, "Idempotency-Key": key,
                         "Content-Type": "application/json", "Accept": "application/json"})
            with self.opener(request, timeout=15) as response:
                code = getattr(response, "status", None) or response.getcode()
            if not 200 <= int(code) < 300:
                self.last_error = f"http {code}"
                return False
            self._save({"sent_at": self.now()})
            self.last_error = None
            return True
        except urllib.error.HTTPError as e:
            self.last_error = f"http {e.code}"
            if e.code in (409, 422):  # key clash or a body the server refuses (invalid_heartbeat): never resend it
                state = self._load()
                state.pop("pending", None)
                self._save(state)
        except Exception as e:  # noqa: BLE001 - a heartbeat must never break the crawler
            self.last_error = type(e).__name__
        return False
