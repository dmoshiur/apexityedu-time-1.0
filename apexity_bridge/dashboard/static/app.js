/* ApexityEdu Bridge dashboard — vanilla JS. All text rendering escapes HTML. */
"use strict";

const csrfToken = () => document.querySelector('meta[name="csrf-token"]')?.content || "";

async function api(path, { method = "GET", json, form } = {}) {
  const opts = { method, headers: { "X-CSRF-Token": csrfToken() } };
  if (json !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(json);
  } else if (form !== undefined) {
    opts.body = form;
  }
  const resp = await fetch(path, opts);
  let data = {};
  try { data = await resp.json(); } catch (_) { /* csv downloads etc. */ }
  if (!resp.ok) {
    const msg = data.error || `HTTP ${resp.status}`;
    const err = new Error(msg);
    err.status = resp.status;
    throw err;
  }
  return data;
}

function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

function flash(message, isError = false) {
  const box = document.getElementById("flash");
  box.textContent = message;
  box.className = "flash " + (isError ? "error" : "success");
  clearTimeout(box._t);
  box._t = setTimeout(() => box.classList.add("hidden"), 6000);
}

function fmtTs(value) {
  if (!value) return "—";
  return String(value).replace("T", " ").replace("+00:00", "");
}

function fmtAgo(value) {
  if (!value) return "never";
  const then = new Date(String(value).includes("+") ? value : value.replace(" ", "T") + "+00:00");
  const secs = Math.max(0, (Date.now() - then.getTime()) / 1000);
  if (secs < 90) return `${Math.round(secs)}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${(secs / 3600).toFixed(1)}h ago`;
  return `${(secs / 86400).toFixed(1)}d ago`;
}

function setEmsPill(online, label) {
  const pill = document.getElementById("ems-pill");
  if (!pill) return;
  pill.className = "pill " + (online === null ? "unknown" : online ? "ok" : "err");
  pill.textContent = label;
}

/* ------------------------------------------------------------- overview ---- */
async function loadOverview() {
  try {
    const d = await api("/api/overview");
    setEmsPill(d.ems.online, d.ems.online ? "EMS online" : "EMS offline");
    const up = d.service.uptime_seconds;
    document.getElementById("st-uptime").textContent =
      up > 3600 ? `${(up / 3600).toFixed(1)}h` : `${Math.max(1, Math.round(up / 60))}m`;
    document.getElementById("st-version").textContent = `v${d.service.version} · TZ ${d.service.timezone}`;
    document.getElementById("st-ems").textContent = d.ems.online ? "ONLINE" : "OFFLINE";
    document.getElementById("st-ems-detail").textContent =
      `${d.ems.protocol || "—"} · last OK ${fmtAgo(d.ems.last_ok_at)}` + (d.ems.last_error ? ` · ${d.ems.last_error}` : "");
    document.getElementById("st-devices").textContent = `${d.devices.online_now}/${d.devices.registered}`;
    document.getElementById("st-devices-detail").textContent =
      `${d.devices.pending} discovered pending · ${d.devices.disabled} disabled`;
    document.getElementById("st-students").textContent = d.students.total;
    document.getElementById("st-mappings").textContent =
      `${d.students.active} active · ${d.students.mappings} biometric mappings`;
    document.getElementById("st-pending").textContent = d.events.pending + d.events.failed;
    document.getElementById("st-queue").textContent = `${d.queue_pending} queued tasks`;
    document.getElementById("st-synced").textContent = d.events.synced;
    document.getElementById("st-lastsync").textContent = `last sync ${fmtAgo(d.last_synced_at)}`;
    document.getElementById("st-failed").textContent = `${d.events.review} (needs review)`;
    const tbody = document.querySelector("#attempts-table tbody");
    tbody.innerHTML = (d.last_attempts || []).map(a => `<tr>
      <td>${esc(fmtTs(a.attempted_at))}</td><td>${esc(a.attempt_type)}</td>
      <td>${esc(a.event_count)}</td><td><span class="tag ${esc(a.result)}">${esc(a.result)}</span></td>
      <td>${esc(a.detail || a.error || "")}</td></tr>`).join("") ||
      `<tr><td colspan="5" class="muted">no sync attempts yet</td></tr>`;
  } catch (err) {
    setEmsPill(null, "…");
  }
}

