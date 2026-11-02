"""Fabrication rule profiles, written into a generated board's .kicad_pro.

KiCad keeps design rules and net classes in the .kicad_pro next to the board, not in the .kicad_pcb,
so a board built from a netlist gets a .kicad_pro from here. DRC (kicad-cli) and the Specctra export
both read it. Imported boards keep their own rules: these profiles never overwrite an existing file.

Minimums: JLCPCB capabilities page (https://jlcpcb.com/capabilities/pcb-capabilities, help-center copy at
https://api.jlcpcb.com/capabilities/Capabilities, read 2026-11-02): 1-2 layer trace width / spacing 5 mil
(0.127 mm); plated hole from 0.2 mm (0.3 mm used here, the common no-extra-cost via); annular ring
0.13 mm (1 oz). JLC does not publish a 2-layer copper-to-edge figure; 0.3 mm is the 4-6 layer figure
and is used as a conservative edge clearance. Defaults sit well above the minimums so ordinary boards
need no special options.
"""
from __future__ import annotations

import json
from pathlib import Path

PROFILES = {
    "jlc_2layer": {
        "name": "JLCPCB 2-layer, 1 oz, standard",
        "source": "https://jlcpcb.com/capabilities/pcb-capabilities (read 2026-11-02)",
        "layers": 2,
        # minimums (DRC errors below these)
        "min_track_width": 0.127, "min_clearance": 0.127, "min_via_diameter": 0.5, "min_via_drill": 0.3,
        "min_annular": 0.13, "copper_edge_clearance": 0.3, "hole_to_hole": 0.5,
        # defaults for the Default net class (what the router uses)
        "track_width": 0.25, "clearance": 0.2, "via_diameter": 0.6, "via_drill": 0.3,
        "power_track_width": 0.5,
        # placement spacing between courtyards (mm), on top of KiCad's courtyard rule
        "courtyard_gap": 0.25,
    },
    "jlc_2layer_fine": {
        "name": "JLCPCB 2-layer, 1 oz, fine pitch",
        "source": "https://jlcpcb.com/capabilities/pcb-capabilities (read 2026-11-02)",
        "layers": 2,
        "min_track_width": 0.127, "min_clearance": 0.127, "min_via_diameter": 0.45, "min_via_drill": 0.3,
        "min_annular": 0.13, "copper_edge_clearance": 0.3, "hole_to_hole": 0.5,
        "track_width": 0.15, "clearance": 0.15, "via_diameter": 0.5, "via_drill": 0.3,
        "power_track_width": 0.4,
        "courtyard_gap": 0.2,
    },
}
DEFAULT = "jlc_2layer"


def get(name: str | None) -> dict:
    return {"id": name or DEFAULT, **PROFILES.get(name or DEFAULT, PROFILES[DEFAULT])}


def listing() -> list[dict]:
    return [{"id": k, "name": v["name"], "source": v["source"]} for k, v in PROFILES.items()]


def kicad_pro(board_file: Path, profile: dict, power_nets: list[str] | None = None) -> dict:
    """The .kicad_pro content for a generated board (KiCad fills every key we leave out)."""
    p = profile
    classes = [{"name": "Default", "clearance": p["clearance"], "track_width": p["track_width"],
                "via_diameter": p["via_diameter"], "via_drill": p["via_drill"],
                "schematic_color": "rgba(0, 0, 0, 0.000)", "pcb_color": "rgba(0, 0, 0, 0.000)"}]
    assignments = []
    if power_nets:
        classes.append({"name": "Power", "clearance": p["clearance"], "track_width": p["power_track_width"],
                        "via_diameter": p["via_diameter"], "via_drill": p["via_drill"],
                        "schematic_color": "rgba(0, 0, 0, 0.000)", "pcb_color": "rgba(0, 0, 0, 0.000)"})
        assignments = [{"pattern": n, "netclass": "Power"} for n in power_nets]
    return {
        "board": {"design_settings": {"rules": {
            "min_track_width": p["min_track_width"], "min_clearance": p["min_clearance"],
            "min_via_diameter": p["min_via_diameter"], "min_through_hole_diameter": p["min_via_drill"],
            "min_via_annular_width": p["min_annular"], "min_copper_edge_clearance": p["copper_edge_clearance"],
            "min_hole_to_hole": p["hole_to_hole"]}}},
        "net_settings": {"classes": classes, "netclass_patterns": assignments, "meta": {"version": 3}},
        "meta": {"filename": board_file.with_suffix(".kicad_pro").name, "version": 1},
        "pcbstudio": {"fab_profile": p["id"], "source": p["source"]},
    }


def write_pro(board_file: Path, profile: dict, power_nets: list[str] | None = None) -> Path:
    out = board_file.with_suffix(".kicad_pro")
    out.write_text(json.dumps(kicad_pro(board_file, profile, power_nets), indent=2), encoding="utf-8")
    return out
