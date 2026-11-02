"""A live KiCad board document backed by KiCad's own pcbnew.

pcbnew is not thread safe: the server calls everything in this module from one
dedicated worker thread (see `pcb()` in app.py). Coordinates leave this module in mm.

Every edit snapshots the file on disk first, mutates the in-memory BOARD, saves, and
bumps `rev`. The viewer refetches on `rev`, and undo restores the last snapshot.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import time

import pcbnew

NM = 1e-6  # pcbnew internal unit is the nanometre


def mm(v: float) -> float:
    return round(v * NM, 4)


def to_nm(v_mm: float) -> int:
    return int(round(v_mm * 1e6))


def _pt(p) -> list[float]:
    return [mm(p.x), mm(p.y)]


def _chain(ch) -> list[float]:
    """SHAPE_LINE_CHAIN -> flat [x0, y0, x1, y1, ...]."""
    out: list[float] = []
    for i in range(ch.PointCount()):
        p = ch.CPoint(i)
        out += [mm(p.x), mm(p.y)]
    return out


def _polyset(ps) -> list[list[list[float]]]:
    """SHAPE_POLY_SET -> [[outline, hole, hole...], ...]."""
    polys = []
    for i in range(ps.OutlineCount()):
        rings = [_chain(ps.Outline(i))]
        for j in range(ps.HoleCount(i)):
            rings.append(_chain(ps.Hole(i, j)))
        polys.append(rings)
    return polys


def _arc_points(start, mid, end, segs: int = 16) -> list[float]:
    """Sample a three-point arc into a polyline."""
    ax, ay, bx, by, cx, cy = start.x, start.y, mid.x, mid.y, end.x, end.y
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return [mm(ax), mm(ay), mm(cx), mm(cy)]
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0, am, a1 = (math.atan2(y - uy, x - ux) for x, y in ((ax, ay), (bx, by), (cx, cy)))

    def ccw_span(a, b):
        return (b - a) % (2 * math.pi)

    span = ccw_span(a0, a1)
    if ccw_span(a0, am) > span:  # mid is not on the ccw path, so go clockwise
        span -= 2 * math.pi
    out: list[float] = []
    for k in range(segs + 1):
        a = a0 + span * k / segs
        out += [mm(ux + r * math.cos(a)), mm(uy + r * math.sin(a))]
    return out


def _shape_geom(s) -> dict | None:
    """PCB_SHAPE / footprint graphic -> {"k": kind, ...} in mm."""
    t = s.GetShape()
    w = mm(s.GetWidth())
    if t == pcbnew.SHAPE_T_SEGMENT:
        return {"k": "line", "p": _pt(s.GetStart()) + _pt(s.GetEnd()), "w": w}
    if t == pcbnew.SHAPE_T_RECTANGLE:
        a, b = s.GetStart(), s.GetEnd()
        pts = [a.x, a.y, b.x, a.y, b.x, b.y, a.x, b.y, a.x, a.y]
        return {"k": "line", "p": [mm(v) for v in pts], "w": w, "fill": bool(s.IsSolidFill())}
    if t == pcbnew.SHAPE_T_CIRCLE:
        return {"k": "circle", "c": _pt(s.GetCenter()), "r": mm(s.GetRadius()), "w": w, "fill": bool(s.IsSolidFill())}
    if t == pcbnew.SHAPE_T_ARC:
        return {"k": "line", "p": _arc_points(s.GetStart(), s.GetArcMid(), s.GetEnd()), "w": w}
    if t == pcbnew.SHAPE_T_POLY:
        return {"k": "poly", "polys": _polyset(s.GetPolyShape()), "w": w, "fill": bool(s.IsSolidFill())}
    if t == pcbnew.SHAPE_T_BEZIER:
        try:
            s.RebuildBezierToSegmentsPointsList(s.GetWidth())
            flat: list[float] = []
            for p in s.GetBezierPoints():
                flat += _pt(p)
            return {"k": "line", "p": flat, "w": w}
        except Exception:
            return {"k": "line", "p": _pt(s.GetStart()) + _pt(s.GetEnd()), "w": w}
    return None


def _fields(fp) -> dict:
    out = {}
    for f in fp.GetFields():
        name = f.GetName()
        if name in ("Reference", "Value", "Footprint"):
            continue
        if f.GetText():
            out[name] = f.GetText()
    return out


class EditError(Exception):
    """A tool asked for something the board can't do (unknown ref, bad value...)."""


