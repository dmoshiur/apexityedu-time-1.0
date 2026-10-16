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
from urllib.parse import quote

import requests

from .base import (
    EmsAuthError,
    EmsProtocolError,
    EmsUnavailableError,
    HealthResult,
    RegisterResult,
    RosterPage,
    StudentInfo,
    UploadOutcome,
)

log = logging.getLogger("apexity_bridge.ems.iclock")

PUSH_VER = "2.4.2"


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
                 auth_token: str | None = None, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.path = "/" + path.strip("/")
        self.timeout = timeout
        self._auth_token = auth_token
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
        """iClock is device-oriented: roster pull is usually NOT available.

        The bridge surfaces this honestly as an unsupported capability; the
        SyncService logs a precise error pointing at docs/EMS_API_CONTRACT.md
        (the EMS must implement the documented roster endpoint, or the REST
        protocol must be used). We never invent students.
        """
        raise EmsProtocolError(
            "the iClock protocol does not provide a student roster download. "
            "The bridge cannot display EMS students until the EMS implements "
            "GET /api/v1/bridge/students (see docs/EMS_API_CONTRACT.md) or the "
            "EMS roster/OPERLOG endpoint is configured."
        )

    # --------------------------------------------------------------- upload --
    @staticmethod
    def format_attlog_line(event: dict) -> str:
        ts = event["event_timestamp"].replace("T", " ")[:19]
        status = event.get("status") or 0
        verify = event.get("verify_mode") or event.get("punch") or 0
        return "\t".join(
            [str(event["device_user_id"]), ts, str(status), str(verify), "0", "0", "0"]
        )

    def push_attendance(self, events: list[dict]) -> UploadOutcome:
        if not events:
            return UploadOutcome()
        # iClock pushes are per-device-serial — the worker already groups.
        serial = events[0]["device_serial"]
        for ev in events:
            if ev["device_serial"] != serial:
                raise EmsProtocolError("iClock push requires same-serial batching")

        body = "\n".join(self.format_attlog_line(ev) for ev in events) + "\n"
        url = self._endpoint("cdata", serial, table="ATTLOG",
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

    # --------------------------------------------------------------- polling --
    def poll_commands(self, serial: str) -> list[str]:
        """GET getrequest — informational; unknown commands are logged only.

        The bridge deliberately does NOT execute device administration
        commands received this way without an explicit admin opt-in.
        """
        resp = self._request("GET", self._endpoint("getrequest", serial))
        text = self._expect_ok_text(resp)
        if text.upper() == "OK":
            return []
        commands = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not commands or any(not cmd.startswith("C:") for cmd in commands):
            raise EmsProtocolError(
                f"EMS presence poll for SN {serial}: expected OK or C: command; "
                f"reply={self._safe_detail(text)[:200]!r}"
            )
        for cmd in commands:
            log.warning("EMS sent iClock command (not executed, logged only): %s",
                        self._safe_detail(cmd)[:200])
        return commands
