// PCB Studio front end: project picker, chat (SSE over fetch), PCB / BOM / DRC preview, layer filters.
import { Viewer, layerColor } from "./viewer.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private window etc. */ } },
};

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: opts.body ? { "Content-Type": "application/json" } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch { /* not json */ }
    throw new Error(msg);
  }
  return r.json();
}

const S = { pid: null, board: null, chat: null, busy: false, bom: [], drc: null, refs: [], net: 0, abort: null, phases: null,
  netlist: null, layout: null, routing: null, preview: null, rtPoll: null };

// ---- viewer --------------------------------------------------------------------------------
const viewer = new Viewer($("board"), {
  onSelect(refs, net) { S.refs = refs; S.net = net; renderCtx(); renderInfo(); markBomRows(); },
  onHover(x, y) { $("coords").textContent = S.board ? `X ${x.toFixed(3)}  Y ${y.toFixed(3)} mm` : "–"; },
  async onMove(ref, x, y) {
    if (S.preview) { toastStatus("This is a routing preview: adopt it or go back to the working copy before moving parts."); viewer.setBoard(S.preview.board, true); return; }
    try {
      await api(`/api/projects/${S.pid}/move`, { method: "POST", body: { ref, x_mm: x, y_mm: y } });
      await reloadBoard();
    } catch (e) { toastStatus(`Move failed: ${e.message}`); }
  },
});

function netIndex(name) { return name && S.board ? Math.max(S.board.nets.indexOf(name), 0) : 0; }
function netName(i) { return i && S.board ? S.board.nets[i] : ""; }

// ---- projects ------------------------------------------------------------------------------
async function loadProjects(select) {
  const list = await api("/api/projects");
  const sel = $("projectSel");
  sel.innerHTML = `<option value="">${list.length ? "Choose a project…" : "No project"}</option>` +
    list.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
  const q = new URLSearchParams(location.search);
  // Start on the home screen unless the URL names a project; `select` comes from creating or importing one.
  const want = select ?? q.get("project");
  if (want && list.some((p) => p.id === want)) { sel.value = want; await openProject(want); }
  else if (select == null) showHome();
  const tab = q.get("tab");
  if (select == null && tab && document.querySelector(`.tab[data-tab="${CSS.escape(tab)}"]`)) showTab(tab);
}

// ---- home: project tiles ---------------------------------------------------------------------
const PHASE_WORD = { active: "in progress", approved: "approved", stale: "needs re-run", locked: "locked" };
function ago(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!(s >= 0)) return iso || "–";
  if (s < 60) return "just now";
  const [n, u] = s < 3600 ? [s / 60, "min"] : s < 86400 ? [s / 3600, "h"] : [s / 86400, "day"];
  return `${Math.floor(n)} ${u}${u === "day" && Math.floor(n) > 1 ? "s" : ""} ago`;
}
function costHtml(s) {
  const c = s.cost;
  if (!s.roles.chosen) return `<span class="muted">No parts chosen yet</span>`;
  // Say what the figure covers on the same line: unchosen roles and unpriced parts are missing from it.
  const r = s.roles, open = r.total - r.chosen;
  const cover = [`covers ${c.priced} of ${r.total} roles`, open && `${open} not chosen`, c.unpriced && `${c.unpriced} not priced yet`].filter(Boolean).join(", ");
  return (c.parts_usd == null ? `<span class="muted">Not priced yet</span>` : `$${c.parts_usd.toFixed(2)} for ${c.boards} boards`) +
    ` <span class="${c.priced < r.total ? "st-unknown" : "muted"}">· ${cover}</span>` +
    (s.new_design ? "" : `<br><span class="st-unknown">Imported board: roles are not its full BOM. Use “Check board BOM” in the BOM tab for that.</span>`) +
    `<br><span class="muted">${esc(c.note)}${c.as_of ? ` Prices as of ${esc(c.as_of)}.` : ""}</span>`;
}
function tileHtml(s) {
  if (s.error) return `<div class="tile-slot"><article class="tile" tabindex="0" data-pid="${esc(s.id)}"><h3>${esc(s.name)}</h3><p class="muted" style="padding:0 12px">${esc(s.error)}</p></article></div>`;
  const p = s.phase;
  const bars = Object.entries(p.statuses).map(([k, st]) => `<i class="${st}" title="${esc(k)}: ${PHASE_WORD[st]}"></i>`).join("");
  const label = p.all_approved ? `<span class="done">✓ All phases approved</span>` : `<b>${esc(p.title)}</b> · ${PHASE_WORD[p.status] || esc(p.status)}`;
  const thumb = s.has_board
    ? `<img src="/api/projects/${encodeURIComponent(s.id)}/thumb.svg?v=${encodeURIComponent(s.last_edited)}" alt="" loading="lazy" onerror="this.outerHTML='<span class=none>No preview</span>'">`
    : `<span class="none">No board yet.<br>Choose parts, then build it in Placement.</span>`;
  const r = s.routing;
  const rows = [
    ["Last edited", `<span title="${esc(s.last_edited)}">${ago(s.last_edited)}</span>`],
    ["Parts cost", `<span data-cost>${costHtml(s)}</span>`],
    ["Scope", s.scope ? `<span>${esc(s.scope)}</span>` : `<span class="muted">No requirements written yet</span>`, "scope"],
    ["Parts", `${s.roles.chosen} of ${s.roles.total} chosen` + (s.netlist ? ` · netlist ${s.netlist.parts} parts, ${s.netlist.nets} nets${s.netlist.ok ? "" : " (has errors)"}` : "")],
    s.outline && ["Board", `${esc(s.outline.shape)} ${s.outline.width_mm} × ${s.outline.height_mm} mm`],
    r && ["Routing", r.clean ? `clean · ${r.vias} vias · ${r.length_mm} mm` : `${r.errors} errors · ${r.unconnected} unconnected`],
    ["Chats", String(s.chats)],
    ["Created", esc((s.created || "").replace("T", " "))],
  ].filter(Boolean);
  return `<div class="tile-slot"><article class="tile" tabindex="0" data-pid="${esc(s.id)}" title="Open ${esc(s.name)}">
    <div class="thumb">${thumb}</div>
    <h3>${esc(s.name)}</h3>
    <div class="phase"><span class="bars">${bars}</span>${label}</div>
    <div class="more"><div><dl>${rows.map(([k, v, cls]) => `<dt>${k}</dt><dd${cls ? ` class="${cls}"` : ""}>${v}</dd>`).join("")}</dl></div></div>
  </article></div>`;
}
async function showHome() {
  document.body.classList.add("at-home");
  $("home").hidden = false;
  $("homeGrid").innerHTML = `<p class="muted">Loading projects…</p>`;
  let list;
  try { list = await api("/api/home"); } catch (e) { $("homeGrid").innerHTML = `<p class="st-short">${esc(e.message)}</p>`; return; }
  $("homeCount").textContent = `${list.length} project${list.length === 1 ? "" : "s"} · newest first`;
  $("homeGrid").innerHTML = `<div class="tile-slot"><button class="tile-new" id="homeNewTile"><b>+</b>New design</button></div>` + list.map(tileHtml).join("");
  $("homeNewTile").onclick = () => openNew();
  $("homeNote").textContent = S.pid ? "Esc goes back to the open project." : "";
  // Fill in prices the local JLC cache doesn't have yet, one project at a time (each part is fetched once, then cached for a day).
  for (const s of list.filter((x) => !x.error && x.cost.unpriced)) {
    if ($("home").hidden) break;
    try {
      const u = await api(`/api/projects/${encodeURIComponent(s.id)}/home/price`, { method: "POST" });
      const el = $("homeGrid").querySelector(`.tile[data-pid="${CSS.escape(s.id)}"] [data-cost]`);
      if (el) el.innerHTML = costHtml(u);
    } catch { /* offline: keep the cached figure */ }
  }
}
function leaveHome() {
  document.body.classList.remove("at-home");
  $("home").hidden = true;
}
$("homeBtn").onclick = showHome;
$("homeNew").onclick = () => openNew();
$("homeImport").onclick = () => openImport();
$("homeGrid").addEventListener("click", (e) => {
  const t = e.target.closest(".tile[data-pid]");
  if (t) { $("projectSel").value = t.dataset.pid; openProject(t.dataset.pid); }
});
$("homeGrid").addEventListener("keydown", (e) => {
  const t = e.target.closest(".tile[data-pid]");
  if (t && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); t.click(); }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("home").hidden && S.pid && !document.querySelector("dialog[open]")) leaveHome();
});

async function openProject(pid) {
  if (pid) leaveHome();
  S.pid = pid || null;
  store.set("pcbstudio.project", S.pid || "");
  $("downloadBtn").href = S.pid ? `/api/projects/${S.pid}/download` : "#";
  S.refs = []; S.net = 0; S.drc = null; S.preview = null; $("previewBar").hidden = true;
  viewer.select([], 0);
  viewer.setDrcMarkers([]);
  $("info").hidden = true;
  if (!S.pid) { S.board = null; showBoard(); loadPhases(); return; }
  $("boardStats").textContent = "loading board…";
  try {
    S.board = await api(`/api/projects/${S.pid}/board`);
  } catch (e) { S.board = null; showBoard(); $("boardStats").textContent = e.message; return; }
  showBoard();
  buildLayerPanel();
  loadBom();
  loadPhases();
  loadLayout();
  loadRouting();
  api(`/api/projects/${S.pid}/drc`).then((d) => { if (d && d.items) setDrc(d); else setDrc(null); });
  await loadChats();
}

