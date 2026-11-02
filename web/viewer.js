// PCB viewer: Canvas 2D, cached Path2D per layer and object type, board coordinates in mm.
// Exposes: setBoard, render, fit, zoomTo, select, highlightNet, setVisible, setFlip, setDrcMarkers.

const LAYER_COLORS = {
  "F.Cu": "#c83434", "B.Cu": "#4d7fc4",
  "F.Silkscreen": "#f2eda1", "B.Silkscreen": "#e8b2a7",
  "F.Courtyard": "#ff26e2", "B.Courtyard": "#26e9ff",
  "F.Fab": "#afafaf", "B.Fab": "#585d84",
  "F.Mask": "#d864ff", "B.Mask": "#02ffee",
  "F.Paste": "#b4a0a0", "B.Paste": "#00c2c2",
  "Edge.Cuts": "#d0d2cd", "Margin": "#ff26e2",
  "User.Drawings": "#c2c2c2", "User.Comments": "#5994dc",
  "User.Eco1": "#b4dbd2", "User.Eco2": "#d8c852",
};
const INNER_COLORS = ["#7fc87f", "#ce7d2c", "#4fcbcb", "#db628b", "#a7a5c6", "#28ccd9", "#e6b33c", "#9c6ccf",
  "#69a84f", "#c96f9b", "#5b9bd5", "#d4a76a"];

export function layerColor(name, copperIndex) {
  if (LAYER_COLORS[name]) return LAYER_COLORS[name];
  if (copperIndex !== undefined && copperIndex > 0) return INNER_COLORS[(copperIndex - 1) % INNER_COLORS.length];
  return "#9aa0a6";
}

function ringPath(path, flat) {
  if (flat.length < 4) return;
  path.moveTo(flat[0], flat[1]);
  for (let i = 2; i < flat.length; i += 2) path.lineTo(flat[i], flat[i + 1]);
  path.closePath();
}

function polysInto(path, polys) {
  for (const rings of polys) for (const r of rings) ringPath(path, r);
}

function shapeInto(widthMap, fillPath, g) {
  // board/footprint graphic -> stroked polyline (grouped by width) or filled polygon
  const w = Math.max(g.w || 0, 0.02);
  if (!widthMap.has(w)) widthMap.set(w, new Path2D());
  const p = widthMap.get(w);
  if (g.k === "line") {
    p.moveTo(g.p[0], g.p[1]);
    for (let i = 2; i < g.p.length; i += 2) p.lineTo(g.p[i], g.p[i + 1]);
    if (g.fill) ringPath(fillPath, g.p);
  } else if (g.k === "circle") {
    p.moveTo(g.c[0] + g.r, g.c[1]);
    p.arc(g.c[0], g.c[1], g.r, 0, Math.PI * 2);
    if (g.fill) { fillPath.moveTo(g.c[0] + g.r, g.c[1]); fillPath.arc(g.c[0], g.c[1], g.r, 0, Math.PI * 2); }
  } else if (g.k === "poly") {
    for (const rings of g.polys) for (const r of rings) ringPath(p, r);
    if (g.fill) polysInto(fillPath, g.polys);
  }
}

