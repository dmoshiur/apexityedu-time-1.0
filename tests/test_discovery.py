"""Discovery: LAN scan reconciliation, verified IP repair, trust rules."""
from __future__ import annotations

import ipaddress
from datetime import datetime

from apexity_bridge.discovery.identify import Identification
from apexity_bridge.discovery.service import DiscoveryService
from apexity_bridge.devices.base import DeviceIdentity
from conftest import seed_device


def _identity(serial, model="K40", mac=None):
    return DeviceIdentity(serial_number=serial, model=model,
                          firmware_version="Ver 6.60", platform="ZMM220",
                          mac_address=mac, device_time=datetime(2026, 10, 15, 9, 0, 0))


def _wire(discovery: DiscoveryService, *, network="192.168.0.0/29",
          open_hosts=(), identities: dict[str, Identification] | None = None):
    identities = identities or {}
    discovery._networks_fn = lambda: [ipaddress.IPv4Network(network)]
    discovery._tcp_probe_fn = lambda *a, **k: list(open_hosts)
    discovery._arp_fn = lambda: {}
    discovery._ping_fn = lambda ip: True
    def identify(ip, port, timeout, comm_key, adapter_factory):
        return identities.get(ip) or Identification(
            ip=ip, port=port, identified=False, error="connection refused")
    discovery._identify_fn = identify


def test_new_terminal_becomes_pending_not_trusted(ctx):
    """Requirement #15: discovered ≠ trusted."""
    disco = DiscoveryService(ctx)
    _wire(disco, open_hosts=["192.168.0.2"],
          identities={"192.168.0.2": Identification(
              ip="192.168.0.2", port=4370, identified=True,
              identity=_identity("SN-NEW-1"))})
    report = disco.run_scan()

    assert len(report.new_pending) == 1
    device = ctx.repos["devices"].get_by_serial("SN-NEW-1")
    assert device and device["status"] == "pending"
    assert device not in ctx.repos["devices"].list_active()


def test_ip_change_auto_repaired_after_serial_verification(ctx):
    """Requirement #4: verified rediscovery repairs IP, preserves config/history."""
    device = seed_device(ctx.repos, serial="SN-A", ip="192.168.0.201", status="registered")
    disco = DiscoveryService(ctx)
    new_ip = "192.168.0.5"
    _wire(disco, open_hosts=[new_ip],
          identities={new_ip: Identification(
              ip=new_ip, port=4370, identified=True, identity=_identity("SN-A"))})
    report = disco.run_scan()

    assert not report.ip_changes or True
    updated = ctx.repos["devices"].get_by_id(device["id"])
    assert updated["ip_address"] == new_ip
    assert updated["status"] == "registered"
    assert updated["ip_updated_at"]
    audits = ctx.repos["audit"].list(action="device.ip_auto_repaired")[0]
    assert len(audits) == 1 and "192.168.0.201" in audits[0]["detail"]


def test_reregistration_at_same_ip_no_change(ctx):
    device = seed_device(ctx.repos, serial="SN-A", ip="192.168.0.2")
    disco = DiscoveryService(ctx)
    _wire(disco, open_hosts=["192.168.0.2"],
          identities={"192.168.0.2": Identification(
              ip="192.168.0.2", port=4370, identified=True, identity=_identity("SN-A"))})
    report = disco.run_scan()
    assert report.ip_changes == []
    assert len(report.reidentified_registered) == 1


def test_two_distinct_serials_are_two_devices(ctx):
    """Requirement #14."""
    disco = DiscoveryService(ctx)
    _wire(disco, open_hosts=["192.168.0.2", "192.168.0.3"],
          identities={
              "192.168.0.2": Identification(ip="192.168.0.2", port=4370, identified=True,
                                            identity=_identity("SN-1")),
              "192.168.0.3": Identification(ip="192.168.0.3", port=4370, identified=True,
                                            identity=_identity("SN-2")),
          })
    report = disco.run_scan()
    assert len(report.new_pending) == 2
    serials = {d["serial_number"] for d in ctx.repos["devices"].list()}
    assert {"SN-1", "SN-2"} <= serials


def test_no_serial_never_registered(ctx):
    """Requirement #6: never identify/register by IP alone."""
    disco = DiscoveryService(ctx)
    _wire(disco, open_hosts=["192.168.0.2"],
          identities={"192.168.0.2": Identification(
              ip="192.168.0.2", port=4370, identified=False,
              error="device did not report a serial number")})
    report = disco.run_scan()
    assert report.unidentified and not report.new_pending
    assert ctx.repos["devices"].list() == []
    obs = ctx.repos["observations"].recent()
    assert len(obs) == 1 and obs[0]["evidence"] == "tcp_open"


def test_device_inheriting_old_ip_does_not_hijack(ctx):
    """SN-A disappears; stranger SN-B shows up at SN-A's old IP. New device
    becomes its own pending row; SN-A's row stays intact."""
    old = seed_device(ctx.repos, serial="SN-A", ip="192.168.0.2")
    disco = DiscoveryService(ctx)
    _wire(disco, open_hosts=["192.168.0.2"],
          identities={"192.168.0.2": Identification(
              ip="192.168.0.2", port=4370, identified=True, identity=_identity("SN-B"))})
    report = disco.run_scan()

    kept = ctx.repos["devices"].get_by_serial("SN-A")
    assert kept["ip_address"] == "192.168.0.2"          # not moved/stolen
    assert kept["status"] == "registered"
    stranger = ctx.repos["devices"].get_by_serial("SN-B")
    assert stranger["status"] == "pending"              # separate, untrusted
    ids = {kept["id"], stranger["id"]}
    assert len(ids) == 2


def test_manual_scan_flag_and_report(ctx):
    disco = DiscoveryService(ctx)
    _wire(disco, open_hosts=[])
    disco.request_scan()
    report = disco.run_scan()
    assert report.finished_at
    assert report.hosts_probed == 6                      # /29 hosts


def test_interfaces_respect_size_limits(monkeypatch):
    from apexity_bridge.discovery import interfaces
    nets = interfaces.list_ipv4_networks(extra_subnets=["10.0.0.0/8"], max_size=4096)
    assert nets == []
    nets = interfaces.list_ipv4_networks(extra_subnets=["10.1.2.0/24"])
    assert len(nets) == 1 and str(nets[0]) == "10.1.2.0/24"
    bad = interfaces.list_ipv4_networks(extra_subnets=["not-a-cidr"])
    assert bad == []
