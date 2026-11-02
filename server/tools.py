"""Tools the design assistant can call, in a provider-neutral form.

Each tool has a JSON schema, a kind ("read", "write" or "ui"), and an async handler.
Providers translate TOOLS into their own wire format and call `execute()`.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import board, changelist, drc, fab, jlc, netlist, ops, phases, routing, selection


@dataclass
class ToolContext:
    pid: str
    pcb: Callable[..., Awaitable[Any]]  # run a function on the pcbnew thread
    doc: Callable[[], Any]              # the project's BoardDoc (only call on the pcbnew thread)
    emit: Callable[[dict], Awaitable[None]]
    parts: list = field(default_factory=list)  # the UI transcript of the current assistant turn
    pending: dict = field(default_factory=dict)  # tool name -> model tool_use ids announced but not run yet


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


S, N, I, B = {"type": "string"}, {"type": "number"}, {"type": "integer"}, {"type": "boolean"}

# How to read a candidate's score; models misreported footprint_internal as "warnings" without this.
ROUTING_LEGEND = {
    "errors": "KiCad DRC errors caused by the layout (scored; 0 needed for 'clean').",
    "footprint_internal": "KiCad DRC ERRORS, not warnings, between pads/holes of ONE footprint (library footprint vs "
                          "the fab rules). No placement or routing can fix them, so they are not scored, but report "
                          "them as errors and say the fab may reject them unless the footprint or rule is changed.",
    "warnings": "KiCad DRC warnings (mostly silkscreen); not scored.",
    "router_violations": "fastroute's own count from its simplified clearance model; it can disagree with KiCad. "
                         "KiCad DRC (errors) is the authority.",
}

TOOLS: list[dict] = [
    {"name": "board_summary", "kind": "read",
     "description": "Overview of the open board: size, copper layers, counts, unrouted connections, net classes "
                    "(track width, clearance, via size) and copper zones. Call this first in a new conversation.",
     "input_schema": _obj({})},
    {"name": "find_components", "kind": "read",
     "description": "Search footprints. `query` is a case-insensitive regex matched against reference, value, "
                    "footprint library id and fields (e.g. 'U\\d+', '100nF', 'QFN'). Empty query lists everything.",
     "input_schema": _obj({"query": S, "limit": I})},
    {"name": "get_component", "kind": "read",
     "description": "Full detail for one footprint: value, footprint, side, position, rotation, fields, and every "
                    "pad with its net and position.",
     "input_schema": _obj({"ref": S}, ["ref"])},
    {"name": "list_nets", "kind": "read",
     "description": "List nets and their net class. `pattern` is a case-insensitive regex (e.g. 'DDR', '^VCC').",
     "input_schema": _obj({"pattern": S, "limit": I})},
    {"name": "get_net", "kind": "read",
     "description": "One net: net class, every pad on it (REF.PAD), routed track length per layer, via count, "
                    "and the layers it has copper zones on.",
     "input_schema": _obj({"name": S}, ["name"])},
    {"name": "get_bom", "kind": "read",
     "description": "Bill of materials grouped by value + footprint + MPN, with quantities and references.",
     "input_schema": _obj({})},
    {"name": "run_drc", "kind": "read",
     "description": "Run KiCad's design rule check on the board (kicad-cli, same engine as the KiCad GUI). Returns "
                    "counts by type and the first violations with locations. Set refill_zones to refill copper "
                    "pours first (slower; the refill is not saved). The full list appears in the DRC tab.",
     "input_schema": _obj({"refill_zones": B})},
    {"name": "components_near", "kind": "read",
     "description": "List footprints within radius_mm (edge to edge, by bounding box) of a part, or of a point. "
                    "Use it to check what a move would crowd, or what sits around a connector or hole.",
     "input_schema": _obj({"ref": S, "x_mm": N, "y_mm": N, "radius_mm": N, "limit": I})},
    {"name": "measure", "kind": "read",
     "description": "Distance between two things on the board. Each end is a reference (U3), a pad (U3.12) or a "
                    "point 'x,y' in mm. Returns centre distance, dx, dy, and the bounding-box edge gap for two parts.",
     "input_schema": _obj({"a": S, "b": S}, ["a", "b"])},
    {"name": "move_component", "kind": "write",
     "description": "Move, rotate and/or flip one footprint. Coordinates are absolute board millimetres (KiCad "
                    "coordinates, y grows downward). Omitted values are kept. Attached tracks are NOT moved, so "
                    "check DRC afterwards. Saved to the project's working copy and undoable.",
     "input_schema": _obj({"ref": S, "x_mm": N, "y_mm": N, "rotation_deg": N, "side": {"type": "string", "enum": ["F", "B"]}}, ["ref"])},
    {"name": "set_component_field", "kind": "write",
     "description": "Set a footprint field: 'Value', 'DNP' ('true'/'false'), or any custom field such as 'MPN', "
                    "'Manufacturer', 'LCSC'. Saved to the project's working copy and undoable.",
     "input_schema": _obj({"ref": S, "field": S, "value": S}, ["ref", "field", "value"])},
    {"name": "undo_last_edit", "kind": "write",
     "description": "Revert the most recent board edit (yours or one made from the app).",
     "input_schema": _obj({})},
    {"name": "show_in_viewer", "kind": "ui",
     "description": "Point the user at something in the PCB preview: select footprints by reference, highlight "
                    "one net, choose which layers are visible, and/or zoom to the selection. Use it whenever you "
                    "talk about specific parts or nets.",
     "input_schema": _obj({"refs": {"type": "array", "items": S}, "net": S,
                           "layers": {"type": "array", "items": S}, "zoom": B})},
    # ---- phase 1: major component selection (JLCPCB parts data) ----
    {"name": "phase_status", "kind": "read",
     "description": "Where the design flow is: 1 Components -> 2 Placement -> 3 Routing, each locked, active, stale "
                    "or approved; plus the phase-1 requirements, board quantity and part roles with their chosen "
                    "LCSC parts. Only the user can approve a phase (a button in the app); ask them when it looks done.",
     "input_schema": _obj({})},
    {"name": "jlc_search", "kind": "read",
     "description": "Search JLCPCB's parts catalogue (daily snapshot via jlcsearch; confirm at order time). Best: "
                    "`subcategory`, JLC's exact subcategory name (e.g. 'Voltage Regulators - Linear, Low Drop Out (LDO) "
                    "Regulators', 'Multilayer Ceramic Capacitors MLCC - SMD/SMT', 'Chip Resistor - Surface Mount', "
                    "'Microcontrollers (MCU/MPU/SOC)'; jlc_part on any known part shows its name) plus `package`: "
                    "returns the 100 best-stocked parts. `query` words then narrow by MPN or description "
                    "(e.g. '3.3V', '100nF'). Alternative: `category`, a parametric table ('capacitors', 'resistors', "
                    "'ldos'...) with `filters` in SI units ({\"capacitance\": \"0.0000001\"}); these tables can miss "
                    "common parts. Filter values are the exact strings the service stores: list_categories=true with "
                    "a category lists each filter and its valid values (null = free-form/SI number); a near miss "
                    "('female') is mapped to the stored value ('Female'), an unknown one is an error listing the "
                    "valid ones. list_categories=true alone lists the tables. A bare "
                    "`query` needs a `package` too. Results "
                    "are ranked for the order quantity. Saves nothing.",
     "input_schema": _obj({"query": S, "subcategory": S, "category": S, "filters": {"type": "object"}, "package": S, "basic_only": B,
                           "qty_per_board": I, "limit": I, "list_categories": B})},
    {"name": "jlc_part", "kind": "read",
     "description": "One JLCPCB part by LCSC number (C25804): stock, price breaks, Basic/Extended, package.",
     "input_schema": _obj({"lcsc": S}, ["lcsc"])},
    {"name": "set_part_role", "kind": "write",
     "description": "Phase 1: define or update a part the design needs (a 'role' such as '3.3 V LDO' or 'main MCU'), "
                    "search JLC for it, rank the candidates (deterministically: enough stock, package match, order "
                    "cost incl. the Extended-part loading fee, then stock) and save the top ones for the user. "
                    "Pass role_id to update an existing role. Put this part's own spec (voltage, current, "
                    "connector type...) in `notes`; it is shown on the role card. `boards` sets the order quantity.",
     "input_schema": _obj({"name": S, "qty": I, "subcategory": S, "category": S, "filters": {"type": "object"}, "query": S, "package": S,
                           "notes": S, "refs": {"type": "array", "items": S}, "role_id": S, "basic_only": B,
                           "boards": I}, ["name"])},
    {"name": "set_board_requirements", "kind": "write",
     "description": "Phase 1: the board-wide requirements text (what the whole board must do) and/or the order quantity. "
                    "`requirements` REPLACES the text the user sees in the Requirements box, so pass the full text "
                    "(theirs plus your additions), and only when the user asked you to write or change it. Per-part "
                    "specs belong in set_part_role `notes`, not here.",
     "input_schema": _obj({"requirements": S, "boards": I})},
    {"name": "choose_part", "kind": "write",
     "description": "Phase 1: pick one of a role's ranked candidates (by LCSC number), or clear the choice with lcsc=''.",
     "input_schema": _obj({"role_id": S, "lcsc": S}, ["role_id", "lcsc"])},
    {"name": "check_bom_availability", "kind": "read",
     "description": "Check the open board's BOM against JLCPCB: for every line with an LCSC number, stock against "
                    "qty x boards, Basic/Extended, price and the total loading fees. Set create_roles to turn each "
                    "BOM line into a phase-1 role with its current part chosen.",
     "input_schema": _obj({"create_roles": B})},
    # ---- phase 1b: netlist ----
    {"name": "find_footprint", "kind": "read",
     "description": "Search the installed KiCad footprint libraries by name words (all must match, case-insensitive), "
                    "e.g. 'SOT-23-5', 'SOIC-8 3.9x4.9', 'USB_C Receptacle 16P', 'C_0603'. Returns 'Library:Name' ids.",
     "input_schema": _obj({"query": S, "limit": I}, ["query"])},
    {"name": "footprint_pads", "kind": "read",
     "description": "A footprint's pad numbers (what netlist pins must use: '1'..'8', 'A1', 'EP', 'SH') and its "
                    "description. Map the part's datasheet pinout onto these numbers.",
     "input_schema": _obj({"footprint": S}, ["footprint"])},
    {"name": "symbol_pins", "kind": "read",
     "description": "A part's pin number -> pin name map from KiCad's installed symbol library (e.g. 'AP2112K-3.3', "
                    "'ATtiny85-20S', 'USB_C_Receptacle_PowerOnly_6P'). Use it to check a netlist pinout instead of "
                    "recalling pinouts from memory; if no symbol exists, say the pinout is unverified.",
     "input_schema": _obj({"name": S}, ["name"])},
    {"name": "get_netlist", "kind": "read",
     "description": "The project's netlist: source, validation issues, and for every part its footprint, LCSC and "
                    "pin -> net map plus pads left unconnected.",
     "input_schema": _obj({})},
    {"name": "draft_netlist", "kind": "write",
     "description": "Phase 1, new designs: write the whole netlist from the chosen parts. parts: [{ref, value, "
                    "footprint 'Lib:Name', lcsc}]; nets: [{name, pins: ['U1.3', 'C1.1', ...]}] using footprint pad "
                    "numbers (footprint_pads). Replaces any earlier netlist, validates every footprint and pad, and "
                    "links parts to Phase 1 roles. Take pinouts from the datasheet; never guess. The user checks "
                    "pin -> net per part in the Components tab before approving.",
     "input_schema": _obj({"parts": {"type": "array", "items": {"type": "object"}},
                           "nets": {"type": "array", "items": {"type": "object"}}}, ["parts", "nets"])},
    # ---- phase 2: placement ----
    {"name": "placement_status", "kind": "read",
     "description": "Phase 2 state: board outline, fab profile, part count, the placer's cost terms for the board "
                    "as it is, the last placer run and its KiCad DRC check (courtyard overlaps, edge clearance).",
     "input_schema": _obj({})},
    {"name": "set_board_outline", "kind": "write",
     "description": "Set the board shape: rect (width, height, optional corner radius), circle (width = diameter) "
                    "or oval (width x height ellipse). Millimetres. Redraws Edge.Cuts if the board exists, otherwise "
                    "it is used when the board is built.",
     "input_schema": _obj({"shape": {"type": "string", "enum": ["rect", "circle", "oval"]}, "width_mm": N,
                           "height_mm": N, "corner_mm": N}, ["shape", "width_mm"])},
    {"name": "build_board", "kind": "write",
     "description": "Phase 2, new designs: build the board from the approved netlist: outline, every footprint from "
                    "the KiCad libraries, nets, and the fab profile's rules (JLC 2-layer by default). Replaces the "
                    "board (undoable). Run the placer next.",
     "input_schema": _obj({"fab_profile": {"type": "string", "enum": list(fab.PROFILES)}})},
    {"name": "run_placer", "kind": "write",
     "description": "Phase 2: automatic placement (simulated annealing: wirelength, overlap, inside the outline, "
                    "connectors to the edge; locked parts stay). Then KiCad DRC checks courtyards and edge clearance. "
                    "refine=true makes small moves from the current placement instead of starting hot. Undoable. "
                    "Report the before/after terms and the DRC check, not just 'done'.",
     "input_schema": _obj({"moves": I, "seed": I, "refine": B, "fixed_refs": {"type": "array", "items": S},
                           "edge_refs": {"type": "array", "items": S}})},
    # ---- phase 3: routing ----
    {"name": "start_routing", "kind": "write",
     "description": "Phase 3: start the background routing loop (fastroute -> KiCad DRC -> re-place with feedback, "
                    "`rounds` times, `max_time_s` per round). Works on copies; the board changes only when the user "
                    "adopts a candidate. Returns at once; poll routing_status.",
     "input_schema": _obj({"rounds": I, "max_time_s": I})},
    {"name": "routing_status", "kind": "read",
     "description": "Phase 3: the running or last routing job: log, and candidates ranked by DRC errors, unconnected, "
                    "vias, length.",
     "input_schema": _obj({})},
    # ---- self-improvement ----
    {"name": "log_improvement", "kind": "write",
     "description": "Record a flaw or improvement for PCB Studio's developers (the app's changelist). Call it yourself, "
                    "without asking, whenever a tool errors or returns misleading or missing data, you need a tool "
                    "or option that does not exist, a workflow is clumsy, or the user wishes the app did something. "
                    "One entry per distinct issue; a repeated title just counts again. Give concrete evidence "
                    "(the tool, its input, what came back) and, if you can, the fix you would suggest.",
     "input_schema": _obj({"title": S, "detail": S, "evidence": S,
                           "area": {"type": "string", "enum": changelist.AREAS},
                           "kind": {"type": "string", "enum": changelist.KINDS}}, ["title", "detail"])},
]

BY_NAME = {t["name"]: t for t in TOOLS}


def validate(schema: dict, args: Any) -> str | None:
    """Minimal JSON-schema check for our own tool schemas. Returns an error or None."""
    if not isinstance(args, dict):
        return "input must be an object"
    props = schema.get("properties", {})
    for r in schema.get("required", []):
        if r not in args:
            return f"missing required field {r!r}"
    for k, v in args.items():
        if k not in props:
            return f"unknown field {k!r}"
        t = props[k].get("type")
        ok = {
            "string": isinstance(v, str),
            "number": isinstance(v, (int, float)) and not isinstance(v, bool),
            "integer": isinstance(v, int) and not isinstance(v, bool),
            "boolean": isinstance(v, bool),
            "array": isinstance(v, list) and all(isinstance(x, dict if props[k].get("items", {}).get("type") == "object"
                                                             else str) for x in v),
            "object": isinstance(v, dict) and all(isinstance(x, (str, int, float)) for x in v.values()),
        }.get(t, True)
        if not ok:
            return f"field {k!r} must be {t}"
        if "enum" in props[k] and v not in props[k]["enum"]:
            return f"field {k!r} must be one of {props[k]['enum']}"
    return None


async def execute(name: str, args: dict, ctx: ToolContext) -> tuple[bool, Any]:
    """Run one tool. Returns (ok, result). Never raises for model-caused errors."""
    spec = BY_NAME.get(name)
    if spec is None:
        return False, f"unknown tool {name!r}"
    err = validate(spec["input_schema"], args)
    if err:
        return False, f"invalid input: {err}"
    try:
        return True, await _dispatch(name, args, ctx)
    except (board.EditError, ValueError, KeyError) as e:
        return False, str(e)
    except RuntimeError as e:
        return False, str(e)


async def _dispatch(name: str, a: dict, ctx: ToolContext) -> Any:
    doc = ctx.doc
    if name == "board_summary":
        return await ctx.pcb(lambda: board.summary(doc()))
    if name == "find_components":
        return await ctx.pcb(lambda: board.find_components(doc(), a.get("query", ""), min(a.get("limit", 50), 300)))
    if name == "get_component":
        return await ctx.pcb(lambda: board.component(doc(), a["ref"]))
    if name == "list_nets":
        return await ctx.pcb(lambda: board.list_nets(doc(), a.get("pattern", ""), min(a.get("limit", 100), 500)))
    if name == "get_net":
        return await ctx.pcb(lambda: board.net(doc(), a["name"]))
    if name == "get_bom":
        rows = await ctx.pcb(lambda: board.bom(doc()))
        return {"lines": len(rows), "parts": sum(r["qty"] for r in rows),
                "rows": [{k: r[k] for k in ("qty", "value", "footprint", "mpn", "dnp", "refs")} for r in rows]}
    if name == "run_drc":
        await ctx.emit({"type": "status", "text": "Running DRC..."})
        res = await drc.run(ctx.pid, bool(a.get("refill_zones", False)))
        await ctx.emit({"type": "drc", "counts": res["counts"]})
        return drc.digest(res)
    if name == "components_near":
        return await ctx.pcb(lambda: board.components_near(doc(), a.get("ref"), a.get("x_mm"), a.get("y_mm"),
                                                           float(a.get("radius_mm", 5.0)), min(a.get("limit", 30), 200)))
    if name == "measure":
        return await ctx.pcb(lambda: board.measure(doc(), a["a"], a["b"]))
    if name == "move_component":
        res = await ctx.pcb(lambda: board.move_component(doc(), a["ref"], a.get("x_mm"), a.get("y_mm"),
                                                         a.get("rotation_deg"), a.get("side")))
        await ctx.emit({"type": "board", "rev": res["rev"]})
        await ctx.emit({"type": "ui", "refs": [a["ref"]]})
        return res
    if name == "set_component_field":
        res = await ctx.pcb(lambda: board.set_field(doc(), a["ref"], a["field"], a["value"]))
        await ctx.emit({"type": "board", "rev": res["rev"]})
        return res
    if name == "undo_last_edit":
        res = await ctx.pcb(lambda: doc().undo_last())
        await ctx.emit({"type": "board", "rev": res["rev"]})
        return res
    if name == "show_in_viewer":
        data = await ctx.pcb(lambda: doc().data())
        known = {f["ref"] for f in data["footprints"]}
        refs = [r for r in a.get("refs", []) if r in known]
        missing = [r for r in a.get("refs", []) if r not in known]
        net = a.get("net")
        if net and net not in data["nets"]:
            return {"shown": False, "error": f"no net named {net!r}"}
        layer_names = {l["name"] for l in data["layers"]}
        layers = [l for l in a.get("layers", []) if l in layer_names]
        await ctx.emit({"type": "ui", "refs": refs, "net": net, "layers": layers or None, "zoom": bool(a.get("zoom", True))})
        return {"shown": True, "refs": refs, "unknown_refs": missing, "net": net, "layers": layers}
    if name == "phase_status":
        return {"flow": phases.state(ctx.pid), "components": phases.components(ctx.pid)}
    if name == "jlc_search":
        if a.get("list_categories"):
            if a.get("category"):
                return {"category": a["category"], "filters": jlc.filter_summary(await asyncio.to_thread(jlc.filters, a["category"]))}
            return {"categories": await asyncio.to_thread(jlc.categories)}
        if not (a.get("subcategory") or a.get("category") or (a.get("query") and a.get("package"))):
            raise ValueError("give a subcategory (preferred), a category, or a query with a package")
        res = await asyncio.to_thread(jlc.search, a.get("query", ""), a.get("category"), a.get("filters"),
                                      a.get("package"), bool(a.get("basic_only")), 100, a.get("subcategory"))
        data = phases.components(ctx.pid)
        ranked = await asyncio.to_thread(selection.shortlist, res["parts"], int(a.get("qty_per_board", 1)),
                                         int(data.get("boards") or 5), a.get("package"), min(a.get("limit", 10), 30))
        return {"source": res["source"], "notes": res["notes"], "total": res["total"],
                "parts": [selection._slim(c) for c in ranked]}
    if name == "jlc_part":
        p = await asyncio.to_thread(jlc.lookup, a["lcsc"])
        if p is None:
            return {"found": False, "lcsc": a["lcsc"], "note": "not in the JLC catalogue snapshot"}
        boards = int(phases.components(ctx.pid).get("boards") or 5)
        return {"found": True, "source": jlc.SOURCE, **selection._slim(jlc.rank([p], 1, boards, selection.extended_fee())[0]),
                "tiers": p["tiers"]}
    if name == "set_part_role":
        if "boards" in a:
            data = phases.components(ctx.pid)
            data["boards"] = max(1, int(a["boards"]))
            phases.save_components(ctx.pid, data)
        await ctx.emit({"type": "status", "text": f"Searching JLC for {a['name']}..."})
        res = await selection.find_candidates(
            ctx.pid, name=a["name"], qty=int(a.get("qty", 1)), category=a.get("category"), filters=a.get("filters"),
            query=a.get("query", ""), package=a.get("package"), notes=a.get("notes", ""), refs=a.get("refs"),
            role_id=a.get("role_id"), basic_only=bool(a.get("basic_only")), subcategory=a.get("subcategory"))
        await ctx.emit({"type": "phase", "phase": "components"})
        return res
    if name == "set_board_requirements":
        data = phases.components(ctx.pid)
        if "requirements" in a:
            data["requirements"] = str(a["requirements"])
        if "boards" in a:
            data["boards"] = max(1, int(a["boards"]))
        phases.save_components(ctx.pid, data)
        await ctx.emit({"type": "phase", "phase": "components"})
        return {"requirements": data["requirements"], "boards": data["boards"]}
    if name == "choose_part":
        r = selection.choose(ctx.pid, a["role_id"], a["lcsc"] or None)
        await ctx.emit({"type": "phase", "phase": "components"})
        return {"role_id": r["id"], "chosen": r["chosen"]}
    if name == "check_bom_availability":
        await ctx.emit({"type": "status", "text": "Checking the BOM against JLC..."})
        res = await selection.check_bom(ctx.pid, ctx.pcb, ctx.doc, bool(a.get("create_roles")))
        if a.get("create_roles"):
            await ctx.emit({"type": "phase", "phase": "components"})
        return res
    if name == "find_footprint":
        return await asyncio.to_thread(netlist.find_footprints, a["query"], min(a.get("limit", 30), 100))
    if name == "footprint_pads":
        return await asyncio.to_thread(netlist.footprint_pads, a["footprint"])
    if name == "symbol_pins":
        return await asyncio.to_thread(netlist.symbol_pins, a["name"])
    if name == "get_netlist":
        return ops.netlist_state(ctx.pid)
    if name == "draft_netlist":
        res = await asyncio.to_thread(ops.draft_netlist, ctx.pid, a["parts"], a["nets"])
        await ctx.emit({"type": "phase", "phase": "components"})
        return res
    if name == "placement_status":
        return await ops.layout_state(ctx.pid, ctx.pcb, ctx.doc)
    if name == "set_board_outline":
        res = await ops.set_outline(ctx.pid, ctx.pcb, ctx.doc, a["shape"], float(a["width_mm"]),
                                    float(a.get("height_mm") or a["width_mm"]), float(a.get("corner_mm", 0)))
        if "rev" in res:
            await ctx.emit({"type": "board", "rev": res["rev"]})
        await ctx.emit({"type": "phase", "phase": "placement"})
        return res
    if name == "build_board":
        await ctx.emit({"type": "status", "text": "Building the board from the netlist..."})
        res = await ops.build(ctx.pid, ctx.pcb, ctx.doc, a.get("fab_profile"))
        await ctx.emit({"type": "board", "rev": res["rev"]})
        await ctx.emit({"type": "phase", "phase": "placement"})
        return res
    if name == "run_placer":
        await ctx.emit({"type": "status", "text": "Placing parts..."})
        res = await ops.place(ctx.pid, ctx.pcb, ctx.doc, a.get("moves", 20000), a.get("seed", 1), bool(a.get("refine")),
                              a.get("fixed_refs", ()), a.get("edge_refs", ()))
        await ctx.emit({"type": "board", "rev": res["rev"]})
        await ctx.emit({"type": "phase", "phase": "placement"})
        res = dict(res)
        res["moved"] = len(res["moved"])
        return res
    if name == "start_routing":
        job = await ops.start_routing(ctx.pid, ctx.pcb, None, a.get("rounds"), a.get("max_time_s"))
        await ctx.emit({"type": "phase", "phase": "routing"})
        return {"started": job["id"], "rounds": job["rounds"], "max_time_s": job["max_time"],
                "note": "Runs in the background; call routing_status to follow it."}
    if name == "routing_status":
        st = ops.routing_state(ctx.pid)
        j = st["jobs"][0] if st["jobs"] else None
        if j:
            j = {k: j[k] for k in ("id", "status", "round", "rounds", "max_time", "candidates", "best", "error",
                                   "feedback_helped", "adopted") if k in j} | {"log": j.get("log", [])[-15:]}
        return {"router": bool(st["router"]), "running": st["running"], "job": j, "legend": ROUTING_LEGEND}
    if name == "log_improvement":
        res = changelist.log(a["title"], a["detail"], a.get("area", "other"), a.get("kind", "improvement"),
                             a.get("evidence", ""), ctx.pid)
        await ctx.emit({"type": "status", "text": "Noted in the changelist." if res["logged"] else "Already on the changelist."})
        return res
    raise ValueError(f"unhandled tool {name}")


def summarize(result: Any, limit: int = 160) -> str:
    """One line for the tool chip in the chat UI."""
    if isinstance(result, str):
        return result[:limit]
    if isinstance(result, dict):
        for k in ("counts", "after", "undone", "total", "lines", "pad_count"):
            if k in result:
                return f"{k}: {json.dumps(result[k])}"[:limit]
    return json.dumps(result)[:limit]
