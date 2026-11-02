"""End-to-end check of the phased flow against a running PCB Studio server (stdlib only).

  python tests/e2e_flow.py [--name e2e-tiny] [--shape rect] [--w 30] [--h 22] [--rounds 3] [--max-time 20]

Creates a new design, imports tests/fixtures/tiny_ldo_mcu.net, approves Phase 1 (force: the fixture has no LCSC
part for J1, U2, J2), sets the outline, builds, places, checks DRC, approves Phase 2, runs the routing loop,
prints every round, and adopts the best candidate. Needs fastroute configured in Settings.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def call(base, method, path, body=None):
    req = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} -> {e.code}: {e.read().decode(errors='replace')[:400]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8766")
    ap.add_argument("--name", default="e2e-tiny")
    ap.add_argument("--shape", default="rect")
    ap.add_argument("--w", type=float, default=30)
    ap.add_argument("--h", type=float, default=22)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--max-time", type=int, default=20)
    a = ap.parse_args()
    B = a.base
    p = call(B, "POST", "/api/projects/new", {"name": a.name})
    pid = p["id"]
    print("project", pid)
    nl = call(B, "POST", f"/api/projects/{pid}/netlist/import", {"path": str(HERE / "fixtures" / "tiny_ldo_mcu.net")})["netlist"]
    print(f"netlist: {nl['parts']} parts, {nl['nets']} nets, ok={nl['ok']}, issues={len(nl['issues'])}")
    assert nl["ok"]
    call(B, "POST", f"/api/projects/{pid}/phases/components/approve", {"force": True, "summary": "e2e"})
    call(B, "POST", f"/api/projects/{pid}/layout/outline", {"shape": a.shape, "width_mm": a.w, "height_mm": a.h, "corner_mm": 1})
    b = call(B, "POST", f"/api/projects/{pid}/layout/build", {})
    print(f"built: {b['parts']} parts, power nets {b['power_nets']}")
    pl = call(B, "POST", f"/api/projects/{pid}/layout/place", {"moves": 20000})
    print("placer", json.dumps(pl["before"]), "->", json.dumps(pl["after"]), f"{pl['seconds']} s")
    print("placement DRC", json.dumps(pl["check"]))
    call(B, "POST", f"/api/projects/{pid}/phases/placement/approve", {})
    job = call(B, "POST", f"/api/projects/{pid}/routing/start", {"rounds": a.rounds, "max_time": a.max_time})
    jid = job["id"]
    while True:
        time.sleep(3)
        j = call(B, "GET", f"/api/projects/{pid}/routing/{jid}")
        if j["status"] not in ("running", "queued"):
            break
    print("\n".join(j["log"]))
    for c in j["candidates"]:
        print(c["id"], json.dumps(c["score"]))
    if j["status"] == "failed" or not j["candidates"]:
        raise SystemExit(f"routing {j['status']}: {j.get('error')}")
    ad = call(B, "POST", f"/api/projects/{pid}/routing/{jid}/{j['best']}/adopt")
    print("adopted", ad)
    best = j["candidates"][0]["score"]
    print("RESULT", "CLEAN" if best["clean"] else "NOT CLEAN", json.dumps(best))
    return 0


if __name__ == "__main__":
    sys.exit(main())
