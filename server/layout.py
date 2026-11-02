"""Phase 2: board outline, building a board from the netlist, and the automatic placer.

Everything here touches pcbnew, so it runs on the pcbnew thread (see `pcb()` in app.py).

Outline shapes: rectangle (optional corner radius), circle, oval (ellipse given by width and height).
KiCad has no ellipse primitive, so curves are drawn as short Edge.Cuts segments (96 per full turn).

The placer is simulated annealing over the top-side parts that are not locked. Its cost, all in mm:
  wirelength     sum over nets of the half-perimeter of the pins' bounding box (x weight per net)
  overlap        area where two parts' courtyards (grown by half the fab gap each) overlap
  outside        how far courtyard corners and side midpoints sit outside the outline (inset by the
                 fab copper-to-edge clearance)
  edge           connectors' distance from the board edge (they belong on it)
Overlap and outside are weighted up as the temperature falls, so the result ends legal when it can.
Routing feeds back through `feedback`: extra spacing around parts near unrouted or failed connections and
a heavier weight on the nets that did not route. KiCad DRC (courtyard and edge rules) is the independent
check of the result; these terms are only the placer's own view.
After the parts move, `place_references` moves each reference designator (text only) clear of pads,
silkscreen and the board edge.
Limits (v1): top side only, rotations in 90-degree steps, connector facing is not modelled.
"""
from __future__ import annotations

import math
import random
import re
import time
from pathlib import Path

import numpy as np
import pcbnew

from . import board as B
from . import fab, netlist

ORIGIN = (100.0, 100.0)  # board centre in KiCad coordinates (mm) for generated boards
POWER_NET = re.compile(r"^(GND\w*|AGND|DGND|PGND|VBUS|VIN\w*|VCC\w*|VDD\w*|VSYS|VBAT\w*|\+?\d+V\d*\w*|\+\w+)$", re.I)


