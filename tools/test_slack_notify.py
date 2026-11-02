"""Offline tests for common/slack_notify.py + common/watchdog.py (an_uong_quanh_day#1388).
Network only to a fake Slack server on 127.0.0.1; never a real webhook.

Run: python3 <tests dir>/test_slack_notify.py
"""
import http.server, json, os, sys, tempfile, threading, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
import slack_notify as sn  # noqa: E402
import watchdog as wd  # noqa: E402

T0 = sn.datetime(2026, 11, 2, 9, 0, tzinfo=sn.ICT).timestamp()  # 02/11/2026 09:00 UTC+7


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class FakeSlack:
    """A local HTTP server that records bodies; mode 'ok' / '500' / 'slow'."""

    def __init__(self, mode="ok"):
        self.bodies, self.mode = [], mode
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.bodies.append(json.loads(body))
                if outer.mode == "slow":
                    time.sleep(2)
                self.send_response(500 if outer.mode == "500" else 200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/services/T/B/x"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.clock = Clock()
        self.out = []
        self.env = {"SLACK_CRAWLER_WEBHOOK_URL": "http://127.0.0.1:9/x", "AQD_HOST_LABEL": "Admin",
                    "SLACK_CRAWLER_WEBHOOK_FILE": os.path.join(self.dir, "none")}

    def notifier(self, **kw):
        env = dict(self.env, **kw.pop("env", {}))
        return sn.Notifier("foody", self.dir, env=env, now=self.clock,
                           sender=lambda url, p: self.out.append(p["text"]) or True, **kw)


class Config(Base):
    def test_env_then_file_then_nothing(self):
        self.assertEqual(sn.webhook_url({"SLACK_CRAWLER_WEBHOOK_URL": " https://h/x "}), "https://h/x")
        f = os.path.join(self.dir, "hook")
        open(f, "w").write("https://from-file/x\n")
        self.assertEqual(sn.webhook_url({"SLACK_CRAWLER_WEBHOOK_FILE": f}), "https://from-file/x")
        self.assertEqual(sn.webhook_url({"SLACK_CRAWLER_WEBHOOK_FILE": f + "nope"}), "")
        self.assertEqual(sn.webhook_url({"SLACK_CRAWLER_WEBHOOK_URL": "not a url"}), "")

    def test_key_value_secret_file_env_wins(self):
        f = os.path.join(self.dir, "slack_webhook")
        open(f, "w").write("# máy Admin\nSLACK_CRAWLER_WEBHOOK_URL=https://file/x\nAQD_HOST_LABEL=Admin\n"
                           "SLACK_ALERT_MENTION=U0FILE\n")
        conf = sn.settings({"SLACK_CRAWLER_WEBHOOK_FILE": f, "AQD_HOST_LABEL": "Mac mini"})
        self.assertEqual((conf["SLACK_CRAWLER_WEBHOOK_URL"], conf["AQD_HOST_LABEL"], conf["SLACK_ALERT_MENTION"]),
                         ("https://file/x", "Mac mini", "U0FILE"))
        n = sn.Notifier("foody", self.dir, env={"SLACK_CRAWLER_WEBHOOK_FILE": f}, now=self.clock, sender=lambda *a: 1)
        self.assertEqual((n.host, n.enabled, sn.mention(n.env)), ("Admin", True, "<@U0FILE> "))

    def test_missing_webhook_sends_nothing_and_keeps_going(self):
        n = self.notifier(env={"SLACK_CRAWLER_WEBHOOK_URL": ""})
        self.assertFalse(n.enabled)
        self.assertFalse(n.start("x"))
        self.assertEqual(self.out, [])

    def test_off_switch(self):
        self.assertFalse(self.notifier(env={"AQD_SLACK": "0"}).enabled)

    def test_mention_from_env_only(self):
        self.assertEqual(sn.mention({"SLACK_ALERT_MENTION": "U012ABCDEF"}), "<@U012ABCDEF> ")
        self.assertEqual(sn.mention({"SLACK_ALERT_MENTION": "<@W99XY>"}), "<@W99XY> ")
        self.assertEqual(sn.mention({"SLACK_ALERT_MENTION": "@here"}), "")
        self.assertEqual(sn.mention({}), "")

    def test_host_label(self):
        self.assertEqual(self.notifier().host, "Admin")


class Send(Base):
    def test_fake_server_gets_payload(self):
        srv = FakeSlack()
        try:
            self.assertTrue(sn.post_json(srv.url, {"text": "xin chào"}))
            self.assertEqual(srv.bodies, [{"text": "xin chào"}])
        finally:
            srv.close()

    def test_http_500_timeout_and_refused_never_raise(self):
        for mode in ("500", "slow"):
            srv = FakeSlack(mode)
            try:
                self.assertFalse(sn.post_json(srv.url, {"text": "x"}, timeout=0.5))
            finally:
                srv.close()
        self.assertFalse(sn.post_json("http://127.0.0.1:9/x", {"text": "x"}, timeout=0.5))

    def test_notifier_over_real_socket(self):
        srv = FakeSlack()
        try:
            n = sn.Notifier("foody", self.dir, env=dict(self.env, SLACK_CRAWLER_WEBHOOK_URL=srv.url), now=self.clock)
            self.assertTrue(n.start("foody_fetch.py"))
            self.assertIn("bắt đầu chạy", srv.bodies[0]["text"])
            self.assertFalse(srv.bodies[0]["unfurl_links"])
        finally:
            srv.close()

    def test_sender_exception_is_swallowed(self):
        def boom(url, p):
            raise RuntimeError("x")
        n = sn.Notifier("foody", self.dir, env=self.env, now=self.clock, sender=boom)
        self.assertFalse(n.start("x"))


class Status(Base):
    def test_stuck_boundary(self):
        n = self.notifier()
        n.start()
        self.clock.t = T0 + 3 * 3600 - 60  # 2h59
        self.assertFalse(n.check_stuck())
        self.assertEqual(n.status(n._load())[0], "🟢")
        self.clock.t = T0 + 3 * 3600  # 3h00
        self.assertEqual(n.status(n._load())[0], "🟡")
        self.assertTrue(n.check_stuck())
        self.assertIn("KẸT: 3 giờ không có quán/review mới", self.out[-1])
        self.assertIn("0 item mới từ 09:00 (UTC+7)", self.out[-1])

    def test_progress_clears_stuck(self):
        n = self.notifier()
        n.start()
        self.clock.t = T0 + 2 * 3600
        n.record_place(True, photos=3)
        self.clock.t = T0 + 4 * 3600
        self.assertEqual(n.status(n._load())[0], "🟢")

    def test_red_until_progress(self):
        n = self.notifier()
        n.start()
        self.clock.t += 60
        n.error("captcha", "Google hiện captcha")
        self.assertEqual(n.status(n._load())[0], "🔴")
        self.clock.t += 60
        n.count(places=1)
        self.assertEqual(n.status(n._load())[0], "🟢")

    def test_non_severe_error_stays_green(self):
        n = self.notifier()
        n.start()
        self.assertFalse(n.error("http_503", "trang 531 trả 503 busy"))
        self.assertEqual(n.status(n._load())[0], "🟢")


class Window(Base):
    def test_four_hour_window_and_reset(self):
        n = self.notifier()
        n.start()
        n.position("foody page 533, parity even")
        for _ in range(42):
            n.record_place(True, photos=7)
        n.record_place(False)
        self.clock.t = T0 + 3.5 * 3600
        n.count(reviews=96, upload_ok=42, upload_fail=1)
        self.clock.t = T0 + 4 * 3600
        n.tick()
        text = self.out[-1]
        self.assertTrue(text.startswith("🟢 *foody @ Admin* — đang chạy"), text)
        self.assertIn("• Vị trí: foody page 533, parity even", text)
        self.assertIn("• 4 giờ qua (09:00–13:00): quán +42 · ảnh +294 · review +96 · upload ok 42 / fail 1", text)
        self.assertIn("_Báo cáo lúc 02/11 13:00 (UTC+7) · tin kế tiếp ~17:00_", text)
        self.clock.t += 60
        n.tick()
        self.assertEqual(len([x for x in self.out if "giờ qua" in x]), 1)  # not due again
        self.clock.t = T0 + 8 * 3600
        n.tick()
        self.assertIn("quán +0 · ảnh +0 · review +0", [x for x in self.out if "giờ qua" in x][-1])

    def test_counts_add_up_across_processes(self):
        main = self.notifier()
        main.start()
        sn.Notifier("foody", self.dir, env=self.env, now=self.clock, sender=lambda *a: True).record_place(True, 5)
        sn.Notifier("foody", self.dir, env=self.env, now=self.clock, sender=lambda *a: True).record_place(True, 2)
        self.clock.t = T0 + 4 * 3600
        main.report()
        self.assertIn("quán +2 · ảnh +7", self.out[-1])

    def test_track_totals_diffs(self):
        n = self.notifier()
        n.start()
        stats = {"reviews": 10, "upload_ok": 3}
        n.track_totals(lambda: stats)
        n.sync_totals()  # baseline
        stats.update(reviews=25, upload_ok=5)
        n.sync_totals()
        c = n._load()["win"]["counts"]
        self.assertEqual((c["reviews"], c["upload_ok"]), (15, 2))


class AntiSpam(Base):
    def test_one_alert_per_code_per_4h(self):
        n = self.notifier(env={"SLACK_ALERT_MENTION": "U1ABC"})
        self.assertTrue(n.error("captcha", "a"))
        self.assertFalse(n.error("captcha", "b"))
        self.assertTrue(n.error("signin_required", "c"))  # other code
        self.clock.t += 4 * 3600 - 1
        self.assertFalse(n.error("captcha", "d"))
        self.clock.t += 1
        self.assertTrue(n.error("captcha", "e"))
        self.assertTrue(self.out[0].startswith("🔴 <@U1ABC> *foody @ Admin* — LỖI: gặp captcha"))

    def test_dedupe_survives_restart(self):
        self.assertTrue(self.notifier().error("disk_low", "x"))
        self.assertFalse(self.notifier().error("disk_low", "x"))

    def test_empty_pages_threshold(self):
        n = self.notifier()
        for p in range(531, 540):
            n.empty_page(f"{p}")
        self.assertEqual(self.out, [])
        n.empty_page("540")
        self.assertIn("LỖI: `foody_empty_pages`", self.out[-1])
        self.assertIn("10 trang list liên tiếp trả \"0 quán\" (531→540)", self.out[-1])
        n.page_ok()
        self.assertNotIn("empty_streak", n._load())

    def test_disk_low_alert(self):
        n = self.notifier(env={"AQD_DISK_MIN_GB": "100000"})
        self.assertTrue(n.check_disk())
        self.assertIn("ổ đĩa sắp đầy", self.out[-1])
        self.assertIn("dọn ổ đĩa trên Admin", self.out[-1])


class Format(Base):
    def test_start_stop(self):
        n = self.notifier(version="1f5396a")
        n.position("page 655")
        n.start("foody_fetch.py --limit 0 --parity odd")
        self.assertRegex(self.out[0], r"^▶️ \*foody @ Admin\* — bắt đầu chạy\n• Lệnh: `foody_fetch.py --limit 0 "
                                      r"--parity odd` · bản 1f5396a\n• Tiếp tục từ: page 655 · ổ đĩa trống [\d,]+ GB\n"
                                      r"_02/11 09:00 \(UTC\+7\) · báo cáo 4h đầu tiên lúc ~13:00_$")
        n.count(places=35, photos=260, reviews=88, upload_ok=35)
        self.clock.t = T0 + 110 * 60
        n.stop("thấy file STOP")
        self.assertEqual(self.out[1], "⚫ *foody @ Admin* — đã dừng (thấy file STOP)\n"
                                      "• Lần chạy này (09:00–10:50, 1h50): quán +35 · ảnh +260 · review +88 · "
                                      "upload ok 35 / fail 0\n• Dừng tại: page 655\n_02/11 10:50 (UTC+7)_")

    def test_crash_names_exception_and_line(self):
        n = self.notifier()
        try:
            raise ConnectionResetError("[WinError 10054] reset")
        except ConnectionResetError as e:
            n.crash(e)
        self.assertIn("LỖI: process thoát do exception", self.out[0])
        self.assertIn("`ConnectionResetError: [WinError 10054] reset` tại test_slack_notify.py:", self.out[0])
        self.assertIn("sớm nhất 13:00", self.out[0])

    def test_no_webhook_url_in_text(self):
        n = self.notifier()
        n.error("captcha", "lỗi https://hooks.slack.com/services/T/B/SECRET")
        self.assertNotIn("hooks.slack.com", self.out[0])

    def test_numbers(self):
        self.assertEqual(sn.num(37208), "37.208")
        self.assertEqual(sn.gb(13.6), "13,6 GB")


class Watch(Base):
    class Proc:
        def poll(self):
            return 1

    def dog(self, procs_after, procs_before=(), stop=False, restarts=3):
        stop_file = os.path.join(self.dir, "STOP")
        if stop:
            open(stop_file, "w").close()
        calls = {"n": 0}

        def lister():
            calls["n"] += 1
            return list(procs_before) if calls["n"] == 1 else list(procs_after)
        return wd.Watchdog("foody", self.dir, r"foody_fetch\.py", "aqd_foody.cmd", [stop_file], wait=10,
                           max_restarts=restarts, env=dict(self.env, SLACK_ALERT_MENTION="U1ABC"), now=self.clock,
                           sender=lambda url, p: self.out.append(p["text"]) or True, lister=lister,
                           starter=lambda cmd: self.Proc(), sleep=lambda s: None)

    def test_alive_does_nothing(self):
        self.assertEqual(self.dog([], procs_before=[(3224, "python foody_fetch.py --parity even")]).run(), "alive")
        self.assertEqual(self.out, [])

    def test_stop_file_does_nothing(self):
        self.assertEqual(self.dog([]).__class__.__name__, "Watchdog")
        self.assertEqual(self.dog([], stop=True).run(), "stop")
        self.assertEqual(self.out, [])

    def test_dead_restarted(self):
        self.assertEqual(self.dog([(9120, "python foody_fetch.py")]).run(), "restarted")
        self.assertTrue(self.out[0].startswith("⚫→🟢 *foody @ Admin* — watchdog: process đã chết, đã khởi động lại"))
        self.assertIn("→ PID 9120 đang chạy", self.out[0])

    def test_dead_not_restarted_is_deduped(self):
        self.assertEqual(self.dog([]).run(), "failed")
        self.assertIn("⚫ <@U1ABC> *foody @ Admin* — watchdog: crawler đã dừng, KHÔNG khởi động lại được", self.out[0])
        self.assertIn("thất bại (exit 1)", self.out[0])
        self.assertEqual(self.dog([]).run(), "failed")
        self.assertEqual(len(self.out), 1)

    def test_flapping_goes_red(self):
        for _ in range(3):
            self.dog([(1, "foody_fetch.py")]).run()
        self.assertEqual(self.dog([(1, "foody_fetch.py")]).run(), "flapping")
        self.assertIn("🔴 <@U1ABC> *foody @ Admin* — watchdog: đã khởi động lại 4 lần hôm nay", self.out[-1])

    def test_find_skips_itself_and_watchdog(self):
        procs = [(1, "python watchdog.py --match foody_fetch.py"), (2, "python foody_fetch.py"), (3, "bash")]
        self.assertEqual(wd.find(r"foody_fetch\.py", procs, me=99), [2])

    def test_list_processes_sees_this_process(self):
        self.assertTrue(any(pid == os.getpid() for pid, _ in wd.list_processes()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
