"""In-process fake EMS servers for tests.

* FakeIclockServer — speaks the ZKTeco iClock/ADMS HTTP push protocol.
* FakeRestServer  — speaks the documented REST bridge contract
                    (docs/EMS_API_CONTRACT.md), with token auth + idempotency.

Both are real HTTP servers (threaded) on an ephemeral localhost port so the
clients are exercised end-to-end, sockets and JSON parsing included.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep pytest output clean
        pass

    def _reply(self, status: int, body: str | bytes, ctype: str = "text/plain"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# --------------------------------------------------------------------------- --
class FakeIclockServer:
    """State: accepted = list of {sn, pin, ts, status, verify} uploads."""

    def __init__(self):
        self.accepted: list[dict] = []
        self.seen: set[tuple] = set()
        self.handshakes: list[str] = []
        self.polls: list[str] = []
        self.user_uploads: list[dict] = []      # {"sn": str, "users": {...}}
        self.attlog_posts: list[dict] = []      # {"sn": str, "lines": [...]}
        self.user_reply = "OK"
        self.rejected_serials: set[str] = set()
        self.denied_serials: set[str] = set()     # HTTP 200 "DENIED" (ApexityEdu style)
        self.command_acks: list[dict] = []    # {"sn", "body", "fields"}
        self.poll_status = 200
        self.poll_body = "OK"
        self.upload_reply: str | None = None
        self.upload_status = 200
        self.upload_location: str | None = None
        self.mode = "ok"          # ok | ok_count | error | http500 | partial | silent
        self.duplicates_accept = True

        server = self

        class Handler(_Quiet):
            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path.endswith("/cdata"):
                    sn = parse_qs(parsed.query).get("SN", [""])[0]
                    server.handshakes.append(sn)
                    if sn in server.rejected_serials:
                        return self._reply(403, "serial rejected")
                    if sn in server.denied_serials:
                        return self._reply(200, "DENIED")
                    if server.mode == "http500":
                        return self._reply(500, "boom")
                    # Real iClock handshake responses are a GET OPTION block;
                    # they do not also include an "OK" token.
                    return self._reply(
                        200,
                        "GET OPTION FROM: APEXITY-FAKE\nStamp=9999\nOpStamp=0\nPhotoStamp=0",
                    )
                if parsed.path.endswith("/getrequest"):
                    sn = parse_qs(parsed.query).get("SN", [""])[0]
                    server.polls.append(sn)
                    return self._reply(server.poll_status, server.poll_body)
                return self._reply(404, "nope")

            def do_POST(self):
                parsed = urlparse(self.path)
                if parsed.path.endswith("/devicecmd"):
                    sn = parse_qs(parsed.query).get("SN", [""])[0]
                    length = int(self.headers.get("Content-Length", "0"))
                    body = self.rfile.read(length).decode("utf-8", errors="replace")
                    fields = dict(
                        part.split("=", 1) for part in body.replace("\n", "&").split("&")
                        if "=" in part
                    )
                    server.command_acks.append({"sn": sn, "body": body, "fields": fields})
                    return self._reply(200, "OK")
                if not parsed.path.endswith("/cdata"):
                    return self._reply(404, "nope")
                if server.upload_location:
                    self.send_response(302)
                    self.send_header("Location", server.upload_location)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if server.upload_reply is not None:
                    return self._reply(server.upload_status, server.upload_reply)
                if server.mode == "http500":
                    return self._reply(500, "boom")
                if server.mode == "error":
                    return self._reply(200, "ERROR: table unknown")
                if server.mode == "silent":
                    self.close_connection = True
                    return
                query = parse_qs(parsed.query)
                sn = query.get("SN", [""])[0]
                table = query.get("table", ["ATTLOG"])[0].upper()
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8", errors="replace")
                lines = [ln for ln in body.splitlines() if ln.strip()]

                if table == "USER":
                    users = {}
                    for ln in lines:
                        fields = dict(
                            part.split("=", 1)
                            for part in ln.replace("USER ", "").split("\t")
                            if "=" in part
                        )
                        pin = fields.get("PIN", "").strip()
                        if pin:
                            users[pin] = fields.get("Name", "")
                    server.user_uploads.append({"sn": sn, "users": users})
                    return self._reply(200, server.user_reply)
                server.attlog_posts.append({"sn": sn, "lines": lines})
                new_count = 0
                limit = 1 if server.mode == "partial" else len(lines)
                for ln in lines[:limit]:
                    parts = ln.replace(",", "\t").split("\t")
                    pin, ts = parts[0], parts[1] if len(parts) > 1 else ""
                    status = parts[2] if len(parts) > 2 else "0"
                    verify = parts[3] if len(parts) > 3 else "0"
                    key = (sn, pin, ts, status)
                    if key not in server.seen or server.mode == "force-dupes":
                        server.seen.add(key)
                        server.accepted.append(
                            {"sn": sn, "pin": pin, "ts": ts, "status": status, "verify": verify}
                        )
                        new_count += 1
                if server.mode == "ok_count" or server.mode == "partial":
                    return self._reply(200, f"OK: {new_count if server.mode != 'partial' else 1}")
                return self._reply(200, "OK")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeRestServer:
    """Implements the documented REST contract. Token-guarded."""

    TOKEN = "test-device-token-123"

    def __init__(self, students: list[dict] | None = None):
        self.students = students if students is not None else [
            {"id": "S1001", "name": "Afnan Rahman", "admission_no": "ADM-001",
             "roll_no": "12", "class": "Six", "section": "A", "active": True},
            {"id": "S1002", "name": "Bithi Akter", "admission_no": "ADM-002",
             "roll_no": "7", "class": "Six", "section": "B", "active": True},
            {"id": "S1003", "name": "Chinmoy Das", "admission_no": "ADM-003",
             "roll_no": "3", "class": "Seven", "section": "A", "active": False},
        ]
        # students + teachers + staff: everybody who punches a terminal
        self.people = [
            *self.students,
            {"id": "T2001", "name": "Sir Alam", "person_type": "teacher",
             "designation": "Headmaster", "employee_no": "EMP-01", "active": True},
            {"id": "T2002", "name": "Madam Nusrat", "person_type": "teacher",
             "designation": "Assistant teacher", "employee_no": "EMP-02", "active": True},
            {"id": "X3001", "name": "Bacchu Mia", "person_type": "staff",
             "designation": "Guard", "employee_no": "EMP-77", "active": True},
        ]
        self.device_user_uploads: list[dict] = []
        self.people_enabled = True       # False → emulate an EMS without /people
        self.people_uploads: list[dict] = []   # POST /people bodies
        self.people_write_enabled = True      # False → EMS without a people write
        self.seen: dict[str, int] = {}     # idempotency_key → remote_id
        self.registered_devices: list[dict] = []
        self.push_calls = 0
        self.mode = "ok"                   # ok | http500 | unauthorized | badjson
        self.page_size = 2                 # forces multi-page roster tests
        self._next_remote_id = 1000

        server = self

        class Handler(_Quiet):
            def _auth_ok(self) -> bool:
                return self.headers.get("Authorization") == f"Bearer {server.TOKEN}"

            def _json_body(self) -> dict:
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    return json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    return {}

            def _out(self, status: int, payload):
                if server.mode == "http500":
                    return self._reply(500, json.dumps({"error": "boom"}), "application/json")
                if server.mode == "badjson":
                    return self._reply(200, "<html>not json</html>", "application/json")
                if server.mode == "unauthorized":
                    return self._reply(401, json.dumps({"error": "token rejected"}), "application/json")
                return self._reply(status, json.dumps(payload), "application/json")

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/api/v1/bridge/health":
                    if not self._auth_ok() and server.mode != "unauthorized":
                        return self._reply(401, json.dumps({"error": "auth"}), "application/json")
                    return self._out(200, {"ok": True, "server_time": "2026-10-15T10:00:00+00:00"})
                if parsed.path == "/api/v1/bridge/students":
                    if not self._auth_ok() and server.mode != "unauthorized":
                        return self._reply(401, json.dumps({"error": "auth"}), "application/json")
                    q = parse_qs(parsed.query)
                    cursor = int(q.get("cursor", ["0"])[0])
                    page = server.students[cursor:cursor + server.page_size]
                    nxt = cursor + server.page_size
                    return self._out(200, {
                        "students": page,
                        "next_cursor": str(nxt) if nxt < len(server.students) else None,
                    })
                if parsed.path == "/api/v1/bridge/people":
                    if not server.people_enabled:
                        return self._reply(404, json.dumps({"error": "nope"}),
                                           "application/json")
                    if not self._auth_ok() and server.mode != "unauthorized":
                        return self._reply(401, json.dumps({"error": "auth"}), "application/json")
                    q = parse_qs(parsed.query)
                    types = [t.strip() for t in (q.get("types", [""])[0] or "").split(",") if t.strip()]
                    rows = server.people
                    if types:
                        rows = [p for p in rows if (p.get("person_type") or "student") in types]
                    cursor = int(q.get("cursor", ["0"])[0])
                    page = rows[cursor:cursor + server.page_size]
                    nxt = cursor + server.page_size
                    return self._out(200, {
                        "people": page,
                        "next_cursor": str(nxt) if nxt < len(rows) else None,
                    })
                return self._reply(404, json.dumps({"error": "nope"}), "application/json")

            def do_POST(self):
                parsed = urlparse(self.path)
                if parsed.path == "/api/v1/bridge/devices/register":
                    if not self._auth_ok() and server.mode != "unauthorized":
                        return self._reply(401, json.dumps({"error": "auth"}), "application/json")
                    body = self._json_body()
                    server.registered_devices.append(body)
                    return self._out(200, {"ok": True,
                                           "device_id": f"dev-{body.get('serial', '?')}"})
                if parsed.path == "/api/v1/bridge/devices/users":
                    if not self._auth_ok() and server.mode != "unauthorized":
                        return self._reply(401, json.dumps({"error": "auth"}), "application/json")
                    body = self._json_body()
                    server.device_user_uploads.append(body)
                    return self._out(200, {"ok": True,
                                           "accepted": len(body.get("users", [])),
                                           "detail": "stored"})
                if parsed.path == "/api/v1/bridge/people":
                    if not server.people_write_enabled:
                        return self._reply(404, json.dumps({"error": "nope"}),
                                           "application/json")
                    if not self._auth_ok() and server.mode != "unauthorized":
                        return self._reply(401, json.dumps({"error": "auth"}), "application/json")
                    body = self._json_body()
                    people = body.get("people") or []
                    server.people_uploads.append(body)
                    known = {p["id"] for p in server.people}
                    created = sum(1 for p in people if p.get("id") not in known)
                    return self._out(200, {
                        "ok": True,
                        "accepted": len(people),
                        "created": created,
                        "updated": len(people) - created,
                        "detail": "stored",
                    })
                if parsed.path == "/api/v1/bridge/attendance/batch":
                    if not self._auth_ok() and server.mode != "unauthorized":
                        return self._reply(401, json.dumps({"error": "auth"}), "application/json")
                    body = self._json_body()
                    server.push_calls += 1
                    results = []
                    for ev in body.get("events", []):
                        key = ev.get("idempotency_key", "")
                        if not ev.get("timestamp") or not ev.get("device_user_id"):
                            results.append({"idempotency_key": key, "status": "rejected",
                                            "reason": "missing timestamp/device_user_id"})
                        elif key in server.seen:
                            results.append({"idempotency_key": key, "status": "duplicate",
                                            "remote_id": server.seen[key]})
                        else:
                            server._next_remote_id += 1
                            server.seen[key] = server._next_remote_id
                            results.append({"idempotency_key": key, "status": "accepted",
                                            "remote_id": server._next_remote_id})
                    return self._out(200, {"results": results})
                return self._reply(404, json.dumps({"error": "nope"}), "application/json")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
