/* WDN Studio frontend — dependency-free (vanilla JS + Canvas 2D). */
"use strict";

// ------------------------------------------------------------------ helpers
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v)) ? "—" :
  (Math.abs(v) >= 1e5 || (Math.abs(v) < 1e-3 && v !== 0)) ? Number(v).toExponential(2) : Number(v).toLocaleString(undefined, { maximumFractionDigits: d });
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v === null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* ignore */ } },
};

async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.json);
  }
  const r = await fetch(path, init);
  let data = null;
  try { data = await r.json(); } catch { /* non-json */ }
  if (!r.ok) throw new Error((data && data.detail) || `HTTP ${r.status}`);
  return data;
}

function busy(on, text = "Working…") {
  $("loading").hidden = !on;
  $("loadingText").textContent = text;
}

let toastTimer = null;
function toast(msg) {
  const el = $("toast");
  el.textContent = msg; el.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.hidden = true; }, 6000);
}

// --------------------------------------------------------------- markdown
function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, (_, c) => `<code class="${S.nodeIdx?.has(c) || S.linkIdx?.has(c) ? "id" : ""}" data-id="${c}">${c}</code>`)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])_([^_]+)_(?=[\s.,;:)]|$)/g, "$1<em>$2</em>")
    .replace(/(^|[\s(])\*([^*]+)\*(?=[\s.,;:)]|$)/g, "$1<em>$2</em>");
}

function md(text) {
  const lines = String(text || "").split("\n");
  let html = "", i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++; html += `<pre><code>${esc(buf.join("\n"))}</code></pre>`; continue;
    }
    if (/^\s*\|/.test(line)) {
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(lines[i++]);
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const isSep = (r) => /^\s*\|?(\s*:?-+:?\s*\|)+\s*:?-*:?\s*\|?\s*$/.test(r);
      const real = rows.filter((r) => !isSep(r));
      html += "<table>" + real.map((r, k) => "<tr>" + cells(r).map((c) => k === 0 ? `<th>${inline(c)}</th>` : `<td>${inline(c)}</td>`).join("") + "</tr>").join("") + "</table>";
      continue;
    }
    if (/^\s*[-*] /.test(line)) {
      html += "<ul>";
      while (i < lines.length && /^\s*[-*] /.test(lines[i])) html += `<li>${inline(lines[i++].replace(/^\s*[-*] /, ""))}</li>`;
      html += "</ul>"; continue;
    }
    if (/^\s*\d+[.)] /.test(line)) {
      html += "<ol>";
      while (i < lines.length && /^\s*\d+[.)] /.test(lines[i])) html += `<li>${inline(lines[i++].replace(/^\s*\d+[.)] /, ""))}</li>`;
      html += "</ol>"; continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)/);
    if (h) { html += `<p><strong>${inline(h[2])}</strong></p>`; i++; continue; }
    if (/^---+$/.test(line.trim())) { html += "<hr>"; i++; continue; }
    if (!line.trim()) { i++; continue; }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^(\s*[-*] |\s*\d+[.)] |\s*\||```|#)/.test(lines[i])) para.push(lines[i++]);
    html += `<p>${inline(para.join(" "))}</p>`;
  }
  return html;
}

function setMd(el, text) {
  el.innerHTML = md(text);
}

document.addEventListener("click", (e) => {
  const code = e.target.closest("code.id");
  if (code) { const id = code.dataset.id; selectElement(S.nodeIdx.has(id) ? "node" : "link", id, true); }
});

// -------------------------------------------------------------------- state
const S = {
  sid: null, summary: null, nodes: [], links: [], nodeIdx: new Map(), linkIdx: new Map(),
  pos: null, view: { k: 1, x: 0, y: 0 }, bounds: null,
  color: { attr: "", stat: "none" }, colorData: null, metric: null,
  highlight: { nodes: new Set(), links: new Set(), label: "" },
  selected: null, hover: null,
  sim: null, runs: [], t: 0, frames: new Map(), playing: null,
  criteria: {}, closed: [], chat: [],
};

// ------------------------------------------------------------------ loading
async function loadPayload(p) {
  S.sid = p.session_id; S.summary = p.summary; S.criteria = p.criteria || {};
  S.sim = null; S.runs = []; S.frames.clear(); S.metric = null; S.selected = null; S.closed = []; S.chat = [];
  busy(true, "Drawing network…");
  const g = await api(`/api/s/${S.sid}/geometry`);
  S.nodes = g.nodes; S.links = g.links;
  S.nodeIdx = new Map(S.nodes.map((n, i) => [n.id, i]));
  S.linkIdx = new Map(S.links.map((l, i) => [l.id, i]));
  const dia = S.links.map((l) => l.diameter_mm).filter((d) => d > 0);
  S.diaRange = dia.length ? [Math.min(...dia), Math.max(...dia)] : [1, 1];
  computeBounds(); S.userMoved = false; fit();
  $("netName").textContent = `${p.summary.name} · ${p.summary.nodes.toLocaleString()} nodes · ${p.summary.links.toLocaleString()} links`;
  $("exportBtn").disabled = false; $("pngBtn").disabled = false; $("saveProjBtn").disabled = false; $("reportBtn").disabled = false;
  S.compare = null; S.lastTable = null; S.layers = {}; $("layerGroup").innerHTML = ""; $("resOut").innerHTML = ""; $("resCsv").hidden = true; $("compareOption").disabled = true; $("analysisCsv").hidden = true;
  $("simXlsx").hidden = true; $("statSelect").disabled = false; showNotice(null); $("chartsBtn").disabled = true; $("chartsPanel").hidden = true; $("editsCard").hidden = true;
  $("idList").innerHTML = [...S.nodes.slice(0, 3000).map((n) => n.id), ...S.links.slice(0, 3000).map((l) => l.id)].map((x) => `<option value="${esc(x)}">`).join("");
  renderOverview(p);
  renderCriteriaForm();
  renderViolations();
  updateColorOptions();
  renderClosed();
  setHighlight(null);
  closeInspector();
  $("timebar").hidden = true; $("runsCard").hidden = true; $("simOut").innerHTML = ""; $("analysisOut").innerHTML = "";
  setColor("");
  $("chatLog").innerHTML = "";
  greet();
  busy(false);
  draw();
}

async function loadFile(file) {
  if (!file) return;
  if (/\.(wdnproj|json)$/i.test(file.name)) return loadProject(file);
  if (!/\.inp$/i.test(file.name)) return toast("Please choose an EPANET .inp file or a .wdnproj project.");
  const fd = new FormData(); fd.append("file", file);
  busy(true, `Loading ${file.name}…`);
  try { await loadPayload(await api("/api/networks", { method: "POST", body: fd })); }
  catch (e) { busy(false); toast(e.message); }
}

async function loadProject(file) {
  const fd = new FormData(); fd.append("file", file);
  busy(true, `Opening project ${file.name} (re-running its simulations)…`);
  try {
    const p = await api("/api/projects", { method: "POST", body: fd });
    await loadPayload(p);
    const extra = p.project || {};
    S.chat = [];
    $("chatLog").innerHTML = "";
    for (const m of extra.chat || []) { addMsg(m.role === "user" ? "user" : "bot", m.content); S.chat.push(m); }
    for (const run of p.runs || []) activateSim(run, false, false);
    const active = (p.runs || []).find((r) => r.sim_id === p.active_sim_id);
    if (active) activateSim(active, true, false);
    if (extra.view && extra.view.k) { S.view = { ...extra.view }; S.userMoved = true; draw(); }
    await refreshEdits();
    toast(`Project opened (saved ${extra.saved_at || "earlier"}): ${(p.runs || []).length} run(s) re-simulated` + (extra.failed_runs?.length ? `, ${extra.failed_runs.length} failed` : "") + ".");
  } catch (e) { busy(false); toast(e.message); }
}

async function saveProject() {
  if (!S.sid) return;
  try {
    const r = await fetch(`/api/s/${S.sid}/project`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat: S.chat, view: { k: S.view.k, x: S.view.x, y: S.view.y, k0: S.view.k0 } }) });
    if (!r.ok) throw new Error((await r.json()).detail || r.status);
    downloadBlob(`${S.summary.name}.wdnproj`, await r.blob());
  } catch (e) { toast(e.message); }
}

async function loadSample(name) {
  if (!name) return;
  busy(true, `Loading ${name}…`);
  try { await loadPayload(await api(`/api/networks/sample/${encodeURIComponent(name)}`, { method: "POST" })); }
  catch (e) { busy(false); toast(e.message); }
}

function renderOverview(p) {
  $("emptyState").hidden = true; $("overviewBody").hidden = false;
  const s = p.summary;
  const tiles = [
    [s.junctions.toLocaleString(), "junctions"], [`${s.reservoirs} / ${s.tanks}`, "reservoirs / tanks"],
    [s.pipes.toLocaleString(), "pipes"], [`${s.pumps} / ${s.valves}`, "pumps / valves"],
    [`${fmt(s.total_pipe_length_km, 1)} km`, "pipe length"], [`${fmt(s.total_base_demand_lps, 1)} L/s`, "base demand"],
  ];
  $("tiles").innerHTML = tiles.map(([v, k]) => `<div class="tile"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("") +
    `<div class="tile" style="grid-column:1/-1"><div class="k">${esc(s.inp_flow_units)} · ${esc(s.headloss_formula)} headloss (roughness = ${esc(s.roughness_meaning)}) · ${fmt(s.duration_h, 1)} h in file · coordinates ${esc(s.coordinates)}</div></div>`;
  const v = p.validation;
  const label = { error: "Error", warning: "Warning", info: "Note" };
  $("validation").innerHTML = v.issues.length ? v.issues.map((i, k) =>
    `<div class="issue" data-k="${k}"><span class="badge ${i.severity}">${label[i.severity]}</span><div>${esc(i.message)}${i.count ? ` <span class="hint">(${i.count})</span>` : ""}</div></div>`).join("")
    : `<p class="hint"><span class="badge info">OK</span> No problems found.</p>`;
  $("validation").querySelectorAll(".issue").forEach((el) => el.addEventListener("click", () => {
    const i = v.issues[+el.dataset.k];
    if (!i.elements.length) return;
    setHighlight(i.element_kind === "link" ? { links: i.elements, label: i.message } : { nodes: i.elements, label: i.message }, true);
  }));
  api(`/api/s/${S.sid}/pipe-statistics`).then((r) => setMd($("pipeStats"), r.markdown)).catch(() => {});
}

// ----------------------------------------------------------------- the map
const canvas = $("map");
const ctx = canvas.getContext("2d");
let DPR = 1;

function resize() {
  const r = canvas.getBoundingClientRect();
  DPR = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, r.width * DPR); canvas.height = Math.max(1, r.height * DPR);
  draw();
}
new ResizeObserver(() => { resize(); if (!S.userMoved) fit(); }).observe(canvas);

function computeBounds() {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const n of S.nodes) { x0 = Math.min(x0, n.x); x1 = Math.max(x1, n.x); y0 = Math.min(y0, n.y); y1 = Math.max(y1, n.y); }
  if (!Number.isFinite(x0)) { x0 = y0 = 0; x1 = y1 = 1; }
  S.bounds = { x0, y0, x1, y1 };
}

function fit() {
  if (!S.bounds) return;
  const r = canvas.getBoundingClientRect();
  const { x0, y0, x1, y1 } = S.bounds;
  const pad = Math.min(60, r.width * 0.05), w = Math.max(x1 - x0, 1e-9), h = Math.max(y1 - y0, 1e-9);
  const k = Math.min((r.width - 2 * pad) / w, (r.height - 2 * pad - 40) / h);
  S.view = { k, x: r.width / 2 - k * (x0 + w / 2), y: r.height / 2 + 20 + k * (y0 + h / 2) };
  S.view.k0 = k;
  draw();
}
function userFit() { S.userMoved = false; fit(); }

const sx = (x) => S.view.x + x * S.view.k;
const sy = (y) => S.view.y - y * S.view.k;
const wx = (px) => (px - S.view.x) / S.view.k;
const wy = (py) => (S.view.y - py) / S.view.k;

function linkPoints(l) {
  const a = S.nodes[S.nodeIdx.get(l.from)], b = S.nodes[S.nodeIdx.get(l.to)];
  const pts = [[a.x, a.y]];
  if (l.vertices) pts.push(...l.vertices);
  pts.push([b.x, b.y]);
  return pts;
}

// colour ramps (reference sequential blue; dark mode reversed so magnitude -> brighter)
const RAMP = ["#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"];
const RAMP_DARK = ["#1c5cab", "#256abf", "#2a78d6", "#3987e5", "#5598e7", "#6da7ec", "#86b6ef", "#9ec5f4", "#b7d3f6", "#cde2fb"];
const isDark = () => document.documentElement.dataset.theme === "dark" ||
  (document.documentElement.dataset.theme !== "light" && matchMedia("(prefers-color-scheme: dark)").matches);
function rampColor(t) {
  const r = isDark() ? RAMP_DARK : RAMP;
  if (!Number.isFinite(t)) return css("--muted");
  return r[Math.max(0, Math.min(r.length - 1, Math.round(t * (r.length - 1))))];
}

// diverging: decrease = red, no change = neutral grey, increase = blue
function mixHex(a, b, t) {
  const p = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const A = p(a), B = p(b);
  return "#" + A.map((v, i) => Math.round(v + (B[i] - v) * t).toString(16).padStart(2, "0")).join("");
}
function divColor(t) {
  const mid = isDark() ? "#383835" : "#d9d8d3";
  t = Math.max(-1, Math.min(1, t));
  return t < 0 ? mixHex(mid, isDark() ? "#e66767" : "#d03b3b", -t) : mixHex(mid, isDark() ? "#3987e5" : "#2a78d6", t);
}

