# Handoff prompts — copy/paste

Two prompts live here:

* **Prompt A** — for whoever builds the **ApexityEdu web app** (the server
  side). This is the only real blocker left for "the people I created must
  show up in ApexityEdu".
* **Prompt B** — for a coding agent continuing work on **this** repository
  (verification, polish, further features).

---

## Prompt A — the ApexityEdu server side (Laravel/PHP or whatever it is)

```text
You are working on the ApexityEdu web application (apexityedu.com). We need
the attendance-bridge contract implemented server-side. The bridge is a
Windows desktop app on the school's LAN that reads ZKTeco K40 fingerprint
terminals and pushes their data to this app. Full contract:
docs/EMS_API_CONTRACT.md in the bridge repository
(github.com/dmoshiur/apexityedu-time-1.0 → docs/EMS_API_CONTRACT.md).

Implement, in this order, and reply with the routes you added plus the
migration/table changes:

1) iClock/ADMS ingestion (Option A) — the app already answers
   GET /iclock/cdata and GET /iclock/getrequest, but it must also accept
   POST /iclock/cdata?SN=<serial>&table=ATTLOG with TAB-separated lines
   "PIN<TAB>YYYY-MM-DD HH:MM:SS<TAB>status<TAB>verify<TAB>workcode<TAB>0<TAB>0"
   and answer exactly "OK" or "OK: <accepted count>" with HTTP 200.
   Requirements:
     * the SN in the query string must be matched against the registered
       biometric device; an unknown SN must be refused (403), not silently
       stored;
     * attendance rows must be unique on (device_serial, device_user_id,
       timestamp, status) so a re-push after a crash never duplicates;
     * an empty body must be accepted with "OK: 0" (the bridge uses this to
       prove ingestion without writing a fake punch);
     * a login redirect, HTML, JSON or an empty body must never be treated
       as acceptance.

2) Device user table (Option D) — accept
   POST /iclock/cdata?SN=<serial>&table=USER with lines
   "USER PIN=7<TAB>Name=Anika Hossain<TAB>Pri=0<TAB>Passwd=<empty><TAB>Card=<empty><TAB>Grp=1<TAB>TZ=..."
   and store PIN → name for that device, so a punch can be attributed to a
   person. Also accept POST /api/v1/bridge/devices/users with
   {"serial": "...", "users": [{"user_id","name","privilege","card"}]}.
   Reply "OK" / "OK: <n>" (iClock) or {"ok": true, "accepted": n} (REST).
   Never store Passwd, never store fingerprints.

3) People directory (Option C) — GET /api/v1/bridge/people?cursor=&limit=&types=
   returning {"people": [{"id","name","person_type","admission_no","roll_no",
   "class","section","employee_no","designation","department","active"}],
   "next_cursor": null}. person_type is student|teacher|staff|other. Keep the
   legacy GET /api/v1/bridge/students working (bridge falls back to it).

4) People write (Option E) — POST /api/v1/bridge/people with
   {"people": [{...same fields..., "id": "S1001"}]} must UPSERT by id
   (never duplicate) and answer
   {"ok": true, "accepted": n, "created": x, "updated": y}. This is what
   the desktop app's "Push people to ApexityEdu" and
   "Add person to ApexityEdu…" buttons call. If a person has no id, reject
   that row. Bearer-token auth like the other /api/v1/bridge routes; 404 is
   the honest "not implemented" answer if you skip it — the bridge reports
   a missing endpoint verbatim instead of pretending the people were saved.

5) Make the pushed people visible in the web UI: a "Biometric users" list
   per device (PIN + name + the person it is bound to), and a "map PIN →
   student/teacher/staff" screen. The bridge exports the mapping as CSV
   (GET /api/v1/bridge/mappings/export.csv) if you prefer to import it.

Acceptance: run the reference implementation in the bridge repo
(docs/reference/ems_reference_api.py — Flask + SQLite, ~300 lines) locally,
point the bridge at it (EMS_BASE_URL=http://127.0.0.1:5000,
EMS_ALLOW_INSECURE_LOCALHOST=1), and see punches, device users and people
arrive. Then repeat against apexityedu.com and paste the
"python -m apexity_bridge verify-ems" report into the ticket.
```

---

## Prompt B — continuing on the bridge repository

```text
You are continuing work on github.com/dmoshiur/apexityedu-time-1.0
(ApexityEdu Smart Attendance Bridge — a Windows desktop app that reads
ZKTeco K40 terminals and pushes attendance to apexityedu.com).

Context: a school created two users directly on the K40. The bridge's
Device Users page shows them, but they never reached ApexityEdu. The cause
was that the manual "publish" button only *queued* work for the background
worker, and that worker only pushes while the EMS is marked "online" — so a
refused push looked exactly like a successful one. The people directory
(students/teachers/staff) was download-only: the bridge could never write
people back.

What is already implemented in this branch (do not redo it):
  * apexity_bridge/ems/push.py — the manual push service
    (push_device_users / push_people / create_person / push_attendance_now).
    Every push returns what the server actually answered.
  * apexity_bridge/ems/base.py, ems/rest.py, ems/iclock.py — the new
    optional "push_people" capability (POST /api/v1/bridge/people, Option E
    in docs/EMS_API_CONTRACT.md); iClock delegates to the REST side-channel
    or refuses with a clear message.
  * apexity_bridge/sync/worker.py — push_device_users_now() for manual
    pushes (works even when the EMS is not marked online).
  * apexity_bridge/desktop/facade.py + desktop/gui.py — the buttons:
    Device Users → "Push users to EMS" (real result dialog), People →
    "Push people to ApexityEdu" and "Add person to ApexityEdu…",
    Settings → "Auto-start & scheduled sync" (install/start/stop/status for
    the Windows Task Scheduler tasks) and "Sync now".
  * apexity_bridge/dashboard/app.py + static/app.js + templates — the same
    actions in the browser dashboard.
  * apexity_bridge/__main__.py — new CLI commands: push-users, push-people,
    sync-now; `service install-sync` / `service uninstall-sync` register the
    periodic "ApexityEduBridge-Sync" task (sync-now every 15 minutes).
  * docs/EMS_API_CONTRACT.md § Option E, docs/PUSH_AND_SCHEDULER.md,
    docs/ACCEPTANCE_TEST_PLAN.md (scenarios 21–23), the reference EMS
    implementation (docs/reference/ems_reference_api.py) and 27 new tests.

Your job now:
  1. Run the full suite (scripts/run_tests.sh — 264 tests) and keep it green.
  2. Review the diff for honesty: every failure path must state what the
     server said and what the server side must implement. No "queued" as a
     success message, no invented people, no fake attendance.
  3. Verify on the real machine (or describe exactly how to):
     `python -m apexity_bridge push-users`, `push-people`, `sync-now`,
     `verify-ems`; check the Task Scheduler entries
     `ApexityEduBridge` and `ApexityEduBridge-Sync`.
  4. Whatever else is still missing for "a person created in the bridge
     shows up in ApexityEdu" — implement it, document it in
     docs/PUSH_AND_SCHEDULER.md and add a test.
  5. Never delete device data, never send biometrics or passwords, and keep
     the offline-first guarantees (events are stored locally first, retries
     use exponential backoff, pushes are idempotent).
```
