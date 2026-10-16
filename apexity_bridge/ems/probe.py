"""Non-destructive EMS capability probe (Stage A verification tool).

Answers, against the LIVE server: which protocol does apexityedu.com actually
speak for attendance ingestion? It performs only read-only handshakes — it
never uploads data and never uses credentials.

Run:  python -m apexity_bridge probe-ems
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

from .iclock import is_success_handshake

log = logging.getLogger("apexity_bridge.ems.probe")


@dataclass
class ProbeReport:
    base_url: str
    reachable: bool = False
    server_header: str | None = None
    iclock_verdict: str = "unknown"      # yes | no | maybe | unreachable
    iclock_evidence: str = ""
    rest_verdict: str = "unknown"
    rest_evidence: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def recommended_protocol(self) -> str | None:
        if self.iclock_verdict == "yes":
            return "iclock"
        if self.rest_verdict == "yes":
            return "rest"
        return None

    def to_dict(self) -> dict:
        return {
            "base_url": self.base_url,
            "reachable": self.reachable,
            "server_header": self.server_header,
            "iclock": {"verdict": self.iclock_verdict, "evidence": self.iclock_evidence},
            "rest": {"verdict": self.rest_verdict, "evidence": self.rest_evidence},
            "errors": self.errors,
            "recommended_protocol": self.recommended_protocol,
        }

    def human(self) -> str:
        lines = [
            f"EMS probe report for {self.base_url}",
            f"  reachable          : {self.reachable}"
            + (f" (server: {self.server_header})" if self.server_header else ""),
            f"  iClock endpoint    : {self.iclock_verdict} — {self.iclock_evidence}",
            f"  REST bridge API    : {self.rest_verdict} — {self.rest_evidence}",
        ]
        if self.recommended_protocol:
            lines.append(f"  ⇒ recommended EMS_PROTOCOL={self.recommended_protocol}")
        else:
            lines.append(
                "  ⇒ no compatible attendance endpoint detected. The EMS needs one of: "
                "(1) a ZKTeco iClock/ADMS handler at the configured path, or "
                "(2) the REST contract in docs/EMS_API_CONTRACT.md. Uploading anything "
                "before this is resolved would be guesswork — the bridge stays in "
                "offline-queue mode."
            )
        for err in self.errors:
            lines.append(f"  ! {err}")
        return "\n".join(lines)


def probe_ems(base_url: str, *, iclock_path: str = "/iclock", timeout: int = 15,
              session: requests.Session | None = None) -> ProbeReport:
    report = ProbeReport(base_url=base_url.rstrip("/"))
    http = session or requests.Session()
    http.headers.update({"User-Agent": "ApexityEduBridge-Probe/1.0"})
    base = base_url.rstrip("/")

    # 1) base reachability -----------------------------------------------------
    try:
        resp = http.get(base + "/", timeout=timeout)
        report.reachable = True
        report.server_header = resp.headers.get("Server")
    except requests.RequestException as exc:
        report.errors.append(f"cannot reach {base}: {exc}")
        report.iclock_verdict = report.rest_verdict = "unreachable"
        return report

    # 2) iClock handshake handshake (read-only) --------------------------------
    iclock_url = f"{base}/{'/'.join([iclock_path.strip('/'), 'cdata'])}?SN=APEXITY-PROBE&options=all"
    try:
        resp = http.get(iclock_url, timeout=timeout)
        text = (resp.text or "").strip()
        if resp.status_code == 200 and is_success_handshake(text):
            report.iclock_verdict = "yes"
            report.iclock_evidence = (
                f"GET cdata answered with valid ZKTeco handshake "
                f"(HTTP 200, body {text[:80]!r})"
            )
        elif resp.status_code == 404:
            report.iclock_verdict = "no"
            report.iclock_evidence = f"{iclock_url} → HTTP 404"
        else:
            report.iclock_verdict = "maybe"
            report.iclock_evidence = f"HTTP {resp.status_code}, body {text[:80]!r}"
    except requests.RequestException as exc:
        report.iclock_verdict = "unreachable"
        report.iclock_evidence = str(exc)

    # 3) custom REST bridge API --------------------------------------------------
    rest_url = f"{base}/api/v1/bridge/health"
    try:
        resp = http.get(rest_url, timeout=timeout)
        if resp.status_code == 200:
            try:
                payload = resp.json()
                if isinstance(payload, dict) and "ok" in payload:
                    report.rest_verdict = "yes"
                    report.rest_evidence = f"health OK at {rest_url}"
                else:
                    report.rest_verdict = "maybe"
                    report.rest_evidence = f"HTTP 200 but unexpected JSON: {str(payload)[:80]}"
            except ValueError:
                report.rest_verdict = "maybe"
                report.rest_evidence = f"HTTP 200 non-JSON body at {rest_url}"
        elif resp.status_code == 401:
            report.rest_verdict = "yes"
            report.rest_evidence = "endpoint exists and demands auth (expected: token)"
        elif resp.status_code == 404:
            report.rest_verdict = "no"
            report.rest_evidence = "HTTP 404"
        else:
            report.rest_verdict = "maybe"
            report.rest_evidence = f"HTTP {resp.status_code}"
    except requests.RequestException as exc:
        report.rest_verdict = "unreachable"
        report.rest_evidence = str(exc)

    return report
