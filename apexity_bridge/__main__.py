"""ApexityEdu Attendance Bridge CLI.

    python -m apexity_bridge [command]

Commands
    run                  start the full bridge service (default on source)
    tray                 start native desktop app + tray (default .exe)
    init-db              initialise/migrate the SQLite database
    scan                 connectivity-proof tool: discover/identify terminals
                         and optionally READ attendance (never writes to devices)
    probe-ems            detect which attendance protocol the EMS speaks
    push-users           publish the terminal user tables (PIN + name) to the EMS now
    push-people          publish the people directory (students/teachers/staff)
    sync-now             one-shot sync: roster + device users + attendance
                         (this is what the scheduled task runs every N minutes)
    verify-ems           prove whether the EMS really ACCEPTS attendance for
                         each device serial (handshake + empty ATTLOG + USER)
    doctor               read-only self-check: DB, token, EMS, commands, logs
    set-token            store the EMS device token in the OS credential store
    delete-token         revoke/remove the stored EMS device token
    set-admin-password   set/change the dashboard admin password (PBKDF2 hash)
    backup               write a consistent SQLite backup
    export-diagnostics   build a redacted support bundle (zip)
    service              Windows auto-start management (install/start/stop/…)
"""
from __future__ import annotations

import argparse
import getpass
import ipaddress
import sys
import threading

from . import __version__
from .config import load_config
from .logging_utils import setup_logging


def _attach_console() -> None:
    """Give a windowed (noconsole) frozen exe a console for CLI commands."""
    if not (sys.platform.startswith("win") and getattr(sys, "frozen", False)):
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        if kernel32.GetConsoleWindow():
            return
        if not kernel32.AttachConsole(-1):  # parent is cmd.exe / PowerShell
            return
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        try:
            sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
        except OSError:
            pass
    except Exception:
        pass


