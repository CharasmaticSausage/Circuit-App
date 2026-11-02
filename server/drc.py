"""Design-rule check through kicad-cli (KiCad's own DRC engine, same rules as the GUI)."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from collections import Counter

from . import workspace

_last: dict[str, dict] = {}  # project id -> last DRC result


async def run(pid: str, refill_zones: bool = False) -> dict:
    result = await run_file(workspace.board_path(pid), refill_zones)
    _last[pid] = result
    return result


async def run_file(board: str, refill_zones: bool = False) -> dict:
    """DRC any .kicad_pcb (rules from the .kicad_pro / .kicad_dru beside it)."""
    cli = workspace.kicad_cli()
    if not cli:
        raise RuntimeError("kicad-cli not found: install KiCad or set its path in Settings")
    fd, out = tempfile.mkstemp(suffix=".json", prefix="drc_")
    os.close(fd)
    args = [cli, "pcb", "drc", "--format", "json", "--severity-all", "--units", "mm", "-o", out]
    if refill_zones:
        args.append("--refill-zones")  # refills in memory only; we never pass --save-board
    args.append(board)
    t0 = time.time()
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    try:
        with open(out, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raise RuntimeError(f"kicad-cli DRC failed (exit {proc.returncode}): {(stderr or stdout).decode(errors='replace')[-600:]}")
    finally:
        try:
            os.remove(out)
        except OSError:
            pass

    items = []
    for kind, lst in (("violation", raw.get("violations", [])), ("unconnected", raw.get("unconnected_items", [])),
                      ("parity", raw.get("schematic_parity", []))):
        for v in lst:
            locs = [{"x": i.get("pos", {}).get("x"), "y": i.get("pos", {}).get("y"), "text": i.get("description", "")}
                    for i in v.get("items", [])]
            items.append({"kind": kind, "type": v.get("type", ""), "severity": v.get("severity", ""),
                          "description": v.get("description", ""), "items": locs})
    by_type = Counter(i["type"] for i in items)
    sev = Counter(i["severity"] for i in items)
    result = {
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "seconds": round(time.time() - t0, 1),
        "refilled_zones": refill_zones,
        "counts": {"total": len(items), "errors": sev.get("error", 0), "warnings": sev.get("warning", 0),
                   "unconnected": sum(1 for i in items if i["kind"] == "unconnected")},
        "by_type": dict(by_type.most_common()),
        "items": items,
    }
    return result


def last(pid: str) -> dict | None:
    return _last.get(pid)


def digest(result: dict, limit: int = 40) -> dict:
    """Compact form for the model: counts plus the first few violations with locations."""
    return {
        "counts": result["counts"], "by_type": result["by_type"], "refilled_zones": result["refilled_zones"],
        "first_items": [
            {"type": i["type"], "severity": i["severity"], "description": i["description"],
             "where": [f"{l['text']} @ ({l['x']}, {l['y']})" for l in i["items"]][:3]}
            for i in result["items"][:limit]
        ],
    }
