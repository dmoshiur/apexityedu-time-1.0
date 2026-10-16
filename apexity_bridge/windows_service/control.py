"""Windows auto-start management via Task Scheduler (no pywin32 required).

* install   → register task "ApexityEduBridge" that starts at logon,
              restarts on failure, runs hidden, single instance guarded
              additionally by our named mutex.
* uninstall → remove the task (data preserved).
* start / stop / restart / status → schtasks.exe control + liveness probe.
* install-sync / uninstall-sync → an *optional* second, periodic task
              ("ApexityEduBridge-Sync") that runs ``sync-now`` every N
              minutes. It is the safety net for schools that close the app
              between shifts: attendance, user tables and the people
              directory are still pushed to ApexityEdu on a schedule.

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
SYNC_TASK_NAME = "ApexityEduBridge-Sync"
DEFAULT_SYNC_INTERVAL_MINUTES = 15


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


def launch_command_for_sync() -> tuple[str, str, str]:
    """(executable, arguments, working_directory) for the periodic sync task.

    The one-shot sync must be able to print its result into a log, so the
    console build (``python``, not ``pythonw``) is used.
    """
    if getattr(sys, "frozen", False):
        exe = str(Path(sys.executable).resolve())
        workdir = str(Path(exe).parent)
        return exe, "sync-now", workdir
    python_exe = str(Path(sys.executable))
    workdir = str(Path.cwd())
    return python_exe, "-m apexity_bridge sync-now", workdir


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


def periodic_task_xml(*, python_exe: str, workdir: str,
                      interval_minutes: int = DEFAULT_SYNC_INTERVAL_MINUTES,
                      arguments: str | None = None) -> str:
    """Task that repeats ``sync-now`` every N minutes, forever.

    This is the *scheduled* half of the auto-start story: the bridge app
    itself is the always-on collector, this task is the belt-and-braces
    pusher for schools that close the app between shifts.
    """
    args = arguments if arguments is not None else "-m apexity_bridge sync-now"
    try:
        minutes = max(1, int(interval_minutes))
    except (TypeError, ValueError):
        minutes = DEFAULT_SYNC_INTERVAL_MINUTES
    iso = f"PT{minutes}M"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <URI>\\{SYNC_TASK_NAME}</URI>
    <Description>ApexityEdu Bridge — scheduled sync: pushes attendance, device users and the people directory to ApexityEdu every {minutes} minute(s).</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>{iso}</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2026-01-01T00:05:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
    <ExecutionTimeLimit>PT15M</ExecutionTimeLimit>
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


# --------------------------------------------------------------------- tasks --
def _register(config, name: str, xml: str) -> tuple[int, str]:
    xml_path = config.data_dir / f"{name}-task.xml"
    try:
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text(xml, encoding="utf-16")
    except OSError as exc:
        return 1, f"could not write {xml_path}: {exc}"
    result = _run(["schtasks", "/Create", "/TN", name, "/XML", str(xml_path), "/F"])
    if result.returncode != 0:
        return 1, f"task registration failed:\n{result.stdout}{result.stderr}"
    return 0, f"Task '{name}' registered."


def install_task(config) -> tuple[int, str]:
    python_exe, arguments, workdir = launch_command()
    xml = task_xml(python_exe=python_exe, workdir=workdir, arguments=arguments)
    code, message = _register(config, TASK_NAME, xml)
    if code:
        return code, message
    return 0, (f"Task '{TASK_NAME}' registered (auto-start at logon/boot, "
               f"restart-on-failure).\nStart it now with:  "
               f"python -m apexity_bridge service start")


def uninstall_task() -> tuple[int, str]:
    _run(["schtasks", "/End", "/TN", TASK_NAME])
    result = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    if result.returncode != 0:
        return 1, f"task removal failed: {result.stdout}{result.stderr}"
    return 0, f"Task '{TASK_NAME}' removed. Data was PRESERVED."


def install_sync_task(config, interval_minutes: int = DEFAULT_SYNC_INTERVAL_MINUTES) -> tuple[int, str]:
    python_exe, arguments, workdir = launch_command_for_sync()
    xml = periodic_task_xml(python_exe=python_exe, workdir=workdir,
                            interval_minutes=interval_minutes, arguments=arguments)
    code, message = _register(config, SYNC_TASK_NAME, xml)
    if code:
        return code, message
    return 0, (f"Task '{SYNC_TASK_NAME}' registered — a scheduled sync runs "
               f"'sync-now' every {interval_minutes} minute(s), so attendance, "
               f"device users and the people directory reach ApexityEdu even "
               f"when the desktop app is closed.")


def uninstall_sync_task() -> tuple[int, str]:
    _run(["schtasks", "/End", "/TN", SYNC_TASK_NAME])
    result = _run(["schtasks", "/Delete", "/TN", SYNC_TASK_NAME, "/F"])
    if result.returncode != 0:
        return 1, f"task removal failed: {result.stdout}{result.stderr}"
    return 0, f"Task '{SYNC_TASK_NAME}' removed."


def start_task(name: str = TASK_NAME) -> tuple[int, str]:
    result = _run(["schtasks", "/Run", "/TN", name])
    return result.returncode, (result.stdout.strip() or result.stderr.strip())


def stop_task(name: str = TASK_NAME) -> tuple[int, str]:
    result = _run(["schtasks", "/End", "/TN", name])
    return result.returncode, (result.stdout.strip() or result.stderr.strip())


def task_status(name: str = TASK_NAME) -> dict:
    """Structured state of one scheduled task (installed? running? next run?)."""
    if not _is_windows():
        return {"name": name, "supported": False, "installed": False,
                "state": "not supported on this platform", "detail": ""}
    result = _run(["schtasks", "/Query", "/TN", name, "/V", "/FO", "LIST"])
    if result.returncode != 0:
        return {"name": name, "supported": True, "installed": False,
                "state": "not installed", "detail": ""}
    state = ""
    detail = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("status"):
            state = stripped.split(":", 1)[-1].strip()
        elif any(k in stripped for k in ("TaskName", "Last Run", "Next Run",
                                         "Task To Run", "Logon Mode")):
            detail.append(stripped)
    return {"name": name, "supported": True, "installed": True,
            "state": state or "installed", "detail": "\n".join(detail)}


def scheduler_overview(config) -> dict:
    """Everything the desktop app needs to show about the scheduler."""
    if not _is_windows():
        return {
            "supported": False,
            "message": "The Windows Task Scheduler is only available on Windows. "
                       "On this platform run the bridge directly "
                       "(python -m apexity_bridge run) or use systemd.",
            "tasks": [],
        }
    tasks = [task_status(TASK_NAME), task_status(SYNC_TASK_NAME)]
    return {"supported": True, "message": "", "tasks": tasks}


# ------------------------------------------------------------------ actions --
def service_action(action: str, config) -> int:
    if not _is_windows():
        print("The 'service' command manages Windows auto-start (Task Scheduler).")
        print("On this platform, run the bridge directly:  python -m apexity_bridge run")
        print("(Linux/macOS deployments may use systemd/supervisord instead.)")
        return 1

    if action == "install":
        code, message = install_task(config)
    elif action == "uninstall":
        code, message = uninstall_task()
    elif action == "start":
        code, message = start_task(TASK_NAME)
    elif action == "stop":
        code, message = stop_task(TASK_NAME)
    elif action == "restart":
        stop_task(TASK_NAME)
        code, message = start_task(TASK_NAME)
    elif action == "status":
        code, message = _status(config)
    elif action == "install-sync":
        code, message = install_sync_task(config)
    elif action == "uninstall-sync":
        code, message = uninstall_sync_task()
    else:
        print(f"unknown service action: {action}")
        return 1
    print(message)
    return code


def _status(config) -> tuple[int, str]:
    lines = []
    code = 0
    for name in (TASK_NAME, SYNC_TASK_NAME):
        state = task_status(name)
        if state["installed"]:
            lines.append(f"Task '{name}': {state['state']}")
            if state["detail"]:
                lines.append(state["detail"])
        else:
            lines.append(f"Task '{name}': NOT installed")
            if name == TASK_NAME:
                code = 1
    # liveness probe of the local dashboard
    try:
        import urllib.request
        url = f"http://{config.dashboard_host}:{config.dashboard_port}/login"
        with urllib.request.urlopen(url, timeout=3) as resp:
            lines.append(f"dashboard: responding (HTTP {resp.status}) at {url}")
    except Exception as exc:
        lines.append(f"dashboard: not responding at {config.dashboard_host}:{config.dashboard_port} ({exc})")
    return code, "\n".join(lines)
