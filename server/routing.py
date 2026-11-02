"""Phase 3: the routing loop. route -> DRC -> feed back into placement -> route again, N rounds.

Each round works on copies in <project>/routing/<job>/, never on the project board:
  r<k>_in.kicad_pcb   the placement routed this round (round 1: the project board; later rounds: the
                      previous placement refined with feedback from the previous round's DRC)
  r<k>.dsn / .ses     Specctra export from KiCad / session back from the router
  r<k>.kicad_pcb      the routed candidate (KiCad's own SES import), checked with kicad-cli DRC
Candidates are ranked lexicographically: hard DRC errors, then unrouted connections, then vias, then
track length. The user previews them and adopts one (the project board is snapshotted first, so undo
works); approving Phase 3 is a separate, explicit step.

The router is fastroute (a separate GPL-3.0 program, configured in Settings, never bundled). Only one
fastroute runs on this machine at a time: the job takes PCB Studio's lock and, if present, the
`tally_fastroute.lck` lock other local routing queues use, without waiting. If either is held the job
fails with "router busy" instead of queueing. Cancel sends Ctrl+Break, which makes fastroute save its
best result so far; a second cancel kills it.
"""
from __future__ import annotations

import asyncio
import json
import msvcrt
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pcbnew

from . import drc, fab, layout, workspace

SIDECARS = (".kicad_pro", ".kicad_dru")
_job: dict | None = None  # the one running job (pid, id, task, proc, cancel)


# ---- router discovery and the machine-wide lock ---------------------------------------------------
def fastroute_exe() -> str | None:
    s = (workspace.load_settings().get("fastroute_exe") or "").strip()
    if s and Path(s).is_file():
        return s
    return shutil.which("fastroute")


def _lock_files() -> list[Path]:
    la = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    files = [workspace.DATA / "fastroute.lck"]
    shared = la / "tally_fastroute.lck"  # other local fastroute queues (see module doc)
    if shared.exists():
        files.append(shared)
    return files


class RouterBusy(RuntimeError):
    pass


class _Locks:
    def __init__(self, label: str):
        self.label, self.held = label, []

    def __enter__(self):
        for p in _lock_files():
            p.parent.mkdir(parents=True, exist_ok=True)
            f = open(p, "a+b")
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                f.close()
                who = "?"
                try:
                    who = p.with_suffix(".owner.txt").read_text(errors="replace").strip()
                except OSError:
                    pass
                self.__exit__()
                raise RouterBusy(f"router busy: {p.name} is held ({who}). Try again when that run finishes.")
            self.held.append((p, f))
            try:
                p.with_suffix(".owner.txt").write_text(f"{os.getpid()} {self.label} {time.strftime('%H:%M:%S')}\n")
            except OSError:
                pass
        return self

    def __exit__(self, *exc):
        for p, f in reversed(self.held):
            try:
                p.with_suffix(".owner.txt").write_text(f"free since {time.strftime('%H:%M:%S')} (last: {self.label})\n")
            except OSError:
                pass
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            f.close()
        self.held = []


# ---- job files ---------------------------------------------------------------------------------
def _root(pid: str) -> Path:
    return workspace.project_dir(pid) / "routing"


def _jdir(pid: str, jid: str) -> Path:
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}", jid or ""):
        raise ValueError("bad job id")
    return _root(pid) / jid


def _save(pid: str, job: dict) -> None:
    d = _jdir(pid, job["id"])
    tmp = d / "job.tmp"
    tmp.write_text(json.dumps({k: v for k, v in job.items() if not k.startswith("_")}, indent=1), encoding="utf-8")
    os.replace(tmp, d / "job.json")


def load(pid: str, jid: str) -> dict:
    return json.loads((_jdir(pid, jid) / "job.json").read_text(encoding="utf-8"))


