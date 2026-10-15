"""Bridge service orchestrator — ties discovery, collectors, sync, dashboard.

Startup order guarantees:
1. Database migrated + integrity-checked (+ optional staged restore applied).
2. Discovery begins immediately (boot-time IP repair & reconnect).
3. Collectors reconnect to registered devices right away.
4. Sync worker resumes unfinished queued work from previous runs.
Everything stops cleanly on SIGINT/SIGTERM/service-stop.
"""
from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .attendance.collector import AttendanceCollector
from .config import Config, SettingsView, load_config
from .database.repositories import open_migrated_database
from .devices.factory import create_adapter
from .discovery.service import DiscoveryService
from .logging_utils import setup_logging
from .sync.worker import EmsStatus, SyncWorker

log = logging.getLogger("apexity_bridge.service")


@dataclass
class BridgeContext:
    version: str
    config: Config
    settings: SettingsView
    repos: dict
    secret_store: object | None
    ems_status: EmsStatus
    stop_event: threading.Event
    started_at: float = field(default_factory=time.monotonic)
    adapter_factory: object = create_adapter
    discovery: DiscoveryService | None = None
    collector: AttendanceCollector | None = None
    sync_worker: SyncWorker | None = None
    dashboard: object | None = None

    # -- helpers used by dashboard/routes -------------------------------------
    def uptime_seconds(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def ems_client(self):
        return self.sync_worker._client if self.sync_worker else None

    def log_file(self) -> Path | None:
        if self.config.log_dir:
            return Path(self.config.log_dir) / "bridge.log"
        return None

    def apply_settings_overrides(self) -> None:
        """Called after the dashboard edits settings."""
        if self.sync_worker:
            self.sync_worker.rebuild_client()
            self.sync_worker.wake()
        if self.discovery:
            self.discovery.request_scan()


class BridgeService:
    def __init__(self, config: Config | None = None, *, adapter_factory=None,
                 start_dashboard: bool = True):
        self.config = config or load_config()
        errors = self.config.validate()
        if errors:
            raise ValueError("invalid configuration:\n- " + "\n- ".join(errors))
        self.start_dashboard = start_dashboard
        self._adapter_factory = adapter_factory or create_adapter
        self.ctx: BridgeContext | None = None
        self._stopping = threading.Event()

    # ------------------------------------------------------------------ setup --
    def _apply_staged_restore(self) -> None:
        """If a restore was staged (data_dir/RESTORE_REQUESTED.db), apply it now:
        current DB is moved aside (kept) and the staged file becomes bridge.db."""
        staged = self.config.data_dir / "RESTORE_REQUESTED.db"
        if not staged.exists():
            return
        from .paths import backup_dir
        import shutil
        target = self.config.db_path
        keep = backup_dir() / f"bridge-pre-restore-{time.strftime('%Y%m%d-%H%M%S')}.db"
        if target.exists():
            shutil.move(str(target), str(keep))
        shutil.move(str(staged), str(target))
        log.warning("staged restore applied; previous database kept at %s", keep)

    def setup(self) -> BridgeContext:
        from .security.secrets import SecretStore

        self._apply_staged_restore()

        db, repos = open_migrated_database(self.config.db_path)
        if not db.check_integrity():
            db.close_all()
            from .database.connection import Database
            Database.quarantine_corrupt(self.config.db_path)
            db, repos = open_migrated_database(self.config.db_path)

        settings = SettingsView(self.config, repos["settings"])
        secret_store = SecretStore(self.config.data_dir)

        ctx = BridgeContext(
            version=__version__, config=self.config, settings=settings,
            repos=repos, secret_store=secret_store, ems_status=EmsStatus(),
            stop_event=threading.Event(), adapter_factory=self._adapter_factory,
        )
        ctx.sync_worker = SyncWorker(ctx)
        ctx.discovery = DiscoveryService(ctx)
        ctx.discovery._adapter_factory = self._adapter_factory
        ctx.collector = AttendanceCollector(ctx)
        ctx.collector._adapter_factory = self._adapter_factory
        self.ctx = ctx
        return ctx

    # ------------------------------------------------------------------- run --
    def start(self) -> BridgeContext:
        ctx = self.ctx or self.setup()
        ctx.sync_worker.start()
        ctx.collector.start()
        ctx.discovery.start()
        if self.start_dashboard:
            from .dashboard.app import DashboardServer
            ctx.dashboard = DashboardServer(ctx)
            ctx.dashboard.start()
            log.info("dashboard: %s", ctx.dashboard.url)
        ctx.repos["audit"].log(actor="system", action="service.started",
                               detail=f"v{ctx.version}, db={self.config.db_path}")
        log.info("ApexityEdu Attendance Bridge v%s started", ctx.version)
        return ctx

    def stop(self) -> None:
        if self._stopping.is_set() or not self.ctx:
            return
        self._stopping.set()
        ctx = self.ctx
        log.info("stopping bridge…")
        ctx.stop_event.set()
        for component in (ctx.discovery, ctx.collector, ctx.sync_worker, ctx.dashboard):
            if component is not None:
                try:
                    component.stop()
                except Exception:
                    log.debug("component stop raised", exc_info=True)
        ctx.repos["audit"].log(actor="system", action="service.stopped",
                               detail=f"uptime={int(ctx.uptime_seconds())}s")
        ctx.repos["db"].close_all()
        log.info("bridge stopped")

    def run_forever(self) -> None:
        ctx = self.start()
        self._install_signal_handlers()
        try:
            while not ctx.stop_event.is_set():
                time.sleep(0.5)
        finally:
            self.stop()

    def _install_signal_handlers(self) -> None:
        def handle(signum, _frame):
            log.info("received signal %s — shutting down", signum)
            if self.ctx:
                self.ctx.stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handle)
            except (ValueError, OSError):  # pragma: no cover - not on main thread
                pass
