"""Attendance collection service (round-robin poller per registered device).

Safety properties:
* Identity is re-verified on EVERY connection: the serial reported by the IP
  we dial must equal the registered serial. If another terminal inherited the
  IP (DHCP), collection refuses and discovery repairs the address — never
  attribute records to the wrong device.
* Read-only: the adapter contract forbids clearing device data.
* Each device has its own exponential backoff; one hung terminal cannot stall
  the others (bounded by device timeout).
* New records are persisted to SQLite BEFORE any internet upload is
  attempted; the EMS never sees a record that is not safely on disk first.
"""
from __future__ import annotations

import logging
import threading
import time

from ..devices.exceptions import (
    DeviceAuthError,
    DeviceConnectionError,
    DeviceError,
)
from ..devices.factory import create_adapter
from ..timeutils import iso_utc
from .pipeline import build_event

log = logging.getLogger("apexity_bridge.attendance.collector")


class AttendanceCollector:
    def __init__(self, ctx):
        self.ctx = ctx                    # supplies config/settings/repos/stop_event
        self._thread: threading.Thread | None = None
        self._backoff_until: dict[int, float] = {}   # device_id → monotonic deadline
        self._backoff_level: dict[int, int] = {}     # device_id → failure streak
        self._adapter_factory = getattr(ctx, "adapter_factory", None) or create_adapter
        self._wake = threading.Event()
        self.last_round: dict[int, dict] = {}        # device_id → stats for dashboard

    # ------------------------------------------------------------------ loop --
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        log.info("attendance collector started")
        while not self.ctx.stop_event.is_set():
            devices = self.ctx.repos["devices"].list_active()
            if not devices:
                self._wake.wait(timeout=15.0)
                self._wake.clear()
                continue
            now = time.monotonic()
            for device in devices:
                if self.ctx.stop_event.is_set():
                    break
                if now < self._backoff_until.get(device["id"], 0):
                    continue
                self.collect_device(device)
            delay = max(5, int(self.ctx.settings.collect_interval))
            self._wake.wait(timeout=delay)
            self._wake.clear()

    # --------------------------------------------------------------- one pass --
    def collect_device(self, device: dict) -> dict:
        """Connect → verify identity → pull + persist. Returns a stats dict."""
        repos = self.ctx.repos
        device_id = device["id"]
        serial = device.get("serial_number") or ""
        host, port = device.get("ip_address"), int(device.get("port") or 4370)

        stats = {"device_id": device_id, "serial": serial, "ok": False, "new": 0,
                 "total_read": 0, "skipped_invalid": 0, "error": None, "at": iso_utc()}

        if not serial:
            stats["error"] = "device has no verified serial — cannot collect safely"
            self._note(device, stats)
            return stats
        if not host:
            stats["error"] = "no known IP (run a LAN scan)"
            self._note(device, stats)
            return stats

        adapter = self._adapter_factory(
            host=host, port=port,
            comm_key=int(device.get("comm_key") or 0),
            timeout=int(self.ctx.config.device_timeout),
        )
        try:
            adapter.connect()
            try:
                identity = adapter.get_identity()
                if identity.serial_number and identity.serial_number != serial:
                    # THE STRANGER-AT-OLD-IP GUARD.
                    stats["error"] = (
                        f"identity mismatch at {host}: expected serial {serial}, "
                        f"terminal reports {identity.serial_number} — refusing to "
                        f"collect; discovery will repair the address"
                    )
                    repos["audit"].log(
                        actor="system", action="device.identity_mismatch",
                        entity="device", entity_id=serial,
                        detail=f"{host} answered with serial {identity.serial_number}",
                    )
                    log.error(stats["error"])
                    self._penalise(device_id, multiplier=4)
                    return stats

                records = adapter.read_attendance()
                stats["total_read"] = len(records)
            finally:
                adapter.disconnect()

            events, quarantined = [], []
            mapping_repo = repos["mappings"]

            def lookup(s: str, uid: str):
                row = mapping_repo.get(s, uid)
                if row:
                    student = repos["students"].get(row["ems_student_id"])
                    row["student_active"] = student["active"] if student else None
                return row

            for rec in records:
                ev = build_event(rec, device=device,
                                 timezone_label=self.ctx.settings.device_timezone,
                                 mapping_lookup=lookup)
                if ev is None:
                    stats["skipped_invalid"] += 1
                    continue
                if ev.pop("quarantine_reason", None):
                    quarantined.append(ev)
                    continue
                events.append(ev)

            new_ids = repos["events"].insert_new(events)
            stats["new"] = len(new_ids)
            if quarantined:
                qids = repos["events"].insert_new(quarantined)
                if qids:
                    repos["events"].mark_review(
                        qids, "suspicious device timestamp — review before upload"
                    )
                    stats["new"] += len(qids)
            repos["devices"].update_seen(device_id)
            self._reset_backoff(device_id)
            stats["ok"] = True
            if stats["new"]:
                log.info("device %s: %d new attendance events (read %d)",
                         serial, stats["new"], stats["total_read"])
        except DeviceAuthError as exc:
            stats["error"] = f"authentication failed: {exc}"
            self._penalise(device_id, multiplier=4)
            repos["audit"].log(actor="system", action="device.auth_failed",
                               entity="device", entity_id=serial, detail=str(exc)[:400])
        except (DeviceConnectionError, DeviceError) as exc:
            stats["error"] = str(exc)
            self._penalise(device_id)
            log.warning("collect %s (%s) failed: %s", serial, host, exc)
        except Exception as exc:  # pragma: no cover - defensive
            stats["error"] = f"unexpected: {exc}"
            self._penalise(device_id)
            log.exception("collect %s crashed", serial)
        finally:
            self._note(device, stats)
        return stats

    # ----------------------------------------------------------------- backoff --
    def _penalise(self, device_id: int, multiplier: int = 1) -> None:
        streak = self._backoff_level.get(device_id, 0) + 1
        self._backoff_level[device_id] = streak
        delay = min(5 * (2 ** min(streak, 6)), 300) * multiplier
        delay = min(delay, 600)
        self._backoff_until[device_id] = time.monotonic() + delay

    def _reset_backoff(self, device_id: int) -> None:
        self._backoff_level.pop(device_id, None)
        self._backoff_until.pop(device_id, None)

    def _note(self, device: dict, stats: dict) -> None:
        self.last_round[device["id"]] = stats