class BoardDoc:
    """One open .kicad_pcb inside a project workspace."""

    def __init__(self, path: str, snapshot_dir: str):
        self.path = os.path.abspath(path)
        self.snapshot_dir = snapshot_dir
        self.rev = 0
        self.undo: list[tuple[str, str]] = []  # (snapshot file, label)
        self._data: dict | None = None
        self._data_rev = -1
        self._reload()

    # ---- loading -------------------------------------------------------------------
    def _reload(self) -> None:
        self.board = pcbnew.LoadBoard(self.path)
        self.mtime = os.path.getmtime(self.path)
        self.rev += 1

    def sync_from_disk(self) -> bool:
        """Pick up edits made outside the app (e.g. in KiCad). True if reloaded."""
        if os.path.getmtime(self.path) != self.mtime:
            self._reload()
            return True
        return False

    def data(self) -> dict:
        self.sync_from_disk()
        if self._data_rev != self.rev:
            self._data = extract(self.board, self.path)
            self._data["rev"] = self.rev
            self._data["can_undo"] = bool(self.undo)
            self._data_rev = self.rev
        return self._data

    # ---- edits -----------------------------------------------------------------------
    def _snapshot(self, label: str) -> None:
        os.makedirs(self.snapshot_dir, exist_ok=True)
        snap = os.path.join(self.snapshot_dir, f"{time.strftime('%Y%m%d-%H%M%S')}_r{self.rev:04d}.kicad_pcb")
        shutil.copy2(self.path, snap)
        self.undo.append((snap, label))

    def _commit(self) -> None:
        pcbnew.SaveBoard(self.path, self.board)
        self.mtime = os.path.getmtime(self.path)
        self.rev += 1

    def edit(self, label: str, fn) -> dict:
        """Snapshot, run fn(board) -> result dict, save, bump rev."""
        self.sync_from_disk()
        self._snapshot(label)
        try:
            result = fn(self.board)
        except Exception:
            # roll back the in-memory board to the file we just snapshotted
            self.undo.pop()
            self._reload()
            raise
        self._commit()
        return {"rev": self.rev, **(result or {})}

    def replace(self, label: str, new_board, after_save=None) -> dict:
        """Swap in a whole new BOARD (built from a netlist, or a routed candidate). Undoable.
        after_save(path) runs before the reload, e.g. to write the .kicad_pro the board needs."""
        if os.path.exists(self.path):
            self._snapshot(label)
        pcbnew.SaveBoard(self.path, new_board)
        if after_save:
            after_save(self.path)
        self._reload()
        return {"rev": self.rev}

    def adopt_file(self, label: str, src: str) -> dict:
        """Replace the board with a finished .kicad_pcb file (snapshot first, so undo works)."""
        self._snapshot(label)
        shutil.copy2(src, self.path)
        self._reload()
        return {"rev": self.rev}

    def undo_last(self) -> dict:
        if not self.undo:
            raise EditError("nothing to undo")
        snap, label = self.undo.pop()
        shutil.copy2(snap, self.path)
        self._reload()
        return {"rev": self.rev, "undone": label}

    # ---- lookups ---------------------------------------------------------------------
    def fp(self, ref: str):
        f = self.board.FindFootprintByReference(ref)
        if f is None:
            raise EditError(f"no footprint with reference {ref!r}")
        return f

    def layer_name(self, lid: int) -> str:
        return self.board.GetLayerName(lid)


