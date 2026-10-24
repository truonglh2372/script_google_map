"""Offline tests for common/heartbeat.py (an_uong_quanh_day#1021). No network: HTTP goes to a fake opener.

Run: python3 -m unittest discover -s tools -p 'test_heartbeat.py'   (or: python3 tools/test_heartbeat.py)
"""
import io, json, os, sys, tempfile, unittest, urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
import heartbeat  # noqa: E402

CRAWLER = os.environ.get("HB_TEST_CRAWLER", "script_google_map")


class FakeResponse:
    def __init__(self, status):
        self.status = status

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes) or [201]
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        if outcome >= 400:
            raise urllib.error.HTTPError(request.full_url, outcome, "x", {}, io.BytesIO(b"{}"))
        return FakeResponse(outcome)

    def bodies(self):
        return [json.loads(r.data.decode("utf-8")) for r in self.requests]

    def keys(self):
        return [r.get_header("Idempotency-key") for r in self.requests]


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.clock = [1_000_000.0]
        env = {"CRAWLER_HOSTNAME": "may-01", "AQD_HEARTBEAT": "1", "AQD_HEARTBEAT_EVERY_MIN": "15"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, opener, token="tok-secret"):
        return heartbeat.Heartbeat(CRAWLER, self.dir, base="https://example.test/", token=token, opener=opener,
                                   now=lambda: self.clock[0])

    def test_posts_the_accumulated_counts_with_the_machine_token(self):
        opener = FakeOpener(201)
        hb = self.make(opener)
        hb.record_result("ok", "info=12 photos=+7/of9 status=published 3.1s")
        hb.record_result("warn", "photos=+2/fail1/of3 | validate_failed")
        hb.record_result("skip", "not an unnamed draft any more")
        hb.record_result("error", "APIError: boom")
        self.assertTrue(hb.beat("FETCH 20 drafts", force=True))

        request = opener.requests[0]
        self.assertEqual("https://example.test/v1/ingest/heartbeats", request.full_url)
        self.assertEqual("POST", request.get_method())
        self.assertEqual("Bearer tok-secret", request.get_header("Authorization"))
        self.assertTrue(request.get_header("Idempotency-key").startswith(CRAWLER + "-hb-"))
        body = opener.bodies()[0]
        self.assertEqual(CRAWLER, body["crawler"])
        self.assertEqual("may-01", body["host"])
        self.assertEqual("FETCH 20 drafts", body["activity"])
        self.assertEqual((2, 9, 0, 1, 0), (body["processed_count"], body["photo_count"], body["dish_count"],
                                           body["error_count"], body["rate_limited_count"]))
        self.assertEqual("running", body["status"])
        self.assertEqual("error: APIError: boom", body["note"])
        self.assertIsInstance(body["disk_free_mb"], int)

    def test_the_body_has_only_the_web_1024_contract_fields(self):
        opener = FakeOpener(201)
        hb = self.make(opener)
        hb.record_place(True, photos=2, dishes=5)
        self.clock[0] += 120
        self.assertTrue(hb.beat("x" * 300, range_label="r" * 150, force=True))
        body = opener.bodies()[0]
        allowed = {"crawler", "host", "status", "activity", "range_label", "window_seconds", "processed_count",
                   "photo_count", "dish_count", "error_count", "rate_limited_count", "disk_free_mb", "note"}
        self.assertLessEqual(set(body), allowed)
        self.assertEqual(120, body["window_seconds"])
        self.assertEqual(200, len(body["activity"]))
        self.assertEqual(100, len(body["range_label"]))
        self.assertIn(body["status"], ("running", "idle", "blocked", "error"))

    def test_an_idle_heartbeat_covers_the_time_since_the_last_one(self):
        opener = FakeOpener(201)
        hb = self.make(opener)
        self.assertTrue(hb.beat(force=True))
        self.clock[0] += 600
        self.assertTrue(hb.beat("end of list, sleeping", status="idle", force=True))
        body = opener.bodies()[1]
        self.assertEqual(("idle", 600, 0), (body["status"], body["window_seconds"], body["processed_count"]))

    def test_only_errors_means_error_and_a_crawler_slug_is_required(self):
        opener = FakeOpener(201)
        hb = self.make(opener)
        hb.record_place(False)
        hb.beat(force=True)
        self.assertEqual("error", opener.bodies()[0]["status"])
        with self.assertRaises(ValueError):
            heartbeat.Heartbeat("Bad Name", self.dir, token="t")

    def test_counts_reset_after_an_accepted_heartbeat_and_sends_are_spaced(self):
        opener = FakeOpener(201)
        hb = self.make(opener)
        hb.record_place(True, photos=3)
        self.assertTrue(hb.beat(force=True))
        hb.record_place(True, photos=1)
        self.assertFalse(hb.beat())            # 15 minutes not yet passed
        self.clock[0] += 15 * 60
        self.assertTrue(hb.beat())
        self.assertEqual([1, 1], [b["processed_count"] for b in opener.bodies()])
        self.assertEqual("running", opener.bodies()[1]["status"])

    def test_a_failed_send_keeps_the_counts_and_retries_the_same_body_with_the_same_key(self):
        opener = FakeOpener(503, OSError("down"), 201)
        hb = self.make(opener)
        hb.record_place(True, photos=4)
        self.assertFalse(hb.beat(force=True))
        self.assertEqual("http 503", hb.last_error)
        self.assertFalse(hb.beat(force=True))
        self.assertEqual("OSError", hb.last_error)
        self.assertTrue(hb.beat(force=True))
        self.assertEqual(1, len(set(opener.keys())))
        self.assertEqual(1, len({json.dumps(b, sort_keys=True) for b in opener.bodies()}))
        self.assertEqual(1, opener.bodies()[2]["processed_count"])

    def test_new_counts_after_a_failure_start_a_new_key(self):
        opener = FakeOpener(500, 201)
        hb = self.make(opener)
        hb.record_place(True)
        hb.beat(force=True)
        hb.record_place(True)
        self.assertTrue(hb.beat(force=True))
        self.assertNotEqual(opener.keys()[0], opener.keys()[1])
        self.assertEqual(2, opener.bodies()[1]["processed_count"])

    def test_a_refused_body_is_dropped_not_resent_forever(self):
        opener = FakeOpener(422, 201)
        hb = self.make(opener)
        hb.record_place(True)
        self.assertFalse(hb.beat(force=True))
        self.assertTrue(hb.beat(force=True))
        self.assertNotEqual(opener.keys()[0], opener.keys()[1])

    def test_a_blocking_issue_makes_the_crawler_blocked_and_goes_into_the_note(self):
        opener = FakeOpener(201)
        hb = self.make(opener)
        hb.note("ip_blocked", "Maps BLOCKED; back off 2h")
        hb.note("made_up_issue")
        self.assertTrue(hb.beat(force=True))
        body = opener.bodies()[0]
        self.assertEqual("blocked", body["status"])
        self.assertEqual("ip_blocked; Maps BLOCKED; back off 2h", body["note"])
        self.assertNotIn("made_up_issue", body["note"])

    def test_rate_limits_are_counted_and_an_explicit_status_wins(self):
        opener = FakeOpener(201)
        hb = self.make(opener)
        hb.note("rate_limited", "busy")
        hb.note("rate_limited", "busy again")
        self.assertTrue(hb.beat(status="error", force=True))
        body = opener.bodies()[0]
        self.assertEqual((2, "error"), (body["rate_limited_count"], body["status"]))

    def test_disk_free_is_reported_as_is(self):
        opener = FakeOpener(201)
        hb = self.make(opener)
        hb.disk_free_mb = lambda: 300
        hb.beat(force=True)
        self.assertEqual(300, opener.bodies()[0]["disk_free_mb"])

    def test_no_token_or_disabled_means_no_request_and_no_state(self):
        opener = FakeOpener(201)
        hb = self.make(opener, token="")
        hb.record_place(True)
        self.assertFalse(hb.beat(force=True))
        with mock.patch.dict(os.environ, {"AQD_HEARTBEAT": "0"}):
            off = self.make(opener)
            off.record_place(True)
            self.assertFalse(off.beat(force=True))
        self.assertEqual([], opener.requests)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "heartbeat_state.json")))

    def test_failures_never_raise_and_never_expose_the_token(self):
        hb = self.make(FakeOpener(RuntimeError("tok-secret leaked?")))
        hb.record_place(True)
        self.assertFalse(hb.beat(force=True))
        self.assertEqual("RuntimeError", hb.last_error)
        with open(os.path.join(self.dir, "heartbeat_state.json"), encoding="utf-8") as f:
            self.assertNotIn("tok-secret", f.read())


    def test_the_token_can_come_from_a_creds_env_file(self):
        creds = os.path.join(self.dir, "creds.env")
        with open(creds, "w", encoding="utf-8") as f:
            f.write("AQD_ADMIN_EMAIL=a@b.c\nAQD_INGEST_TOKEN='file-token'\nAQD_BASE=https://staging.example.test/\n")
        opener = FakeOpener(201)
        with mock.patch.dict(os.environ, {"AQD_INGEST_TOKEN": "", "AQD_BASE": ""}):
            hb = heartbeat.Heartbeat(CRAWLER, self.dir, opener=opener, env_file=creds)
            self.assertTrue(hb.beat(force=True))
        self.assertEqual("Bearer file-token", opener.requests[0].get_header("Authorization"))
        self.assertEqual("https://staging.example.test/v1/ingest/heartbeats", opener.requests[0].full_url)


if __name__ == "__main__":
    unittest.main()
