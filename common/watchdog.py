#!/usr/bin/env python3
"""OS watchdog for a crawler (truonglh2372/an_uong_quanh_day#1388). Run every 15 min by Task Scheduler (Windows) or
launchd (Mac); see the README. Stdlib only; the same file lives in script_foody/common and script_google_map/common.

  python watchdog.py --crawler foody --state-dir C:/script_foody/foody_data --match "foody_fetch\\.py" \
      --restart "C:\\...\\aqd_foody.cmd" [--stop-file C:/script_foody/foody_data/STOP]

- A STOP file present -> the crawler was stopped on purpose: nothing is done.
- The crawler process (command line matching --match) is alive -> nothing is done.
- Otherwise it runs --restart (detached), waits up to --wait seconds for the process and posts to Slack:
  restarted (⚫→🟢) or could not restart (⚫, red mention). More than --max-restarts restarts in one day (UTC+7)
  -> a red "restarted too often" alert instead. Alerts use the same webhook, mention and anti-spam as slack_notify.
Exit code 0 when the crawler runs (or STOP), 1 when it could not be restarted.

Optional (gmaps, only with --cdp-port): Chrome CDP health. /json/version can keep answering while a real CDP
connect hangs (Admin 12/11 05:17-12:30: gmaps fell back to thin HTTP, PREP ERROR JSONDecodeError), so the check is a
FULL connect: /json/version -> websocket handshake on webSocketDebuggerUrl -> Browser.getVersion reply, all within
--cdp-timeout seconds. After --cdp-fails consecutive failures (counted across runs in watchdog_cdp_state.json; one
run probes up to --cdp-attempts times (default = --cdp-fails), --cdp-gap seconds apart) the crawler Chrome of THIS profile (command line has
--remote-debugging-port=<port> and --user-data-dir=<--chrome-profile>) is closed and --chrome-restart is run with
AQD_CHROME_ONLY=1 (windows/start_fetcher.ps1: same chrome.exe, same profile, same port and launch args; alt.py is not
touched). Anti-loop: a cooldown after each restart that doubles with every restart that day (--chrome-cooldown-min,
max 4h) and at most --chrome-max-restarts restarts per day (UTC+7), then a red alert and no more restarts. Every
restart, failed restart and the daily cap is posted to Slack. A Chrome on that port with ANOTHER profile is never
closed. Without --cdp-port nothing of this runs (script_foody uses the same file without it).
"""
import argparse, base64, json, os, re, socket, subprocess, sys, time, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import slack_notify as sn  # noqa: E402


def list_processes():
    """[(pid, command line)] of this machine's processes; [] when it cannot be read."""
    try:
        if os.name == "nt":
            ps = ("Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                                 timeout=60).stdout
            rows = json.loads(out or "[]")
            rows = rows if isinstance(rows, list) else [rows]
            return [(int(r.get("ProcessId") or 0), r.get("CommandLine") or "") for r in rows]
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=30).stdout
        procs = []
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                procs.append((int(parts[0]), parts[1]))
        return procs
    except Exception:  # noqa: BLE001
        return []


def find(pattern, procs, me=None):
    rx = re.compile(pattern)
    me = os.getpid() if me is None else me
    return [pid for pid, cmd in procs if pid != me and rx.search(cmd or "") and "watchdog.py" not in cmd]


def start_detached(command, env=None):
    """Start the restart command without waiting for it; returns the Popen or raises."""
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if env is not None:
        kw["env"] = env
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(command, shell=True, **kw)


