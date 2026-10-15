"""Offline-first synchronisation worker.

Guarantees (see docs/ARCHITECTURE.md):
* Events are persisted BEFORE any network call; "synced" is written only
  after explicit EMS acceptance (HTTP 200 + protocol-level OK, or per-event
  'accepted'/'duplicate' results).
* Exponential backoff with jitter on every failure mode; 'failed' events
  re-enter the due queue automatically; >10 attempts → 'review'.
* Crash-safety: at worst, events accepted by the EMS but not yet marked are
  re-pushed on restart; server-side dedup (SN+PIN+time for iClock, explicit
  idempotency keys for REST) absorbs that. Device logs are never deleted, so
  the device itself remains the final backup.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field

from ..ems.base import (
    EmsAuthError,
    EmsError,
    EmsProtocolError,
    EmsUnavailableError,
)
from ..ems.factory import build_ems_client
from ..students.directory import refresh_roster
from ..timeutils import iso_utc, seconds_from_now
from .backoff import backoff_seconds

log = logging.getLogger("apexity_bridge.sync.worker")

HEALTH_INTERVAL = 30
AUTH_PENALTY_DELAY = 300


@dataclass
class EmsStatus:
    """Thread-safe view for the dashboard."""
    online: bool = False
    protocol: str = ""
    last_ok_at: str | None = None
    last_error: str | None = None
    server_time: str | None = None
    probe: dict | None = None
    roster_supported: bool = True
    roster_error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, **changes) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(self, key, value)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "online": self.online,
                "protocol": self.protocol,
                "last_ok_at": self.last_ok_at,
                "last_error": self.last_error,
                "server_time": self.server_time,
                "probe": self.probe,
                "roster_supported": self.roster_supported,
                "roster_error": self.roster_error,
            }


class SyncWorker:
    def __init__(self, ctx):
        """ctx: config, settings, repos, stop_event, secret_store, status (EmsStatus)."""
        self.ctx = ctx
        self.status: EmsStatus = ctx.ems_status
        self._client = None
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._next_health = 0.0
        self._next_roster = 0.0
        self._audited_once: set[str] = set()
        self._build_client()

    # -------------------------------------------------------------- lifecycle --
    def _build_client(self) -> None:
        """Build/rebuild the client from the *effective* settings
        (dashboard DB overrides win over .env)."""
        import dataclasses

        try:
            cfg = dataclasses.replace(
                self.ctx.config,
                ems_base_url=self.ctx.settings.ems_base_url,
                ems_protocol=self.ctx.settings.ems_protocol,
            )
            self._client, probe = build_ems_client(cfg, self.ctx.secret_store)
            self.status.protocol = getattr(self._client, "protocol_name", "?")
            if probe:
                self.status.probe = probe
        except Exception as exc:
            log.error("EMS client could not be constructed: %s", exc)
            self._client = None
            self.status.last_error = str(exc)

    def rebuild_client(self) -> None:
        """Called when EMS settings change in the dashboard."""
        self._build_client()
        self._next_health = 0.0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="sync-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    # -------------------------------------------------------------------- loop --
    def _loop(self) -> None:
        log.info("sync worker started (protocol=%s)", self.status.protocol)
        # On boot: reconnect state + resume anything unfinished.
        self.ctx.repos["queue"].enqueue_once("register_device", {})
        while not self.ctx.stop_event.is_set():
            try:
                self._tick()
            except Exception:  # pragma: no cover - defensive
                log.exception("sync tick crashed (loop continues)")
            self._wake.wait(timeout=5.0)
            self._wake.clear()

    def _tick(self) -> None:
        now = time.monotonic()
        if now >= self._next_health:
            self._health_check()
            self._next_health = now + HEALTH_INTERVAL

        if not self.status.online or self._client is None:
            return  # everything stays queued; offline-first by design

        self._process_queue()
        self._maybe_refresh_roster(now)
        self.push_due_events()

    # ----------------------------------------------------------------- health --
    def _health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            result = self._client.health_check()
        except EmsAuthError as exc:
            self.status.update(online=False, last_error=str(exc))
            log.error("EMS auth problem: %s", exc)
            return False
        except EmsUnavailableError as exc:
            self.status.update(online=False, last_error=str(exc)[:300])
            log.warning("EMS unreachable: %s", exc)
            return False
        except EmsError as exc:
            self.status.update(online=False, last_error=str(exc)[:300])
            return False
        self.status.update(
            online=result.ok, last_ok_at=iso_utc(), server_time=result.server_time,
            last_error=None if result.ok else result.detail[:300],
        )
        if result.ok:
            self.ctx.repos["settings"].set("ems_last_ok", iso_utc())
        return result.ok

    # ------------------------------------------------------------- task queue --
    def _process_queue(self) -> None:
        queue = self.ctx.repos["queue"]
        for task in queue.due(iso_utc(), limit=5):
            try:
                if task["task_type"] == "fetch_students":
                    refresh_roster(self._client, self.ctx.repos)
                elif task["task_type"] == "register_device":
                    self._register_devices()
                elif task["task_type"] == "push_attendance":
                    pass  # due events are driven by push_due_events()
                else:
                    log.warning("unknown queue task %s", task["task_type"])
                queue.complete(task["id"])
            except EmsProtocolError as exc:
                # e.g. roster not supported by this EMS — do not keep hammering.
                queue.complete(task["id"])
                self.status.update(roster_supported=False, roster_error=str(exc)[:400])
                log.error("task %s not supported by EMS: %s", task["task_type"], exc)
            except EmsAuthError as exc:
                queue.fail(task["id"], str(exc)[:400],
                           seconds_from_now(AUTH_PENALTY_DELAY))
            except (EmsUnavailableError, EmsError) as exc:
                attempts = int(task["attempts"]) + 1
                delay = backoff_seconds(attempts, base=self.ctx.settings.sync_retry_base,
                                        cap=self.ctx.config.sync_retry_max)
                queue.fail(task["id"], str(exc)[:400], seconds_from_now(delay))

    def _register_devices(self) -> None:
        repos = self.ctx.repos
        for device in repos["devices"].list_active():
            flag = f"ems_registered:{device['serial_number']}"
            if repos["settings"].get(flag) == "1":
                continue
            try:
                result = self._client.register_device(
                    serial=device["serial_number"], model=device.get("model"),
                    firmware=device.get("firmware_version"), label=device.get("label"),
                )
            except EmsAuthError:
                raise
            if result.ok:
                repos["settings"].set(flag, "1")
                repos["audit"].log(
                    actor="system", action="device.ems_registered",
                    entity="device", entity_id=device["serial_number"],
                    detail=result.detail[:300],
                )
            else:
                raise EmsUnavailableError(
                    f"EMS refused device {device['serial_number']}: {result.detail}"
                )

    def _maybe_refresh_roster(self, now: float) -> None:
        if not self.status.roster_supported:
            return
        if now < self._next_roster:
            return
        self._next_roster = now + max(60, self.ctx.settings.roster_interval)
        self.ctx.repos["queue"].enqueue_once("fetch_students", {})

    # ----------------------------------------------------------------- pushes --
    def push_due_events(self) -> int:
        """Push due events in per-device batches. Returns number marked synced."""
        events_repo = self.ctx.repos["events"]
        queue = self.ctx.repos["queue"]
        due = events_repo.due_for_sync(iso_utc(), limit=self.ctx.config.sync_batch_size)
        if not due:
            return 0

        batches: dict[str, list[dict]] = {}
        for ev in due:
            batches.setdefault(ev["device_serial"], []).append(ev)

        synced_total = 0
        for serial, batch in batches.items():
            ids = [e["id"] for e in batch]
            started = time.monotonic()
            try:
                outcome = self._client.push_attendance(batch)
            except EmsAuthError as exc:
                events_repo.mark_attempt_failed(ids, str(exc)[:400],
                                                seconds_from_now(AUTH_PENALTY_DELAY))
                queue.record_attempt(attempt_type="push_attendance", event_count=len(ids),
                                     result="failed", error="auth: token rejected")
                self.status.update(online=False, last_error=str(exc)[:300])
                self._audit_once("ems.auth_failed",
                                 "EMS rejected device credentials during attendance push")
                continue
            except (EmsUnavailableError, EmsError) as exc:
                retry = backoff_seconds(batch[0]["retry_count"] + 1,
                                        base=self.ctx.settings.sync_retry_base,
                                        cap=self.ctx.config.sync_retry_max)
                events_repo.mark_attempt_failed(ids, str(exc)[:400], seconds_from_now(retry))
                queue.record_attempt(attempt_type="push_attendance", event_count=len(ids),
                                     result="failed", error=str(exc)[:300],
                                     duration_ms=int((time.monotonic() - started) * 1000))
                if isinstance(exc, EmsUnavailableError):
                    self.status.update(online=False, last_error=str(exc)[:300])
                continue

            # server accepted (possibly partially) -----------------------------
            accepted = list(outcome.accepted_ids or []) + list(outcome.duplicates or [])
            if accepted:
                events_repo.mark_synced(accepted, remote_ids=outcome.remote_ids)
                synced_total += len(accepted)
            for key, reason in (outcome.rejected or {}).items():
                events_repo.mark_rejected(key, reason)
            if outcome.partial_index is not None and outcome.partial_index < len(batch):
                leftover = ids[outcome.partial_index:]
                retry = backoff_seconds(batch[0]["retry_count"] + 1,
                                        base=self.ctx.settings.sync_retry_base,
                                        cap=self.ctx.config.sync_retry_max)
                events_repo.mark_attempt_failed(
                    leftover, f"EMS accepted only {outcome.partial_index}/{len(batch)}",
                    seconds_from_now(retry),
                )
            queue.record_attempt(
                attempt_type="push_attendance", event_count=len(ids),
                result="success" if len(accepted) == len(ids) else "partial",
                detail=f"accepted={len(accepted)} rejected={len(outcome.rejected or {})}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            if synced_total:
                self.status.update(last_ok_at=iso_utc())
        return synced_total

    # ----------------------------------------------------------------- helpers --
    def _audit_once(self, action: str, detail: str) -> None:
        if action in self._audited_once:
            return
        self._audited_once.add(action)
        self.ctx.repos["audit"].log(actor="system", action=action, detail=detail[:400])
