"""Tests for common/origin_pin.py (AQD_ORIGIN_IP: call the origin directly, bypassing Cloudflare; an_uong_quanh_day#1058).

A real TLS server runs on 127.0.0.1 with a throwaway CA (made with the `openssl` CLI), so the tests prove that the pin
changes only where the connection goes: SNI, the Host header and the certificate check keep the host name, and a wrong
or untrusted certificate is still refused. No outside network.

Run: python3 <this file>   (the requests/aqd_api checks run only when `requests` and Pillow are installed)
"""
import http.server, json, os, shutil, socket, socketserver, ssl, subprocess, sys, tempfile, threading, unittest, urllib.error, \
    urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
import origin_pin  # noqa: E402
import heartbeat  # noqa: E402

HOST = "aqd-origin.test"  # never resolvable: reaching it at all proves the pin was used

try:
    import requests  # noqa: F401
    import PIL  # noqa: F401
    HAVE_REQUESTS = True
except ImportError:
    HAVE_REQUESTS = False


def _openssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_certs(directory, name):
    """A CA and a leaf certificate for `name` signed by it. Returns (ca.pem, leaf.pem, leaf.key)."""
    _openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "1",
             "-subj", "/CN=AQD Test CA", "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign", cwd=directory)
    _openssl("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", "leaf.key", "-out", "leaf.csr", "-subj", f"/CN={name}",
             cwd=directory)
    with open(os.path.join(directory, "ext.cnf"), "w") as f:
        f.write(f"subjectAltName=DNS:{name}\nbasicConstraints=CA:FALSE\n"
                "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n")
    _openssl("x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out",
             "leaf.pem", "-days", "1", "-extfile", "ext.cnf", cwd=directory)
    return [os.path.join(directory, n) for n in ("ca.pem", "leaf.pem", "leaf.key")]


