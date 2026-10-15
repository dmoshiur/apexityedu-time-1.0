"""Optional native Windows Service wrapper (pywin32).

The recommended auto-start mechanism is Task Scheduler (scripts/install),
but for environments that prefer a real service this class is drop-in:

    python -m apexity_bridge.windows_service.service install
    python -m apexity_bridge.windows_service.service start

Crash recovery is configured at registration time (SC_FAILURE_ACTIONS via
``win32serviceutil`` + ``ChangeServiceConfig2`` in install.ps1).
"""
from __future__ import annotations

import sys

SERVICE_NAME = "ApexityEduBridge"
SERVICE_DISPLAY = "ApexityEdu Smart Attendance Bridge"
SERVICE_DESC = (
    "Collects ZKTeco K40 attendance on the LAN and synchronises it with the "
    "ApexityEdu EMS (offline-first, automatic IP repair)."
)

if sys.platform.startswith("win"):
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil
    import win32api
    import winerror

    class ApexityBridgeService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY
        _svc_description_ = SERVICE_DESC

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self._service = None

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)
            if self._service and self._service.ctx:
                self._service.ctx.stop_event.set()

        def SvcDoRun(self):
            from ..config import load_config
            from ..logging_utils import setup_logging
            from ..service import BridgeService
            from .singleton import SingleInstance

            cfg = load_config()
            logger = setup_logging(cfg.log_level, cfg.log_dir)
            servicemanager.LogInfoMsg("ApexityEdu Bridge service starting")
            try:
                with SingleInstance():
                    self._service = BridgeService(cfg)
                    ctx = self._service.start()
                    while not ctx.stop_event.is_set():
                        if win32event.WaitForSingleObject(self.stop_event, 500) == win32event.WAIT_OBJECT_0:
                            ctx.stop_event.set()
                    self._service.stop()
            except RuntimeError as exc:
                servicemanager.LogErrorMsg(f"Bridge service aborted: {exc}")
                logger.error("service aborted: %s", exc)
            except Exception as exc:  # pragma: no cover
                servicemanager.LogErrorMsg(f"Bridge service crashed: {exc}")
                logger.exception("service crashed")
                raise

    def main():
        win32serviceutil.HandleCommandLine(ApexityBridgeService)

else:
    def main():  # pragma: no cover - Windows only
        print("The native service wrapper is only available on Windows. "
              "Use 'python -m apexity_bridge run' here.")


if __name__ == "__main__":
    main()
