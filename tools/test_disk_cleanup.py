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


if __name__ == "__main__":
    unittest.main(verbosity=1)
