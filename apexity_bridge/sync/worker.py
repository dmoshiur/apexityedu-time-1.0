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

from ..database.repositories import ems_serial_for
from ..ems.base import (
    EmsAuthError,
    EmsError,
    EmsProtocolError,
    EmsUnavailableError,
    HealthResult,
)
from ..ems.factory import build_ems_client
from ..ems.iclock import command_is_read_only, command_verb
from ..students.directory import refresh_people
from ..timeutils import iso_utc, seconds_from_now
from .backoff import backoff_seconds

log = logging.getLogger("apexity_bridge.sync.worker")

HEALTH_INTERVAL = 30
AUTH_PENALTY_DELAY = 300
MAX_COMMAND_REPLAY = 1000          # "DATA QUERY ATTLOG" re-push cap


@dataclass
class EmsStatus:
    """Thread-safe view for the dashboard.

    ``online`` only means "the EMS answered a handshake". It is NOT proof that
    attendance is being stored: every iClock server worth its salt answers a
    ``cdata`` handshake for an unknown serial too (that is how real terminals
    discover their configuration). ``ingestion_verified`` is the honest signal —
    it flips only after the server explicitly accepted an ATTLOG upload.
    """
    online: bool = False
    protocol: str = ""
    last_ok_at: str | None = None
    last_error: str | None = None
    server_time: str | None = None
    probe: dict | None = None
    device_presence: dict = field(default_factory=dict)
    roster_supported: bool = True
    roster_error: str | None = None
    ingestion_verified: bool = False
    last_push_at: str | None = None
    last_push_error: str | None = None
    users_pushed_at: str | None = None
    users_last_error: str | None = None
    commands_seen: dict = field(default_factory=dict)
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
                "device_presence": {sn: dict(state) for sn, state in self.device_presence.items()},
                "roster_supported": self.roster_supported,
                "roster_error": self.roster_error,
                "ingestion_verified": self.ingestion_verified,
                "last_push_at": self.last_push_at,
                "last_push_error": self.last_push_error,
                "users_pushed_at": self.users_pushed_at,
                "users_last_error": self.users_last_error,
                "commands_seen": dict(self.commands_seen),
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
        self._next_users = 0.0
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
        self.status.update(online=False, device_presence={})
        self._next_health = 0.0
        self._next_users = 0.0
        self.wake()

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
        self._maybe_sync_device_users(now)
        self.push_due_events()

    # ----------------------------------------------------------------- health --
    def _health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            devices = self.ctx.repos["devices"].list_active()
            if self._client.protocol_name == "iclock" and devices:
                result = self._iclock_device_health(devices)
            else:
                self.status.update(device_presence={})
                result = self._client.health_check()
                if result.ok:
                    result = HealthResult(ok=True, server_time=result.server_time)
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
            online=result.ok, server_time=result.server_time,
            last_error=result.detail[:300] or None,
            **({"last_ok_at": iso_utc()} if result.ok else {}),
        )
        if result.ok:
            self.ctx.repos["settings"].set("ems_last_ok", iso_utc())
        return result.ok

    def _iclock_device_health(self, devices: list[dict]) -> HealthResult:
        """Refresh real serials every health interval, even with no new punches.

        This advertises bridge-to-EMS presence, not proof that the physical
        terminal is currently reachable on the LAN. A missing/failed presence
        endpoint is reported, but does not prevent ATTLOG uploads to servers
        that support ingestion without getrequest. One bad serial must not
        stop checks or uploads for the other devices.
        """
        presence = {}
        errors = []
        for device in devices:
            if self.ctx.stop_event.is_set():
                break
            serial = device["serial_number"]
            target = ems_serial_for(device)          # the SN the EMS has on file
            state = {"handshake_ok": False, "poll_ok": False, "error": None,
                     "ems_serial": target, "override": target != serial}
            try:
                result = self._client.register_device(
                    serial=target, model=device.get("model"),
                    firmware=device.get("firmware_version"), label=device.get("label"),
                )
                if not result.ok:
                    raise EmsProtocolError(result.detail)
                state["handshake_ok"] = True
                self.ctx.repos["settings"].set(f"ems_registered:{target}", "1")
                commands = self._client.poll_commands(target)
                state["poll_ok"] = True
                state["last_ok_at"] = iso_utc()
                self._handle_commands(device, commands)
            except EmsError as exc:
                state["error"] = str(exc)[:300]
                errors.append(f"SN {target}: {state['error']}")
                log.warning("EMS device presence failed for SN %s: %s", target, exc)
            presence[serial] = state
        self.status.update(device_presence=presence)
        return HealthResult(
            ok=any(state["handshake_ok"] for state in presence.values()),
            protocol="iclock", detail="; ".join(errors),
        )

    # ------------------------------------------------------------- task queue --
    def _process_queue(self) -> None:
        queue = self.ctx.repos["queue"]
        for task in queue.due(iso_utc(), limit=5):
            try:
                if task["task_type"] in ("fetch_students", "fetch_people"):
                    refresh_people(self._client, self.ctx.repos)
                    self.status.update(roster_supported=True, roster_error=None)
                elif task["task_type"] == "register_device":
                    self._register_devices()
                elif task["task_type"] == "sync_device_users":
                    self._sync_device_users()
                elif task["task_type"] == "push_attendance":
                    pass  # due events are driven by push_due_events()
                else:
                    log.warning("unknown queue task %s", task["task_type"])
                queue.complete(task["id"])
            except EmsProtocolError as exc:
                if task["task_type"] in ("fetch_students", "fetch_people"):
                    # iClock roster downloads are unsupported, not device failures.
                    queue.complete(task["id"])
                    self.status.update(roster_supported=False, roster_error=str(exc)[:400])
                else:
                    delay = backoff_seconds(int(task["attempts"]) + 1,
                                            base=self.ctx.settings.sync_retry_base,
                                            cap=self.ctx.config.sync_retry_max)
                    queue.fail(task["id"], str(exc)[:400], seconds_from_now(delay))
                    self.status.update(last_error=str(exc)[:300])
                log.error("EMS task %s failed: %s", task["task_type"], exc)
            except EmsAuthError as exc:
                if task["task_type"] in ("fetch_students", "fetch_people"):
                    # Without a token the REST directory is unreachable, so the
                    # capability is genuinely unavailable to this installation —
                    # say so instead of retrying forever. Setting a token resets
                    # the flag (see facade.set_token).
                    queue.complete(task["id"])
                    message = (
                        "the EMS people roster (students + teachers + staff) is "
                        f"unavailable: {exc}"
                    )[:500]
                    self.status.update(roster_supported=False, roster_error=message)
                    self.ctx.repos["settings"].set("roster_last_error", message)
                else:
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
            serial = device["serial_number"]
            target = ems_serial_for(device)
            flag = f"ems_registered:{target}"
            if repos["settings"].get(flag) == "1":
                continue
            try:
                result = self._client.register_device(
                    serial=target, model=device.get("model"),
                    firmware=device.get("firmware_version"), label=device.get("label"),
                )
            except EmsAuthError:
                raise
            if result.ok:
                repos["settings"].set(flag, "1")
                repos["audit"].log(
                    actor="system", action="device.ems_registered",
                    entity="device", entity_id=serial,
                    detail=(f"EMS serial {target}: {result.detail}")[:400],
                )
            else:
                raise EmsUnavailableError(
                    f"EMS refused device {serial} (pushed as SN {target}): {result.detail}"
                )

    def _maybe_refresh_roster(self, now: float) -> None:
        if not self.status.roster_supported:
            return
        if now < self._next_roster:
            return
        self._next_roster = now + max(60, self.ctx.settings.roster_interval)
        self.ctx.repos["queue"].enqueue_once("fetch_people", {})

    # ---------------------------------------------------------- device users --
    def _maybe_sync_device_users(self, now: float) -> None:
        """Keep the EMS informed about who is enrolled on each terminal.

        An iClock server that only ever receives PINs cannot attribute a punch
        to a student, a teacher or a member of staff. Publishing the terminal's
        user table (PIN + name, never templates or passwords) is what lets the
        EMS offer a "map this device user to a person" screen — and it is what
        makes the bridge's own Device Users page useful.
        """
        if not bool(getattr(self.ctx.settings, "ems_push_users", True)):
            return
        if now < self._next_users:
            return
        self._next_users = now + max(60, int(getattr(self.ctx.config,
                                                     "users_sync_interval", 900)))
        self._sync_device_users()

    def _sync_device_users(self) -> dict:
        """Push each terminal's cached user table. Returns a per-device summary.

        The periodic path only republishes a table when it actually changed
        (``force=False``); the manual button in the desktop app calls
        :meth:`push_device_users_now` with ``force=True`` instead.
        """
        push_users = getattr(self._client, "push_users", None)
        if push_users is None:
            return {"skipped": "this EMS protocol cannot receive a user table"}
        if not bool(getattr(self.ctx.settings, "ems_push_users", True)):
            return {"skipped": "user sync disabled (EMS_PUSH_USERS=0)"}

        from ..ems.push import push_device_users

        try:
            result = push_device_users(self.ctx, device_id=None, force=False,
                                       actor="system")
        except Exception as exc:            # noqa: BLE001 - never kill the loop
            log.warning("device-user sync failed: %s", exc)
            self.status.update(users_last_error=str(exc)[:300])
            return {"skipped": str(exc)[:200]}

        if result.get("pushed"):
            self.status.update(users_pushed_at=iso_utc(), users_last_error=None)
        elif result.get("error"):
            self.status.update(users_last_error=str(result["error"])[:300])
        return {entry.get("serial"): entry for entry in result.get("devices") or []}

    def push_device_users_now(self, device_id: int | None = None, *,
                              force: bool = True) -> dict:
        """Manual push of the terminal user tables — returns what happened.

        Unlike the periodic path this does **not** depend on the EMS being
        marked "online": the operator pressed the button, so the bridge
        tries right now and reports the server's exact answer.
        """
        from ..ems.push import push_device_users

        result = push_device_users(self.ctx, device_id=device_id, force=force,
                                   actor="manual")
        if result.get("pushed"):
            self.status.update(users_pushed_at=iso_utc(), users_last_error=None)
        elif result.get("error"):
            self.status.update(users_last_error=str(result["error"])[:300])
        self._next_users = time.monotonic() + max(
            60, int(getattr(self.ctx.config, "users_sync_interval", 900)))
        return result

    # ---------------------------------------------------------- ADMS commands --
    def _handle_commands(self, device: dict, commands: list[str]) -> None:
        """Answer the *read-only* commands an ADMS server sends via getrequest.

        A real terminal answers ``DATA QUERY USERINFO`` by uploading its user
        table and ``DATA QUERY ATTLOG`` by re-uploading punches. The bridge does
        the same for those two, and refuses everything else — it is a collector,
        not a remote control. Destructive commands (CLEAR/DELETE/REBOOT/SET…)
        are logged and audited but never executed.
        """
        if not commands:
            return
        if not bool(getattr(self.ctx.settings, "ems_execute_commands", True)):
            return
        serial = device.get("serial_number")
        for command in commands:
            verb = command_verb(command)
            if not command_is_read_only(command):
                self.ctx.repos["audit"].log(
                    actor="system", action="ems.command_refused", entity="device",
                    entity_id=serial, detail=f"{command!r} — read-only bridge, refused",
                )
                log.warning("EMS command refused (not executed): %s", command)
                continue
            if verb.startswith("DATA QUERY BIODATA") or verb.startswith("DATA QUERY FINGERTMP"):
                log.info("ignored %s — the bridge never handles biometric templates", verb)
                continue
            if verb.startswith("DATA QUERY USERINFO"):
                self._sync_device_users()
            elif verb.startswith("DATA QUERY ATTLOG"):
                self._replay_events(device)
            else:
                log.debug("EMS command %s needs no data response", verb)
            seen = dict(self.status.commands_seen)
            seen[verb] = iso_utc()
            self.status.update(commands_seen=seen)

    def _replay_events(self, device: dict) -> None:
        """Re-upload this terminal's punches after a DATA QUERY ATTLOG request.

        Safe by contract: iClock servers deduplicate on SN+PIN+timestamp+status,
        so replaying records the EMS already has changes nothing.
        """
        serial = device.get("serial_number")
        if not serial:
            return
        rows = self.ctx.repos["db"].query(
            """SELECT * FROM attendance_events WHERE device_serial = ?
               ORDER BY event_timestamp DESC LIMIT ?""",
            (serial, MAX_COMMAND_REPLAY),
        )
        if not rows:
            return
        batch = [{k: r[k] for k in r.keys()} for r in rows]
        try:
            outcome = self._client.push_attendance(batch, serial=ems_serial_for(device))
        except EmsError as exc:
            log.warning("ATTLOG replay for SN %s failed: %s", serial, exc)
            return
        accepted = list(outcome.accepted_ids or []) + list(outcome.duplicates or [])
        if accepted:
            self.ctx.repos["events"].mark_synced(accepted, remote_ids=outcome.remote_ids)
            self.status.update(ingestion_verified=True, last_push_at=iso_utc())
        self.ctx.repos["queue"].record_attempt(
            attempt_type="push_attendance", event_count=len(batch),
            result="success" if len(accepted) == len(batch) else "partial",
            detail="DATA QUERY ATTLOG replay",
        )

    # ----------------------------------------------------------------- pushes --
    def push_due_events(self) -> int:
        """Push due events in per-device batches. Returns number marked synced."""
        events_repo = self.ctx.repos["events"]
        queue = self.ctx.repos["queue"]
        due = events_repo.due_for_sync(iso_utc(), limit=self.ctx.config.sync_batch_size)
        if not due:
            return 0

        # Group by the serial the EMS knows, NOT by the hardware serial: a
        # school that registered a device in the EMS under a different serial
        # gets uploads the server will actually accept (devices.ems_serial).
        devices = {d.get("serial_number"): d for d in self.ctx.repos["devices"].list()}
        batches: dict[str, list[dict]] = {}
        for ev in due:
            device = devices.get(ev["device_serial"])
            key = ems_serial_for(device) if device else ev["device_serial"]
            ev["ems_serial"] = key
            batches.setdefault(key, []).append(ev)

        synced_total = 0
        for serial, batch in batches.items():
            ids = [e["id"] for e in batch]
            started = time.monotonic()
            try:
                outcome = self._client.push_attendance(batch, serial=serial)
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
                self.status.update(last_error=str(exc)[:300],
                                   **({"online": False} if isinstance(exc, EmsUnavailableError) else {}))
                log.warning("EMS attendance upload failed for SN %s: %s", serial, exc)
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
            if accepted:
                # Proof, not assumption: the server explicitly acknowledged
                # records for this serial.
                self.status.update(ingestion_verified=True, last_push_at=iso_utc(),
                                   last_push_error=None, last_ok_at=iso_utc())
            elif outcome.rejected:
                self.status.update(
                    last_push_error=("the EMS rejected records: "
                                     + "; ".join(list(outcome.rejected.values())[:3]))[:300])
        return synced_total

    # ----------------------------------------------------------------- helpers --
    def _audit_once(self, action: str, detail: str) -> None:
        if action in self._audited_once:
            return
        self._audited_once.add(action)
        self.ctx.repos["audit"].log(actor="system", action=action, detail=detail[:400])
