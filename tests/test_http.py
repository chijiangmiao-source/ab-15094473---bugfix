"""End-to-end HTTP / API tests against the stdlib server."""

import json
import os
import sys
import threading
import time
import unittest
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.server import create_server  # noqa: E402

PROG = """
ADD R1,R0,R0
ADD R2,R1,R0
BEQ R1,R2 predict=taken
ADD R3,R1,R2
"""

EVENTS_OK = """
dispatch I0
dispatch I1
dispatch I2
dispatch I3
writeback I0
writeback I1
commit I0
writeback I3
writeback I2
resolve I2 not-taken
"""

EVENTS_BAD = EVENTS_OK + "\nwriteback I3"  # I3 squashed: late writeback rejected


class ServerHarness:
    def __init__(self):
        self.httpd = create_server("127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        time.sleep(0.02)
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def request(self, path, method="GET", body=None, expect_status=None,
                json_body=True):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.url(path), data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            raw = e.read()
            status = e.code
        payload = json.loads(raw.decode()) if (json_body and raw) else raw
        if expect_status is not None:
            assert status == expect_status, f"{path}: expected {expect_status}, got {status}"
        return status, payload


class HttpApiTests(unittest.TestCase):
    def test_health(self):
        with ServerHarness() as h:
            status, body = h.request("/health", expect_status=200)
            self.assertEqual(body["status"], "ok")

    def test_index_and_static(self):
        with ServerHarness() as h:
            for path in ("/", "/static/app.js", "/static/styles.css"):
                status, body = h.request(path, expect_status=200, json_body=False)
                self.assertGreater(len(body), 100)

    def test_validate_ok(self):
        with ServerHarness() as h:
            status, body = h.request("/api/validate", "POST",
                                     {"program": PROG, "events": EVENTS_OK}, 200)
            self.assertEqual(body["num_instructions"], 4)
            self.assertEqual(body["num_events"], 10)

    def test_validate_bad_input_returns_400(self):
        with ServerHarness() as h:
            status, body = h.request("/api/validate", "POST",
                                     {"program": "FOO R1,R0,R0", "events": "dispatch 0"}, 400)
            self.assertIn("unknown opcode", body["error"])

    def test_simulate_legal_trace(self):
        with ServerHarness() as h:
            status, body = h.request("/api/simulate", "POST",
                                     {"program": PROG, "events": EVENTS_OK}, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(len(body["steps"]), 10)
            self.assertIn("before", body["steps"][0])
            self.assertIn("after", body["steps"][0])

    def test_simulate_locates_first_violation(self):
        with ServerHarness() as h:
            status, body = h.request("/api/simulate", "POST",
                                     {"program": PROG, "events": EVENTS_BAD}, 200)
            self.assertFalse(body["ok"])
            self.assertEqual(body["violation"]["code"], "WRITEBACK_AFTER_SQUASH")
            self.assertEqual(body["violation_step"], 10)
            self.assertEqual(len(body["steps"]), 11)

    def test_session_step_through(self):
        with ServerHarness() as h:
            status, s = h.request("/api/sessions", "POST",
                                  {"program": PROG, "events": EVENTS_BAD}, 201)
            sid = s["id"]
            self.assertEqual(s["cursor"], 0)
            for _ in range(10):
                status, r = h.request(f"/api/sessions/{sid}/step", "POST", expect_status=200)
                self.assertIsNone(r["state"]["violation"])
            status, r = h.request(f"/api/sessions/{sid}/step", "POST", expect_status=200)
            self.assertEqual(r["state"]["violation"]["code"], "WRITEBACK_AFTER_SQUASH")
            self.assertTrue(r["state"]["done"])
            # further steps are no-ops
            status, r = h.request(f"/api/sessions/{sid}/step", "POST", expect_status=200)
            self.assertTrue(r["already_failed"])
            # reset replays from scratch
            status, r = h.request(f"/api/sessions/{sid}/reset", "POST", expect_status=200)
            self.assertEqual(r["state"]["cursor"], 0)

    def test_unknown_session_404(self):
        with ServerHarness() as h:
            h.request("/api/sessions/nope/step", "POST", expect_status=404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
