"""ApexityEdu Attendance Bridge CLI.

    python -m apexity_bridge [command]

Commands
    run                  start the full bridge service (default)
    init-db              initialise/migrate the SQLite database
    scan                 connectivity-proof tool: discover/identify terminals
                         and optionally READ attendance (never writes to devices)
    probe-ems            detect which attendance protocol the EMS speaks
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

from . import __version__
from .config import load_config
from .logging_utils import setup_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apexity_bridge",
                                     description="ApexityEdu Smart Attendance Bridge")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="start the bridge service (default)")
    run_p.add_argument("--no-dashboard", action="store_true", help="run headless")

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

    sub.add_parser("set-token", help="store the EMS device token in the OS credential store")
    sub.add_parser("delete-token", help="remove the stored EMS device token")
    sub.add_parser("set-admin-password", help="set the dashboard admin password")

    sub.add_parser("backup", help="write a consistent SQLite backup")
    sub.add_parser("export-diagnostics", help="build a redacted support bundle (zip)")

    svc_p = sub.add_parser("service", help="Windows auto-start management")
    svc_p.add_argument("action", choices=["install", "uninstall", "start", "stop",
                                          "restart", "status"])

    args = parser.parse_args(argv)
    command = args.command or "run"
    config = load_config()
    setup_logging(config.log_level, config.log_dir)

    handler = {
        "run": _cmd_run, "init-db": _cmd_init_db, "scan": _cmd_scan,
        "probe-ems": _cmd_probe_ems, "set-token": _cmd_set_token,
        "delete-token": _cmd_delete_token,
        "set-admin-password": _cmd_set_admin_password,
        "backup": _cmd_backup, "export-diagnostics": _cmd_diagnostics,
        "service": _cmd_service,
    }[command]
    return handler(config, args)


# --------------------------------------------------------------------------- --
def _cmd_run(config, args) -> int:
    from .service import BridgeService
    service = BridgeService(config, start_dashboard=not args.no_dashboard)
    try:
        service.run_forever()
    except KeyboardInterrupt:
        service.stop()
    return 0


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