def _default_command() -> str:
    if sys.platform.startswith("win") and getattr(sys, "frozen", False):
        return "tray"
    return "run"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apexity_bridge",
                                     description="ApexityEdu Smart Attendance Bridge")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="start the bridge service (default)")
    run_p.add_argument("--no-dashboard", action="store_true", help="run headless")

    tray_p = sub.add_parser("tray", help="start the native desktop app + tray")
    tray_p.add_argument("--hidden", action="store_true",
                        help="background only — no window (used by auto-start)")

    sub.add_parser("init-db", help="create/migrate the SQLite database and exit")

    scan_p = sub.add_parser("scan", help="discover & identify ZKTeco terminals (read-only)")
    scan_p.add_argument("--ip", action="append", default=[],
                        help="specific IP to probe (repeatable)")
    scan_p.add_argument("--subnet", action="append", default=[],
                        help="extra CIDR to scan, e.g. 192.168.0.0/24 (repeatable)")
    scan_p.add_argument("--port", type=int, default=4370)
    scan_p.add_argument("--comm-key", type=int, default=None)
    scan_p.add_argument("--timeout", type=int, default=None)
    scan_p.add_argument("--read-attendance", type=int, default=0, metavar="N",
                        help="also READ the last N attendance records (never deletes)")

    sub.add_parser("probe-ems", help="detect the EMS attendance protocol (read-only)")

    push_users_p = sub.add_parser(
        "push-users", help="publish the terminal user tables (PIN + name) to the EMS now")
    push_users_p.add_argument("--sn", default="",
                              help="one serial (default: every registered device)")

    push_people_p = sub.add_parser(
        "push-people", help="publish the people directory (students/teachers/staff)")
    push_people_p.add_argument("--type", action="append", default=[], dest="types",
                               choices=["student", "teacher", "staff", "other"],
                               help="only this person type (repeatable)")
    push_people_p.add_argument("--id", dest="ems_student_id", default="",
                               help="publish one person by EMS id")

    sub.add_parser("sync-now",
                   help="one-shot sync: roster + device users + attendance")

    ver_p = sub.add_parser("verify-ems", help="prove the EMS accepts attendance for each serial")
    ver_p.add_argument("--sn", default="", help="check one serial (default: every registered device)")
    ver_p.add_argument("--no-users", action="store_true",
                       help="skip publishing the device user table")

    doc_p = sub.add_parser("doctor", help="read-only self-check (DB, token, EMS, logs)")
    doc_p.add_argument("--no-network", action="store_true",
                       help="skip every EMS call (offline report)")
    doc_p.add_argument("--json", action="store_true", help="machine-readable output")

    sub.add_parser("set-token", help="store the EMS device token in the OS credential store")
    sub.add_parser("delete-token", help="remove the stored EMS device token")
    sub.add_parser("set-admin-password", help="set the dashboard admin password")

    sub.add_parser("backup", help="write a consistent SQLite backup")
    sub.add_parser("export-diagnostics", help="build a redacted support bundle (zip)")

    svc_p = sub.add_parser("service", help="Windows auto-start management")
    svc_p.add_argument("action", choices=["install", "uninstall", "start", "stop",
                                          "restart", "status", "install-sync",
                                          "uninstall-sync"])

    args = parser.parse_args(argv)
    command = args.command or _default_command()
    # Ensure 'hidden' attribute exists for tray command (may default on Windows)
    if not hasattr(args, "hidden"):
        args.hidden = False
    if command not in ("run", "tray"):
        _attach_console()
    config = load_config()
    setup_logging(config.log_level, config.log_dir)

    handler = {
        "run": _cmd_run, "tray": _cmd_tray, "init-db": _cmd_init_db,
        "scan": _cmd_scan, "probe-ems": _cmd_probe_ems,
        "push-users": _cmd_push_users, "push-people": _cmd_push_people,
        "sync-now": _cmd_sync_now,
        "verify-ems": _cmd_verify_ems, "doctor": _cmd_doctor,
        "set-token": _cmd_set_token,
        "delete-token": _cmd_delete_token,
        "set-admin-password": _cmd_set_admin_password,
        "backup": _cmd_backup, "export-diagnostics": _cmd_diagnostics,
        "service": _cmd_service,
    }[command]
    return handler(config, args)


# --------------------------------------------------------------------------- --
def _cmd_run(config, args) -> int:
    from .service import BridgeService
    from .windows_service.singleton import SingleInstance
    try:
        with SingleInstance():
            service = BridgeService(config, start_dashboard=not args.no_dashboard)
            try:
                service.run_forever()
            except KeyboardInterrupt:
                service.stop()
    except RuntimeError:
        from .desktop.ipc import notify_running_instance
        if notify_running_instance():
            print("ApexityEdu Bridge is already running — brought the window to the front.")
        else:
            print("ApexityEdu Bridge is already running.")
        return 0
    return 0


def _cmd_tray(config, args) -> int:
    from .tray import run_tray
    return run_tray(config, open_browser=not args.hidden)


def _cmd_init_db(config, args) -> int:
    from .database.repositories import open_migrated_database
    db, _ = open_migrated_database(config.db_path)
    ok = db.check_integrity()
    print(f"database ready: {config.db_path} (integrity: {'ok' if ok else 'PROBLEM'})")
    db.close_all()
    return 0 if ok else 1


