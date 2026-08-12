/* Operator panel client. No frameworks, no build step. Renders the JSON
 * snapshot pushed over /ws into the DOM, and posts operator actions
 * (setpoint entry, APC toggle) back to the REST endpoints in main.py.
 */

const unitsEl = document.getElementById("units");
const alarmsBody = document.getElementById("alarms-body");
const apcStatusEl = document.getElementById("apc-status");
const apcSolveEl = document.getElementById("apc-solvetime");
const apcToggleEl = document.getElementById("apc-toggle");
const dcsConnEl = document.getElementById("dcs-conn");
const wsStatusEl = document.getElementById("ws-status");
const serverTimeEl = document.getElementById("server-time");
const clockEl = document.getElementById("clock");

let lastSnapshot = null;
let apcEnabled = false;

function fmt(value, decimals, unit) {
  if (value === null || value === undefined) return "---";
  if (typeof value === "number") {
    return value.toFixed(decimals) + (unit ? " " + unit : "");
  }
  return String(value);
}

function badgeClass(kind) {
  if (kind === "ok") return "badge badge-ok";
  if (kind === "warn") return "badge badge-warn";
  if (kind === "bad") return "badge badge-bad";
  return "badge badge-unknown";
}

function tankLevelPct(pv, hh) {
  const span = hh && hh > 0 ? hh : 10;
  const pct = (pv / span) * 100;
  return Math.max(0, Math.min(100, pct));
}

function buildTrendSvg(history, height, width) {
  if (!history || history.length < 2) {
    return `<svg class="trend" viewBox="0 0 ${width} ${height}"></svg>`;
  }
  const t0 = history[0][0];
  const t1 = history[history.length - 1][0];
  const tSpan = Math.max(1, t1 - t0);

  let vMin = Infinity, vMax = -Infinity;
  for (const [, pv, sp] of history) {
    for (const v of [pv, sp]) {
      if (typeof v === "number") {
        vMin = Math.min(vMin, v);
        vMax = Math.max(vMax, v);
      }
    }
  }
  if (!isFinite(vMin)) { vMin = 0; vMax = 1; }
  if (vMax - vMin < 0.1) { vMax += 0.5; vMin -= 0.5; }

  const x = (t) => ((t - t0) / tSpan) * width;
  const y = (v) => height - ((v - vMin) / (vMax - vMin)) * height;

  const pvPts = history
    .filter((row) => typeof row[1] === "number")
    .map((row) => `${x(row[0]).toFixed(1)},${y(row[1]).toFixed(1)}`)
    .join(" ");
  const spPts = history
    .filter((row) => typeof row[2] === "number")
    .map((row) => `${x(row[0]).toFixed(1)},${y(row[2]).toFixed(1)}`)
    .join(" ");

  return `<svg class="trend" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
    <polyline points="${spPts}" fill="none" stroke="#e0b12e" stroke-width="1" />
    <polyline points="${pvPts}" fill="none" stroke="#3d8bd4" stroke-width="1.5" />
  </svg>`;
}

function renderUnit(unit) {
  const v = unit.values || {};
  const connected = unit.connected;
  const alive = unit.alive;

  let connBadge = badgeClass("bad");
  let connText = "OFFLINE";
  if (connected && alive) { connBadge = badgeClass("ok"); connText = "ONLINE"; }
  else if (connected && !alive) { connBadge = badgeClass("warn"); connText = "STALE"; }

  const pv = v["Level.PV"];
  const sp = v["Level.SP"];
  const hh = v["Level.HH"];
  const ll = v["Level.LL"];
  const fillPct = tankLevelPct(pv ?? 0, hh);
  const spPct = tankLevelPct(sp ?? 0, hh);

  const mode = v["PID.Mode"] || "---";
  const trip = v["Interlock.Trip"];

  return `
  <div class="unit-card ${connected ? "" : "disconnected"}" data-unit="${unit.unit_id}">
    <div class="unit-head">
      <span class="unit-name">UNIT ${unit.unit_id}</span>
      <span class="${connBadge}">${connText}</span>
    </div>
    <div class="mimic">
      <div class="tank">
        <div class="tank-fill" style="height:${fillPct}%"></div>
        <div class="tank-sp-line" style="bottom:${spPct}%"></div>
      </div>
      <div class="readout-grid">
        <span class="k">LEVEL PV</span><span class="v">${fmt(pv, 2, "m")}</span>
        <span class="k">LEVEL SP</span><span class="v">${fmt(sp, 2, "m")}</span>
        <span class="k">PUMP CMD</span><span class="v">${fmt(v["Pump.CMD"], 1, "%")}</span>
        <span class="k">VALVE CMD</span><span class="v">${fmt(v["Valve.CMD"], 1, "%")}</span>
        <span class="k">PID OUT</span><span class="v">${fmt(v["PID.OUT"], 1, "%")}</span>
        <span class="k">MODE</span><span class="v"><span class="mode-tag">${mode}</span></span>
        <span class="k">HEARTBEAT</span><span class="v">${fmt(v["Status.Heartbeat"], 0)}</span>
        <span class="k">SCAN</span><span class="v">${fmt(v["Status.ScanTime_ms"], 0, "ms")}</span>
      </div>
    </div>
    ${buildTrendSvg(unit.history, 70, 300)}
    <div class="sp-row">
      <span class="label mono">MANUAL SP</span>
      <input type="number" step="0.1" data-sp-input="${unit.unit_id}" placeholder="${fmt(sp, 2)}">
      <span class="mono">m</span>
      <button data-sp-submit="${unit.unit_id}" ${connected ? "" : "disabled"}>WRITE</button>
    </div>
  </div>`;
}

