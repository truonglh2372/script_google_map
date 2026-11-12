"""Offline tests for common/disk_cleanup.py (+ Notifier.cleanup). No network: Slack sender is a stub.

Run: python3 <tests dir>/test_disk_cleanup.py
"""
import json, os, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
import disk_cleanup as dc  # noqa: E402
import slack_notify as sn  # noqa: E402

NOW = time.time()
DAY = 86400


def w(p, data=b"x" * 1000, age=0):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    os.utime(p, (NOW - age, NOW - age))
    return p


class FakeNotifier:
    def __init__(self):
        self.calls = []

    def cleanup(self, *a):
        self.calls.append(a)


def gmaps_tree(r):
    d = os.path.join(r, "aqd_data")
    files = {
        "done_img": w(f"{d}/101/streetview.jpg"), "done_json": w(f"{d}/101/info.json"),
        "done_sub": w(f"{d}/101/photos/a.webp"),
        "retry_img": w(f"{d}/103/p.jpg"), "notdone_img": w(f"{d}/105/p.jpg"),
        "young_img": w(f"{d}/107/p.jpg"),
        "menu_old": w(f"{r}/menu_ocr_cache/aa.jpg", age=10 * DAY), "menu_ocr": w(f"{r}/menu_ocr_cache/aa.ocr.json", age=10 * DAY),
        "menu_new": w(f"{r}/menu_ocr_cache/bb.jpg", age=DAY),
        "env": w(f"{r}/crawler.env", age=99 * DAY), "creds": w(f"{r}/creds.env", age=99 * DAY),
        "profile_img": w(f"{r}/chrome-profile/Default/Cache/x.jpg", age=99 * DAY),
        "profile_log": w(f"{r}/chrome-profile/chrome_debug.log", age=99 * DAY),
        "old_log": w(f"{r}/pip-heavy.log", age=30 * DAY), "rotated": w(f"{r}/fetch.log.1", age=30 * DAY), "new_log": w(f"{r}/alt.out", age=0),
        "big_log": w(f"{r}/alt.launchd.err", b"line\n" * 300000, age=0),
        "old_tmp": w(f"{r}/queue.json.123.tmp", age=2 * DAY), "queue": w(f"{r}/queue_odd.json", age=99 * DAY),
        "stop": w(f"{r}/STOP_ALL", age=99 * DAY),
    }
    for i in ("101", "103", "107"):
        w(f"{d}/{i}/SAFE_DONE", b"1", age=0 if i == "107" else 2 * 3600)
    w(f"{d}/105/SAFE_PREP", b"1", age=2 * 3600)
    w(f"{r}/photo_retry.json", json.dumps({"103": {"state": "retry"}}).encode())
    return files


class GmapsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.r = self.tmp.name; self.f = gmaps_tree(self.r)

    def tearDown(self):
        self.tmp.cleanup()

    def run_it(self, env=None, **kw):
        n = FakeNotifier()
        e = {"AQD_CLEANUP_LOG_MAX_MB": "1", "AQD_CLEANUP_LOG_KEEP_MB": "0.1"}; e.update(env or {})
        res = dc.run("gmaps", self.r, now=NOW, notifier=n, env=e,
                     say=lambda s: None, **kw)
        return res, n

    def test_dry_run_deletes_nothing_and_sends_nothing(self):
        res, n = self.run_it(dry_run=True)
        self.assertTrue(res["ran"]); self.assertGreater(res["files"], 0)
        for k, p in self.f.items():
            self.assertTrue(os.path.exists(p), k)
        self.assertEqual(os.path.getsize(self.f["big_log"]), 5 * 300000)
        self.assertEqual(n.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.r, "cleanup_state.json")))

    def test_real_run_deletes_only_regenerable(self):
        res, n = self.run_it(force=True)
        gone = {"done_img", "done_sub", "menu_old", "old_tmp", "rotated"}
        for k, p in self.f.items():
            self.assertEqual(os.path.exists(p), k not in gone, k)
        self.assertEqual(os.path.getsize(self.f["old_log"]), 0)  # emptied, inode kept for launchd
        self.assertEqual(os.path.getsize(self.f["new_log"]), 1000)
        self.assertLess(os.path.getsize(self.f["big_log"]), 200 * 1024)
        self.assertTrue(open(self.f["big_log"], "rb").read().startswith(b"[disk_cleanup]"))
        self.assertTrue(os.path.exists(os.path.join(self.r, "aqd_data/101/SAFE_DONE")))
        self.assertEqual(len(n.calls), 1)
        freed, files = n.calls[0][0], n.calls[0][1]
        self.assertEqual(files, 7); self.assertGreater(freed, 1400000)

    def test_low_disk_keeps_menu_cache_7_days_but_drops_logs_to_3(self):
        menu2 = w(f"{self.r}/menu_ocr_cache/cc.jpg", age=2 * DAY)
        menu6 = w(f"{self.r}/menu_ocr_cache/dd.jpg", age=6 * DAY)
        log4 = w(f"{self.r}/old4.log", age=4 * DAY)
        real = dc.free_gb
        dc.free_gb = lambda p: 1.0
        try:
            res, _ = self.run_it()
        finally:
            dc.free_gb = real
        self.assertTrue(res["ran"])
        self.assertTrue(os.path.exists(menu2)); self.assertTrue(os.path.exists(menu6))
        self.assertFalse(os.path.exists(self.f["menu_old"]))  # > 7 ngày vẫn xoá
        self.assertEqual(os.path.getsize(log4), 0)

    def test_symlink_out_of_root_is_never_followed(self):
        with tempfile.TemporaryDirectory() as out:
            victim = w(f"{out}/keep.jpg")
            os.symlink(victim, os.path.join(self.r, "aqd_data/101/link.jpg"))
            self.run_it(force=True)
            self.assertTrue(os.path.exists(victim))

    def test_schedule(self):
        o = dc.options({})
        self.assertEqual(dc.decide({}, NOW, 50, o)[:2], (True, "định kỳ"))
        self.assertFalse(dc.decide({"last_run": NOW - 3600}, NOW, 50, o)[0])
        self.assertTrue(dc.decide({"last_run": NOW - 7 * 3600}, NOW, 50, o)[0])
        run, trig, low = dc.decide({"last_run": NOW - 3600}, NOW, 4.9, o)
        self.assertTrue(run and low); self.assertIn("4.9", trig)
        self.assertFalse(dc.decide({"last_run": NOW - 600}, NOW, 4.9, o)[0])
        o2 = dc.options({"AQD_CLEANUP_EVERY_H": "2"})
        self.assertFalse(dc.decide({"last_run": NOW - 3600}, NOW, 50, o2)[0])
        self.assertTrue(dc.decide({"last_run": NOW - 3 * 3600}, NOW, 50, o2)[0])
        self.assertFalse(dc.decide({}, NOW, 4.9, dc.options({"AQD_CLEANUP_MIN_FREE_GB": "0", "AQD_CLEANUP_EVERY_H": "1e9"}))[2])

    def test_not_due_does_nothing(self):
        w(os.path.join(self.r, "cleanup_state.json"), json.dumps({"last_run": NOW - 60}).encode())
        res, n = self.run_it(env={"AQD_CLEANUP_MIN_FREE_GB": "0"})
        self.assertFalse(res["ran"]); self.assertTrue(os.path.exists(self.f["done_img"])); self.assertEqual(n.calls, [])

    def test_lock_blocks_second_run(self):
        w(os.path.join(self.r, "cleanup.lock"), b"1")
        res, _ = self.run_it(force=True)
        self.assertFalse(res["ran"]); self.assertTrue(os.path.exists(self.f["done_img"]))


