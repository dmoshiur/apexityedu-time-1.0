"""Protocol-level identification of a candidate host.

Only the device's own protocol responses count as identification. A TCP-open
port proves nothing by itself (requirement: never identify by IP alone).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..devices.base import DeviceIdentity
from ..devices.exceptions import DeviceError

log = logging.getLogger("apexity_bridge.discovery.identify")


@dataclass
class Identification:
    ip: str
    port: int
    identified: bool
    identity: DeviceIdentity | None = None
    error: str | None = None


def identify_host(ip: str, port: int, *, timeout: int, comm_key: int,
                  adapter_factory) -> Identification:
    try:
        adapter = adapter_factory(
            host=ip, port=port, timeout=timeout, comm_key=comm_key,
        )
        adapter.connect()
    except DeviceError as exc:
        return Identification(ip=ip, port=port, identified=False, error=str(exc))
    try:
        identity = adapter.get_identity()
    except DeviceError as exc:
        return Identification(ip=ip, port=port, identified=False, error=str(exc))
    finally:
        try:
            adapter.disconnect()
        except Exception:
            pass
    return Identification(ip=ip, port=port, identified=True, identity=identity)