function showBoard() {
  $("boardEmpty").hidden = !!S.board && (S.board.stats.footprints > 0 || hasOutline());
  $("boardEmptyMsg").innerHTML = S.board ? "This design has no board yet. Finish <b>1 Components</b> (parts and netlist), then set the outline and build it in <b>Placement</b>." : "No board open.";
  $("layersPanel").hidden = !S.board || !$("layersBtn").classList.contains("on");
  viewer.insetRight = $("layersPanel").hidden ? 0 : 230;
  if (S.board) {
    viewer.setBoard(S.board);
    const st = S.board.stats;
    $("boardStats").textContent = `${S.board.name} · rev ${S.board.rev} · ${st.footprints} parts · ${st.nets} nets · ${st.copper_layers} Cu`;
    $("undoBtn").disabled = !S.board.can_undo;
    $("netList").innerHTML = S.board.nets.filter(Boolean).map((n) => `<option value="${esc(n)}">`).join("");
  } else {
    $("boardStats").textContent = "";
    $("undoBtn").disabled = true;
    viewer.b = null; viewer.render();
  }
}

const hasOutline = () => !!S.board?.drawings?.["Edge.Cuts"]?.length || !!S.layout?.has_outline;

async function reloadBoard() {
  if (!S.pid) return;
  S.preview = null; $("previewBar").hidden = true;
  const b = await api(`/api/projects/${S.pid}/board`);
  S.board = b;
  viewer.setBoard(b, true);
  const st = b.stats;
  $("boardStats").textContent = `${b.name} · rev ${b.rev} · ${st.footprints} parts · ${st.nets} nets · ${st.copper_layers} Cu`;
  $("undoBtn").disabled = !b.can_undo;
  $("boardEmpty").hidden = b.stats.footprints > 0 || hasOutline();
  renderInfo();
  loadBom();
}

// ---- layer panel ---------------------------------------------------------------------------
const OBJECTS = [["fpF", "Front components"], ["fpB", "Back components"], ["tracks", "Tracks"], ["vias", "Vias"], ["pads", "Pads"], ["zones", "Zones / pours"],
  ["refs", "Reference text"], ["keepouts", "Keepouts"], ["drc", "DRC markers"]];
const DEFAULT_TECH = new Set(["Edge.Cuts", "F.Silkscreen", "B.Silkscreen", "F.Courtyard"]);

function visKey() { return `pcbstudio.vis.${S.pid}`; }

function buildLayerPanel() {
  const b = S.board;
  const saved = store.get(visKey(), null);
  const used = new Set([...Object.keys(b.tracks), ...Object.keys(b.drawings)]);
  for (const f of b.footprints) { for (const l of Object.keys(f.graphics)) used.add(l); for (const p of f.pads) p.layers.forEach((l) => used.add(l)); }
  const copper = b.layers.filter((l) => l.copper);
  const tech = b.layers.filter((l) => !l.copper && used.has(l.name));
  viewer.visible = new Set(saved?.layers ?? [...copper.map((l) => l.name), ...tech.filter((l) => DEFAULT_TECH.has(l.name)).map((l) => l.name)]);
  if (saved?.show) Object.assign(viewer.show, saved.show);
  viewer.active = saved?.active && copper.some((l) => l.name === saved.active) ? saved.active : "F.Cu";

  const row = (l, i, isCu) => `<label class="lrow${isCu && l.name === viewer.active ? " active" : ""}" data-layer="${esc(l.name)}">
      <input type="checkbox" ${viewer.visible.has(l.name) ? "checked" : ""}>
      <span class="sw" style="background:${layerColor(l.name, isCu ? (l.name === "B.Cu" ? copper.length : i) : undefined)}"></span>
      <span class="nm" title="${isCu ? "Click the name to make this the active layer" : ""}">${esc(l.name)}</span></label>`;
  $("cuList").innerHTML = copper.map((l, i) => row(l, i, true)).join("");
  $("techList").innerHTML = tech.map((l) => row(l, 0, false)).join("") || `<span class="muted">none</span>`;
  $("objList").innerHTML = OBJECTS.map(([k, n]) =>
    `<label class="lrow" data-obj="${k}"><input type="checkbox" ${viewer.show[k] ? "checked" : ""}><span class="nm">${n}</span></label>`).join("");
  viewer.netCache?.clear();
  viewer.render();
}

function saveVis() {
  store.set(visKey(), { layers: [...viewer.visible], show: viewer.show, active: viewer.active });
}

$("layersPanel").addEventListener("change", (e) => {
  const lr = e.target.closest(".lrow");
  if (!lr) return;
  if (lr.dataset.layer) viewer.setVisible(lr.dataset.layer, e.target.checked);
  if (lr.dataset.obj) { viewer.show[lr.dataset.obj] = e.target.checked; viewer.render(); }
  saveVis();
});
$("cuList").addEventListener("click", (e) => {
  const nm = e.target.closest(".nm");
  if (!nm) return;
  e.preventDefault();
  const lr = nm.closest(".lrow");
  viewer.setActive(lr.dataset.layer);
  if (!viewer.visible.has(lr.dataset.layer)) { viewer.setVisible(lr.dataset.layer, true); lr.querySelector("input").checked = true; }
  document.querySelectorAll("#cuList .lrow").forEach((r) => r.classList.toggle("active", r === lr));
  saveVis();
});
document.querySelectorAll("[data-cu]").forEach((a) => a.addEventListener("click", (e) => {
  e.preventDefault();
  const on = a.dataset.cu === "all";
  document.querySelectorAll("#cuList .lrow").forEach((r) => { r.querySelector("input").checked = on; viewer.setVisible(r.dataset.layer, on); });
  saveVis();
}));
$("netFind").addEventListener("change", () => {
  const i = netIndex($("netFind").value.trim());
  if (!i) return;
  S.net = i; viewer.highlightNet(i); viewer.zoomToNet(i); renderCtx(); renderInfo();
});

// ---- toolbar -------------------------------------------------------------------------------
$("topBtn").onclick = () => { viewer.setFlip(false); $("topBtn").classList.add("on"); $("botBtn").classList.remove("on"); };
$("botBtn").onclick = () => { viewer.setFlip(true); $("botBtn").classList.add("on"); $("topBtn").classList.remove("on"); };
$("fitBtn").onclick = () => viewer.fit();
$("layersBtn").onclick = () => {
  const on = $("layersBtn").classList.toggle("on");
  $("layersPanel").hidden = !on || !S.board;
  viewer.insetRight = $("layersPanel").hidden ? 0 : 230;
};
$("kicadBtn").onclick = async () => {
  if (!S.pid) return;
  try { await api(`/api/projects/${S.pid}/open-kicad`, { method: "POST" }); toastStatus("Opened in KiCad. Saves there reload here."); }
  catch (e) { toastStatus(e.message); }
};
$("undoBtn").onclick = async () => {
  try { await api(`/api/projects/${S.pid}/undo`, { method: "POST" }); await reloadBoard(); }
  catch (e) { toastStatus(`Undo failed: ${e.message}`); }
};

document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));
function showTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".pane").forEach((p) => p.classList.toggle("active", p.id === `pane-${name}`));
  $("pcbTools").style.visibility = name === "pcb" ? "visible" : "hidden";
  if (name === "pcb") viewer.render();
  if (name === "place") loadLayout();
  if (name === "route") loadRouting();
}
api("/api/settings").then((s) => { $("rtRounds").value = s.route_rounds || 3; $("rtTime").value = s.route_max_time || 60; }).catch(() => {});

// ---- selection info ------------------------------------------------------------------------
function renderInfo() {
  const box = $("info");
  if (!S.board || (!S.refs.length && !S.net)) { box.hidden = true; return; }
  let html = "";
  if (S.refs.length === 1) {
    const f = viewer.fpByRef.get(S.refs[0]);
    if (f) {
      const nets = [...new Set(f.pads.map((p) => p.net).filter(Boolean))].map((i) => S.board.nets[i]).sort();
      const fields = Object.entries(f.fields).filter(([k, v]) => v && !["Reference", "Value", "Footprint", "Datasheet", "Description"].includes(k));
      html += `<h4>${esc(f.ref)} <small>${esc(f.value)}</small></h4>
        <dl><dt>Footprint</dt><dd>${esc(f.fpid)}</dd>
        <dt>Position</dt><dd>${f.c[0].toFixed(3)}, ${f.c[1].toFixed(3)} mm · ${f.rot}° · ${f.side === "F" ? "top" : "bottom"}</dd>
        <dt>Pads</dt><dd>${f.pads.length}${f.dnp ? ' · <span class="tag">DNP</span>' : ""}</dd>
        ${fields.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
        <div class="nets">${nets.map((n) => `<a href="#" data-net="${esc(n)}">${esc(n)}</a>`).join("")}</div>`;
    }
  } else if (S.refs.length > 1) {
    html += `<h4>${S.refs.length} parts</h4><div class="nets">${S.refs.map((r) => `<a href="#" data-ref="${esc(r)}">${esc(r)}</a>`).join("")}</div>`;
  }
  if (S.net) {
    const n = netName(S.net);
    const pads = [];
    for (const f of S.board.footprints) for (const p of f.pads) if (p.net === S.net) pads.push(`${f.ref}.${p.n}`);
    html += `<h4 style="margin-top:${html ? 10 : 0}px">Net <small>${esc(n)}</small></h4><dl><dt>Pads</dt><dd>${pads.length}: ${esc(pads.slice(0, 24).join(", "))}${pads.length > 24 ? " …" : ""}</dd></dl>`;
  }
  html += `<div class="actions"><button class="btn ghost" data-act="zoom">Zoom to</button><button class="btn ghost" data-act="ask">Ask about this</button><button class="btn ghost" data-act="clear">Clear</button></div>`;
  box.innerHTML = html;
  box.hidden = false;
}
$("info").addEventListener("click", (e) => {
  const a = e.target.closest("a, button");
  if (!a) return;
  e.preventDefault();
  if (a.dataset.net) { S.net = netIndex(a.dataset.net); viewer.highlightNet(S.net); }
  else if (a.dataset.ref) { S.refs = [a.dataset.ref]; viewer.select(S.refs); viewer.zoomTo(S.refs); }
  else if (a.dataset.act === "zoom") { S.refs.length ? viewer.zoomTo(S.refs) : viewer.zoomToNet(S.net); }
  else if (a.dataset.act === "ask") { $("input").focus(); }
  else if (a.dataset.act === "clear") { S.refs = []; S.net = 0; viewer.select([], 0); }
  renderCtx(); renderInfo(); markBomRows();
});

