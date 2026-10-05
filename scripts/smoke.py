#!/usr/bin/env python3
"""HTTP/API smoke test against a running reviewer instance.

Usage: smoke.py [BASE_URL]
Exits non-zero on the first failed check so the Compose verify service can
propagate the result code.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TARGET_URL", "http://127.0.0.1:8080")

PROG = """
ADD R1,R0,R0
ADD R2,R1,R0
BEQ R1,R2 predict=taken
ADD R3,R1,R2
ADD R4,R3,R1
"""

EVENTS_OK = """
dispatch I0
dispatch I1
dispatch I2
dispatch I3
dispatch I4
writeback I0
writeback I1
commit I0
writeback I3
writeback I2
resolve I2 not-taken
"""

EVENTS_BAD = EVENTS_OK + "\nwriteback I3\n"

# Same static instruction re-fetched after a mispredict: the first
# execution's late completion must not complete the re-fetched execution.
REFETCH_PROG = """
ADD R1,R0,R0
BEQ R1,R0 predict=taken
ADD R2,R1,R0
"""

REFETCH_STALE_EVENTS = """
dispatch I0
dispatch I1
dispatch I2
writeback I0
writeback I1
commit I0
resolve I1 not-taken
dispatch I2
writeback I2#1
"""

REFETCH_OK_EVENTS = """
dispatch I0
dispatch I1
dispatch I2
writeback I0
writeback I1
commit I0
resolve I1 not-taken
dispatch I2
writeback I2#2
commit I1
commit I2
"""

failures = []


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def request(method, path, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE.rstrip("/") + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def wait_for_health(deadline=60):
    start = time.time()
    while time.time() - start < deadline:
        try:
            status, raw = request("GET", "/health", timeout=3)
            if status == 200:
                return json.loads(raw.decode())
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.5)
    return None


def main():
    print(f"smoke target: {BASE}")
    health = wait_for_health()
    check("GET /health returns 200 ok", health is not None and health.get("status") == "ok",
          str(health))

    status, raw = request("GET", "/")
    check("GET / serves the reviewer page",
          status == 200 and "复核".encode() in raw, f"status={status}")

    status, raw = request("GET", "/static/app.js")
    check("GET /static/app.js served", status == 200 and len(raw) > 500, f"status={status}")

    status, raw = request("GET", "/static/styles.css")
    check("GET /static/styles.css served", status == 200 and len(raw) > 200, f"status={status}")

    status, raw = request("POST", "/api/validate",
                          {"program": PROG, "events": EVENTS_OK})
    body = json.loads(raw.decode())
    check("POST /api/validate parses 5 instructions / 11 events",
          status == 200 and body.get("num_instructions") == 5
          and body.get("num_events") == 11, str(body)[:200])

    status, raw = request("POST", "/api/simulate",
                          {"program": PROG, "events": EVENTS_OK})
    body = json.loads(raw.decode())
    check("POST /api/simulate accepts legal trace (mispredict rollback)",
          status == 200 and body.get("ok") is True
          and set(body["final"]["squashed"]) == {3, 4}
          and body["final"]["committed"] == [0], str(body)[:300])

    status, raw = request("POST", "/api/simulate",
                          {"program": PROG, "events": EVENTS_BAD})
    body = json.loads(raw.decode())
    check("POST /api/simulate locates late squashed writeback",
          status == 200 and body.get("ok") is False
          and body["violation"]["code"] == "WRITEBACK_AFTER_SQUASH"
          and body["violation_step"] == 11, str(body.get("violation"))[:200])

    # precise-exception boundary through the API
    exc_prog = "ADD R1,R0,R0\nMUL R2,R1,R0\nADD R3,R2,R1\n"
    exc_events = ("dispatch I0\ndispatch I1\ndispatch I2\nwriteback I0\ncommit I0\n"
                  "writeback I1\nexception I1\nwriteback I2\n")
    status, raw = request("POST", "/api/simulate",
                          {"program": exc_prog, "events": exc_events})
    body = json.loads(raw.decode())
    check("API flags late writeback after precise-exception rollback",
          body.get("ok") is False
          and body["violation"]["code"] == "WRITEBACK_AFTER_SQUASH", str(body)[:300])

    # re-fetch identity boundary (one-shot): stale gen-1 completion rejected
    status, raw = request("POST", "/api/simulate",
                          {"program": REFETCH_PROG, "events": REFETCH_STALE_EVENTS})
    body = json.loads(raw.decode())
    evidence = (body.get("violation") or {}).get("evidence") or {}
    check("API rejects stale gen-1 writeback after re-fetch at first violation",
          status == 200 and body.get("ok") is False
          and body["violation"]["code"] == "WRITEBACK_AFTER_SQUASH"
          and body["violation_step"] == 8
          and evidence.get("stale_generation") == 1
          and evidence.get("live_generation") == 2
          and (evidence.get("stale_execution") or {}).get("status") == "squashed",
          str(body.get("violation"))[:300])

    # re-fetched execution completes via its own completion and commits
    status, raw = request("POST", "/api/simulate",
                          {"program": REFETCH_PROG, "events": REFETCH_OK_EVENTS})
    body = json.loads(raw.decode())
    ledger = {(e["seq"], e["generation"]): e for e in body.get("final", {}).get("executions", [])}
    check("API accepts re-fetched instance own completion then commits",
          status == 200 and body.get("ok") is True
          and body["final"]["committed"] == [0, 1, 2]
          and ledger.get((2, 1), {}).get("status") == "squashed"
          and ledger.get((2, 2), {}).get("status") == "committed",
          str(body)[:300])

    # incremental session API: stale completion must not mark gen-2 done
    status, raw = request("POST", "/api/sessions",
                          {"program": REFETCH_PROG, "events": REFETCH_STALE_EVENTS})
    session = json.loads(raw.decode())
    sid = session.get("id", "")
    check("POST /api/sessions creates refetch session", status == 201 and bool(sid),
          str(session)[:200])
    if sid:
        for _ in range(8):
            request("POST", f"/api/sessions/{sid}/step")
        status, raw = request("POST", f"/api/sessions/{sid}/step")
        step_body = json.loads(raw.decode())
        current = (step_body.get("state") or {}).get("current") or {}
        rob = [(e["seq"], e["generation"], e["done"]) for e in current.get("rob", [])]
        led = {(e["seq"], e["generation"]): e for e in current.get("executions", [])}
        check("session: stale writeback rejected, re-fetched gen-2 stays unfinished",
              (step_body.get("state") or {}).get("violation", {}).get("code")
              == "WRITEBACK_AFTER_SQUASH"
              and rob == [(1, 1, True), (2, 2, False)]
              and led.get((2, 1), {}).get("status") == "squashed"
              and led.get((2, 2), {}).get("done") is False,
              str(step_body)[:300])

    # incremental session API: re-fetched gen-2 own completion then commits
    status, raw = request("POST", "/api/sessions",
                          {"program": REFETCH_PROG, "events": REFETCH_OK_EVENTS})
    ok_session = json.loads(raw.decode())
    osid = ok_session.get("id", "")
    if osid:
        for _ in range(11):
            request("POST", f"/api/sessions/{osid}/step")
        status, raw = request("GET", f"/api/sessions/{osid}")
        final_state = json.loads(raw.decode())
        check("session: own completion lets branch and re-fetched item commit",
              status == 200 and final_state.get("violation") is None
              and final_state.get("done") is True
              and final_state["current"]["committed"] == [0, 1, 2],
              str(final_state)[:300])

    # incremental session API
    status, raw = request("POST", "/api/sessions",
                          {"program": PROG, "events": EVENTS_OK})
    session = json.loads(raw.decode())
    sid = session.get("id", "")
    check("POST /api/sessions creates session", status == 201 and bool(sid), str(session)[:200])
    if sid:
        status, raw = request("POST", f"/api/sessions/{sid}/step")
        step_body = json.loads(raw.decode())
        check("POST /api/sessions/<id>/step advances cursor",
              status == 200 and step_body["state"]["cursor"] == 1, str(step_body)[:200])
        status, raw = request("POST", f"/api/sessions/{sid}/reset")
        reset_body = json.loads(raw.decode())
        check("POST /api/sessions/<id>/reset rewinds cursor",
              status == 200 and reset_body["state"]["cursor"] == 0, str(reset_body)[:200])

    print()
    if failures:
        print(f"SMOKE FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("SMOKE PASSED: all HTTP/API checks succeeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