function draw() {
  const W = canvas.width / DPR, H = canvas.height / DPR;
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  ctx.fillStyle = css("--map-bg"); ctx.fillRect(0, 0, W, H);
  if (!S.nodes.length) return;
  const cd = S.colorData;
  const zoom = S.view.k / (S.view.k0 || S.view.k);
  const linkGrey = css("--link"), nodeInk = css("--node"), hl = css("--highlight"), sel = css("--select"), crit = css("--critical");
  const [dmin, dmax] = S.diaRange;
  const widthOn = $("widthToggle").checked;
  const baseW = Math.min(3, 1 + Math.log2(Math.max(1, zoom)) * 0.5);

  // links
  ctx.lineCap = "round"; ctx.lineJoin = "round";
  for (let i = 0; i < S.links.length; i++) {
    const l = S.links[i];
    const pts = linkPoints(l);
    let w = baseW;
    if (widthOn && l.diameter_mm && dmax > dmin) w = baseW * (0.6 + 2.2 * Math.sqrt((l.diameter_mm - dmin) / (dmax - dmin)));
    let col = linkGrey;
    if (cd && cd.kind === "link") { const v = cd.values[l.id]; col = v === undefined || v === null ? linkGrey : cd.colorOf(v); }
    if (l.status === "Closed" || S.closed.includes(l.id)) { ctx.setLineDash([4, 3]); }
    ctx.strokeStyle = col; ctx.lineWidth = w;
    ctx.beginPath(); ctx.moveTo(sx(pts[0][0]), sy(pts[0][1]));
    for (let k = 1; k < pts.length; k++) ctx.lineTo(sx(pts[k][0]), sy(pts[k][1]));
    ctx.stroke(); ctx.setLineDash([]);
  }
  // links violating velocity / headloss limits (when colouring by that variable)
  const lim = cd && cd.attr === "velocity" ? S.criteria.max_velocity_ms : cd && cd.attr === "headloss" ? S.criteria.max_headloss_m_per_km : null;
  if (lim !== null && lim !== undefined) {
    ctx.strokeStyle = crit; ctx.lineWidth = baseW + 2.5; ctx.globalAlpha = 0.9;
    for (const l of S.links) {
      const v = cd.values[l.id]; if (v === null || v === undefined || v <= lim) continue;
      const pts = linkPoints(l);
      ctx.beginPath(); ctx.moveTo(sx(pts[0][0]), sy(pts[0][1]));
      for (let k = 1; k < pts.length; k++) ctx.lineTo(sx(pts[k][0]), sy(pts[k][1]));
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }
  // links whose peak velocity stays below the minimum (stagnation)
  const vmin = cd && cd.attr === "velocity" ? S.criteria.min_velocity_ms : null;
  if (vmin !== null && vmin !== undefined) {
    ctx.strokeStyle = css("--warning"); ctx.lineWidth = baseW + 2; ctx.setLineDash([5, 4]);
    for (const l of S.links) {
      const v = cd.values[l.id]; if (v === null || v === undefined || v >= vmin) continue;
      const pts = linkPoints(l);
      ctx.beginPath(); ctx.moveTo(sx(pts[0][0]), sy(pts[0][1]));
      for (let k = 1; k < pts.length; k++) ctx.lineTo(sx(pts[k][0]), sy(pts[k][1]));
      ctx.stroke();
    }
    ctx.setLineDash([]);
  }
  // highlighted links
  if (S.highlight.links.size) {
    ctx.strokeStyle = hl; ctx.lineWidth = baseW + 3;
    for (const id of S.highlight.links) {
      const l = S.links[S.linkIdx.get(id)]; if (!l) continue;
      const pts = linkPoints(l);
      ctx.beginPath(); ctx.moveTo(sx(pts[0][0]), sy(pts[0][1]));
      for (let k = 1; k < pts.length; k++) ctx.lineTo(sx(pts[k][0]), sy(pts[k][1]));
      ctx.stroke();
    }
  }
  // flow arrows
  if (S.flowFrame && $("arrowToggle").checked) drawArrows(S.flowFrame, nodeInk);
  // pumps & valves symbols
  for (const l of S.links) {
    if (l.type === "Pipe") continue;
    const pts = linkPoints(l); const m = Math.floor((pts.length - 1) / 2);
    const x = (sx(pts[m][0]) + sx(pts[m + 1][0])) / 2, y = (sy(pts[m][1]) + sy(pts[m + 1][1])) / 2;
    ctx.fillStyle = css("--surface"); ctx.strokeStyle = nodeInk; ctx.lineWidth = 1.5;
    if (l.type === "Pump") { ctx.beginPath(); ctx.arc(x, y, 6, 0, 7); ctx.fill(); ctx.stroke(); ctx.fillStyle = nodeInk; ctx.beginPath(); ctx.moveTo(x - 2.5, y - 3.5); ctx.lineTo(x + 3.5, y); ctx.lineTo(x - 2.5, y + 3.5); ctx.fill(); }
    else { ctx.beginPath(); ctx.moveTo(x - 6, y - 4); ctx.lineTo(x + 6, y + 4); ctx.lineTo(x + 6, y - 4); ctx.lineTo(x - 6, y + 4); ctx.closePath(); ctx.fill(); ctx.stroke(); }
  }
  // nodes
  const many = S.nodes.length > 1500;
  const rJ = Math.max(many ? 1.2 : 2, Math.min(5, (many ? 1.2 : 2.2) * Math.sqrt(zoom)));
  const showJ = !(cd && cd.kind === "link") || zoom > 2.5;
  const minP = cd && cd.attr === "pressure" && S.criteria.min_pressure_m != null ? S.criteria.min_pressure_m : null;
  const maxP = cd && cd.attr === "pressure" && S.criteria.max_pressure_m != null ? S.criteria.max_pressure_m : null;
  for (const n of S.nodes) {
    const x = sx(n.x), y = sy(n.y);
    if (x < -10 || y < -10 || x > W + 10 || y > H + 10) continue;
    let col = nodeInk;
    if (cd && cd.kind === "node" && n.type === "Junction") { const v = cd.values[n.id]; col = v === undefined || v === null ? css("--muted") : cd.colorOf(v); }
    if (n.type === "Junction") {
      if (!showJ && !(cd && cd.kind === "node")) continue;
      const r = cd && cd.kind === "node" ? rJ + 1 : rJ;
      const blank = cd && cd.kind === "node" && cd.values[n.id] === null;
      if (blank) { ctx.strokeStyle = crit; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(x, y, r + 1, 0, 7); ctx.stroke(); }
      else { ctx.fillStyle = col; ctx.beginPath(); ctx.arc(x, y, r, 0, 7); ctx.fill(); }
      if (minP !== null) { const v = cd.values[n.id]; if (v !== null && v !== undefined && v < minP) { ctx.strokeStyle = crit; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(x, y, r + 2.5, 0, 7); ctx.stroke(); } }
      if (maxP !== null) { const v = cd.values[n.id]; if (v !== null && v !== undefined && v > maxP) { ctx.strokeStyle = sel; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(x, y, r + 2.5, 0, 7); ctx.stroke(); } }
    } else if (n.type === "Reservoir") {
      ctx.fillStyle = col === nodeInk ? css("--accent") : col; ctx.strokeStyle = css("--surface"); ctx.lineWidth = 2;
      ctx.beginPath(); ctx.rect(x - 7, y - 7, 14, 14); ctx.fill(); ctx.stroke();
    } else {
      ctx.fillStyle = col === nodeInk ? css("--accent") : col; ctx.strokeStyle = css("--surface"); ctx.lineWidth = 2;
      ctx.beginPath(); ctx.rect(x - 5, y - 8, 10, 16); ctx.fill(); ctx.stroke();
    }
  }
  // highlighted nodes
  if (S.highlight.nodes.size) {
    ctx.strokeStyle = hl; ctx.lineWidth = 2.5;
    for (const id of S.highlight.nodes) {
      const n = S.nodes[S.nodeIdx.get(id)]; if (!n) continue;
      ctx.beginPath(); ctx.arc(sx(n.x), sy(n.y), rJ + 4, 0, 7); ctx.stroke();
    }
  }
  // labels when zoomed in
  const visible = S.nodes.filter((n) => { const x = sx(n.x), y = sy(n.y); return x > 0 && y > 0 && x < W && y < H; });
  if (visible.length < 120) {
    ctx.font = "11px system-ui, sans-serif"; ctx.fillStyle = css("--text-2");
    for (const n of visible) ctx.fillText(n.id, sx(n.x) + 6, sy(n.y) - 6);
  }
  // selection
  if (S.selected) {
    ctx.strokeStyle = sel; ctx.lineWidth = 3;
    if (S.selected.kind === "node") {
      const n = S.nodes[S.nodeIdx.get(S.selected.id)];
      if (n) { ctx.beginPath(); ctx.arc(sx(n.x), sy(n.y), rJ + 7, 0, 7); ctx.stroke(); }
    } else {
      const l = S.links[S.linkIdx.get(S.selected.id)];
      if (l) { const pts = linkPoints(l); ctx.lineWidth = baseW + 5; ctx.globalAlpha = 0.55; ctx.beginPath(); ctx.moveTo(sx(pts[0][0]), sy(pts[0][1])); for (let k = 1; k < pts.length; k++) ctx.lineTo(sx(pts[k][0]), sy(pts[k][1])); ctx.stroke(); ctx.globalAlpha = 1; }
    }
  }
}

function drawArrows(frame, ink) {
  ctx.fillStyle = ink;
  for (const l of S.links) {
    const q = frame.values[l.id];
    if (!q || Math.abs(q) < 1e-6) continue;
    const pts = linkPoints(l);
    const a = pts[0], b = pts[pts.length - 1];
    const x1 = sx(a[0]), y1 = sy(a[1]), x2 = sx(b[0]), y2 = sy(b[1]);
    const len = Math.hypot(x2 - x1, y2 - y1);
    if (len < 34) continue;
    let ang = Math.atan2(y2 - y1, x2 - x1); if (q < 0) ang += Math.PI;
    const mx = (x1 + x2) / 2, my = (y1 + y2) / 2, s = 4.5;
    ctx.save(); ctx.translate(mx, my); ctx.rotate(ang);
    ctx.beginPath(); ctx.moveTo(s, 0); ctx.lineTo(-s, -s * 0.8); ctx.lineTo(-s, s * 0.8); ctx.closePath(); ctx.fill(); ctx.restore();
  }
}

// pan / zoom / hover
let drag = null;
const pointers = new Map();
canvas.addEventListener("pointerdown", (e) => {
  canvas.setPointerCapture(e.pointerId);
  pointers.set(e.pointerId, { x: e.offsetX, y: e.offsetY });
  drag = { x: e.offsetX, y: e.offsetY, vx: S.view.x, vy: S.view.y, moved: false };
});
canvas.addEventListener("pointermove", (e) => {
  if (pointers.has(e.pointerId)) pointers.set(e.pointerId, { x: e.offsetX, y: e.offsetY });
  if (pointers.size === 2) { pinch(); return; }
  if (drag) {
    const dx = e.offsetX - drag.x, dy = e.offsetY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    if (drag.moved) { S.userMoved = true; canvas.classList.add("dragging"); S.view.x = drag.vx + dx; S.view.y = drag.vy + dy; hideTip(); draw(); return; }
  }
  hoverAt(e.offsetX, e.offsetY);
});
canvas.addEventListener("pointerup", (e) => {
  pointers.delete(e.pointerId); lastPinch = null;
  canvas.classList.remove("dragging");
  if (drag && !drag.moved) {
    const hit = hitTest(e.offsetX, e.offsetY);
    if (hit) selectElement(hit.kind, hit.id, false); else closeInspector();
  }
  drag = null;
});
canvas.addEventListener("pointerleave", hideTip);
canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  // Normalise wheel units (pixels / lines / pages) so one mouse-wheel notch is ~30% zoom.
  const dy = e.deltaMode === 1 ? e.deltaY * 33 : e.deltaMode === 2 ? e.deltaY * 400 : e.deltaY;
  const f = Math.exp(-dy * 0.0035);
  zoomAt(e.offsetX, e.offsetY, Math.max(0.4, Math.min(2.5, f)));
}, { passive: false });
canvas.addEventListener("dblclick", (e) => zoomAt(e.offsetX, e.offsetY, e.shiftKey ? 0.5 : 2));
function zoomCenter(f) { const r = canvas.getBoundingClientRect(); zoomAt(r.width / 2, r.height / 2, f); }
let lastPinch = null;
function pinch() {
  const [a, b] = [...pointers.values()];
  const d = Math.hypot(a.x - b.x, a.y - b.y), cx = (a.x + b.x) / 2, cy = (a.y + b.y) / 2;
  if (lastPinch) zoomAt(cx, cy, d / lastPinch);
  lastPinch = d; drag = null;
}
function zoomAt(px, py, f) {
  S.userMoved = true;
  const k0 = S.view.k0 || S.view.k;
  const k = Math.max(k0 * 0.2, Math.min(k0 * 1000, S.view.k * f));
  const X = wx(px), Y = wy(py);
  S.view.k = k; S.view.x = px - X * k; S.view.y = py + Y * k;
  draw();
}

function hitTest(px, py) {
  let best = null, bd = 9;
  for (const n of S.nodes) {
    const d = Math.hypot(sx(n.x) - px, sy(n.y) - py);
    if (d < bd) { bd = d; best = { kind: "node", id: n.id }; }
  }
  if (best) return best;
  bd = 6;
  for (const l of S.links) {
    const pts = linkPoints(l);
    for (let k = 0; k < pts.length - 1; k++) {
      const d = segDist(px, py, sx(pts[k][0]), sy(pts[k][1]), sx(pts[k + 1][0]), sy(pts[k + 1][1]));
      if (d < bd) { bd = d; best = { kind: "link", id: l.id }; }
    }
  }
  return best;
}
function segDist(px, py, x1, y1, x2, y2) {
  const dx = x2 - x1, dy = y2 - y1, L = dx * dx + dy * dy;
  const t = L ? Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / L)) : 0;
  return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
}

function hoverAt(px, py) {
  const hit = hitTest(px, py);
  const tip = $("tooltip");
  if (!hit) { hideTip(); canvas.style.cursor = ""; return; }
  canvas.style.cursor = "pointer";
  const rec = hit.kind === "node" ? S.nodes[S.nodeIdx.get(hit.id)] : S.links[S.linkIdx.get(hit.id)];
  let extra = "";
  if (hit.kind === "link" && rec.type === "Pipe") extra = `${fmt(rec.diameter_mm, 0)} mm · ${fmt(rec.length_m, 1)} m`;
  if (hit.kind === "node" && rec.type === "Junction") extra = `elev ${fmt(rec.elevation_m, 1)} m · ${fmt(rec.base_demand_lps, 3)} L/s`;
  const cd = S.colorData;
  if (cd && cd.kind === hit.kind && cd.values[hit.id] !== undefined) extra += `<br>${esc(cd.title)}: <b>${fmt(cd.values[hit.id], 3)}</b> ${esc(cd.unit || "")}`;
  tip.innerHTML = `<b>${esc(rec.type)} ${esc(rec.id)}</b>${extra ? "<br>" + extra : ""}`;
  tip.hidden = false;
  const r = canvas.getBoundingClientRect();
  tip.style.left = Math.min(px + 14, r.width - tip.offsetWidth - 8) + "px";
  tip.style.top = Math.max(8, py - tip.offsetHeight - 10) + "px";
}
function hideTip() { $("tooltip").hidden = true; }

function zoomTo(kind, id) {
  S.userMoved = true;
  let x, y;
  if (kind === "node") { const n = S.nodes[S.nodeIdx.get(id)]; if (!n) return; x = n.x; y = n.y; }
  else { const l = S.links[S.linkIdx.get(id)]; if (!l) return; const p = linkPoints(l); x = (p[0][0] + p[p.length - 1][0]) / 2; y = (p[0][1] + p[p.length - 1][1]) / 2; }
  const r = canvas.getBoundingClientRect();
  const k = Math.max(S.view.k, (S.view.k0 || S.view.k) * 6);
  S.view.k = k; S.view.x = r.width / 2 - x * k; S.view.y = r.height / 2 + y * k;
  draw();
}

function zoomToSet(nodes, links) {
  const xs = [], ys = [];
  for (const id of nodes) { const n = S.nodes[S.nodeIdx.get(id)]; if (n) { xs.push(n.x); ys.push(n.y); } }
  for (const id of links) { const l = S.links[S.linkIdx.get(id)]; if (l) for (const p of linkPoints(l)) { xs.push(p[0]); ys.push(p[1]); } }
  if (!xs.length) return;
  const save = S.bounds;
  S.bounds = { x0: Math.min(...xs), x1: Math.max(...xs), y0: Math.min(...ys), y1: Math.max(...ys) };
  const k0 = S.view.k0;
  const spanOk = (S.bounds.x1 - S.bounds.x0) + (S.bounds.y1 - S.bounds.y0) > 0;
  if (spanOk) { fit(); S.view.k0 = k0; S.userMoved = true; } else zoomTo(nodes.length ? "node" : "link", nodes[0] || links[0]);
  S.bounds = save; draw();
}