// ---- BOM -----------------------------------------------------------------------------------
async function loadBom() {
  if (!S.pid) return;
  try { S.bom = await api(`/api/projects/${S.pid}/bom`); } catch { S.bom = []; }
  renderBom();
}
function renderBom() {
  const q = $("bomFilter").value.trim().toLowerCase();
  const rows = S.bom.filter((g) => !q || [g.refs.join(" "), g.value, g.footprint, g.mpn, g.lcsc, g.manufacturer].join(" ").toLowerCase().includes(q));
  const parts = S.bom.reduce((n, g) => n + g.qty, 0);
  $("bomCount").textContent = S.bom.length || "";
  $("bomSummary").textContent = S.bom.length ? `${S.bom.length} lines · ${parts} parts${q ? ` · ${rows.length} shown` : ""}` : "";
  $("bomTable").tBodies[0].innerHTML = rows.map((g) => `<tr data-refs="${esc(g.refs.join(","))}">
    <td>${g.qty}</td><td class="refs">${esc(g.refs.join(", "))}</td><td>${esc(g.value)}</td>
    <td class="mono">${esc(g.footprint)}</td><td class="mono">${esc(g.mpn)}</td><td class="mono">${esc(g.lcsc)}</td>
    <td>${g.dnp ? '<span class="tag">DNP</span>' : ""}</td></tr>`).join("");
  markBomRows();
}
function markBomRows() {
  const sel = new Set(S.refs);
  for (const tr of $("bomTable").tBodies[0].rows) tr.classList.toggle("sel", tr.dataset.refs.split(",").some((r) => sel.has(r)));
}
$("bomFilter").addEventListener("input", renderBom);
$("bomTable").tBodies[0].addEventListener("click", (e) => {
  const tr = e.target.closest("tr");
  if (!tr) return;
  S.refs = tr.dataset.refs.split(","); S.net = 0;
  viewer.select(S.refs, 0);
  showTab("pcb");
  requestAnimationFrame(() => viewer.zoomTo(S.refs));
  renderCtx(); renderInfo(); markBomRows();
});
$("bomCsv").onclick = () => {
  const q = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const lines = [["Qty", "References", "Value", "Footprint", "Manufacturer", "MPN", "LCSC", "DNP"].map(q).join(",")]
    .concat(S.bom.map((g) => [g.qty, g.refs.join(" "), g.value, g.footprint, g.manufacturer, g.mpn, g.lcsc, g.dnp ? "DNP" : ""].map(q).join(",")));
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([lines.join("\r\n")], { type: "text/csv" }));
  a.download = `${S.board?.name || "board"}_bom.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
};

// ---- phases: 1 components -> 2 placement -> 3 routing -------------------------------------------
const STEP_TAB = { components: "parts", placement: "place", routing: "route" };
const money = (v) => (v == null ? "–" : `$${Number(v).toFixed(v < 1 ? 4 : 2)}`);
const lcscLink = (c) => (c ? `<a href="https://jlcpcb.com/partdetail/${esc(c)}" target="_blank" rel="noopener" class="mono">${esc(c)}</a>` : "");

async function loadPhases() {
  if (!S.pid) { $("flow").hidden = true; S.phases = null; return; }
  try { S.phases = await api(`/api/projects/${S.pid}/phases`); } catch (e) { toastStatus(e.message); return; }
  renderFlow();
  renderParts();
  loadNetlist();
  renderApprove("placement", $("placeApprove"));
  renderApprove("routing", $("routeApprove"));
  if (S.routing) renderRouting();  // Start depends on placement being approved
}

function renderFlow() {
  const f = S.phases.flow;
  $("flow").hidden = false;
  $("flow").innerHTML = f.order.map((k, i) => {
    const st = f.phases[k].status;
    const label = { active: k === f.current ? "in progress" : "open", approved: "approved", stale: "needs re-run", locked: "locked" }[st];
    return `${i ? '<span class="arrow">›</span>' : ""}<button class="step ${st}" data-step="${k}" ${st === "locked" ? "disabled" : ""}>
      <b>${st === "approved" ? "✓" : i + 1}</b>${esc(f.titles[k].replace(/^\d+ /, ""))} <small>${label}</small></button>`;
  }).join("");
}
$("flow").addEventListener("click", (e) => {
  const b = e.target.closest(".step");
  if (b && !b.disabled) showTab(STEP_TAB[b.dataset.step]);
});

function renderParts() {
  const P = S.phases;
  if (!P) return;
  const c = P.components, st = P.flow.phases.components.status;
  if (document.activeElement !== $("reqInput")) $("reqInput").value = c.requirements || "";
  if (document.activeElement !== $("boardsInput")) $("boardsInput").value = c.boards || 5;
  $("partsCount").textContent = c.roles.length || "";
  $("jlcSource").textContent = `Parts data: ${P.jlc_source}. Stock and prices are a snapshot: confirm on jlcpcb.com when ordering. ` +
    `Ranking: enough stock for ${c.boards} boards, then package, then order cost including a $${P.extended_fee} loading fee per Extended part.`;
  const ab = $("approveBtn");
  ab.textContent = st === "approved" ? "Reopen components" : "Approve components";
  ab.classList.toggle("primary", st !== "approved");
  ab.classList.toggle("ghost", st === "approved");
  $("roles").innerHTML = c.roles.length ? c.roles.map(roleCard).join("") :
    `<div class="role-empty">No part roles yet. Describe the board above and ask the assistant to “propose the major parts”, or use <b>Use BOM as roles</b> for an existing board.</div>`;
}

function roleCard(r) {
  const how = [r.subcategory, r.category && `table ${r.category}`, r.package, r.query && `“${r.query}”`,
    r.filters && Object.keys(r.filters).length && JSON.stringify(r.filters)].filter(Boolean).join(" · ");
  const chosen = (r.candidates || []).find((x) => x.lcsc === r.chosen);
  const rows = (r.candidates || []).map((x) => `<tr class="${x.lcsc === r.chosen ? "chosen" : ""}">
      <td>${x.rank}</td><td>${lcscLink(x.lcsc)}</td><td class="mono">${esc(x.mpn)}</td><td class="mono">${esc(x.package)}</td>
      <td class="kind-${esc(x.kind)}">${esc(x.kind)}</td><td class="st-${esc(x.stock_state)}">${x.stock == null ? "?" : x.stock.toLocaleString()}</td>
      <td>${money(x.unit_price)}</td><td>${money(x.order_cost)}</td><td class="why">${esc(x.why)}</td>
      <td>${x.lcsc === r.chosen ? "<b>chosen</b>" : `<button class="btn ghost" data-choose="${esc(r.id)}" data-lcsc="${esc(x.lcsc)}">Choose</button>`}</td></tr>`).join("");
  return `<div class="role" data-role="${esc(r.id)}">
    <div class="role-h"><h4>${esc(r.name)}</h4><span class="muted">× ${r.qty || 1}/board${r.refs ? " · " + esc(r.refs.join(", ")) : ""}</span>
      <span class="q">${esc(how)}</span><div class="spacer"></div>
      <span>${chosen ? `Chosen: ${lcscLink(chosen.lcsc)} ${esc(chosen.mpn)}` : r.chosen ? `Chosen: ${lcscLink(r.chosen)} <span class="muted">(from the netlist, not looked up yet)</span>` : '<span class="st-unknown">no part chosen</span>'}</span>
      ${r.subcategory || r.category || /^C\d+$/.test(r.query || "") ? `<button class="btn ghost" data-research="${esc(r.id)}" title="Search again (fresh stock once the 24 h cache expires)">Re-search</button>` : ""}
      <button class="btn ghost" data-delete="${esc(r.id)}">Remove</button></div>
    ${rows ? `<div class="table-wrap"><table class="grid"><thead><tr><th>#</th><th>LCSC</th><th>MPN</th><th>Package</th><th>Type</th><th>Stock</th><th>Unit</th><th>Order cost</th><th>Why this rank</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<div class="role-empty">${esc(r.search_notes?.join(" ") || (r.searched_at ? "No candidates found." : r.chosen ? "Click Re-search to look this part up at JLC (stock, Basic/Extended, price)." : "Not searched yet: ask the assistant to find candidates for this role."))}</div>`}
    ${r.notes ? `<div class="role-empty">${esc(r.notes)}</div>` : ""}</div>`;
}

$("roles").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b || !S.pid) return;
  try {
    if (b.dataset.choose) await api(`/api/projects/${S.pid}/components/${b.dataset.choose}/choose`, { method: "POST", body: { lcsc: b.dataset.lcsc } });
    else if (b.dataset.research) { b.disabled = true; b.textContent = "Searching…"; await api(`/api/projects/${S.pid}/components/${b.dataset.research}/research`, { method: "POST" }); }
    else if (b.dataset.delete) { if (!confirm("Remove this part role?")) return; await api(`/api/projects/${S.pid}/components/${b.dataset.delete}`, { method: "DELETE" }); }
    else return;
  } catch (err) { toastStatus(err.message); }
  loadPhases();
});

async function saveComponents(patch) {
  if (!S.pid) return;
  try { await api(`/api/projects/${S.pid}/components`, { method: "POST", body: patch }); loadPhases(); } catch (e) { toastStatus(e.message); }
}
$("reqInput").addEventListener("change", () => saveComponents({ requirements: $("reqInput").value }));
$("boardsInput").addEventListener("change", () => saveComponents({ boards: Math.max(1, parseInt($("boardsInput").value, 10) || 1) }));

async function bomCheck(createRoles) {
  if (!S.pid) return;
  const btn = createRoles ? $("bomRolesBtn") : $("bomCheckBtn");
  btn.disabled = true;
  $("bomCheck").innerHTML = `<div class="bomcheck muted">Looking up ${S.bom.length} BOM lines on JLC…</div>`;
  try {
    const r = await api(`/api/projects/${S.pid}/bom-check?create_roles=${createRoles}`, { method: "POST" });
    const counts = Object.entries(r.counts).map(([k, v]) => `${v} ${k}`).join(" · ");
    $("bomCheck").innerHTML = `<details class="bomcheck" ${createRoles ? "" : "open"}><summary><b>BOM check</b> for ${r.boards} boards: ${esc(counts)} ·
      ${r.extended_parts} Extended parts ≈ $${r.extended_fees_usd} loading fees</summary>
      <div class="table-wrap"><table class="grid"><thead><tr><th>Refs</th><th>Qty</th><th>Value</th><th>LCSC</th><th>Status</th><th>Type</th><th>Stock</th><th>Order cost</th></tr></thead><tbody>
      ${r.rows.map((x) => `<tr><td class="refs">${esc(x.refs.join(", "))}</td><td>${x.qty}</td><td>${esc(x.value)}</td><td>${lcscLink(x.lcsc)}</td>
        <td class="${x.status === "SHORT" ? "st-short" : x.status === "ok" ? "" : "st-unknown"}">${esc(x.status)}</td>
        <td class="kind-${esc(x.part?.kind)}">${esc(x.part?.kind)}</td><td>${x.part?.stock == null ? "" : x.part.stock.toLocaleString()}</td><td>${money(x.part?.order_cost)}</td></tr>`).join("")}
      </tbody></table></div><p class="muted src">${esc(r.note)}</p></details>`;
  } catch (e) { $("bomCheck").innerHTML = `<div class="bomcheck st-short">${esc(e.message)}</div>`; }
  btn.disabled = false;
  if (createRoles) loadPhases();
}
$("bomCheckBtn").onclick = () => bomCheck(false);
$("bomRolesBtn").onclick = () => bomCheck(true);

$("approveBtn").onclick = async () => {
  if (!S.pid || !S.phases) return;
  const approved = S.phases.flow.phases.components.status === "approved";
  try {
    if (approved) {
      if (!confirm("Reopen components? Placement and routing will be marked as needing a re-run.")) return;
      await api(`/api/projects/${S.pid}/phases/components/reopen`, { method: "POST" });
    } else {
      const roles = S.phases.components.roles;
      const n = roles.filter((r) => r.chosen).length;
      let force = false;
      if (n < roles.length) {
        if (!confirm(`${roles.length - n} of ${roles.length} roles have no chosen part. Approve anyway?`)) return;
        force = true;
      }
      await api(`/api/projects/${S.pid}/phases/components/approve`, { method: "POST", body: { force, summary: `${n} parts chosen` } });
    }
  } catch (e) { toastStatus(e.message); }
  loadPhases();
};


// ---- netlist (Phase 1 of a new design) -------------------------------------------------------
async function loadNetlist() {
  if (!S.pid) return;
  try { S.netlist = await api(`/api/projects/${S.pid}/netlist`); } catch (e) { $("netlistBox").innerHTML = ""; return; }
  renderNetlist();
}

function renderNetlist() {
  const st = S.netlist, nl = st?.netlist;
  if (!st || (!nl && !st.required)) { $("netlistBox").innerHTML = ""; return; }
  const head = `<div class="role-h"><h4>Netlist</h4>`;
  if (!nl) {
    $("netlistBox").innerHTML = `<div class="role">${head}<span class="st-unknown">needed for a new design</span><div class="spacer"></div>
      <button class="btn ghost" data-nl="import">Import KiCad schematic / netlist…</button></div>
      <div class="role-empty">Import a <code>.kicad_sch</code> (exported with kicad-cli) or a <code>.net</code> file. Or ask the assistant to
      “draft the netlist”: it looks up footprints and pads, and takes pinouts from the datasheets. Either way, check pin → net for every part before you approve.</div></div>`;
    return;
  }
  const errs = nl.issues.filter((i) => i.level === "error"), warns = nl.issues.filter((i) => i.level !== "error");
  const issues = nl.issues.length ? `<ul class="issues">${nl.issues.map((i) => `<li class="${i.level === "error" ? "st-short" : "st-unknown"}">${esc(i.msg)}</li>`).join("")}</ul>` : "";
  const rows = nl.by_part.map((p) => `<tr><td><a href="#" data-ref="${esc(p.ref)}">${esc(p.ref)}</a></td><td>${esc(p.value)}</td>
      <td class="mono fp">${esc(p.footprint)}</td><td>${lcscLink(p.lcsc)}</td>
      <td class="pins">${Object.entries(p.pins).map(([pad, net]) => `<span class="pin"><b>${esc(pad)}</b> ${esc(net)}</span>`).join("")}
      ${p.unconnected.length ? `<span class="pin nc" title="pads with no net">nc: ${esc(p.unconnected.join(" "))}</span>` : ""}</td></tr>`).join("");
  $("netlistBox").innerHTML = `<details class="role" ${errs.length || !nl.approved ? "open" : ""}><summary class="role-h"><h4>Netlist</h4>
      <span class="muted">${nl.parts} parts · ${nl.nets} nets · from ${esc(nl.source)} · ${esc(nl.updated_at || "")}</span>
      <span class="${errs.length ? "st-short" : warns.length ? "st-unknown" : "kind-Basic"}">${errs.length ? `${errs.length} error(s)` : nl.ok ? "valid" : ""}${warns.length ? ` · ${warns.length} warning(s)` : ""}</span>
      <div class="spacer"></div>
      <button class="btn ghost" data-nl="import">Re-import…</button><button class="btn ghost" data-nl="delete">Delete</button></summary>
    ${issues}
    <p class="muted src">Check every pin → net against the part's datasheet before approving. The app checks pad names against the footprint, but it can't tell whether a pinout is right.</p>
    <div class="table-wrap"><table class="grid"><thead><tr><th>Ref</th><th>Value</th><th>Footprint</th><th>LCSC</th><th>Pad → net</th></tr></thead><tbody>${rows}</tbody></table></div></details>`;
}

$("netlistBox").addEventListener("click", async (e) => {
  const a = e.target.closest("[data-ref]");
  if (a) { e.preventDefault(); S.refs = [a.dataset.ref]; viewer.select(S.refs, 0); showTab("pcb"); requestAnimationFrame(() => viewer.zoomTo(S.refs)); renderCtx(); renderInfo(); return; }
  const b = e.target.closest("button[data-nl]");
  if (!b || !S.pid) return;
  e.preventDefault();
  if (b.dataset.nl === "import") return openBrowse("netlist");
  if (!confirm("Delete the netlist? Phase 1 can't be approved without one.")) return;
  try { await api(`/api/projects/${S.pid}/netlist`, { method: "DELETE" }); } catch (err) { toastStatus(err.message); }
  loadPhases();
});

// ---- shared approve/reopen buttons for phases 2 and 3 ---------------------------------------------
function renderApprove(phase, btn) {
  const st = S.phases?.flow.phases[phase]?.status;
  btn.disabled = !st || st === "locked";
  btn.textContent = st === "approved" ? `Reopen ${phase}` : `Approve ${phase}`;
  btn.classList.toggle("primary", st !== "approved");
  btn.classList.toggle("ghost", st === "approved");
  btn.title = st === "locked" ? "Approve the previous phase first" : "";
}
async function approvePhase(phase, summary) {
  if (!S.pid || !S.phases) return;
  const st = S.phases.flow.phases[phase].status;
  try {
    if (st === "approved") {
      if (!confirm(`Reopen ${phase}? Later phases will be marked as needing a re-run.`)) return;
      await api(`/api/projects/${S.pid}/phases/${phase}/reopen`, { method: "POST" });
    } else await api(`/api/projects/${S.pid}/phases/${phase}/approve`, { method: "POST", body: { summary: summary() } });
  } catch (e) { toastStatus(e.message); alert(e.message); }
  loadPhases();
}
$("placeApprove").onclick = () => {
  const c = S.layout?.place_check;
  if (c && (c.courtyard_overlaps || c.edge_clearance || c.other_errors) &&
      !confirm(`The last DRC check found ${c.courtyard_overlaps} courtyard overlaps, ${c.edge_clearance} edge-clearance and ${c.other_errors} other errors. Approve anyway?`)) return;
  approvePhase("placement", () => (c ? `${c.courtyard_overlaps} overlaps, ${c.edge_clearance} edge, ${c.other_errors} other` : "not DRC-checked"));
};
$("routeApprove").onclick = () => {
  const j = S.routing?.jobs?.find((x) => x.adopted);
  if (!j && !confirm("No routed candidate has been adopted onto the board yet. Approve anyway?")) return;
  const c = j?.candidates.find((x) => x.id === j.adopted);
  approvePhase("routing", () => (c ? `adopted ${j.id}/${c.id}: ${c.score.errors} errors, ${c.score.unconnected} unconnected, ${c.score.vias} vias` : "nothing adopted"));
};

// ---- Phase 2: placement ----------------------------------------------------------------------
async function loadLayout() {
  if (!S.pid) return;
  try { S.layout = await api(`/api/projects/${S.pid}/layout`); } catch (e) { $("placeState").textContent = e.message; return; }
  renderLayout();
}

function renderLayout() {
  const L = S.layout;
  if (document.activeElement?.closest?.("#pane-place .form-row") == null) {
    $("olShape").value = L.shape; $("olW").value = L.width_mm; $("olH").value = L.height_mm; $("olR").value = L.corner_mm;
    $("fabSel").innerHTML = L.profiles.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
    $("fabSel").value = L.fab_profile;
  }
  shapeFields();
  const prof = L.profiles.find((p) => p.id === L.fab_profile);
  $("fabSrc").innerHTML = prof ? `Rules from <a href="${esc(prof.source.split(" ")[0])}" target="_blank" rel="noopener">${esc(prof.source)}</a>, written to the project's <code>.kicad_pro</code>. Power nets (GND, VBUS, +3V3…) get the wider Power class.` : "";
  const ol = L.has_outline ? "outline set" : L.new_design && !L.parts && L.outline_saved ? `outline saved (${L.shape} ${L.width_mm} × ${L.height_mm} mm, drawn when you build)` : "outline not set";
  $("placeState").textContent = `${L.parts} parts on the board · ${ol}` +
    (L.terms && !L.terms.error ? ` · wirelength ${L.terms.wirelength_mm} mm` : "");
  $("buildBtn").disabled = !L.has_outline && !L.new_design;
  $("placeBtn").disabled = $("placeCheckBtn").disabled = !L.parts;
  const lp = L.last_place, c = L.place_check;
  const term = (k, u) => lp ? `<tr><td>${k.replace(/_mm2?$/, "").replace("_", " ")}</td><td>${lp.before[k]}</td><td>${lp.after[k]}</td><td>${u}</td></tr>` : "";
  $("placeResult").innerHTML = (lp ? `<div class="kv"><table class="grid compact"><thead><tr><th>Placer term</th><th>Before</th><th>After</th><th></th></tr></thead><tbody>
      ${term("wirelength_mm", "mm (half-perimeter)")}${term("overlap_mm2", "mm² courtyard overlap incl. spacing")}${term("outside_mm", "mm outside the outline")}${term("edge_mm", "mm connectors from the edge")}</tbody></table>
      <p class="muted src">rev ${lp.rev} · ${lp.moved ?? lp.movable} parts moved, ${lp.fixed} fixed · ${lp.accepted}/${lp.moves} moves accepted in ${lp.seconds} s · seed ${lp.seed} ·
      edge parts: ${esc((lp.edge_parts || []).join(", ") || "none")} · board ${Math.round((lp.utilization || 0) * 100)} % full</p>
      ${lp.warning ? `<p class="st-unknown">${esc(lp.warning)}</p>` : ""}</div>` : "") +
    (c ? `<div class="checkline ${c.courtyard_overlaps || c.edge_clearance || c.other_errors ? "bad" : "good"}"><b>KiCad DRC</b> (${esc(c.drc_ran_at)}):
      ${c.courtyard_overlaps} courtyard overlaps · ${c.edge_clearance} edge clearance · ${c.other_errors} other errors
      <span class="muted">· ${c.footprint_internal} inside footprints (not placement problems) · ${c.unconnected} unrouted connections (expected before routing) · ${c.warnings} warnings</span></div>` : "");
}
function shapeFields() {
  const sh = $("olShape").value;
  $("olHWrap").hidden = sh === "circle";
  $("olRWrap").hidden = sh !== "rect";
}
$("olShape").addEventListener("change", shapeFields);