# ---- outline ---------------------------------------------------------------------------------
def outline_points(shape: str, w: float, h: float, r: float = 0.0, cx: float = ORIGIN[0], cy: float = ORIGIN[1],
                   segs: int = 96) -> list[tuple[float, float]]:
    shape = shape.lower()
    if w <= 0 or (shape != "circle" and h <= 0):
        raise ValueError("width and height must be positive (mm)")
    if shape == "circle":
        h = w
    if shape in ("circle", "oval", "ellipse"):
        a, b = w / 2, h / 2
        return [(cx + a * math.cos(2 * math.pi * i / segs), cy + b * math.sin(2 * math.pi * i / segs)) for i in range(segs)]
    if shape not in ("rect", "rectangle"):
        raise ValueError("shape must be rect, circle or oval")
    r = max(0.0, min(r, w / 2, h / 2))
    x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
    if r < 0.01:
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    pts, n = [], max(4, segs // 8)
    for (ccx, ccy, a0) in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        for i in range(n + 1):
            t = math.radians(a0 + 90 * i / n)
            pts.append((ccx + r * math.cos(t), ccy + r * math.sin(t)))
    return pts


def _draw_outline(b, pts) -> None:
    for i in range(len(pts)):
        s = pcbnew.PCB_SHAPE(b)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(B.to_nm(0.05))
        (x0, y0), (x1, y1) = pts[i], pts[(i + 1) % len(pts)]
        s.SetStart(pcbnew.VECTOR2I(B.to_nm(x0), B.to_nm(y0)))
        s.SetEnd(pcbnew.VECTOR2I(B.to_nm(x1), B.to_nm(y1)))
        b.Add(s)


def set_outline(doc: B.BoardDoc, shape: str, w: float, h: float, r: float = 0.0) -> dict:
    """Replace the board's Edge.Cuts with a new outline centred where the old one was (or at ORIGIN)."""
    pts_old = board_outline(doc.board)
    if pts_old is not None:
        cx, cy = (pts_old[:, 0].min() + pts_old[:, 0].max()) / 2, (pts_old[:, 1].min() + pts_old[:, 1].max()) / 2
    else:
        cx, cy = ORIGIN
    pts = outline_points(shape, w, h, r, cx, cy)

    def fn(b):
        for d in list(b.GetDrawings()):
            if d.GetLayer() == pcbnew.Edge_Cuts:
                b.Remove(d)
        _draw_outline(b, pts)
        return {"outline": {"shape": shape, "width_mm": w, "height_mm": h, "corner_mm": r, "points": len(pts)}}
    return doc.edit(f"outline {shape} {w}x{h}", fn)


def board_outline(b) -> np.ndarray | None:
    ps = pcbnew.SHAPE_POLY_SET()
    if not b.GetBoardPolygonOutlines(ps, False) or ps.OutlineCount() == 0:
        return None
    ch = ps.Outline(0)
    if ch.PointCount() < 3:
        return None
    return np.array([[B.mm(ch.CPoint(i).x), B.mm(ch.CPoint(i).y)] for i in range(ch.PointCount())])


# ---- building a board from the netlist ----------------------------------------------------------
def _hide_field(fl) -> None:
    fl.SetVisible(False)
    fl.SetLayer(pcbnew.B_Fab if fl.GetParentFootprint().IsFlipped() else pcbnew.F_Fab)


def build_board(doc: B.BoardDoc, nl: dict, shape: str, w: float, h: float, r: float, profile_id: str) -> dict:
    """Create a fresh 2-layer board: outline, every footprint from the installed KiCad libraries, nets on
    pads, the fab profile's rules in a new .kicad_pro. Parts start packed in rows; run the placer next."""
    if not nl or not nl.get("ok"):
        raise B.EditError("the netlist has errors or is missing: fix it in Phase 1 first")
    prof = fab.get(profile_id)
    b = pcbnew.CreateEmptyBoard()
    b.SetCopperLayerCount(prof["layers"])
    pts = outline_points(shape, w, h, r)
    _draw_outline(b, pts)
    fps, missing = {}, []
    for p in nl["parts"]:
        lib, name = p["footprint"].split(":", 1)
        path = netlist.libraries().get(lib)
        f = pcbnew.FootprintLoad(path, name) if path else None
        if f is None:
            missing.append(f"{p['ref']}: {p['footprint']}")
            continue
        f.SetReference(p["ref"])
        f.SetValue(p.get("value") or name)
        f.SetFPID(pcbnew.LIB_ID(lib, name))
        if p.get("lcsc"):
            f.SetField("LCSC", p["lcsc"])
            _hide_field(f.GetField("LCSC"))  # a new field defaults to visible silkscreen at the part's centre
        b.Add(f)
        fps[p["ref"]] = f
    if missing:
        raise B.EditError("footprints failed to load: " + "; ".join(missing))
    for n in nl["nets"]:
        ni = pcbnew.NETINFO_ITEM(b, n["name"])
        b.Add(ni)
        for ref, pin in n["nodes"]:
            for q in fps[ref].Pads():
                if q.GetNumber() == pin:
                    q.SetNet(ni)
    _pack_rows(list(fps.values()), np.array(pts))
    power = sorted({n["name"] for n in nl["nets"] if POWER_NET.match(n["name"])})
    doc.replace(f"build board from netlist ({len(fps)} parts)", b,
                after_save=lambda path: fab.write_pro(Path(path), prof, power))
    return {"rev": doc.rev, "parts": len(fps), "nets": len(nl["nets"]), "power_nets": power, "fab_profile": prof["id"],
            "outline": {"shape": shape, "width_mm": w, "height_mm": h, "corner_mm": r},
            "note": "Parts are packed in rows as a start; run the placer."}


def _crtyd_box(f) -> tuple[float, float, float, float]:
    """Courtyard bbox relative to the footprint anchor (falls back to pads+body without text)."""
    pos = f.GetPosition()
    c = f.GetCourtyard(pcbnew.B_CrtYd if f.IsFlipped() else pcbnew.F_CrtYd)
    bb = c.BBox() if c.OutlineCount() else f.GetBoundingBox(False)
    return (B.mm(bb.GetLeft() - pos.x), B.mm(bb.GetTop() - pos.y), B.mm(bb.GetRight() - pos.x), B.mm(bb.GetBottom() - pos.y))


def _pack_rows(fps, pts: np.ndarray) -> None:
    x0, y0 = pts[:, 0].min(), pts[:, 1].min()
    x1 = pts[:, 0].max()
    width = x1 - x0
    order = sorted(fps, key=lambda f: -((_crtyd_box(f)[2] - _crtyd_box(f)[0]) * (_crtyd_box(f)[3] - _crtyd_box(f)[1])))
    x, y, row_h = x0 + 1, y0 + 1, 0.0
    for f in order:
        bx = _crtyd_box(f)
        fw, fh = bx[2] - bx[0], bx[3] - bx[1]
        if x + fw > x0 + width - 1 and x > x0 + 1:
            x, y, row_h = x0 + 1, y + row_h + 0.5, 0.0
        f.SetPosition(pcbnew.VECTOR2I(B.to_nm(x - bx[0]), B.to_nm(y - bx[1])))
        x += fw + 0.5
        row_h = max(row_h, fh)


# ---- the placer --------------------------------------------------------------------------------
def _is_edge_part(f) -> bool:
    fpid = f.GetFPIDAsString()
    return f.GetReference().startswith("J") or "Connector" in fpid or "USB" in fpid.upper()


def _model(b, edge_refs: set, fixed_refs: set):
    fps = list(b.GetFootprints())
    refs = [f.GetReference() for f in fps]
    n = len(fps)
    pos = np.zeros((n, 2))
    rot = np.zeros(n, dtype=int)
    movable = np.zeros(n, dtype=bool)
    edge = np.zeros(n, dtype=bool)
    boxes = np.zeros((n, 4, 4))  # [part, rotation k, (x0,y0,x1,y1)] relative to the anchor
    front = np.full((n, 4), -1, dtype=int)  # side the body overhangs its pads (a connector's mouth): 4 top 5 bottom 6 left 7 right
    pad_off: list[np.ndarray] = []  # per part: (4, pads, 2)
    pad_index: list[dict] = []
    for i, f in enumerate(fps):
        p0, o0 = f.GetPosition(), f.GetOrientationDegrees()
        pos[i] = (B.mm(p0.x), B.mm(p0.y))
        ok_rot = abs(o0 / 90 - round(o0 / 90)) < 1e-6
        rot[i] = int(round(o0 / 90)) % 4 if ok_rot else 0
        movable[i] = (not f.IsLocked()) and (not f.IsFlipped()) and ok_rot and refs[i] not in fixed_refs
        edge[i] = refs[i] in edge_refs or (not edge_refs and _is_edge_part(f))
        pads = list(f.Pads())
        pad_index.append({})
        offs = np.zeros((4, len(pads), 2))
        f.SetPosition(pcbnew.VECTOR2I(0, 0))
        for k in range(4):
            f.SetOrientationDegrees(90 * k if ok_rot else o0)
            boxes[i, k] = _crtyd_box(f)
            for j, q in enumerate(pads):
                qp = q.GetPosition()
                offs[k, j] = (B.mm(qp.x), B.mm(qp.y))
            if len(pads):
                bx = boxes[i, k]
                v = np.array([(bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2]) - (offs[k].max(0) + offs[k].min(0)) / 2
                if np.abs(v).max() > 0.5:
                    front[i, k] = (5 if v[1] > 0 else 4) if abs(v[1]) >= abs(v[0]) else (7 if v[0] > 0 else 6)
        for j, q in enumerate(pads):
            pad_index[i].setdefault(q.GetNetname(), []).append(j)
        f.SetOrientationDegrees(o0)
        f.SetPosition(p0)
        pad_off.append(offs)
    nets: dict[str, list[tuple[int, int]]] = {}
    for i in range(n):
        for name, js in pad_index[i].items():
            if name:
                nets.setdefault(name, []).extend((i, j) for j in js)
    nets = {k: v for k, v in nets.items() if len({c for c, _ in v}) > 1}
    return fps, refs, pos, rot, movable, edge, boxes, pad_off, nets, front


def _poly_dist(pts: np.ndarray, poly: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """For points (m,2): distance to the polygon boundary and an inside flag."""
    a = poly
    bq = np.roll(poly, -1, axis=0)
    ab = bq - a
    ap = pts[:, None, :] - a[None, :, :]
    t = np.clip((ap * ab).sum(-1) / np.maximum((ab * ab).sum(-1), 1e-12), 0, 1)
    proj = a[None] + t[..., None] * ab[None]
    d = np.sqrt(((pts[:, None, :] - proj) ** 2).sum(-1)).min(1)
    x, y = pts[:, 0:1], pts[:, 1:2]
    ay, by = a[None, :, 1], bq[None, :, 1]
    cond = (ay > y) != (by > y)
    xint = a[None, :, 0] + (y - ay) * (bq[None, :, 0] - a[None, :, 0]) / np.where(by - ay == 0, 1e-12, by - ay)
    inside = (cond & (x < xint)).sum(1) % 2 == 1
    return d, inside


class Placer:
    def __init__(self, b, prof: dict, edge_refs=(), fixed_refs=(), extra_gap: dict | None = None,
                 net_weight: dict | None = None):
        (self.fps, self.refs, self.pos, self.rot, self.movable, self.edge, self.boxes, self.pad_off,
         self.nets, self.front) = _model(b, set(edge_refs), set(fixed_refs))
        self.poly = board_outline(b)
        if self.poly is None:
            raise B.EditError("the board has no closed outline on Edge.Cuts: set one first")
        self.n = len(self.refs)
        gap = prof["courtyard_gap"] / 2
        self.grow = np.array([gap + (extra_gap or {}).get(r, 0.0) for r in self.refs])
        self.edge_clear = prof["copper_edge_clearance"] + 0.1
        self.net_names = list(self.nets)
        self.net_w = np.array([(net_weight or {}).get(k, 1.0) for k in self.net_names])
        self.net_comp = [np.array([c for c, _ in self.nets[k]]) for k in self.net_names]
        self.net_pad = [np.array([j for _, j in self.nets[k]]) for k in self.net_names]
        self.comp_nets = [[] for _ in range(self.n)]
        for ni, cs in enumerate(self.net_comp):
            for c in set(cs.tolist()):
                self.comp_nets[c].append(ni)
        self.w_ov, self.w_out, self.w_edge = 2.0, 10.0, 3.0
        self.rng = random.Random(1)

    # -- geometry helpers --
    def abs_box(self, i, pos=None, rot=None):
        p = self.pos[i] if pos is None else pos
        k = self.rot[i] if rot is None else rot
        bx = self.boxes[i, k]
        g = self.grow[i]
        return np.array([p[0] + bx[0] - g, p[1] + bx[1] - g, p[0] + bx[2] + g, p[1] + bx[3] + g])

    def all_boxes(self):
        bx = self.boxes[np.arange(self.n), self.rot]
        g = self.grow[:, None]
        return np.concatenate([self.pos + bx[:, :2] - g, self.pos + bx[:, 2:] + g], axis=1)

    def net_len(self, ni):
        cs, js = self.net_comp[ni], self.net_pad[ni]
        pts = self.pos[cs] + np.array([self.pad_off[c][self.rot[c], j] for c, j in zip(cs, js)])
        return (pts.max(0) - pts.min(0)).sum()

    def _overlap_of(self, i, box, allb):
        ov_w = np.minimum(box[2], allb[:, 2]) - np.maximum(box[0], allb[:, 0])
        ov_h = np.minimum(box[3], allb[:, 3]) - np.maximum(box[1], allb[:, 1])
        a = np.clip(ov_w, 0, None) * np.clip(ov_h, 0, None)
        a[i] = 0
        return a

    def _outside_of(self, box, i=None):
        x0, y0, x1, y1 = box
        e = self.edge_clear
        x0, y0, x1, y1 = x0 - e, y0 - e, x1 + e, y1 + e
        pts = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1], [(x0 + x1) / 2, y0], [(x0 + x1) / 2, y1],
                        [x0, (y0 + y1) / 2], [x1, (y0 + y1) / 2]])
        d, inside = _poly_dist(pts, self.poly)
        f = self.front[i, self.rot[i]] if i is not None else -1
        return float(d[~inside].sum()), float(d[f] if f >= 0 else d[4:].min())

    # -- cost --
    def terms(self) -> dict:
        allb = self.all_boxes()
        wl = float(sum(self.net_w[k] * self.net_len(k) for k in range(len(self.net_names))))
        ov = 0.0
        out = 0.0
        edge = 0.0
        for i in range(self.n):
            ov += float(self._overlap_of(i, allb[i], allb)[i + 1:].sum())
            o, dmin = self._outside_of(allb[i], i)
            out += o
            if self.edge[i]:
                edge += dmin
        return {"wirelength_mm": round(wl, 2), "overlap_mm2": round(ov, 3), "outside_mm": round(out, 3),
                "edge_mm": round(edge, 2)}

    def _part_cost(self, idx: list[int], allb) -> float:
        nets = set()
        for i in idx:
            nets.update(self.comp_nets[i])
        c = sum(self.net_w[k] * self.net_len(k) for k in nets)
        seen = set()
        for i in idx:
            a = self._overlap_of(i, allb[i], allb)
            for j in seen:
                a[j] = 0  # count each moved pair once
            c += self.w_ov * float(a.sum())
            seen.add(i)
            o, dmin = self._outside_of(allb[i], i)
            c += self.w_out * o
            if self.edge[i]:
                c += self.w_edge * dmin
        return c

    def run(self, moves: int = 20000, t_start: float | None = None, seed: int = 1, deadline_s: float = 60) -> dict:
        self.rng = random.Random(seed)
        mv = np.flatnonzero(self.movable)
        if len(mv) == 0:
            raise B.EditError("no movable parts (all locked, on the bottom side, or at non-90-degree angles)")
        before = self.terms()
        t0 = time.time()
        span = float(max(np.ptp(self.poly[:, 0]), np.ptp(self.poly[:, 1])))
        allb = self.all_boxes()
        if t_start is None:  # sample typical |delta| of random moves
            ds = []
            for _ in range(60):
                i = int(self.rng.choice(mv))
                old = self.pos[i].copy()
                c0 = self._part_cost([i], allb)
                self.pos[i] = old + np.array([self.rng.uniform(-1, 1), self.rng.uniform(-1, 1)]) * span * 0.2
                allb[i] = self.abs_box(i)
                ds.append(abs(self._part_cost([i], allb) - c0))
                self.pos[i] = old
                allb[i] = self.abs_box(i)
            t_start = float(np.mean(ds)) or 1.0
        t_end = t_start * 1e-4
        accepted = 0
        done = 0
        for m in range(moves):
            frac = m / moves
            temp = t_start * (t_end / t_start) ** frac
            self.w_ov, self.w_out = 2.0 + 998.0 * frac ** 3, 10.0 + 1990.0 * frac ** 3
            r = self.rng.random()
            i = int(self.rng.choice(mv))
            if r < 0.15 and len(mv) > 1:  # swap two parts
                j = int(self.rng.choice(mv))
                if j == i:
                    continue
                idx = [i, j]
                old = (self.pos[i].copy(), self.pos[j].copy())
                c0 = self._part_cost(idx, allb)
                self.pos[i], self.pos[j] = old[1].copy(), old[0].copy()
                undo = lambda: (self.pos.__setitem__(i, old[0]), self.pos.__setitem__(j, old[1]))
            elif r < 0.30:  # rotate 90
                idx = [i]
                old_r = self.rot[i]
                c0 = self._part_cost(idx, allb)
                self.rot[i] = (old_r + self.rng.choice((1, 2, 3))) % 4
                undo = lambda: self.rot.__setitem__(i, old_r)
            else:  # shift
                idx = [i]
                old = self.pos[i].copy()
                c0 = self._part_cost(idx, allb)
                step = max(0.2, span * 0.35 * math.sqrt(temp / t_start))
                self.pos[i] = old + np.array([self.rng.gauss(0, step), self.rng.gauss(0, step)])
                undo = lambda: self.pos.__setitem__(i, old)
            for k in idx:
                allb[k] = self.abs_box(k)
            d = self._part_cost(idx, allb) - c0
            done += 1
            if d <= 0 or self.rng.random() < math.exp(-d / max(temp, 1e-12)):
                accepted += 1
            else:
                undo()
                for k in idx:
                    allb[k] = self.abs_box(k)
            if time.time() - t0 > deadline_s:
                break
        self.pos[mv] = np.round(self.pos[mv] / 0.05) * 0.05  # 0.05 mm grid
        after = self.terms()
        x, y = self.poly[:, 0], self.poly[:, 1]
        board_area = 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))
        bx = self.boxes[np.arange(self.n), self.rot]
        util = float(((bx[:, 2] - bx[:, 0]) * (bx[:, 3] - bx[:, 1])).sum()) / board_area if board_area else 0.0
        return {"before": before, "after": after, "moves": done, "accepted": accepted, "seconds": round(time.time() - t0, 1),
                "movable": int(len(mv)), "fixed": int(self.n - len(mv)), "seed": seed,
                "edge_parts": [self.refs[i] for i in np.flatnonzero(self.edge)],
                "utilization": round(util, 2),
                "warning": ("courtyards fill %d%% of the board: above ~50%% a 2-layer board is usually too small to "
                            "place and route cleanly" % round(util * 100)) if util > 0.5 else ""}

    def apply(self, b) -> list[str]:
        moved = []
        for i in np.flatnonzero(self.movable):
            f = self.fps[i]
            np_ = pcbnew.VECTOR2I(B.to_nm(self.pos[i][0]), B.to_nm(self.pos[i][1]))
            if f.GetPosition() != np_ or int(round(f.GetOrientationDegrees() / 90)) % 4 != self.rot[i]:
                f.SetOrientationDegrees(90 * int(self.rot[i]))
                f.SetPosition(np_)
                moved.append(self.refs[i])
        return moved