// ------------------------------------------------------------- colouring
const NODE_ATTRS = { elevation: ["elevation_m", "m", "Elevation"], base_demand: ["base_demand_lps", "L/s", "Base demand"] };
const LINK_ATTRS = { diameter: ["diameter_mm", "mm", "Diameter"], length: ["length_m", "m", "Length"], roughness: ["roughness", "", "Roughness"] };
const SIM_VARS = { pressure: ["node", "m", "Pressure"], head: ["node", "m", "Head"], demand: ["node", "L/s", "Demand"], age: ["node", "h", "Water age"],
  chlorine: ["node", "mg/L", "Chlorine"], trace: ["node", "%", "Source trace"], leak: ["node", "L/s", "Leak flow"],
  flow: ["link", "L/s", "Flow"], velocity: ["link", "m/s", "Velocity"], headloss: ["link", "m/km", "Headloss"] };
const ABS_VARS = new Set(["flow", "velocity", "headloss"]);

function buildColorData(kind, values, title, unit, attr, binary = false) {
  const vals = Object.values(values).filter((v) => v !== null && Number.isFinite(v)).sort((a, b) => a - b);
  if (!vals.length) return null;
  const q = (p) => vals[Math.min(vals.length - 1, Math.max(0, Math.round(p * (vals.length - 1))))];
  let lo = q(0.02), hi = q(0.98);
  const nz = vals.filter((v) => v !== 0);
  if (nz.length && nz.length < vals.length * 0.5) {  // mostly zeros (e.g. criticality): scale on the non-zero values
    lo = Math.min(0, nz[0]); hi = nz[Math.min(nz.length - 1, Math.round(0.98 * (nz.length - 1)))];
  }
  if (hi - lo < 1e-12) { lo = vals[0]; hi = vals[vals.length - 1]; }
  const span = hi - lo || 1;
  const colorOf = binary ? (v) => (v > 0.5 ? css("--highlight") : css("--link")) : (v) => rampColor((v - lo) / span);
  return { kind, values, title, unit, attr, lo, hi, min: vals[0], max: vals[vals.length - 1], colorOf, binary };
}

function buildDivergingData(kind, values, title, unit) {
  const abs = Object.values(values).filter((v) => v !== null && Number.isFinite(v)).map(Math.abs).sort((a, b) => a - b);
  if (!abs.length) return null;
  const M = abs[Math.min(abs.length - 1, Math.round(0.98 * (abs.length - 1)))] || abs[abs.length - 1] || 1;
  const all = Object.values(values).filter((v) => v !== null);
  return { kind, values, title, unit, attr: "compare", lo: -M, hi: M, min: Math.min(...all), max: Math.max(...all),
    colorOf: (v) => divColor(v / (M || 1)), diverging: true };
}

async function setColor(attr, stat) {
  S.color.attr = attr;
  if (stat) S.color.stat = stat;
  if (S.sim && S.sim.single_step) S.color.stat = "none";
  $("colorSelect").value = attr;
  $("statSelect").value = S.color.stat;
  $("statWrap").hidden = !(attr in SIM_VARS);
  await refreshColor();
}

async function refreshColor() {
  const attr = S.color.attr;
  S.colorData = null;
  try {
    if (!attr) { /* none */ }
    else if (attr in NODE_ATTRS) {
      const [f, u, t] = NODE_ATTRS[attr];
      S.colorData = buildColorData("node", Object.fromEntries(S.nodes.filter((n) => n.type === "Junction").map((n) => [n.id, n[f]])), t, u, attr);
    } else if (attr in LINK_ATTRS) {
      const [f, u, t] = LINK_ATTRS[attr];
      S.colorData = buildColorData("link", Object.fromEntries(S.links.filter((l) => l.type === "Pipe").map((l) => [l.id, l[f]])), t, u, attr);
    } else if (attr.startsWith("layer:")) {
      const name = attr.slice(6);
      let L = S.layers[name];
      if (!L) L = S.layers[name] = await api(`/api/s/${S.sid}/layers/${encodeURIComponent(name)}`);
      if (L) S.colorData = buildColorData(L.target, L.values, L.title, L.unit, attr);
    } else if (attr === "compare" && S.compare) {
      const c = S.compare;
      const vals = c.kind === "link" ? Object.fromEntries(Object.entries(c.values).filter(([k]) => S.links[S.linkIdx.get(k)]?.type === "Pipe"))
        : Object.fromEntries(Object.entries(c.values).filter(([k]) => S.nodes[S.nodeIdx.get(k)]?.type === "Junction"));
      S.colorData = buildDivergingData(c.kind, vals, c.label, c.unit);
    } else if (attr === "metric" && S.metric) {
      const m = S.metric;
      S.colorData = buildColorData(m.target, m.values, m.metric.replace(/_/g, " "), "", "metric", m.metric === "articulation" || m.metric === "bridge");
    } else if (attr in SIM_VARS) {
      if (!S.sim) { S.color.attr = ""; $("colorSelect").value = ""; toast("Run a simulation first to colour by simulated values."); }
      else if (["age", "chlorine", "trace", "leak"].includes(attr) && !S.sim.node_vars.includes(attr)) { toast(`The shown run has no ${SIM_VARS[attr][2].toLowerCase()} results.`); }
      else {
        let stat = S.color.stat === "none" ? null : S.color.stat;
        if (ABS_VARS.has(attr) && stat === "max") stat = "absmax";
        const f = await frame(attr, stat);
        let values = f.values;
        if (ABS_VARS.has(attr)) values = Object.fromEntries(Object.entries(values).map(([k, v]) => [k, v === null ? null : Math.abs(v)]));
        if (attr !== "pressure" && attr !== "head" && attr !== "age" && SIM_VARS[attr][0] === "link") {
          values = Object.fromEntries(Object.entries(values).filter(([k]) => S.links[S.linkIdx.get(k)]?.type === "Pipe"));
        }
        if (SIM_VARS[attr][0] === "node") values = Object.fromEntries(Object.entries(values).filter(([k]) => S.nodes[S.nodeIdx.get(k)]?.type === "Junction"));
        S.colorData = buildColorData(SIM_VARS[attr][0], values, `${SIM_VARS[attr][2]}${ABS_VARS.has(attr) ? " (abs)" : ""} · ${f.label}`, SIM_VARS[attr][1], attr);
      }
    }
  } catch (e) { toast(e.message); }
  await refreshArrows();
  renderLegend(); draw();
}

async function frame(variable, stat) {
  const key = `${S.sim.sim_id}|${variable}|${stat || S.t}`;
  if (!S.frames.has(key)) {
    const q = stat ? `stat=${stat}` : `t=${S.t}`;
    S.frames.set(key, await api(`/api/s/${S.sid}/sim/${S.sim.sim_id}/frame?var=${variable}&${q}`));
    if (S.frames.size > 300) S.frames.delete(S.frames.keys().next().value);
  }
  return S.frames.get(key);
}

async function refreshArrows() {
  S.flowFrame = null;
  if (S.sim && $("arrowToggle").checked) {
    try { S.flowFrame = await frame("flow", null); } catch { /* ignore */ }
  }
}

function renderLegend() {
  const cd = S.colorData, el = $("legend");
  if (!cd) { el.hidden = true; return; }
  el.hidden = false;
  if (cd.binary) {
    el.innerHTML = `<div class="ttl">${esc(cd.title)}</div><div><span style="color:var(--highlight)">━━</span> yes &nbsp; <span style="color:var(--link)">━━</span> no</div>`;
    return;
  }
  if (cd.diverging) {
    el.innerHTML = `<div class="ttl">${esc(cd.title)} (${esc(cd.unit)})</div>
      <div class="bar" style="background:linear-gradient(90deg,${divColor(-1)},${divColor(0)},${divColor(1)})"></div>
      <div class="ticks"><span>≤${fmt(cd.lo, 3)}</span><span>0</span><span>≥+${fmt(cd.hi, 3)}</span></div>
      <div class="hint">red = decreased · blue = increased · range ${fmt(cd.min, 3)} – ${fmt(cd.max, 3)}</div>`;
    return;
  }
  const ramp = (isDark() ? RAMP_DARK : RAMP).join(",");
  let crit = "";
  if (cd.attr === "pressure" && S.criteria.min_pressure_m != null) crit += `<div class="crit">◯ red ring: below min ${fmt(S.criteria.min_pressure_m)} m</div>`;
  if (cd.attr === "pressure" && S.criteria.max_pressure_m != null) crit += `<div class="crit" style="color:var(--select)">◯ purple ring: above max ${fmt(S.criteria.max_pressure_m)} m</div>`;
  if (cd.attr === "velocity" && S.criteria.max_velocity_ms != null) crit += `<div class="crit">red outline: above max ${fmt(S.criteria.max_velocity_ms)} m/s</div>`;
  if (cd.attr === "velocity" && S.criteria.min_velocity_ms != null) crit += `<div class="crit" style="color:color-mix(in srgb, var(--warning) 70%, var(--text))">amber dashes: below min ${fmt(S.criteria.min_velocity_ms)} m/s${S.color.stat === "none" && S.sim && !S.sim.single_step ? " (at this time step — choose “Max over period” to check peak velocity)" : ""}</div>`;
  if (cd.kind === "node" && Object.values(cd.values).some((v) => v === null)) crit += `<div class="crit">hollow red: cut off from every source</div>`;
  if (cd.attr === "headloss" && S.criteria.max_headloss_m_per_km != null) crit += `<div class="crit">red outline: above max ${fmt(S.criteria.max_headloss_m_per_km)} m/km</div>`;
  el.innerHTML = `<div class="ttl">${esc(cd.title)}${cd.unit ? ` (${esc(cd.unit)})` : ""}</div>
    <div class="bar" style="background:linear-gradient(90deg,${ramp})"></div>
    <div class="ticks"><span>${cd.lo > cd.min ? "≤" : ""}${fmt(cd.lo, 3)}</span><span>${fmt((cd.lo + cd.hi) / 2, 3)}</span><span>${cd.hi < cd.max ? "≥" : ""}${fmt(cd.hi, 3)}</span></div>
    <div class="hint">range ${fmt(cd.min, 3)} – ${fmt(cd.max, 3)}</div>${crit}`;
}

// ------------------------------------------------------------ highlight
function setHighlight(h, zoom = false) {
  S.highlight = { nodes: new Set(h?.nodes || []), links: new Set(h?.links || []), label: h?.label || "" };
  const n = S.highlight.nodes.size + S.highlight.links.size;
  $("hlLabel").hidden = !n; $("clearHl").hidden = !n;
  $("hlLabel").textContent = n ? `${S.highlight.label || "Highlighted"} · ${n}` : "";
  if (zoom && n) zoomToSet([...S.highlight.nodes], [...S.highlight.links]);
  draw();
}

// ------------------------------------------------------------- inspector
async function selectElement(kind, id, zoom) {
  if (!S.sid) return;
  if (kind === "node" && !S.nodeIdx.has(id) && S.linkIdx.has(id)) kind = "link";
  if (kind === "link" && !S.linkIdx.has(id) && S.nodeIdx.has(id)) kind = "node";
  S.selected = { kind, id };
  if (document.querySelector('.tab.active')?.dataset.tab === "resilience") {
    const rec = kind === "node" ? S.nodes[S.nodeIdx.get(id)] : null;
    if (rec && rec.type === "Junction") $("ffNode").value = id;
    if (!rec || rec.type === "Junction") $("lkAt").value = id;
  }
  if (zoom) zoomTo(kind, id); else draw();
  $("inspector").hidden = false;
  document.body.classList.add("insp-open");
  resize();
  $("inspTitle").innerHTML = `${esc(id)}<small>…</small>`;
  $("inspBody").innerHTML = ""; $("inspChart").innerHTML = ""; $("inspActions").innerHTML = "";
  try {
    const r = await api(`/api/s/${S.sid}/element/${kind}/${encodeURIComponent(id)}`);
    if (!S.selected || S.selected.id !== id) return;
    renderInspector(kind, r);
    if (S.sim) renderSeries(kind, id);
  } catch (e) { $("inspBody").textContent = e.message; }
}

function renderInspector(kind, r) {
  $("inspTitle").innerHTML = `${esc(r.id)}<small>${esc(r.type)}${r.valve_type ? " · " + esc(r.valve_type) : ""}</small>`;
  const skip = new Set(["id", "type", "success", "x", "y", "connected_links", "vertices", "simulated", "valve_type"]);
  const label = (k) => k.replace(/_lps$/, " (L/s)").replace(/_ms$/, " (m/s)").replace(/_m_per_km$/, " (m/km)")
    .replace(/_mm$/, " (mm)").replace(/_m$/, " (m)").replace(/_h$/, " (h)").replace(/_kwh$/, " (kWh)").replace(/_/g, " ");
  const rows = Object.entries(r).filter(([k, v]) => !skip.has(k) && v !== null && typeof v !== "object")
    .map(([k, v]) => `<dt>${esc(label(k))}</dt><dd>${typeof v === "number" ? fmt(v, 3) : esc(String(v))}</dd>`);
  if (r.connected_links) rows.push(`<dt>links</dt><dd>${r.connected_links.map((c) => `<code class="id" data-id="${esc(c.id)}">${esc(c.id)}</code>`).join(" ")}</dd>`);
  if (r.simulated) for (const [k, v] of Object.entries(r.simulated)) if (k !== "sim_id") rows.push(`<dt>${esc(label(k))}</dt><dd>${fmt(v, 3)}</dd>`);
  $("inspBody").innerHTML = `<dl class="kv">${rows.join("")}</dl>`;
  const acts = [];
  if (kind === "node") {
    acts.push(`<button class="btn small" data-act="from">Path from here</button>`, `<button class="btn small" data-act="to">Path to here</button>`);
  } else {
    const closed = S.closed.includes(r.id);
    acts.push(`<button class="btn small" data-act="close">${closed ? "Keep open in scenario" : "Close in scenario"}</button>`);
  }
  acts.push(`<button class="btn small" data-act="zoom">Zoom to</button>`, `<button class="btn small" data-act="edit">✏️ Edit</button>`);
  $("inspActions").innerHTML = acts.join("");
  $("inspActions").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    const a = b.dataset.act;
    if (a === "from") { $("pathFrom").value = r.id; showTab("analysis"); }
    if (a === "to") { $("pathTo").value = r.id; showTab("analysis"); if ($("pathFrom").value) $("pathBtn").click(); }
    if (a === "close") { toggleClosed(r.id); renderInspector(kind, r); showTab("simulate"); }
    if (a === "zoom") zoomTo(kind, r.id);
    if (a === "edit") openEditor(kind, r.id);
  }));
}

function closeInspector() {
  S.selected = null; $("inspector").hidden = true; document.body.classList.remove("insp-open"); resize();
}