class _QuickServer(http.server.ThreadingHTTPServer):
    def server_bind(self):  # HTTPServer.server_bind does a reverse DNS lookup (getfqdn), slow on some machines
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class TlsServer:
    """HTTPS server on 127.0.0.1 that records the SNI and the request headers it saw."""

    def __init__(self, cert, key):
        self.seen = {}
        seen = self.seen

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                seen.update(path=self.path, host=self.headers.get("Host"), authorization=self.headers.get("Authorization"))
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                body = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _answer

            def log_message(self, *args):
                pass

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        context.sni_callback = lambda sock, name, ctx: seen.__setitem__("sni", name)
        self.httpd = _QuickServer(("127.0.0.1", 0), Handler)
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@unittest.skipUnless(shutil.which("openssl"), "needs the openssl CLI")
class OriginPinTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(cls.tmp, "good")); os.makedirs(os.path.join(cls.tmp, "other"))
        cls.ca, cert, key = make_certs(os.path.join(cls.tmp, "good"), HOST)
        cls.other_ca, other_cert, other_key = make_certs(os.path.join(cls.tmp, "other"), "someone-else.test")
        cls.server = TlsServer(cert, key)
        cls.wrong_name = TlsServer(other_cert, other_key)

    @classmethod
    def tearDownClass(cls):
        cls.server.close(); cls.wrong_name.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        origin_pin.reset()
        self.server.seen.clear()
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start(); self.addCleanup(env.stop)
        os.environ.pop("AQD_ORIGIN_IP", None)
        self.addCleanup(origin_pin.reset)

    def url(self, server, path="/v1/ingest/heartbeats"):
        return f"https://{HOST}:{server.port}{path}"

    # ---- parsing ---------------------------------------------------------------------------------------------------
    def test_unset_or_empty_changes_nothing(self):
        self.assertIsNone(origin_pin.install("https://anuongquanhday.com"))
        self.assertIsNone(origin_pin.install("https://anuongquanhday.com", "  "))
        self.assertIs(socket.getaddrinfo, origin_pin._REAL_GETADDRINFO)

    def test_ipv4_and_ipv6_literals(self):
        self.assertEqual(("anuongquanhday.com", "203.0.113.10"),
                         origin_pin.parse("https://AnUongQuanhDay.com/", " 203.0.113.10 "))
        self.assertEqual(("anuongquanhday.com", "2001:db8::1"), origin_pin.parse("https://anuongquanhday.com", "2001:db8::1"))

    def test_bad_values_are_refused(self):
        for value in ("anuongquanhday.com", "10.0.0.0/8", "1.2.3", "fe80::1%en0", "1.2.3.4 5.6.7.8"):
            with self.assertRaises(origin_pin.OriginPinError, msg=value):
                origin_pin.parse("https://anuongquanhday.com", value)

    def test_http_base_is_refused(self):
        with self.assertRaises(origin_pin.OriginPinError):
            origin_pin.install("http://anuongquanhday.com", "127.0.0.1")
        with self.assertRaises(origin_pin.OriginPinError):
            origin_pin.install("", "127.0.0.1")

    def test_reads_the_environment(self):
        os.environ["AQD_ORIGIN_IP"] = "127.0.0.1"
        self.assertEqual(("anuongquanhday.com", "127.0.0.1"), origin_pin.install("https://anuongquanhday.com"))
        self.assertEqual("127.0.0.1", origin_pin.pinned("ANUONGQUANHDAY.COM."))
        self.assertIsNone(origin_pin.pinned("www.anuongquanhday.com"))

    def test_only_the_pinned_host_is_rewritten(self):
        origin_pin.install(f"https://{HOST}", "127.0.0.1")
        with mock.patch.object(origin_pin, "_REAL_GETADDRINFO", return_value=["real"]) as real:
            socket.getaddrinfo(HOST, 443)
            socket.getaddrinfo("www.google.com", 443)
            socket.getaddrinfo(HOST.upper().encode(), 443)
        self.assertEqual([("127.0.0.1", 443), ("www.google.com", 443), ("127.0.0.1", 443)],
                         [c.args for c in real.call_args_list])

    # ---- real TLS ----------------------------------------------------------------------------------------------------
    def test_urllib_reaches_the_ip_with_sni_host_and_a_verified_certificate(self):
        origin_pin.install(f"https://{HOST}", "127.0.0.1")
        request = urllib.request.Request(self.url(self.server), data=b"{}", method="POST",
                                         headers={"Authorization": "Bearer t"})
        context = ssl.create_default_context(cafile=self.ca)
        with urllib.request.urlopen(request, timeout=10, context=context) as response:
            self.assertEqual(200, response.status)
        self.assertEqual(HOST, self.server.seen["sni"])
        self.assertEqual(f"{HOST}:{self.server.port}", self.server.seen["host"])
        self.assertEqual("Bearer t", self.server.seen["authorization"])

    def test_a_certificate_for_another_name_is_refused(self):
        origin_pin.install(f"https://{HOST}", "127.0.0.1")
        context = ssl.create_default_context(cafile=self.other_ca)
        with self.assertRaises(urllib.error.URLError) as raised:
            urllib.request.urlopen(self.url(self.wrong_name, "/up"), timeout=10, context=context)
        self.assertIsInstance(raised.exception.reason, ssl.SSLCertVerificationError)

    def test_an_untrusted_certificate_is_refused(self):
        origin_pin.install(f"https://{HOST}", "127.0.0.1")
        with self.assertRaises(urllib.error.URLError) as raised:
            urllib.request.urlopen(self.url(self.server, "/up"), timeout=10)
        self.assertIsInstance(raised.exception.reason, ssl.SSLCertVerificationError)

    def test_without_a_pin_the_test_host_does_not_resolve(self):
        with self.assertRaises(urllib.error.URLError) as raised:
            urllib.request.urlopen(self.url(self.server, "/up"), timeout=10, context=ssl.create_default_context(cafile=self.ca))
        self.assertNotIsInstance(raised.exception.reason, ssl.SSLError)

    @unittest.skipUnless(HAVE_REQUESTS, "needs requests + Pillow (pip install -r requirements.txt)")
    def test_requests_session_of_aqd_api_uses_the_pin(self):
        import aqd_api
        os.environ["AQD_ORIGIN_IP"] = "127.0.0.1"
        client = aqd_api.AQD(base=f"https://{HOST}:{self.server.port}")
        self.assertEqual("127.0.0.1", origin_pin.pinned(HOST))
        response = client.s.get(self.url(self.server, "/v1/admin/restaurants"), timeout=10, verify=self.ca)
        self.assertEqual(200, response.status_code)
        self.assertEqual(HOST, self.server.seen["sni"])
        import requests
        with self.assertRaises(requests.exceptions.SSLError):
            client.s.get(self.url(self.server, "/up"), timeout=10)  # system trust store: throwaway CA refused

    @unittest.skipUnless(HAVE_REQUESTS, "needs requests + Pillow")
    def test_aqd_api_rejects_a_bad_pin_before_any_request(self):
        import aqd_api
        os.environ["AQD_ORIGIN_IP"] = "not-an-ip"
        with self.assertRaises(origin_pin.OriginPinError):
            aqd_api.AQD(base="https://anuongquanhday.com")

    # ---- heartbeat -----------------------------------------------------------------------------------------------------
    def test_heartbeat_reads_the_pin_from_creds_env(self):
        state = tempfile.mkdtemp(dir=self.tmp)
        creds = os.path.join(state, "creds.env")
        with open(creds, "w") as f:
            f.write("AQD_INGEST_TOKEN=tok\nAQD_ORIGIN_IP=127.0.0.1\n")
        with mock.patch.dict(os.environ, {"AQD_INGEST_TOKEN": "", "AQD_BASE": ""}):
            beat = heartbeat.Heartbeat("script_test", state, env_file=creds)
        self.assertTrue(beat.enabled)
        self.assertEqual("127.0.0.1", origin_pin.pinned("anuongquanhday.com"))

    def test_heartbeat_with_a_bad_pin_turns_itself_off(self):
        state = tempfile.mkdtemp(dir=self.tmp)
        with mock.patch.dict(os.environ, {"AQD_ORIGIN_IP": "nope"}):
            beat = heartbeat.Heartbeat("script_test", state, token="tok")
        self.assertFalse(beat.enabled)
        self.assertIn("AQD_ORIGIN_IP", beat.pin_error)
        self.assertNotIn("tok", beat.pin_error)
        self.assertFalse(beat.beat(force=True))

    def test_heartbeat_without_a_pin_leaves_dns_alone(self):
        state = tempfile.mkdtemp(dir=self.tmp)
        beat = heartbeat.Heartbeat("script_test", state, token="tok")
        self.assertTrue(beat.enabled)
        self.assertIs(socket.getaddrinfo, origin_pin._REAL_GETADDRINFO)


if __name__ == "__main__":
    unittest.main()
