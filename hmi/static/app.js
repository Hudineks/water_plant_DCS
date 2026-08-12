/* Operator panel client. No frameworks, no build step. Renders the JSON
 * snapshot pushed over /ws into the DOM, and posts operator actions
 * (setpoint entry, cycle selection) back to the REST endpoints in main.py.
 */

const unitsEl = document.getElementById("units");
const alarmsBody = document.getElementById("alarms-body");
const apcStatusEl = document.getElementById("apc-status");
const apcSolveEl = document.getElementById("apc-solvetime");
const dcsConnEl = document.getElementById("dcs-conn");
const wsStatusEl = document.getElementById("ws-status");
const serverTimeEl = document.getElementById("server-time");
const clockEl = document.getElementById("clock");

let lastSnapshot = null;

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

/* --- Focus preservation across full-innerHTML re-renders ---
 * render() rebuilds #units from scratch on every websocket push (~1/s).
 * A plain rebuild destroys and recreates every <input>/<select>, so
 * anything the operator is mid-typing into is lost before they can submit
 * it. captureFocusedInput()/restoreFocusedInput() save and reapply focus,
 * value, and cursor position across one rebuild, for any element tagged
 * with one of the data-* attributes below. This is the fix for "the
 * manual SP field doesn't hold what I type."
 */
const FOCUS_TRACK_ATTRS = ["data-sp-input", "data-cycle-select", "data-manual-target"];

function captureFocusedInput(container) {
  const el = document.activeElement;
  if (!el || !container.contains(el)) return null;
  for (const attr of FOCUS_TRACK_ATTRS) {
    const val = el.getAttribute(attr);
    if (val !== null) {
      return { attr, val, value: el.value, selStart: el.selectionStart, selEnd: el.selectionEnd };
    }
  }
  return null;
}

function restoreFocusedInput(container, captured) {
  if (!captured) return;
  const el = container.querySelector(`[${captured.attr}="${captured.val}"]`);
  if (!el) return;
  el.value = captured.value;
  el.focus();
  if (typeof captured.selStart === "number" && el.setSelectionRange) {
    try { el.setSelectionRange(captured.selStart, captured.selEnd); } catch (e) { /* not a text-selectable input */ }
  }
}

const AXIS_LABEL_COLOR = "#8a97a3";
const AXIS_LINE_COLOR = "#24303a";
const ACTIVE_SHADE_COLOR = "#3d8bd4";

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
  if (vMax - vMin < 0.02) { vMax += 0.01; vMin -= 0.01; }

  const marginLeft = 36, marginBottom = 13, marginTop = 3;
  const plotW = Math.max(10, width - marginLeft);
  const plotH = Math.max(10, height - marginBottom - marginTop);

  const x = (t) => marginLeft + ((t - t0) / tSpan) * plotW;
  const y = (v) => marginTop + plotH - ((v - vMin) / (vMax - vMin)) * plotH;

  // Active-intervention shading: contiguous stretches where the 4th
  // history field (cycle_name at that time) was not "off", i.e. the DCS
  // was actually driving this unit's PID.SP.
  const shadeRects = [];
  let segStart = null;
  for (let i = 0; i < history.length; i++) {
    const active = !!history[i][3] && history[i][3] !== "off";
    if (active && segStart === null) {
      segStart = history[i][0];
    } else if (!active && segStart !== null) {
      shadeRects.push([segStart, history[i][0]]);
      segStart = null;
    }
  }
  if (segStart !== null) shadeRects.push([segStart, t1]);

  const shadeSvg = shadeRects
    .map(([a, b]) => {
      const xa = x(a), xb = x(b);
      return `<rect x="${xa.toFixed(1)}" y="${marginTop}" width="${Math.max(0.5, xb - xa).toFixed(1)}" height="${plotH}" fill="${ACTIVE_SHADE_COLOR}" opacity="0.12" />`;
    })
    .join("");

  const pvPts = history
    .filter((row) => typeof row[1] === "number")
    .map((row) => `${x(row[0]).toFixed(1)},${y(row[1]).toFixed(1)}`)
    .join(" ");
  const spPts = history
    .filter((row) => typeof row[2] === "number")
    .map((row) => `${x(row[0]).toFixed(1)},${y(row[2]).toFixed(1)}`)
    .join(" ");

  const yTicks = [vMax, (vMin + vMax) / 2, vMin];
  const yLabels = yTicks
    .map((v) => `<text x="${(marginLeft - 4).toFixed(1)}" y="${(y(v) + 3).toFixed(1)}" text-anchor="end" font-family="Consolas,monospace" font-size="9" fill="${AXIS_LABEL_COLOR}">${v.toFixed(2)}</text>`)
    .join("");

  const xTickFracs = [0, 0.5, 1];
  const xAnchors = ["start", "middle", "end"];
  const xLabels = xTickFracs
    .map((frac, i) => {
      const t = t0 + frac * tSpan;
      const minsAgo = Math.round((t1 - t) / 60);
      const label = minsAgo <= 0 ? "now" : `-${minsAgo}m`;
      return `<text x="${x(t).toFixed(1)}" y="${height - 2}" text-anchor="${xAnchors[i]}" font-family="Consolas,monospace" font-size="9" fill="${AXIS_LABEL_COLOR}">${label}</text>`;
    })
    .join("");

  return `<svg class="trend" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
    ${shadeSvg}
    <line x1="${marginLeft}" y1="${marginTop}" x2="${marginLeft}" y2="${marginTop + plotH}" stroke="${AXIS_LINE_COLOR}" stroke-width="1" />
    <polyline points="${spPts}" fill="none" stroke="#e0b12e" stroke-width="1" />
    <polyline points="${pvPts}" fill="none" stroke="#3d8bd4" stroke-width="1.5" />
    ${yLabels}
    ${xLabels}
  </svg>`;
}