function pointInRing(x, y, r) {
  let inside = false;
  for (let i = 0, j = r.length - 2; i < r.length; j = i, i += 2) {
    const xi = r[i], yi = r[i + 1], xj = r[j], yj = r[j + 1];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

function distSeg(px, py, x1, y1, x2, y2) {
  const dx = x2 - x1, dy = y2 - y1;
  const L = dx * dx + dy * dy;
  let t = L ? ((px - x1) * dx + (py - y1) * dy) / L : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
}

export class Viewer {
  constructor(canvas, opts = {}) {
    this.c = canvas;
    this.ctx = canvas.getContext("2d");
    this.opts = opts; // { onSelect(refs, net), onHover(x, y), onMove(ref, x, y) }
    this.b = null;
    this.view = { s: 5, tx: 0, ty: 0 };
    this.flip = false;
    this.insetRight = 0; // px covered by overlays on the right
    this.visible = new Set();
    this.show = { tracks: true, vias: true, pads: true, zones: true, refs: true, keepouts: true, drc: true, fpF: true, fpB: true };
    this.active = null;
    this.selected = new Set();
    this.net = 0;
    this.drc = [];
    this.drcFocus = null;
    this.drag = null;
    this._raf = 0;
    new ResizeObserver(() => this.render()).observe(canvas);
    this._bindInput();
  }

  // ---------- data -------------------------------------------------------------------
  setBoard(b, keepView = false) {
    const first = !this.b;
    this.b = b;
    this.copper = b.layers.filter((l) => l.copper).map((l) => l.name);
    this.copperIdx = Object.fromEntries(this.copper.map((n, i) => [n, n === "B.Cu" ? this.copper.length : i]));
    this.fpByRef = new Map(b.footprints.map((f) => [f.ref, f]));
    for (const r of [...this.selected]) if (!this.fpByRef.has(r)) this.selected.delete(r);
    if (this.net >= b.nets.length) this.net = 0;
    this._build();
    if (first || !keepView) this.fit();
    else this.render();
  }

  color(name) {
    return layerColor(name, this.copperIdx ? this.copperIdx[name] : undefined);
  }

  _build() {
    const b = this.b;
    const L = {};
    const get = (name) => (L[name] ||= { tracks: new Map(), pads: new Path2D(), vias: new Path2D(), zones: new Path2D(),
      gfx: new Map(), gfxFill: new Path2D(), hasGfx: false, hasCopper: false });
    for (const [layer, a] of Object.entries(b.tracks)) {
      const e = get(layer);
      e.hasCopper = true;
      for (let i = 0; i < a.length; i += 6) {
        const w = a[i + 4];
        if (!e.tracks.has(w)) e.tracks.set(w, new Path2D());
        const p = e.tracks.get(w);
        p.moveTo(a[i], a[i + 1]);
        p.lineTo(a[i + 2], a[i + 3]);
      }
    }
    for (const z of b.zones) {
      if (z.rule) continue;
      const e = get(z.layer);
      e.hasCopper = true;
      polysInto(e.zones, z.polys);
    }
    this.drills = new Path2D();
    for (const v of b.vias) {
      const [x, y, dia, drill, top, bot] = v;
      const i0 = this.copper.indexOf(top), i1 = this.copper.indexOf(bot);
      for (let i = Math.min(i0, i1); i <= Math.max(i0, i1); i++) {
        const e = get(this.copper[i]);
        e.vias.moveTo(x + dia / 2, y);
        e.vias.arc(x, y, dia / 2, 0, Math.PI * 2);
      }
      this.drills.moveTo(x + drill / 2, y);
      this.drills.arc(x, y, drill / 2, 0, Math.PI * 2);
    }
    // footprint pads and graphics are kept per side so front/back components can be hidden without a rebuild
    const FP = { F: {}, B: {} };
    const getf = (side, name) => (FP[side][name] ||= { pads: new Path2D(), gfx: new Map(), gfxFill: new Path2D() });
    this.padDrills = { F: new Path2D(), B: new Path2D() };
    for (const f of b.footprints) {
      const side = f.side === "B" ? "B" : "F";
      for (const p of f.pads) {
        for (const ln of p.layers) {
          get(ln).hasCopper = true;
          polysInto(getf(side, ln).pads, p.poly);
        }
        if (p.drill) {
          this.padDrills[side].moveTo(p.c[0] + p.drill / 2, p.c[1]);
          this.padDrills[side].arc(p.c[0], p.c[1], p.drill / 2, 0, Math.PI * 2);
        }
      }
      for (const [ln, gs] of Object.entries(f.graphics)) {
        get(ln).hasGfx = true;
        const fe = getf(side, ln);
        for (const g of gs) shapeInto(fe.gfx, fe.gfxFill, g);
      }
    }
    this.FP = FP;
    for (const [ln, gs] of Object.entries(b.drawings)) {
      const e = get(ln);
      e.hasGfx = true;
      for (const g of gs) shapeInto(e.gfx, e.gfxFill, g);
    }
    this.keepouts = new Path2D();
    for (const z of b.zones) if (z.rule) polysInto(this.keepouts, z.outline);
    this.L = L;
    this.netCache = new Map();
  }

  _netPaths(idx) {
    if (this.netCache.has(idx)) return this.netCache.get(idx);
    const b = this.b;
    const tracks = new Map(), fill = new Path2D(), zones = new Path2D();
    for (const a of Object.values(b.tracks)) {
      for (let i = 0; i < a.length; i += 6) {
        if (a[i + 5] !== idx) continue;
        const w = a[i + 4];
        if (!tracks.has(w)) tracks.set(w, new Path2D());
        const p = tracks.get(w);
        p.moveTo(a[i], a[i + 1]);
        p.lineTo(a[i + 2], a[i + 3]);
      }
    }
    for (const v of b.vias) if (v[6] === idx) { fill.moveTo(v[0] + v[2] / 2, v[1]); fill.arc(v[0], v[1], v[2] / 2, 0, Math.PI * 2); }
    for (const f of b.footprints) for (const p of f.pads) if (p.net === idx) polysInto(fill, p.poly);
    for (const z of b.zones) if (!z.rule && z.net === idx && this.visible.has(z.layer)) polysInto(zones, z.polys);
    const r = { tracks, fill, zones };
    this.netCache.set(idx, r);
    return r;
  }

  // ---------- view -------------------------------------------------------------------
  fit() {
    if (!this.b) return;
    const [x0, y0, x1, y1] = this.b.bbox;
    const W = this.c.clientWidth || 800, H = this.c.clientHeight || 600;
    const free = Math.max(W - this.insetRight, W * 0.4); // keep the board clear of the layers panel
    const s = Math.min(free / (x1 - x0 || 1), H / (y1 - y0 || 1)) * 0.92;
    const fx = this.flip ? -1 : 1;
    this.view = { s, tx: free / 2 - fx * s * (x0 + x1) / 2, ty: H / 2 - s * (y0 + y1) / 2 };
    this.render();
  }

  _center(cx, cy, s) {
    const W = this.c.clientWidth || 800, H = this.c.clientHeight || 600;
    const fx = this.flip ? -1 : 1;
    this.view = { s, tx: W / 2 - fx * s * cx, ty: H / 2 - s * cy };
    this.render();
  }

  zoomTo(refs) {
    const fps = refs.map((r) => this.fpByRef.get(r)).filter(Boolean);
    if (!fps.length) return;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const f of fps) { x0 = Math.min(x0, f.bbox[0]); y0 = Math.min(y0, f.bbox[1]); x1 = Math.max(x1, f.bbox[2]); y1 = Math.max(y1, f.bbox[3]); }
    this.zoomToBox(x0, y0, x1, y1);
  }

  zoomToBox(x0, y0, x1, y1, minSpan = 6) {
    const W = this.c.clientWidth, H = this.c.clientHeight;
    const w = Math.max(x1 - x0, minSpan), h = Math.max(y1 - y0, minSpan);
    const s = Math.min(Math.min(W / w, H / h) * 0.6, 400);
    this._center((x0 + x1) / 2, (y0 + y1) / 2, s);
  }

  zoomToNet(idx) {
    const b = this.b;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    const add = (x, y) => { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); };
    for (const f of b.footprints) for (const p of f.pads) if (p.net === idx) add(p.c[0], p.c[1]);
    for (const a of Object.values(b.tracks)) for (let i = 0; i < a.length; i += 6) if (a[i + 5] === idx) { add(a[i], a[i + 1]); add(a[i + 2], a[i + 3]); }
    if (x0 < Infinity) this.zoomToBox(x0, y0, x1, y1);
  }

  setFlip(on) {
    // keep the board centre where it is when switching sides
    const W = this.c.clientWidth, H = this.c.clientHeight;
    const [cx, cy] = this.toWorld(W / 2, H / 2);
    this.flip = on;
    this._center(cx, cy, this.view.s);
  }

  toWorld(sx, sy) {
    const fx = this.flip ? -1 : 1;
    return [(sx - this.view.tx) / (fx * this.view.s), (sy - this.view.ty) / this.view.s];
  }

  toScreen(x, y) {
    const fx = this.flip ? -1 : 1;
    return [fx * this.view.s * x + this.view.tx, this.view.s * y + this.view.ty];
  }

  setVisible(name, on) { on ? this.visible.add(name) : this.visible.delete(name); this.netCache?.clear(); this.render(); }
  setActive(name) { this.active = name; this.render(); }
  select(refs, net = undefined) {
    this.selected = new Set(refs || []);
    if (net !== undefined) this.net = net || 0;
    this.render();
  }
  highlightNet(idx) { this.net = idx || 0; this.render(); }
  setDrcMarkers(items) { this.drc = items || []; this.render(); }
  focusDrc(item) {
    this.drcFocus = item;
    const pts = item.items.filter((l) => l.x != null);
    if (pts.length) {
      const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
      this.zoomToBox(Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys), 3);
    } else this.render();
  }

  _sides() { return ["F", "B"].filter((sd) => this.show[`fp${sd}`]); }
  _fpShown(f) { return this.show[f.side === "B" ? "fpB" : "fpF"]; }

  render() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => { this._raf = 0; this._draw(); });
  }

  // ---------- drawing ----------------------------------------------------------------
  _draw() {
    const c = this.c, ctx = this.ctx, dpr = window.devicePixelRatio || 1;
    const W = c.clientWidth, H = c.clientHeight;
    if (c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); }
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    const bg = getComputedStyle(c).getPropertyValue("--canvas-bg").trim() || "#0b0e13";
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, c.width, c.height);
    if (!this.b) return;
    const { s, tx, ty } = this.view, fx = this.flip ? -1 : 1;
    ctx.setTransform(dpr * fx * s, 0, 0, dpr * s, dpr * tx, dpr * ty);
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    const px = 1 / s; // one CSS pixel in mm

    const dim = this.net ? 0.28 : 1;
    // copper, far side first
    let order = [...this.copper];
    if (!this.flip) order.reverse();
    if (this.active && order.includes(this.active)) order = order.filter((n) => n !== this.active).concat([this.active]);
    for (const name of order) {
      if (!this.visible.has(name)) continue;
      const e = this.L[name];
      if (!e) continue;
      const col = this.color(name);
      const base = (this.active && name !== this.active ? 0.55 : 0.9) * dim;
      ctx.fillStyle = col;
      ctx.strokeStyle = col;
      if (this.show.zones) { ctx.globalAlpha = base * 0.42; ctx.fill(e.zones, "evenodd"); }
      ctx.globalAlpha = base;
      if (this.show.tracks) for (const [w, p] of e.tracks) { ctx.lineWidth = w; ctx.stroke(p); }
      if (this.show.pads) for (const sd of this._sides()) { const fe = this.FP[sd][name]; if (fe) ctx.fill(fe.pads, "nonzero"); }
      if (this.show.vias) ctx.fill(e.vias, "nonzero");
    }
    ctx.globalAlpha = 1;
    ctx.fillStyle = bg;
    if (this.show.vias) ctx.fill(this.drills);
    if (this.show.pads) for (const sd of this._sides()) ctx.fill(this.padDrills[sd]);

    // technical layers, far side first
    const tech = Object.keys(this.L).filter((n) => !this.copper.includes(n) && this.L[n].hasGfx);
    const sideRank = (n) => (n.startsWith("B.") ? (this.flip ? 2 : 0) : n.startsWith("F.") ? (this.flip ? 0 : 2) : 1);
    tech.sort((a, b) => sideRank(a) - sideRank(b) || (a === "Edge.Cuts") - (b === "Edge.Cuts"));
    for (const name of tech) {
      if (!this.visible.has(name)) continue;
      const e = this.L[name];
      const col = this.color(name);
      ctx.strokeStyle = col; ctx.fillStyle = col;
      ctx.globalAlpha = (name === "Edge.Cuts" ? 1 : 0.85) * (name === "Edge.Cuts" ? 1 : dim);
      for (const [w, p] of e.gfx) { ctx.lineWidth = Math.max(w, px); ctx.stroke(p); }
      ctx.fill(e.gfxFill, "evenodd");
      for (const sd of this._sides()) {
        const fe = this.FP[sd][name];
        if (!fe) continue;
        for (const [w, p] of fe.gfx) { ctx.lineWidth = Math.max(w, px); ctx.stroke(p); }
        ctx.fill(fe.gfxFill, "evenodd");
      }
    }
    ctx.globalAlpha = 1;

    if (this.show.keepouts) {
      ctx.setLineDash([4 * px, 3 * px]);
      ctx.strokeStyle = "#e05d5d"; ctx.lineWidth = px;
      ctx.stroke(this.keepouts);
      ctx.setLineDash([]);
    }

    // net highlight overlay
    if (this.net) {
      const n = this._netPaths(this.net);
      ctx.fillStyle = "#ffffff"; ctx.strokeStyle = "#ffffff";
      ctx.globalAlpha = 0.25; ctx.fill(n.zones, "evenodd");
      ctx.globalAlpha = 0.95;
      for (const [w, p] of n.tracks) { ctx.lineWidth = Math.max(w, 1.5 * px); ctx.stroke(p); }
      ctx.fill(n.fill);
      ctx.globalAlpha = 1;
    }

    // selection
    ctx.strokeStyle = "#ffd400"; ctx.lineWidth = 2 * px;
    for (const r of this.selected) {
      const f = this.fpByRef.get(r);
      if (!f) continue;
      const [x0, y0, x1, y1] = f.bbox;
      ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
    }
    if (this.drag && this.drag.moved && this.drag.ref) {
      const f = this.fpByRef.get(this.drag.ref);
      const [dx, dy] = [this.drag.dx, this.drag.dy];
      ctx.save();
      ctx.translate(dx, dy);
      ctx.setLineDash([3 * px, 2 * px]);
      ctx.strokeStyle = "#ffd400";
      ctx.strokeRect(f.bbox[0], f.bbox[1], f.bbox[2] - f.bbox[0], f.bbox[3] - f.bbox[1]);
      ctx.setLineDash([]);
      ctx.fillStyle = "rgba(255,212,0,0.45)";
      const p2 = new Path2D();
      for (const p of f.pads) polysInto(p2, p.poly);
      ctx.fill(p2);
      ctx.restore();
    }

    // DRC markers (screen-constant size)
    if (this.show.drc && this.drc.length) {
      for (const it of this.drc) {
        const focus = it === this.drcFocus;
        ctx.strokeStyle = focus ? "#ffffff" : it.severity === "error" ? "#ff4d4f" : "#f5a623";
        ctx.lineWidth = (focus ? 2.5 : 1.5) * px;
        for (const l of it.items) {
          if (l.x == null) continue;
          const r = (focus ? 9 : 5) * px;
          ctx.beginPath();
          ctx.moveTo(l.x - r, l.y - r); ctx.lineTo(l.x + r, l.y + r);
          ctx.moveTo(l.x + r, l.y - r); ctx.lineTo(l.x - r, l.y + r);
          ctx.stroke();
        }
      }
    }

    // reference text in screen space (never mirrored)
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (this.show.refs) {
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      for (const f of this.b.footprints) {
        if (!this._fpShown(f)) continue;
        const silk = f.side === "F" ? "F.Silkscreen" : "B.Silkscreen";
        if (!this.visible.has(silk) && !this.selected.has(f.ref)) continue;
        const bw = (f.bbox[2] - f.bbox[0]) * s, bh = (f.bbox[3] - f.bbox[1]) * s;
        let size = Math.min(bh * 0.5, (bw / Math.max(f.ref.length, 2)) * 1.5, 22);
        if (size < 7) continue;
        const [sx, sy] = this.toScreen((f.bbox[0] + f.bbox[2]) / 2, (f.bbox[1] + f.bbox[3]) / 2);
        if (sx < -50 || sy < -50 || sx > W + 50 || sy > H + 50) continue;
        ctx.font = `600 ${size.toFixed(1)}px ui-monospace, Consolas, monospace`;
        ctx.fillStyle = this.selected.has(f.ref) ? "#ffd400" : (f.side === (this.flip ? "B" : "F") ? "rgba(255,255,255,0.82)" : "rgba(180,190,210,0.45)");
        ctx.fillText(f.ref, sx, sy);
      }
    }
  }

  // ---------- picking & input -------------------------------------------------------
  pick(x, y) {
    // returns { ref, net } for the topmost thing under (x, y) in board mm
    const b = this.b;
    const tol = 3 / this.view.s;
    const order = [...this.copper];
    if (this.flip) order.reverse();
    if (this.active) order.unshift(this.active);
    const vis = order.filter((n) => this.visible.has(n));
    if (this.show.pads) {
      for (const f of b.footprints) {
        if (!this._fpShown(f)) continue;
        if (x < f.bbox[0] - tol || x > f.bbox[2] + tol || y < f.bbox[1] - tol || y > f.bbox[3] + tol) continue;
        for (const p of f.pads) {
          if (!p.layers.some((l) => this.visible.has(l))) continue;
          for (const rings of p.poly) if (pointInRing(x, y, rings[0])) return { ref: f.ref, net: p.net, pad: p.n };
        }
      }
    }
    if (this.show.vias) for (const v of b.vias) if (Math.hypot(x - v[0], y - v[1]) <= v[2] / 2 + tol) return { net: v[6] };
    if (this.show.tracks) {
      for (const ln of vis) {
        const a = b.tracks[ln];
        if (!a) continue;
        for (let i = 0; i < a.length; i += 6) if (distSeg(x, y, a[i], a[i + 1], a[i + 2], a[i + 3]) <= a[i + 4] / 2 + tol) return { net: a[i + 5] };
      }
    }
    // footprint body: prefer the smallest box containing the point, on the viewed side first
    let best = null, area = Infinity;
    for (const f of b.footprints) {
      if (!this._fpShown(f)) continue;
      const [x0, y0, x1, y1] = f.bbox;
      if (x < x0 || x > x1 || y < y0 || y > y1) continue;
      const ar = (x1 - x0) * (y1 - y0) * (f.side === (this.flip ? "B" : "F") ? 1 : 4);
      if (ar < area) { area = ar; best = f; }
    }
    return best ? { ref: best.ref, net: 0 } : null;
  }

  _bindInput() {
    const c = this.c;
    c.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = c.getBoundingClientRect();
      const sx = e.clientX - r.left, sy = e.clientY - r.top;
      const [wx, wy] = this.toWorld(sx, sy);
      const k = Math.exp(-e.deltaY * 0.0015);
      const s = Math.min(Math.max(this.view.s * k, 0.2), 2000);
      const fx = this.flip ? -1 : 1;
      this.view = { s, tx: sx - fx * s * wx, ty: sy - s * wy };
      this.render();
    }, { passive: false });

    c.addEventListener("pointerdown", (e) => {
      c.setPointerCapture(e.pointerId);
      const r = c.getBoundingClientRect();
      const sx = e.clientX - r.left, sy = e.clientY - r.top;
      const [wx, wy] = this.toWorld(sx, sy);
      const hit = e.button === 0 && this.b ? this.pick(wx, wy) : null;
      // dragging a part that is already selected moves it; anything else pans
      const moving = hit && hit.ref && this.selected.has(hit.ref) && this.selected.size === 1 && !e.shiftKey && this.opts.onMove;
      this.drag = { sx, sy, tx: this.view.tx, ty: this.view.ty, wx, wy, moved: false, hit, shift: e.shiftKey,
        ref: moving ? hit.ref : null, dx: 0, dy: 0 };
    });

    c.addEventListener("pointermove", (e) => {
      const r = c.getBoundingClientRect();
      const sx = e.clientX - r.left, sy = e.clientY - r.top;
      const [wx, wy] = this.toWorld(sx, sy);
      this.opts.onHover?.(wx, wy);
      const d = this.drag;
      if (!d) return;
      if (Math.hypot(sx - d.sx, sy - d.sy) > 4) d.moved = true;
      if (!d.moved) return;
      if (d.ref) {
        const grid = 0.05;
        d.dx = Math.round((wx - d.wx) / grid) * grid;
        d.dy = Math.round((wy - d.wy) / grid) * grid;
      } else {
        this.view.tx = d.tx + (sx - d.sx);
        this.view.ty = d.ty + (sy - d.sy);
      }
      this.render();
    });

    const end = () => {
      const d = this.drag;
      this.drag = null;
      if (!d) return;
      if (d.moved && d.ref) {
        const f = this.fpByRef.get(d.ref);
        if (d.dx || d.dy) this.opts.onMove?.(d.ref, +(f.c[0] + d.dx).toFixed(4), +(f.c[1] + d.dy).toFixed(4));
        this.render();
        return;
      }
      if (d.moved) return;
      const hit = d.hit;
      let refs = d.shift ? new Set(this.selected) : new Set();
      if (hit?.ref) d.shift && refs.has(hit.ref) ? refs.delete(hit.ref) : refs.add(hit.ref);
      this.selected = refs;
      this.net = hit ? hit.net || 0 : 0;
      this.render();
      this.opts.onSelect?.([...refs], this.net, hit);
    };
    c.addEventListener("pointerup", end);
    c.addEventListener("pointercancel", () => { this.drag = null; this.render(); });
    c.addEventListener("dblclick", () => this.fit());
  }
}