class FoodyTest(unittest.TestCase):
    def test_only_cleaned_places(self):
        with tempfile.TemporaryDirectory() as r:
            d = os.path.join(r, "foody_data")
            a = w(f"{d}/11/photos/1.jpg"); b = w(f"{d}/12/photos/1.jpg"); pj = w(f"{d}/11/place.json")
            st = w(f"{d}/_uploaded.json", b"{}"); rev = w(f"{d}/_reviews_pending.jsonl", age=99 * DAY)
            w(f"{d}/_cleaned.json", b'{"11": 5}')
            env_file = w(f"{r}/crawler.env", age=99 * DAY)
            n = FakeNotifier()
            dc.run("foody", r, force=True, now=NOW, notifier=n, env={}, say=lambda s: None)
            self.assertFalse(os.path.exists(a))
            for p in (b, pj, st, rev, env_file):
                self.assertTrue(os.path.exists(p), p)
            self.assertEqual(n.calls[0][1], 1)
            self.assertTrue(os.path.exists(f"{d}/cleanup_state.json"))


class SlackLineTest(unittest.TestCase):
    def test_one_line(self):
        sent = []
        with tempfile.TemporaryDirectory() as d:
            nt = sn.Notifier("gmaps", d, env={"SLACK_CRAWLER_WEBHOOK_URL": "http://127.0.0.1:9/x", "AQD_HOST_LABEL": "Mac mini"},
                             sender=lambda url, payload: sent.append(payload) or True, now=lambda: 1793790000)
            self.assertTrue(nt.cleanup(3 * 1024 ** 3, 1234, 9.25, "ổ còn 4.9 GB"))
        text = sent[0]["text"]
        self.assertEqual(text.count("\n"), 0)
        self.assertIn("🧹 *gmaps @ Mac mini*", text); self.assertIn("giải phóng 3,0 GB (1.234 file)", text)
        self.assertIn("còn trống 9,2 GB", text)


REPO = os.path.dirname(HERE)


