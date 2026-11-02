"""Phase operations shared by the HTTP endpoints (buttons) and the assistant's tools, so both do exactly
the same thing. `pcb` runs a function on the pcbnew thread; `doc` returns the project's BoardDoc there."""
from __future__ import annotations

import asyncio

from . import drc, fab, layout, netlist, phases, routing, workspace


# ---- per-project layout settings (outline, fab profile, last placer run) --------------------------
def layout_cfg(pid: str) -> dict:
    return phases._read(pid, "layout.json", {"shape": "rect", "width_mm": 50.0, "height_mm": 40.0, "corner_mm": 1.0,
                                             "fab_profile": fab.DEFAULT, "last_place": None, "place_check": None})


def save_layout_cfg(pid: str, patch: dict) -> dict:
    c = layout_cfg(pid)
    c.update(patch)
    phases._write(pid, "layout.json", c)
    return c


def is_new_design(pid: str) -> bool:
    return bool(workspace.meta(pid).get("new_design"))


# ---- phase 1b: netlist ---------------------------------------------------------------------------
def netlist_state(pid: str) -> dict:
    return {"netlist": netlist.summary(netlist.load(pid)), "required": is_new_design(pid)}


async def import_netlist(pid: str, path: str) -> dict:
    nl = await asyncio.to_thread(netlist.import_file, pid, path)
    return netlist.summary(nl)


def draft_netlist(pid: str, parts: list, nets: list) -> dict:
    return netlist.summary(netlist.save(pid, netlist.from_draft(parts, nets)))


def check_components_ready(pid: str) -> list[str]:
    """Reasons Phase 1 can't be approved yet (empty = fine)."""
    why = []
    if is_new_design(pid):
        nl = netlist.load(pid)
        if not nl:
            why.append("no netlist yet: import a KiCad schematic/netlist or have the assistant draft one")
        elif not nl.get("ok"):
            errs = [i["msg"] for i in nl.get("issues", []) if i["level"] == "error"]
            why.append(f"the netlist has {len(errs)} error(s): " + "; ".join(errs[:3]))
    return why


# ---- phase 2: placement --------------------------------------------------------------------------
async def set_outline(pid: str, pcb, doc, shape: str, w: float, h: float, r: float = 0.0) -> dict:
    layout.outline_points(shape, w, h, r)  # validates
    cfg = save_layout_cfg(pid, {"shape": shape, "width_mm": w, "height_mm": h, "corner_mm": r})
    has_parts = await pcb(lambda: len(doc().board.GetFootprints()) > 0)
    if not has_parts and is_new_design(pid):
        return {"saved": cfg, "note": "outline saved; it is drawn when the board is built from the netlist"}
    return await pcb(lambda: layout.set_outline(doc(), shape, w, h, r))


async def build(pid: str, pcb, doc, profile_id: str | None = None) -> dict:
    phases.require(pid, "placement")
    nl = netlist.load(pid)
    if not nl:
        raise ValueError("no netlist: Phase 1 needs one for a new design")
    cfg = save_layout_cfg(pid, {"fab_profile": profile_id or layout_cfg(pid)["fab_profile"]})
    await asyncio.to_thread(netlist.libraries)
    res = await pcb(lambda: layout.build_board(doc(), nl, cfg["shape"], cfg["width_mm"], cfg["height_mm"],
                                               cfg["corner_mm"], cfg["fab_profile"]))
    save_layout_cfg(pid, {"last_place": None, "place_check": None})
    return res


async def place(pid: str, pcb, doc, moves: int = 20000, seed: int = 1, refine: bool = False,
                fixed_refs=(), edge_refs=()) -> dict:
    phases.require(pid, "placement")
    cfg = layout_cfg(pid)
    moves = max(500, min(int(moves), 200000))
    res = await pcb(lambda: layout.place(doc(), cfg["fab_profile"], moves, seed, refine, edge_refs, fixed_refs))
    check = await placement_check(pid)
    save_layout_cfg(pid, {"last_place": {k: v for k, v in res.items() if k != "moved"} | {"moved": len(res["moved"])},
                          "place_check": check})
    if phases.state(pid)["phases"]["placement"]["status"] == "approved":
        phases.reopen(pid, "placement")
    return {**res, "check": check}


async def placement_check(pid: str) -> dict:
    """The independent check of a placement: KiCad DRC's courtyard and edge rules (unrouted is expected)."""
    d = await drc.run(pid)
    hard = [i for i in d["items"] if i["kind"] == "violation" and i["severity"] == "error"]
    internal = [i for i in hard if routing._internal(i)]
    by = d["by_type"]
    return {"courtyard_overlaps": by.get("courtyards_overlap", 0),
            "edge_clearance": by.get("copper_edge_clearance", 0) + by.get("edge_clearance", 0),
            "other_errors": len(hard) - len(internal) - by.get("courtyards_overlap", 0) - by.get("copper_edge_clearance", 0),
            "footprint_internal": len(internal), "unconnected": d["counts"]["unconnected"],
            "warnings": d["counts"]["warnings"], "drc_ran_at": d["ran_at"]}


async def layout_state(pid: str, pcb, doc) -> dict:
    cfg = layout_cfg(pid)
    info = await pcb(lambda: {"parts": len(doc().board.GetFootprints()),
                              "has_outline": layout.board_outline(doc().board) is not None})
    terms = None
    if info["parts"] and info["has_outline"]:
        try:
            terms = await pcb(lambda: layout.current_terms(doc(), cfg["fab_profile"]))
        except Exception as e:  # noqa: BLE001 - shown to the user, not fatal
            terms = {"error": str(e)}
    return {**cfg, **info, "terms": terms, "profiles": fab.listing(), "new_design": is_new_design(pid),
            "outline_saved": (workspace.project_dir(pid) / "layout.json").is_file()}


# ---- phase 3: routing ------------------------------------------------------------------------------
def routing_state(pid: str) -> dict:
    cur = routing.current()
    return {"router": routing.fastroute_exe(), "running": cur if cur and cur["pid"] == pid else None,
            "busy_elsewhere": bool(cur and cur["pid"] != pid), "jobs": routing.jobs(pid)[:10]}


async def start_routing(pid: str, pcb, emit=None, rounds=None, max_time=None) -> dict:
    phases.require(pid, "routing")
    return await routing.start(pid, pcb, emit, rounds, max_time, profile_id=layout_cfg(pid)["fab_profile"])


async def adopt(pid: str, pcb, doc, jid: str, cid: str) -> dict:
    path = routing.candidate_path(pid, jid, cid)
    res = await pcb(lambda: doc().adopt_file(f"adopt routed candidate {jid}/{cid}", str(path)))
    j = routing.load(pid, jid)
    j["adopted"] = cid
    routing._save(pid, j)
    return {**res, "adopted": f"{jid}/{cid}"}
