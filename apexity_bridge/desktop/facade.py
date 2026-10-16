"""Qt-free façade over BridgeContext — the desktop window talks only to this."""
from __future__ import annotations

import logging
from pathlib import Path

from ..config import DB_MANAGED_KEYS
from ..dashboard.app import _device_online, tail_log_file
from ..dashboard.auth import (
    SETTINGS_PASSWORD_HASH, SETTINGS_USERNAME, admin_username,
    password_is_configured,
)
from ..security.passwords import hash_password, verify_password
from ..students.mapping import MappingError, MappingService

log = logging.getLogger("apexity_bridge.desktop.facade")


class FacadeError(Exception):
    pass


class DesktopFacade:
    def __init__(self, ctx):
        self.ctx = ctx
        self.actor = "desktop"

    # ---------------------------------------------------------------- auth --
    def needs_setup(self) -> bool:
        return not password_is_configured(self.ctx.repos["settings"])

    def check_login(self, username: str, password: str) -> bool:
        stored = self.ctx.repos["settings"].get(SETTINGS_PASSWORD_HASH)
        if not stored:
            return False
        if username != admin_username(self.ctx.repos["settings"]):
            return False
        if not verify_password(password, stored):
            return False
        self.actor = username or "admin"
        self.ctx.repos["audit"].log(actor=self.actor, action="desktop.login")
        return True

    def initial_setup(self, username: str, password: str, token: str = "") -> None:
        hashed = hash_password(password)
        self.ctx.repos["settings"].set(SETTINGS_PASSWORD_HASH, hashed, is_secret=True)
        name = (username or "admin").strip()[:64] or "admin"
        self.ctx.repos["settings"].set(SETTINGS_USERNAME, name)
        if token and self.ctx.secret_store:
            self.ctx.secret_store.set_ems_token(token)
        self.actor = name
        self.ctx.repos["audit"].log(actor=name, action="desktop.initial_setup")

    def change_password(self, current: str, new: str, confirm: str) -> None:
        stored = self.ctx.repos["settings"].get(SETTINGS_PASSWORD_HASH)
        if not stored or not verify_password(current, stored):
            raise FacadeError("current password is incorrect")
        if new != confirm:
            raise FacadeError("new passwords do not match")
        self.ctx.repos["settings"].set(
            SETTINGS_PASSWORD_HASH, hash_password(new), is_secret=True)
        self.ctx.repos["audit"].log(actor=self.actor, action="desktop.password_changed")

    def mapping(self) -> MappingService:
        return MappingService(self.ctx.repos, audit_actor=self.actor)

    # ------------------------------------------------------------- overview --
    def overview(self) -> dict:
        repos = self.ctx.repos
        events = repos["events"].counts()
        devices = repos["devices"].counts()
        online_known = [d for d in repos["devices"].list_active()
                        if _device_online(self.ctx, d)]
        return {
            "service": {
                "version": self.ctx.version,
                "uptime_seconds": int(self.ctx.uptime_seconds()),
                "timezone": self.ctx.settings.device_timezone,
            },
            "ems": self.ctx.ems_status.snapshot(),
            "devices": {**devices, "online_now": len(online_known)},
            "students": {
                "total": repos["students"].count(),
                "active": repos["students"].count(active_only=True),
                "mappings": repos["mappings"].count(),
                "by_type": repos["students"].counts_by_type(),
            },
            "device_users": {
                "total": repos["device_users"].count(),
                "unmapped": repos["device_users"].unmapped_count(),
            },
            "events": events,
            "queue_pending": repos["queue"].pending_count(),
            "last_synced_at": repos["events"].last_synced_at(),
            "last_attempts": repos["queue"].recent_attempts(8),
            "needs_setup": self.needs_setup(),
        }

    # -------------------------------------------------------------- devices --
    def devices(self) -> dict:
        rows = self.ctx.repos["devices"].list()
        for d in rows:
            d["online_now"] = _device_online(self.ctx, d)
            d["event_count"] = self.ctx.repos["devices"].count_events_for(d["id"])
        report = None
        if self.ctx.discovery and self.ctx.discovery.last_report:
            report = self.ctx.discovery.last_report.to_dict()
        return {"devices": rows, "last_scan": report}

    def scan_now(self) -> dict:
        if self.ctx.discovery is None:
            raise FacadeError("discovery service is not running")
        self.ctx.repos["audit"].log(actor=self.actor, action="scan.manual")
        report = self.ctx.discovery.run_scan(blocking=True)
        return report.to_dict()

    def device_action(self, device_id: int, action: str, **kw) -> dict:
        repos = self.ctx.repos
        device = repos["devices"].get_by_id(device_id)
        if not device:
            raise FacadeError("device not found")
        if action == "register":
            repos["devices"].register(device_id, label=kw.get("label") or device.get("label"))
            repos["audit"].log(actor=self.actor, action="device.register",
                               entity="device", entity_id=str(device.get("serial_number")))
            repos["queue"].enqueue_once("register_device", {})
            if self.ctx.collector:
                self.ctx.collector.wake()
        elif action == "disable":
            repos["devices"].set_status(device_id, "disabled")
            repos["audit"].log(actor=self.actor, action="device.disable",
                               entity="device", entity_id=str(device.get("serial_number")))
        elif action == "enable":
            repos["devices"].set_status(device_id, "registered")
            repos["audit"].log(actor=self.actor, action="device.enable",
                               entity="device", entity_id=str(device.get("serial_number")))
        elif action == "label":
            repos["devices"].set_label(device_id, (kw.get("label") or "")[:100])
        elif action == "remove":
            repos["devices"].remove(device_id)
            repos["audit"].log(actor=self.actor, action="device.remove",
                               entity="device", entity_id=str(device.get("serial_number")))
        elif action == "test":
            return self._device_test(device)
        else:
            raise FacadeError(f"unknown action {action!r}")
        return repos["devices"].get_by_id(device_id) or {}

    def _device_test(self, device: dict) -> dict:
        from ..discovery.identify import identify_host
        import time
        started = time.monotonic()
        result = identify_host(
            device["ip_address"], int(device.get("port") or 4370),
            timeout=self.ctx.config.device_timeout,
            comm_key=int(device.get("comm_key") or 0),
            adapter_factory=self.ctx.adapter_factory,
        )
        elapsed = int((time.monotonic() - started) * 1000)
        self.ctx.repos["audit"].log(
            actor=self.actor, action="device.test",
            entity="device", entity_id=str(device.get("serial_number")),
            detail=f"{device['ip_address']} identified={result.identified}",
        )
        if not result.identified:
            raise FacadeError(f"connection test failed: {result.error}")
        identity = result.identity
        return {
            "serial": identity.serial_number, "model": identity.model,
            "firmware": identity.firmware_version,
            "serial_matches_registered": identity.serial_number == (device.get("serial_number") or ""),
            "elapsed_ms": elapsed,
        }

    def device_manual(self, ip: str, port: int = 4370, comm_key: int = 0) -> dict:
        from ..discovery.identify import identify_host
        ip = (ip or "").strip()
        if not ip:
            raise FacadeError("ip is required")
        result = identify_host(ip, port, timeout=self.ctx.config.device_timeout,
                               comm_key=comm_key, adapter_factory=self.ctx.adapter_factory)
        if not result.identified or not (result.identity and result.identity.serial_number):
            raise FacadeError(f"device at {ip} did not verify its identity: {result.error}")
        identity = result.identity
        repos = self.ctx.repos
        existing = repos["devices"].get_by_serial(identity.serial_number)
        if existing:
            repos["devices"].update_verified_ip(existing["id"], ip)
            repos["audit"].log(actor=self.actor, action="device.manual_verify",
                               entity="device", entity_id=identity.serial_number)
            return repos["devices"].get_by_id(existing["id"])
        device_id, _ = repos["devices"].upsert_identified(
            serial_number=identity.serial_number, model=identity.model,
            mac_address=identity.mac_address, firmware_version=identity.firmware_version,
            platform=identity.platform, ip_address=ip, port=port,
        )
        repos["devices"].update_verified_ip(device_id, ip)
        repos["audit"].log(actor=self.actor, action="device.manual_add",
                           entity="device", entity_id=identity.serial_number)
        return repos["devices"].get_by_id(device_id)

    # ------------------------------------------------------------- students --
    def students(self, *, search=None, class_name=None, section=None,
                 active=None, person_type=None, page=1, page_size=50) -> dict:
        """The EMS directory: students **and** teachers **and** staff."""
        rows, total = self.ctx.repos["students"].list(
            search=search, class_name=class_name, section=section,
            person_type=person_type, active=active, page=page, page_size=page_size,
            sort="name", desc=False,
        )
        return {
            "students": rows, "people": rows, "total": total, "page": page,
            "page_size": page_size,
            "classes": self.ctx.repos["students"].classes(),
            "by_type": self.ctx.repos["students"].counts_by_type(),
            "roster_last_sync": self.ctx.repos["settings"].get("roster_last_sync"),
            "roster_last_error": self.ctx.repos["settings"].get("roster_last_error"),
        }

    # --------------------------------------------------------- device users --
    def device_users(self, *, device_serial=None, search=None,
                     unmapped_only=False, page=1, page_size=200) -> dict:
        rows, total = self.ctx.repos["device_users"].list(
            device_serial=device_serial, search=search,
            unmapped_only=unmapped_only, page=page, page_size=page_size,
        )
        return {
            "device_users": rows, "total": total, "page": page,
            "page_size": page_size,
            "unmapped": self.ctx.repos["device_users"].unmapped_count(),
            "devices": self.ctx.repos["devices"].list_active(),
        }

    def read_device_users(self, device_id: int) -> dict:
        """Read one terminal's user table now (read-only on the device)."""
        if self.ctx.collector is None:
            raise FacadeError("collector is not running")
        try:
            result = self.ctx.collector.refresh_users(device_id)
        except ValueError as exc:
            raise FacadeError(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - device dependent
            raise FacadeError(f"could not read the device user table: {exc}") from exc
        self.ctx.repos["audit"].log(actor=self.actor, action="device_users.read",
                                    detail=str(result)[:300])
        return result

    def push_device_users(self, device_id: int | None = None) -> dict:
        if self.ctx.sync_worker is None:
            raise FacadeError("sync worker is not running")
        if device_id:
            device = self.ctx.repos["devices"].get_by_id(device_id)
            if not device:
                raise FacadeError("device not found")
            self.ctx.repos["devices"].set_users_pushed_hash(device_id, None)
        else:
            for device in self.ctx.repos["devices"].list_active():
                self.ctx.repos["devices"].set_users_pushed_hash(device["id"], None)
        self.ctx.repos["audit"].log(actor=self.actor,
                                    action="device_users.push_requested")
        self.ctx.sync_worker._next_users = 0.0
        self.ctx.sync_worker.wake()
        return {"queued": True}

    def set_ems_serial(self, device_id: int, value: str) -> dict:
        device = self.ctx.repos["devices"].get_by_id(device_id)
        if not device:
            raise FacadeError("device not found")
        self.ctx.repos["devices"].set_ems_serial(device_id, value)
        self.ctx.repos["devices"].set_users_pushed_hash(device_id, None)
        self.ctx.repos["audit"].log(
            actor=self.actor, action="device.ems_serial",
            entity="device", entity_id=str(device.get("serial_number")),
            detail=f"EMS serial → {(value or '').strip() or '(hardware serial)'}")
        if self.ctx.sync_worker:
            self.ctx.sync_worker.rebuild_client()
        return self.ctx.repos["devices"].get_by_id(device_id) or {}

    def ems_verify(self, sn: str | None = None) -> dict:
        """Live proof of what the EMS accepts (handshake is not proof)."""
        from ..ems.verify import format_report, verify_ems_ingestion
        if self.ctx.ems_client is None:
            raise FacadeError("no EMS client is configured")
        report = verify_ems_ingestion(self.ctx, (sn or "").strip() or None)
        report["text"] = format_report(report)
        self.ctx.repos["audit"].log(actor=self.actor, action="ems.verify",
                                    detail=str(report.get("verdict"))[:400])
        return report

    def refresh_roster(self) -> dict:
        from ..students.directory import refresh_roster
        from ..ems.base import EmsAuthError, EmsProtocolError, EmsUnavailableError
        self.ctx.repos["audit"].log(actor=self.actor, action="roster.refresh")
        try:
            return refresh_roster(self.ctx.ems_client, self.ctx.repos)
        except (EmsProtocolError, EmsAuthError, EmsUnavailableError) as exc:
            raise FacadeError(str(exc)) from exc
        except Exception as exc:
            raise FacadeError(f"roster refresh failed: {exc}") from exc

    def mapping_review(self) -> dict:
        return self.mapping().review_report()

    def assign_mapping(self, device_serial: str, device_user_id: str, ems_student_id: str) -> int:
        try:
            return self.mapping().assign(
                device_serial=device_serial, device_user_id=device_user_id,
                ems_student_id=ems_student_id,
            )
        except MappingError as exc:
            raise FacadeError(str(exc)) from exc

    def auto_map(self, device_serial: str | None = None, apply: bool = True) -> dict:
        """Bind device users to people automatically where the match is exact."""
        try:
            return self.mapping().auto_map(device_serial=device_serial or None,
                                           apply=apply)
        except MappingError as exc:
            raise FacadeError(str(exc)) from exc

    def unassign_mapping(self, mapping_id: int) -> None:
        try:
            self.mapping().unassign(mapping_id)
        except MappingError as exc:
            raise FacadeError(str(exc)) from exc

    def import_csv(self, text: str) -> dict:
        try:
            return self.mapping().import_csv(text)
        except MappingError as exc:
            raise FacadeError(str(exc)) from exc

    def export_csv(self) -> str:
        self.ctx.repos["audit"].log(actor=self.actor, action="mapping.csv_export")
        return self.mapping().export_csv()

    # ----------------------------------------------------------- attendance --
    def events(self, *, status=None, search=None, page=1, page_size=50) -> dict:
        rows, total = self.ctx.repos["events"].list_recent(
            limit=page_size, offset=(page - 1) * page_size,
            status=status or None, search=search or None,
        )
        return {"events": rows, "total": total, "page": page, "page_size": page_size}

    def retry_events(self, ids: list[int]) -> int:
        updated = self.ctx.repos["events"].retry_now(ids[:500])
        self.ctx.repos["audit"].log(actor=self.actor, action="events.retry",
                                    detail=f"{updated} events re-queued")
        if self.ctx.sync_worker:
            self.ctx.sync_worker.wake()
        return updated

    # ---------------------------------------------------------------- logs --
    def logs(self, *, search=None, action=None, page=1, page_size=50) -> dict:
        rows, total = self.ctx.repos["audit"].list(
            limit=page_size, offset=(page - 1) * page_size,
            search=search or None, action=action or None,
        )
        return {
            "logs": rows, "total": total, "page": page, "page_size": page_size,
            "actions": self.ctx.repos["audit"].actions(),
            "attempts": self.ctx.repos["queue"].recent_attempts(20),
        }

    def app_log(self, search: str = "") -> list[str]:
        lines = tail_log_file(self.ctx.log_file(), max_lines=400)
        needle = (search or "").lower()
        if needle:
            lines = [ln for ln in lines if needle in ln.lower()]
        return lines[-200:]

    # ------------------------------------------------------------ settings --
    def settings(self) -> dict:
        repos = self.ctx.repos
        return {
            "settings": {k: repos["settings"].get(k) for k in DB_MANAGED_KEYS},
            "effective": {k: str(getattr(self.ctx.settings, attr))
                          for k, (attr, _c) in DB_MANAGED_KEYS.items()},
            "token_configured": bool(self.ctx.secret_store and self.ctx.secret_store.has_ems_token()),
        }

    def save_settings(self, payload: dict) -> list[str]:
        repos = self.ctx.repos
        changed = []
        for key in DB_MANAGED_KEYS:
            if key not in payload:
                continue
            value = str(payload.get(key) or "").strip()[:300]
            if key == "ems_protocol" and value and value not in ("auto", "iclock", "rest"):
                raise FacadeError("ems_protocol must be auto, iclock or rest")
            old = repos["settings"].get(key)
            if value in ("", "None"):
                repos["settings"].delete(key)
            elif value != old:
                repos["settings"].set(key, value)
            changed.append(key)
        if changed:
            repos["audit"].log(actor=self.actor, action="settings.update",
                               detail=",".join(changed))
            self.ctx.apply_settings_overrides()
        return changed

    def set_token(self, token: str) -> bool:
        if self.ctx.secret_store is None:
            raise FacadeError("credential store is not available")
        token = (token or "").strip()
        if not token:
            self.ctx.secret_store.delete_ems_token()
            self.ctx.repos["audit"].log(actor=self.actor, action="settings.token_cleared")
            if self.ctx.sync_worker:
                self.ctx.sync_worker.rebuild_client()
            return False
        self.ctx.secret_store.set_ems_token(token)
        self.ctx.repos["audit"].log(actor=self.actor, action="settings.token_set",
                                    detail="EMS device token updated")
        if self.ctx.sync_worker:
            self.ctx.sync_worker.rebuild_client()
            # A token can unlock the REST people directory, so clear the
            # "roster unsupported" flag and let the worker try again.
            self.ctx.sync_worker.status.update(roster_supported=True, roster_error=None)
            self.ctx.sync_worker._next_roster = 0.0
            self.ctx.sync_worker.wake()
        return True

    def ems_test(self) -> dict:
        from ..ems.probe import probe_ems
        from ..ems.base import EmsError
        base = (self.ctx.repos["settings"].get("ems_base_url")
                or self.ctx.config.ems_base_url)
        report = probe_ems(base, iclock_path=self.ctx.config.ems_iclock_path,
                           timeout=self.ctx.config.ems_timeout)
        health = None
        if self.ctx.ems_client is not None:
            try:
                h = self.ctx.ems_client.health_check()
                health = {"ok": h.ok, "detail": h.detail, "server_time": h.server_time}
            except EmsError as exc:
                health = {"ok": False, "detail": str(exc)[:300]}
        self.ctx.repos["audit"].log(actor=self.actor, action="settings.ems_test",
                                    detail=str(base))
        return {"probe": report.to_dict(), "health": health}

    def backup(self) -> str:
        import time
        from ..paths import backup_dir
        target = backup_dir() / f"bridge-backup-{time.strftime('%Y%m%d-%H%M%S')}.db"
        self.ctx.repos["db"].backup_to(target)
        self.ctx.repos["audit"].log(actor=self.actor, action="backup.created",
                                    detail=str(target))
        return str(target)

    def diagnostics(self) -> Path:
        from ..dashboard.app import build_diagnostic_zip
        path = build_diagnostic_zip(self.ctx)
        self.ctx.repos["audit"].log(actor=self.actor, action="diagnostics.export",
                                    detail=path.name)
        return path