SILK_MARGIN = 0.15  # mm kept between a reference designator and pads, other silkscreen and the board edge


def _box(bb) -> tuple[float, float, float, float]:
    return (B.mm(bb.GetLeft()), B.mm(bb.GetTop()), B.mm(bb.GetRight()), B.mm(bb.GetBottom()))


def _overlap(a, b, m=0.0) -> float:
    w = min(a[2], b[2] + m) - max(a[0], b[0] - m)
    h = min(a[3], b[3] + m) - max(a[1], b[1] - m)
    return w * h if w > 0 and h > 0 else 0.0


def place_references(b) -> dict:
    """Move each part's reference designator (only the text, never the part) to the spot around its courtyard
    that is clear of every pad, footprint silkscreen, other references and the board edge. KiCad leaves
    references where the library put them, which after placement gives most of a board's silk_* DRC warnings.
    Text is not shrunk (no cited minimum height for the fab); references with no clear spot are reported."""
    silk_layers = (pcbnew.F_SilkS, pcbnew.B_SilkS)
    hidden = []
    for f in b.GetFootprints():  # boards built before the fix printed the LCSC number on the silkscreen
        for fl in f.GetFields():
            if fl.GetName() == "LCSC" and fl.IsVisible() and fl.GetLayer() in silk_layers:
                _hide_field(fl)
                hidden.append(f.GetReference())
    obstacles = []  # pads and footprint silk graphics
    for f in b.GetFootprints():
        for p in f.Pads():
            obstacles.append(_box(p.GetBoundingBox()))
        for g in f.GraphicalItems():
            if g.GetLayer() in silk_layers and g.GetClass() == "PCB_SHAPE":
                obstacles.append(_box(g.GetBoundingBox()))
    poly = board_outline(b)
    placed, moved, unresolved = [], [], []

    def cost(box):
        c = sum(_overlap(box, o, SILK_MARGIN) for o in obstacles) + sum(_overlap(box, o, SILK_MARGIN) for o in placed)
        if poly is not None:
            pts = np.array([[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]])
            d, inside = _poly_dist(pts, poly)
            c += float(np.sum(np.where(inside, np.maximum(0.0, SILK_MARGIN - d), d + SILK_MARGIN))) * 10
        return c

    fps = sorted(b.GetFootprints(), key=lambda f: -(lambda c: (c[2] - c[0]) * (c[3] - c[1]))(_crtyd_box(f)))
    for f in fps:
        t = f.Reference()
        if not t.IsVisible() or t.GetLayer() not in silk_layers or f.IsLocked():
            continue
        cb = _crtyd_box(f)
        px, py = B.mm(f.GetPosition().x), B.mm(f.GetPosition().y)
        cx0, cy0, cx1, cy1 = px + cb[0], py + cb[1], px + cb[2], py + cb[3]
        mx, my = (cx0 + cx1) / 2, (cy0 + cy1) / 2
        start = (t.GetPosition(), t.GetTextAngleDegrees())
        best = (cost(_box(t.GetBoundingBox())), 0.0, start)
        for ang in (0.0, 90.0):
            t.SetTextAngleDegrees(ang)
            bb = _box(t.GetBoundingBox())
            hw, hh = (bb[2] - bb[0]) / 2, (bb[3] - bb[1]) / 2
            for gap in (SILK_MARGIN + 0.05, SILK_MARGIN + 0.6):
                for x, y in ((mx, cy0 - hh - gap), (mx, cy1 + hh + gap), (cx0 - hw - gap, my), (cx1 + hw + gap, my),
                             (cx0 - hw - gap, cy0 - hh - gap), (cx1 + hw + gap, cy0 - hh - gap),
                             (cx0 - hw - gap, cy1 + hh + gap), (cx1 + hw + gap, cy1 + hh + gap)):
                    pos = pcbnew.VECTOR2I(B.to_nm(x), B.to_nm(y))
                    t.SetPosition(pos)
                    # small preference for upright text and for staying close to the part
                    c = cost(_box(t.GetBoundingBox()))
                    tie = (0.01 if ang else 0.0) + 0.001 * math.hypot(x - mx, y - my)
                    if (c, tie) < best[:2]:
                        best = (c, tie, (pos, ang))
        t.SetPosition(best[2][0])
        t.SetTextAngleDegrees(best[2][1])
        if best[2] != start:
            moved.append(f.GetReference())
        if best[0] > 1e-6:
            unresolved.append(f.GetReference())
        placed.append(_box(t.GetBoundingBox()))
    return {"moved": moved, "unresolved": unresolved, "lcsc_text_hidden": hidden}


