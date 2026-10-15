"""Rate-limited TCP port probing (port 4370 = ZKTeco ZK protocol by default)."""
from __future__ import annotations

import logging
import socket
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("apexity_bridge.discovery.ports")

ZK_PORT = 4370


def check_tcp_open(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def probe_hosts_port(ips: list[str], port: int, *, timeout: float = 0.6,
                     concurrency: int = 32, stop_check=None) -> list[str]:
    """Return hosts where ``port`` accepts TCP, with bounded concurrency.

    The thread pool size *is* the rate limiter; on a /24 this issues roughly
    ``concurrency`` sockets at a time, well within polite LAN behaviour.
    """
    open_hosts: list[str] = []
    if not ips:
        return open_hosts
    with ThreadPoolExecutor(max_workers=max(1, concurrency), thread_name_prefix="tcp-probe") as pool:
        futures = {ip: pool.submit(check_tcp_open, ip, port, timeout) for ip in ips}
        for ip, fut in futures.items():
            if stop_check and stop_check():
                break
            try:
                if fut.result(timeout=timeout + 2):
                    open_hosts.append(ip)
            except Exception as exc:
                log.debug("probe %s:%s failed: %s", ip, port, exc)
    log.info("TCP probe %d hosts port %s → %d open", len(ips), port, len(open_hosts))
    return sorted(open_hosts, key=lambda s: tuple(int(p) for p in s.split(".")))
