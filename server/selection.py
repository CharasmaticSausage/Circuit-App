"""Phase 1, major component selection: roles -> JLC candidates -> deterministic ranking -> choice.

The model proposes roles and search filters; the ranking itself is jlc.rank (code, not the model),
so the order is reproducible and every candidate carries its reason. Network calls run on worker
threads (asyncio.to_thread), never on the pcbnew thread.
"""
from __future__ import annotations

import asyncio
import time

from . import board, jlc, phases, workspace

KEEP = 12  # candidates stored per role


def extended_fee() -> float:
    try:
        return float(workspace.load_settings().get("jlc_extended_fee") or 3.0)
    except ValueError:
        return 3.0


def shortlist(parts: list[dict], qty: int, boards: int, package: str | None, keep: int = KEEP) -> list[dict]:
    """Rank everything, fill in stock and price breaks for the leaders, then rank those again.
    Blocking (network): call via asyncio.to_thread."""
    first = jlc.rank(parts, qty, boards, extended_fee(), package)
    return jlc.rank(jlc.enrich(first[:keep + 6]), qty, boards, extended_fee(), package)[:keep]


def _slim(c: dict) -> dict:
    return {k: c.get(k) for k in ("rank", "lcsc", "mpn", "package", "kind", "stock", "stock_state", "unit_price",
                                  "order_cost", "need", "package_ok", "why", "description", "category")}


async def find_candidates(pid: str, *, name: str, qty: int = 1, category: str | None = None,
                          filters: dict | None = None, query: str = "", package: str | None = None,
                          notes: str = "", refs: list | None = None, role_id: str | None = None,
                          basic_only: bool = False, subcategory: str | None = None) -> dict:
    data = phases.components(pid)
    res = await asyncio.to_thread(jlc.search, query, category, filters, package, basic_only, 100, subcategory)
    ranked = await asyncio.to_thread(shortlist, res["parts"], qty, int(data.get("boards") or 5), package)
    role = {"name": name, "qty": qty, "subcategory": subcategory, "category": category, "filters": filters or {}, "query": query,
            "package": package, "notes": notes, "candidates": [_slim(c) for c in ranked[:KEEP]],
            "searched_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "search_notes": res["notes"]}
    if refs is not None:
        role["refs"] = refs
    if role_id:
        role["id"] = role_id
    saved = phases.upsert_role(pid, role)
    return {"role_id": saved["id"], "name": name, "found": res["total"], "source": jlc.SOURCE,
            "notes": res["notes"], "boards": data.get("boards"), "top": saved["candidates"][:6]}


async def lookup_role(pid: str, role: dict) -> dict:
    """A role that only names an LCSC number (e.g. from a schematic's LCSC field): look that part up and rank it alone,
    so the user sees its stock, Basic/Extended and order cost. Alternatives need a proper search (subcategory + package)."""
    data = phases.components(pid)
    part = await asyncio.to_thread(jlc.lookup, role["query"])
    ranked = jlc.rank([part], int(role.get("qty") or 1), int(data.get("boards") or 5), extended_fee()) if part else []
    saved = phases.upsert_role(pid, {"id": role["id"], "candidates": [_slim(c) for c in ranked],
                                     "searched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                     "search_notes": [] if part else [f"{role['query']} is not in the JLC catalogue snapshot (LCSC number lookup)."]})
    return {"role_id": saved["id"], "name": saved["name"], "found": len(ranked), "source": jlc.SOURCE,
            "notes": saved["search_notes"], "boards": data.get("boards"), "top": saved["candidates"]}


def choose(pid: str, role_id: str, lcsc: str | None) -> dict:
    data = phases.components(pid)
    r = next((x for x in data["roles"] if x["id"] == role_id), None)
    if r is None:
        raise ValueError(f"no role {role_id!r}; existing: {[x['id'] for x in data['roles']]}")
    if lcsc and not any(c["lcsc"] == lcsc for c in r.get("candidates", [])):
        raise ValueError(f"{lcsc} is not one of the ranked candidates for {role_id}; search again or use jlc_part to add it")
    return phases.upsert_role(pid, {"id": role_id, "chosen": lcsc})


async def check_bom(pid: str, pcb, doc, create_roles: bool = False) -> dict:
    """Look up every BOM line's LCSC number on JLC: stock against qty x boards, Basic/Extended, price.
    With create_roles, each line becomes a phase-1 role with its current part as the chosen candidate."""
    rows = await pcb(lambda: board.bom(doc()))
    data = phases.components(pid)
    boards = int(data.get("boards") or 5)
    fee = extended_fee()
    sem = asyncio.Semaphore(4)

    async def one(r):
        if r["dnp"]:
            return {**_line(r), "status": "dnp"}
        if not r["lcsc"]:
            return {**_line(r), "status": "no LCSC field"}
        async with sem:
            try:
                p = await asyncio.to_thread(jlc.lookup, r["lcsc"])
            except jlc.JlcError as e:
                return {**_line(r), "status": f"lookup failed: {e}"}
        if p is None:
            return {**_line(r), "status": "not in JLC catalogue snapshot"}
        c = jlc.rank([p], r["qty"], boards, fee)[0]
        status = {"ok": "ok", "short": "SHORT", "unknown": "stock unknown"}[c["stock_state"]]
        return {**_line(r), "status": status, "part": _slim(c)}

    lines = await asyncio.gather(*(one(r) for r in rows))
    if create_roles:
        existing = {tuple(r.get("refs") or []): r["id"] for r in phases.components(pid)["roles"]}
        for ln in lines:
            if "part" in ln:  # a second run updates the same roles instead of adding "-2" copies
                phases.upsert_role(pid, {"id": existing.get(tuple(ln["refs"])), "name": f"{ln['value']} ({ln['refs'][0]}{'…' if len(ln['refs']) > 1 else ''})",
                                         "qty": ln["qty"], "query": ln["mpn"] or ln["value"], "refs": ln["refs"],
                                         "candidates": [{**ln["part"], "rank": 1}], "chosen": ln["part"]["lcsc"],
                                         "notes": "from the board BOM"})
    counts: dict[str, int] = {}
    for ln in lines:
        k = ln["status"] if not ln["status"].startswith("lookup failed") else "lookup failed"
        counts[k] = counts.get(k, 0) + 1
    extended = sorted({ln["part"]["lcsc"] for ln in lines if ln.get("part") and ln["part"]["kind"] == "Extended"})
    return {"source": jlc.SOURCE, "boards": boards, "lines": len(lines), "counts": counts,
            "extended_parts": len(extended), "extended_fees_usd": round(len(extended) * fee, 2),
            "rows": lines, "note": "Snapshot data: confirm stock and price on jlcpcb.com when ordering."}


def _line(r: dict) -> dict:
    return {"refs": r["refs"], "qty": r["qty"], "value": r["value"], "mpn": r["mpn"], "lcsc": r["lcsc"],
            "footprint": r["footprint"]}
