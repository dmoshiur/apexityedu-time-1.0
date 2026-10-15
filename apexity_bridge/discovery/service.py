"""Background LAN discovery service.

Responsibilities:
1. Periodic full scans of the PC's subnets for ZKTeco terminals (rate-limited).
2. Strong-identity reconciliation: match discoveries to known devices by
   serial number first, MAC+model as a *supporting* signal only.
3. Automatic IP-change repair *only after identity verification*.
4. New (unknown-serial) terminals appear as 'pending' — never auto-trusted.
5. Manual "Scan LAN Now" via ``request_scan()``.

Failure handling: a scan that throws is logged and retried after the normal
interval; per-host failures are recorded as observations and retried on the
next pass (natural exponential spacing comes from the scan interval growing
via SettingsView -> discovery_interval).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from ..devices.factory import create_adapter
from ..devices.exceptions import DeviceError
from . import arp as arp_mod
from . import identify as identify_mod
from . import interfaces as interfaces_mod
from . import ping as ping_mod
from . import ports as ports_mod

log = logging.getLogger("apexity_bridge.discovery")


@dataclass
class ScanReport:
    started_at: str
    finished_at: str | None = None
    subnets_scanned: list[str] = field(default_factory=list)
    hosts_probed: int = 0
    tcp_open: int = 0
    identified: list[dict] = field(default_factory=list)
    reidentified_registered: list[dict] = field(default_factory=list)
    ip_changes: list[dict] = field(default_factory=list)
    new_pending: list[dict] = field(default_factory=list)
    unidentified: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class DiscoveryService:
    def __init__(self, ctx):
        """ctx supplies: settings (SettingsView), repos, stop_event, config."""
        self.ctx = ctx
        self._manual_scan = threading.Event()
        self._scanning = threading.Lock()
        self.last_report: ScanReport | None = None
        self._thread: threading.Thread | None = None

        # injectables for tests ------------------------------------------------
        self._networks_fn = lambda: interfaces_mod.list_ipv4_networks(
            list(ctx.config.discovery_extra_subnets)
        )
        self._hosts_fn = interfaces_mod.probe_hosts
        self._tcp_probe_fn = ports_mod.probe_hosts_port
        self._arp_fn = arp_mod.read_arp_table
        self._ping_fn = ping_mod.ping
        self._identify_fn = identify_mod.identify_host
        self._adapter_factory = getattr(ctx, "adapter_factory", None) or create_adapter

    # ------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run_loop, name="discovery", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._manual_scan.set()

    def request_scan(self) -> None:
        """Dashboard 'Scan LAN Now'."""
        self._manual_scan.set()

    # ------------------------------------------------------------------
    def _run_loop(self) -> None:
        from ..timeutils import utcnow, parse_iso

        log.info("discovery service started (interval=%ss)", self.ctx.settings.discovery_interval)
        # Discover immediately at startup (requirement: reconnect & repair IP on boot).
        self.run_scan(blocking=False)
        while not self.ctx.stop_event.is_set():
            interval = max(60, int(self.ctx.settings.discovery_interval))
            if self._manual_scan.wait(timeout=interval):
                self._manual_scan.clear()
                if self.ctx.stop_event.is_set():
                    break
                self.run_scan(blocking=False)

    # ------------------------------------------------------------------
    def run_scan(self, *, blocking: bool = True, subnets: list[str] | None = None,
                 targets: list[str] | None = None,
                 read_attendance_probe: bool = False) -> ScanReport:
        """One full scan. Safe against concurrent invocation (single-flight)."""
        from ..timeutils import iso_utc

        if not self._scanning.acquire(blocking=False):
            report = ScanReport(started_at=iso_utc())
            report.errors.append("another scan is already running")
            return report

        report = ScanReport(started_at=iso_utc())
        try:
            self._scan_inner(report, subnets=subnets, targets=targets)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("LAN scan failed unexpectedly")
            report.errors.append(str(exc))
        finally:
            report.finished_at = iso_utc()
            self.last_report = report
            self._scanning.release()
        log.info(
            "scan done: probed=%d tcp-open=%d identified=%d ip-changes=%d new-pending=%d",
            report.hosts_probed, report.tcp_open, len(report.identified),
            len(report.ip_changes), len(report.new_pending),
        )
        return report if blocking else report

    # ------------------------------------------------------------------
    def _scan_inner(self, report: ScanReport, *, subnets=None, targets=None) -> None:
        cfg, repos = self.ctx.config, self.ctx.repos
        stop = self.ctx.stop_event

        # 1) candidate hosts --------------------------------------------------
        if targets:
            candidates = list(targets)
            networks = []
        else:
            networks = self._networks_fn() if subnets is None else subnets
            if networks and isinstance(networks[0], str):
                import ipaddress
                networks = [ipaddress.IPv4Network(n, strict=False) for n in networks]
            candidates = self._hosts_fn(networks)
        report.subnets_scanned = [str(n) for n in networks] if networks else ["(targets)"]
        report.hosts_probed = len(candidates)
        if not candidates:
            return

        # 2) optional ping pre-filter (aid only; TCP is authoritative) --------
        if self.ctx.config.discovery_use_ping:
            alive = [ip for ip in candidates if self._ping_fn(ip)]
            log.info("ping aid: %d/%d hosts answered", len(alive), len(candidates))
            candidates = alive or candidates  # never trust ping alone either way

        # 3) ARP table snapshot (MAC hints) ------------------------------------
        try:
            arp_table = self._arp_fn()
        except Exception as exc:
            log.warning("ARP table read failed: %s", exc)
            arp_table = {}

        known_by_ip = {d["ip_address"]: d for d in repos["devices"].list() if d["ip_address"]}

        # Seed the ARP presence of known device IPs so switch caches refresh.
        probe_targets = set(candidates) | set(known_by_ip) - set(candidates)
        open_hosts = self._tcp_probe_fn(
            sorted(probe_targets - set(candidates)) + candidates,
            ports_mod.ZK_PORT,
            timeout=min(1.0, self.ctx.config.device_timeout),
            concurrency=self.ctx.config.discovery_concurrency,
            stop_check=stop.is_set,
        )
        report.tcp_open = len(open_hosts)

        # extra configured ports (rare firmwares) ------------------------------
        for extra_port in self.ctx.config.discovery_ports:
            remaining = [ip for ip in candidates if ip not in open_hosts]
            open_hosts += [
                ip for ip in self._tcp_probe_fn(
                    remaining, extra_port, timeout=1.0,
                    concurrency=self.ctx.config.discovery_concurrency,
                    stop_check=stop.is_set,
                ) if ip not in open_hosts
            ]

        # refresh ARP after probing so fresh entries exist ----------------------
        try:
            arp_table = self._arp_fn() or arp_table
        except Exception:
            pass

        # 4) protocol identification of open hosts ------------------------------
        for ip in open_hosts:
            if stop.is_set():
                return
            mac = arp_table.get(ip)
            result = self._identify_fn(
                ip, ports_mod.ZK_PORT,
                timeout=self.ctx.config.device_timeout,
                comm_key=self._comm_key_for_ip(ip, known_by_ip),
                adapter_factory=self._adapter_factory,
            )
            if not result.identified or result.identity is None:
                obs_id = repos["observations"].add(
                    ip_address=ip, port=ports_mod.ZK_PORT, evidence="tcp_open",
                    mac_address=mac, error=result.error,
                )
                report.unidentified.append({"ip": ip, "mac": mac, "error": result.error})
                log.info("host %s open but not identified: %s", ip, result.error)
                continue
            self._reconcile(result, mac, report)

    # ------------------------------------------------------------------
    def _comm_key_for_ip(self, ip: str, known_by_ip: dict) -> int:
        device = known_by_ip.get(ip)
        if device is not None:
            return int(device.get("comm_key") or 0)
        return int(self.ctx.config.device_comm_key or 0)

    def _reconcile(self, result: identify_mod.Identification, mac: str | None,
                   report: ScanReport) -> None:
        """Match an identified terminal to the device registry."""
        repos = self.ctx.repos
        identity = result.identity
        serial = (identity.serial_number or "").strip()
        report.identified.append(
            {"ip": result.ip, "serial": serial, "model": identity.model, "mac": mac}
        )

        if serial:
            existing = repos["devices"].get_by_serial(serial)
            if existing:
                old_ip = existing["ip_address"]
                repos["devices"].upsert_identified(
                    serial_number=serial, model=identity.model, mac_address=mac,
                    firmware_version=identity.firmware_version,
                    platform=identity.platform, mark_seen=True,
                )
                if old_ip != result.ip:
                    # VERIFIED IP REPAIR: serial matched, so moving the IP is safe.
                    repos["devices"].update_verified_ip(existing["id"], result.ip, mac_address=mac)
                    change = {
                        "serial": serial, "label": existing.get("label"),
                        "old_ip": old_ip, "new_ip": result.ip,
                    }
                    report.ip_changes.append(change)
                    repos["audit"].log(
                        actor="system", action="device.ip_auto_repaired",
                        entity="device", entity_id=serial,
                        detail=f"IP changed {old_ip} → {result.ip} after serial verification",
                    )
                    log.info("device %s IP repaired %s → %s", serial, old_ip, result.ip)
                else:
                    report.reidentified_registered.append({"serial": serial, "ip": result.ip})
                repos["observations"].add(
                    ip_address=result.ip, port=result.port, evidence="protocol_identified",
                    mac_address=mac, model=identity.model, serial_number=serial,
                    matched_device_id=existing["id"],
                )
                return

            # unknown serial → pending (never auto-registered) -------------------
            device_id, created = repos["devices"].upsert_identified(
                serial_number=serial, model=identity.model, mac_address=mac,
                firmware_version=identity.firmware_version, platform=identity.platform,
                ip_address=result.ip, port=result.port, mark_seen=True,
            )
            # Serial is verified → updating the pending device's IP is safe.
            repos["devices"].update_verified_ip(device_id, result.ip, mac_address=mac)
            pending = {"serial": serial, "ip": result.ip, "model": identity.model, "created": created}
            report.new_pending.append(pending)
            repos["observations"].add(
                ip_address=result.ip, port=result.port, evidence="protocol_identified",
                mac_address=mac, model=identity.model, serial_number=serial,
                matched_device_id=device_id,
            )
            if created:
                repos["audit"].log(
                    actor="system", action="device.discovered_pending",
                    entity="device", entity_id=serial,
                    detail=f"new terminal at {result.ip} ({identity.model}) awaiting admin registration",
                )
            return

        # No serial → supporting signals only: never auto-register/repair.
        repos["observations"].add(
            ip_address=result.ip, port=result.port, evidence="tcp_open",
            mac_address=mac, model=identity.model if identity else None,
            error="device did not report a serial number",
        )
        report.unidentified.append({"ip": result.ip, "mac": mac, "error": "no serial number"})

    # -- fast targeted helpers used by the dashboard -------------------------
    def identify_one(self, ip: str, port: int = ports_mod.ZK_PORT,
                     comm_key: int | None = None) -> identify_mod.Identification:
        key = self.ctx.config.device_comm_key if comm_key is None else comm_key
        return self._identify_fn(
            ip, port, timeout=self.ctx.config.device_timeout,
            comm_key=key, adapter_factory=self._adapter_factory,
        )