@unittest.skipIf(os.name == "nt", "launchd installer test runs on macOS/Linux")
class MacInstallerTest(unittest.TestCase):
    """mac/install_cleanup.sh with a stub launchctl and a throwaway HOME: plist content, dry-run, uninstall."""

    def test_install_dry_run_uninstall(self):
        import shutil, subprocess
        crawler = "foody" if os.path.exists(os.path.join(REPO, "common", "foody_fetch.py")) else "gmaps"
        with tempfile.TemporaryDirectory() as home:
            bin_ = os.path.join(home, "bin"); os.makedirs(bin_)
            calls = os.path.join(home, "launchctl.calls")
            w(os.path.join(bin_, "launchctl"), f'#!/bin/sh\necho "$@" >> "{calls}"\n'.encode()); os.chmod(os.path.join(bin_, "launchctl"), 0o755)
            root = os.path.join(home, "root")
            if crawler == "foody":
                mod = os.path.join(root, "common", "disk_cleanup.py"); env_root = {"AQD_FOODY_ROOT": root}
                w(os.path.join(root, "foody_data", "_cleaned.json"), b"{}")
            else:
                mod = os.path.join(root, "disk_cleanup.py"); env_root = {"AQD_ROOT": root}
            os.makedirs(os.path.dirname(mod), exist_ok=True)
            shutil.copy(os.path.join(REPO, "common", "disk_cleanup.py"), mod)
            env = dict(os.environ, HOME=home, PATH=bin_ + os.pathsep + os.environ["PATH"], **env_root)
            sh = os.path.join(REPO, "mac", "install_cleanup.sh")
            r = subprocess.run(["sh", sh, "--dry-run"], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("[dry-run]", r.stdout)
            self.assertFalse(os.path.exists(calls))
            r = subprocess.run(["sh", sh], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            pl = open(os.path.join(home, "Library", "LaunchAgents", f"aqd.cleanup.{crawler}.plist")).read()
            for s in (mod, f"<string>{crawler}</string>", "<integer>900</integer>", "cleanup.log"):
                self.assertIn(s, pl)
            self.assertIn("bootstrap", open(calls).read())
            r = subprocess.run(["sh", sh, "--uninstall"], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0); self.assertFalse(os.path.exists(os.path.join(home, "Library", "LaunchAgents", f"aqd.cleanup.{crawler}.plist")))


class ConsoleEncodingTest(unittest.TestCase):
    """Admin (Windows): console cp1252 -> --dry-run không được chết vì chữ tiếng Việt."""
    def test_dry_run_on_cp1252_console(self):
        import subprocess
        with tempfile.TemporaryDirectory() as r:
            w(f"{r}/old.log", age=30 * DAY)
            env = dict(os.environ, PYTHONIOENCODING="cp1252")
            p = subprocess.run([sys.executable, dc.__file__, "--crawler", "gmaps", "--root", r, "--dry-run"],
                               env=env, capture_output=True)
            self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
            self.assertIn(b"[dry-run]", p.stdout)


class SlackResultLoggedTest(unittest.TestCase):
    """Mỗi lần gửi Slack từ job dọn ổ: ghi 'slack: ok' / 'slack: http <code>' vào log, không bao giờ ghi URL webhook."""
    def _serve(self, code):
        import http.server, threading
        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(code); self.end_headers(); self.wfile.write(b"ok" if code == 200 else b"invalid_token")
            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close); self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}/services/T0/B0/SECRETTOKEN42"

    def _run(self, code):
        url = self._serve(code)
        with tempfile.TemporaryDirectory() as r:
            w(f"{r}/old.log", age=30 * DAY)
            env = {"SLACK_CRAWLER_WEBHOOK_URL": url, "AQD_HOST_LABEL": "test", "AQD_SLACK": "1"}
            n = sn.Notifier("gmaps", r, env=env)
            lines = []
            dc.run("gmaps", r, force=True, notifier=n, env={}, say=lines.append)
        text = "\n".join(lines)
        self.assertNotIn("SECRETTOKEN42", text); self.assertNotIn("127.0.0.1", text); self.assertNotIn("/services/", text)
        return lines

    def test_ok_is_logged(self):
        self.assertIn("slack: ok", self._run(200))

    def test_http_error_code_is_logged(self):
        self.assertIn("slack: http 403", self._run(403))

    def test_not_configured_is_logged(self):
        with tempfile.TemporaryDirectory() as r:
            w(f"{r}/old.log", age=30 * DAY)
            n = sn.Notifier("gmaps", r, env={"SLACK_CRAWLER_WEBHOOK_URL": "", "SLACK_CRAWLER_WEBHOOK_FILE": os.path.join(r, "none")})
            lines = []
            dc.run("gmaps", r, force=True, notifier=n, env={}, say=lines.append)
        self.assertIn("slack: chưa cấu hình webhook", lines)



# ---- Windows space report, disk_low alert when 0 bytes freed, Chrome cache (an_uong_quanh_day crawler, Admin 12/11) ----
import ntpath  # noqa: E402
import host_usage as hu  # noqa: E402

GB = 1024 ** 3


class _St:
    def __init__(self, size=0, attrs=0):
        self.st_size, self.st_file_attributes = size, attrs


class _Entry:
    """os.DirEntry look-alike for a fake Windows drive (no is_junction: like Python 3.11 on Admin)."""
    def __init__(self, parent, name, kind, size=0, attrs=0):
        self.name, self.path, self.kind = name, ntpath.join(parent, name), kind
        self._st = _St(size, attrs | (0x10 if kind == "d" else 0x20))

    def is_dir(self, follow_symlinks=True):
        return self.kind == "d"

    def is_symlink(self):
        return False

    def stat(self, follow_symlinks=True):
        return self._st