def place(doc: B.BoardDoc, profile_id: str, moves: int = 20000, seed: int = 1, refine: bool = False,
          edge_refs=(), fixed_refs=(), extra_gap=None, net_weight=None) -> dict:
    """Run the placer on the open board and save the result (undoable)."""
    prof = fab.get(profile_id)

    def fn(b):
        pl = Placer(b, prof, edge_refs, fixed_refs, extra_gap, net_weight)
        res = pl.run(moves=moves, seed=seed, t_start=(None if not refine else 0.5))
        res["moved"] = pl.apply(b)
        res["references"] = place_references(b)
        return res
    return doc.edit(f"auto-place ({'refine' if refine else 'full'}, seed {seed})", fn)


def current_terms(doc: B.BoardDoc, profile_id: str) -> dict:
    """The placer's view of the board as it stands (no changes)."""
    pl = Placer(doc.board, fab.get(profile_id))
    return pl.terms()


def feedback(drc_result: dict, base_gap: dict | None = None, base_w: dict | None = None) -> tuple[dict, dict, dict]:
    """Turn a routed candidate's DRC into placement hints: extra spacing around parts involved in
    unrouted items or errors, and heavier weights on unrouted nets (shorter = easier to route)."""
    gap = dict(base_gap or {})
    w = dict(base_w or {})
    refs_hit, nets_hit = {}, {}
    for it in drc_result.get("items", []):
        if it["kind"] == "unconnected" or it["severity"] == "error":
            for loc in it["items"]:
                t = loc.get("text", "")
                for ref in re.findall(r"(?:\bof|^Footprint) ([A-Za-z]+\d+[A-Za-z0-9]*)", t):
                    refs_hit[ref] = refs_hit.get(ref, 0) + 1
                if it["kind"] == "unconnected":
                    for net in re.findall(r"\[([^\]]+)\]", t):
                        nets_hit[net] = nets_hit.get(net, 0) + 1
    for ref in refs_hit:
        gap[ref] = min(gap.get(ref, 0.0) + 0.3, 1.5)
    for net in nets_hit:
        w[net] = min(w.get(net, 1.0) * 1.5, 6.0)
    return gap, w, {"refs": refs_hit, "nets": nets_hit}