def _cmd_scan(config, args) -> int:
    """Stage B: prove connectivity. Pure read-only network operations."""
    from .devices.factory import create_adapter
    from .devices.exceptions import DeviceError
    from .discovery import ports as ports_mod
    from .discovery.identify import identify_host
    from .discovery.interfaces import list_ipv4_networks, probe_hosts
    from .discovery.ping import ping

    timeout = args.timeout or config.device_timeout
    comm_key = args.comm_key if args.comm_key is not None else config.device_comm_key

    targets = list(args.ip)
    subnets = [ipaddress.IPv4Network(s, strict=False) for s in args.subnet]
    if not subnets and not targets:
        subnets = list_ipv4_networks(list(config.discovery_extra_subnets))
        print(f"auto-detected subnets: {', '.join(str(s) for s in subnets) or '(none)'}")
    if subnets:
        hosts = probe_hosts(subnets)
        print(f"TCP-probing {len(hosts)} hosts on port {args.port} "
              f"(concurrency {config.discovery_concurrency})…")
        open_hosts = ports_mod.probe_hosts_port(
            hosts, args.port, timeout=min(1.0, timeout),
            concurrency=config.discovery_concurrency,
        )
        print(f"  {len(open_hosts)} host(s) have port {args.port} open: "
              f"{', '.join(open_hosts) or 'none'}")
        targets += open_hosts

    if not targets:
        print("no targets to identify (use --ip or --subnet)")
        return 1

    failures = 0
    for ip in dict.fromkeys(targets):
        print(f"\n=== {ip}:{args.port} ===")
        result = identify_host(ip, args.port, timeout=timeout, comm_key=comm_key,
                               adapter_factory=create_adapter)
        if not result.identified:
            failures += 1
            print(f"  NOT identified: {result.error}")
            continue
        identity = result.identity
        print(f"  serial    : {identity.serial_number}")
        print(f"  model     : {identity.model}")
        print(f"  firmware  : {identity.firmware_version}")
        print(f"  platform  : {identity.platform}")
        print(f"  mac       : {identity.mac_address or '(unavailable)'}")
        print(f"  dev time  : {identity.device_time}")

        if args.read_attendance > 0:
            adapter = create_adapter(host=ip, port=args.port, comm_key=comm_key,
                                     timeout=timeout)
            try:
                adapter.connect()
                records = adapter.read_attendance()
                print(f"  attendance: {len(records)} record(s) on device (NOT deleted)")
                for rec in records[-args.read_attendance:]:
                    print(f"    user={rec.user_id:<8} {rec.timestamp}  "
                          f"status={rec.status} punch={rec.punch}")
            except DeviceError as exc:
                failures += 1
                print(f"  attendance read FAILED: {exc}")
            finally:
                try:
                    adapter.disconnect()
                except Exception:
                    pass
    return 1 if failures else 0


def _cmd_probe_ems(config, args) -> int:
    """Stage A: verify which attendance protocol apexityedu.com speaks. Read-only."""
    from .ems.probe import probe_ems
    report = probe_ems(config.ems_base_url, iclock_path=config.ems_iclock_path,
                       timeout=config.ems_timeout)
    print(report.human())
    return 0 if report.reachable else 1


def _open_cli_context(config):
    """Open the database and build a context with a live EMS client.

    Used by the manual commands (``push-users``/``push-people``/``sync-now``)
    so they work while the desktop app is closed.
    """
    import threading

    from .config import SettingsView
    from .database.repositories import open_migrated_database
    from .security.secrets import SecretStore
    from .service import BridgeContext
    from .sync.worker import EmsStatus, SyncWorker

    db, repos = open_migrated_database(config.db_path)
    ctx = BridgeContext(
        version=__version__, config=config,
        settings=SettingsView(config, repos["settings"]),
        repos=repos, secret_store=SecretStore(config.data_dir),
        ems_status=EmsStatus(), stop_event=threading.Event(),
    )
    ctx.sync_worker = SyncWorker(ctx)
    return ctx, db


def _cmd_push_users(config, args) -> int:
    """Publish the terminal user tables to the EMS right now."""
    from .ems.push import PushError, format_push_result, push_device_users

    ctx, db = _open_cli_context(config)
    try:
        device_id = None
        if (args.sn or "").strip():
            device = ctx.repos["devices"].get_by_serial(args.sn.strip())
            if not device:
                print(f"no registered device with serial {args.sn!r}")
                return 1
            device_id = device["id"]
        try:
            result = push_device_users(ctx, device_id=device_id, force=True,
                                       actor="cli")
        except PushError as exc:
            print(str(exc))
            return 1
        print(format_push_result(result))
        return 0 if result.get("ok") else 1
    finally:
        db.close_all()


