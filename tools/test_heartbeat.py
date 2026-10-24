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
        self.assertEqual("may-01", body["hostname"])
        self.assertEqual("FETCH 20 drafts", body["activity"])
        self.assertEqual({"restaurants": 2, "photos": 9, "dishes": 0, "errors": 1}, body["counts"])
        self.assertEqual("degraded", body["status"])
        self.assertEqual("error: APIError: boom", body["error_summary"])
        self.assertIsInstance(body["disk_free_mb"], int)

    def test_counts_reset_after_an_accepted_heartbeat_and_sends_are_spaced(self):
        opener = FakeOpener(201)
        hb = self.make(opener)
        hb.record_place(True, photos=3)
        self.assertTrue(hb.beat(force=True))
        hb.record_place(True, photos=1)
        self.assertFalse(hb.beat())            # 15 minutes not yet passed
        self.clock[0] += 15 * 60
        self.assertTrue(hb.beat())
        self.assertEqual([1, 1], [b["counts"]["restaurants"] for b in opener.bodies()])
        self.assertEqual("ok", opener.bodies()[1]["status"])

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
        self.assertEqual(1, opener.bodies()[2]["counts"]["restaurants"])

    def test_new_counts_after_a_failure_start_a_new_key(self):
        opener = FakeOpener(500, 201)
        hb = self.make(opener)
        hb.record_place(True)
        hb.beat(force=True)
        hb.record_place(True)
        self.assertTrue(hb.beat(force=True))
        self.assertNotEqual(opener.keys()[0], opener.keys()[1])
        self.assertEqual(2, opener.bodies()[1]["counts"]["restaurants"])

    def test_a_refused_body_is_dropped_not_resent_forever(self):
        opener = FakeOpener(409, 201)
        hb = self.make(opener)
        hb.record_place(True)
        self.assertFalse(hb.beat(force=True))
        self.assertTrue(hb.beat(force=True))
        self.assertNotEqual(opener.keys()[0], opener.keys()[1])

    def test_issues_are_limited_to_the_known_list(self):
        opener = FakeOpener(201)
        hb = self.make(opener)
        hb.note("ip_blocked", "Maps BLOCKED; back off 2h")
        hb.note("made_up_issue")
        self.assertTrue(hb.beat(status="error", force=True))
        body = opener.bodies()[0]
        self.assertEqual(["ip_blocked"], [i for i in body["issues"] if i != "disk_full"])
        self.assertEqual("error", body["status"])
        self.assertEqual("Maps BLOCKED; back off 2h", body["error_summary"])

    def test_low_disk_adds_disk_full(self):
        opener = FakeOpener(201)
        hb = self.make(opener)
        hb.disk_free_mb = lambda: 300
        hb.beat(force=True)
        self.assertEqual(["disk_full"], opener.bodies()[0]["issues"])
        self.assertEqual("degraded", opener.bodies()[0]["status"])

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