const CYCLE_OPTIONS = ["off", "step", "ramp", "manual"];

function renderUnit(unit, h1Estimated, control) {
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
  const h1Pct = tankLevelPct(h1Estimated ?? 0, hh);

  const mode = v["PID.Mode"] || "---";

  const cycleName = (control && control.cycle_name) || "off";
  const manualTargetM = (control && control.manual_target_m) ?? 0.15;
  const cycleIsOff = cycleName === "off";

  const cycleOptionsHtml = CYCLE_OPTIONS
    .map((c) => `<option value="${c}" ${c === cycleName ? "selected" : ""}>${c.toUpperCase()}</option>`)
    .join("");

  return `
  <div class="unit-card ${connected ? "" : "disconnected"}" data-unit="${unit.unit_id}">
    <div class="unit-head">
      <span class="unit-name">UNIT ${unit.unit_id}</span>
      <span class="${connBadge}">${connText}</span>
    </div>
    <div class="mimic">
      <div class="tank-stack">
        <div class="pump-glyph mono" title="PUMP, the only actuator">P</div>
        <div class="tank tank-upper" title="H1: upstream tank, estimated, no sensor in the real rig">
          <div class="tank-fill tank-fill-dim" style="height:${h1Pct}%"></div>
        </div>
        <div class="pipe-connector"></div>
        <div class="tank" title="H2: downstream tank, measured, this is Level.PV">
          <div class="tank-fill" style="height:${fillPct}%"></div>
          <div class="tank-sp-line" style="bottom:${spPct}%"></div>
        </div>
      </div>
      <div class="readout-grid">
        <span class="k">LEVEL PV (H2)</span><span class="v">${fmt(pv, 2, "m")}</span>
        <span class="k">LEVEL SP</span><span class="v">${fmt(sp, 2, "m")}</span>
        <span class="k">PUMP CMD</span><span class="v">${fmt(v["Pump.CMD"], 1, "%")}</span>
        <span class="k">H1 (EST., UNMEASURED)</span><span class="v">${fmt(h1Estimated, 3, "m")}</span>
        <span class="k">PID OUT</span><span class="v">${fmt(v["PID.OUT"], 1, "%")}</span>
        <span class="k">MODE</span><span class="v"><span class="mode-tag">${mode}</span></span>
        <span class="k">HEARTBEAT</span><span class="v">${fmt(v["Status.Heartbeat"], 0)}</span>
        <span class="k">SCAN</span><span class="v">${fmt(v["Status.ScanTime_ms"], 0, "ms")}</span>
      </div>
    </div>
    ${buildTrendSvg(unit.history, 70, 300)}
    <div class="cycle-row">
      <span class="label mono">CYCLE</span>
      <select data-cycle-select="${unit.unit_id}" ${connected ? "" : "disabled"}>${cycleOptionsHtml}</select>
      <input type="number" step="0.01" data-manual-target="${unit.unit_id}" value="${manualTargetM.toFixed(2)}" title="target used when CYCLE = MANUAL">
      <span class="mono">m</span>
      <button data-cycle-submit="${unit.unit_id}" ${connected ? "" : "disabled"}>SET</button>
    </div>
    <div class="sp-row">
      <span class="label mono">MANUAL SP</span>
      <input type="number" step="0.1" data-sp-input="${unit.unit_id}" placeholder="${fmt(sp, 2)}" ${cycleIsOff ? "" : "disabled"}>
      <span class="mono">m</span>
      <button data-sp-submit="${unit.unit_id}" ${connected && cycleIsOff ? "" : "disabled"}>WRITE</button>
      ${cycleIsOff ? "" : '<span class="hint">set CYCLE to OFF to hold a manual write</span>'}
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
}

function render(snapshot) {
  lastSnapshot = snapshot;
  const focused = captureFocusedInput(unitsEl);

  const order = Object.keys(snapshot.units).sort((a, b) => Number(a) - Number(b));
  const diagnostics = (snapshot.dcs && snapshot.dcs.diagnostics) || {};
  const unitControl = (snapshot.dcs && snapshot.dcs.unit_control) || {};
  unitsEl.innerHTML = order
    .map((uid) => renderUnit(snapshot.units[uid], diagnostics[uid], unitControl[uid]))
    .join("");

  restoreFocusedInput(unitsEl, focused);

  renderAlarms(snapshot);
  renderTopbar(snapshot);
  serverTimeEl.textContent = "SERVER: " + new Date(snapshot.server_time * 1000).toLocaleTimeString();
}

unitsEl.addEventListener("click", async (ev) => {
  const spUid = ev.target.getAttribute("data-sp-submit");
  if (spUid) {
    const input = unitsEl.querySelector(`[data-sp-input="${spUid}"]`);
    const value = parseFloat(input.value);
    if (Number.isNaN(value)) return;
    ev.target.disabled = true;
    try {
      const resp = await fetch(`/api/units/${spUid}/setpoint`, {
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
    return;
  }

  const cycleUid = ev.target.getAttribute("data-cycle-submit");
  if (cycleUid) {
    const select = unitsEl.querySelector(`[data-cycle-select="${cycleUid}"]`);
    const targetInput = unitsEl.querySelector(`[data-manual-target="${cycleUid}"]`);
    const name = select.value;
    const body = { name };
    const targetVal = parseFloat(targetInput.value);
    if (!Number.isNaN(targetVal)) body.target_m = targetVal;

    ev.target.disabled = true;
    try {
      const resp = await fetch(`/api/units/${cycleUid}/cycle`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const respBody = await resp.json().catch(() => ({}));
        alert(`Cycle set failed: ${respBody.error || resp.status}`);
      }
    } finally {
      ev.target.disabled = false;
    }
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
