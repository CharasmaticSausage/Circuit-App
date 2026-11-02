"""The home screen: one summary per project (phase, scope, cost, last edit) and a board thumbnail.

Summaries read only the project's own files and the local JLC cache, never the network, so the
home screen opens instantly and offline. Thumbnails are KiCad's own plot (`kicad-cli pcb export svg`,
Edge.Cuts + copper), cached next to the board and redrawn when the board file changes.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from . import jlc, netlist, phases, routing, selection, workspace

THUMB = ".thumb.svg"
THUMB_LAYERS = "Edge.Cuts,F.Cu,B.Cu"
_thumb_sem = asyncio.Semaphore(2)


def _json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _last_edited(d: Path) -> float:
    """Newest change to anything the user made in the project (board, phase data, chats), not the thumbnail."""
    newest = 0.0
    for root, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if x != ".snapshots"]
        for f in files:
            if f != THUMB and not f.endswith(".tmp"):
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(root, f)))
                except OSError:
                    pass
    return newest


def _cost(comp: dict) -> dict:
    """Parts cost for the order (`boards` boards) from the chosen parts: the ranked candidate's order cost
    (unit price at the order quantity x quantity + Extended loading fee), else the cached JLC lookup.
    Parts with no cached price are counted, not guessed."""
    boards = int(comp.get("boards") or 5)
    fee = selection.extended_fee()
    total, priced, unpriced, as_of = 0.0, 0, 0, None
    for r in comp.get("roles", []):
        lcsc = r.get("chosen")
        if not lcsc:
            continue
        c = next((x for x in r.get("candidates") or [] if x.get("lcsc") == lcsc and x.get("order_cost") is not None), None)
        if c is None:
            p = jlc.cached_lookup(lcsc)
            c = jlc.rank([p], int(r.get("qty") or 1), boards, fee)[0] if p else None
        if c and c.get("order_cost") is not None:
            total += c["order_cost"]
            priced += 1
            as_of = max(as_of or 0, c.get("as_of") or 0) or as_of
        else:
            unpriced += 1
    return {"parts_usd": round(total, 2) if priced else None, "priced": priced, "unpriced": unpriced, "boards": boards,
            "as_of": time.strftime("%Y-%m-%d", time.localtime(as_of)) if as_of else None,
            "note": "Parts only, from the chosen JLC parts (cached snapshot prices). No PCB, assembly or shipping."}


def summary(pid: str) -> dict:
    d = workspace.project_dir(pid)
    m = workspace.meta(pid)
    st = phases.state(pid)
    ph = st["phases"]
    cur = st["current"]
    done = all(ph[k]["status"] == "approved" for k in phases.ORDER)
    comp = phases.components(pid)
    roles = comp.get("roles", [])
    nl = netlist.load(pid)
    lay = _json(d / "layout.json")
    jobs = routing.jobs(pid)
    best = None
    for j in jobs:
        for c in j.get("candidates") or []:
            s = c.get("score") or {}
            if s.get("key") and (best is None or s["key"] < best["key"]):
                best = {**s, "job": j["id"], "candidate": c["id"]}
        if best:
            break  # newest job with a scored candidate
    scope = (comp.get("requirements") or "").strip()
    board_file = d / m["board"]
    return {
        "id": pid, "name": m.get("name") or pid, "new_design": bool(m.get("new_design")),
        "source": m.get("source") or "", "created": m.get("imported_at"),
        "last_edited": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(_last_edited(d))),
        "phase": {"current": cur, "title": st["titles"][cur], "status": "approved" if done else ph[cur]["status"],
                  "all_approved": done, "statuses": {k: ph[k]["status"] for k in phases.ORDER}},
        "scope": scope or (f"Imported board: {Path(m['source']).name}" if m.get("source") else ""),
        "roles": {"total": len(roles), "chosen": sum(1 for r in roles if r.get("chosen"))},
        "netlist": {"parts": len(nl.get("parts") or []), "nets": len(nl.get("nets") or []), "ok": nl.get("ok")} if nl else None,
        "outline": {k: lay.get(k) for k in ("shape", "width_mm", "height_mm")} if lay.get("shape") else None,
        "routing": best and {k: best.get(k) for k in ("clean", "errors", "unconnected", "vias", "length_mm", "job", "candidate")},
        "chats": len(list((d / "chats").glob("*.json"))) if (d / "chats").is_dir() else 0,
        "cost": _cost(comp),
        "has_board": board_file.is_file() and (not m.get("new_design") or bool(lay.get("shape"))),
    }


async def price(pid: str) -> dict:
    """Look up (network, then cached for a day) the chosen parts that have no cached price, then re-summarise."""
    for r in phases.components(pid).get("roles", []):
        lcsc = r.get("chosen")
        if lcsc and not any(x.get("lcsc") == lcsc and x.get("order_cost") is not None for x in r.get("candidates") or []) \
                and jlc.cached_lookup(lcsc) is None:
            try:
                await asyncio.to_thread(jlc.lookup, lcsc)
            except jlc.JlcError:
                break  # offline: keep what is cached
    return summary(pid)


def summaries() -> list[dict]:
    out = []
    for p in workspace.list_projects():
        try:
            out.append(summary(p["id"]))
        except (OSError, ValueError, KeyError) as e:
            out.append({"id": p["id"], "name": p.get("name") or p["id"], "error": str(e)})
    out.sort(key=lambda s: s.get("last_edited") or "", reverse=True)
    return out


async def thumbnail(pid: str) -> Path | None:
    """The board plotted as SVG, regenerated only when the board file is newer than the cached plot."""
    d = workspace.project_dir(pid)
    bf = d / workspace.meta(pid)["board"]
    out = d / THUMB
    if not bf.is_file():
        return None
    if out.is_file() and out.stat().st_mtime >= bf.stat().st_mtime:
        return out
    cli = workspace.kicad_cli()
    if not cli:
        return None
    async with _thumb_sem:
        tmp = d / ".thumb.tmp.svg"
        proc = await asyncio.create_subprocess_exec(
            cli, "pcb", "export", "svg", "--mode-single", "-o", str(tmp), "--layers", THUMB_LAYERS,
            "--exclude-drawing-sheet", "--page-size-mode", "2", "--drill-shape-opt", "1", str(bf),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            await asyncio.wait_for(proc.communicate(), 60)
        except asyncio.TimeoutError:
            proc.kill()
            return None
        if proc.returncode != 0 or not tmp.is_file():
            return None
        os.replace(tmp, out)
    return out
