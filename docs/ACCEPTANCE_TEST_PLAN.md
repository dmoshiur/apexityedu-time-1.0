# Acceptance Test Plan — real K40 + live EMS

Automated unit/integration coverage is in `tests/` (run:
`scripts/run_tests.sh` — **116 tests**). This plan is the **real-device
gate**: it must be executed on site with the actual K40 and the actual EMS
before the bridge is declared production-ready. Unit tests passing never
substitute for hardware verification.

Legend: ✅ covered by automated tests — still verify on hardware · 🔧 hardware-only

| # | Scenario | Automated coverage | On-site procedure | Expected result |
|---|---|---|---|---|
| 1 | PC startup with a known K40 | ✅ `test_service_lifecycle`, collector tests | Reboot the PC with the bridge installed. Wait 2 min. | Dashboard: device **online**; punches from the test period appear; audit shows `service.started` + `scan.manual`/`ip_auto_repaired` as applicable. |
| 2 | K40 powered off at startup | ✅ `test_offline_device_backs_off_then_recovers` | Power the K40 off, reboot PC, boot fully, then power K40 on. | Dashboard marks device registered-but-offline; after power-on + one collect interval it flips online **without admin action**; no duplicate records. |
| 3 | K40 powered on after bridge starts | ✅ same | Start bridge first, then power device. | First successful collect logs `N new attendance events`; device `last_seen` updates. |
| 4 | Device IP changes | ✅ `test_ip_change_auto_repaired_after_serial_verification` | Change the K40's IP from its menu (or DHCP reservation). Do nothing else. | Within one discovery interval: dashboard shows the new IP with the ↻ marker; audit `device.ip_auto_repaired`; collection continues; **no mapping/history loss**; no new device registered. |
| 5 | Internet disconnected & restored | ✅ `test_offline_then_online_full_recovery` | Unplug WAN on the router 10 min while students punch. Reconnect. | Overview shows EMS OFFLINE, pending count grows (events already in SQLite). After restore: pending drains to 0; EMS shows each punch **once**; device logs on the K40 are untouched. |
| 6 | EMS server temporarily unavailable | ✅ `test_failed_push_retries_with_backoff_then_succeeds` | Block apexityedu.com on the firewall for 30 min. | Sync attempts log `failed` with growing backoff; events recover automatically after unblock; nothing duplicated. |
| 7 | Windows restart with pending records | ✅ `test_crash_between_accept_and_mark_repushes_safely` | Disconnect internet, collect punches, restart Windows *while* pending > 0. | After restart + internet: all pending events reach the EMS exactly once (server idempotency proves no doubles). |
| 8 | Duplicate device records | ✅ `test_device_serial_is_unique_when_present`, `test_repeated_reads_never_duplicate` | Run three consecutive scans; collect twice in a row. | One device row per serial; second collect adds `0 new events`. |
| 9 | Identical timestamps | ✅ `test_same_timestamp_different_users_are_distinct` | Two students punch within the same second. | Both records exist with distinct dedup hashes. |
| 10 | Unknown biometric user ID | ✅ `test_unmapped_device_users_report` | Enroll a fresh finger on the K40, punch, wait a cycle. | Attendance page shows the event `unmapped`; Students → *Unmapped device users* lists it; EMS receives it flagged (or it stays reviewable per rules). |
| 11 | Student roster API failure | ✅ `test_roster_unsupported_is_surfaced_not_faked` | Temporarily break the roster endpoint. | Students page shows the exact endpoint problem + remediation; **no fake students**; attendance flow continues unaffected. |
| 12 | Auth failure / expired token | ✅ `test_auth_failure_penalty_and_no_spam`, REST client tests | Revoke the EMS token. | Pushes pause with 5-min penalty; dashboard shows EMS auth error; audit contains one `ems.auth_failed`. Re-set token via `set-token` → recovery without data loss. |
| 13 | Corrupt local database | ✅ `test_corrupt_db_quarantined_and_recreated` | (Maintenance window) stop bridge, corrupt a copy of the DB, restart. | Bridge quarantines the corrupt file (never deletes), starts on a fresh DB, and the troubleshooting guide shows how to restore from `backups/`. |
| 14 | Two K40s with distinct serials | ✅ `test_two_distinct_serials_are_two_devices` | Connect a second K40, scan. | Two rows, correct serials/IPs; events attribute to the right device; iClock pushes go under each device's own SN. |
| 15 | Discovered but not trusted | ✅ `test_new_terminal_becomes_pending_not_trusted`, `test_stranger_at_old_ip_is_refused` | Put an *unregistered* ZKTeco at the old IP of a registered device. | Collector refuses (`identity mismatch` audit entry); the stranger appears as `pending` only; zero records flow under the old device's identity. |
| 16 | Simulated device / protocol fixture | ✅ the whole suite runs on `FakeDeviceAdapter` + fake iClock/REST servers | `scripts/run_tests.sh` | CI/development without hardware. |

## Sign-off checklist

- [ ] `probe-ems` on the school PC identifies the live EMS protocol and the bridge is configured with it (`EMS_PROTOCOL` in Settings or `.env`).
- [ ] Student roster loads from the real EMS; classes/sections shown.
- [ ] All enrolled device users mapped to students (0 unmapped, 0 conflicts).
- [ ] Scenarios 1–16 executed and ticked above.
- [ ] One full school day comparison: K40 screen count = bridge collected = EMS received.
- [ ] Backup + restore drill performed (`Backup database now`, restore per TROUBLESHOOTING.md).
