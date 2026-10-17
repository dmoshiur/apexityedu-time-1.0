# "EMS OFFLINE" / `DENIED` — fix guide and copy-paste prompt

**Symptom:** the bridge shows **EMS OFFLINE** (from v1.3.2: **SERIAL DENIED**),
and `https://apexityedu.com/iclock...` answers `DENIED` in a browser.

**What it means:** the server is up and the internet works. The ApexityEdu
iClock handler simply refuses the serial the bridge identifies as. Until at
least one real K40 serial is accepted, the bridge holds every punch in its
local queue (nothing is lost) and uploads them once the serial is accepted.

> Note: `https://apexityedu.com/iclock` with **no** `SN=` always answers
> `DENIED`. That URL does not prove anything. Test with the real serial (step 1).

---

## Part 1 — quick checks (no coding, ~5 minutes)

1. Find the terminal's serial: bridge → **Devices** (or on the K40:
   *System Info → Device Info → Serial No.*).
2. Open in a browser (replace `CKXX123`):
   `https://apexityedu.com/iclock/cdata?SN=CKXX123&options=all`
   * `GET OPTION FROM: CKXX123 …` → the serial is accepted. Fully exit and
     restart the bridge (tray → Exit). It should show ONLINE within a minute.
   * `DENIED` → go to step 3.
3. ApexityEdu admin → **Biometric Devices**: add/activate the device with
   **exactly** that serial (no spaces, same letters). If the device is already
   registered under a *different* serial, copy that serial and paste it into
   the bridge → **Devices → EMS serial…** instead.
4. Repeat step 2. If it still says `DENIED` while the device is registered and
   active, the problem is in the ApexityEdu server code. Use the prompt below.

---

## Part 2 — prompt for the ApexityEdu server codebase

Copy everything in the block into the coding agent/developer that works on the
**ApexityEdu web application** (not this bridge repository). Fill in the two
`<…>` values first.