# ---- extraction for the viewer ----------------------------------------------------------
def extract(board, path: str) -> dict:
    enabled = board.GetEnabledLayers()
    copper = list(enabled.CuStack())
    others = [l for l in enabled.Seq() if l not in copper]
    names = {lid: board.GetLayerName(lid) for lid in copper + others}
    layers = [{"id": lid, "name": names[lid], "copper": lid in copper} for lid in copper + others]

    nets = [""]  # index 0 = no net
    net_index: dict[str, int] = {"": 0}

    def ni(name: str) -> int:
        if name not in net_index:
            net_index[name] = len(nets)
            nets.append(name)
        return net_index[name]

    # tracks: per layer flat arrays [x1, y1, x2, y2, w, net], arcs pre-sampled into segments
    tracks: dict[str, list[float]] = {}
    vias: list[list] = []
    for t in board.GetTracks():
        cls = t.GetClass()
        n = ni(t.GetNetname())
        if cls == "PCB_VIA":
            top, bot = t.TopLayer(), t.BottomLayer()
            try:
                dia = t.GetWidth(top)
            except TypeError:
                dia = t.GetWidth()
            p = t.GetPosition()
            vias.append([mm(p.x), mm(p.y), mm(dia), mm(t.GetDrillValue()), names.get(top, ""), names.get(bot, ""), n])
            continue
        arr = tracks.setdefault(names.get(t.GetLayer(), str(t.GetLayer())), [])
        w = mm(t.GetWidth())
        if cls == "PCB_ARC":
            pts = _arc_points(t.GetStart(), t.GetMid(), t.GetEnd(), 8)
            for k in range(0, len(pts) - 2, 2):
                arr += [pts[k], pts[k + 1], pts[k + 2], pts[k + 3], w, n]
        else:
            s, e = t.GetStart(), t.GetEnd()
            arr += [mm(s.x), mm(s.y), mm(e.x), mm(e.y), w, n]

    zones: list[dict] = []
    for z in board.Zones():
        zlayers = [names.get(l, "") for l in z.GetLayerSet().Seq() if l in names]
        if z.GetIsRuleArea():
            zones.append({"rule": True, "name": z.GetZoneName(), "layers": zlayers, "outline": _polyset(z.Outline())})
            continue
        n = ni(z.GetNetname())
        for lid in z.GetLayerSet().Seq():
            if lid not in copper:
                continue
            polys = _polyset(z.GetFilledPolysList(lid)) if z.IsFilled() else []
            zones.append({"net": n, "layer": names[lid], "name": z.GetZoneName(), "polys": polys, "outline": _polyset(z.Outline())})

    drawings: dict[str, list] = {}
    for d in board.GetDrawings():
        if d.GetClass() == "PCB_SHAPE":
            g = _shape_geom(d)
            if g:
                drawings.setdefault(names.get(d.GetLayer(), ""), []).append(g)

    fps: list[dict] = []
    for fp in board.GetFootprints():
        side = "B" if fp.IsFlipped() else "F"
        pads = []
        for p in fp.Pads():
            cu_ids = [l for l in p.GetLayerSet().CuStack() if l in names]
            shape = []
            if cu_ids:
                try:
                    shape = _polyset(p.GetEffectivePolygon(cu_ids[0], pcbnew.ERROR_INSIDE))
                except Exception:
                    shape = []
            pads.append({
                "n": p.GetNumber(), "net": ni(p.GetNetname()), "c": _pt(p.GetPosition()),
                "layers": [names[l] for l in cu_ids], "poly": shape,
                "drill": mm(p.GetDrillSize().x) if p.GetDrillSize().x else 0,
            })
        graphics: dict[str, list] = {}
        for g in fp.GraphicalItems():
            if g.GetClass() == "PCB_SHAPE":
                geo = _shape_geom(g)
                if geo:
                    graphics.setdefault(names.get(g.GetLayer(), ""), []).append(geo)
        bb = fp.GetBoundingBox(False)
        ref = fp.Reference()
        fps.append({
            "ref": fp.GetReference(), "value": fp.GetValue(), "fpid": fp.GetFPIDAsString(),
            "side": side, "c": _pt(fp.GetPosition()), "rot": round(fp.GetOrientationDegrees(), 3),
            "bbox": [mm(bb.GetLeft()), mm(bb.GetTop()), mm(bb.GetRight()), mm(bb.GetBottom())],
            "refpos": _pt(ref.GetTextPos()), "refsize": mm(ref.GetTextHeight()), "refvis": bool(ref.IsVisible()),
            "dnp": bool(fp.IsDNP()), "fields": _fields(fp), "pads": pads, "graphics": graphics,
        })

    bb = board.GetBoardEdgesBoundingBox()
    return {
        "path": path, "name": os.path.basename(path),
        "bbox": [mm(bb.GetLeft()), mm(bb.GetTop()), mm(bb.GetRight()), mm(bb.GetBottom())],
        "layers": layers, "nets": nets, "tracks": tracks, "vias": vias, "zones": zones,
        "drawings": drawings, "footprints": fps,
        "stats": {"footprints": len(fps), "nets": len(nets) - 1, "vias": len(vias),
                  "segments": sum(len(v) // 6 for v in tracks.values()), "copper_layers": len(copper)},
    }


# ---- queries used by the assistant's tools ----------------------------------------------
def summary(doc: BoardDoc) -> dict:
    b = doc.board
    d = doc.data()
    b.BuildConnectivity()
    ns = b.GetDesignSettings().m_NetSettings
    classes = {}
    for name, nc in [("Default", ns.GetDefaultNetclass())] + list(ns.GetNetclasses().items()):
        classes[str(name)] = {"track_mm": mm(nc.GetTrackWidth()), "clearance_mm": mm(nc.GetClearance()),
                              "via_mm": mm(nc.GetViaDiameter()), "via_drill_mm": mm(nc.GetViaDrill())}
    x0, y0, x1, y1 = d["bbox"]
    return {
        "file": d["name"], "revision": doc.rev,
        "board_size_mm": [round(x1 - x0, 3), round(y1 - y0, 3)], "origin_mm": [x0, y0],
        "copper_layers": [l["name"] for l in d["layers"] if l["copper"]],
        **d["stats"],
        "unrouted_connections": b.GetConnectivity().GetUnconnectedCount(True),
        "netclasses": classes,
        "zones": sorted({f"{z['name'] or '(unnamed)'} [{d['nets'][z['net']]}] on {z['layer']}" for z in d["zones"] if not z.get("rule")}),
        "can_undo": bool(doc.undo),
    }


def find_components(doc: BoardDoc, query: str = "", limit: int = 50) -> dict:
    rx = re.compile(query, re.I) if query else None
    hits = []
    for f in doc.data()["footprints"]:
        hay = f"{f['ref']} {f['value']} {f['fpid']} " + " ".join(f"{k}={v}" for k, v in f["fields"].items())
        if rx is None or rx.search(hay):
            hits.append({"ref": f["ref"], "value": f["value"], "footprint": f["fpid"], "side": f["side"],
                         "x_mm": f["c"][0], "y_mm": f["c"][1], "rot_deg": f["rot"], "pads": len(f["pads"]), "dnp": f["dnp"]})
    total = len(hits)
    return {"total": total, "shown": min(total, limit), "components": hits[:limit]}


def component(doc: BoardDoc, ref: str) -> dict:
    d = doc.data()
    f = next((f for f in d["footprints"] if f["ref"] == ref), None)
    if f is None:
        raise EditError(f"no footprint with reference {ref!r}")
    return {"ref": f["ref"], "value": f["value"], "footprint": f["fpid"], "side": f["side"],
            "x_mm": f["c"][0], "y_mm": f["c"][1], "rot_deg": f["rot"], "dnp": f["dnp"], "fields": f["fields"],
            "bbox_mm": f["bbox"],
            "pads": [{"pad": p["n"], "net": d["nets"][p["net"]], "x_mm": p["c"][0], "y_mm": p["c"][1]} for p in f["pads"]]}


def _box_gap(a: list, b: list) -> float:
    """Edge-to-edge distance between two [x0, y0, x1, y1] boxes (0 when they overlap)."""
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def components_near(doc: BoardDoc, ref: str | None = None, x_mm: float | None = None, y_mm: float | None = None,
                    radius_mm: float = 5.0, limit: int = 30) -> dict:
    """Footprints whose courtyard-ish bbox lies within radius_mm of a part (edge to edge) or of a point."""
    fps = doc.data()["footprints"]
    if ref:
        me = next((f for f in fps if f["ref"] == ref), None)
        if me is None:
            raise EditError(f"no footprint with reference {ref!r}")
        probe = me["bbox"]
    elif x_mm is not None and y_mm is not None:
        probe = [x_mm, y_mm, x_mm, y_mm]
    else:
        raise EditError("give either ref or both x_mm and y_mm")
    hits = []
    for f in fps:
        if ref and f["ref"] == ref:
            continue
        gap = _box_gap(probe, f["bbox"])
        if gap <= radius_mm:
            hits.append({"ref": f["ref"], "value": f["value"], "side": f["side"], "gap_mm": round(gap, 3),
                         "x_mm": f["c"][0], "y_mm": f["c"][1]})
    hits.sort(key=lambda h: h["gap_mm"])
    return {"around": ref or [x_mm, y_mm], "radius_mm": radius_mm, "total": len(hits), "components": hits[:limit]}


def _locate(d: dict, what: str) -> tuple[list[float], list | None, str]:
    """Resolve "U3", "U3.12" (pad) or "12.5,30" (point) to (centre, bbox or None, label)."""
    what = what.strip()
    m = re.fullmatch(r"(-?[\d.]+)\s*,\s*(-?[\d.]+)", what)
    if m:
        p = [float(m.group(1)), float(m.group(2))]
        return p, None, f"({p[0]}, {p[1]})"
    by_ref = {f["ref"]: f for f in d["footprints"]}
    if what in by_ref:
        f = by_ref[what]
        return f["c"], f["bbox"], what
    ref, _, pad = what.rpartition(".")
    if ref in by_ref:
        p = next((p for p in by_ref[ref]["pads"] if p["n"] == pad), None)
        if p is None:
            raise EditError(f"{ref} has no pad {pad!r}")
        return p["c"], None, what
    raise EditError(f"can't find {what!r}: use a reference (U3), a pad (U3.12) or a point (x,y in mm)")


def measure(doc: BoardDoc, a: str, b: str) -> dict:
    d = doc.data()
    (ca, ba, la), (cb, bb, lb) = _locate(d, a), _locate(d, b)
    dx, dy = cb[0] - ca[0], cb[1] - ca[1]
    out = {"from": la, "to": lb, "centre_distance_mm": round(math.hypot(dx, dy), 4),
           "dx_mm": round(dx, 4), "dy_mm": round(dy, 4)}
    if ba and bb:
        out["edge_gap_mm"] = round(_box_gap(ba, bb), 4)
    return out


def list_nets(doc: BoardDoc, pattern: str = "", limit: int = 100) -> dict:
    rx = re.compile(pattern, re.I) if pattern else None
    ns = doc.board.GetDesignSettings().m_NetSettings
    out = []
    for name in doc.data()["nets"][1:]:
        if rx is None or rx.search(name):
            out.append({"net": name, "class": ns.GetEffectiveNetClass(name).GetName()})
    return {"total": len(out), "shown": min(len(out), limit), "nets": out[:limit]}


def net(doc: BoardDoc, name: str) -> dict:
    d = doc.data()
    if name not in d["nets"]:
        raise EditError(f"no net named {name!r}")
    idx = d["nets"].index(name)
    pads = [f"{f['ref']}.{p['n']}" for f in d["footprints"] for p in f["pads"] if p["net"] == idx]
    length: dict[str, float] = {}
    for layer, arr in d["tracks"].items():
        tot = 0.0
        for k in range(0, len(arr), 6):
            if arr[k + 5] == idx:
                tot += math.hypot(arr[k + 2] - arr[k], arr[k + 3] - arr[k + 1])
        if tot:
            length[layer] = round(tot, 3)
    vias = sum(1 for v in d["vias"] if v[6] == idx)
    ns = doc.board.GetDesignSettings().m_NetSettings
    return {"net": name, "class": ns.GetEffectiveNetClass(name).GetName(), "pads": pads, "pad_count": len(pads),
            "routed_length_mm": length, "total_length_mm": round(sum(length.values()), 3), "vias": vias,
            "zones": sorted({z["layer"] for z in d["zones"] if not z.get("rule") and z["net"] == idx})}


def bom(doc: BoardDoc) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for f in doc.data()["footprints"]:
        if f["ref"].startswith("#") or not f["pads"]:
            continue  # skip graphics-only footprints (logos, fiducial art without pads)
        key = (f["value"], f["fpid"], f["fields"].get("MPN", ""), f["dnp"])
        g = groups.setdefault(key, {"value": f["value"], "footprint": f["fpid"], "mpn": f["fields"].get("MPN", ""),
                                    "manufacturer": f["fields"].get("Manufacturer", ""), "dnp": f["dnp"],
                                    "lcsc": f["fields"].get("LCSC", "") or f["fields"].get("LCSC Part", ""),
                                    "refs": []})
        g["refs"].append(f["ref"])

    def natural(r):
        m = re.match(r"([A-Za-z_#]*)(\d*)", r)
        return (m.group(1), int(m.group(2) or 0))

    rows = []
    for g in groups.values():
        g["refs"].sort(key=natural)
        g["qty"] = len(g["refs"])
        rows.append(g)
    rows.sort(key=lambda g: natural(g["refs"][0]))
    return rows


# ---- edits used by the assistant's tools ------------------------------------------------
def move_component(doc: BoardDoc, ref: str, x_mm: float | None = None, y_mm: float | None = None,
                   rotation_deg: float | None = None, side: str | None = None) -> dict:
    f = doc.fp(ref)

    def apply(board):
        before = {"x_mm": mm(f.GetPosition().x), "y_mm": mm(f.GetPosition().y), "rot_deg": f.GetOrientationDegrees(),
                  "side": "B" if f.IsFlipped() else "F"}
        if side in ("F", "B") and side != before["side"]:
            f.Flip(f.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        if x_mm is not None or y_mm is not None:
            p = f.GetPosition()
            f.SetPosition(pcbnew.VECTOR2I(to_nm(x_mm) if x_mm is not None else p.x, to_nm(y_mm) if y_mm is not None else p.y))
        if rotation_deg is not None:
            f.SetOrientationDegrees(float(rotation_deg))
        after = {"x_mm": mm(f.GetPosition().x), "y_mm": mm(f.GetPosition().y), "rot_deg": f.GetOrientationDegrees(),
                 "side": "B" if f.IsFlipped() else "F"}
        return {"ref": ref, "before": before, "after": after,
                "note": "Tracks attached to this part were not moved; run DRC to see what broke."}

    return doc.edit(f"move {ref}", apply)


def set_field(doc: BoardDoc, ref: str, field: str, value: str) -> dict:
    f = doc.fp(ref)
    if field in ("Reference", "Footprint"):
        raise EditError(f"changing {field} is not supported by this tool")

    def apply(board):
        if field == "Value":
            old = f.GetValue()
            f.SetValue(value)
        elif field == "DNP":
            old = f.IsDNP()
            f.SetDNP(str(value).lower() in ("1", "true", "yes", "dnp"))
        else:
            old = _fields(f).get(field, "")
            new = not f.HasField(field)
            f.SetField(field, value)
            if new:  # a new field defaults to visible silkscreen text at the part's centre
                fl = f.GetField(field)
                fl.SetVisible(False)
                fl.SetLayer(pcbnew.B_Fab if f.IsFlipped() else pcbnew.F_Fab)
        return {"ref": ref, "field": field, "old": old, "new": value}

    return doc.edit(f"set {ref}.{field}", apply)