async function renderSeries(kind, id) {
  try {
    const r = await api(`/api/s/${S.sid}/sim/${S.sim.sim_id}/series/${kind}/${encodeURIComponent(id)}`);
    if (!S.selected || S.selected.id !== id) return;
    const vars = kind === "node" ? ["pressure", "demand", "age"] : ["flow", "velocity", "headloss"];
    const el = $("inspChart"); el.innerHTML = "";
    if (r.times_h.length < 2) {
      el.innerHTML = `<p class="hint">Single time step: ${vars.filter((v) => r.series[v]).map((v) => `${v} ${fmt(r.series[v][0], 3)} ${r.units[v]}`).join(" · ")}</p>`;
      return;
    }
    const csvBtn = document.createElement("button");
    csvBtn.className = "btn small"; csvBtn.textContent = "⬇ Time series (CSV)"; csvBtn.style.marginTop = "10px";
    csvBtn.addEventListener("click", () => downloadCsv(`${id}_timeseries.csv`,
      r.times_h.map((t, i) => Object.fromEntries([["time_h", t], ...Object.keys(r.series).map((v) => [`${v}_${r.units[v] || ""}`.replace(/_$/, ""), r.series[v][i]])]))));
    for (const v of vars) if (r.series[v]) el.appendChild(lineChart(r.times_h, r.series[v], `${v[0].toUpperCase() + v.slice(1)} (${r.units[v]})`,
      v === "pressure" ? [S.criteria.min_pressure_m, S.criteria.max_pressure_m] : v === "velocity" ? [S.criteria.max_velocity_ms] : []));
    el.appendChild(csvBtn);
  } catch { /* simulation gone */ }
}

function lineChart(xs, ys, title, limits = []) {
  const W = 300, H = 150, P = { l: 40, r: 8, t: 8, b: 20 };
  const vals = ys.filter((v) => v !== null);
  if (!vals.length) {
    const d0 = document.createElement("div"); d0.className = "chart";
    d0.innerHTML = `<div class="ctitle"><span>${esc(title)}</span></div><p class="hint">No values in this run — the element is cut off from every source.</p>`;
    return d0;
  }
  const lims = limits.filter((v) => v !== null && v !== undefined);
  let lo = Math.min(...vals, ...lims), hi = Math.max(...vals, ...lims);
  if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const X = (x) => P.l + (x - xs[0]) / (xs[xs.length - 1] - xs[0] || 1) * (W - P.l - P.r);
  const Y = (y) => H - P.b - (y - lo) / (hi - lo) * (H - P.t - P.b);
  const d = ys.map((y, i) => y === null ? "" : `${i && ys[i - 1] !== null ? "L" : "M"}${X(xs[i]).toFixed(1)},${Y(y).toFixed(1)}`).join("");
  const ticks = [lo + pad, (lo + hi) / 2, hi - pad];
  const nowX = X(xs[Math.min(S.t, xs.length - 1)]);
  const div = document.createElement("div"); div.className = "chart";
  div.innerHTML = `<div class="ctitle"><span>${esc(title)}</span><span class="val"></span></div>
  <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(title)} over time">
    ${ticks.map((t) => `<line class="grid" x1="${P.l}" x2="${W - P.r}" y1="${Y(t)}" y2="${Y(t)}"/><text class="axis" x="${P.l - 4}" y="${Y(t) + 3}" text-anchor="end">${fmt(t, 2)}</text>`).join("")}
    <text class="axis" x="${P.l}" y="${H - 5}">${fmt(xs[0], 1)} h</text><text class="axis" x="${W - P.r}" y="${H - 5}" text-anchor="end">${fmt(xs[xs.length - 1], 1)} h</text>
    ${lims.map((v) => `<line class="limit" x1="${P.l}" x2="${W - P.r}" y1="${Y(v)}" y2="${Y(v)}"/>`).join("")}
    <path class="line" d="${d}"/>
    <line class="now" x1="${nowX}" x2="${nowX}" y1="${P.t}" y2="${H - P.b}"/>
    <circle class="hover-dot" r="4" cx="-10" cy="-10"/>
    <rect x="${P.l}" y="0" width="${W - P.l - P.r}" height="${H}" fill="transparent"/>
  </svg>`;
  const svg = div.querySelector("svg"), dot = div.querySelector(".hover-dot"), val = div.querySelector(".val");
  svg.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width * W;
    let best = 0, bd = Infinity;
    xs.forEach((t, i) => { const dd = Math.abs(X(t) - x); if (dd < bd) { bd = dd; best = i; } });
    if (ys[best] === null) return;
    dot.setAttribute("cx", X(xs[best])); dot.setAttribute("cy", Y(ys[best]));
    val.textContent = `${fmt(ys[best], 3)} @ ${fmt(xs[best], 2)} h`;
  });
  svg.addEventListener("pointerleave", () => { dot.setAttribute("cx", -10); val.textContent = ""; });
  svg.addEventListener("click", () => {
    const i = xs.findIndex((t) => t >= +val.textContent.split("@ ")[1]?.replace(" h", ""));
    if (i >= 0) setTime(i);
  });
  return div;
}

// -------------------------------------------------------------- tabs
function showTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));

function needNet() { if (!S.sid) { toast("Load a network first."); return true; } return false; }

async function runTool(name, args, out) {
  if (needNet()) return null;
  busy(true, "Calculating…");
  try {
    const r = await api(`/api/s/${S.sid}/tool/${name}`, { json: args || {} });
    if (out) setMd(out, r.markdown);
    if (out === $("analysisOut")) { S.lastTable = tableFromResult(name, r.result); $("analysisCsv").hidden = !S.lastTable || !S.lastTable.rows.length; }
    await applyActions(r.actions, r.result);
    return r;
  } catch (e) { toast(e.message); return null; }
  finally { busy(false); }
}

async function applyActions(actions, result) {
  for (const a of actions || []) {
    if (a.type === "highlight") setHighlight(a, true);
    else if (a.type === "select") selectElement(a.kind, a.id, true);
    else if (a.type === "time") { if (S.sim && S.sim.times_h.length > 1) { if (S.color.attr in SIM_VARS && S.color.stat !== "none") { S.color.stat = "none"; $("statSelect").value = "none"; } await setTime(a.index); } }
    else if (a.type === "color") {
      if (a.metric) {
        const m = result && result.values && result.metric === a.metric ? result
          : await api(`/api/s/${S.sid}/metric/${a.target}/${a.metric}?weighted=${!!a.weighted}`);
        S.metric = { target: a.target, metric: a.metric, values: m.values };
        $("metricOption").disabled = false; $("metricOption").textContent = `Metric: ${a.metric.replace(/_/g, " ")}`;
        await setColor("metric");
      } else if (a.attribute) {
        if (a.attribute in SIM_VARS && !S.sim) continue;
        await setColor(a.attribute, a.stat || "none");
      }
    } else if (a.type === "simulation") {
      const sim = await api(`/api/s/${S.sid}/sim/${a.sim_id}`);
      activateSim(sim, true, !S.keepTab);
    } else if (a.type === "edited") {
      await afterEdit();
    } else if (a.type === "layer") {
      try { await loadLayer(a.name, true); } catch (e) { toast(e.message); }
    } else if (a.type === "compare") {
      try { showCompare(await api(`/api/s/${S.sid}/compare?base=${a.base}&scenario=${a.scenario}&var=${a.var}&stat=${a.stat || "min"}`)); } catch { /* ignore */ }
    } else if (a.type === "criteria") {
      S.criteria = await api(`/api/s/${S.sid}/criteria`); renderCriteriaForm();
      if (S.sim) { const fresh = await api(`/api/s/${S.sid}/sim/${S.sim.sim_id}`); Object.assign(S.sim, fresh); }
      renderViolations();
    }
  }
}

// analysis tab
document.querySelectorAll("[data-topo]").forEach((b) => b.addEventListener("click", () =>
  runTool("topology_characterization", { focus: b.dataset.topo }, $("analysisOut"))));
$("distBtn").addEventListener("click", () => runTool("distance_statistics", { weighted: $("metricWeighted").checked }, $("analysisOut")));
$("metricBtn").addEventListener("click", () => {
  const [target, metric] = $("metricSelect").value.split(":");
  runTool(target === "node" ? "node_metric" : "link_metric", { metric, weighted: $("metricWeighted").checked }, $("analysisOut"));
});
$("pathBtn").addEventListener("click", () => {
  const w = document.querySelector("input[name=pw]:checked").value === "1";
  runTool("shortest_path", { source: $("pathFrom").value.trim(), target: $("pathTo").value.trim(), weighted: w }, $("analysisOut"));
});
function addCondition(field = "diameter_mm", op = "<", value = "") {
  const row = document.createElement("div"); row.className = "cond";
  row.innerHTML = `<select class="select"><option value="diameter_mm">Diameter (mm)</option><option value="length_m">Length (m)</option><option value="roughness">Roughness</option></select>
    <select class="select">${["<", "<=", ">", ">=", "=", "!="].map((o) => `<option>${o}</option>`).join("")}</select>
    <input class="input" type="number" step="any" placeholder="value"><button class="btn icon small" aria-label="Remove">✕</button>`;
  const [f, o, v, x] = row.children;
  f.value = field; o.value = op; v.value = value;
  x.addEventListener("click", () => row.remove());
  $("conditions").appendChild(row);
}
addCondition("diameter_mm", "<", 100);
$("addCond").addEventListener("click", () => addCondition("length_m", ">", ""));
$("filterBtn").addEventListener("click", () => {
  const conditions = [...$("conditions").children].map((r) => ({ field: r.children[0].value, op: r.children[1].value, value: parseFloat(r.children[2].value) }))
    .filter((c) => Number.isFinite(c.value));
  if (!conditions.length) return toast("Enter a value for at least one condition.");
  runTool("filter_pipes", { conditions }, $("analysisOut"));
});

// ------------------------------------------------------------- simulation
$("simModel").addEventListener("change", () => { $("reqPressWrap").hidden = $("simModel").value !== "PDD"; });
function renderClosed() {
  $("closedChips").innerHTML = S.closed.length ? S.closed.map((id) => `<span class="chip">${esc(id)}<button data-id="${esc(id)}" aria-label="Remove">✕</button></span>`).join("")
    : `<span class="hint">none</span>`;
  $("closedChips").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => toggleClosed(b.dataset.id)));
  draw();
}
function toggleClosed(id) {
  S.closed = S.closed.includes(id) ? S.closed.filter((x) => x !== id) : [...S.closed, id];
  renderClosed();
}
$("closeAdd").addEventListener("click", () => {
  const v = $("closeInput").value.trim();
  if (!v) return;
  if (!S.linkIdx.has(v)) return toast(`Link '${v}' not found.`);
  if (!S.closed.includes(v)) toggleClosed(v);
  $("closeInput").value = "";
});
$("simBtn").addEventListener("click", async () => {
  if (needNet()) return;
  const dur = $("simDuration").value;
  const body = {
    duration_h: dur === "" ? null : parseFloat(dur), demand_model: $("simModel").value,
    required_pressure_m: parseFloat($("simReqP").value) || 20, demand_multiplier: parseFloat($("simMult").value) || 1,
    water_age: $("simAge").checked, closed_links: S.closed,
  };
  busy(true, "Running EPANET…");
  try {
    const sim = await api(`/api/s/${S.sid}/simulate`, { json: body });
    activateSim(sim, true);
  } catch (e) { toast(e.message); }
  finally { busy(false); }
});

function updateColorOptions() {
  const special = { age: "tick ‘Water age’ and re-run", chlorine: "run Resilience → Water quality → Chlorine",
    trace: "run a source-trace analysis", leak: "run Resilience → Leak scenario" };
  const has = (v) => S.sim && (special[v] ? S.sim.node_vars?.includes(v) : true);
  for (const opt of $("simColorGroup").querySelectorAll("option")) {
    const ok = !!has(opt.value);
    opt.disabled = !ok;
    opt.textContent = opt.textContent.replace(/ —.*$/, "") + (ok ? "" : !S.sim ? " — run a simulation first" : ` — ${special[opt.value]}`);
  }
}

function activateSim(sim, colour, switchTab = true) {
  S.sim = sim; S.t = 0;
  if (!S.runs.find((r) => r.sim_id === sim.sim_id)) S.runs.unshift(sim);
  setMd($("simOut"), sim.markdown);
  $("runsCard").hidden = S.runs.length < 2;
  $("runSelect").innerHTML = S.runs.map((r) => `<option value="${r.sim_id}">${esc(runLabel(r))}</option>`).join("");
  $("runSelect").value = sim.sim_id;
  const others = S.runs.filter((r) => r.sim_id !== sim.sim_id);
  $("cmpBase").innerHTML = others.map((r) => `<option value="${r.sim_id}">${esc(runLabel(r))}</option>`).join("");
  const n = sim.times_h.length;
  $("timebar").hidden = n < 2;
  $("timeSlider").max = Math.max(0, n - 1); $("timeSlider").value = 0;
  // A steady-state run has one time step: period statistics are meaningless.
  $("statSelect").disabled = n < 2;
  $("statSelect").title = n < 2 ? "Steady-state run: only one time step, so min = max = mean. Set a Duration to get a time series." : "";
  if (n < 2) S.color.stat = "none";
  $("simXlsx").hidden = false;
  updateColorOptions();
  $("chartsBtn").disabled = false;
  if (!$("chartsPanel").hidden) openCharts();
  updateTimeLabel();
  if (switchTab) showTab("simulate");
  if (colour) {
    const attr = S.color.attr in SIM_VARS ? S.color.attr : "pressure";
    setColor(attr, n < 2 ? "none" : S.color.attr in SIM_VARS ? S.color.stat : "min");
  } else refreshColor();
  if (S.selected) renderSeries(S.selected.kind, S.selected.id);
  renderViolations();
}

function runLabel(r) {
  const i = S.runs.length - S.runs.indexOf(r);
  return `#${i} · ${r.demand_model} · ${r.single_step ? "steady" : fmt(r.duration_h, 0) + " h"} · ×${r.settings.demand_multiplier}` +
    (r.settings.closed_links.length ? ` · closed ${r.settings.closed_links.join(",")}` : "");
}

const VIOL = { low_pressure: ["Below min pressure", "node"], high_pressure: ["Above max pressure", "node"],
  high_velocity: ["Above max velocity", "link"], low_velocity: ["Peak velocity below min", "link"], high_headloss: ["Above max headloss", "link"] };
function renderViolations() {
  const card = $("violCard"), list = $("violList");
  const c = S.sim && S.sim.criteria;
  const anySet = ["min_pressure_m", "max_pressure_m", "min_velocity_ms", "max_velocity_ms", "max_headloss_m_per_km"].some((k) => S.criteria[k] != null);
  if (!anySet) { card.hidden = true; return; }
  card.hidden = false;
  if (!S.sim) { list.innerHTML = `<p class="hint">Run a simulation to check these limits.</p>`; return; }
  if (!c) { list.innerHTML = `<p class="hint">Click “Apply criteria” to check the current run.</p>`; return; }
  list.innerHTML = Object.entries(VIOL).filter(([k]) => c[k]).map(([k, [label]]) => {
    const v = c[k]; const lim = Object.entries(v).find(([kk]) => kk.startsWith("limit"))?.[1];
    return `<div class="viol ${v.count ? "bad" : "ok"}"><span class="cnt">${v.count.toLocaleString()}</span><span class="grow">${label} (${fmt(lim)})</span>${v.count ? `<button class="btn small" data-check="${k}">Show</button>` : "✓"}</div>`;
  }).join("") + `<p class="hint">Counts are elements that break the limit at least once in the run.</p>`;
  list.querySelectorAll("button[data-check]").forEach((b) => b.addEventListener("click", async () => {
    const k = b.dataset.check;
    const colourBy = { low_pressure: ["pressure", "min"], high_pressure: ["pressure", "max"], high_velocity: ["velocity", "max"], low_velocity: ["velocity", "max"], high_headloss: ["headloss", "max"] }[k];
    await setColor(colourBy[0], S.sim.single_step ? "none" : colourBy[1]);
    await runTool("criteria_violations", { check: k }, $("critOut"));
  }));
}

