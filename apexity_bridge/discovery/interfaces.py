"""Enumerate local IPv4 interfaces/subnets for LAN discovery."""
from __future__ import annotations

import ipaddress
import logging
import socket

log = logging.getLogger("apexity_bridge.discovery.interfaces")

MAX_NETWORK_SIZE = 4096  # refuse to scan networks larger than this (rate limiting / safety)


def list_ipv4_networks(extra_subnets: list[str] | tuple[str, ...] = (),
                       max_size: int = MAX_NETWORK_SIZE) -> list[ipaddress.IPv4Network]:
    """Subnets directly attached to this PC + administrator-provided extras.

    Loopback, link-local and /32 interfaces are excluded.
    """
    networks: dict[str, ipaddress.IPv4Network] = {}
    try:
        import psutil

        for iface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family != socket.AF_INET:
                    continue
                ip, netmask = addr.address, addr.netmask
                if not ip or ip.startswith("127.") or ip.startswith("169.254."):
                    continue
                try:
                    prefix = ipaddress.IPv4Network(f"0.0.0.0/{netmask}").prefixlen
                    net = ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)
                except (ipaddress.AddressValueError, ValueError):
                    log.debug("skipping interface %s addr %s/%s", iface, ip, netmask)
                    continue
                if net.num_addresses > max_size:
                    log.warning(
                        "interface %s network %s too large to auto-scan (> %d hosts) — "
                        "add a narrower DISCOVERY_EXTRA_SUBNETS CIDR if needed",
                        iface, net, max_size,
                    )
                    continue
                networks[str(net)] = net
    except ImportError:  # pragma: no cover
        log.warning("psutil not available; only configured subnets will be scanned")

    for cidr in extra_subnets or ():
        try:
            net = ipaddress.IPv4Network(cidr.strip(), strict=False)
        except ValueError:
            log.warning("ignoring invalid DISCOVERY_EXTRA_SUBNETS entry %r", cidr)
            continue
        if net.num_addresses > max_size:
            log.warning("extra subnet %s too large (> %d hosts) — skipped", net, max_size)
            continue
        networks[str(net)] = net

    return sorted(networks.values(), key=lambda n: int(n.network_address))


def probe_hosts(networks: list[ipaddress.IPv4Network]) -> list[str]:
    """The list of usable host IPs across all networks (deduplicated)."""
    ips: set[str] = set()
    for net in networks:
        for ip in net.hosts():
            ips.add(str(ip))
        if net.num_addresses <= 2:  # /31, /32 edge cases
            ips.add(str(net.network_address))
    return sorted(ips, key=lambda s: tuple(int(p) for p in s.split(".")))