def _cmd_push_people(config, args) -> int:
    """Publish the people directory (students/teachers/staff) to the EMS."""
    from .ems.push import PushError, push_people

    ctx, db = _open_cli_context(config)
    try:
        try:
            result = push_people(ctx, person_types=list(args.types or []) or None,
                                 ems_student_id=(args.ems_student_id or "").strip() or None)
        except PushError as exc:
            print(str(exc))
            return 1
        if result.get("unsupported"):
            print(f"NOT PUBLISHED — {result.get('error') or result.get('detail')}")
            return 2
        if not result.get("ok"):
            print(f"FAILED — {result.get('error')}")
            return 1
        by_type = result.get("by_type") or {}
        summary = ", ".join(f"{count} {kind}" for kind, count in sorted(by_type.items()))
        print(f"published {result.get('accepted', 0)} of {result.get('people', 0)} "
              f"people to the EMS ({summary})")
        return 0
    finally:
        db.close_all()


def _cmd_sync_now(config, args) -> int:
    """One-shot sync — the command the scheduled task runs every N minutes."""
    from .ems.base import EmsError
    from .ems.push import PushError, format_push_result, push_device_users
    from .students.directory import refresh_people

    ctx, db = _open_cli_context(config)
    failures = 0
    try:
        # 1. people directory (download)
        try:
            stats = refresh_people(ctx.ems_client, ctx.repos)
            print(f"roster    : {stats.get('fetched', 0)} people "
                  f"({stats.get('inserted', 0)} new, {stats.get('updated', 0)} updated)")
        except (EmsError, PushError) as exc:
            failures += 1
            print(f"roster    : FAILED — {exc}")
        # 2. device user tables (upload)
        try:
            result = push_device_users(ctx, force=True, actor="cli")
            print(format_push_result(result))
            if not result.get("ok"):
                failures += 1
        except PushError as exc:
            failures += 1
            print(f"users     : FAILED — {exc}")
        # 3. attendance (upload)
        try:
            pushed = ctx.sync_worker.push_due_events()
            counts = ctx.repos["events"].counts()
            print(f"attendance: {pushed} event(s) pushed · "
                  f"{counts.get('pending', 0)} pending, {counts.get('synced', 0)} synced")
        except Exception as exc:            # noqa: BLE001 - reported, not raised
            failures += 1
            print(f"attendance: FAILED — {exc}")
        return 0 if failures == 0 else 1
    finally:
        db.close_all()


def _cmd_verify_ems(config, args) -> int:
    """Ask the live EMS, serial by serial, what it actually accepts.

    A green dashboard only means the EMS answered a handshake. This command
    proves ingestion instead: it posts an EMPTY ATTLOG (no fake punches are
    ever written) and, unless --no-users is given, publishes each terminal's
    user table so the EMS can map a PIN to a real person.
    """
    from .database.repositories import open_migrated_database
    from .ems.factory import build_ems_client
    from .ems.verify import format_report, verify_ems_ingestion
    from .security.secrets import SecretStore
    from .service import BridgeContext
    from .sync.worker import EmsStatus

    db, repos = open_migrated_database(config.db_path)
    try:
        secret_store = SecretStore(config.data_dir)
        from .config import SettingsView
        ctx = BridgeContext(
            version=__version__, config=config,
            settings=SettingsView(config, repos["settings"]),
            repos=repos, secret_store=secret_store,
            ems_status=EmsStatus(), stop_event=threading.Event(),
        )
        from .sync.worker import SyncWorker
        ctx.sync_worker = SyncWorker(ctx)
        report = verify_ems_ingestion(
            ctx, (args.sn or "").strip() or None,
            push_users=False if args.no_users else None,
        )
        print(format_report(report))
        return 0 if report.get("ok") else 1
    finally:
        db.close_all()