$("cmpBtn").addEventListener("click", async () => {
  const base = $("cmpBase").value;
  if (!base || !S.sim) return;
  busy(true, "Comparing runs…");
  try {
    const r = await api(`/api/s/${S.sid}/compare?base=${base}&scenario=${S.sim.sim_id}&var=${$("cmpVar").value}&stat=${$("cmpStat").value}&t=${S.t}`);
    showCompare(r);
  } catch (e) { toast(e.message); } finally { busy(false); }
});
function runNo(id) { const r = S.runs.find((x) => x.sim_id === id); return r ? `#${S.runs.length - S.runs.indexOf(r)}` : id; }
function showCompare(r) {
  const nice = `Δ ${r.var} (${r.label.split("(")[1].split(")")[0]}): run ${runNo(r.scenario)} − run ${runNo(r.base)}`;
  r.markdown = r.markdown.replace(r.label, nice); r.label = nice;
  S.compare = r; S.lastTable = { name: `change_${r.var}`, rows: Object.entries(r.values).map(([id, v]) => ({ id, change: v })) };
  $("compareOption").disabled = false;
  setMd($("simOut"), r.markdown);
  if (r.lost_supply_count) setHighlight({ nodes: r.kind === "node" ? r.lost_supply : [], links: r.kind === "link" ? r.lost_supply : [], label: "Lost supply" });
  setColor("compare");
}
$("runSelect").addEventListener("change", async () => {
  const sim = S.runs.find((r) => r.sim_id === $("runSelect").value);
  if (sim) { try { const fresh = await api(`/api/s/${S.sid}/sim/${sim.sim_id}`); Object.assign(sim, fresh); activateSim(sim, false); } catch (e) { toast(e.message); } }
});

function updateTimeLabel() {
  if (!S.sim) return;
  const h = S.sim.times_h[S.t] ?? 0;
  const d = Math.floor(h / 24), hh = Math.floor(h % 24), mm = Math.round((h % 1) * 60);
  $("timeLabel").textContent = `Day ${d + 1}, ${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}`;
}
let timeBusy = false;
async function setTime(t) {
  S.t = t; $("timeSlider").value = t; updateTimeLabel();
  if (timeBusy) return;
  timeBusy = true;
  try {
    if (S.color.attr in SIM_VARS && S.color.stat !== "none") { await refreshArrows(); draw(); }
    else await refreshColor();
    if (S.selected) document.querySelectorAll(".chart .now").forEach((l) => {
      const svg = l.ownerSVGElement; const xs = S.sim.times_h;
      const x = 40 + (xs[S.t] - xs[0]) / (xs[xs.length - 1] - xs[0] || 1) * (300 - 48);
      l.setAttribute("x1", x); l.setAttribute("x2", x); void svg;
    });
  } finally { timeBusy = false; }
}
$("timeSlider").addEventListener("input", (e) => setTime(+e.target.value));
$("playBtn").addEventListener("click", () => {
  if (S.playing) { clearInterval(S.playing); S.playing = null; $("playBtn").textContent = "▶"; return; }
  if (S.color.attr in SIM_VARS && S.color.stat !== "none") { S.color.stat = "none"; $("statSelect").value = "none"; }
  $("playBtn").textContent = "❚❚";
  S.playing = setInterval(() => { if (!timeBusy) setTime((S.t + 1) % S.sim.times_h.length); }, 220);
});

// ----------------------------------------------------------------- criteria
let PRESETS = {};
function renderCriteriaForm() {
  document.querySelectorAll("[data-crit]").forEach((i) => { const v = S.criteria[i.dataset.crit]; i.value = v === null || v === undefined ? "" : v; });
}
$("presetSelect").addEventListener("change", () => {
  const p = PRESETS[$("presetSelect").value]; if (!p) return;
  document.querySelectorAll("[data-crit]").forEach((i) => { i.value = p[i.dataset.crit] ?? ""; });
});
$("critSave").addEventListener("click", async () => {
  if (needNet()) return;
  const pk = $("presetSelect").value;
  const body = { label: pk && pk !== "none" ? PRESETS[pk]?.label || "Custom" : "Custom" };
  document.querySelectorAll("[data-crit]").forEach((i) => { body[i.dataset.crit] = i.value === "" ? null : parseFloat(i.value); });
  try {
    const r = await api(`/api/s/${S.sid}/criteria`, { method: "PUT", json: body });
    S.criteria = r.criteria;
    setMd($("critOut"), r.markdown ? "Criteria applied to the latest run:\n\n" + r.markdown : "Criteria saved. Run a simulation to check them.");
    if (r.simulation && S.sim) { Object.assign(S.sim, r.simulation, { markdown: r.markdown }); if (!S.sim.criteria) delete S.sim.criteria; setMd($("simOut"), r.markdown); }
    if (S.sim && !r.simulation?.criteria) delete S.sim.criteria;
    renderViolations(); renderLegend(); draw();
  } catch (e) { toast(e.message); }
});

// ------------------------------------------------------------------- chat
function openChat() { $("chat").hidden = false; $("chatFab").hidden = true; $("chatText").focus(); }
$("chatFab").addEventListener("click", openChat);
$("chatClose").addEventListener("click", () => { $("chat").hidden = true; $("chatFab").hidden = false; });
$("chatSettingsBtn").addEventListener("click", () => { $("chatSettings").hidden = !$("chatSettings").hidden; });

function markUnverified(root, tokens) {
  if (!tokens || !tokens.length) return;
  const esc2 = (t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(`(?<![\\w.])(${tokens.map(esc2).join("|")})(?![\\w])`, "g");
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) if (!walker.currentNode.parentElement.closest("code")) nodes.push(walker.currentNode);
  for (const n of nodes) {
    if (!re.test(n.nodeValue)) continue;
    re.lastIndex = 0;
    const span = document.createElement("span");
    span.innerHTML = esc(n.nodeValue).replace(re, '<mark class="unverified" title="Not found in any tool result — treat with caution">$1⚠</mark>');
    n.replaceWith(span);
  }
}

function addMsg(role, text, trace, verification) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  if (role === "user") el.textContent = text; else el.innerHTML = `<div class="md">${md(text)}</div>`;
  const v = verification;
  if (v && v.unverified && v.unverified.length) markUnverified(el.querySelector(".md"), v.unverified);
  if (trace && trace.length) {
    const t = document.createElement("div"); t.className = "trace";
    const checked = v && v.numbers_checked ? (v.unverified.length
      ? `<span class="warnchip">⚠ ${v.unverified.length} of ${v.numbers_checked} numbers not found in tool results</span>`
      : `<span class="okchip">✓ ${v.numbers_checked} numbers match tool results</span>`) : "";
    const cav = (v && v.caveats || []).map((c) => `<span class="cavchip" title="${esc(c)}">≈ ${esc(c)}</span>`).join("");
    t.innerHTML = `${checked}${cav}<span>verified by: ${trace.map((x) => esc(x.tool) + (x.ok ? "" : " (failed)")).join(", ")}</span>`;
    t.title = trace.map((x) => `${x.tool}(${JSON.stringify(x.args)})`).join("\n");
    const rerun = document.createElement("button"); rerun.className = "linkbtn"; rerun.textContent = "↻ re-run these calculations";
    rerun.addEventListener("click", () => rerunTrace(trace));
    t.appendChild(rerun);
    el.appendChild(t);
  }
  $("chatLog").appendChild(el); $("chatLog").scrollTop = $("chatLog").scrollHeight;
  return el;
}

async function rerunTrace(trace) {
  const parts = [];
  for (const x of trace) {
    try {
      const r = await api(`/api/s/${S.sid}/tool/${x.tool}`, { json: x.args || {} });
      parts.push(r.markdown);
      await applyActions(r.actions, r.result);
    } catch (e) { parts.push(`⚠️ ${x.tool}: ${e.message}`); }
  }
  addMsg("bot", "**Re-run of the calculations (direct tool output, no LLM):**\n\n" + parts.join("\n\n"));
}

function lowestGuess() {
  const j = S.nodes.filter((n) => n.type === "Junction");
  if (!j.length) return null;
  return j.reduce((a, b) => ((b.elevation_m ?? -1e9) > (a.elevation_m ?? -1e9) ? b : a)).id;  // highest junction = likely lowest pressure
}

function greet() {
  const el = addMsg("bot", `Loaded **${S.summary.name}**. Ask me anything about it — every number comes from a calculation.`);
  const sug = document.createElement("div"); sug.className = "suggest";
  ["Check the model for issues", "Which nodes have the lowest pressure?", "Pipes up to 100 mm with velocity above 0.5 m/s", "When is demand highest?", "Why is pressure low at " + (lowestGuess() || "this node") + "?", "Is this network well designed?", "What is PDD?", "Nodes within 300 m of " + (S.nodes.find((n) => n.type !== "Junction")?.id || S.nodes[0]?.id)]
    .forEach((q) => { const b = document.createElement("button"); b.className = "btn small"; b.textContent = q; b.addEventListener("click", () => ask(q)); sug.appendChild(b); });
  el.appendChild(sug);
}

async function ask(text) {
  if (!text.trim()) return;
  if (needNet()) return;
  openChat();
  addMsg("user", text);
  const typing = addMsg("bot typing", "Thinking…");
  const cfg = llmConfig();
  try {
    let job = await api(`/api/s/${S.sid}/chat_async`, { json: { message: text, history: S.chat.slice(-10), context: screenContext(), ...cfg } });
    while (job.status === "queued" || job.status === "running") {
      await new Promise((res) => setTimeout(res, 350));
      job = await api(`/api/s/${S.sid}/jobs/${job.job_id}`);
      const steps = (job.steps || []).filter((x) => !x.startsWith("Waiting"));
      typing.innerHTML = `<div class="md"><p>Working… <span class="hint">${job.elapsed_s}s</span></p>${steps.length ? `<ol class="steps">${steps.map((x, i) => `<li class="${i === steps.length - 1 ? "now" : "done"}">${esc(x)}</li>`).join("")}</ol>` : ""}</div>`;
      $("chatLog").scrollTop = $("chatLog").scrollHeight;
    }
    if (job.status === "error") throw new Error(job.error);
    const r = job;
    typing.remove();
    showNotice(r.notice);
    addMsg("bot", r.text, r.trace, r.verification);
    $("chatMode").textContent = r.mode === "llm" ? `${r.provider}${r.cached ? " · cached" : ""}` : "rules";
    S.chat.push({ role: "user", content: text }, { role: "assistant", content: r.text });
    await applyActions(r.actions);
    if ((r.actions || []).length) {   // remember what the map showed for this answer (used by the chat export)
      try {
        const c = document.createElement("canvas"); const scale = 700 / canvas.width;
        c.width = 700; c.height = Math.round(canvas.height * scale);
        c.getContext("2d").drawImage(canvas, 0, 0, c.width, c.height);
        const last = [...$("chatLog").querySelectorAll(".msg.bot")].pop(); if (last) last.dataset.snapshot = c.toDataURL("image/jpeg", 0.8);
      } catch { /* ignore */ }
    }
  } catch (e) { typing.remove(); addMsg("bot", `⚠️ ${e.message}`); }
}
$("chatForm").addEventListener("submit", (e) => { e.preventDefault(); const t = $("chatText").value; $("chatText").value = ""; ask(t); });
$("chatText").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("chatForm").requestSubmit(); } });

function screenContext() {
  const n = S.highlight.nodes.size + S.highlight.links.size;
  return {
    selected: S.selected ? { kind: S.selected.kind, id: S.selected.id } : null,
    color: S.color.attr ? { attr: S.color.attr, stat: S.color.stat } : null,
    time_index: S.sim ? S.t : null,
    time_h: S.sim && S.sim.times_h ? S.sim.times_h[S.t] : null,
    highlight: n ? `${S.highlight.label} (${n})` : null,
    runs: S.runs.map((r, i) => ({ no: S.runs.length - i, sim_id: r.sim_id, label: runLabel(r) })),
  };
}

function showNotice(text) {
  const el = $("chatNotice");
  if (!text) { el.hidden = true; return; }
  el.hidden = false;
  el.innerHTML = `⚠️ ${esc(text)} <button class="btn small" id="noticeFix">Open settings</button>`;
  $("noticeFix").addEventListener("click", () => { $("chatSettings").hidden = false; });
}
$("llmTest").addEventListener("click", async () => {
  $("llmTestOut").textContent = "Testing…";
  try { const r = await api("/api/llm/test", { json: llmConfig() }); $("llmTestOut").textContent = r.message; if (r.ok) showNotice(null); }
  catch (e) { $("llmTestOut").textContent = e.message; }
});

let PROVIDERS = [];
// Keys/models are remembered per provider (only if "remember" is ticked), so other providers can act as backups.
function savedKeys() { try { return JSON.parse(store.get("wdn.llm.keys") || "{}"); } catch { return {}; } }
function llmConfig() {
  const provider = $("llmProvider").value;
  const remember = $("llmRemember").checked;
  const keys = savedKeys();
  if (provider !== "rules") {
    const entry = { model: $("llmModel").value.trim() || null, api_key: $("llmKey").value.trim() || null };
    if (provider === "custom") entry.base_url = $("llmUrl").value.trim() || null;
    if (remember && entry.api_key) keys[provider] = entry; else if (!remember) delete keys[provider];
  }
  store.set("wdn.llm.keys", remember || Object.keys(keys).length ? JSON.stringify(keys) : null);
  store.set("wdn.llm", JSON.stringify({ provider, remember, backups: $("llmBackups").checked }));
  if (provider === "rules") return { provider: "rules" };
  const cfg = { provider, model: $("llmModel").value.trim() || null, api_key: $("llmKey").value.trim() || null };
  if (provider === "custom") cfg.base_url = $("llmUrl").value.trim() || null;
  if ($("llmBackups").checked) {
    cfg.fallbacks = Object.entries(keys).filter(([p, e]) => p !== provider && p !== "custom" && e.api_key)
      .map(([p, e]) => ({ provider: p, model: e.model, api_key: e.api_key }));
  }
  return cfg;
}
async function initLLM() {
  try {
    const r = await api("/api/llm/providers");
    PROVIDERS = r.providers;
    $("llmProvider").innerHTML = `<option value="rules">Built-in rules (no LLM)</option>` +
      PROVIDERS.filter((p) => p.id !== "custom" || r.custom_url_allowed).map((p) => `<option value="${p.id}">${esc(p.label)}${p.server_key ? ` · key from ${p.env_var}` : ""}</option>`).join("");
    let saved = null; try { saved = JSON.parse(store.get("wdn.llm") || "null"); } catch { /* ignore */ }
    if (saved && saved.api_key) {  // migrate the old single-key format
      const keys = savedKeys(); keys[saved.provider] = { model: saved.model, api_key: saved.api_key, base_url: saved.base_url };
      store.set("wdn.llm.keys", JSON.stringify(keys)); saved = { provider: saved.provider, remember: true, backups: true };
      store.set("wdn.llm", JSON.stringify(saved));
    }
    const pick = saved?.provider || r.default || "rules";
    $("llmProvider").value = pick;
    $("llmRemember").checked = !!saved?.remember;
    $("llmBackups").checked = saved ? saved.backups !== false : true;
    onProvider();
  } catch { /* offline */ }
}
function onProvider() {
  const id = $("llmProvider").value;
  const p = PROVIDERS.find((x) => x.id === id);
  const entry = savedKeys()[id] || {};
  $("llmUrlWrap").hidden = id !== "custom";
  $("llmModel").value = entry.model || (p ? p.default_model : "");
  $("llmKey").value = entry.api_key || "";
  $("llmUrl").value = entry.base_url || "";
  const others = Object.keys(savedKeys()).filter((k) => k !== id);
  $("llmHint").innerHTML = (p && p.key_url ? `Get a free key at <a href="${p.key_url}" target="_blank" rel="noopener">${esc(new URL(p.key_url).host)}</a>. Your key is sent with each question to the provider via this server and never stored there.`
    : "Without an LLM the built-in rules answer common questions.") +
    (others.length ? ` Saved backup keys: ${others.map(esc).join(", ")}.` : " Tip: save keys for two providers so one can step in when the other hits its free limit.");
  $("chatMode").textContent = id === "rules" ? "rules" : id;
}
$("llmProvider").addEventListener("change", () => onProvider());
$("listModels").addEventListener("click", async () => {
  try {
    const cfg = llmConfig();
    const r = await api("/api/llm/models", { json: cfg });
    $("modelList").innerHTML = r.models.map((m) => `<option value="${esc(m)}">`).join("");
    $("llmModel").focus();
    addMsg("bot", `Found ${r.models.length} models. Start typing in the Model box to pick one.`);
  } catch (e) { addMsg("bot", `⚠️ ${e.message}`); }
});

