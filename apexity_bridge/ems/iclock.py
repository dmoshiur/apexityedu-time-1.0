"""ZKTeco ADMS/iClock *push* client.

Many EMS products keep ``/iclock`` endpoints so real terminals can push data
(ADMS mode). The K40 models in the field often have ADMS unavailable, so the
bridge pulls records over the ZK protocol (port 4370) and then *re-publishes*
them to the EMS using the exact iClock wire format the server already
understands — the bridge plays the role of "one virtual device" per real
terminal (its serial number is used).

Protocol (documented in docs/EMS_API_CONTRACT.md):
* Handshake:  GET  {base}{path}/cdata?SN=<serial>&options=all&pushver=2.4.2&language=69
* Upload:     POST {base}{path}/cdata?SN=<serial>&table=ATTLOG&Stamp=<unix>
              body: TAB-separated lines  PIN<TAB>YYYY-MM-DD HH:MM:SS<TAB>status<TAB>verify<TAB>workcode<TAB>r1<TAB>r2
              server answers "OK" or "OK: <n>"
* Presence:   GET  {base}{path}/getrequest?SN=<serial> → "OK" or a C:... command

Security notes:
* Only ever sent over HTTPS in production (factory enforces).
* Server-side acceptance is per-SN; the EMS operator MUST whitelist each
  terminal's serial. An optional shared-secret token can be appended
  (``?AuthToken=`` / ``X-Auth-Token`` header) if the EMS implements the
  documented extension — stored in the OS credential store, never in files.
* iClock has no per-record ids: dedup on the server is SN+PIN+time(+status).
  The bridge therefore never deletes its own copy and never deletes device
  data — a re-push after a crash is harmless.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import quote

import requests

from .base import (
    EmsAuthError,
    EmsError,
    EmsProtocolError,
    EmsUnavailableError,
    HealthResult,
    PeoplePushOutcome,
    RegisterResult,
    RosterPage,
    StudentInfo,
    UploadOutcome,
    UserSyncOutcome,
)

log = logging.getLogger("apexity_bridge.ems.iclock")

PUSH_VER = "2.4.2"

# iClock/ADMS "table" names used by the bridge.
TABLE_ATTLOG = "ATTLOG"
TABLE_USER = "USER"
TABLE_OPERLOG = "OPERLOG"

# Commands an ADMS server may hand to a terminal through getrequest. Only
# read-only DATA QUERY commands may ever be answered; everything else is
# logged and refused — the bridge is a collector, not a remote control.
READ_ONLY_COMMANDS = (
    "DATA QUERY USERINFO",
    "DATA QUERY ATTLOG",
    "DATA QUERY OPERLOG",
    "DATA QUERY BIODATA",     # answered with an empty body: no templates, ever
    "DATA QUERY FINGERTMP",
    "CHECK",
    "INFO",
    "OPTIONS",
)
REFUSED_COMMANDS = (
    "REBOOT", "RESTART", "CLEAR", "DELETE", "SET", "ENROLL", "UPDATE",
    "INIT", "SHELL", "EXEC", "FORMAT", "LOCK", "UNLOCK",
)


@dataclass(frozen=True)
class IclockCommand:
    """One parsed ``C:<id>:<COMMAND>`` instruction from ``getrequest``."""

    raw: str
    command_id: str | None
    body: str          # command text without the C:<id>: envelope
    verb: str          # upper-cased, recognised verb where possible

    @property
    def signature(self) -> str:
        """Stable grouping key for repeated server commands.

        ``SET TIME 2026-10-17 09:00:00`` and the same command one heartbeat
        later must collapse to one audit entry — otherwise the log becomes a
        wall of identical warnings.  ``DATA QUERY`` keeps its subject
        (USERINFO/ATTLOG/BIODATA) so different queries are not merged.
        """
        parts = self.verb.split()
        if parts[:2] == ["DATA", "QUERY"]:
            return " ".join(parts[:3])
        return " ".join(parts[:2]) or self.verb


def parse_command(command: str) -> IclockCommand:
    """Parse one raw command, tolerating both iClock envelope shapes.

    Real servers send ``C:12:DATA QUERY USERINFO``; some omit the numeric id
    (``C:CHECK``) or already unwrap the body (``CHECK``).  All three are
    accepted so a syntactic variant is answered instead of being mistaken for
    a destructive command.
    """
    raw = (command or "").strip()
    body = raw
    command_id: str | None = None
    if raw.upper().startswith("C:"):
        rest = raw[2:].strip()
        if ":" in rest:
            candidate, remainder = rest.split(":", 1)
            command_id = candidate.strip() or None
            body = remainder.strip()
        else:
            body = rest
    upper = body.upper()
    verb = upper
    for known in ("DATA QUERY USERINFO", "DATA QUERY ATTLOG", "DATA QUERY OPERLOG",
                  "DATA QUERY BIODATA", "DATA QUERY FINGERTMP"):
        if upper.startswith(known):
            verb = known
            break
    return IclockCommand(raw=raw, command_id=command_id, body=body, verb=verb)


def parse_commands(text: str) -> list[str]:
    """Extract the command bodies from a getrequest reply.

    Servers that answer a plain ``OK`` yield an empty list.
    """
    commands: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.upper() == "OK":
            continue
        commands.append(parse_command(line).body)
    return commands


def command_verb(command: str) -> str:
    """Upper-cased verb of an iClock command, e.g. 'DATA QUERY USERINFO'."""
    return parse_command(command).verb


def command_is_read_only(command: str) -> bool:
    verb = command_verb(command)
    if verb in READ_ONLY_COMMANDS:
        return True
    return not any(verb.startswith(bad) for bad in REFUSED_COMMANDS) and verb.startswith(
        ("DATA QUERY", "CHECK", "INFO")
    )


def is_user_success(text: str | None) -> bool:
    """Acceptance check for a ``table=USER`` upload."""
    body = (text or "").lstrip("\ufeff").strip()
    return bool(re.fullmatch(r"OK(?:\s*:\s*[0-9]+)?", body, re.IGNORECASE))


def is_success_handshake(body: str | None) -> bool:
    """Return whether an iClock ``cdata`` GET reply is a valid handshake.

    The standard ZKTeco response is a ``GET OPTION FROM: <serial>`` block,
    not ``OK``. Some compatible servers return a plain ``OK`` instead. Keep
    this check shared by health, registration, and the diagnostic probe so a
    valid handshake is not reported as successful by one path and rejected
    by another.
    """
    text = (body or "").lstrip("\ufeff").strip().upper()
    return (
        text.startswith("GET OPTION")
        or text == "OK"
        or text.startswith("OK:")
    )


class IclockEmsClient:
    protocol_name = "iclock"
    ATTLOG_FIELDS = 7  # PIN,time,status,verify,workcode,r1,r2

    def __init__(self, base_url: str, *, path: str = "/iclock", timeout: int = 15,
                 auth_token: str | None = None, session: requests.Session | None = None,
                 roster_client=None):
        self.base_url = base_url.rstrip("/")
        self.path = "/" + path.strip("/")
        self.timeout = timeout
        self._auth_token = auth_token
        # Optional REST side-channel used *only* to download the EMS people
        # directory. iClock has no roster download, and a school that cannot
        # attribute a PIN to a person cannot use attendance at all, so the
        # bridge supports downloading the roster over REST while still pushing
        # punches over iClock (see docs/EMS_API_CONTRACT.md § "Split mode").
        self._roster_client = roster_client
        self._http = session or requests.Session()
        self._http.headers.update({"User-Agent": "ApexityEduBridge/1.0"})
        if auth_token:
            self._http.headers.update({"X-Auth-Token": auth_token})

    # ------------------------------------------------------------------ urls --
    def _endpoint(self, suffix: str, serial: str, **params) -> str:
        qs = {"SN": serial, **{k: str(v) for k, v in params.items()}}
        if self._auth_token:
            qs.setdefault("AuthToken", self._auth_token)
        query = "&".join(f"{k}={quote(v, safe='')}" for k, v in qs.items())
        return f"{self.base_url}{self.path}/{suffix}?{query}"

    # ------------------------------------------------------------- transport --
    def _safe_detail(self, text: str) -> str:
        # URLs in requests exceptions (and server replies) can echo AuthToken.
        if self._auth_token:
            for secret in (quote(self._auth_token, safe=""), self._auth_token):
                text = text.replace(secret, "[REDACTED]")
        return text

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        try:
            # A redirect to a login page must never become a successful upload.
            resp = self._http.request(method, url, timeout=self.timeout,
                                      allow_redirects=False, **kwargs)
        except requests.Timeout as exc:
            raise EmsUnavailableError(f"EMS timeout: {self._safe_detail(str(exc))}") from exc
        except requests.ConnectionError as exc:
            raise EmsUnavailableError(f"EMS unreachable: {self._safe_detail(str(exc))}") from exc
        except requests.RequestException as exc:
            raise EmsUnavailableError(f"EMS request failed: {self._safe_detail(str(exc))}") from exc
        if resp.status_code in (401, 403):
            raise EmsAuthError(
                f"EMS rejected device credentials (HTTP {resp.status_code}) — "
                f"check that the serial is whitelisted / token is valid"
            )
        if resp.status_code >= 500 or resp.status_code == 429:
            raise EmsUnavailableError(f"EMS HTTP {resp.status_code}")
        return resp

    def _expect_ok_text(self, resp: requests.Response) -> str:
        # iClock replies are ASCII/UTF-8; text/plain without a charset would
        # otherwise default to Latin-1 and corrupt an optional UTF-8 BOM.
        resp.encoding = "utf-8"
        text = (resp.text or "").lstrip("\ufeff").strip()
        if resp.status_code != 200:
            raise EmsProtocolError(
                f"EMS HTTP {resp.status_code}: {self._safe_detail(text)[:200]}"
            )
        return text

    # -------------------------------------------------------------- contract --
    def health_check(self) -> HealthResult:
        url = self._endpoint("cdata", "APEXITY-HEALTHCHECK", options="all",
                             pushver=PUSH_VER, language="69")
        resp = self._request("GET", url)
        text = self._expect_ok_text(resp)
        ok = is_success_handshake(text)
        return HealthResult(ok=ok, protocol=self.protocol_name,
                            detail=f"handshake response: {self._safe_detail(text)[:120]!r}")

    def handshake(self, serial: str) -> str:
        resp = self._request(
            "GET",
            self._endpoint("cdata", serial, options="all", pushver=PUSH_VER,
                           language="69", PushOptionsFlag="1"),
        )
        return self._expect_ok_text(resp)

    def register_device(self, *, serial: str, model: str | None,
                        firmware: str | None, label: str | None) -> RegisterResult:
        """iClock has no device registry; 'registration' = whitelisted handshake.

        The serial must be pre-whitelisted in the EMS admin panel (documented).
        A successful handshake with the device SN proves the EMS accepts us.
        """
        text = self.handshake(serial)
        ok = is_success_handshake(text)
        return RegisterResult(
            ok=ok,
            remote_device_id=serial if ok else None,
            detail=("handshake OK — serial accepted by EMS"
                    if ok else f"unexpected handshake reply: {self._safe_detail(text)[:120]!r}"),
        )

    # ---------------------------------------------------------------- roster --
    def fetch_students(self, cursor: str | None = None) -> RosterPage:
        return self.fetch_people(cursor)

    def fetch_people(self, cursor: str | None = None) -> RosterPage:
        """Download the EMS directory (students + teachers + staff).

        iClock is device-oriented and has no roster download of its own, so
        this delegates to the optional REST side-channel (``/api/v1/bridge/
        people``, falling back to ``/students``). When no side-channel is
        configured the bridge reports the capability as missing instead of
        inventing people.
        """
        if self._roster_client is None:
            raise EmsProtocolError(
                "the iClock protocol cannot download a people roster, and no "
                "REST roster endpoint is enabled (EMS_ROSTER_MODE=off). The EMS "
                "must expose GET /api/v1/bridge/people — or the legacy "
                "GET /api/v1/bridge/students — so the bridge can show students, "
                "teachers and staff and bind them to device users "
                "(docs/EMS_API_CONTRACT.md)."
            )
        try:
            return self._roster_client.fetch_people(cursor)
        except EmsProtocolError as exc:
            raise EmsProtocolError(
                f"the EMS people roster could not be downloaded: {exc}. iClock "
                "has no roster download of its own, so the bridge needs "
                "GET /api/v1/bridge/people (students + teachers + staff) — see "
                "docs/EMS_API_CONTRACT.md, or set EMS_ROSTER_MODE=off to stop "
                "checking."
            ) from exc

    # --------------------------------------------------------------- upload --
    @staticmethod
    def format_attlog_line(event: dict) -> str:
        # The wire format is TAB-separated; a tab/newline in a PIN or timestamp
        # would split one record into two and make the server's parser (and its
        # Celery ingestion task) fail. Keep the line structurally valid.
        pin = str(event.get("device_user_id") or "").strip()
        pin = pin.replace("\t", " ").replace("\r", " ").replace("\n", " ")
        ts = str(event.get("event_timestamp") or "").replace("T", " ")[:19]
        ts = ts.replace("\t", " ").replace("\r", " ").replace("\n", " ")
        status = event.get("status") or 0
        verify = event.get("verify_mode") or event.get("punch") or 0
        return "\t".join([pin, ts, str(status), str(verify), "0", "0", "0"])

    def push_attendance(self, events: list[dict], *, serial: str | None = None) -> UploadOutcome:
        if not events:
            return UploadOutcome()
        # iClock pushes are per-device-serial — the worker groups by the serial
        # the EMS knows (devices.ems_serial when the operator set an override).
        if serial:
            serial = serial.strip()
        else:
            serial = events[0]["device_serial"]
            for ev in events:
                if ev["device_serial"] != serial:
                    raise EmsProtocolError("iClock push requires same-serial batching")

        body = "\n".join(self.format_attlog_line(ev) for ev in events) + "\n"
        url = self._endpoint("cdata", serial, table=TABLE_ATTLOG,
                             Stamp=int(time.time()))
        resp = self._request("POST", url, data=body.encode("utf-8"),
                             headers={"Content-Type": "text/plain"})
        text = self._expect_ok_text(resp)
        # An HTTP 200 alone is not acceptance: login HTML, JSON, empty
        # replies, handshake blocks and malformed counts must stay retryable.
        match = re.fullmatch(r"OK(?:\s*:\s*([0-9]+))?", text, re.IGNORECASE)
        if match is None:
            raise EmsProtocolError(
                f"EMS refused ATTLOG for SN {serial}: expected OK or OK: <count>; "
                f"HTTP {resp.status_code}, reply={self._safe_detail(text)[:200]!r}"
            )
        digits = match.group(1)
        if digits is None:
            count = len(events)
        else:
            digits = digits.lstrip("0") or "0"
            if len(digits) > len(str(len(events))):
                raise EmsProtocolError(f"EMS ATTLOG count exceeds batch for SN {serial}")
            count = int(digits)
        if count > len(events):
            raise EmsProtocolError(
                f"EMS ATTLOG count {count} exceeds batch of {len(events)} for SN {serial}"
            )
        accepted = [ev["id"] for ev in events[:count]]
        partial_index = count if count < len(events) else None
        if partial_index is not None:
            log.warning("EMS accepted only %d/%d records for SN %s",
                        count, len(events), serial)
        return UploadOutcome(
            accepted_ids=tuple(accepted),
            partial_index=partial_index,
        )

    # ------------------------------------------------------------ user table --
    @staticmethod
    def format_user_line(user: dict) -> str:
        """One ``table=USER`` line, in the format ADMS servers parse.

        ``USER PIN=<id>\\tName=<name>\\tPri=<privilege>\\tPasswd=\\tCard=<card>\\tGrp=1\\tTZ=…``

        The device password is deliberately never sent: the bridge only needs
        the EMS to learn which PIN belongs to which human.
        """
        pin = str(user.get("user_id") or "").strip()
        pin = pin.replace("\t", " ").replace("\r", " ").replace("\n", " ")
        if not pin:
            return ""
        name = (user.get("name") or f"User {pin}").strip()
        name = name.replace("\t", " ").replace("\r", " ").replace("\n", " ")[:60]
        privilege = user.get("privilege")
        try:
            privilege = int(privilege) if privilege is not None else 0
        except (TypeError, ValueError):
            privilege = 0
        card = str(user.get("card") or "").replace("\t", " ").replace("\n", " ")
        return (
            f"USER PIN={pin}\tName={name}\tPri={privilege}\t"
            f"Passwd=\tCard={card}\tGrp=1\tTZ=0000000000000000"
        )

    def push_users(self, serial: str, users: list[dict]) -> UserSyncOutcome:
        """Publish a terminal's enrolled users so the EMS can map PIN → person.

        Without this, an iClock server receives attendance lines full of bare
        PINs and has no way to attribute them to a student, teacher or staff
        member. The upload is idempotent (the table replaces itself) and
        contains no biometric templates or passwords.
        """
        if not users:
            return UserSyncOutcome(accepted=0, detail="no users to push")
        lines = [line for line in (self.format_user_line(u) for u in users) if line]
        if not lines:
            return UserSyncOutcome(
                fatal="the device user table has no usable PIN; nothing was uploaded")
        body = "\n".join(lines) + "\n"
        url = self._endpoint("cdata", serial, table=TABLE_USER, Stamp=int(time.time()))
        resp = self._request("POST", url, data=body.encode("utf-8"),
                             headers={"Content-Type": "text/plain"})
        text = self._expect_ok_text(resp)
        if not is_user_success(text):
            return UserSyncOutcome(
                fatal=(
                    f"EMS refused the USER table for SN {serial}: expected OK or "
                    f"OK: <count>; HTTP {resp.status_code}, "
                    f"reply={self._safe_detail(text)[:200]!r}"
                )
            )
        match = re.search(r"([0-9]+)", text)
        accepted = int(match.group(1)) if match else len(lines)
        return UserSyncOutcome(accepted=min(accepted, len(lines)),
                               detail=f"USER table accepted: {self._safe_detail(text)[:80]!r}")

    # ------------------------------------------------------------ people table --
    def push_people(self, people: list[dict]) -> PeoplePushOutcome:
        """Write people back to the EMS.

        iClock/ADMS has **no** people-write endpoint — it only carries
        device tables and punches. In split mode (iClock for punches, REST
        for the directory) the write goes through the REST side-channel; in
        pure iClock mode the capability is honestly reported as missing.
        """
        if not people:
            return PeoplePushOutcome(accepted=0, detail="nothing to push")
        delegate = getattr(self._roster_client, "push_people", None)
        if delegate is not None:
            return delegate(people)
        raise EmsProtocolError(
            "the iClock protocol cannot write people to the EMS — it has no "
            "people endpoint. Set EMS_PROTOCOL=rest (or keep the REST roster "
            "side-channel enabled with EMS_ROSTER_MODE=auto) so the bridge can "
            "use POST /api/v1/bridge/people "
            "(docs/EMS_API_CONTRACT.md § Option E)."
        )

    # ------------------------------------------------------------- diagnostics --
    def probe_attlog_path(self, serial: str) -> dict:
        """POST an EMPTY ATTLOG body and report the server's exact answer.

        Sending no records proves whether the server's ingestion route exists
        and accepts this serial, without writing a single fake punch into the
        school's attendance register. This is how "it says everything is OK but
        nothing arrives" gets disproved or confirmed in one step.
        """
        url = self._endpoint("cdata", serial, table=TABLE_ATTLOG, Stamp=int(time.time()))
        try:
            resp = self._request("POST", url, data=b"",
                                 headers={"Content-Type": "text/plain"})
        except EmsError as exc:
            return {"serial": serial, "ok": False, "error": str(exc)[:300],
                    "reply": None, "status": None}
        text = self._expect_ok_text(resp)
        ok = bool(re.fullmatch(r"OK(?:\s*:\s*[0-9]+)?", text, re.IGNORECASE))
        return {
            "serial": serial, "ok": ok, "status": resp.status_code,
            "reply": self._safe_detail(text)[:300],
            "url": self._safe_detail(url),
            "note": ("empty ATTLOG accepted — this serial can receive punches"
                     if ok else
                     "server did not answer OK to an empty ATTLOG upload; check that "
                     "this serial is registered in the EMS and that POST (not GET) "
                     "is accepted on the cdata route"),
        }

    # --------------------------------------------------------------- polling --
    def poll_commands(self, serial: str) -> list[str]:
        """GET getrequest — return queued ``C:<id>:<COMMAND>`` lines.

        The bridge logs the raw commands at debug level here; the sync worker
        decides whether each one is safe to answer, and audits a refusal only
        once per command type (servers re-queue unacknowledged commands, so a
        warning here would repeat every 30 seconds forever).
        """
        resp = self._request("GET", self._endpoint("getrequest", serial))
        text = self._expect_ok_text(resp)
        if text.upper() == "OK":
            return []
        if text.upper() == "DENIED":
            raise EmsProtocolError(
                f"EMS denied presence for SN {serial} — register/authorize this "
                "serial in ApexityEdu → Biometric Devices, or correct the device's "
                "EMS serial in the bridge"
            )
        commands = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not commands or any(not cmd.upper().startswith("C:") for cmd in commands):
            raise EmsProtocolError(
                f"EMS presence poll for SN {serial}: expected OK or C: command; "
                f"reply={self._safe_detail(text)[:200]!r}"
            )
        for cmd in commands:
            log.debug("EMS sent iClock command: %s", self._safe_detail(cmd)[:200])
        return commands

    def acknowledge_command(self, serial: str, *, command_id: str,
                            command: str, return_code: int = 0) -> None:
        """POST /iclock/devicecmd — the ADMS command result ack.

        A real terminal reports ``ID=<id>&Return=<code>&CMD=<command>`` after
        executing a command.  Without this ack an ADMS server keeps the
        command queued and re-sends it on every ``getrequest`` poll, which is
        exactly how a single unsupported command turns into a log line every
        30 seconds (and, on some backends, a permanently retrying queue task).
        ``return_code=0`` means success; ``-1`` means "not supported/not
        executed".  The bridge never acknowledges a destructive command as
        successful.
        """
        command_id = str(command_id or "").strip()
        if not command_id:
            raise EmsProtocolError(
                f"EMS command for SN {serial} has no C:<id> envelope, cannot ack"
            )
        body = f"ID={command_id}&Return={int(return_code)}&CMD={command}".encode("utf-8")
        resp = self._request(
            "POST", self._endpoint("devicecmd", serial),
            data=body, headers={"Content-Type": "text/plain"},
        )
        self._expect_ok_text(resp)