def jobs(pid: str) -> list[dict]:
    out = []
    root = _root(pid)
    if root.is_dir():
        for d in sorted(root.iterdir(), reverse=True):
            try:
                out.append(json.loads((d / "job.json").read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
    return out


def mark_interrupted() -> None:
    """At server start: a job that was running when the server stopped will never finish."""
    if not workspace.PROJECTS.is_dir():
        return
    for jf in workspace.PROJECTS.glob("*/routing/*/job.json"):
        try:
            j = json.loads(jf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if j.get("status") in ("queued", "running"):
            j["status"] = "interrupted"
            j["log"] = j.get("log", []) + [f"{time.strftime('%H:%M:%S')} server stopped during the run"]
            jf.write_text(json.dumps(j, indent=1), encoding="utf-8")


def _copy_board(src: Path, dst: Path) -> None:
    shutil.copy2(src, dst)
    for ext in SIDECARS:
        s = src.with_suffix(ext)
        if s.is_file():
            if ext == ".kicad_pro":  # the project file names its board; keep it consistent
                try:
                    data = json.loads(s.read_text(encoding="utf-8"))
                    data.setdefault("meta", {})["filename"] = dst.with_suffix(".kicad_pro").name
                    dst.with_suffix(ext).write_text(json.dumps(data, indent=2), encoding="utf-8")
                    continue
                except ValueError:
                    pass
            shutil.copy2(s, dst.with_suffix(ext))


# ---- scoring --------------------------------------------------------------------------------------
_REF = re.compile(r"\bof ([A-Za-z]+\d+[A-Za-z0-9]*)")


def _internal(item: dict) -> bool:
    """A violation between two parts of the same footprint (library footprint vs board rules), which no
    placement or routing can fix. Reported separately, not scored."""
    texts = [l.get("text", "") for l in item["items"]]
    refs = {r for t in texts for r in _REF.findall(t)}
    return len(refs) == 1 and all(_REF.search(t) for t in texts) and not any(
        t.startswith(("Track", "Via", "Zone")) for t in texts)


def score(d: dict, report: dict | None) -> dict:
    hard = [i for i in d["items"] if i["kind"] == "violation" and i["severity"] == "error"]
    internal = [i for i in hard if _internal(i)]
    st = (report or {}).get("stats", {})
    s = {"errors": len(hard) - len(internal), "footprint_internal": len(internal),
         "unconnected": d["counts"]["unconnected"], "warnings": d["counts"]["warnings"],
         "vias": st.get("vias"), "length_mm": round(st.get("trace_length_mm") or 0, 1),
         "router_unrouted": st.get("unrouted"), "router_violations": st.get("violations")}
    s["key"] = [s["errors"], s["unconnected"], s["vias"] or 0, s["length_mm"]]
    s["clean"] = s["errors"] == 0 and s["unconnected"] == 0
    return s


# ---- the job ---------------------------------------------------------------------------------------
def current() -> dict | None:
    return None if _job is None else {"pid": _job["pid"], "id": _job["id"]}


def _log(pid, job, text):
    job.setdefault("log", []).append(f"{time.strftime('%H:%M:%S')} {text}")
    job["log"] = job["log"][-300:]
    _save(pid, job)


async def start(pid: str, pcb, emit=None, rounds: int | None = None, max_time: int | None = None,
                passes: int = 40, profile_id: str | None = None) -> dict:
    global _job
    if _job is not None:
        raise RouterBusy(f"a routing job is already running ({_job['pid']} / {_job['id']})")
    exe = fastroute_exe()
    if not exe:
        raise RuntimeError("fastroute not found: set its path in Settings (fastroute is a separate GPL-3.0 "
                           "program, not shipped with PCB Studio)")
    s = workspace.load_settings()
    rounds = max(1, min(int(rounds or s.get("route_rounds") or 3), 10))
    max_time = max(5, min(int(max_time or s.get("route_max_time") or 60), 3600))
    jid = time.strftime("%Y%m%d-%H%M%S")
    d = _root(pid) / jid
    d.mkdir(parents=True)
    job = {"id": jid, "pid": pid, "status": "running", "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "rounds": rounds, "max_time": max_time, "passes": passes, "round": 0, "candidates": [], "log": [],
           "fab_profile": profile_id or fab.DEFAULT, "router": Path(exe).name}
    _save(pid, job)
    _job = {"pid": pid, "id": jid, "proc": None, "cancel": 0, "task": None}
    _job["task"] = asyncio.create_task(_run(pid, job, pcb, exe, emit))
    return job


async def cancel(pid: str, jid: str) -> dict:
    if _job is None or _job["pid"] != pid or _job["id"] != jid:
        raise ValueError("that job is not running")
    _job["cancel"] += 1
    proc = _job.get("proc")
    if proc and proc.returncode is None:
        try:
            if _job["cancel"] == 1:
                proc.send_signal(signal.CTRL_BREAK_EVENT)  # fastroute saves its best result so far
            else:
                proc.kill()
        except (ProcessLookupError, OSError):
            pass
    return {"cancelling": True, "second_cancel_kills": _job["cancel"] == 1}


def shutdown() -> None:
    if _job and _job.get("proc") and _job["proc"].returncode is None:
        try:
            _job["proc"].kill()
        except (ProcessLookupError, OSError):
            pass


async def _run(pid, job, pcb, exe, emit):
    global _job
    d = _jdir(pid, job["id"])
    src = Path(workspace.board_path(pid))
    gap, weights = {}, {}

    async def note(text):
        _log(pid, job, text)
        if emit:
            try:
                await emit({"type": "routing", "id": job["id"], "status": job["status"], "round": job["round"], "text": text})
            except Exception:
                pass

    try:
        for k in range(1, job["rounds"] + 1):
            if _job["cancel"]:
                break
            job["round"] = k
            inp = d / f"r{k}_in.kicad_pcb"
            if k == 1:
                _copy_board(src, inp)
                await note(f"round {k}: routing the current placement")
            else:
                prev = d / f"r{k - 1}_in.kicad_pcb"
                _copy_board(prev, inp)
                res = await pcb(_refine, str(inp), job["fab_profile"], gap, weights, k)
                await note(f"round {k}: re-placed with feedback (moved {len(res['moved'])} parts; "
                           f"wirelength {res['before']['wirelength_mm']} -> {res['after']['wirelength_mm']} mm)")
            dsn, ses, rep = d / f"r{k}.dsn", d / f"r{k}.ses", d / f"r{k}_report.json"
            ok = await pcb(lambda: pcbnew.ExportSpecctraDSN(pcbnew.LoadBoard(str(inp)), str(dsn)))
            if not ok or not dsn.is_file():
                raise RuntimeError("KiCad's Specctra (DSN) export failed")
            report = await _route(pid, job, exe, dsn, ses, rep, note)
            if not ses.is_file():
                await note(f"round {k}: the router produced no session file")
                break
            out = d / f"r{k}.kicad_pcb"
            _copy_board(inp, out)
            ok = await pcb(_import_ses, str(out), str(ses))
            if not ok:
                raise RuntimeError("KiCad could not import the routed session (.ses)")
            await note(f"round {k}: running DRC on the routed board")
            dres = await drc.run_file(str(out))
            (d / f"r{k}_drc.json").write_text(json.dumps(dres), encoding="utf-8")
            sc = score(dres, report)
            job["candidates"].append({"id": f"r{k}", "round": k, "board": out.name, "score": sc,
                                      "drc_by_type": dres["by_type"], "partial": bool(_job["cancel"])})
            job["candidates"].sort(key=lambda c: c["score"]["key"])
            job["best"] = job["candidates"][0]["id"]
            await note(f"round {k}: {sc['errors']} DRC errors, {sc['unconnected']} unconnected, {sc['vias']} vias, "
                       f"{sc['length_mm']} mm" + (f" ({sc['footprint_internal']} inside footprints, not scored)"
                                                  if sc["footprint_internal"] else ""))
            if sc["clean"]:
                await note(f"round {k} is clean (0 errors, 0 unconnected): stopping early")
                break
            gap, weights, hit = layout.feedback(dres, gap, weights)
            if k < job["rounds"]:
                await note(f"feedback: more room around {', '.join(sorted(hit['refs'])[:12]) or 'nothing'}; "
                           f"heavier nets {', '.join(sorted(hit['nets'])[:12]) or 'none'}")
        job["status"] = "cancelled" if _job["cancel"] else "done"
        if len(job["candidates"]) > 1:
            first = next(c for c in job["candidates"] if c["round"] == 1)
            best = job["candidates"][0]
            job["feedback_helped"] = best["round"] != 1 and best["score"]["key"] < first["score"]["key"]
        await note(f"finished: {len(job['candidates'])} candidate(s); best {job.get('best', '-')}"
                   + ("" if job.get("feedback_helped", True) else " (the feedback rounds did not beat round 1)"))
    except Exception as e:  # noqa: BLE001 - surface every failure in the job log
        job["status"] = "failed"
        job["error"] = str(e)
        await note(f"failed: {e}")
    finally:
        _save(pid, job)
        _job = None
        if emit:
            try:
                await emit({"type": "routing", "id": job["id"], "status": job["status"], "done": True})
            except Exception:
                pass


async def _route(pid, job, exe, dsn, ses, rep, note) -> dict | None:
    args = [exe, "-de", str(dsn), "-do", str(ses), "-mp", str(job["passes"]), f"--max-time={job['max_time']}",
            f"--report={rep}"]
    with _Locks(f"pcbstudio {pid} {job['id']} r{job['round']}"):
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT,
                                                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        _job["proc"] = proc
        last = 0.0
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line and time.time() - last > 3 and ("INFO" in line or "WARN" in line or "ERROR" in line):
                last = time.time()
                job["router_line"] = re.sub(r"^\s*[\d.]+\s+", "", line)[:200]
                _save(pid, job)
        await proc.wait()
        _job["proc"] = None
    await note(f"router exit {proc.returncode}")
    try:
        return json.loads(rep.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _import_ses(board_file: str, ses: str) -> bool:
    b = pcbnew.LoadBoard(board_file)
    ok = pcbnew.ImportSpecctraSES(b, ses)
    if ok:
        pcbnew.SaveBoard(board_file, b)
    return bool(ok)


def _refine(board_file: str, profile_id: str, gap: dict, weights: dict, seed: int) -> dict:
    b = pcbnew.LoadBoard(board_file)
    pl = layout.Placer(b, fab.get(profile_id), extra_gap=gap, net_weight=weights)
    res = pl.run(moves=6000, t_start=0.5, seed=seed)
    res["moved"] = pl.apply(b)
    res["references"] = layout.place_references(b)
    pcbnew.SaveBoard(board_file, b)
    return res


def candidate_path(pid: str, jid: str, cid: str) -> Path:
    if not re.fullmatch(r"r\d{1,2}", cid or ""):
        raise ValueError("bad candidate id")
    p = _jdir(pid, jid) / f"{cid}.kicad_pcb"
    if not p.is_file():
        raise ValueError(f"no candidate {cid} in job {jid}")
    return p