// ------------------------------------------------------------- resilience
async function loadLayer(name, colour) {
  const L = await api(`/api/s/${S.sid}/layers/${encodeURIComponent(name)}`);
  S.layers[name] = L;
  if (!$("layerGroup").querySelector(`option[value="layer:${CSS.escape(name)}"]`)) {
    const o = document.createElement("option"); o.value = `layer:${name}`; o.textContent = L.title; $("layerGroup").appendChild(o);
  }
  if (colour) await setColor(`layer:${name}`);
  // offer sibling layers produced by the same analysis (e.g. share from each source)
  try {
    for (const x of await api(`/api/s/${S.sid}/layers`)) {
      if (!$("layerGroup").querySelector(`option[value="layer:${CSS.escape(x.name)}"]`)) {
        const o = document.createElement("option"); o.value = `layer:${x.name}`; o.textContent = x.title; $("layerGroup").appendChild(o);
        S.layers[x.name] = S.layers[x.name] || null;
      }
    }
  } catch { /* ignore */ }
}

function analysisArgs(name, mode) {
  const num = (id) => { const v = $(id).value; return v === "" ? null : parseFloat(v); };
  if (name === "pipe_criticality") return { required_pressure_m: num("critReq"), include: $("critAll").checked ? "all" : "pipes" };
  if (name === "valve_segments") return { placement: $("segPlace").value, n: parseInt($("segN").value || "2", 10) };
  if (name === "fire_flow") {
    const a = { residual_pressure_m: num("ffRes") ?? 14, required_flow_lps: num("ffReq"), system_min_pressure_m: num("ffSys") };
    if (mode === "all") a.all_nodes = true;
    else { a.node = $("ffNode").value.trim(); if (!a.node) throw new Error("Choose a hydrant junction (click one on the map)."); }
    return a;
  }
  if (name === "demand_growth") return { min_pressure_m: num("dgMin") };
  if (name === "leak_scenario") {
    const at = $("lkAt").value.trim(); if (!at) throw new Error("Choose a leak location (click a junction or pipe).");
    return { elements: at.split(/[ ,]+/).filter(Boolean), diameter_mm: num("lkD") ?? 10, duration_h: num("lkDur"), demand_model: $("lkModel").value };
  }
  if (name === "water_quality") return { mode: $("wqMode").value, duration_h: num("wqDur"), chlorine_mgl: num("wqCl") ?? 1, bulk_coeff_per_day: num("wqK") ?? -0.5 };
  return {};
}

async function runAnalysis(name, mode) {
  if (needNet()) return;
  let args;
  try { args = analysisArgs(name, mode); } catch (e) { return toast(e.message); }
  const btns = document.querySelectorAll("[data-analysis]");
  btns.forEach((b) => (b.disabled = true));
  $("resProgress").hidden = false; $("resBar").style.width = "2%"; $("resMsg").textContent = "Starting…";
  try {
    let job = await api(`/api/s/${S.sid}/analysis/${name}`, { json: args });
    while (job.status === "queued" || job.status === "running") {
      await new Promise((r) => setTimeout(r, 400));
      job = await api(`/api/s/${S.sid}/jobs/${job.job_id}`);
      $("resBar").style.width = `${Math.round(job.progress * 100)}%`;
      $("resMsg").textContent = `${job.message} · ${job.elapsed_s}s`;
    }
    if (job.status === "error") throw new Error(job.error);
    setMd($("resOut"), job.markdown);
    const r = job.result || {};
    const rows = r.all || r.top || r.sources || r.curve || r.weakest || null;
    S.resTable = rows && rows.length ? { name, rows: rows.map((x) => Object.fromEntries(Object.entries(x).filter(([k]) => !k.startsWith("_")))) } : null;
    $("resCsv").hidden = !S.resTable;
    S.keepTab = true;
    try { await applyActions(job.actions, r); } finally { S.keepTab = false; }
    $("resOut").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) { toast(e.message); }
  finally { btns.forEach((b) => (b.disabled = false)); $("resProgress").hidden = true; }
}
document.querySelectorAll("[data-analysis]").forEach((b) => b.addEventListener("click", () => runAnalysis(b.dataset.analysis, b.dataset.mode)));
$("resCsv").addEventListener("click", () => { if (S.resTable) downloadCsv(`${S.summary.name}_${S.resTable.name}.csv`, S.resTable.rows); });
$("wqMode").addEventListener("change", () => document.querySelectorAll(".wq-cl").forEach((el) => (el.hidden = $("wqMode").value !== "chlorine")));

// ------------------------------------------------------------- editing
async function openEditor(kind, id) {
  let f;
  try { f = await api(`/api/s/${S.sid}/editable/${kind}/${encodeURIComponent(id)}`); } catch (e) { return toast(e.message); }
  const box = document.createElement("div"); box.className = "card editor";
  box.innerHTML = `<h3>Edit ${esc(f.type)} ${esc(f.id)}</h3><div class="form">` + f.fields.map((x) => {
    const label = x.field.replace(/_(mm|m|lps)$/, "").replace(/_/g, " ");
    const input = x.field === "status"
      ? `<select class="select" data-f="status">${(f.type === "Valve" ? ["Open", "Closed", "Active"] : ["Open", "Closed"]).map((o) => `<option ${o === x.value ? "selected" : ""}>${o}</option>`).join("")}</select>`
      : `<input class="input" type="number" step="any" data-f="${x.field}" value="${x.value ?? ""}">`;
    return `<label>${esc(label)}${x.unit && x.field !== "status" ? ` (${esc(x.unit)})` : ""}${input}</label>`;
  }).join("") + `</div><div class="row"><button class="btn primary grow" data-e="save">Apply edit</button><button class="btn" data-e="cancel">Cancel</button></div>
  <p class="hint">Edits change the model for new simulations, analyses, project saves and the exported .inp. Undo from Overview → Your edits.</p>`;
  $("inspActions").after(box);
  box.querySelector('[data-e="cancel"]').addEventListener("click", () => box.remove());
  box.querySelector('[data-e="save"]').addEventListener("click", async () => {
    const changes = {};
    box.querySelectorAll("[data-f]").forEach((el) => {
      const orig = f.fields.find((x) => x.field === el.dataset.f).value;
      const v = el.tagName === "SELECT" ? el.value : (el.value === "" ? null : parseFloat(el.value));
      if (v !== null && String(v) !== String(orig)) changes[el.dataset.f] = v;
    });
    if (!Object.keys(changes).length) { box.remove(); return; }
    try {
      const r = await api(`/api/s/${S.sid}/element/${kind}/${encodeURIComponent(id)}`, { method: "PATCH", json: { changes } });
      box.remove();
      await afterEdit();
      toast(r.edits.map((e) => `${e.id}: ${e.field.replace(/_/g, " ")} ${e.old} → ${e.new}`).join("; ") + ". Run a new simulation to see the effect.");
      selectElement(kind, id, false);
    } catch (e) { toast(e.message); }
  });
}
async function afterEdit() {
  const g = await api(`/api/s/${S.sid}/geometry`);
  S.nodes = g.nodes; S.links = g.links;
  S.nodeIdx = new Map(S.nodes.map((n, i) => [n.id, i])); S.linkIdx = new Map(S.links.map((l, i) => [l.id, i]));
  const dia = S.links.map((l) => l.diameter_mm).filter((d) => d > 0);
  S.diaRange = dia.length ? [Math.min(...dia), Math.max(...dia)] : [1, 1];
  S.layers = {}; $("layerGroup").innerHTML = "";
  if (S.color.attr.startsWith("layer:")) S.color.attr = "";
  await refreshEdits();
  await refreshColor();
}
async function refreshEdits() {
  const list = await api(`/api/s/${S.sid}/edits`);
  $("editsCard").hidden = !list.length;
  $("editsList").innerHTML = list.slice(-12).reverse().map((e) => `<div class="hint"><code class="id" data-id="${esc(e.id)}">${esc(e.id)}</code> ${esc(e.field.replace(/_/g, " "))}: ${esc(String(e.old))} → <b>${esc(String(e.new))}</b> <span>(${esc(e.at_epoch ? new Date(e.at_epoch * 1000).toLocaleTimeString() : e.at)})</span></div>`).join("");
  $("editsHint").textContent = `${list.length} edit(s) — runs made before an edit don't include it.`;
}
$("undoBtn").addEventListener("click", async () => {
  try { const r = await api(`/api/s/${S.sid}/undo`, { method: "POST" }); await afterEdit(); toast(`Undid ${r.undone.id} ${r.undone.field.replace(/_/g, " ")} → ${r.undone.old}`); }
  catch (e) { toast(e.message); }
});
$("saveProjBtn").addEventListener("click", saveProject);

// ------------------------------------------------------------- report & chat export
$("reportBtn").addEventListener("click", () => { if (S.sid) { $("reportModal").hidden = false; $("repLink").hidden = true; $("repProgress").hidden = true; } });
$("reportClose").addEventListener("click", () => { $("reportModal").hidden = true; });
$("repGo").addEventListener("click", async () => {
  const body = { format: $("repFormat").value, style: $("repStyle").value, author: $("repAuthor").value.trim(),
    maps: $("repMaps").checked, assessment: $("repAssess").checked, discussion: $("repLLM").checked };
  if (body.discussion) {
    const cfg = llmConfig();
    if (cfg.provider === "rules") return toast("Choose an LLM provider in the chat ⚙️ settings first, or untick the LLM discussion.");
    body.llm = cfg;
  }
  $("repGo").disabled = true; $("repProgress").hidden = false; $("repBar").style.width = "3%"; $("repMsg").textContent = "Starting…";
  try {
    let job = await api(`/api/s/${S.sid}/report`, { json: body });
    while (job.status === "queued" || job.status === "running") {
      await new Promise((r) => setTimeout(r, 400));
      job = await api(`/api/s/${S.sid}/jobs/${job.job_id}`);
      $("repBar").style.width = `${Math.round(job.progress * 100)}%`; $("repMsg").textContent = `${job.message} · ${job.elapsed_s}s`;
    }
    if (job.status === "error") throw new Error(job.error);
    const href = `/api/s/${S.sid}/report/${job.job_id}`;
    location.href = href;
    $("repLink").href = href; $("repLink").hidden = false;
    $("repLink").textContent = `⬇ Download report again (${job.format.toUpperCase()}, ${job.size_kb} KB)`;
    $("repMsg").textContent = `Done — saved to your Downloads folder as ${S.summary.name}_report.${job.format} (Ctrl+J shows downloads).`;
  } catch (e) { toast(e.message); $("repMsg").textContent = e.message; }
  finally { $("repGo").disabled = false; }
});

function chatTranscript() {
  // the visible conversation incl. verification chips and map snapshots taken when answers arrived
  return [...$("chatLog").querySelectorAll(".msg")].filter((m) => !m.classList.contains("typing")).map((m) => ({
    role: m.classList.contains("user") ? "user" : "assistant",
    html: m.classList.contains("user") ? esc(m.textContent) : (m.querySelector(".md")?.innerHTML || ""),
    text: m.textContent,
    trace: m.querySelector(".trace")?.innerText.replace("↻ re-run these calculations", "").trim() || "",
    snapshot: m.dataset.snapshot || null,
  }));
}
function exportChat() {
  if (!S.sid) return;
  const msgs = chatTranscript();
  if (msgs.length < 2) return toast("Nothing to export yet — ask a question first.");
  const when = new Date().toLocaleString();
  const body = msgs.map((m) => `<div class="m ${m.role}"><div class="who">${m.role === "user" ? "You" : "WDN Studio"}</div><div class="c">${m.html}</div>` +
    (m.trace ? `<div class="tr">${esc(m.trace)}</div>` : "") + (m.snapshot ? `<img src="${m.snapshot}" alt="map at the time of this answer">` : "") + `</div>`).join("");
  const html = `<!doctype html><html><head><meta charset="utf-8"><title>${esc(S.summary.name)} — analysis log</title>
<style>body{font:14px/1.5 system-ui,sans-serif;max-width:860px;margin:24px auto;padding:0 16px;color:#0b0b0b}h1{font-size:20px}
.m{border:1px solid #dedcd6;border-radius:10px;padding:10px 14px;margin:10px 0}.user{background:#eef4fc}.who{font-size:12px;color:#52514e;font-weight:600}
table{border-collapse:collapse;font-size:12px}td,th{border-bottom:1px solid #dedcd6;padding:3px 6px;text-align:left}code{background:#f0efec;padding:0 4px;border-radius:4px}
.tr{font-size:11px;color:#8a8984;margin-top:6px}img{max-width:100%;border:1px solid #dedcd6;border-radius:6px;margin-top:8px}mark{background:#fde9b5}</style></head>
<body><h1>${esc(S.summary.name)} — analysis log</h1><p style="color:#52514e">Exported from WDN Studio on ${esc(when)}. Every number was computed by the engine; numbers marked ⚠ could not be matched to a calculation.</p>${body}</body></html>`;
  downloadBlob(`${S.summary.name}_analysis_log.html`, new Blob([html], { type: "text/html" }));
  const mdText = msgs.map((m) => `### ${m.role === "user" ? "You" : "WDN Studio"}\n\n${m.text.trim()}\n`).join("\n");
  S.lastChatMd = mdText;
}
$("chatExportBtn").addEventListener("click", exportChat);