class FakeDrive:
    def __init__(self, tree):
        self.tree, self.opened = tree, []

    def __call__(self, path):
        self.opened.append(path)
        if path not in self.tree:
            raise PermissionError(path)
        entries = [_Entry(path, *row) for row in self.tree[path]]

        class _It:
            def __iter__(s):
                return iter(entries)

            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False
        return _It()


WIN_TREE = {
    "C:\\": [("Windows", "d"), ("Users", "d"), ("pagefile.sys", "f", 16 * GB), ("hiberfil.sys", "f", 6 * GB),
             ("Documents and Settings", "d", 0, 0x400), ("System Volume Information", "d"), ("aqd", "d")],
    "C:\\Windows": [("big.bin", "f", 20 * GB)],
    "C:\\Users": [("Admin", "d")],
    "C:\\Users\\Admin": [("AppData", "d"), ("Downloads", "d")],
    "C:\\Users\\Admin\\Downloads": [("setup.iso", "f", 9 * GB)],
    "C:\\Users\\Admin\\AppData": [("Local", "d")],
    "C:\\Users\\Admin\\AppData\\Local": [("Docker", "d"), ("Temp", "d")],
    "C:\\Users\\Admin\\AppData\\Local\\Docker": [("ext4.vhdx", "f", 40 * GB)],
    "C:\\Users\\Admin\\AppData\\Local\\Temp": [("a.tmp", "f", 5 * GB)],
    "C:\\aqd": [("aqd_data", "d"), ("chrome-profile", "d"), ("alt.out", "f", 1024)],
    "C:\\aqd\\aqd_data": [("1", "d")],
    "C:\\aqd\\aqd_data\\1": [("p.jpg", "f", 2 * GB)],
    "C:\\aqd\\chrome-profile": [("Default", "d")],
    "C:\\aqd\\chrome-profile\\Default": [("Cache", "f", 3 * GB)],
    # a junction pointing back up: must never be entered (would loop / double count)
    "C:\\Documents and Settings": [("Admin", "d")],
}
TASKLIST = ('"chrome.exe","1234","Console","1","512.000 K"\r\n"chrome.exe","1240","Console","1","512.000 K"\r\n'
            '"python.exe","22","Console","1","1,048,576 K"\r\n"MsMpEng.exe","9","Services","0","300 K"\r\n')
WIN_ENV = {"USERPROFILE": "C:\\Users\\Admin", "LOCALAPPDATA": "C:\\Users\\Admin\\AppData\\Local", "AQD_SPACE_SCAN_SECONDS": "30"}


def _win_stat(path):
    sizes = {"C:\\pagefile.sys": 16 * GB, "C:\\hiberfil.sys": 6 * GB}
    if path not in sizes:
        raise FileNotFoundError(path)
    return _St(sizes[path])


