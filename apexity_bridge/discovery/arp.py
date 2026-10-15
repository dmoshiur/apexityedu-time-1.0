"""ARP / neighbour-table inspection.

Cross-platform and read-only. MAC addresses from ARP are a *hint only* —
they don't exist across routed networks and can be spoofed, so device
identity is never decided from them alone.
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

log = logging.getLogger("apexity_bridge.discovery.arp")

_MAC_RE = re.compile(r"([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}")
_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def _normalise_mac(mac: str | None) -> str | None:
    if not mac:
        return None
    mac = mac.upper().replace("-", ":")
    return mac if _MAC_RE.fullmatch(mac) else None


def _run(cmd: list[str]) -> str:
    import subprocess

    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return out.stdout or ""
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("arp command %s failed: %s", cmd, exc)
        return ""


def read_arp_table() -> dict[str, str]:
    """Return {ip: mac} from the OS neighbour table (best effort)."""
    table: dict[str, str] = {}
    if sys.platform.startswith("win"):
        for line in _run(["arp", "-a"]).splitlines():
            ips = _IP_RE.findall(line)
            macs = _MAC_RE.findall(line)
            if ips and macs:
                mac_line = _MAC_RE.search(line)
                if mac_line:
                    table.setdefault(ips[0], _normalise_mac(mac_line.group(0)) or "")
    else:
        out = _run(["ip", "neigh", "show"])
        if out:
            for line in out.splitlines():
                parts = line.split()
                if "lladdr" in parts:
                    ip = parts[0]
                    mac = parts[parts.index("lladdr") + 1]
                    table[ip] = _normalise_mac(mac) or ""
        else:
            proc = Path("/proc/net/arp")
            if proc.exists():
                for line in proc.read_text().splitlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 4 and parts[2] != "0x0":
                        mac = _normalise_mac(parts[3])
                        if mac:
                            table[parts[0]] = mac
    return {ip: mac for ip, mac in table.items() if mac}