// ------------------------------------------------------------- charts panel
const SERIES_LIGHT = ["#2a78d6", "#eb6834"], SERIES_DARK = ["#3987e5", "#d95926"];
const seriesColor = (i) => (isDark() ? SERIES_DARK : SERIES_LIGHT)[i % 2];
function niceTicks(lo, hi, n = 4) {
  if (!(hi > lo)) { hi = lo + 1; }
  const step0 = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((x) => x >= step0) || step0;
  const out = []; for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}
function chartCard(title, unit, build, csvRows, csvName) {
  const card = document.createElement("div"); card.className = "ccard";
  card.innerHTML = `<div class="chead"><span class="t">${esc(title)}</span><span class="u">${esc(unit || "")}</span><span class="sp"></span>
    <button class="linkbtn" data-a="png">⬇ PNG</button><button class="linkbtn" data-a="csv">⬇ CSV</button></div>`;
  build(card);
  card.querySelector('[data-a="csv"]').addEventListener("click", () => downloadCsv(`${S.summary.name}_${csvName}.csv`, csvRows()));
  card.querySelector('[data-a="png"]').addEventListener("click", () => {
    const legend = [...card.querySelectorAll(".legend2 span")].map((sp) => ({ color: sp.querySelector("i").style.background, text: sp.textContent }));
    svgToPng(card.querySelector("svg"), `${S.summary.name}_${csvName}.png`, title + (unit ? ` (${unit})` : ""), legend);
  });
  return card;
}
function svgToPng(svg, name, title, legend = []) {
  if (!svg) return;
  const W = svg.viewBox.baseVal.width, H = svg.viewBox.baseVal.height, scale = 2, head = 34, foot = legend.length ? 26 : 0;
  const clone = svg.cloneNode(true);
  clone.querySelectorAll('[class^="dot"], .xh').forEach((n) => n.remove());   // no hover marks in the export
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg"); clone.setAttribute("width", W); clone.setAttribute("height", H);
  clone.setAttribute("font-family", "system-ui, -apple-system, Segoe UI, Roboto, sans-serif");
  const blob = new Blob([new XMLSerializer().serializeToString(clone)], { type: "image/svg+xml" });
  const url = URL.createObjectURL(blob); const img = new Image();
  img.onload = () => {
    const c = document.createElement("canvas"); c.width = W * scale; c.height = (H + head + foot) * scale;
    const g = c.getContext("2d"); g.scale(scale, scale);
    g.fillStyle = css("--surface"); g.fillRect(0, 0, W, H + head + foot);
    g.fillStyle = css("--text"); g.font = "600 14px system-ui, sans-serif"; g.fillText(title, 10, 20);
    g.drawImage(img, 0, head); URL.revokeObjectURL(url);
    let x = 12; g.font = "12px system-ui, sans-serif";
    for (const it of legend) {
      g.fillStyle = it.color; g.fillRect(x, H + head + 10, 14, 3);
      g.fillStyle = css("--text-2"); g.fillText(it.text, x + 20, H + head + 15); x += 30 + g.measureText(it.text).width;
    }
    c.toBlob((b) => downloadBlob(name, b), "image/png");
  };
  img.src = url;
}
// Multi-series line chart (thin 2px lines, recessive grid, crosshair tooltip). series: [{name, xs, ys, color, dash, step}]
function lineSvg(card, series, opts = {}) {
  const W = 560, H = 220, P = { l: 46, r: 12, t: 10, b: 26 };
  const xsAll = series.flatMap((s) => s.xs), ysAll = series.flatMap((s) => s.ys).filter((v) => v !== null && Number.isFinite(v));
  (opts.limits || []).forEach((l) => ysAll.push(l.y));
  if (opts.band) opts.band.lo.concat(opts.band.hi).forEach((v) => v !== null && ysAll.push(v));
  if (!ysAll.length || !xsAll.length) { card.insertAdjacentHTML("beforeend", `<div class="empty">No data</div>`); return; }
  let x0 = Math.min(...xsAll), x1 = Math.max(...xsAll); if (x1 === x0) x1 = x0 + 1;
  let y0 = Math.min(...ysAll), y1 = Math.max(...ysAll); if (opts.zero) y0 = Math.min(0, y0);
  const pad = (y1 - y0 || 1) * 0.08; y0 -= y0 === 0 ? 0 : pad; y1 += pad;
  const X = (x) => P.l + (x - x0) / (x1 - x0) * (W - P.l - P.r), Y = (y) => H - P.b - (y - y0) / (y1 - y0) * (H - P.t - P.b);
  const grid = css("--border"), ink = css("--muted");
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opts.label || "chart")}" font-family="system-ui, sans-serif">`;
  for (const t of niceTicks(y0, y1)) svg += `<line x1="${P.l}" x2="${W - P.r}" y1="${Y(t)}" y2="${Y(t)}" stroke="${grid}" stroke-width="1"/><text x="${P.l - 6}" y="${Y(t) + 3}" text-anchor="end" font-size="10" fill="${ink}">${fmt(t, 2)}</text>`;
  for (const t of niceTicks(x0, x1, 6)) svg += `<text x="${X(t)}" y="${H - 8}" text-anchor="middle" font-size="10" fill="${ink}">${fmt(t, 1)}${opts.xUnit || ""}</text>`;
  if (opts.band) {
    const { xs, lo, hi } = opts.band;
    const up = xs.map((x, i) => `${X(x)},${Y(hi[i] ?? lo[i] ?? y0)}`).join(" "), dn = xs.map((x, i) => `${X(x)},${Y(lo[i] ?? y0)}`).reverse().join(" ");
    svg += `<polygon points="${up} ${dn}" fill="${seriesColor(0)}" fill-opacity="0.15" stroke="none"/>`;
  }
  for (const l of opts.limits || []) svg += `<line x1="${P.l}" x2="${W - P.r}" y1="${Y(l.y)}" y2="${Y(l.y)}" stroke="${css("--critical")}" stroke-dasharray="4 3" stroke-width="1.2"/><text x="${W - P.r}" y="${Y(l.y) - 4}" text-anchor="end" font-size="10" fill="${css("--critical")}">${esc(l.label)}</text>`;
  for (const s of series) {
    let d = "", pen = false;
    s.ys.forEach((y, i) => {
      if (y === null || !Number.isFinite(y)) { pen = false; return; }
      const px = X(s.xs[i]).toFixed(1), py = Y(y).toFixed(1);
      if (s.step && pen && i > 0) d += `H${px}V${py}`; else d += `${pen ? "L" : "M"}${px},${py}`;
      pen = true;
    });
    svg += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" ${s.dash ? 'stroke-dasharray="5 4"' : ""} stroke-linejoin="round"/>`;
  }
  svg += `<line class="xh" x1="-10" x2="-10" y1="${P.t}" y2="${H - P.b}" stroke="${ink}" stroke-width="1"/>`;
  svg += series.map((s, i) => `<circle class="dot${i}" r="4" cx="-10" cy="-10" fill="${s.color}" stroke="${css("--surface")}" stroke-width="2"/>`).join("");
  svg += `<rect x="${P.l}" y="0" width="${W - P.l - P.r}" height="${H}" fill="transparent"/></svg>`;
  card.insertAdjacentHTML("beforeend", svg);
  if (series.length > 1) card.insertAdjacentHTML("beforeend", `<div class="legend2">${series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>`);
  const el = card.querySelector("svg"), tip = document.createElement("div"); tip.className = "ctip"; tip.hidden = true; card.appendChild(tip);
  el.addEventListener("pointermove", (e) => {
    const r = el.getBoundingClientRect(), x = x0 + ((e.clientX - r.left) / r.width * W - P.l) / (W - P.l - P.r) * (x1 - x0);
    const lines = [];
    series.forEach((s, si) => {
      let best = 0, bd = Infinity; s.xs.forEach((t, i) => { const dd = Math.abs(t - x); if (dd < bd) { bd = dd; best = i; } });
      const y = s.ys[best], dot = el.querySelector(`.dot${si}`);
      if (y === null || !Number.isFinite(y)) { dot.setAttribute("cx", -10); return; }
      dot.setAttribute("cx", X(s.xs[best])); dot.setAttribute("cy", Y(y));
      lines.push(`<span style="color:${s.color}">●</span> ${esc(s.name)}: <b>${fmt(y, 3)}</b>`);
      if (si === 0) { el.querySelector(".xh").setAttribute("x1", X(s.xs[best])); el.querySelector(".xh").setAttribute("x2", X(s.xs[best])); lines.unshift(`${opts.xName || "t"} = ${fmt(s.xs[best], 2)}${opts.xUnit || ""}`); }
    });
    tip.innerHTML = lines.join("<br>"); tip.hidden = false;
    const cr = card.getBoundingClientRect();
    tip.style.left = Math.min(cr.width - tip.offsetWidth - 6, e.clientX - cr.left + 12) + "px"; tip.style.top = (e.clientY - cr.top + 12) + "px";
  });
  el.addEventListener("pointerleave", () => { tip.hidden = true; el.querySelector(".xh").setAttribute("x1", -10); el.querySelector(".xh").setAttribute("x2", -10); });
}
// Histogram: adjacent bars with a 2px surface gap; second run drawn as an outline step for comparison.
function histSvg(card, h, names) {
  const W = 560, H = 220, P = { l: 40, r: 12, t: 10, b: 26 };
  if (!h.edges.length) { card.insertAdjacentHTML("beforeend", `<div class="empty">No data</div>`); return; }
  const e = h.edges, nb = e.length - 1, ymax = Math.max(1, ...h.counts.flat());
  const X = (v) => P.l + (v - e[0]) / (e[nb] - e[0]) * (W - P.l - P.r), Y = (c) => H - P.b - c / ymax * (H - P.t - P.b);
  const grid = css("--border"), ink = css("--muted"), surf = css("--surface");
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="histogram" font-family="system-ui, sans-serif">`;
  for (const t of niceTicks(0, ymax)) svg += `<line x1="${P.l}" x2="${W - P.r}" y1="${Y(t)}" y2="${Y(t)}" stroke="${grid}"/><text x="${P.l - 6}" y="${Y(t) + 3}" text-anchor="end" font-size="10" fill="${ink}">${fmt(t, 0)}</text>`;
  for (const t of niceTicks(e[0], e[nb], 6)) svg += `<text x="${X(t)}" y="${H - 8}" text-anchor="middle" font-size="10" fill="${ink}">${fmt(t, 2)}</text>`;
  h.counts[0].forEach((c, i) => {
    if (!c) return;
    const x = X(e[i]) + 1, w = Math.max(1, X(e[i + 1]) - X(e[i]) - 2);
    svg += `<rect class="bar" data-i="${i}" x="${x}" y="${Y(c)}" width="${w}" height="${H - P.b - Y(c)}" rx="2" fill="${seriesColor(0)}" stroke="${surf}" stroke-width="0"/>`;
  });
  if (h.counts[1]) {
    let d = `M${X(e[0])},${Y(0)}`;
    h.counts[1].forEach((c, i) => { d += `V${Y(c)}H${X(e[i + 1])}`; });
    svg += `<path d="${d}V${Y(0)}" fill="none" stroke="${seriesColor(1)}" stroke-width="2"/>`;
  }
  svg += `<rect x="${P.l}" y="0" width="${W - P.l - P.r}" height="${H}" fill="transparent"/></svg>`;
  card.insertAdjacentHTML("beforeend", svg);
  if (h.counts[1]) card.insertAdjacentHTML("beforeend", `<div class="legend2"><span><i style="background:${seriesColor(0)}"></i>${esc(names[0])} (bars)</span><span><i style="background:${seriesColor(1)}"></i>${esc(names[1])} (outline)</span></div>`);
  const el = card.querySelector("svg"), tip = document.createElement("div"); tip.className = "ctip"; tip.hidden = true; card.appendChild(tip);
  el.addEventListener("pointermove", (ev) => {
    const r = el.getBoundingClientRect(), xv = e[0] + ((ev.clientX - r.left) / r.width * W - P.l) / (W - P.l - P.r) * (e[nb] - e[0]);
    const i = Math.min(nb - 1, Math.max(0, Math.floor((xv - e[0]) / (e[nb] - e[0]) * nb)));
    tip.innerHTML = `${fmt(e[i], 2)} – ${fmt(e[i + 1], 2)}<br>${h.counts.map((c, k) => `<span style="color:${seriesColor(k)}">●</span> ${esc(names[k])}: <b>${c[i]}</b>`).join("<br>")}`;
    tip.hidden = false;
    const cr = card.getBoundingClientRect();
    tip.style.left = Math.min(cr.width - tip.offsetWidth - 6, ev.clientX - cr.left + 12) + "px"; tip.style.top = (ev.clientY - cr.top + 12) + "px";
  });
  el.addEventListener("pointerleave", () => { tip.hidden = true; });
}

async function openCharts() {
  if (!S.sim) return toast("Run a simulation first.");
  $("chartsPanel").hidden = false;
  const others = S.runs.filter((r) => r.sim_id !== S.sim.sim_id);
  const keep = $("chartsOther").value;
  $("chartsOther").innerHTML = `<option value="">— none —</option>` + others.map((r) => `<option value="${r.sim_id}">${esc(runLabel(r))}</option>`).join("");
  if (others.some((r) => r.sim_id === keep)) $("chartsOther").value = keep;
  await renderCharts();
}
async function renderCharts() {
  const other = $("chartsOther").value;
  const grid = $("chartsGrid"); grid.innerHTML = `<div class="empty">Loading…</div>`;
  let d;
  try { d = await api(`/api/s/${S.sid}/sim/${S.sim.sim_id}/charts${other ? `?other=${other}` : ""}`); }
  catch (e) { grid.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  const runA = runLabel(S.sim), runB = other ? runLabel(S.runs.find((r) => r.sim_id === other) || { settings: {}, sim_id: other }) : null;
  const nameA = other ? "shown run" : runA, nameB = "compared run";
  $("chartsRun").textContent = `Shown run: ${runA}`;
  grid.innerHTML = "";
  const minP = S.criteria.min_pressure_m, maxV = S.criteria.max_velocity_ms;
  const histRows = (h) => () => h.edges.slice(0, -1).map((e0, i) => ({ from: e0, to: h.edges[i + 1], shown_run: h.counts[0][i], ...(h.counts[1] ? { compared_run: h.counts[1][i] } : {}) }));
  grid.appendChild(chartCard("Minimum pressure per junction", "count of junctions per pressure band (m)", (c) => histSvg(c, d.pressure_hist, [nameA, nameB]), histRows(d.pressure_hist), "pressure_histogram"));
  grid.appendChild(chartCard("Peak velocity per pipe", "count of pipes per velocity band (m/s)", (c) => histSvg(c, d.velocity_hist, [nameA, nameB]), histRows(d.velocity_hist), "velocity_histogram"));
  if (!d.single_step) {
    const xs = d.times_h, xsB = d.other_times_h || xs;
    const seriesOf = (arr) => arr.map((ys, k) => ({ name: k ? nameB : nameA, xs: k ? xsB : xs, ys, color: seriesColor(k), dash: k === 1 }));
    grid.appendChild(chartCard("Total demand", "L/s", (c) => lineSvg(c, seriesOf(d.total_demand.series), { xUnit: " h", zero: true }),
      () => xs.map((t, i) => ({ time_h: t, shown_run: d.total_demand.series[0][i], ...(d.total_demand.series[1] ? { compared_run: d.total_demand.series[1][i] } : {}) })), "total_demand"));
    grid.appendChild(chartCard("Pressure across the network", "m — band = 5th–95th percentile of junctions, line = median, dashed = minimum",
      (c) => lineSvg(c, [{ name: "median", xs, ys: d.pressure_band.p50, color: seriesColor(0) }, { name: "minimum", xs, ys: d.pressure_band.min, color: seriesColor(1), dash: true }],
        { band: { xs, lo: d.pressure_band.p5, hi: d.pressure_band.p95 }, xUnit: " h", limits: minP != null ? [{ y: minP, label: `min ${minP} m` }] : [] }),
      () => xs.map((t, i) => ({ time_h: t, p5: d.pressure_band.p5[i], median: d.pressure_band.p50[i], p95: d.pressure_band.p95[i], minimum: d.pressure_band.min[i] })), "pressure_band"));
    for (const t of d.tanks) {
      grid.appendChild(chartCard(`Tank ${t.id} level`, "m", (c) => lineSvg(c, seriesOf(t.series), { xUnit: " h", limits: [{ y: t.min, label: "min" }, { y: t.max, label: "max" }] }),
        () => xs.map((x, i) => ({ time_h: x, shown_run: t.series[0][i], ...(t.series[1] ? { compared_run: t.series[1][i] } : {}) })), `tank_${t.id}`));
    }
    for (const p of d.pumps) {
      grid.appendChild(chartCard(`Pump ${p.id} flow`, "L/s (0 = off)", (c) => lineSvg(c, p.flow.map((ys, k) => ({ name: k ? nameB : nameA, xs: k ? xsB : xs, ys, color: seriesColor(k), dash: k === 1, step: true })), { xUnit: " h", zero: true }),
        () => xs.map((x, i) => ({ time_h: x, flow_lps: p.flow[0][i], status: p.status[0][i], ...(p.flow[1] ? { compared_flow_lps: p.flow[1][i] } : {}) })), `pump_${p.id}`));
    }
  } else {
    grid.insertAdjacentHTML("beforeend", `<div class="ccard"><div class="empty">The shown run is steady-state (one time step), so there are no time-series charts. Run with a Duration (e.g. 24 h).</div></div>`);
  }
  if (maxV != null) grid.querySelectorAll(".ccard")[1]?.insertAdjacentHTML("beforeend", `<div class="hint">Your max velocity limit: ${maxV} m/s</div>`);
}
$("chartsBtn").addEventListener("click", openCharts);
$("chartsClose").addEventListener("click", () => { $("chartsPanel").hidden = true; });
$("chartsOther").addEventListener("change", renderCharts);

// ------------------------------------------------------------- help (ⓘ)
let GLOSSARY = null;
const HELP_FOR = {
  simDuration: "eps", simModel: "pdd", simReqP: "required_pressure", simMult: "demand_multiplier", simAge: "water_age",
  critReq: "criticality", segPlace: "segment", ffRes: "fire_flow", dgMin: "demand_growth", lkD: "leak", wqMode: "chlorine",
  presetSelect: "min_pressure", statSelect: "steady_state",
};
const CRIT_HELP = { min_pressure_m: "min_pressure", max_pressure_m: "max_pressure", min_velocity_ms: "min_velocity",
  max_velocity_ms: "max_velocity", max_headloss_m_per_km: "max_headloss" };
const METRIC_HELP = { betweenness_centrality: "betweenness", closeness_centrality: "closeness", eigenvector_centrality: "eigenvector",
  degree: "degree", clustering: "clustering", articulation: "articulation", edge_betweenness: "betweenness", bridge: "bridge" };
function helpIcon(term) {
  const b = document.createElement("button");
  b.type = "button"; b.className = "help"; b.textContent = "ⓘ"; b.setAttribute("aria-label", "Explain");
  b.dataset.term = term;
  return b;
}
function attachHelp(label, term) {
  if (!label || label.querySelector(".help")) return;
  const control = label.querySelector("input, select");
  if (control && control.type === "checkbox") { label.appendChild(helpIcon(term)); return; }
  // keep the label text and the icon on one line above the input
  const head = document.createElement("span"); head.className = "lbl";
  for (const n of [...label.childNodes]) { if (n === control) break; head.appendChild(n); }
  head.appendChild(helpIcon(term));
  label.insertBefore(head, label.firstChild);
}
function addHelpIcons() {
  for (const [id, term] of Object.entries(HELP_FOR)) {
    const el = $(id); attachHelp(el && el.closest("label"), term);
  }
  document.querySelectorAll("[data-crit]").forEach((el) => attachHelp(el.closest("label"), CRIT_HELP[el.dataset.crit]));
  const ms = $("metricSelect");
  if (ms && !ms.parentElement.querySelector(".help")) {
    const b = helpIcon("betweenness"); b.dataset.dynamic = "metric"; ms.after(b);
  }
  [["Topology", "bridge"], ["Pipe criticality (N-1)", "criticality"], ["Valve isolation segments", "segment"], ["Fire flow", "fire_flow"],
   ["Demand growth capacity", "demand_growth"], ["Leak scenario", "leak"], ["Water quality", "trace"]].forEach(([title, term]) => {
    const h = [...document.querySelectorAll(".card h3")].find((x) => x.textContent.trim() === title);
    if (h && !h.querySelector(".help")) h.appendChild(helpIcon(term));
  });
}
document.addEventListener("click", async (e) => {
  const b = e.target.closest(".help");
  const pop = $("helpPop");
  if (!b) { if (!e.target.closest("#helpPop")) pop.hidden = true; return; }
  e.preventDefault(); e.stopPropagation();
  if (!GLOSSARY) { try { GLOSSARY = await api("/api/glossary"); } catch { return; } }
  let term = b.dataset.term;
  if (b.dataset.dynamic === "metric") term = METRIC_HELP[$("metricSelect").value.split(":")[1]] || term;
  const g = GLOSSARY[term]; if (!g) return;
  pop.innerHTML = `<strong>${esc(g.title)}</strong><p>${esc(g.short)}</p><p class="hint">${esc(g.detail)}</p>`;
  pop.hidden = false;
  const r = b.getBoundingClientRect();
  pop.style.left = Math.min(window.innerWidth - 336, Math.max(8, r.left - 8)) + "px";
  pop.style.top = Math.min(window.innerHeight - pop.offsetHeight - 8, r.bottom + 6) + "px";
});
addHelpIcons();

// ----------------------------------------------------------------- exports
function csvCell(v) {
  if (v === null || v === undefined) return "";
  const t = Array.isArray(v) ? v.join(" ") : String(v);
  return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
}
function downloadBlob(name, blob) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
function downloadCsv(name, rows) {
  if (!rows || !rows.length) return toast("Nothing to export.");
  const cols = [...new Set(rows.flatMap((r) => Object.keys(r)))];
  const text = [cols.join(","), ...rows.map((r) => cols.map((c) => csvCell(r[c])).join(","))].join("\r\n");
  downloadBlob(name, new Blob(["\ufeff" + text], { type: "text/csv;charset=utf-8" }));
}
function tableFromResult(tool, r) {
  if (!r || r.success === false) return null;
  const ids = (list, col) => list.map((id) => ({ [col]: id }));
  if (r.values && r.metric) return { name: `${r.metric}${r.weighted ? "_weighted" : ""}`, rows: Object.entries(r.values).map(([id, v]) => ({ id, [r.metric]: v })).sort((a, b) => b[r.metric] - a[r.metric]) };
  if (r.pipes) return { name: "pipes_filter", rows: r.pipes };
  if (r.path_nodes) return { name: `path_${r.source}_${r.target}`, rows: r.path_nodes.map((n, i) => ({ step: i, node: n, next_link: r.path_links[i] || "" })) };
  if (r.components) return { name: "distance_statistics", rows: r.components };
  if (tool === "topology_characterization") {
    const f = r.focus;
    if (f === "dead_ends") return { name: "dead_ends", rows: ids(r.dead_end_junctions, "junction") };
    if (f === "bridges") return { name: "bridges", rows: r.bridges };
    if (f === "articulation_points") return { name: "articulation_points", rows: ids(r.articulation_points, "node") };
    if (f === "unsupplied") return { name: "no_path_to_source", rows: ids(r.source_connectivity.unsupplied_junctions, "junction") };
    return { name: "characterization", rows: Object.entries(r).filter(([, v]) => typeof v !== "object" || v === null).map(([k, v]) => ({ item: k, value: v })) };
  }
  return null;
}
$("analysisCsv").addEventListener("click", () => { if (S.lastTable) downloadCsv(`${S.summary.name}_${S.lastTable.name}.csv`, S.lastTable.rows); });
$("netXlsx").addEventListener("click", () => { if (S.sid) location.href = `/api/s/${S.sid}/export/network.xlsx`; });
$("simXlsx").addEventListener("click", () => { if (S.sim) location.href = `/api/s/${S.sid}/sim/${S.sim.sim_id}/export.xlsx`; });

function exportPNG() {
  if (!S.nodes.length) return;
  const W = canvas.width, H = canvas.height, d = DPR;
  const out = document.createElement("canvas");
  const header = 44 * d;
  out.width = W; out.height = H + header;
  const c = out.getContext("2d");
  c.fillStyle = css("--map-bg"); c.fillRect(0, 0, out.width, out.height);
  c.drawImage(canvas, 0, header);
  c.scale(d, d);
  c.fillStyle = css("--text"); c.font = "600 15px system-ui, sans-serif";
  const time = S.sim && S.sim.times_h.length > 1 && (!S.colorData || S.color.stat === "none") ? ` · ${$("timeLabel").textContent}` : "";
  c.fillText(`${S.summary.name}${S.colorData ? " · " + S.colorData.title : ""}${time}`, 14, 20);
  c.fillStyle = css("--text-2"); c.font = "12px system-ui, sans-serif";
  const hl = S.highlight.nodes.size + S.highlight.links.size ? ` · highlighted: ${S.highlight.label} (${S.highlight.nodes.size + S.highlight.links.size})` : "";
  c.fillText(`WDN Studio · ${new Date().toLocaleString()}${hl}`, 14, 37);
  const cd = S.colorData;
  if (cd) {   // legend, bottom-left
    const lw = 240, lh = 56, x = 12, y = (H / d) + 44 - lh - 12;
    c.fillStyle = css("--surface"); c.strokeStyle = css("--border"); c.lineWidth = 1;
    c.fillRect(x, y, lw, lh); c.strokeRect(x, y, lw, lh);
    c.fillStyle = css("--text"); c.font = "600 12px system-ui, sans-serif";
    c.fillText(`${cd.title}${cd.unit ? " (" + cd.unit + ")" : ""}`.slice(0, 40), x + 10, y + 17);
    if (cd.binary) {
      c.fillStyle = css("--highlight"); c.fillRect(x + 10, y + 26, 30, 8);
      c.fillStyle = css("--text-2"); c.fillText("yes", x + 46, y + 34);
    } else {
      const g = c.createLinearGradient(x + 10, 0, x + lw - 10, 0);
      if (cd.diverging) { g.addColorStop(0, divColor(-1)); g.addColorStop(0.5, divColor(0)); g.addColorStop(1, divColor(1)); }
      else (isDark() ? RAMP_DARK : RAMP).forEach((col, i, arr) => g.addColorStop(i / (arr.length - 1), col));
      c.fillStyle = g; c.fillRect(x + 10, y + 25, lw - 20, 9);
      c.fillStyle = css("--text-2"); c.font = "11px system-ui, sans-serif";
      c.fillText(fmt(cd.lo, 3), x + 10, y + 48);
      const hi = fmt(cd.hi, 3); c.fillText(hi, x + lw - 10 - c.measureText(hi).width, y + 48);
    }
  }
  out.toBlob((b) => downloadBlob(`${S.summary.name}_map${cd ? "_" + (cd.attr || "colour") : ""}.png`, b), "image/png");
}
$("pngBtn").addEventListener("click", exportPNG);
$("zoomIn").addEventListener("click", () => zoomCenter(1.6));
$("zoomOut").addEventListener("click", () => zoomCenter(1 / 1.6));

// ------------------------------------------------------------------ wiring
$("fileInput").addEventListener("change", (e) => loadFile(e.target.files[0]));
$("fileInput2").addEventListener("change", (e) => loadFile(e.target.files[0]));
for (const el of [$("dropzone"), $("mapwrap")]) {
  el.addEventListener("dragover", (e) => { e.preventDefault(); $("dropzone").classList.add("drag"); });
  el.addEventListener("dragleave", () => $("dropzone").classList.remove("drag"));
  el.addEventListener("drop", (e) => { e.preventDefault(); $("dropzone").classList.remove("drag"); loadFile(e.dataTransfer.files[0]); });
}
$("sampleSelect").addEventListener("change", (e) => { loadSample(e.target.value); e.target.value = ""; });
$("exportBtn").addEventListener("click", () => { if (S.sid) location.href = `/api/s/${S.sid}/export`; });
$("fitBtn").addEventListener("click", userFit);
$("clearHl").addEventListener("click", () => setHighlight(null));
$("inspClose").addEventListener("click", closeInspector);
$("colorSelect").addEventListener("change", (e) => setColor(e.target.value, e.target.value in SIM_VARS && !S.color.stat ? "none" : undefined));
$("statSelect").addEventListener("change", (e) => setColor(S.color.attr, e.target.value));
document.addEventListener("keydown", (e) => {
  if (e.target.closest("input, textarea, select")) return;
  if (e.key === "+" || e.key === "=") zoomCenter(1.6);
  if (e.key === "-" || e.key === "_") zoomCenter(1 / 1.6);
});
$("widthToggle").addEventListener("change", draw);
$("arrowToggle").addEventListener("change", async () => { await refreshArrows(); draw(); });
$("searchInput").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  const v = e.target.value.trim();
  const hit = S.nodeIdx.has(v) ? "node" : S.linkIdx.has(v) ? "link" : null;
  if (!hit) {
    const low = v.toLowerCase();
    const n = S.nodes.find((x) => x.id.toLowerCase() === low), l = S.links.find((x) => x.id.toLowerCase() === low);
    if (n) return selectElement("node", n.id, true);
    if (l) return selectElement("link", l.id, true);
    return toast(`No node or link '${v}'.`);
  }
  selectElement(hit, v, true);
});
$("themeBtn").addEventListener("click", () => {
  const next = isDark() ? "light" : "dark";
  document.documentElement.dataset.theme = next; store.set("wdn.theme", next);
  renderLegend(); if (S.colorData) refreshColor(); else draw();
});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { renderLegend(); draw(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeInspector(); } });

(async function init() {
  const theme = store.get("wdn.theme"); if (theme) document.documentElement.dataset.theme = theme;
  try {
    const samples = await api("/api/samples");
    $("sampleSelect").innerHTML = `<option value="">Samples…</option>` + samples.map((s) => `<option value="${esc(s.name)}" title="${esc(s.description)}">${esc(s.name)} — ${esc(s.description)}</option>`).join("");
    PRESETS = await api("/api/criteria/presets");
    $("presetSelect").innerHTML = Object.entries(PRESETS).map(([k, p]) => `<option value="${k}">${esc(p.label)}</option>`).join("");
  } catch { /* server not ready */ }
  initLLM();
  resize();
  const q = new URLSearchParams(location.search).get("sample");
  if (q) loadSample(q);
})();
