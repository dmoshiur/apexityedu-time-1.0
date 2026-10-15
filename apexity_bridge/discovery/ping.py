"""Optional ICMP echo discovery aid.

The ZK protocol itself is the authoritative check — ping is only used (when
enabled) to skip obviously-dead addresses faster on large subnets. Devices
that block ICMP are still found because TCP probing runs regardless.
"""
from __future__ import annotations

import subprocess
import sys


def ping(ip: str, timeout_ms: int = 400) -> bool:
    if sys.platform.startswith("win"):
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), ip]
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout_ms / 500 + 1
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