async function layoutCall(btn, busyText, fn) {
  if (!S.pid) return;
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = busyText;
  try { await fn(); } catch (e) { toastStatus(e.message); alert(e.message); }
  btn.textContent = old; btn.disabled = false;
  await loadLayout();
  await reloadBoard();
  loadPhases();
}
$("olSet").onclick = () => layoutCall($("olSet"), "Setting…", () => api(`/api/projects/${S.pid}/layout/outline`, { method: "POST", body: {
  shape: $("olShape").value, width_mm: +$("olW").value, height_mm: +$("olH").value, corner_mm: +$("olR").value } }));
$("buildBtn").onclick = () => {
  if (S.layout?.parts && !confirm("Rebuild the board from the netlist? Parts on the board are replaced (undo restores them).")) return;
  layoutCall($("buildBtn"), "Building…", () => api(`/api/projects/${S.pid}/layout/build`, { method: "POST", body: { fab_profile: $("fabSel").value } }));
};
$("placeBtn").onclick = () => layoutCall($("placeBtn"), "Placing…", () => api(`/api/projects/${S.pid}/layout/place`, { method: "POST", body: {
  moves: +$("plMoves").value || 20000, seed: +$("plSeed").value || 1, refine: $("plRefine").checked } }));
$("placeCheckBtn").onclick = () => layoutCall($("placeCheckBtn"), "Checking…", () => api(`/api/projects/${S.pid}/layout/check`, { method: "POST" }));

