"""Offline tests for the Chrome CDP health check in common/watchdog.py (Admin 12/11 05:17-12:30: /json/version kept
answering while a real CDP connect hung -> thin HTTP + PREP ERROR JSONDecodeError).

Network only to fake servers on 127.0.0.1; Chrome, taskkill and start_fetcher.ps1 are stubs, so this runs on macOS,
Linux and Windows. Run: python3 tools/test_watchdog_cdp.py
"""
import base64, hashlib, json, os, socket, sys, tempfile, threading, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
import slack_notify as sn  # noqa: E402
import watchdog as wd  # noqa: E402

T0 = sn.datetime(2026, 11, 12, 9, 0, tzinfo=sn.ICT).timestamp()  # 12/11/2026 09:00 UTC+7
PROFILE = "C:\\aqd\\chrome-profile"
MAIN = (4100, '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --no-first-run '
              '--remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 '
              '--user-data-dir=C:\\aqd\\chrome-profile --restore-last-session')
RENDERER = (4200, 'chrome.exe --type=renderer --user-data-dir=C:\\aqd\\chrome-profile --remote-debugging-port=9222')
OTHER_ROOT = (4300, 'chrome.exe --remote-debugging-port=9223 --user-data-dir="C:\\aqd_test\\chrome-profile"')
ALT = (5000, "python C:\\aqd\\alt.py")


class FakeCdp:
    """Minimal Chrome DevTools endpoint: /json/version always answers; the websocket either works ('ok'),
    accepts the TCP connection but never answers the handshake ('hang' = the 12/11 incident), or never answers
    the command ('mute')."""

    def __init__(self, mode="ok"):
        self.mode, self.stop = mode, threading.Event()
        self.srv = socket.socket(); self.srv.bind(("127.0.0.1", 0)); self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.hold = []
        threading.Thread(target=self.loop, daemon=True).start()

    def close(self):
        self.stop.set()
        for c in self.hold:
            try: c.close()
            except OSError: pass
        self.srv.close()

    def loop(self):
        while not self.stop.is_set():
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(c,), daemon=True).start()

    def handle(self, c):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = c.recv(4096)
            if not chunk:
                return
            data += chunk
        line = data.split(b"\r\n", 1)[0]
        if b"/json/version" in line:
            body = json.dumps({"Browser": "Chrome/156", "webSocketDebuggerUrl":
                               f"ws://localhost:{self.port}/devtools/browser/abc"}).encode()
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            c.close(); return
        if self.mode == "hang":
            self.hold.append(c); return              # TCP accepted, handshake never answered
        key = [l.split(b":", 1)[1].strip() for l in data.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  b"Sec-WebSocket-Accept: " + acc + b"\r\n\r\n")
        b0, b1 = c.recv(2)
        n = b1 & 0x7F
        mask = c.recv(4); payload = bytes(x ^ mask[i % 4] for i, x in enumerate(c.recv(n)))
        req = json.loads(payload)
        if self.mode == "mute":
            self.hold.append(c); return
        c.sendall(bytes([0x89, 0x00]))                # a ping first: the client must pong and keep reading
        out = json.dumps({"id": req["id"], "result": {"product": "Chrome/156.0", "protocolVersion": "1.3"}}).encode()
        c.sendall(bytes([0x81, len(out)]) + out if len(out) < 126 else bytes([0x81, 126]) + len(out).to_bytes(2, "big") + out)
        time.sleep(0.2); c.close()