/* -------------------------------------------------------------- devices ---- */
let scanBusy = false;

async function loadDevices() {
  const d = await api("/api/devices");
  const tbody = document.querySelector("#devices-table tbody");
  tbody.innerHTML = d.devices.map(dev => {
    const status = dev.status === "registered"
      ? `<span class="tag ${dev.online_now ? "registered" : "pending"}">${dev.online_now ? "online" : "registered"}</span>`
      : `<span class="tag ${dev.status}">${dev.status}</span>`;
    const ipCell = esc(dev.ip_address || "—") + (dev.ip_updated_at && dev.status !== "pending"
      ? ` <span class="muted" title="auto-repaired ${esc(fmtTs(dev.ip_updated_at))}">↻</span>` : "");
    const actions = [];
    if (dev.status === "pending" && dev.serial_number)
      actions.push(`<button data-act="register" data-id="${dev.id}" class="primary">Register</button>`);
    if (dev.status === "registered") {
      actions.push(`<button data-act="test" data-id="${dev.id}">Test</button>`);
      actions.push(`<button data-act="disable" data-id="${dev.id}">Disable</button>`);
    }
    if (dev.status === "disabled")
      actions.push(`<button data-act="enable" data-id="${dev.id}" class="primary">Enable</button>`);
    if (dev.status !== "registered")
      actions.push(`<button data-act="remove" data-id="${dev.id}" class="danger">Remove</button>`);
    return `<tr>
      <td>${status}</td>
      <td>${esc(dev.label || "")} <button data-act="label" data-id="${dev.id}" class="mini">✎</button></td>
      <td>${esc(dev.serial_number || "?")}</td>
      <td>${esc(dev.model || "—")}</td>
      <td>${ipCell}</td><td>${esc(dev.port)}</td>
      <td>${esc(dev.mac_address || "—")}</td>
      <td>${esc(dev.firmware_version || "—")}</td>
      <td title="${esc(fmtTs(dev.last_seen_at))}">${fmtAgo(dev.last_seen_at)}</td>
      <td>${esc(dev.event_count)}</td>
      <td>${actions.join(" ")}</td></tr>`;
  }).join("") || `<tr><td colspan="11" class="muted">No devices yet — click “Scan LAN now”.</td></tr>`;

  const ls = document.getElementById("last-scan");
  if (d.last_scan) {
    const s = d.last_scan;
    ls.innerHTML =
      `<div>Scan at ${esc(fmtTs(s.started_at))} — probed ${esc(s.hosts_probed)} hosts, ` +
      `${esc(s.tcp_open)} with port 4370 open, ${esc(s.identified.length)} identified, ` +
      `${esc(s.ip_changes.length)} IP repairs, ${esc(s.new_pending.length)} new pending.</div>` +
      (s.ip_changes.length ? `<div>IP repairs: ${s.ip_changes.map(c =>
        `<span class="tag pending">${esc(c.old_ip)} → ${esc(c.new_ip)} (${esc(c.serial)})</span>`).join(" ")}</div>` : "") +
      (s.errors.length ? `<div class="notice error">${esc(s.errors.join("; "))}</div>` : "");
  }
}

async function runScan() {
  if (scanBusy) return;
  scanBusy = true;
  document.getElementById("scan-status").textContent = "scanning LAN…";
  try {
    const d = await api("/api/scan", { method: "POST", form: new URLSearchParams({ blocking: "1" }) });
    const r = d.report;
    flash(`Scan complete: ${r.hosts_probed} hosts probed, ${r.identified.length} terminals identified, ${r.ip_changes.length} IP repairs.`);
  } catch (err) {
    flash(`Scan failed: ${err.message}`, true);
  } finally {
    scanBusy = false;
    document.getElementById("scan-status").textContent = "";
    loadDevices();
  }
}