function renderAlarms(snapshot) {
  const rows = [];
  for (const uid of Object.keys(snapshot.units)) {
    const unit = snapshot.units[uid];
    const v = unit.values || {};
    if (v["Interlock.Trip"]) {
      rows.push({ cls: "trip", unit: `UNIT ${uid}`, type: "TRIP", detail: v["Interlock.Reason"] || "unspecified" });
    }
    if (!unit.connected) {
      rows.push({ cls: "conn-loss", unit: `UNIT ${uid}`, type: "COMM LOSS", detail: unit.error || "not connected" });
    } else if (!unit.alive) {
      rows.push({ cls: "conn-loss", unit: `UNIT ${uid}`, type: "STALE HEARTBEAT", detail: "no scan activity" });
    }
  }
  if (!snapshot.dcs.connected) {
    rows.push({ cls: "conn-loss", unit: "DCS", type: "COMM LOSS", detail: snapshot.dcs.error || "not connected" });
  }
  if (snapshot.dcs.values && snapshot.dcs.values["APC.Status"] === "SOLVER_FAIL") {
    rows.push({ cls: "trip", unit: "DCS", type: "SOLVER FAIL", detail: "last MPC solve did not converge, holding last setpoints" });
  }

  if (rows.length === 0) {
    alarmsBody.innerHTML = `<tr><td colspan="3" class="mono">NO ACTIVE ALARMS</td></tr>`;
    return;
  }
  alarmsBody.innerHTML = rows
    .map((r) => `<tr class="${r.cls}"><td>${r.unit}</td><td>${r.type}</td><td>${r.detail}</td></tr>`)
    .join("");
}

function renderTopbar(snapshot) {
  const dcs = snapshot.dcs;
  const v = dcs.values || {};
  const status = v["APC.Status"] || "UNKNOWN";

  let cls = "badge-unknown";
  if (status === "OK") cls = "badge-ok";
  else if (status === "DEGRADED") cls = "badge-warn";
  else if (status === "SOLVER_FAIL") cls = "badge-bad";
  else if (status === "DISABLED") cls = "badge-unknown";

  apcStatusEl.className = `badge ${cls}`;
  apcStatusEl.textContent = status;
  apcSolveEl.textContent = `SOLVE: ${fmt(v["APC.SolveTime_ms"], 0, "ms")}`;

  dcsConnEl.className = `badge ${dcs.connected ? "badge-ok" : "badge-bad"}`;
  dcsConnEl.textContent = dcs.connected ? "DCS: LINK OK" : "DCS: NO LINK";

  apcEnabled = !!v["APC.Enabled"];
  apcToggleEl.disabled = !dcs.connected;
  apcToggleEl.textContent = apcEnabled ? "APC: ENABLED (click to disable)" : "APC: DISABLED (click to enable)";
}

function render(snapshot) {
  lastSnapshot = snapshot;
  const order = Object.keys(snapshot.units).sort((a, b) => Number(a) - Number(b));
  unitsEl.innerHTML = order.map((uid) => renderUnit(snapshot.units[uid])).join("");
  renderAlarms(snapshot);
  renderTopbar(snapshot);
  serverTimeEl.textContent = "SERVER: " + new Date(snapshot.server_time * 1000).toLocaleTimeString();
}

unitsEl.addEventListener("click", async (ev) => {
  const uid = ev.target.getAttribute("data-sp-submit");
  if (!uid) return;
  const input = unitsEl.querySelector(`[data-sp-input="${uid}"]`);
  const value = parseFloat(input.value);
  if (Number.isNaN(value)) return;
  ev.target.disabled = true;
  try {
    const resp = await fetch(`/api/units/${uid}/setpoint`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      alert(`SP write failed: ${body.error || resp.status}`);
    } else {
      input.value = "";
    }
  } finally {
    ev.target.disabled = false;
  }
});

apcToggleEl.addEventListener("click", async () => {
  apcToggleEl.disabled = true;
  try {
    const resp = await fetch("/api/apc/enabled", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !apcEnabled }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      alert(`APC toggle failed: ${body.error || resp.status}`);
    }
  } finally {
    apcToggleEl.disabled = lastSnapshot ? !lastSnapshot.dcs.connected : true;
  }
});

function connectWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws`);

  ws.onopen = () => { wsStatusEl.textContent = "WS: CONNECTED"; };
  ws.onclose = () => {
    wsStatusEl.textContent = "WS: DISCONNECTED, retrying...";
    setTimeout(connectWs, 2000);
  };
  ws.onerror = () => { ws.close(); };
  ws.onmessage = (ev) => {
    try {
      render(JSON.parse(ev.data));
    } catch (e) {
      console.error("bad snapshot", e);
    }
  };
}

setInterval(() => {
  clockEl.textContent = new Date().toLocaleTimeString();
}, 1000);

connectWs();