# ---- Chrome CDP health ---------------------------------------------------------------------------------------------
def _ws_frame(payload, opcode=1):
    """One masked client frame (RFC 6455)."""
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
    else:
        head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
    mask = os.urandom(4)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def cdp_probe(port, timeout=15.0, host="127.0.0.1", clock=time.monotonic):
    """FULL CDP connect, stdlib only: GET /json/version -> websocket handshake on its webSocketDebuggerUrl path ->
    send Browser.getVersion -> wait for the reply with id 1. Everything shares one deadline of `timeout` seconds.
    -> (ok, detail). Never raises."""
    deadline = clock() + float(timeout)

    def left():
        r = deadline - clock()
        if r <= 0:
            raise socket.timeout("hết thời gian")
        return r
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/json/version", timeout=min(5.0, left())) as r:
            ws = json.loads(r.read().decode("utf-8", "replace"))["webSocketDebuggerUrl"]
        path = urllib.parse.urlsplit(ws).path or "/"
        with socket.create_connection((host, int(port)), timeout=left()) as sock:
            key = base64.b64encode(os.urandom(16)).decode()
            req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
            sock.settimeout(left()); sock.sendall(req.encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                sock.settimeout(left()); chunk = sock.recv(4096)
                if not chunk:
                    return False, "websocket: Chrome đóng kết nối khi bắt tay"
                buf += chunk
                if len(buf) > 65536:
                    return False, "websocket: trả lời bắt tay quá dài"
            head, buf = buf.split(b"\r\n\r\n", 1)
            status = head.split(b"\r\n", 1)[0].split()
            if len(status) < 2 or status[1] != b"101":
                return False, "websocket: " + head.split(b"\r\n", 1)[0].decode("latin-1")[:80]
            sock.settimeout(left()); sock.sendall(_ws_frame(json.dumps({"id": 1, "method": "Browser.getVersion"}).encode()))

            def need(n):
                nonlocal buf
                while len(buf) < n:
                    sock.settimeout(left()); chunk = sock.recv(65536)
                    if not chunk:
                        raise ConnectionError("Chrome đóng websocket")
                    buf += chunk
                out, buf = buf[:n], buf[n:]
                return out
            msg = b""
            while True:
                b0, b1 = need(2)
                n = b1 & 0x7F
                if n == 126:
                    n = int.from_bytes(need(2), "big")
                elif n == 127:
                    n = int.from_bytes(need(8), "big")
                if b1 & 0x80:
                    m = need(4); data = bytes(x ^ m[i % 4] for i, x in enumerate(need(n)))
                else:
                    data = need(n)
                op = b0 & 0x0F
                if op == 8:
                    return False, "websocket: Chrome gửi close"
                if op == 9:
                    sock.sendall(_ws_frame(data, 10)); continue
                if op not in (0, 1):
                    continue
                msg += data
                if not b0 & 0x80:
                    continue
                try:
                    j = json.loads(msg.decode("utf-8", "replace"))
                except ValueError:
                    j = {}
                msg = b""
                if isinstance(j, dict) and j.get("id") == 1:
                    try:
                        sock.sendall(_ws_frame(b"\x03\xe8", 8))
                    except OSError:
                        pass
                    if "result" in j:
                        return True, str((j.get("result") or {}).get("product") or "ok")[:60]
                    return False, "CDP lỗi: " + str(j.get("error"))[:100]
    except Exception as e:  # noqa: BLE001 - socket.timeout / URLError / ValueError / KeyError ...
        return False, f"{type(e).__name__}: {e}"[:160]


def _norm_path(p):
    return str(p or "").strip().strip('"').replace("\\", "/").rstrip("/").lower()


def chrome_main_pids(procs, port, profile):
    """PIDs of the BROWSER process (not renderers: no --type=) started with --remote-debugging-port=<port> and
    --user-data-dir=<profile>. A Chrome on that port with another profile is not returned."""
    want = _norm_path(profile)
    out = []
    for pid, cmd in procs:
        cmd = cmd or ""
        if f"--remote-debugging-port={port}" not in cmd or "--type=" in cmd:
            continue
        m = re.search(r'--user-data-dir=(?:"([^"]+)"|(\S+))', cmd)
        if m and _norm_path(m.group(1) or m.group(2)) == want:
            out.append(pid)
    return out


def kill_tree(pid):
    """Close a process and its children. Windows: taskkill /T /F. Elsewhere: SIGTERM, then SIGKILL after 10 s."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=60)
        return
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            time.sleep(0.5)
            os.kill(pid, 0)
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def port_open(port, host="127.0.0.1", timeout=1.0):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _env_float(env, name, default):
    try:
        return float((env or {}).get(name, "") or default)
    except ValueError:
        return default


class ChromeWatch:
    """Restart the crawler Chrome after `fails` consecutive failed FULL CDP connects (see the module doc)."""

    def __init__(self, notifier, state_dir, port, profile, restart, fails=3, timeout=15.0, gap=20.0,
                 cooldown=1800.0, max_day=4, restart_wait=90.0, now=time.time, sleep=time.sleep, probe=cdp_probe,
                 lister=list_processes, killer=kill_tree, starter=start_detached, port_check=port_open, attempts=None):
        self.n, self.port, self.profile, self.restart = notifier, int(port), profile, restart
        self.fails, self.timeout, self.gap = max(1, int(fails)), float(timeout), float(gap)
        self.attempts = max(1, int(attempts or self.fails))  # probes per run; the failure count spans runs
        self.cooldown, self.max_day, self.restart_wait = float(cooldown), int(max_day), float(restart_wait)
        self.now, self.sleep, self.probe, self.lister = now, sleep, probe, lister
        self.killer, self.starter, self.port_check = killer, starter, port_check
        self.state_file = os.path.join(state_dir, "watchdog_cdp_state.json")

    def _load(self):
        try:
            with open(self.state_file, encoding="utf-8") as f:
                st = json.load(f)
            return st if isinstance(st, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, st):
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False)
            os.replace(tmp, self.state_file)
        except OSError:
            pass

    def cooldown_for(self, restarts_today):
        """Base cooldown, doubled for every restart already done today, at most 4 h."""
        return min(4 * 3600.0, self.cooldown * (2 ** max(0, int(restarts_today) - 1)))

    def _alert(self, code, text, dedupe=True):
        if dedupe and not self.n._update(lambda s: self.n._allow(s, code)):
            return False
        return self.n._send("watchdog", text)

    def run(self):
        """'cdp_ok' / 'cdp_fail' / 'cooldown' / 'capped' / 'foreign' / 'restarted' / 'restart_failed'."""
        seen = self.now()
        day = sn.datetime.fromtimestamp(seen, sn.ICT).strftime("%Y-%m-%d")
        st = self._load()
        if st.get("day") != day:
            st.update(day=day, restarts=0)
        st["fails"] = int(st.get("fails") or 0)
        for i in range(self.attempts):
            ok, detail = self.probe(self.port, self.timeout)
            if ok:
                if st["fails"]:
                    sn.log(f"watchdog: CDP {self.port} trả lời lại sau {st['fails']} lần lỗi ({detail})")
                st.update(fails=0, last_ok=self.now())
                self._save(st)
                return "cdp_ok"
            st["fails"] += 1
            st["last_error"] = detail
            self._save(st)
            sn.log(f"watchdog: CDP {self.port} connect lỗi {st['fails']}/{self.fails}: {detail}")
            if st["fails"] >= self.fails:
                break
            if i < self.attempts - 1:
                self.sleep(self.gap)
        if st["fails"] < self.fails:
            return "cdp_fail"
        title, foot = self.n._title(), f"_Watchdog ({self.n.scheduler}) · {sn.ddmm_hhmm(seen)} (UTC+7)_"
        why = f"{st['fails']} lần connect CDP đầy đủ liên tiếp lỗi (timeout {self.timeout:g}s): `{sn._clean(st.get('last_error'), 120)}`"
        restarts = int(st.get("restarts") or 0)
        last = float(st.get("last_restart") or 0)
        wait = self.cooldown_for(restarts)
        if last and seen - last < wait:
            sn.log(f"watchdog: Chrome vừa khởi động lại lúc {sn.hhmm(last)}; chờ hết cooldown {int(wait // 60)} phút")
            return "cooldown"
        if restarts >= self.max_day:
            self._alert("chrome_restart_cap",
                        f"🔴 {sn.mention(self.n.env)}{title} — watchdog: Chrome crawler (CDP {self.port}) vẫn treo, đã "
                        f"khởi động lại {restarts} lần hôm nay → NGỪNG tự khởi động lại tới hết ngày\n• {why}\n"
                        f"• Việc cần làm: *đăng nhập {self.n.host} xem Chrome / đăng nhập Google*\n{foot}")
            return "capped"
        procs = self.lister()
        pids = chrome_main_pids(procs, self.port, self.profile)
        if not pids and self.port_check(self.port):
            self._alert("chrome_foreign",
                        f"⚫ {sn.mention(self.n.env)}{title} — watchdog: CDP {self.port} treo nhưng không thấy Chrome của "
                        f"profile `{sn._clean(self.profile, 80)}` trên cổng đó → KHÔNG đóng Chrome lạ\n• {why}\n"
                        f"• Việc cần làm: *đăng nhập {self.n.host} kiểm tra Chrome nào giữ cổng {self.port}*\n{foot}")
            return "foreign"
        st.update(restarts=restarts + 1, last_restart=seen, fails=0)
        self._save(st)  # saved BEFORE acting: a crash here can never turn into a restart loop
        sn.log(f"watchdog: đóng Chrome crawler PID {pids or '-'} (CDP {self.port}) rồi mở lại: {why}")
        for pid in pids:
            try:
                self.killer(pid)
            except Exception as e:  # noqa: BLE001
                sn.log(f"watchdog: đóng PID {pid} lỗi {type(e).__name__}")
        for _ in range(20):
            if not self.port_check(self.port):
                break
            self.sleep(1)
        ok, detail, rc = False, "", None
        try:
            self.starter(self.restart, dict(os.environ, AQD_CHROME_ONLY="1"))
            for _ in range(max(1, int(self.restart_wait // 5))):
                self.sleep(5)
                ok, detail = self.probe(self.port, self.timeout)
                if ok:
                    break
        except Exception as e:  # noqa: BLE001
            rc, detail = type(e).__name__, type(e).__name__
        n_today = restarts + 1
        if ok:
            st["last_ok"] = self.now(); self._save(st)
            self._alert("chrome_restarted",
                        f"🟠→🟢 {title} — watchdog: Chrome crawler treo CDP, đã khởi động lại (lần {n_today}/{self.max_day} hôm nay)\n"
                        f"• {why}\n• Đã đóng PID {', '.join(map(str, pids)) or '-'} và mở lại cùng profile, cổng {self.port}: "
                        f"CDP trả lời ({sn._clean(detail, 60)})\n"
                        f"• Việc cần làm: không (lần sau sớm nhất sau {int(self.cooldown_for(n_today) // 60)} phút)\n{foot}",
                        dedupe=False)
            return "restarted"
        self._alert("chrome_restart_failed",
                    f"🔴 {sn.mention(self.n.env)}{title} — watchdog: Chrome crawler treo CDP, khởi động lại KHÔNG thành công\n"
                    f"• {why}\n• Đã đóng PID {', '.join(map(str, pids)) or '-'}, chạy `{sn._clean(self.restart, 100)}`"
                    f" nhưng CDP {self.port} vẫn không trả lời sau {int(self.restart_wait)}s"
                    + (f" ({sn._clean(detail, 80)})" if detail else "") + "\n"
                    f"• Việc cần làm: *đăng nhập {self.n.host} mở Chrome crawler / kiểm tra đăng nhập Google*\n{foot}",
                    dedupe=False)
        return "restart_failed"


class Watchdog:
    def __init__(self, crawler, state_dir, match, restart, stop_files=(), wait=30, max_restarts=3, env=None,
                 now=time.time, sender=None, lister=list_processes, starter=start_detached, sleep=time.sleep,
                 chrome=None):
        self.n = sn.Notifier(crawler, state_dir, env=env, now=now, sender=sender)
        self.chrome = chrome(self.n) if callable(chrome) and not isinstance(chrome, ChromeWatch) else chrome
        self.chrome_result = None
        self.match, self.restart, self.stop_files = match, restart, list(stop_files)
        self.wait, self.max_restarts = wait, max_restarts
        self.lister, self.starter, self.sleep, self.now = lister, starter, sleep, now
        self.state_file = os.path.join(state_dir, "watchdog_state.json")

    def _load(self):
        try:
            with open(self.state_file, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self, st):
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f)
            os.replace(tmp, self.state_file)
        except OSError:
            pass

    def run(self):
        """'stop' / 'alive' / 'restarted' / 'failed' / 'flapping'. Then, when configured, the Chrome CDP check
        (self.chrome_result); skipped after STOP and right after alt.py was (re)started (start_fetcher just
        (re)opened Chrome; the next run checks it)."""
        out = self._run_alt()
        if self.chrome is not None and out in ("alive", "failed"):
            try:
                self.chrome_result = self.chrome.run()
            except Exception as e:  # noqa: BLE001 - the Chrome check never breaks the alt.py watchdog
                self.chrome_result = f"lỗi {type(e).__name__}"
            sn.log(f"watchdog: chrome {self.chrome_result}")
        return out

    def _run_alt(self):
        if any(os.path.exists(p) for p in self.stop_files):
            sn.log("watchdog: có file STOP -> không làm gì")
            return "stop"
        if find(self.match, self.lister()):
            return "alive"
        seen = self.now()
        last = self.n._load().get("last_report") or self.n._load().get("alive_at")
        day = sn.datetime.fromtimestamp(seen, sn.ICT).strftime("%Y-%m-%d")
        st = self._load()
        if st.get("day") != day:
            st = {"day": day, "restarts": 0}
        st["restarts"] = int(st.get("restarts") or 0) + 1
        self._save(st)
        rc, pids = None, []
        try:
            proc = self.starter(self.restart)
            for _ in range(max(1, int(self.wait // 5))):
                self.sleep(5)
                pids = find(self.match, self.lister())
                if pids:
                    break
                rc = proc.poll() if hasattr(proc, "poll") else None
        except Exception as e:  # noqa: BLE001
            rc = type(e).__name__
        title = self.n._title()
        foot = f"_Watchdog ({self.n.scheduler}, 15 phút/lần) · {sn.ddmm_hhmm(seen)} (UTC+7)_"
        cmd = f"`{sn._clean(self.restart, 100)}`"
        if not pids:
            if not self.n._update(lambda s: self.n._allow(s, "watchdog_failed")):
                return "failed"
            why = f"thất bại (exit {rc})" if rc not in (None, 0) else "không thấy process sau khi chạy"
            text = (f"⚫ {sn.mention(self.n.env)}{title} — watchdog: crawler đã dừng, KHÔNG khởi động lại được\n"
                    f"• Không thấy process `{self.match}` lúc {sn.hhmm(seen)}; chạy lại {cmd} {why}\n"
                    f"• Việc cần làm: *đăng nhập {self.n.host} kiểm tra*\n{foot}")
            self.n._send("watchdog", text)
            return "failed"
        if st["restarts"] > self.max_restarts:
            if self.n._update(lambda s: self.n._allow(s, "watchdog_flapping")):
                text = (f"🔴 {sn.mention(self.n.env)}{title} — watchdog: đã khởi động lại {st['restarts']} lần hôm nay\n"
                        f"• Lần này: chạy lại {cmd} → PID {pids[0]} đang chạy\n"
                        f"• Việc cần làm: *crawler chết liên tục, xem log trên {self.n.host}*\n{foot}")
                self.n._send("watchdog", text)
            return "flapping"
        text = (f"⚫→🟢 {title} — watchdog: process đã chết, đã khởi động lại\n"
                f"• Không thấy `{self.match}` lúc {sn.hhmm(seen)} (UTC+7)"
                + (f"; báo cáo cuối của crawler: {sn.hhmm(float(last))}" if last else "") + "\n"
                f"• Đã chạy lại {cmd} → PID {pids[0]} đang chạy\n"
                f"• Việc cần làm: không (nếu bị khởi động lại >{self.max_restarts} lần/ngày sẽ báo 🔴)\n{foot}")
        self.n._send("watchdog", text)
        return "restarted"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--crawler", required=True)
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--match", required=True, help="regex on the crawler's command line")
    ap.add_argument("--restart", required=True, help="command that starts the crawler again")
    ap.add_argument("--stop-file", action="append", default=[])
    ap.add_argument("--wait", type=int, default=30)
    ap.add_argument("--max-restarts", type=int, default=3)
    ap.add_argument("--log", help="append output to this file (pythonw / launchd have no console)")
    e = os.environ
    ap.add_argument("--cdp-port", type=int, default=0, help="Chrome CDP port to health-check (0 = off)")
    ap.add_argument("--chrome-profile", default="", help="the crawler Chrome --user-data-dir (default <state-dir>/chrome-profile)")
    ap.add_argument("--chrome-restart", default="", help="command that opens the crawler Chrome (run with AQD_CHROME_ONLY=1)")
    ap.add_argument("--cdp-fails", type=int, default=int(_env_float(e, "AQD_CDP_FAILS", 3)))
    ap.add_argument("--cdp-attempts", type=int, default=int(_env_float(e, "AQD_CDP_ATTEMPTS", 0)),
                    help="probes per watchdog run (default = --cdp-fails); failures are counted across runs")
    ap.add_argument("--cdp-timeout", type=float, default=_env_float(e, "AQD_CDP_TIMEOUT", 15))
    ap.add_argument("--cdp-gap", type=float, default=_env_float(e, "AQD_CDP_GAP", 20))
    ap.add_argument("--chrome-cooldown-min", type=float, default=_env_float(e, "AQD_CHROME_COOLDOWN_MIN", 30))
    ap.add_argument("--chrome-max-restarts", type=int, default=int(_env_float(e, "AQD_CHROME_MAX_RESTARTS", 4)))
    ap.add_argument("--chrome-wait", type=float, default=_env_float(e, "AQD_CHROME_WAIT", 90))
    a = ap.parse_args(argv)
    if a.log:
        try:
            sys.stdout = open(a.log, "a", encoding="utf-8", buffering=1)
            sys.stderr = sys.stdout
        except OSError:
            pass
    sn.log(f"watchdog {a.crawler}: {sn.ddmm_hhmm(time.time())} kiểm tra")
    chrome = None
    if a.cdp_port and a.chrome_restart:
        prof = a.chrome_profile or os.path.join(a.state_dir, "chrome-profile")
        chrome = lambda n: ChromeWatch(n, a.state_dir, a.cdp_port, prof, a.chrome_restart, fails=a.cdp_fails,  # noqa: E731
                                       attempts=a.cdp_attempts or None,
                                       timeout=a.cdp_timeout, gap=a.cdp_gap, cooldown=a.chrome_cooldown_min * 60,
                                       max_day=a.chrome_max_restarts, restart_wait=a.chrome_wait)
    out = Watchdog(a.crawler, a.state_dir, a.match, a.restart, a.stop_file, a.wait, a.max_restarts, chrome=chrome).run()
    sn.log(f"watchdog {a.crawler}: {out}")
    return 1 if out == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