class ProbeTest(unittest.TestCase):
    def serve(self, mode):
        s = FakeCdp(mode); self.addCleanup(s.close); return s

    def test_full_connect_ok(self):
        ok, detail = wd.cdp_probe(self.serve("ok").port, timeout=5)
        self.assertTrue(ok, detail); self.assertEqual(detail, "Chrome/156.0")

    def test_json_version_answers_but_websocket_hangs(self):
        t = time.monotonic()
        ok, detail = wd.cdp_probe(self.serve("hang").port, timeout=1.5)
        self.assertFalse(ok); self.assertIn("timeout", detail.lower() + type(detail).__name__.lower())
        self.assertLess(time.monotonic() - t, 5)

    def test_command_never_answered(self):
        ok, _ = wd.cdp_probe(self.serve("mute").port, timeout=1.5)
        self.assertFalse(ok)

    def test_nothing_listening(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        self.assertFalse(wd.cdp_probe(port, timeout=2)[0])


class MainPidTest(unittest.TestCase):
    def test_only_this_profiles_browser_process(self):
        self.assertEqual(wd.chrome_main_pids([MAIN, RENDERER, OTHER_ROOT, ALT], 9222, PROFILE), [4100])
        self.assertEqual(wd.chrome_main_pids([MAIN], 9222, "C:/AQD/chrome-profile/"), [4100])
        self.assertEqual(wd.chrome_main_pids([OTHER_ROOT], 9223, "C:\\aqd_test\\chrome-profile"), [4300])
        self.assertEqual(wd.chrome_main_pids([OTHER_ROOT], 9223, PROFILE), [])


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class ChromeWatchTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.clock, self.out, self.killed, self.started, self.slept = Clock(), [], [], [], []
        self.results = []          # probe results consumed in order; empty -> self.default
        self.default = (False, "TimeoutError: timed out")
        self.port_up = True
        self.procs = [MAIN, RENDERER, OTHER_ROOT, ALT]
        env = {"SLACK_CRAWLER_WEBHOOK_URL": "http://127.0.0.1:9/x", "AQD_HOST_LABEL": "Admin",
               "SLACK_ALERT_MENTION": "U1ABC", "SLACK_CRAWLER_WEBHOOK_FILE": os.path.join(self.dir, "none")}
        self.n = sn.Notifier("gmaps", self.dir, env=env, now=self.clock,
                             sender=lambda url, p: self.out.append(p["text"]) or True)

    def probe(self, port, timeout):
        return self.results.pop(0) if self.results else self.default

    def kill(self, pid):
        self.killed.append(pid); self.port_up = False

    def start(self, cmd, env=None):
        self.started.append((cmd, (env or {}).get("AQD_CHROME_ONLY")))
        self.port_up = True

    def watch(self, **kw):
        o = dict(fails=3, timeout=15, gap=20, cooldown=1800, max_day=4, restart_wait=30)
        o.update(kw)
        return wd.ChromeWatch(self.n, self.dir, 9222, PROFILE, "powershell -File C:\\aqd\\start_fetcher.ps1",
                              now=self.clock, sleep=self.slept.append, probe=self.probe, lister=lambda: list(self.procs),
                              killer=self.kill, starter=self.start, port_check=lambda p: self.port_up, **o)

    def state(self):
        with open(os.path.join(self.dir, "watchdog_cdp_state.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_restart_after_n_consecutive_full_cdp_failures(self):
        self.results = [self.default] * 3 + [(False, "x"), (True, "Chrome/156.0")]
        self.assertEqual(self.watch().run(), "restarted")
        self.assertEqual(self.killed, [4100])                           # only this profile's browser process
        self.assertEqual(self.started, [("powershell -File C:\\aqd\\start_fetcher.ps1", "1")])  # AQD_CHROME_ONLY=1
        self.assertEqual(self.slept[:2], [20, 20])                      # in-run retries, --cdp-gap apart
        self.assertEqual(len(self.out), 1)
        self.assertIn("🟠→🟢 *gmaps @ Admin* — watchdog: Chrome crawler treo CDP, đã khởi động lại (lần 1/4 hôm nay)", self.out[0])
        self.assertIn("3 lần connect CDP đầy đủ liên tiếp lỗi (timeout 15s)", self.out[0])
        self.assertIn("Đã đóng PID 4100", self.out[0])
        st = self.state(); self.assertEqual((st["fails"], st["restarts"]), (0, 1))

    def test_failures_counted_across_runs(self):
        w = self.watch(fails=3, attempts=1)                             # one probe per watchdog run
        self.assertEqual(w.run(), "cdp_fail"); self.assertEqual(w.run(), "cdp_fail")
        self.assertEqual((self.state()["fails"], self.killed), (2, []))
        self.results = [self.default, (True, "ok")]
        self.assertEqual(w.run(), "restarted")
        self.assertEqual(self.killed, [4100]); self.assertEqual(self.slept.count(20), 0)

    def test_no_restart_when_it_recovers_before_n(self):
        self.results = [self.default, self.default, (True, "Chrome/156.0")]
        self.assertEqual(self.watch().run(), "cdp_ok")
        self.results = [self.default, (True, "Chrome/156.0")]
        self.assertEqual(self.watch().run(), "cdp_ok")
        self.assertEqual((self.killed, self.started, self.out), ([], [], []))
        self.assertEqual(self.state()["fails"], 0)

    def test_cooldown_and_backoff(self):
        self.results = [self.default] * 3 + [(True, "ok")]
        self.assertEqual(self.watch().run(), "restarted")
        self.clock.t += 15 * 60                                         # next watchdog run, still hung
        self.assertEqual(self.watch().run(), "cooldown")
        self.assertEqual(self.killed, [4100])                           # no second kill inside the cooldown
        self.clock.t += 16 * 60                                         # 31 min after restart 1 -> allowed
        self.results = [self.default] * 3 + [(True, "ok")]
        self.assertEqual(self.watch().run(), "restarted")
        self.clock.t += 31 * 60                                         # cooldown doubled to 60 min after restart 2
        self.assertEqual(self.watch().run(), "cooldown")
        self.clock.t += 30 * 60
        self.results = [self.default] * 3 + [(True, "ok")]
        self.assertEqual(self.watch().run(), "restarted")
        self.assertEqual(len(self.killed), 3)
        self.assertEqual(self.watch().cooldown_for(10), 4 * 3600)

    def test_daily_cap_goes_red_and_stops(self):
        w = self.watch(max_day=1, cooldown=60)
        self.results = [self.default] * 3 + [(True, "ok")]
        self.assertEqual(w.run(), "restarted")
        self.clock.t += 3600
        self.assertEqual(w.run(), "capped")
        self.assertIn("🔴 <@U1ABC> *gmaps @ Admin* — watchdog: Chrome crawler (CDP 9222) vẫn treo", self.out[-1])
        n = len(self.out)
        self.clock.t += 1800
        self.assertEqual(w.run(), "capped"); self.assertEqual(len(self.out), n)   # deduped
        self.assertEqual(self.killed, [4100])
        self.clock.t = sn.datetime(2026, 11, 13, 9, 0, tzinfo=sn.ICT).timestamp()  # new day (UTC+7) -> allowed again
        self.results = [self.default] * 3 + [(True, "ok")]
        self.assertEqual(w.run(), "restarted")

    def test_foreign_chrome_on_the_port_is_never_closed(self):
        self.procs = [OTHER_ROOT, ALT, (4400, "chrome.exe --remote-debugging-port=9222 --user-data-dir=D:\\other")]
        self.assertEqual(self.watch().run(), "foreign")
        self.assertEqual((self.killed, self.started), ([], []))
        self.assertIn("KHÔNG đóng Chrome lạ", self.out[0])

    def test_chrome_gone_is_started(self):
        self.procs, self.port_up = [ALT], False
        self.results = [self.default] * 3 + [(True, "ok")]
        self.assertEqual(self.watch().run(), "restarted")
        self.assertEqual(self.killed, []); self.assertEqual(len(self.started), 1)

    def test_restart_that_does_not_help_goes_red(self):
        self.assertEqual(self.watch().run(), "restart_failed")
        self.assertIn("🔴 <@U1ABC> *gmaps @ Admin* — watchdog: Chrome crawler treo CDP, khởi động lại KHÔNG thành công", self.out[0])
        self.assertEqual(self.state()["restarts"], 1)                  # counted -> cooldown/cap still apply
        self.assertEqual(self.watch().run(), "cooldown")


class WatchdogIntegrationTest(unittest.TestCase):
    """alt.py watchdog + Chrome check: STOP -> nothing; alt alive -> Chrome checked; alt just restarted -> skipped."""

    class Chrome:
        def __init__(self):
            self.calls = 0

        def run(self):
            self.calls += 1
            return "cdp_ok"

    class Proc:
        def poll(self):
            return None

    def dog(self, procs, stop=False):
        d = tempfile.mkdtemp(); stop_file = os.path.join(d, "STOP_ALL")
        if stop:
            open(stop_file, "w").close()
        ch = self.Chrome()
        env = {"SLACK_CRAWLER_WEBHOOK_URL": "", "AQD_SLACK": "0", "SLACK_CRAWLER_WEBHOOK_FILE": os.path.join(d, "none")}
        dog = wd.Watchdog("gmaps", d, r"alt\.py", "aqd_fetcher.cmd", [stop_file], wait=10, env=env,
                          lister=lambda: procs, starter=lambda cmd: self.Proc(), sleep=lambda s: None, chrome=ch)
        return dog, ch

    def test_alive_checks_chrome(self):
        dog, ch = self.dog([ALT])
        self.assertEqual(dog.run(), "alive"); self.assertEqual((ch.calls, dog.chrome_result), (1, "cdp_ok"))

    def test_stop_skips_chrome(self):
        dog, ch = self.dog([ALT], stop=True)
        self.assertEqual(dog.run(), "stop"); self.assertEqual(ch.calls, 0)

    def test_alt_restarted_skips_chrome_this_run(self):
        dog, ch = self.dog([ALT])
        dog.lister = iter([[], [ALT]]).__next__
        self.assertEqual(dog.run(), "restarted"); self.assertEqual(ch.calls, 0)

    def test_factory_and_cli_off_by_default(self):
        made = []
        d = tempfile.mkdtemp()
        dog = wd.Watchdog("gmaps", d, r"alt\.py", "x", [], env={"AQD_SLACK": "0"}, lister=lambda: [ALT],
                          chrome=lambda n: made.append(n) or self.Chrome())
        self.assertEqual(len(made), 1); self.assertIs(made[0], dog.n)



class CliTest(unittest.TestCase):
    """main(): --cdp-port wires the check; a hung /json/version-only Chrome of another profile is reported, never killed."""

    def test_cli_hang_foreign_and_off(self):
        from unittest import mock
        srv = FakeCdp("hang"); self.addCleanup(srv.close)
        d = tempfile.mkdtemp()
        env = {"AQD_SLACK": "0", "SLACK_CRAWLER_WEBHOOK_URL": "", "SLACK_CRAWLER_WEBHOOK_FILE": os.path.join(d, "none")}
        argv = ["--crawler", "gmaps", "--state-dir", d, "--match", ".", "--restart", "true",
                "--cdp-port", str(srv.port), "--chrome-profile", os.path.join(d, "chrome-profile"),
                "--chrome-restart", "true", "--cdp-fails", "2", "--cdp-timeout", "1", "--cdp-gap", "0"]
        logs = []
        with mock.patch.dict(os.environ, env), mock.patch.object(wd.sn, "log", logs.append), \
                mock.patch.object(wd, "kill_tree", lambda pid: self.fail("must not kill a foreign Chrome")):
            self.assertEqual(wd.main(argv), 0)
            self.assertIn("watchdog: chrome foreign", logs)
            st = json.load(open(os.path.join(d, "watchdog_cdp_state.json"), encoding="utf-8"))
            self.assertEqual(st.get("restarts", 0), 0)
            logs.clear()
            self.assertEqual(wd.main(argv[:8]), 0)                       # no --cdp-port -> no Chrome check at all
            self.assertFalse(any("chrome" in l for l in logs))


if __name__ == "__main__":
    unittest.main(verbosity=1)
