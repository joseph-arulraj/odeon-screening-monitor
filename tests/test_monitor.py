"""Offline tests. A local fake stands in for odeon.co.uk, the Vista API and ntfy.

Run with:  python -m unittest discover -s tests -v
"""
import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor  # noqa: E402


def make_jwt(lifetime_seconds=12 * 3600, marker="a"):
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    exp = int(time.time()) + lifetime_seconds
    return f"{part({'alg': 'none'})}.{part({'exp': exp, 'm': marker})}.sig{marker}"


def showtime(show_id, site_id, film_id="HO1", hour=18):
    return {
        "id": show_id,
        "siteId": site_id,
        "filmId": film_id,
        "isSoldOut": False,
        "attributeIds": ["imax"],
        "schedule": {
            "startsAt": f"2026-12-16T{hour:02d}:00:00Z",
            "filmStartsAt": f"2026-12-16T{hour:02d}:25:00Z",
        },
    }


class Fake:
    """Shared, mutable behaviour for the fake server."""

    def __init__(self):
        self.valid_token = make_jwt()
        self.page_token = self.valid_token  # what the website hands out
        self.block = False                  # Cloudflare-style 403 on everything
        self.page_has_token = True
        self.shows = {"155": [], "153": []}
        self.ntfy = []
        self.ntfy_fail_after = None         # fail once this many have been accepted
        self.page_hits = 0
        self.api_hits = 0


class Handler(BaseHTTPRequestHandler):
    fake = None

    def log_message(self, *args):
        pass

    def _send(self, code, body, content_type, server=None):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if server:
            self.send_header("Server", server)
        self.end_headers()
        self.wfile.write(data)

    def send_response(self, code, message=None):
        # Suppress the default Server header so tests control it.
        self.send_response_only(code, message)

    def do_GET(self):
        fake = self.fake
        if fake.block:
            self._send(403, "<!DOCTYPE html><title>Attention Required!</title>",
                       "text/html; charset=UTF-8", server="cloudflare")
            return
        if self.path in ("/my-account/", "/"):
            fake.page_hits += 1
            token = f'"authToken":"{fake.page_token}"' if fake.page_has_token else ""
            self._send(200, "<html><script>window.initialData={\"api\":{" + token +
                       "}}</script></html>", "text/html; charset=UTF-8")
            return
        if self.path.startswith("/ocapi/v1/showtimes/by-business-date/2026-12-16"):
            fake.api_hits += 1
            if self.headers.get("Authorization") != f"Bearer {fake.valid_token}":
                self._send(401, '{"status":401,"title":"Authentication Token Failed"}',
                           "application/json")
                return
            site_id = self.path.split("siteIds=")[1]
            self._send(200, json.dumps({
                "businessDate": "2026-12-16",
                "showtimes": fake.shows[site_id],
                "relatedData": {
                    "films": [{"id": "HO1", "title": {"text": "Amélie 映画 🎬"}}],
                    "sites": [{"id": site_id, "name": {"text": f"ODEON {site_id}"}}],
                    "attributes": [{"id": "imax", "name": {"text": "IMAX"}}],
                },
            }), "application/json")
            return
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        fake = self.fake
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if fake.ntfy_fail_after is not None and len(fake.ntfy) >= fake.ntfy_fail_after:
            self._send(500, "boom", "text/plain")
            return
        fake.ntfy.append(json.loads(body))
        self._send(200, "{}", "application/json")


class MonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.fake = Handler.fake = Fake()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self._saved = (monitor.STATE_PATH, monitor.TOKEN_PATH, dict(os.environ))
        monitor.STATE_PATH = tmp / "state.json"
        monitor.TOKEN_PATH = tmp / ".odeon_token.json"
        os.environ.update({
            "ODEON_SITE_URL": self.base,
            "ODEON_API_URL": self.base,
            "NTFY_URL": self.base,
        })
        os.environ.pop("ODEON_BEARER_TOKEN", None)
        self.addCleanup(self._restore)

    def _restore(self):
        monitor.STATE_PATH, monitor.TOKEN_PATH, env = self._saved
        os.environ.clear()
        os.environ.update(env)

    def state(self):
        return json.loads(monitor.STATE_PATH.read_text())

    # ------------------------------------------------------------------ tests
    def test_baseline_then_alert_for_new_session_only(self):
        self.fake.shows["155"] = [showtime("155-1", "155")]
        self.assertTrue(monitor.run_once("topic"))
        self.assertEqual(self.fake.ntfy, [])
        self.assertEqual(self.state()["seen"], ["155-1"])

        self.fake.shows["153"] = [showtime("153-9", "153", hour=20)]
        self.assertTrue(monitor.run_once("topic"))
        self.assertEqual(len(self.fake.ntfy), 1)
        sent = self.fake.ntfy[0]
        self.assertEqual(sent["topic"], "topic")
        self.assertEqual(sent["title"], "New Odeon showtime: Amélie 映画 🎬")
        self.assertIn("Cinema: ODEON 153", sent["message"])
        self.assertIn("Film begins: 20:25", sent["message"])
        self.assertIn("Features: IMAX", sent["message"])
        self.assertTrue(sent["click"].endswith("/ticketing/seat-picker/?showtimeId=153-9"))

        # Nothing new: no repeat alert.
        self.assertTrue(monitor.run_once("topic"))
        self.assertEqual(len(self.fake.ntfy), 1)

    def test_token_is_fetched_once_and_reused(self):
        for _ in range(3):
            self.assertTrue(monitor.run_once("topic"))
        self.assertEqual(self.fake.page_hits, 1)
        self.assertEqual(self.fake.api_hits, 6)

    def test_rejected_token_is_replaced_automatically(self):
        self.assertTrue(monitor.run_once("topic"))
        # Odeon rotates: the cached token stops working, the site hands out a new one.
        self.fake.valid_token = self.fake.page_token = make_jwt(marker="b")
        self.assertTrue(monitor.run_once("topic"))
        self.assertEqual(self.fake.page_hits, 2)

    def test_nearly_expired_cached_token_is_refreshed_before_use(self):
        old = make_jwt(lifetime_seconds=120, marker="old")
        monitor.TOKEN_PATH.write_text(json.dumps({
            "value": old, "expires": monitor.jwt_expiry(old).isoformat()}))
        self.assertTrue(monitor.run_once("topic"))
        self.assertEqual(self.fake.page_hits, 1)
        self.assertEqual(json.loads(monitor.TOKEN_PATH.read_text())["value"],
                         self.fake.valid_token)

    def test_manual_token_that_expired_gives_a_clear_error(self):
        os.environ["ODEON_BEARER_TOKEN"] = make_jwt(marker="stale")
        with self.assertRaises(monitor.MonitorError) as ctx:
            monitor.fetch("155")
        self.assertIn("ODEON_BEARER_TOKEN you set has expired", str(ctx.exception))
        self.assertEqual(self.fake.page_hits, 0)

    def test_cloudflare_block_is_reported_as_an_ip_block(self):
        self.fake.block = True
        with self.assertRaises(monitor.Blocked) as ctx:
            monitor.fetch("155")
        self.assertIn("not a token problem", str(ctx.exception))

    def test_failure_alert_is_sent_once_and_recovery_is_announced(self):
        self.assertTrue(monitor.run_once("topic"))  # baseline
        self.fake.block = True
        self.assertFalse(monitor.run_once("topic"))
        self.assertEqual(self.fake.ntfy, [])        # too soon to bother the phone

        state = self.state()
        state["failing_since"] = (
            datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
        monitor.STATE_PATH.write_text(json.dumps(state))
        self.assertFalse(monitor.run_once("topic"))
        self.assertFalse(monitor.run_once("topic"))
        self.assertEqual([n["title"] for n in self.fake.ntfy],
                         ["Odeon monitor is not getting through"])

        self.fake.block = False
        self.assertTrue(monitor.run_once("topic"))
        self.assertEqual(self.fake.ntfy[-1]["title"], "Odeon monitor is working again")
        self.assertNotIn("failing_since", self.state())
        self.assertNotIn("failure_alerted", self.state())

    def test_failed_notification_is_retried_without_duplicating_earlier_ones(self):
        self.assertTrue(monitor.run_once("topic"))  # baseline
        self.fake.shows["155"] = [showtime("155-1", "155"), showtime("155-2", "155")]
        self.fake.ntfy_fail_after = 1
        self.assertFalse(monitor.run_once("topic"))
        self.assertEqual(self.state()["seen"], ["155-1"])
        self.fake.ntfy_fail_after = None
        self.assertTrue(monitor.run_once("topic"))
        self.assertEqual(len(self.fake.ntfy), 2)
        self.assertEqual(self.state()["seen"], ["155-1", "155-2"])

    def test_page_without_token_fails_cleanly(self):
        self.fake.page_has_token = False
        self.assertFalse(monitor.run_once("topic"))
        self.assertFalse(self.state().get("initialised"))

    def test_env_file_is_loaded_without_overriding_real_environment(self):
        env_file = Path(self.tmp.name) / ".env"
        env_file.write_text('# comment\nNTFY_TOPIC="from-file"\nNTFY_URL=ignored\n')
        os.environ.pop("NTFY_TOPIC", None)
        monitor.load_env_file(env_file)
        self.assertEqual(os.environ["NTFY_TOPIC"], "from-file")
        self.assertEqual(os.environ["NTFY_URL"], self.base)


if __name__ == "__main__":
    unittest.main()
