#!/usr/bin/env python3
"""Crawler heartbeat for the daily Slack ops report (truonglh2372/an_uong_quanh_day#1021).

POST {AQD_BASE}/v1/ingest/heartbeats with `Authorization: Bearer $AQD_INGEST_TOKEN` (an ingest machine token issued in
Admin -> Ingest clients, one per machine) and an `Idempotency-Key`.

- No token (AQD_INGEST_TOKEN unset/empty) or AQD_HEARTBEAT=0 -> every call is a no-op.
- Best effort: any failure returns False and is swallowed; it never raises into the crawler and never logs the token.
- Counts are deltas since the last heartbeat the server accepted. They accumulate in <state dir>/heartbeat_state.json,
  so several short-lived processes (one per pass) add up, and a failed send keeps them (and the same body with the same
  Idempotency-Key) for the next try, so a retry is never counted twice. New counts start a new heartbeat.
- At most one heartbeat every AQD_HEARTBEAT_EVERY_MIN minutes (default 15) unless force=True.
"""
import json, os, re, shutil, socket, time, urllib.error, urllib.request, uuid

PATH = "/v1/ingest/heartbeats"
ISSUES = {"rate_limited", "ip_blocked", "captcha", "login_required", "disk_full", "repeated_errors", "network", "other"}
COUNTS = ("restaurants", "photos", "dishes", "errors")
SUMMARY_MAX = 300
LOW_DISK_MB = 1024
PHOTOS_RE = re.compile(r"photos=\+(\d+)")


def _int_env(name, default):
    try:
        value = int(os.environ.get(name, "") or default)
        return value if value > 0 else default
    except ValueError:
        return default


def _read_env_file(path):
    """KEY=VALUE lines of a creds.env-style file (only AQD_BASE / AQD_INGEST_TOKEN are used); {} if unreadable."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    if key.strip() in ("AQD_BASE", "AQD_INGEST_TOKEN"):
                        out[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


class Heartbeat:
    def __init__(self, crawler, state_dir, base=None, token=None, opener=None, now=time.time, disk_dir=None,
                 env_file=None):
        self.crawler = crawler
        self.state_file = os.path.join(state_dir, "heartbeat_state.json")
        file_env = _read_env_file(env_file) if env_file else {}
        base = base if base is not None else os.environ.get("AQD_BASE") or file_env.get("AQD_BASE")
        self.base = (base or "https://anuongquanhday.com").rstrip("/")
        if token is None:
            token = os.environ.get("AQD_INGEST_TOKEN") or file_env.get("AQD_INGEST_TOKEN") or ""
        self.token = token.strip()
        self.enabled = bool(self.token) and os.environ.get("AQD_HEARTBEAT", "1").strip() != "0"
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

    def _bump(self, **delta):
        if not self.enabled:
            return
        state = self._load()
        counts = state.setdefault("counts", {})
        for name, value in delta.items():
            counts[name] = int(counts.get(name, 0)) + int(value)
        state.pop("pending", None)  # the counts changed: the next send is a new heartbeat
        self._save(state)

    def record_place(self, ok, photos=0, dishes=0):
        """One restaurant handled: ok -> restaurants+1 (plus its photos/dishes), otherwise errors+1."""
        if ok:
            self._bump(restaurants=1, photos=photos, dishes=dishes)
        else:
            self._bump(errors=1)

    def record_result(self, kind, msg=""):
        """safe_run style result: kind ok/warn counts as done, error as an error, skip as nothing."""
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
        if issue in ISSUES and issue not in state.setdefault("issues", []):
            state["issues"].append(issue)
        if summary:
            state["error_summary"] = str(summary)[:SUMMARY_MAX]
        state.pop("pending", None)
        self._save(state)

    # ---- send ------------------------------------------------------------------------------------------------------
    def disk_free_mb(self):
        try:
            return int(shutil.disk_usage(self.disk_dir).free // (1024 * 1024))
        except OSError:
            return None

    def body(self, state, activity, status):
        counts = {name: int((state.get("counts") or {}).get(name, 0)) for name in COUNTS}
        issues = [i for i in state.get("issues") or [] if i in ISSUES]
        disk = self.disk_free_mb()
        if disk is not None and disk < LOW_DISK_MB and "disk_full" not in issues:
            issues.append("disk_full")
        if status is None:
            status = "degraded" if issues or counts["errors"] else "ok"
        body = {"crawler": self.crawler, "hostname": os.environ.get("CRAWLER_HOSTNAME") or socket.gethostname(),
                "activity": (activity or "")[:500] or None, "status": status, "counts": counts, "issues": issues,
                "disk_free_mb": disk, "error_summary": state.get("error_summary")}
        return {k: v for k, v in body.items() if v is not None}

    def beat(self, activity=None, status=None, force=False):
        """Send the accumulated counts if due. True when the server stored it."""
        if not self.enabled:
            return False
        try:
            state = self._load()
            if not force and self.now() - float(state.get("sent_at") or 0) < self.every:
                return False
            pending = state.get("pending") or {}
            if not (pending.get("key") and pending.get("body")):
                # a retry resends the very same body with the same key, so the server stores it once
                pending = {"key": f"{self.crawler}-hb-{uuid.uuid4()}", "body": self.body(state, activity, status)}
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
            if e.code in (409, 422):  # key clash or a body the server refuses: never resend that one
                state = self._load()
                state.pop("pending", None)
                self._save(state)
        except Exception as e:  # noqa: BLE001 - a heartbeat must never break the crawler
            self.last_error = type(e).__name__
        return False