async function deviceAction(act, id) {
  const btn = document.querySelector(`button[data-act="${act}"][data-id="${id}"]`);
  try {
    if (act === "label") {
      const label = prompt("Device label (e.g. Main gate K40):");
      if (label === null) return;
      await api(`/api/devices/${id}/label`, { method: "POST", form: new URLSearchParams({ label }) });
    } else if (act === "register") {
      const label = prompt("Label for this device (optional):", "K40");
      await api(`/api/devices/${id}/register`, { method: "POST", form: new URLSearchParams({ label: label || "" }) });
      flash("Device registered — collection starts automatically.");
    } else if (act === "test") {
      if (btn) btn.disabled = true;
      const d = await api(`/api/devices/${id}/test`, { method: "POST" });
      flash(`Test OK in ${d.elapsed_ms}ms — serial ${d.serial}, model ${d.model || "?"}, registered-match: ${d.serial_matches_registered ? "YES" : "NO"}`);
    } else if (act === "remove") {
      if (!confirm("Remove this device? (refused automatically if it has attendance history)")) return;
      await api(`/api/devices/${id}/remove`, { method: "POST" });
    } else {
      await api(`/api/devices/${id}/${act}`, { method: "POST" });
    }
    loadDevices();
  } catch (err) {
    flash(err.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* -------------------------------------------------------------- students --- */
const st = { page: 1, pageSize: 50, sort: "name", order: "asc" };

async function loadStudents() {
  const q = document.getElementById("student-search").value.trim();
  const cls = document.getElementById("filter-class").value;
  const sec = document.getElementById("filter-section").value;
  const act = document.getElementById("filter-active").value;
  const params = new URLSearchParams({
    page: st.page, page_size: st.pageSize, sort: st.sort, order: st.order, active: act,
  });
  if (q) params.set("search", q);
  if (cls) params.set("class", cls);
  if (sec) params.set("section", sec);
  const d = await api("/api/students?" + params);

  const errBox = document.getElementById("roster-error");
  if (d.roster_last_error) {
    errBox.textContent = `Roster problem: ${d.roster_last_error}`;
    errBox.classList.remove("hidden");
  } else {
    errBox.classList.add("hidden");
  }
  document.getElementById("roster-status").textContent =
    d.roster_last_sync ? `roster updated ${fmtAgo(d.roster_last_sync)} · ${d.total} students` : `${d.total} students`;

  // class/section filters from real data
  const clsSel = document.getElementById("filter-class");
  if (!clsSel.dataset.loaded) {
    for (const c of d.classes) {
      const opt = document.createElement("option");
      opt.value = c.class; opt.textContent = c.class;
      clsSel.appendChild(opt);
    }
    clsSel.dataset.loaded = "1";
  }
  const secSel = document.getElementById("filter-section");
  const current = secSel.value;
  secSel.innerHTML = '<option value="">Section: all</option>';
  const chosen = d.classes.find(c => c.class === clsSel.value);
  for (const s of (chosen ? chosen.sections : d.classes.flatMap(c => c.sections))) {
    const opt = document.createElement("option");
    opt.value = s; opt.textContent = s;
    secSel.appendChild(opt);
  }
  secSel.value = current;

  const tbody = document.querySelector("#students-table tbody");
  tbody.innerHTML = d.students.map(s => `<tr>
    <td>${esc(s.name)}</td><td>${esc(s.ems_student_id)}</td>
    <td>${esc(s.admission_no || "—")}</td><td>${esc(s.roll_no || "—")}</td>
    <td>${esc(s.class_name || "—")}</td><td>${esc(s.section || "—")}</td>
    <td><span class="tag ${s.active ? "active" : "inactive"}">${s.active ? "active" : "inactive"}</span></td>
    <td>${esc(s.device_users || "—")}</td>
    <td>${s.device_users
      ? `<span class="tag resolved">mapped</span> <button data-unmap-student="${esc(s.ems_student_id)}">edit</button>`
      : `<button data-map-student="${esc(s.ems_student_id)}" data-name="${esc(s.name)}">map…</button>`}</td>
  </tr>`).join("") || `<tr><td colspan="9" class="muted">No students. If the EMS roster API exists, use “Refresh from EMS”. The bridge never shows invented students.</td></tr>`;

  document.getElementById("page-info").textContent =
    `page ${d.page} / ${Math.max(1, Math.ceil(d.total / d.page_size))} (${d.total})`;

  const dl = document.getElementById("students-datalist");
  dl.innerHTML = d.students.map(s =>
    `<option value="${esc(s.ems_student_id)}">${esc(s.name)} (${esc(s.class_name || "?")}${esc(s.section || "")}, ID ${esc(s.ems_student_id)})</option>`).join("");

  await loadMappingReview();
}

async function loadMappingReview() {
  const d = await api("/api/mappings/review");
  const r = d.review;
  const un = document.querySelector("#unmapped-table tbody");
  un.innerHTML = (r.unmapped_device_users || []).map(u => `<tr>
    <td>${esc(u.device_serial)}</td><td>${esc(u.device_user_id)}</td>
    <td>${esc(u.events)}</td><td>${esc(fmtTs(u.last_ts))}</td>
    <td><button data-map-device="${esc(u.device_serial)}" data-user="${esc(u.device_user_id)}">map…</button></td>
  </tr>`).join("") || `<tr><td colspan="5" class="muted">all device users are mapped 🎉</td></tr>`;

  const box = document.getElementById("mapping-review");
  let html = "";
  if (r.conflicts?.length) {
    html += `<div class="notice warn"><strong>Same student mapped to multiple device users:</strong><ul>` +
      r.conflicts.map(c => `<li>student ${esc(c.ems_student_id)} ⇐ ${esc(c.bindings)}</li>`).join("") + `</ul></div>`;
  }
  if (r.unknown_or_inactive_students?.length) {
    html += `<div class="notice warn"><strong>Mappings to unknown/inactive students:</strong><ul>` +
      r.unknown_or_inactive_students.map(m =>
        `<li>${esc(m.device_serial)}:${esc(m.device_user_id)} → ${esc(m.ems_student_id)} ${m.student_name ? "(" + esc(m.student_name) + ", inactive)" : "(not in roster)"} <button data-del-mapping="${m.id}">remove</button></li>`).join("") + `</ul></div>`;
  }
  box.innerHTML = html || `<div class="muted">No conflicts.</div>`;
}

function openMapDialog({ serial = "", user = "", student = "" } = {}) {
  const dlg = document.getElementById("map-dialog");
  dlg.querySelector('[name=device_serial]').value = serial;
  dlg.querySelector('[name=device_user_id]').value = user;
  dlg.querySelector('[name=ems_student_id]').value = student;
  if (user) dlg.querySelector('[name=device_user_id]').readOnly = true;
  else dlg.querySelector('[name=device_user_id]').readOnly = false;
  dlg.showModal();
}

/* ------------------------------------------------------------- attendance -- */
const ev = { page: 1, pageSize: 50 };

async function loadEvents() {
  const params = new URLSearchParams({ page: ev.page, page_size: ev.pageSize });
  const q = document.getElementById("event-search").value.trim();
  const status = document.getElementById("event-status").value;
  if (q) params.set("search", q);
  if (status) params.set("status", status);
  const d = await api("/api/events?" + params);
  const tbody = document.querySelector("#events-table tbody");
  tbody.innerHTML = d.events.map(e => `<tr>
    <td>${["failed", "review", "rejected"].includes(e.sync_status) ? `<input type="checkbox" class="ev-check" value="${e.id}">` : ""}</td>
    <td>${esc(fmtTs(e.event_timestamp))} <span class="muted">${esc(e.event_timestamp_tz)}</span></td>
    <td>${e.student_name ? esc(e.student_name) : `<span class="tag unmapped">unmapped</span>`}
      <div class="muted">${esc(e.ems_student_id || "")}</div></td>
    <td>${esc(e.device_user_id)}</td>
    <td>${esc(e.device_label || e.device_serial)}</td>
    <td class="muted">${esc(e.punch ?? "?")}/${esc(e.status ?? "?")}</td>
    <td><span class="tag ${esc(e.sync_status)}">${esc(e.sync_status)}</span></td>
    <td>${esc(e.retry_count)}</td>
    <td class="muted" title="${esc(e.last_error || "")}">${esc((e.last_error || "").slice(0, 60))}</td>
    <td class="muted">${esc(e.remote_id || "—")}</td>
  </tr>`).join("") || `<tr><td colspan="10" class="muted">No attendance records yet.</td></tr>`;
  document.getElementById("page-info").textContent =
    `page ${d.page} / ${Math.max(1, Math.ceil(d.total / d.page_size))} (${d.total})`;
  document.getElementById("select-all").checked = false;
}

async function retrySelected() {
  const ids = [...document.querySelectorAll(".ev-check:checked")].map(c => Number(c.value));
  if (!ids.length) return flash("Select failed/review events first (checkbox column).", true);
  const d = await api("/api/events/retry", { method: "POST", json: { ids } });
  flash(`${d.updated} events re-queued for upload.`);
  loadEvents();
}

/* ------------------------------------------------------------------ logs --- */
const lg = { page: 1, pageSize: 50 };

async function loadLogs() {
  const params = new URLSearchParams({ page: lg.page, page_size: lg.pageSize });
  const q = document.getElementById("log-search").value.trim();
  const action = document.getElementById("log-action").value;
  if (q) params.set("search", q);
  if (action) params.set("action", action);
  const d = await api("/api/logs?" + params);
  const sel = document.getElementById("log-action");
  if (!sel.dataset.loaded) {
    for (const a of d.actions) {
      const opt = document.createElement("option");
      opt.value = a; opt.textContent = a;
      sel.appendChild(opt);
    }
    sel.dataset.loaded = "1";
  }
  document.querySelector("#audit-table tbody").innerHTML = d.logs.map(l => `<tr>
    <td>${esc(fmtTs(l.created_at))}</td><td>${esc(l.actor)}</td>
    <td><code>${esc(l.action)}</code></td><td>${esc(l.entity_id || l.entity || "")}</td>
    <td>${esc(l.detail || "")}</td></tr>`).join("") ||
    `<tr><td colspan="5" class="muted">no audit entries</td></tr>`;
  document.getElementById("page-info").textContent =
    `page ${lg.page} / ${Math.max(1, Math.ceil(d.total / lg.page_size))} (${d.total})`;
  document.querySelector("#attempts-table tbody").innerHTML = d.attempts.map(a => `<tr>
    <td>${esc(fmtTs(a.attempted_at))}</td><td>${esc(a.attempt_type)}</td><td>${esc(a.event_count)}</td>
    <td><span class="tag ${esc(a.result)}">${esc(a.result)}</span></td>
    <td>${esc(a.error || a.detail || "")}</td><td>${esc(a.duration_ms ?? "")}</td></tr>`).join("");
}

async function loadAppLog() {
  const q = document.getElementById("app-log-search").value.trim();
  const d = await api("/api/logs/app" + (q ? "?search=" + encodeURIComponent(q) : ""));
  document.getElementById("app-log").textContent = d.lines.join("\n") || "(log file is empty or not configured)";
}

/* --------------------------------------------------------------- settings -- */
async function loadSettings() {
  const d = await api("/api/settings");
  const form = document.getElementById("settings-form");
  for (const [key, val] of Object.entries(d.settings)) {
    const input = form.querySelector(`[name=${CSS.escape(key)}]`);
    if (input && val !== null && val !== undefined) input.value = val;
  }
  document.getElementById("token-configured").textContent = d.token_configured ? "yes (in credential store)" : "NO — run set-token";
}

async function saveSettings(ev) {
  ev.preventDefault();
  const form = ev.target;
  const payload = {};
  for (const el of form.querySelectorAll("[name]")) payload[el.name] = el.value;
  try {
    const d = await api("/api/settings", { method: "POST", json: payload });
    document.getElementById("settings-status").textContent = d.changed.length
      ? `saved: ${d.changed.join(", ")}` : "no changes";
    flash("Settings saved.");
  } catch (err) {
    flash(err.message, true);
  }
}

async function testEms() {
  const box = document.getElementById("ems-test-result");
  box.innerHTML = "probing…";
  try {
    const d = await api("/api/settings/ems-test", { method: "POST" });
    const p = d.probe;
    box.innerHTML = `<div class="card" style="margin:10px 0"><pre>${esc(JSON.stringify(p, null, 2))}</pre>` +
      (d.health ? `<div class="notice ${d.health.ok ? "warn" : "error"}">health: ${esc(d.health.ok ? "OK " + (d.health.server_time || "") : d.health.detail)}</div>` : "") + `</div>`;
  } catch (err) {
    box.innerHTML = `<div class="notice error">${esc(err.message)}</div>`;
  }
}

/* ------------------------------------------------------------------- init -- */
document.addEventListener("DOMContentLoaded", () => {
  const page = document.body.dataset.page;

  if (page === "overview") {
    loadOverview();
    setInterval(loadOverview, 5000);
  }

  if (page === "devices") {
    document.getElementById("btn-scan").addEventListener("click", runScan);
    const dlg = document.getElementById("manual-dialog");
    document.getElementById("btn-manual-add").addEventListener("click", () => dlg.showModal());
    document.getElementById("manual-cancel").addEventListener("click", () => dlg.close());
    document.getElementById("manual-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const d = await api("/api/devices/manual", { method: "POST", form: new FormData(e.target) });
        dlg.close();
        flash(`Device verified: serial ${d.device.serial_number} (${d.device.model || "model unknown"}). Register it when ready.`);
        loadDevices();
      } catch (err) { flash(err.message, true); }
    });
    document.getElementById("devices-table").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-act]");
      if (btn) deviceAction(btn.dataset.act, btn.dataset.id);
    });
    loadDevices();
    setInterval(loadDevices, 15000);
  }

  if (page === "students") {
    let t;
    document.getElementById("student-search").addEventListener("input", () => {
      clearTimeout(t); t = setTimeout(() => { st.page = 1; loadStudents(); }, 300);
    });
    for (const id of ["filter-class", "filter-section", "filter-active"])
      document.getElementById(id).addEventListener("change", () => { st.page = 1; loadStudents(); });
    document.getElementById("btn-refresh-roster").addEventListener("click", async () => {
      try {
        const d = await api("/api/students/refresh", { method: "POST" });
        flash(`Roster refreshed: ${d.stats.fetched} students (${d.stats.inserted} new).`);
      } catch (err) { flash(err.message, true); }
      loadStudents();
    });
    document.getElementById("prev-page").addEventListener("click", () => { if (st.page > 1) { st.page--; loadStudents(); } });
    document.getElementById("next-page").addEventListener("click", () => { st.page++; loadStudents(); });
    document.getElementById("map-cancel").addEventListener("click", () => document.getElementById("map-dialog").close());
    document.getElementById("map-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      try {
        await api("/api/mappings", {
          method: "POST", json: {
            device_serial: f.device_serial.value,
            device_user_id: f.device_user_id.value,
            ems_student_id: f.ems_student_id.value.split(" ")[0],
          }
        });
        document.getElementById("map-dialog").close();
        flash("Mapping saved.");
        loadStudents();
      } catch (err) { flash(err.message, true); }
    });
    document.getElementById("csv-import-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const box = document.getElementById("csv-import-result");
      try {
        const d = await api("/api/mappings/import", { method: "POST", form: new FormData(e.target) });
        box.textContent = `imported ${d.result.imported} mappings`;
        flash(`Imported ${d.result.imported} mappings.`);
        loadStudents();
      } catch (err) {
        box.textContent = err.message;
        flash("CSV import rejected — see details on the page.", true);
      }
    });
    document.body.addEventListener("click", (e) => {
      const mapDev = e.target.closest("[data-map-device]");
      const mapStu = e.target.closest("[data-map-student]");
      const delMap = e.target.closest("[data-del-mapping]");
      if (mapDev) openMapDialog({ serial: mapDev.dataset.mapDevice, user: mapDev.dataset.user });
      if (mapStu) openMapDialog({ student: mapStu.dataset.mapStudent });
      if (delMap) {
        api(`/api/mappings/${delMap.dataset.delMapping}`, { method: "DELETE" })
          .then(() => { flash("Mapping removed."); loadStudents(); })
          .catch(err => flash(err.message, true));
      }
    });
    loadStudents();
    setInterval(loadMappingReview, 20000);
  }

  if (page === "attendance") {
    let t;
    document.getElementById("event-search").addEventListener("input", () => {
      clearTimeout(t); t = setTimeout(() => { ev.page = 1; loadEvents(); }, 300);
    });
    document.getElementById("event-status").addEventListener("change", () => { ev.page = 1; loadEvents(); });
    document.getElementById("btn-retry-selected").addEventListener("click", retrySelected);
    document.getElementById("prev-page").addEventListener("click", () => { if (ev.page > 1) { ev.page--; loadEvents(); } });
    document.getElementById("next-page").addEventListener("click", () => { ev.page++; loadEvents(); });
    document.getElementById("select-all").addEventListener("change", (e) => {
      document.querySelectorAll(".ev-check").forEach(c => c.checked = e.target.checked);
    });
    loadEvents();
    setInterval(loadEvents, 10000);
  }

  if (page === "logs") {
    let t;
    document.getElementById("log-search").addEventListener("input", () => {
      clearTimeout(t); t = setTimeout(() => { lg.page = 1; loadLogs(); }, 300);
    });
    document.getElementById("log-action").addEventListener("change", () => { lg.page = 1; loadLogs(); });
    document.getElementById("app-log-search").addEventListener("input", () => {
      clearTimeout(t); t = setTimeout(loadAppLog, 300);
    });
    document.getElementById("prev-page").addEventListener("click", () => { if (lg.page > 1) { lg.page--; loadLogs(); } });
    document.getElementById("next-page").addEventListener("click", () => { lg.page++; loadLogs(); });
    loadLogs();
    loadAppLog();
  }

  if (page === "settings") {
    document.getElementById("settings-form").addEventListener("submit", saveSettings);
    document.getElementById("btn-ems-test").addEventListener("click", testEms);
    document.getElementById("btn-backup").addEventListener("click", async () => {
      try {
        const d = await api("/api/backup", { method: "POST" });
        document.getElementById("backup-status").textContent = `backup written to ${d.backup}`;
        flash("Backup complete.");
      } catch (err) { flash(err.message, true); }
    });
    document.getElementById("btn-diag").addEventListener("click", async () => {
      try {
        const d = await api("/api/diagnostics/export", { method: "POST" });
        const link = document.getElementById("diag-download");
        link.href = `/api/diagnostics/download/${encodeURIComponent(d.file)}`;
        link.classList.remove("hidden");
        flash("Diagnostics bundle ready.");
      } catch (err) { flash(err.message, true); }
    });
    loadSettings();
  }
});
