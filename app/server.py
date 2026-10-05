"""
HTTP server for the out-of-order trace reviewer.

Endpoints
---------
GET  /                       reviewer web page
GET  /static/<file>          static assets
GET  /health                 health response {"status": "ok"}
POST /api/validate           parse-only check of program + events
POST /api/simulate           one-shot replay, returns full step-by-step report
POST /api/sessions           create an incremental review session
GET  /api/sessions/<id>      current session state (steps up to cursor)
POST /api/sessions/<id>/step advance one event
POST /api/sessions/<id>/reset  restart the replay at event 0

The port is configurable through the PORT environment variable (default 8080).
Only Python standard library is required.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .simulator import (
    DEFAULT_NUM_PHYS,
    Violation,
    parse_events,
    parse_program,
    simulate,
    Simulator,
)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
}

_sessions: dict[str, "Session"] = {}
_sessions_lock = threading.Lock()


class Session:
    def __init__(self, instructions, events, num_phys):
        self.id = uuid.uuid4().hex[:12]
        self.instructions = instructions
        self.events = events
        self.num_phys = num_phys
        self.sim = Simulator(instructions, num_phys)
        self.steps = []
        self.lock = threading.Lock()

    def to_state(self) -> dict:
        with self.lock:
            violation = next((s["violation"] for s in self.steps if s["violation"]), None)
            return {
                "id": self.id,
                "num_instructions": len(self.instructions),
                "num_events": len(self.events),
                "num_phys": self.num_phys,
                "cursor": len(self.steps),
                "done": len(self.steps) >= len(self.events) or violation is not None,
                "violation": violation,
                "instructions": [
                    {"seq": i.seq, "text": i.text, "name": i.name, "op": i.op,
                     "dest": i.dest, "srcs": i.srcs, "is_branch": i.is_branch,
                     "predicted_taken": i.predicted_taken}
                    for i in self.instructions
                ],
                "events": [
                    {"kind": e.kind, "seq": e.seq, "taken": e.taken, "text": e.text,
                     "description": e.describe()}
                    for e in self.events
                ],
                "steps": self.steps,
                "current": self.sim.snapshot(),
                "counters": dict(self.sim.counters),
            }

    def advance(self) -> dict:
        with self.lock:
            violation = next((s["violation"] for s in self.steps if s["violation"]), None)
            if violation is not None:
                return {"already_failed": True}
            if len(self.steps) >= len(self.events):
                return {"exhausted": True}
            step = self.sim.step(self.events[len(self.steps)])
            self.steps.append(step)
            return {"step": _public_step(step)}

    def reset(self) -> dict:
        with self.lock:
            self.sim = Simulator(self.instructions, self.num_phys)
            self.steps = []
            return {"reset": True}


def _public_step(step: dict) -> dict:
    # Steps are already plain dicts; kept as a seam in case the schema evolves.
    return step


def _build_inputs(payload) -> tuple:
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    program = payload.get("program", "")
    events = payload.get("events", "")
    if isinstance(program, list):
        program = "\n".join(str(x) for x in program)
    if isinstance(events, list):
        events = "\n".join(str(x) for x in events)
    if not isinstance(program, str) or not isinstance(events, str):
        raise ValueError("'program' and 'events' must be strings or lists of strings")
    num_phys = payload.get("num_phys", DEFAULT_NUM_PHYS)
    try:
        num_phys = int(num_phys)
    except (TypeError, ValueError):
        raise ValueError("'num_phys' must be an integer") from None
    instructions = parse_program(program)
    parsed_events = parse_events(events)
    if not parsed_events:
        raise ValueError("at least one event is required")
    return instructions, parsed_events, num_phys


class Handler(BaseHTTPRequestHandler):
    server_version = "OOOReviewer/1.0"

    def log_message(self, fmt, *args):  # quiet, structured access log
        if os.environ.get("QUIET") != "1":
            super().log_message(fmt, *args)

    # -- helpers -----------------------------------------------------------

    def _send_json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, rel: str, content_type: str):
        path = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not path.startswith(STATIC_DIR + os.sep) or not os.path.isfile(path):
            self._send_json({"error": "not found"}, 404)
            return
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from None
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    # -- routing -----------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/health":
                self._send_json({"status": "ok", "service": "ooo-trace-reviewer"})
            elif path == "/":
                self._send_static("index.html", CONTENT_TYPES[".html"])
            elif path == "/index.html":
                self._send_static("index.html", CONTENT_TYPES[".html"])
            elif path.startswith("/static/"):
                rel = path[len("/static/"):]
                ext = os.path.splitext(rel)[1]
                self._send_static(rel, CONTENT_TYPES.get(ext, "application/octet-stream"))
            elif path.startswith("/api/sessions/"):
                rest = path[len("/api/sessions/"):]
                sid = rest.strip("/")
                with _sessions_lock:
                    session = _sessions.get(sid)
                if session is None:
                    self._send_json({"error": f"unknown session {sid}"}, 404)
                else:
                    self._send_json(session.to_state())
            else:
                self._send_json({"error": "not found", "path": path}, 404)
        except Exception as exc:  # pragma: no cover - defensive
            self._send_json({"error": str(exc)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/validate":
                payload = self._read_json()
                instructions, events, num_phys = _build_inputs(payload)
                self._send_json({
                    "ok": True,
                    "num_instructions": len(instructions),
                    "num_events": len(events),
                    "num_phys": num_phys,
                    "instructions": [i.name for i in instructions],
                    "events": [e.describe() for e in events],
                })
            elif path == "/api/simulate":
                payload = self._read_json()
                try:
                    instructions, events, num_phys = _build_inputs(payload)
                except ValueError as exc:
                    self._send_json({"ok": False, "error": str(exc)}, 400)
                    return
                report = simulate(instructions, events, num_phys)
                self._send_json(report)
            elif path == "/api/sessions":
                payload = self._read_json()
                try:
                    instructions, events, num_phys = _build_inputs(payload)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, 400)
                    return
                session = Session(instructions, events, num_phys)
                with _sessions_lock:
                    _sessions[session.id] = session
                self._send_json(session.to_state(), 201)
            elif path.startswith("/api/sessions/"):
                rest = path[len("/api/sessions/"):]
                parts = [p for p in rest.split("/") if p]
                if len(parts) == 2 and parts[1] in ("step", "reset"):
                    sid = parts[0]
                    with _sessions_lock:
                        session = _sessions.get(sid)
                    if session is None:
                        self._send_json({"error": f"unknown session {sid}"}, 404)
                        return
                    if parts[1] == "step":
                        result = session.advance()
                        self._send_json({"ok": True, **result, "state": session.to_state()})
                    else:
                        session.reset()
                        self._send_json({"ok": True, "state": session.to_state()})
                else:
                    self._send_json({"error": "not found", "path": path}, 404)
            else:
                self._send_json({"error": "not found", "path": path}, 404)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
        except Violation as exc:  # pragma: no cover - simulate path handles these
            self._send_json({"error": exc.to_dict()}, 422)
        except Exception as exc:  # pragma: no cover - defensive
            self._send_json({"error": str(exc)}, 500)


def create_server(host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None) -> int:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    httpd = create_server(host, port)
    print(f"OOO trace reviewer listening on http://{host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
