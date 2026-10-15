"""EMS client interface & shared DTOs.

The bridge supports two wire protocols (selection is explicit or probed):

* ``iclock`` — the ZKTeco ADMS/iClock push protocol that many school
  systems (including the expected https://apexityedu.com/iclock) expose so
  that real terminals can push data. The bridge acts as the "device".
* ``rest`` — a secure, token-authenticated JSON contract designed for this
  bridge (docs/EMS_API_CONTRACT.md) for EMS backends without iClock support.

Everything the sync worker needs is defined here, so worker code is
protocol-agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


# ---------------------------------------------------------------- errors ----
class EmsError(Exception):
    """Base class for EMS failures."""


class EmsUnavailableError(EmsError):
    """Network/server unreachable — retry with backoff."""


class EmsAuthError(EmsError):
    """401/403 — credentials invalid or expired. Retried slowly; admin alerted."""


class EmsProtocolError(EmsError):
    """Server answered but the payload is not what the contract requires."""


# ------------------------------------------------------------------ DTOs ----
@dataclass(frozen=True)
class StudentInfo:
    ems_student_id: str
    name: str
    admission_no: str | None = None
    roll_no: str | None = None
    class_name: str | None = None
    section: str | None = None
    active: bool = True
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    server_time: str | None = None
    detail: str = ""
    protocol: str = ""


@dataclass(frozen=True)
class RegisterResult:
    ok: bool
    detail: str = ""
    remote_device_id: str | None = None


@dataclass(frozen=True)
class RosterPage:
    students: list[StudentInfo]
    next_cursor: str | None = None        # None → last page
    complete: bool = True


@dataclass(frozen=True)
class UploadOutcome:
    """Per-record upload result. On transport failure: ``fatal`` set,
    and accepted stays empty so the queue retries everything later."""
    accepted_ids: tuple[int, ...] = ()
    remote_ids: dict[int, str] = field(default_factory=dict)
    rejected: dict[str, str] = field(default_factory=dict)   # dedup_hash → reason
    duplicates: tuple[int, ...] = ()      # already on server → treat as synced
    fatal: str | None = None
    partial_index: int | None = None      # accepted only the first N (iclock)

    @property
    def ok(self) -> bool:
        return self.fatal is None


class EmsClient(Protocol):
    """The contract the sync worker relies upon."""

    protocol_name: str

    def health_check(self) -> HealthResult: ...
    def register_device(self, *, serial: str, model: str | None,
                        firmware: str | None, label: str | None) -> RegisterResult: ...
    def fetch_students(self, cursor: str | None = None) -> RosterPage: ...
    def push_attendance(self, events: list[dict]) -> UploadOutcome: ...