```text
You are working on the ApexityEdu web application (https://apexityedu.com).
Its ZKTeco iClock/ADMS endpoint answers "DENIED" for our biometric terminal,
so the school's attendance bridge cannot upload punches.

FACTS
- Terminal serial (as the bridge sends it): <SERIAL, e.g. CKXX123>
- It IS registered and active in ApexityEdu → Biometric Devices for school:
  <SCHOOL NAME / TENANT>
- GET https://apexityedu.com/iclock/cdata?SN=<SERIAL>&options=all  →  "DENIED"
- On 2026-10-16 the same route answered a "GET OPTION FROM:" block for ANY
  serial; now it answers "DENIED". Something changed in the serial check.
- Bridge contract (reference): github.com/dmoshiur/apexityedu-time-1.0 →
  docs/EMS_API_CONTRACT.md (Option A — iClock/ADMS).

STEP 1 — FIND THE ROOT CAUSE FIRST (do not change behaviour yet)
Find every code path that returns the body "DENIED" under /iclock/*
(cdata GET+POST, getrequest, devicecmd, and any middleware/decorator in
front of them). For our serial, report which condition fails. Check
specifically:
  a) Serial comparison: is it case-sensitive, or does the stored value have
     leading/trailing whitespace, a different prefix, or a zero-width
     character? Compare after trim() and case-insensitively.
  b) Device state: an is_active / status / approved / deleted_at flag, or a
     device that must be "approved" after first contact.
  c) Tenant/school resolution: does the lookup depend on the request Host or
     a subdomain (e.g. school.apexityedu.com)? The bridge calls the bare
     domain https://apexityedu.com, so the device must be found by serial
     globally (serials are unique), and the tenant derived FROM the device.
  d) Auth: is a token/key/IP allow-list required? The bridge can send a shared
     secret as BOTH the query param AuthToken=<token> and the header
     X-Auth-Token: <token>. If the server expects a different name
     (e.g. "key", "token", "Authorization"), accept AuthToken/X-Auth-Token too.
  e) Required query params: the bridge sends
     SN, options=all, pushver=2.4.2, language=69, PushOptionsFlag=1.
     Missing optional params must not cause DENIED.
  f) Caching: a cached "unknown serial" result that survives registration.
Reply with the exact file/function/line and the failing condition.

STEP 2 — FIX (keep security: unknown serials must still be refused)
For a serial that is registered and active (matched trimmed and
case-insensitively, tenant derived from the device):
  1. GET  /iclock/cdata?SN=<SN>&options=all…  → HTTP 200, text/plain:
         GET OPTION FROM: <SN>
         Stamp=<n>
         OpStamp=<n>
         ErrorDelay=30
         Delay=10
         TransTimes=00:00;14:05
         TransInterval=1
         TransFlag=1111000000
         Realtime=1
         Encrypt=0
     and update the device's last_seen (the "online" indicator).
  2. GET  /iclock/getrequest?SN=<SN>  → HTTP 200 "OK" (or queued
     "C:<id>:<command>" lines), and update last_seen.
  3. POST /iclock/cdata?SN=<SN>&table=ATTLOG&Stamp=<unix>
     body: one line per punch, TAB-separated:
         PIN<TAB>YYYY-MM-DD HH:MM:SS<TAB>status<TAB>verify<TAB>workcode<TAB>0<TAB>0
     → HTTP 200 "OK: <number of lines accepted>". An EMPTY body must answer
     "OK: 0" (the bridge uses it to prove ingestion without a fake punch).
     Store with a unique key (device serial, PIN, timestamp, status) so a
     re-sent punch is ignored instead of duplicated.
  4. POST /iclock/cdata?SN=<SN>&table=USER, lines like
         USER PIN=7<TAB>Name=Anika<TAB>Pri=0<TAB>Passwd=<TAB>Card=<TAB>Grp=1<TAB>TZ=0000000000000000
     → HTTP 200 "OK: <n>". Store PIN → name per device. Never store Passwd.
  5. POST /iclock/devicecmd?SN=<SN>, body "ID=<id>&Return=<code>&CMD=<cmd>"
     → HTTP 200 "OK", and mark that queued command done (Return 0) or failed
     (non-zero) so it is NOT re-sent on every poll.
  Never answer with a redirect, an HTML login page or JSON on these routes —
  always text/plain. CSRF protection must be disabled for /iclock/* (devices
  cannot send CSRF tokens), and the routes must not require a browser session.
For an unknown or inactive serial keep answering "DENIED" (or HTTP 403),
but LOG it: serial, reason (unknown / inactive / tenant / token), client IP,
time.

STEP 3 — MAKE IT VISIBLE TO THE ADMIN
In Biometric Devices add:
  - "Last seen" per device (from cdata/getrequest), shown as Online when
    within the last 2 minutes;
  - a "Recently refused serials" list (serial, reason, last attempt, count)
    with a one-click "Register this device" action.

STEP 4 — ACCEPTANCE TESTS (add automated tests, and run these by hand)
Replace SN with the registered serial; UNKNOWN-123 must stay unknown.
  curl -s "https://apexityedu.com/iclock/cdata?SN=SN&options=all"
      → starts with "GET OPTION FROM: SN"
  curl -s "https://apexityedu.com/iclock/cdata?SN=sn%20&options=all"
      → same as above (case/whitespace tolerant)
  curl -s "https://apexityedu.com/iclock/getrequest?SN=SN"
      → "OK"
  curl -s -X POST -H "Content-Type: text/plain" --data-binary "" \
       "https://apexityedu.com/iclock/cdata?SN=SN&table=ATTLOG&Stamp=0"
      → "OK: 0"
  curl -s "https://apexityedu.com/iclock/cdata?SN=UNKNOWN-123&options=all"
      → "DENIED" (or 403), and the refusal appears in the admin list
  A test punch sent twice → stored once.
Reply with: root cause, files changed, migrations, and the output of the
curl checks.
```

---

## Part 3 — after the server is fixed

1. Bridge → fully exit (tray → Exit) and start it again.
2. Within ~1 minute the Overview should show **EMS ONLINE**.
3. Check that ingestion really works (sends only an empty ATTLOG, never a fake punch):

   ```powershell
   & "C:\Program Files\ApexityEdu\Attendance Bridge\ApexityEduBridge.exe" verify-ems
   & "C:\Program Files\ApexityEdu\Attendance Bridge\ApexityEduBridge.exe" doctor
   ```

4. Give one test punch on the K40. It should appear in ApexityEdu within one
   sync cycle (~30 s). Punches that were queued while the serial was DENIED are
   uploaded automatically.
