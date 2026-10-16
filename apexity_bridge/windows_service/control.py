"""Windows auto-start management via Task Scheduler (no pywin32 required).

* install   → register task "ApexityEduBridge" that starts at logon,
              restarts on failure, runs hidden, single instance guarded
              additionally by our named mutex.
* uninstall → remove the task (data preserved).
* start / stop / restart / status → schtasks.exe control + liveness probe.

A native pywin32 Windows Service also exists (windows_service/service.py);
Task Scheduler is the default because it needs no extra packages and shows
up in the standard "Task Scheduler" UI admins know.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

log = logging.getLogger("apexity_bridge.windows_service.control")

TASK_NAME = "ApexityEduBridge"


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def launch_command() -> tuple[str, str, str]:
    """(executable, arguments, working_directory) for auto-start.

    Frozen installs run ApexityEduBridge.exe (tray, hidden). Source
    checkouts keep the historical ``pythonw -m apexity_bridge run``.
    """
    if getattr(sys, "frozen", False):
        exe = str(Path(sys.executable).resolve())
        workdir = str(Path(exe).parent)
        return exe, "tray --hidden", workdir
    python_exe = str(Path(sys.executable))
    workdir = str(Path.cwd())
    return python_exe, "-m apexity_bridge run", workdir


def task_xml(*, python_exe: str, workdir: str, logon_user: str | None = None,
             arguments: str | None = None) -> str:
    """Task: at logon + restart-on-failure (1 min), unlimited restarts,
    stop-if-already-running ignored (our mutex prevents duplicates anyway),
    run whether or not the user is logged on only when SYSTEM is used —
    default is interactive user so ProgramData paths stay per-user."""

    args = arguments if arguments is not None else "-m apexity_bridge run"
    user_block = (
        f"<UserId>{escape(logon_user)}</UserId><LogonType>InteractiveToken</LogonType>"
        if logon_user else "")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <URI>\\{TASK_NAME}</URI>
    <Description>ApexityEdu Smart Attendance Bridge — ZKTeco K40 LAN attendance bridge with offline-first EMS sync.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
    <BootTrigger>
      <Enabled>true</Enabled>
    </BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      {user_block}
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>false</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(python_exe)}</Command>
      <Arguments>{escape(args)}</Arguments>
      <WorkingDirectory>{escape(workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _run(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def service_action(action: str, config) -> int:
    if not _is_windows():
        print("The 'service' command manages Windows auto-start (Task Scheduler).")
        print("On this platform, run the bridge directly:  python -m apexity_bridge run")
        print("(Linux/macOS deployments may use systemd/supervisord instead.)")
        return 1

    handlers = {
        "install": _install, "uninstall": _uninstall, "start": _start,
        "stop": _stop, "restart": _restart, "status": _status,
    }
    return handlers[action](config)


def _install(config) -> int:
    python_exe, arguments, workdir = launch_command()
    xml = task_xml(python_exe=python_exe, workdir=workdir, arguments=arguments)
    xml_path = config.data_dir / "ApexityEduBridge-task.xml"
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(xml, encoding="utf-16")
    result = _run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"])
    if result.returncode != 0:
        print(f"task registration failed:\n{result.stdout}{result.stderr}")
        return 1
    print(f"Task '{TASK_NAME}' registered (auto-start at logon/boot, restart-on-failure).")
    print("Start it now with:  python -m apexity_bridge service start")
    return 0


def _uninstall(config) -> int:
    _run(["schtasks", "/End", "/TN", TASK_NAME])
    result = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    if result.returncode != 0:
        print(f"task removal failed: {result.stdout}{result.stderr}")
        return 1
    print(f"Task '{TASK_NAME}' removed. Data at {config.data_dir} was PRESERVED.")
    return 0


def _start(config) -> int:
    result = _run(["schtasks", "/Run", "/TN", TASK_NAME])
    print(result.stdout.strip() or result.stderr.strip())
    return result.returncode


def _stop(config) -> int:
    result = _run(["schtasks", "/End", "/TN", TASK_NAME])
    print(result.stdout.strip() or result.stderr.strip())
    return result.returncode


def _restart(config) -> int:
    _stop(config)
    return _start(config)


def _status(config) -> int:
    result = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"])
    if result.returncode != 0:
        print(f"Task '{TASK_NAME}' is NOT installed.")
        return 1
    for line in result.stdout.splitlines():
        if any(k in line for k in ("TaskName", "Status", "Last Run", "Next Run",
                                   "Task To Run", "Logon Mode")):
            print(line.strip())
    # liveness probe of the local dashboard
    try:
        import urllib.request
        url = f"http://{config.dashboard_host}:{config.dashboard_port}/login"
        with urllib.request.urlopen(url, timeout=3) as resp:
            print(f"dashboard: responding (HTTP {resp.status}) at {url}")
    except Exception as exc:
        print(f"dashboard: not responding at {config.dashboard_host}:{config.dashboard_port} ({exc})")
    return 0