def _cmd_doctor(config, args) -> int:
    """Read-only self-check across database, token, EMS and logs."""
    import json

    from .config import SettingsView
    from .database.repositories import open_migrated_database
    from .diagnostics import build_doctor_report, format_doctor_report
    from .security.secrets import SecretStore
    from .service import BridgeContext
    from .sync.worker import EmsStatus, SyncWorker

    db, repos = open_migrated_database(config.db_path)
    try:
        ctx = BridgeContext(
            version=__version__, config=config,
            settings=SettingsView(config, repos["settings"]),
            repos=repos, secret_store=SecretStore(config.data_dir),
            ems_status=EmsStatus(), stop_event=threading.Event(),
        )
        if not args.no_network:
            ctx.sync_worker = SyncWorker(ctx)
        report = build_doctor_report(ctx, network=not args.no_network)
        print(json.dumps(report, indent=2, default=str) if args.json
              else format_doctor_report(report))
        return 0 if report.get("ok") else 1
    finally:
        db.close_all()


def _cmd_set_token(config, args) -> int:
    from .security.secrets import SecretStore
    token = getpass.getpass(
        "EMS device token (input hidden, stored in the OS credential store): "
    ).strip()
    if not token:
        print("empty token — nothing stored")
        return 1
    SecretStore(config.data_dir).set_ems_token(token)
    print("token stored. It is never written to .env, the database, or logs.")
    return 0


def _cmd_delete_token(config, args) -> int:
    from .security.secrets import SecretStore
    SecretStore(config.data_dir).delete_ems_token()
    print("token removed from the credential store.")
    return 0


def _cmd_set_admin_password(config, args) -> int:
    from .database.repositories import open_migrated_database
    from .security.passwords import hash_password

    db, repos = open_migrated_database(config.db_path)
    try:
        pw1 = getpass.getpass("new dashboard admin password (min 8 chars): ")
        pw2 = getpass.getpass("repeat password: ")
        if pw1 != pw2:
            print("passwords do not match")
            return 1
        try:
            hashed = hash_password(pw1)
        except ValueError as exc:
            print(exc)
            return 1
        repos["settings"].set("dashboard_admin_password_hash", hashed, is_secret=True)
        repos["audit"].log(actor="cli", action="dashboard.password_set")
        print("password updated (stored as salted PBKDF2 hash).")
        return 0
    finally:
        db.close_all()


def _cmd_backup(config, args) -> int:
    from .database.repositories import open_migrated_database
    from .paths import backup_dir
    import time
    db, _ = open_migrated_database(config.db_path)
    try:
        target = db.backup_to(
            backup_dir() / f"bridge-backup-{time.strftime('%Y%m%d-%H%M%S')}.db"
        )
        print(f"backup written: {target}")
        return 0
    finally:
        db.close_all()


def _cmd_diagnostics(config, args) -> int:
    from .service import BridgeContext
    from .database.repositories import open_migrated_database
    from .config import SettingsView
    from .dashboard.app import build_diagnostic_zip
    from .sync.worker import EmsStatus
    from .security.secrets import SecretStore
    import threading

    db, repos = open_migrated_database(config.db_path)
    try:
        ctx = BridgeContext(
            version=__version__, config=config,
            settings=SettingsView(config, repos["settings"]),
            repos=repos, secret_store=SecretStore(config.data_dir),
            ems_status=EmsStatus(), stop_event=threading.Event(),
        )
        path = build_diagnostic_zip(ctx)
        print(f"diagnostics bundle: {path}")
        return 0
    finally:
        db.close_all()


def _cmd_service(config, args) -> int:
    from .windows_service.control import service_action
    return service_action(args.action, config)


if __name__ == "__main__":
    sys.exit(main())
