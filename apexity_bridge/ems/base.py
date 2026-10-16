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
    """One person in the EMS directory.

    ``person_type`` is what makes teachers and non-teaching staff first-class
    citizens: a K40 does not care who punches, so the roster the bridge pulls
    must contain everybody who is expected to punch in.
    """

    ems_student_id: str
    name: str
    admission_no: str | None = None
    roll_no: str | None = None
    class_name: str | None = None
    section: str | None = None
    active: bool = True
    person_type: str = "student"          # student | teacher | staff | other
    employee_no: str | None = None
    designation: str | None = None
    department: str | None = None
    raw: dict = field(default_factory=dict)

    def as_cache_row(self) -> dict:
        return {
            "ems_student_id": self.ems_student_id,
            "name": self.name,
            "admission_no": self.admission_no,
            "roll_no": self.roll_no,
            "class_name": self.class_name,
            "section": self.section,
            "active": self.active,
            "person_type": self.person_type,
            "employee_no": self.employee_no,
            "designation": self.designation,
            "department": self.department,
            "raw": self.raw,
        }


# Historical name kept: the directory is people, not only students.
PersonInfo = StudentInfo

PERSON_TYPES = ("student", "teacher", "staff", "other")


@dataclass(frozen=True)
class UserSyncOutcome:
    """Result of pushing one terminal's user table to the EMS."""

    accepted: int = 0
    detail: str = ""
    fatal: str | None = None

    @property
    def ok(self) -> bool:
        return self.fatal is None


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
    """The contract the sync worker relies upon.

    ``fetch_people`` and ``push_users`` are optional capabilities: the worker
    checks for them with ``getattr`` so a minimal EMS (or a test double) that
    only ingests attendance keeps working.
    """

    protocol_name: str

    def health_check(self) -> HealthResult: ...
    def register_device(self, *, serial: str, model: str | None,
                        firmware: str | None, label: str | None) -> RegisterResult: ...
    def fetch_students(self, cursor: str | None = None) -> RosterPage: ...
    def push_attendance(self, events: list[dict], *, serial: str | None = None) -> UploadOutcome: ...
    # optional
    def fetch_people(self, cursor: str | None = None) -> RosterPage: ...
    def push_users(self, serial: str, users: list[dict]) -> UserSyncOutcome: ...