// ---- Phase 3: routing ------------------------------------------------------------------------
async function loadRouting() {
  if (!S.pid) return;
  try { S.routing = await api(`/api/projects/${S.pid}/routing`); } catch (e) { $("rtRouter").textContent = e.message; return; }
  renderRouting();
  clearTimeout(S.rtPoll);
  if (S.routing.running) S.rtPoll = setTimeout(loadRouting, 2000);
}

function renderRouting() {
  const R = S.routing, run = R.running;
  $("rtRouter").innerHTML = R.router ? `Router: <code>${esc(R.router)}</code> (fastroute, GPL-3.0, run as a separate program). Each round: route → KiCad DRC → re-place the parts DRC flagged → next round.
      ${R.busy_elsewhere ? '<b class="st-unknown">The router is busy with another project.</b>' : ""}`
    : `<span class="st-short">fastroute isn't configured.</span> Set its path in Settings → Router.`;
  $("rtStart").disabled = !R.router || !!run || R.busy_elsewhere;
  $("rtCancel").hidden = !run;
  $("routeCount").textContent = run ? "…" : "";
  const best = (j) => j.candidates.find((c) => c.id === j.best);
  $("rtJobs").innerHTML = R.jobs.length ? R.jobs.map((j, i) => {
    const rows = j.candidates.map((c) => {
      const sc = c.score, types = Object.entries(c.drc_by_type || {}).map(([k, v]) => `${v} ${k}`).join(", ");
      return `<tr class="${c.id === j.adopted ? "chosen" : ""}"><td>${esc(c.id)}${c.id === j.best ? " <b>best</b>" : ""}${c.partial ? ' <span class="st-unknown">partial</span>' : ""}</td>
        <td class="${sc.errors ? "st-short" : ""}">${sc.errors}</td><td class="${sc.unconnected ? "st-short" : ""}">${sc.unconnected}</td><td>${sc.vias}</td><td>${sc.length_mm}</td>
        <td class="muted">${sc.footprint_internal}</td><td class="muted">${sc.warnings}</td><td class="muted">${sc.router_unrouted} / ${sc.router_violations}</td>
        <td class="why">${esc(types)}</td>
        <td><button class="btn ghost" data-prev="${esc(j.id)}/${esc(c.id)}">Preview</button>
          ${c.id === j.adopted ? "<b>adopted</b>" : `<button class="btn ghost" data-adopt="${esc(j.id)}/${esc(c.id)}">Adopt</button>`}</td></tr>`;
    }).join("");
    const b = best(j);
    return `<details class="role" ${i === 0 ? "open" : ""}><summary class="role-h"><h4>Run ${esc(j.id)}</h4>
        <span class="muted">${esc(j.status)} · round ${j.round}/${j.rounds} · ${j.max_time} s per round · ${esc(j.fab_profile || "")}</span>
        <span class="${b?.score.clean ? "kind-Basic" : "st-unknown"}">${b ? (b.score.clean ? `best ${b.id} is clean` : `best ${b.id}: ${b.score.errors} errors, ${b.score.unconnected} unconnected`) : j.error ? esc(j.error) : "no candidates yet"}</span>
        ${j.status === "running" ? `<div class="spacer"></div><button class="btn ghost" data-cancel="${esc(j.id)}">Stop</button>` : ""}</summary>
      ${rows ? `<div class="table-wrap"><table class="grid"><thead><tr><th>Candidate</th><th>DRC errors</th><th>Unconnected</th><th>Vias</th><th>Length mm</th>
        <th title="DRC errors between pads of one footprint: a library property, not a routing fault">In footprint</th><th>Warnings</th><th title="the router's own count (KiCad DRC is the judge)">Router unrouted / violations</th><th>DRC by type</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}
      <p class="muted src">Ranked by KiCad DRC errors, then unconnected, then vias, then length. Errors inside one footprint are listed but not scored.</p>
      <pre class="log">${esc(j.log.join("\n"))}</pre></details>`;
  }).join("") : `<div class="role-empty">No routing runs yet. Approve placement first, then start a run.</div>`;
  const P = S.phases?.flow.phases;
  if (!R.router) return;
  if (P && P.routing.status === "locked") { $("rtStart").disabled = true; $("rtStart").title = "Approve placement first"; } else $("rtStart").title = "";
}

$("rtStart").onclick = async () => {
  if (!S.pid) return;
  $("rtStart").disabled = true;
  try {
    await api(`/api/projects/${S.pid}/routing/start`, { method: "POST", body: { rounds: +$("rtRounds").value || 3, max_time: +$("rtTime").value || 60 } });
  } catch (e) { toastStatus(e.message); alert(e.message); }
  loadRouting();
};
async function cancelRun(jid) {
  try { await api(`/api/projects/${S.pid}/routing/${jid}/cancel`, { method: "POST" }); } catch (e) { toastStatus(e.message); }
  loadRouting();
}
$("rtCancel").onclick = () => S.routing?.running && cancelRun(S.routing.running.id);
$("rtJobs").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b || !S.pid) return;
  e.preventDefault();
  if (b.dataset.cancel) return cancelRun(b.dataset.cancel);
  if (b.dataset.prev) return previewCandidate(b.dataset.prev);
  if (b.dataset.adopt) return adoptCandidate(b.dataset.adopt);
});

async function previewCandidate(key) {
  const [jid, cid] = key.split("/");
  toastStatus(`Loading candidate ${key}…`);
  try {
    const b = await api(`/api/projects/${S.pid}/routing/${jid}/${cid}/board`);
    S.preview = { key, board: b };
    viewer.setBoard(b, true);
    $("previewText").textContent = `Previewing routed candidate ${key} (not on your board yet). ${b.stats.tracks ?? ""}`.trim();
    $("previewBar").hidden = false;
    $("boardEmpty").hidden = true;
    showTab("pcb");
    toastStatus("");
  } catch (e) { toastStatus(e.message); }
}
async function adoptCandidate(key) {
  const [jid, cid] = key.split("/");
  if (!confirm(`Put candidate ${key} on the working board? The current board is snapshotted first, so Undo brings it back.`)) return;
  try { await api(`/api/projects/${S.pid}/routing/${jid}/${cid}/adopt`, { method: "POST" }); } catch (e) { toastStatus(e.message); return; }
  await reloadBoard();
  loadRouting(); loadPhases(); loadLayout();
  api(`/api/projects/${S.pid}/drc`).then((d) => setDrc(d && d.items ? d : null)).catch(() => {});
}
$("previewAdopt").onclick = () => S.preview && adoptCandidate(S.preview.key);
$("previewBack").onclick = () => { S.preview = null; $("previewBar").hidden = true; if (S.board) viewer.setBoard(S.board, true); };

// ---- new design dialog -----------------------------------------------------------------------
const openNew = () => { $("newErr").textContent = ""; $("newDlg").showModal(); $("newName").focus(); };
$("newGo").onclick = async () => {
  const name = $("newName").value.trim();
  if (!name) { $("newErr").textContent = "Give the design a name."; return; }
  $("newGo").disabled = true;
  try {
    const p = await api("/api/projects/new", { method: "POST", body: { name } });
    if ($("newReq").value.trim()) await api(`/api/projects/${p.id}/components`, { method: "POST", body: { requirements: $("newReq").value.trim() } });
    $("newDlg").close();
    $("newName").value = ""; $("newReq").value = "";
    await loadProjects(p.id);
    showTab("parts");
  } catch (e) { $("newErr").textContent = e.message; }
  $("newGo").disabled = false;
};

// ---- DRC -----------------------------------------------------------------------------------
$("drcRun").onclick = async () => {
  if (!S.pid) return;
  $("drcRun").disabled = true;
  $("drcSummary").textContent = "Running kicad-cli DRC…";
  try { setDrc(await api(`/api/projects/${S.pid}/drc?refill=${$("drcRefill").checked}`, { method: "POST" })); }
  catch (e) { $("drcSummary").textContent = `DRC failed: ${e.message}`; }
  $("drcRun").disabled = false;
};
function setDrc(d) {
  S.drc = d;
  viewer.setDrcMarkers(d ? d.items : []);
  $("drcCount").textContent = d ? d.counts.total : "";
  if (!d) { $("drcBody").innerHTML = ""; $("drcSummary").textContent = "KiCad's own DRC engine (kicad-cli), using the board's rules."; return; }
  $("drcSummary").textContent = `Ran ${d.ran_at.replace("T", " ")} in ${d.seconds}s${d.refilled_zones ? " · zones refilled" : ""}`;
  const groups = {};
  d.items.forEach((it, i) => (groups[it.type] ||= []).push(i));
  const order = Object.keys(groups).sort((a, b) => groups[b].length - groups[a].length);
  $("drcBody").innerHTML = `<div class="drc-cards">
      <div class="card err"><span class="muted">Errors</span><b>${d.counts.errors}</b></div>
      <div class="card warn"><span class="muted">Warnings</span><b>${d.counts.warnings}</b></div>
      <div class="card"><span class="muted">Unconnected</span><b>${d.counts.unconnected}</b></div></div>` +
    order.map((t) => {
      const idx = groups[t];
      const sev = d.items[idx[0]].severity;
      return `<details class="drc-group"${order.length <= 3 ? " open" : ""}><summary><span class="sev ${esc(sev)}">${esc(sev)}</span><b>${esc(t)}</b><span class="muted">${idx.length}</span></summary>` +
        idx.slice(0, 300).map((i) => {
          const it = d.items[i];
          return `<div class="drc-item" data-i="${i}">${esc(it.description)}<div class="where">${it.items.map((l) => `${esc(l.text)}${l.x != null ? ` @ ${(+l.x).toFixed(2)}, ${(+l.y).toFixed(2)}` : ""}`).join("<br>")}</div></div>`;
        }).join("") + (idx.length > 300 ? `<div class="drc-item muted">…and ${idx.length - 300} more</div>` : "") + "</details>";
    }).join("");
}
$("drcBody").addEventListener("click", (e) => {
  const el = e.target.closest(".drc-item[data-i]");
  if (!el) return;
  showTab("pcb");
  const it = S.drc.items[+el.dataset.i];
  requestAnimationFrame(() => viewer.focusDrc(it));
});

// ---- chat ----------------------------------------------------------------------------------
function md(src) {
  // small markdown subset: fences, headings, lists, tables, bold/italic/code, part references
  const blocks = [];
  src = src.replace(/```[^\n]*\n([\s\S]*?)(```|$)/g, (_, code) => `\u0000${blocks.push(code) - 1}\u0000`);
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<i>$2</i>")
    .replace(/\b([A-Z]{1,4}\d{1,4}[A-Z]?)\b/g, (m) => (viewer.fpByRef?.has(m) ? `<span class="ref" data-ref="${m}">${m}</span>` : m));
  const out = [];
  const lines = src.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    const fence = l.match(/^\u0000(\d+)\u0000$/);
    if (fence) { out.push(`<pre><code>${esc(blocks[+fence[1]])}</code></pre>`); continue; }
    if (/^\s*\|.*\|\s*$/.test(l) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1] || "")) {
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
      let h = `<table><thead><tr>${cells(l).map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>`;
      i += 2;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { h += `<tr>${cells(lines[i]).map((c) => `<td>${c}</td>`).join("")}</tr>`; i++; }
      i--;
      out.push(h + "</tbody></table>");
      continue;
    }
    const hd = l.match(/^(#{1,4})\s+(.*)/);
    if (hd) { out.push(`<h${hd[1].length}>${inline(hd[2])}</h${hd[1].length}>`); continue; }
    const li = l.match(/^\s*([-*]|\d+\.)\s+(.*)/);
    if (li) {
      const tag = /\d/.test(li[1]) ? "ol" : "ul";
      let h = `<${tag}>`;
      while (i < lines.length) {
        const m = lines[i].match(/^\s*([-*]|\d+\.)\s+(.*)/);
        if (!m || /\d/.test(m[1]) !== (tag === "ol")) break;
        // keep the source number: models often put blank lines between items ("1." "2." each alone)
        h += `<li${tag === "ol" ? ` value="${parseInt(m[1], 10)}"` : ""}>${inline(m[2])}</li>`; i++;
        // a single blank line between items of the same list keeps the list going
        if (!lines[i]?.trim() && (lines[i + 1] || "").match(tag === "ol" ? /^\s*\d+\.\s+/ : /^\s*[-*]\s+/)) i++;
      }
      i--;
      out.push(h + `</${tag}>`);
      continue;
    }
    if (!l.trim()) { out.push(""); continue; }
    // merge consecutive prose lines into one paragraph
    if (out.length && out[out.length - 1].startsWith("<p>") && lines[i - 1]?.trim()) out[out.length - 1] = out[out.length - 1].replace(/<\/p>$/, `<br>${inline(l)}</p>`);
    else out.push(`<p>${inline(l)}</p>`);
  }
  return out.join("");
}

function el(html) { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; }
function scrollChat() { const m = $("msgs"); if (m.scrollHeight - m.scrollTop - m.clientHeight < 160) m.scrollTop = m.scrollHeight; }

function toolChip(name, state, summary, input) {
  return el(`<details class="tool"><summary><span class="dot ${state}"></span><span class="tname">${esc(name)}</span><span class="tsum">${esc(summary || "")}</span></summary>
    <pre>${esc(input ? JSON.stringify(input, null, 1) : "")}</pre></details>`);
}

class AssistantView {
  constructor() {
    this.root = el(`<div class="msg assistant"></div>`);
    $("msgs").appendChild(this.root);
    this.text = null; this.think = null; this.tools = new Map();
  }
  addText(d) {
    if (!this.text) { this.text = { raw: "", node: el(`<div class="md"></div>`) }; this.root.appendChild(this.text.node); this.think = null; }
    this.text.raw += d;
    this.text.node.innerHTML = md(this.text.raw);
  }
  addThinking(d) {
    if (!this.think) { this.think = el(`<details class="think"><summary>Thinking</summary><div></div></details>`); this.root.appendChild(this.think); this.text = null; }
    this.think.lastChild.textContent += d;
  }
  toolPending(id, name) {
    const n = toolChip(name, "run", "preparing…");
    this.root.appendChild(n); this.tools.set(id, n); this.text = null; this.think = null;
  }
  toolStart(id, name, input) {
    let n = this.tools.get(id);
    if (!n) { n = toolChip(name, "run", ""); this.root.appendChild(n); this.tools.set(id, n); }
    n.querySelector(".tsum").textContent = "running…";
    n.querySelector("pre").textContent = JSON.stringify(input, null, 1);
    this.text = null; this.think = null;
  }
  toolEnd(id, ok, summary) {
    const n = this.tools.get(id);
    if (!n) return;
    n.querySelector(".dot").className = `dot ${ok ? "ok" : "bad"}`;
    n.querySelector(".tsum").textContent = summary || "";
  }
  error(m) { this.root.appendChild(el(`<div class="error">${esc(m)}</div>`)); this.text = null; }
  usage(u, model) {
    if (!u || u.input == null) return;
    const plan = u.billing === "subscription" ? " · Claude plan" : "";
    this.root.appendChild(el(`<div class="usage">${esc(model || "")}${plan} · in ${u.input} (cache read ${u.cache_read || 0}, write ${u.cache_write || 0}) · out ${u.output}</div>`));
  }
}

function renderHistory(ui) {
  $("msgs").innerHTML = "";
  if (!ui.length) { $("msgs").appendChild(emptyNode); return; }
  for (const m of ui) {
    if (m.role === "user") { $("msgs").appendChild(el(`<div class="msg user">${esc(m.text)}</div>`)); continue; }
    const v = new AssistantView();
    for (const p of m.parts || []) {
      if (p.t === "text") v.addText(p.text);
      else if (p.t === "thinking") v.addThinking(p.text);
      else if (p.t === "error") v.error(p.text);
      else if (p.t === "tool") { const n = toolChip(p.name, p.ok ? "ok" : "bad", p.summary, p.input); v.root.appendChild(n); v.text = null; v.think = null; }
    }
    if (!(m.parts || []).length) v.error("No reply was saved for this turn.");
    v.usage(m.usage, m.model);
  }
  $("msgs").scrollTop = $("msgs").scrollHeight;
}
const emptyNode = $("chatEmpty");

async function loadChats(select) {
  const sel = $("chatSel");
  if (!S.pid) { sel.innerHTML = ""; return; }
  const list = await api(`/api/projects/${S.pid}/chats`);
  sel.innerHTML = `<option value="">New conversation</option>` +
    list.map((c) => `<option value="${esc(c.id)}">${esc(c.title || "(untitled)")} · ${esc(c.created.slice(5, 16).replace("T", " "))}</option>`).join("");
  const want = select !== undefined ? select
    : new URLSearchParams(location.search).get("chat") || store.get(`pcbstudio.chat.${S.pid}`, "");
  sel.value = list.some((c) => c.id === want) ? want : "";
  await openChat(sel.value);
}
async function openChat(cid) {
  S.chat = cid || null;
  store.set(`pcbstudio.chat.${S.pid}`, S.chat || "");
  if (!S.chat) { renderHistory([]); return; }
  const c = await api(`/api/projects/${S.pid}/chats/${S.chat}`);
  renderHistory(c.ui);
}
$("chatSel").addEventListener("change", () => openChat($("chatSel").value));
$("newChatBtn").onclick = () => { $("chatSel").value = ""; openChat(""); $("input").focus(); };

function renderCtx() {
  const parts = [];
  if (S.refs.length) parts.push(`<span class="chip">${esc(S.refs.slice(0, 6).join(", "))}${S.refs.length > 6 ? ` +${S.refs.length - 6}` : ""}<button data-x="refs" title="Don't send">×</button></span>`);
  if (S.net) parts.push(`<span class="chip">net ${esc(netName(S.net))}<button data-x="net" title="Don't send">×</button></span>`);
  $("ctxChips").innerHTML = parts.join("");
}
$("ctxChips").addEventListener("click", (e) => {
  const x = e.target.dataset.x;
  if (x === "refs") { S.refs = []; viewer.select([]); }
  if (x === "net") { S.net = 0; viewer.highlightNet(0); }
  renderCtx(); renderInfo(); markBomRows();
});

function toastStatus(t) { $("chatStatus").textContent = t; }
function setBusy(b) {
  S.busy = b;
  $("sendBtn").disabled = b;
  $("stopBtn").hidden = !b;
  if (!b) toastStatus("");
}

async function send() {
  const text = $("input").value.trim();
  if (!text || S.busy) return;
  if (!S.pid) { toastStatus("Open a board first."); return; }
  $("input").value = "";
  if (emptyNode.isConnected) emptyNode.remove();
  $("msgs").appendChild(el(`<div class="msg user">${esc(text)}</div>`));
  const view = new AssistantView();
  scrollChat();
  setBusy(true);
  toastStatus("Thinking…");
  const ctrl = new AbortController();
  S.abort = ctrl;
  let model = $("modelSel").value;
  try {
    const r = await fetch(`/api/projects/${S.pid}/chat`, {
      method: "POST", headers: { "Content-Type": "application/json" }, signal: ctrl.signal,
      body: JSON.stringify({ message: text, conversation: S.chat, provider: $("providerSel").value, model, effort: $("effortSel").value,
        selection: { refs: S.refs, net: netName(S.net) || null } }),
    });
    if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail; } catch { /* */ } throw new Error(m); }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let k;
      while ((k = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, k); buf = buf.slice(k + 2);
        const line = chunk.split("\n").filter((l) => l.startsWith("data: ")).map((l) => l.slice(6)).join("\n");
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line); } catch { continue; }
        await handleEvent(ev, view, model, (m) => { model = m; });
        scrollChat();
      }
    }
  } catch (e) {
    if (e.name === "AbortError") view.error("Stopped watching. The turn keeps running on the server and is saved to this conversation.");
    else view.error(e.message);
  } finally {
    S.abort = null;
    setBusy(false);
    loadChats(S.chat || undefined).catch(() => {});
  }
}

async function handleEvent(ev, view, model, setModel) {
  switch (ev.type) {
    case "start":
      S.chat = ev.conversation; setModel(ev.model);
      store.set(`pcbstudio.chat.${S.pid}`, S.chat);
      break;
    case "text": toastStatus("Writing…"); view.addText(ev.delta); break;
    case "thinking": toastStatus("Thinking…"); view.addThinking(ev.delta); break;
    case "tool_pending": toastStatus(`Preparing ${ev.name}…`); view.toolPending(ev.id, ev.name); break;
    case "tool_start": toastStatus(`Running ${ev.name}…`); view.toolStart(ev.id, ev.name, ev.input); break;
    case "tool_end": view.toolEnd(ev.id, ev.ok, ev.summary); break;
    case "status": toastStatus(ev.text); break;
    case "error": view.error(ev.message); break;
    case "board": await reloadBoard(); break;
    case "phase": await loadPhases(); loadLayout(); loadRouting(); if (ev.phase) showTab(STEP_TAB[ev.phase] || "parts"); break;
    case "routing": loadRouting(); break;
    case "drc":
      api(`/api/projects/${S.pid}/drc`).then((d) => d && d.items && setDrc(d));
      break;
    case "ui": {
      if (ev.layers) for (const l of ev.layers) { viewer.visible.add(l); }
      if (ev.layers) buildLayerPanelChecks();
      S.refs = ev.refs || [];
      S.net = netIndex(ev.net);
      viewer.select(S.refs, S.net);
      showTab("pcb");
      if (ev.zoom) requestAnimationFrame(() => (S.refs.length ? viewer.zoomTo(S.refs) : S.net && viewer.zoomToNet(S.net)));
      renderCtx(); renderInfo(); markBomRows();
      break;
    }
    case "done": view.usage(ev.usage, model); break;
  }
}
function buildLayerPanelChecks() {
  document.querySelectorAll("#layersPanel .lrow[data-layer]").forEach((r) => { r.querySelector("input").checked = viewer.visible.has(r.dataset.layer); });
  viewer.netCache?.clear(); viewer.render(); saveVis();
}

$("sendBtn").onclick = send;
$("stopBtn").onclick = () => S.abort?.abort();
$("input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
$("msgs").addEventListener("click", (e) => {
  const r = e.target.closest(".ref");
  if (!r) return;
  S.refs = [r.dataset.ref]; S.net = 0;
  viewer.select(S.refs, 0); showTab("pcb");
  requestAnimationFrame(() => viewer.zoomTo(S.refs));
  renderCtx(); renderInfo(); markBomRows();
});

// ---- models & settings ---------------------------------------------------------------------
async function loadProviders() {
  const p = await api("/api/providers");
  const want = store.get("pcbstudio.provider", p.active);
  $("providerSel").innerHTML = p.providers.map((x) =>
    `<option value="${esc(x.id)}"${x.available ? "" : " disabled"}>${esc(x.name)}${x.available ? "" : " (not found)"}</option>`).join("");
  const ok = p.providers.find((x) => x.id === want && x.available);
  $("providerSel").value = ok ? want : p.active;
  await loadModels();
}
async function loadModels() {
  const s = await api("/api/settings");
  const prov = $("providerSel").value;
  const fallback = { anthropic: s.model, claude_code: s.cc_model, openai: s.openai_model }[prov] || "";
  const want = store.get(`pcbstudio.model.${prov}`, fallback);
  let list = [];
  try { list = (await api(`/api/models?provider=${encodeURIComponent(prov)}`)).models; } catch { /* offline: defaults below */ }
  if (!list.length && fallback) list = [{ id: fallback, name: fallback }];
  $("modelSel").innerHTML = list.length
    ? list.map((m) => `<option value="${esc(m.id)}">${esc(m.name || m.id)}</option>`).join("")
    : `<option value="">(no models: see Settings)</option>`;
  $("modelSel").value = list.some((m) => m.id === want) ? want : (list[0]?.id ?? "");
  $("effortWrap").hidden = prov === "openai";
  $("effortSel").value = store.get("pcbstudio.effort", s.effort || "high");
  if (prov === "anthropic" && !s.anthropic_key_set && !s.anthropic_key_env)
    toastStatus("No Anthropic API key yet: add one in Settings, or switch to Claude Code to use your Claude plan.");
  if (prov === "openai" && !list.length) toastStatus(`No models from ${s.openai_base_url}: start the server or set a model name in Settings.`);
  return s;
}
$("providerSel").addEventListener("change", () => { store.set("pcbstudio.provider", $("providerSel").value); loadModels(); });
$("modelSel").addEventListener("change", () => store.set(`pcbstudio.model.${$("providerSel").value}`, $("modelSel").value));
$("effortSel").addEventListener("change", () => store.set("pcbstudio.effort", $("effortSel").value));

$("settingsBtn").onclick = async () => {
  const s = await api("/api/settings");
  $("keyInput").value = "";
  $("keyState").textContent = s.anthropic_key_set ? "A key is saved. Leave blank to keep it." :
    s.anthropic_key_env ? "Using ANTHROPIC_API_KEY from the environment." : "No key set.";
  $("cliInput").value = s.kicad_cli || "";
  $("cliState").textContent = s.kicad_cli_found ? `Found: ${s.kicad_cli_found}` : "Not found: DRC needs kicad-cli.";
  $("claudeCliInput").value = s.claude_cli || "";
  $("claudeCliState").textContent = s.claude_cli_found ? `Found: ${s.claude_cli_found}` : "Not found: install Claude Code, or give the path to claude.exe.";
  $("oaBaseInput").value = s.openai_base_url || "";
  $("oaKeyInput").value = "";
  $("oaKeyState").textContent = s.openai_key_set ? "A key is saved. Leave blank to keep it." : "No key set.";
  $("oaModelInput").value = s.openai_model || "";
  $("chatSideSel").value = s.chat_side || "right";
  $("frInput").value = s.fastroute_exe || "";
  $("frState").textContent = s.fastroute_found ? `Found: ${s.fastroute_found}` : "Not found: Phase 3 routing needs fastroute.exe.";
  api("/api/changelist").then((c) => { $("clState").textContent = `${c.open.length} open, ${c.done} done, in ${c.file}.`; }).catch(() => {});
  $("settingsDlg").showModal();
};
$("settingsSave").onclick = async () => {
  const body = { kicad_cli: $("cliInput").value.trim(), claude_cli: $("claudeCliInput").value.trim(),
    openai_base_url: $("oaBaseInput").value.trim(), openai_model: $("oaModelInput").value.trim(),
    chat_side: $("chatSideSel").value, fastroute_exe: $("frInput").value.trim() };
  if ($("keyInput").value.trim()) body.anthropic_api_key = $("keyInput").value.trim();
  if ($("oaKeyInput").value.trim()) body.openai_api_key = $("oaKeyInput").value.trim();
  await api("/api/settings", { method: "POST", body });
  $("keyInput").value = ""; $("oaKeyInput").value = "";
  $("settingsDlg").close();
  applyChatSide(body.chat_side);
  await loadProviders();
  loadRouting();
};

// ---- import dialog -------------------------------------------------------------------------
let browsePath = null, pickedBoard = null, browseKind = "board";
const FILE_RE = { board: /\.kicad_pcb$/i, netlist: /\.(kicad_sch|net)$/i };
async function browse(path) {
  try {
    const d = await api(`/api/browse?kind=${browseKind}${path ? `&path=${encodeURIComponent(path)}` : ""}`);
    browsePath = d.path; pickedBoard = null;
    $("pathInput").value = d.path;
    $("upBtn").disabled = !d.parent;
    $("upBtn").dataset.parent = d.parent || "";
    $("importGo").disabled = true;
    $("importErr").textContent = "";
    $("browser").innerHTML = d.dirs.map((n) => `<div data-dir="${esc(n)}"><span class="kind">▸</span>${esc(n)}</div>`).join("") +
      d.boards.map((b) => `<div data-board="${esc(b.name)}"><span class="kind">▣</span>${esc(b.name)}<span class="size">${(b.size / 1048576).toFixed(1)} MB</span></div>`).join("") ||
      `<div class="muted">No folders or ${browseKind === "board" ? ".kicad_pcb" : ".kicad_sch / .net"} files here.</div>`;
  } catch (e) { $("importErr").textContent = e.message; }
}
const join = (a, b) => (a.endsWith("\\") || a.endsWith("/") ? a + b : `${a}\\${b}`);
$("browser").addEventListener("click", (e) => {
  const d = e.target.closest("div[data-dir], div[data-board]");
  if (!d) return;
  if (d.dataset.dir) return browse(join(browsePath, d.dataset.dir));
  pickedBoard = join(browsePath, d.dataset.board);
  $("browser").querySelectorAll(".sel").forEach((x) => x.classList.remove("sel"));
  d.classList.add("sel");
  $("importGo").disabled = false;
});
$("browser").addEventListener("dblclick", (e) => { if (e.target.closest("div[data-board]")) $("importGo").click(); });
$("upBtn").onclick = () => browse($("upBtn").dataset.parent);
$("pathInput").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  e.preventDefault();
  const v = $("pathInput").value.trim();
  if (FILE_RE[browseKind].test(v)) { pickedBoard = v; $("importGo").disabled = false; } else browse(v);
});
$("importGo").onclick = async () => {
  if (!pickedBoard) return;
  $("importGo").disabled = true;
  $("importErr").textContent = "";
  try {
    if (browseKind === "netlist") {
      const r = await api(`/api/projects/${S.pid}/netlist/import`, { method: "POST", body: { path: pickedBoard } });
      $("importDlg").close();
      toastStatus(`Netlist: ${r.netlist.parts} parts, ${r.netlist.nets} nets, ${r.netlist.issues.length} issue(s).`);
      loadPhases();
      return;
    }
    const p = await api("/api/projects/import", { method: "POST", body: { path: pickedBoard, name: $("projName").value.trim() || null } });
    $("importDlg").close();
    $("projName").value = "";
    if (p.missing?.length) toastStatus(`Imported. Not found next to the board: ${p.missing.join(", ")} (default rules apply).`);
    await loadProjects(p.id);
  } catch (e) { $("importErr").textContent = e.message; $("importGo").disabled = false; }
};
function openBrowse(kind) {
  const changed = kind !== browseKind;
  browseKind = kind;
  $("importTitle").textContent = kind === "board" ? "Open a KiCad board" : "Import a schematic or netlist";
  $("importHelp").innerHTML = kind === "board"
    ? "PCB Studio copies the board and its <code>.kicad_pro</code> / <code>.kicad_dru</code> into a project folder. Edits only touch the copy."
    : "Pick a KiCad schematic (<code>.kicad_sch</code>, exported to a netlist with kicad-cli) or a KiCad netlist (<code>.net</code>). Every symbol needs a footprint.";
  $("projNameWrap").hidden = kind !== "board";
  $("importGo").textContent = kind === "board" ? "Import" : "Import netlist";
  $("importDlg").showModal();
  browse(changed ? browsePath : browsePath);
}
const openImport = () => openBrowse("board");
$("newBtn").onclick = openNew;
$("newBtn2").onclick = openNew;
$("importBtn").onclick = openImport;
$("importBtn2").onclick = openImport;
$("projectSel").addEventListener("change", () => openProject($("projectSel").value));

// ---- chat side (right = VS Code style) -------------------------------------------------------
function applyChatSide(side) {
  document.querySelector(".split").classList.toggle("chat-right", side !== "left");
  store.set("pcbstudio.chatSide", side);
  requestAnimationFrame(() => viewer.render());
}
applyChatSide(store.get("pcbstudio.chatSide", "right"));  // no flash while the setting loads
api("/api/settings").then((s) => applyChatSide(s.chat_side || "right")).catch(() => {});

// ---- resizable split -----------------------------------------------------------------------
(() => {
  const g = $("gutter"), pane = $("chatPane");
  const w = store.get("pcbstudio.chatW", null);
  if (w) pane.style.width = `${w}px`;
  g.addEventListener("pointerdown", (e) => {
    g.setPointerCapture(e.pointerId); g.classList.add("drag");
    const move = (ev) => {
      const x = document.querySelector(".split").classList.contains("chat-right") ? window.innerWidth - ev.clientX : ev.clientX;
      pane.style.width = `${Math.max(300, Math.min(x, window.innerWidth * 0.7))}px`; viewer.render();
    };
    const up = () => { g.classList.remove("drag"); g.removeEventListener("pointermove", move); g.removeEventListener("pointerup", up); store.set("pcbstudio.chatW", pane.offsetWidth); };
    g.addEventListener("pointermove", move); g.addEventListener("pointerup", up);
  });
})();

// ---- boot ----------------------------------------------------------------------------------
// Pick up edits made outside the app (e.g. the board opened in KiCad and saved).
setInterval(async () => {
  if (!S.pid || !S.board || S.busy || document.hidden) return;
  try { const r = await api(`/api/projects/${S.pid}/rev`); if (r.rev !== S.board.rev) await reloadBoard(); } catch { /* server gone */ }
}, 4000);

showTab(["parts", "pcb", "bom", "drc"].includes(new URLSearchParams(location.search).get("tab")) ? new URLSearchParams(location.search).get("tab") : "pcb");
loadProviders().catch((e) => toastStatus(e.message));
loadProjects().catch((e) => toastStatus(e.message));
