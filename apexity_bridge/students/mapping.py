"""K40 device-user ↔ EMS student mapping service.

Rules:
* Mappings key on DEVICE SERIAL + device user id, so an IP change can never
  remap students (identity follows the serial).
* A mapping never creates or modifies EMS student records — it only links
  existing students retrieved from the EMS.
* Conflicts (one student ⇐ multiple device users) are detected, not blocked:
  real schools sometimes legitimately share accounts, so it is surfaced for
  administrator review rather than silently forbidden.
* After every change, unsent history is re-resolved so queued events gain
  the new target student; already-synced history stays untouched.
"""
from __future__ import annotations

import csv
import io
import logging

from ..ems.base import EmsProtocolError

log = logging.getLogger("apexity_bridge.students.mapping")

CSV_HEADER = ["device_serial", "device_user_id", "ems_student_id"]
MAX_CSV_ROWS = 5000


class MappingError(ValueError):
    pass


class MappingService:
    def __init__(self, repos, audit_actor: str = "admin"):
        self.repos = repos
        self.actor = audit_actor

    # ---------------------------------------------------------------- checks --
    def _assert_student_known(self, ems_student_id: str) -> dict:
        student = self.repos["students"].get(str(ems_student_id))
        if not student:
            raise MappingError(
                f"student {ems_student_id!r} is not in the EMS roster cache — "
                f"refresh the roster first (we never create student records locally)"
            )
        return student

    def _resolution_for(self, ems_student_id: str) -> str:
        student = self.repos["students"].get(str(ems_student_id))
        if not student:
            return "unmapped"
        return "inactive_student" if not student["active"] else "resolved"

    # ------------------------------------------------------------------- API ---
    def assign(self, *, device_serial: str, device_user_id: str, ems_student_id: str,
               source: str = "manual") -> int:
        device_user_id = str(device_user_id).strip()
        if not device_user_id:
            raise MappingError("device user id must not be empty")
        student = self._assert_student_known(ems_student_id)
        mapping_id = self.repos["mappings"].upsert(
            device_serial=device_serial, device_user_id=device_user_id,
            ems_student_id=str(ems_student_id), mapped_by=self.actor, source=source,
        )
        status = self._resolution_for(ems_student_id)
        retagged = self.repos["events"].re_resolve_mappings(
            device_serial, device_user_id, str(ems_student_id), status
        )
        self.repos["audit"].log(
            actor=self.actor, action="mapping.assign", entity="mapping",
            entity_id=f"{device_serial}:{device_user_id}",
            detail=f"→ EMS student {ems_student_id} ({student['name']}); "
                   f"{retagged} unsent events re-resolved",
        )
        return mapping_id

    def unassign(self, mapping_id: int) -> None:
        rows = [r for r in self.repos["mappings"].list() if r["id"] == mapping_id]
        if not rows:
            raise MappingError(f"mapping {mapping_id} not found")
        row = rows[0]
        self.repos["mappings"].delete(mapping_id)
        self.repos["events"].re_resolve_mappings(
            row["device_serial"], row["device_user_id"], None, "unmapped"
        )
        self.repos["audit"].log(
            actor=self.actor, action="mapping.remove", entity="mapping",
            entity_id=f"{row['device_serial']}:{row['device_user_id']}",
            detail=f"was → EMS student {row['ems_student_id']}",
        )

    # ------------------------------------------------------------------ CSV ---
    def export_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(CSV_HEADER)
        for row in self.repos["mappings"].list():
            writer.writerow([row["device_serial"], row["device_user_id"], row["ems_student_id"]])
        return buf.getvalue()

    def import_csv(self, text: str) -> dict:
        """Validated bulk import. Bad rows are reported, never partially applied
        silently: valid rows import atomically only when the WHOLE file parses;
        row-level validation failures abort the import with a detailed report."""
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None or [f.strip() for f in reader.fieldnames][:3] != CSV_HEADER:
            raise MappingError(f"CSV must start with header: {','.join(CSV_HEADER)}")

        rows = list(reader)
        if len(rows) > MAX_CSV_ROWS:
            raise MappingError(f"CSV has {len(rows)} rows; max {MAX_CSV_ROWS}")

        errors, staged = [], []
        seen_keys: set[tuple[str, str]] = set()
        for n, row in enumerate(rows, start=2):
            serial = (row.get("device_serial") or "").strip()
            user = (row.get("device_user_id") or "").strip()
            student_id = (row.get("ems_student_id") or "").strip()
            if not serial or not user or not student_id:
                errors.append(f"row {n}: device_serial, device_user_id and ems_student_id are required")
                continue
            if (serial, user) in seen_keys:
                errors.append(f"row {n}: duplicate mapping for {serial}:{user} inside the file")
                continue
            try:
                self._assert_student_known(student_id)
            except MappingError as exc:
                errors.append(f"row {n}: {exc}")
                continue
            seen_keys.add((serial, user))
            staged.append((serial, user, student_id))

        if errors:
            raise MappingError("import aborted — fix these rows first:\n" + "\n".join(errors[:50]))

        for serial, user, student_id in staged:
            self.assign(device_serial=serial, device_user_id=user,
                        ems_student_id=student_id, source="csv")
        self.repos["audit"].log(
            actor=self.actor, action="mapping.csv_import",
            detail=f"imported {len(staged)} mappings",
        )
        return {"imported": len(staged)}

    # --------------------------------------------------------------- reports ---
    def review_report(self) -> dict:
        return {
            "unmapped_device_users": self.repos["events"].unmapped_device_users(),
            "conflicts": self.repos["mappings"].conflicts(),
            "unknown_or_inactive_students": self.repos["mappings"].mappings_for_unknown_students(),
        }