class WindowsSpaceReportTest(unittest.TestCase):
    """Directory sizes on Windows: os.scandir walk (no du), system files, top memory processes via tasklist."""

    def report(self, **kw):
        calls = []
        def runner(args, timeout=30):
            calls.append(args)
            return TASKLIST if args[0] == "tasklist" else ""
        drive = FakeDrive(WIN_TREE)
        text = hu.report("C:\\aqd", env=dict(WIN_ENV, **kw), is_win=True, scandir=drive, stat=_win_stat, runner=runner)
        return text, calls, drive

    def test_windows_measurement_path(self):
        text, calls, drive = self.report()
        self.assertIn("hệ thống: pagefile.sys 16,0 GB, hiberfil.sys 6,0 GB", text)
        self.assertNotIn("swapfile.sys", text)
        self.assertIn("C:\\: Users 54,0 GB, Windows 20,0 GB, pagefile.sys 16,0 GB", text)
        self.assertIn("C:\\Users\\Admin: AppData 45,0 GB, Downloads 9,0 GB", text)
        self.assertIn("C:\\Users\\Admin\\AppData\\Local: Docker 40,0 GB, Temp 5,0 GB", text)
        self.assertIn("C:\\aqd: chrome-profile 3,0 GB, aqd_data 2,0 GB", text)
        self.assertIn("RAM: python.exe 1,0 GB, chrome.exe x2 1000,0 MB", text)
        self.assertEqual([c[0] for c in calls], ["tasklist"])           # no du, no ps on Windows
        self.assertNotIn("C:\\Documents and Settings", drive.opened)    # junction never followed
        self.assertNotIn("Documents and Settings", text)

    def test_time_budget_marks_partial_totals(self):
        t = [0.0]
        def clock():
            t[0] += 1.0
            return t[0]
        size, complete = hu.dir_size("C:\\Users", deadline=3.0, max_entries=10 ** 6, scandir=FakeDrive(WIN_TREE), clock=clock)
        self.assertFalse(complete)
        rows = hu.children_sizes("C:\\", 0.0, 2, scandir=FakeDrive(WIN_TREE))
        self.assertTrue(any(not ok for _n, _b, ok in rows))
        self.assertIn(">=", hu._fmt_rows([("Users", GB, False)], 5))

    def test_tasklist_and_ps_parsing(self):
        self.assertEqual(hu.parse_tasklist('"a.exe","1","Console","1","1.234 K"\n"b.exe","2","S","0","N/A"\n'), [("a.exe", 1234)])
        self.assertEqual(hu.parse_ps(" 2048 /Applications/X.app/Contents/MacOS/X\n bad\n"), [("X", 2048)])
        top = hu.top_memory(runner=lambda a, timeout=30: TASKLIST, is_win=True)
        self.assertEqual(top[0], ("python.exe", 1, 1048576 * 1024))

    def test_real_scandir_walk_does_not_follow_links(self):
        with tempfile.TemporaryDirectory() as r, tempfile.TemporaryDirectory() as out:
            w(f"{r}/a/b/c.bin", b"x" * 5000); w(f"{r}/top.bin", b"x" * 100); w(f"{out}/huge.bin", b"x" * 90000)
            try:
                os.symlink(out, os.path.join(r, "link"))
            except (OSError, NotImplementedError):
                pass
            size, ok = hu.dir_size(r, time.monotonic() + 30, 10 ** 6)
            self.assertEqual((size, ok), (5100, True))


class DiskLowNothingFreedTest(unittest.TestCase):
    """Disk low + cleanup freed 0 bytes -> one disk_low alert listing what takes the space (max 1 / 6h)."""

    def setUp(self):
        self.real = dc.free_gb, dc.top_space_users
        self.addCleanup(self._restore)
        dc.free_gb = lambda p: 1.5

    def _restore(self):
        dc.free_gb, dc.top_space_users = self.real

    def test_alert_lists_space_users(self):
        def fake_top(root, env=None):
            return hu.report("C:\\aqd", env=WIN_ENV, is_win=True, scandir=FakeDrive(WIN_TREE), stat=_win_stat,
                             runner=lambda a, timeout=30: TASKLIST)
        dc.top_space_users = fake_top
        sent = []
        with tempfile.TemporaryDirectory() as r:
            nt = sn.Notifier("gmaps", r, env={"SLACK_CRAWLER_WEBHOOK_URL": "http://127.0.0.1:9/x", "AQD_HOST_LABEL": "Admin",
                                              "SLACK_ALERT_MENTION": "U1ABC", "SLACK_CRAWLER_WEBHOOK_FILE": os.path.join(r, "none")},
                             sender=lambda url, payload: sent.append(payload["text"]) or True, now=lambda: 1793790000)
            lines = []
            res = dc.run("gmaps", r, force=True, now=NOW, notifier=nt, env={}, say=lines.append,
                         chrome_state_fn=lambda: "stopped")
            self.assertEqual((res["freed"], res["files"]), (0, 0))
            self.assertEqual(len(sent), 1)
            text = sent[0]
            for s in ("⚠️ <@U1ABC> *gmaps @ Admin* — `disk_low`", "còn 1,5 GB", "0 byte", "pagefile.sys 16,0 GB",
                      "hiberfil.sys 6,0 GB", "C:\\Users\\Admin\\AppData\\Local: Docker 40,0 GB", "RAM: python.exe 1,0 GB",
                      "Việc cần làm: *dọn ổ đĩa trên Admin*"):
                self.assertIn(s, text)
            self.assertNotIn("🧹", text)
            self.assertTrue(any("chỗ đang chiếm" in l for l in lines))
            dc.run("gmaps", r, force=True, now=NOW + 3600, notifier=nt, env={}, say=lambda s: None)
            self.assertEqual(len(sent), 1)                       # anti-spam: 1 alert / AQD_CLEANUP_ALERT_H (6h)
            dc.run("gmaps", r, force=True, now=NOW + 7 * 3600, notifier=nt, env={}, say=lambda s: None)
            self.assertEqual(len(sent), 2)

    def test_nothing_freed_and_disk_ok_sends_nothing(self):
        dc.free_gb = lambda p: 500.0
        dc.top_space_users = lambda root, env=None: self.fail("must not measure when the disk is fine")
        n = FakeNotifier()
        with tempfile.TemporaryDirectory() as r:
            lines = []
            dc.run("gmaps", r, force=True, now=NOW, notifier=n, env={}, say=lines.append)
        self.assertEqual(n.calls, []); self.assertIn("slack: bỏ qua (không giải phóng gì)", lines)


