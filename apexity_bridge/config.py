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
    # "auto" = use the REST /people endpoint when iClock is the push protocol
    # (iClock has no roster download of its own). off = never try.
    ems_roster_mode: str = "auto"
    # Publish each terminal's user table (PIN + name) so the EMS can map a
    # punch to a real person. Never includes templates or passwords.
    ems_push_users: bool = True
    # Answer read-only ADMS commands the EMS sends through getrequest
    # (DATA QUERY USERINFO / ATTLOG). Destructive commands are never executed.
    ems_execute_commands: bool = True
    # Seconds between device user-table reads (and EMS re-publishes).
    users_sync_interval: int = 900

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


def find_env_file() -> Path | None:
    """Locate .env for a source checkout or a frozen Windows install.

    Search order:
      1. BRIDGE_ENV_FILE (explicit override)
      2. %ProgramData%\\ApexityEduBridge\\.env  (installer-managed)
      3. next to ApexityEduBridge.exe
      4. current working directory
    """
    override = os.environ.get("BRIDGE_ENV_FILE", "").strip()
    if override:
        path = Path(override).expanduser()
        return path if path.exists() else None
    candidates = [
        paths.default_data_dir() / ".env",
        paths.app_dir() / ".env",
        Path.cwd() / ".env",
    ]
    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate
        except OSError:
            continue
    return None


def load_config(env_file: str | os.PathLike | None = None, environ: dict | None = None) -> Config:
    """Build a Config from a .env file (if present) + environment variables."""
    if env_file is not None:
        load_dotenv(env_file)
    else:
        found = find_env_file()
        if found is not None:
            load_dotenv(found)
    env = os.environ if environ is None else environ

    cfg = Config(
        ems_base_url=env.get("EMS_BASE_URL", "https://apexityedu.com").rstrip("/"),
        ems_protocol=env.get("EMS_PROTOCOL", "auto").strip().lower(),
        ems_iclock_path=env.get("EMS_ICLOCK_PATH", "/iclock") or "/iclock",
        ems_allow_insecure_localhost=_bool(env.get("EMS_ALLOW_INSECURE_LOCALHOST")),
        ems_timeout=_int(env.get("EMS_TIMEOUT"), 15),
        ems_roster_mode=(env.get("EMS_ROSTER_MODE", "auto").strip().lower() or "auto"),
        ems_push_users=_bool(env.get("EMS_PUSH_USERS"), True),
        ems_execute_commands=_bool(env.get("EMS_EXECUTE_COMMANDS"), True),
        users_sync_interval=max(60, _int(env.get("USERS_SYNC_INTERVAL"), 900)),
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
def _as_bool(value) -> bool:
    """bool() would turn the string "0" into True — settings are strings."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


DB_MANAGED_KEYS = {
    "scan_interval": ("discovery_interval", int),
    "sync_interval": ("sync_retry_base", int),
    "collect_interval": ("collect_interval", int),
    "roster_interval": ("roster_interval", int),
    "ems_base_url": ("ems_base_url", str),
    "ems_protocol": ("ems_protocol", str),
    "device_timezone": ("device_timezone", str),
    "ems_push_users": ("ems_push_users", _as_bool),
    "ems_execute_commands": ("ems_execute_commands", _as_bool),
    "users_sync_interval": ("users_sync_interval", int),
}

# Guard rails applied by every writer (dashboard API and desktop façade).  An
# invalid value used to be stored verbatim, which then crashed whichever
# component read it; rejecting it at the edge keeps a typo in one web form from
# taking the whole sync worker offline.
DB_MANAGED_MINIMUMS = {
    "scan_interval": 60,
    "sync_interval": 5,
    "collect_interval": 5,
    "roster_interval": 60,
    "users_sync_interval": 60,
}


def normalise_db_setting(key: str, raw) -> tuple[str, str | None]:
    """Return (stored_value, error).

    ``""`` means "remove the override and fall back to .env".  Empty is legal
    for every key — including ``ems_protocol``, whose form option is literally
    "(from environment)".  That used to be rejected as an invalid protocol,
    which made every Save on the Settings page fail.
    """
    if key not in DB_MANAGED_KEYS:
        return "", f"unknown setting {key!r}"
    _attr, caster = DB_MANAGED_KEYS[key]
    value = "" if raw is None else str(raw).strip()[:300]
    if value in ("", "None"):
        return "", None
    if key == "ems_protocol":
        if value not in ("auto", "iclock", "rest"):
            return "", "EMS protocol must be auto, iclock or rest (or empty for .env)"
        return value, None
    if key == "ems_base_url":
        if not value.startswith(("https://", "http://")):
            return "", "EMS base URL must start with https:// (or http:// on localhost)"
        if value.startswith("http://") and not _is_localhost(value):
            return "", ("plaintext HTTP is only allowed for localhost; use https:// "
                        "for a remote EMS")
        return value, None
    if caster is _as_bool:
        lowered = value.lower()
        if lowered in {"1", "true", "yes", "on"}:
            return "1", None
        if lowered in {"0", "false", "no", "off"}:
            return "0", None
        return "", f"{key} must be 1 or 0"
    if caster is int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return "", f"{key} must be a whole number"
        minimum = DB_MANAGED_MINIMUMS.get(key)
        if minimum is not None and number < minimum:
            return "", f"{key} must be at least {minimum}"
        return str(number), None
    return value, None


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
    @property
    def ems_push_users(self) -> bool: return bool(self.get("ems_push_users"))
    @property
    def ems_execute_commands(self) -> bool: return bool(self.get("ems_execute_commands"))
    @property
    def users_sync_interval(self) -> int: return int(self.get("users_sync_interval"))
