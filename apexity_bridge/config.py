"""Runtime configuration.

Precedence: environment variables / .env file  <  values an administrator
changed from the dashboard (stored in the ``application_settings`` table).

``Config`` is built once from the environment at startup; ``SettingsView``
layers DB-managed overrides on top for the handful of values that are
editable in the dashboard.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from . import paths


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _list(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


@dataclass(frozen=True)
class Config:
    # EMS
    ems_base_url: str = "https://apexityedu.com"
    ems_protocol: str = "auto"            # auto | iclock | rest
    ems_iclock_path: str = "/iclock"
    ems_allow_insecure_localhost: bool = False
    ems_timeout: int = 15

    # storage
    db_path: Path = Path("bridge.db")
    data_dir: Path = Path(".")

    # dashboard
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8321
    dashboard_allow_lan: bool = False

    # devices / discovery
    device_timezone: str = "Asia/Dhaka"
    discovery_extra_subnets: tuple[str, ...] = ()
    discovery_interval: int = 300
    discovery_concurrency: int = 32
    discovery_use_ping: bool = False
    discovery_ports: tuple[int, ...] = ()
    collect_interval: int = 30
    device_timeout: int = 10
    device_comm_key: int = 0

    # sync
    sync_retry_base: int = 30
    sync_retry_max: int = 1800
    roster_interval: int = 900
    sync_batch_size: int = 100

    log_level: str = "INFO"
    log_dir: Path | None = None

    extra: dict = field(default_factory=dict)

    KNOWN_PROTOCOLS = ("auto", "iclock", "rest")

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.ems_protocol not in self.KNOWN_PROTOCOLS:
            errors.append(f"EMS_PROTOCOL must be one of {self.KNOWN_PROTOCOLS}")
        if not self.ems_base_url.startswith(("https://", "http://")):
            errors.append("EMS_BASE_URL must be a full http(s) URL")
        if (
            self.ems_base_url.startswith("http://")
            and not (self.ems_allow_insecure_localhost and _is_localhost(self.ems_base_url))
            and not _is_localhost(self.ems_base_url)
        ):
            errors.append(
                "Refusing plaintext HTTP EMS endpoint outside localhost. "
                "Use HTTPS (EMS_BASE_URL=https://...) — the bridge never sends "
                "attendance data over the internet unencrypted."
            )
        if self.dashboard_allow_lan and self.dashboard_host in ("127.0.0.1", "localhost"):
            errors.append("DASHBOARD_ALLOW_LAN=1 requires DASHBOARD_HOST=0.0.0.0 (LAN access needs firewall review)")
        if not self.dashboard_allow_lan and self.dashboard_host == "0.0.0.0":
            errors.append(
                "Dashboard would bind to all interfaces without DASHBOARD_ALLOW_LAN=1. "
                "Refusing: the dashboard must never be exposed to the LAN by accident."
            )
        if self.sync_batch_size < 1:
            errors.append("SYNC_BATCH_SIZE must be >= 1")
        return errors


def _is_localhost(url: str) -> bool:
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def load_config(env_file: str | os.PathLike | None = None, environ: dict | None = None) -> Config:
    """Build a Config from a .env file (if present) + environment variables."""
    if env_file is not None:
        load_dotenv(env_file)
    else:
        default_env = Path.cwd() / ".env"
        if default_env.exists():
            load_dotenv(default_env)
    env = os.environ if environ is None else environ

    cfg = Config(
        ems_base_url=env.get("EMS_BASE_URL", "https://apexityedu.com").rstrip("/"),
        ems_protocol=env.get("EMS_PROTOCOL", "auto").strip().lower(),
        ems_iclock_path=env.get("EMS_ICLOCK_PATH", "/iclock") or "/iclock",
        ems_allow_insecure_localhost=_bool(env.get("EMS_ALLOW_INSECURE_LOCALHOST")),
        ems_timeout=_int(env.get("EMS_TIMEOUT"), 15),
        db_path=paths.default_db_path(),
        data_dir=paths.default_data_dir(),
        dashboard_host=env.get("DASHBOARD_HOST", "127.0.0.1"),
        dashboard_port=_int(env.get("DASHBOARD_PORT"), 8321),
        dashboard_allow_lan=_bool(env.get("DASHBOARD_ALLOW_LAN")),
        device_timezone=env.get("DEVICE_TIMEZONE", "Asia/Dhaka"),
        discovery_extra_subnets=tuple(_list(env.get("DISCOVERY_EXTRA_SUBNETS"))),
        discovery_interval=_int(env.get("DISCOVERY_INTERVAL"), 300),
        discovery_concurrency=max(1, _int(env.get("DISCOVERY_CONCURRENCY"), 32)),
        discovery_use_ping=_bool(env.get("DISCOVERY_USE_PING")),
        discovery_ports=tuple(int(p) for p in _list(env.get("DISCOVERY_PORTS"))),
        collect_interval=max(5, _int(env.get("COLLECT_INTERVAL"), 30)),
        device_timeout=max(2, _int(env.get("DEVICE_TIMEOUT"), 10)),
        device_comm_key=_int(env.get("DEVICE_COMM_KEY"), 0),
        sync_retry_base=max(5, _int(env.get("SYNC_RETRY_BASE"), 30)),
        sync_retry_max=max(60, _int(env.get("SYNC_RETRY_MAX"), 1800)),
        roster_interval=max(60, _int(env.get("ROSTER_INTERVAL"), 900)),
        sync_batch_size=max(1, _int(env.get("SYNC_BATCH_SIZE"), 100)),
        log_level=env.get("LOG_LEVEL", "INFO").upper(),
        log_dir=paths.default_log_dir(),
    )
    return cfg


# Keys an administrator may edit from the dashboard (stored in the DB, winning
# over the environment). Anything secret or security-critical stays env-only.
DB_MANAGED_KEYS = {
    "scan_interval": ("discovery_interval", int),
    "sync_interval": ("sync_retry_base", int),
    "collect_interval": ("collect_interval", int),
    "roster_interval": ("roster_interval", int),
    "ems_base_url": ("ems_base_url", str),
    "ems_protocol": ("ems_protocol", str),
    "device_timezone": ("device_timezone", str),
}


class SettingsView:
    """Read-through view: DB override -> Config attribute."""

    def __init__(self, config: Config, settings_repo):
        self._config = config
        self._repo = settings_repo

    def get(self, name: str):
        for db_key, (attr, caster) in DB_MANAGED_KEYS.items():
            if attr == name:
                raw = self._repo.get(db_key)
                if raw is not None and raw != "":
                    try:
                        return caster(raw)
                    except (TypeError, ValueError):
                        return getattr(self._config, attr)
                return getattr(self._config, attr)
        return getattr(self._config, name)

    # convenience accessors used across the codebase -----------------------
    @property
    def discovery_interval(self) -> int: return int(self.get("discovery_interval"))
    @property
    def sync_retry_base(self) -> int: return int(self.get("sync_retry_base"))
    @property
    def collect_interval(self) -> int: return int(self.get("collect_interval"))
    @property
    def roster_interval(self) -> int: return int(self.get("roster_interval"))
    @property
    def ems_base_url(self) -> str: return str(self.get("ems_base_url"))
    @property
    def ems_protocol(self) -> str: return str(self.get("ems_protocol"))
    @property
    def device_timezone(self) -> str: return str(self.get("device_timezone"))
