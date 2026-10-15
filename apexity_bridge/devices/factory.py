"""Adapter factory — selected per device row.

``driver`` currently supports 'zk' (real hardware via pyzk). The interface is
deliberately pluggable: the official ZKEM SDK adapter or another vendor SDK
can be added here without touching collectors/sync.
"""
from __future__ import annotations

from .base import DeviceAdapter
from .zk_adapter import ZkDeviceAdapter


def create_adapter(driver: str = "zk", **kwargs) -> DeviceAdapter:
    if driver == "zk":
        return ZkDeviceAdapter(
            host=kwargs["host"],
            port=kwargs.get("port", ZkDeviceAdapter.DEFAULT_PORT),
            comm_key=kwargs.get("comm_key", 0),
            timeout=kwargs.get("timeout", 10),
            force_udp=kwargs.get("force_udp", False),
            ommit_ping=kwargs.get("ommit_ping", True),
        )
    raise ValueError(f"unknown device driver: {driver!r}")
