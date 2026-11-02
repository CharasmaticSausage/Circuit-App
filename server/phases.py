"""The chronological design flow: 1 components -> 2 placement -> 3 routing.

Each project keeps phases.json. A phase is "locked" until the one before it is approved, then
"active"; approving it freezes its output and unlocks the next. Reopening an approved phase makes
every later phase "stale" (its inputs changed), so the user re-runs and re-approves them in order.

Phase 1 data (roles and ranked JLC candidates) lives in components.json.
"""
from __future__ import annotations

import json
import os
import re
import time

from . import workspace

ORDER = ["components", "placement", "routing"]
TITLES = {"components": "1 Components", "placement": "2 Placement", "routing": "3 Routing"}


def _read(pid: str, name: str, default):
    f = workspace.project_dir(pid) / name
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(pid: str, name: str, data) -> None:
    f = workspace.project_dir(pid) / name
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, f)


# ---- phase state -----------------------------------------------------------------------------
def state(pid: str) -> dict:
    s = _read(pid, "phases.json", None)
    if not s:
        s = {"phases": {k: {"status": "active" if i == 0 else "locked"} for i, k in enumerate(ORDER)}}
        _write(pid, "phases.json", s)
    s["order"] = ORDER
    s["titles"] = TITLES
    s["current"] = next((k for k in ORDER if s["phases"][k]["status"] in ("active", "stale")), ORDER[-1])
    return s


def approve(pid: str, phase: str, summary: str = "") -> dict:
    s = state(pid)
    if phase not in ORDER:
        raise ValueError(f"unknown phase {phase!r}")
    st = s["phases"][phase]["status"]
    if st == "locked":
        raise ValueError(f"{TITLES[phase]} is locked: approve the earlier phases first")
    s["phases"][phase] = {"status": "approved", "approved_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "summary": summary}
    i = ORDER.index(phase)
    if i + 1 < len(ORDER) and s["phases"][ORDER[i + 1]]["status"] == "locked":
        s["phases"][ORDER[i + 1]]["status"] = "active"
    _write(pid, "phases.json", {"phases": s["phases"]})
    return state(pid)


def reopen(pid: str, phase: str) -> dict:
    s = state(pid)
    if phase not in ORDER:
        raise ValueError(f"unknown phase {phase!r}")
    s["phases"][phase]["status"] = "active"
    for later in ORDER[ORDER.index(phase) + 1:]:
        if s["phases"][later]["status"] != "locked":
            s["phases"][later]["status"] = "stale"
    _write(pid, "phases.json", {"phases": s["phases"]})
    return state(pid)


def require(pid: str, phase: str) -> None:
    """Refuse work on a phase whose predecessors are not approved (keeps the flow chronological)."""
    s = state(pid)
    for earlier in ORDER[:ORDER.index(phase)]:
        if s["phases"][earlier]["status"] != "approved":
            raise ValueError(f"{TITLES[phase]} needs {TITLES[earlier]} approved first")


# ---- phase 1: components ----------------------------------------------------------------------
def components(pid: str) -> dict:
    return _read(pid, "components.json", {"requirements": "", "boards": 5, "roles": []})


def save_components(pid: str, data: dict) -> None:
    _write(pid, "components.json", data)


def _role_id(name: str, roles: list) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "part"
    rid, k = base, 2
    while any(r["id"] == rid for r in roles):
        rid, k = f"{base}-{k}", k + 1
    return rid


def upsert_role(pid: str, role: dict) -> dict:
    """Add or update one role (a needed part: "3.3 V LDO", "main MCU"). Changing phase-1 data while
    later phases are approved reopens phase 1."""
    data = components(pid)
    roles = data["roles"]
    r = next((x for x in roles if x["id"] == role.get("id")), None)
    if r is None:
        r = {"id": _role_id(role.get("name", "part"), roles), "candidates": [], "chosen": None}
        roles.append(r)
    for k in ("name", "qty", "subcategory", "category", "filters", "query", "package", "notes", "refs", "candidates", "chosen",
              "searched_at", "search_notes"):
        if k in role:
            r[k] = role[k]
    save_components(pid, data)
    if state(pid)["phases"]["components"]["status"] == "approved":
        reopen(pid, "components")
    return r