PROTECTED = ("Default/History", "Default/Cookies", "Default/Login Data", "Default/Network/Cookies", "Local State",
             "Default/Preferences", "Default/GPUCache/data_0", "Default/Service Worker/CacheStorage/abc/x",
             "Default/IndexedDB/https_www.google.com_0.indexeddb.leveldb/000003.log", "Default/Web Data")


class ChromeCacheTest(unittest.TestCase):
    """Only chrome-profile/Default/{Cache, Code Cache}; only when the crawler Chrome is NOT running."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.r = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        cp = os.path.join(self.r, "chrome-profile")
        self.cache = [w(f"{cp}/Default/Cache/Cache_Data/f_000001", b"c" * 4000, age=DAY),
                      w(f"{cp}/Default/Cache/Cache_Data/index", b"c" * 100),
                      w(f"{cp}/Default/Code Cache/js/0a1b", b"j" * 3000)]
        self.protected = [w(f"{cp}/{p}", b"secret") for p in PROTECTED]
        self.out = tempfile.TemporaryDirectory(); self.addCleanup(self.out.cleanup)
        self.victim = w(f"{self.out.name}/outside.bin", b"v" * 10)
        try:
            os.symlink(self.victim, f"{cp}/Default/Cache/Cache_Data/link_out")
        except (OSError, NotImplementedError):
            pass
        self.real = dc.free_gb, dc.top_space_users
        self.addCleanup(lambda: (setattr(dc, "free_gb", self.real[0]), setattr(dc, "top_space_users", self.real[1])))
        dc.free_gb = lambda p: 1.0   # low disk -> AQD_CLEANUP_CHROME_CACHE=low applies
        dc.top_space_users = lambda root, env=None: "fake"

    def run_it(self, state, env=None):
        lines = []
        res = dc.run("gmaps", self.r, force=True, now=NOW, notifier=FakeNotifier(), env=env or {}, say=lines.append,
                     chrome_state_fn=lambda: state)
        return res, lines

    def assert_protected(self):
        for p in self.protected + [self.victim]:
            self.assertTrue(os.path.exists(p), p)
            self.assertEqual(os.path.getsize(p), os.path.getsize(p))

    def test_skipped_while_chrome_running(self):
        for state in ("running", "unknown"):
            res, lines = self.run_it(state)
            for p in self.cache:
                self.assertTrue(os.path.exists(p), (state, p))
            self.assertTrue(any(l.startswith("cache Chrome: bỏ qua") for l in lines), lines)
        self.assert_protected()

    def test_deleted_when_chrome_stopped_and_protected_untouched(self):
        res, _ = self.run_it("stopped")
        for p in self.cache:
            self.assertFalse(os.path.exists(p), p)
        self.assertEqual(res["files"], 3); self.assertEqual(res["freed"], 7100)
        self.assert_protected()
        self.assertTrue(os.path.isdir(os.path.join(self.r, "chrome-profile", "Default", "Cache")))  # folder kept

    def test_mode_low_and_off(self):
        dc.free_gb = lambda p: 500.0
        self.run_it("stopped")                                   # default 'low' + disk fine -> untouched
        self.run_it("stopped", env={"AQD_CLEANUP_CHROME_CACHE": "0"})
        for p in self.cache:
            self.assertTrue(os.path.exists(p), p)
        self.run_it("stopped", env={"AQD_CLEANUP_CHROME_CACHE": "1"})
        self.assertFalse(any(os.path.exists(p) for p in self.cache))
        self.assert_protected()

    def test_forged_plan_never_deletes_protected(self):
        plan = [("cache", p, 6, "forged") for p in self.protected]
        plan.append(("cache", os.path.join(self.r, "chrome-profile", "Default", "Cache", "..", "History"), 6, "forged"))
        freed, files = dc.execute(plan, self.r, dc.options({}), False, say=lambda s: None)
        self.assertEqual((freed, files), (0, 0))
        self.assert_protected()

    def test_cache_dir_symlinked_elsewhere_is_ignored(self):
        import shutil
        cache = os.path.join(self.r, "chrome-profile", "Default", "Code Cache")
        shutil.rmtree(cache)
        os.makedirs(os.path.join(self.out.name, "cc"))
        keep = w(os.path.join(self.out.name, "cc", "x"))
        try:
            os.symlink(os.path.join(self.out.name, "cc"), cache)
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks")
        self.run_it("stopped")
        self.assertTrue(os.path.exists(keep))

    def test_chrome_state_detection(self):
        prof = os.path.join(os.path.abspath(self.r), "chrome-profile")
        no_port = lambda p: False
        self.assertEqual(dc.chrome_state(self.r, 9222, lambda: None, no_port), "unknown")
        self.assertEqual(dc.chrome_state(self.r, 9222, lambda: [(1, "python alt.py")], no_port), "stopped")
        self.assertEqual(dc.chrome_state(self.r, 9222, lambda: [(1, "python alt.py")], lambda p: True), "running")
        self.assertEqual(dc.chrome_state(self.r, 9222, lambda: [(5, f'chrome.exe --type=renderer "--user-data-dir={prof}"')],
                                         no_port), "running")
        self.assertEqual(dc.chrome_state(self.r, 9222, lambda: [(5, "chrome.exe --remote-debugging-port=9222 --user-data-dir=X")],
                                         no_port), "running")
        win = "C:\\aqd"
        self.assertEqual(dc._norm('"--user-data-dir=C:\\AQD\\chrome-profile"').count("c:/aqd/chrome-profile"), 1)
        self.assertIn(dc._norm(win + "\\chrome-profile"), dc._norm('chrome.exe --user-data-dir=C:\\aqd\\chrome-profile'))

    def test_cdp_port_from_env_or_config(self):
        self.assertEqual(dc.cdp_port(self.r, {}), 9222)
        w(os.path.join(self.r, "aqd_config.json"), b'{"cdp_port": 9230}')
        self.assertEqual(dc.cdp_port(self.r, {}), 9230)
        self.assertEqual(dc.cdp_port(self.r, {"AQD_CDP_PORT": "9241"}), 9241)

if __name__ == "__main__":
    unittest.main(verbosity=1)
