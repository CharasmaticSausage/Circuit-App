"""PCB Studio server: board API, BOM, DRC and the design-assistant chat (SSE).

Run with `python -m server` from the pcbstudio folder (see run.bat). Binds to 127.0.0.1 only.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import board, changelist, drc, fab, home, jlc, layout, netlist, ops, phases, routing, selection, tools, workspace
from .providers import anthropic_provider, claude_code_provider, common, openai_provider

# id -> (module, label, settings key holding that provider's model)
PROVIDERS = {
    "claude_code": (claude_code_provider, "Claude Code (your Claude plan)", "cc_model"),
    "anthropic": (anthropic_provider, "Anthropic API (pay per token)", "model"),
    "openai": (openai_provider, "OpenAI-compatible (Ollama, LM Studio, OpenRouter)", "openai_model"),
}

WEB = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="PCB Studio", docs_url=None, redoc_url=None)

# pcbnew is not thread safe: every call into it runs on this one thread.
_PCB = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pcbnew")
_docs: dict[str, board.BoardDoc] = {}
_busy: set[str] = set()  # projects with a chat turn in flight
_running: set = set()  # strong refs so in-flight chat tasks aren't garbage collected
_turns: dict[str, dict] = {}  # pid -> {"token", "ctx"} while a Claude Code turn may call tools


async def pcb(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(_PCB, fn, *args)


def _doc(pid: str) -> board.BoardDoc:
    """Open (or reuse) the project's board. Call only on the pcbnew thread."""
    d = _docs.get(pid)
    if d is None:
        d = board.BoardDoc(workspace.board_path(pid), str(workspace.project_dir(pid) / ".snapshots"))
        _docs[pid] = d
    return d


# ---- localhost-only guard ---------------------------------------------------------------
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    if host not in _LOCAL_HOSTS:  # blocks DNS rebinding from other sites
        return Response("forbidden host", status_code=403)
    origin = request.headers.get("origin")
    if origin and request.method not in ("GET", "HEAD"):
        o = origin.split("://", 1)[-1].rsplit(":", 1)[0]
        if o not in _LOCAL_HOSTS:
            return Response("forbidden origin", status_code=403)
    resp = await call_next(request)
    if not request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache"  # always revalidate app.js etc. after an update
    return resp


def _bad(e: Exception, code: int = 400):
    raise HTTPException(code, str(e))


# ---- settings, providers & models --------------------------------------------------------
def _provider(settings: dict, requested: str | None = None) -> str:
    p = requested or settings.get("provider") or ""
    if p in PROVIDERS:
        return p
    return "claude_code" if claude_code_provider.available(settings) else "anthropic"


@app.get("/api/settings")
async def get_settings():
    s = workspace.load_settings()
    return {**workspace.public_settings(), "kicad_cli_found": workspace.kicad_cli(),
            "claude_cli_found": claude_code_provider.cli_path(s), "fastroute_found": routing.fastroute_exe(),
            "provider_active": _provider(s)}


@app.post("/api/settings")
async def post_settings(request: Request):
    body = await request.json()
    for k in ("anthropic_api_key", "openai_api_key"):
        if k in body and body[k] is None:
            body[k] = ""
    workspace.save_settings({k: v for k, v in body.items() if isinstance(v, str)})
    return await get_settings()


@app.get("/api/providers")
async def providers():
    s = workspace.load_settings()
    return {"active": _provider(s), "providers": [
        {"id": k, "name": label, "available": mod.available(s), "model": s.get(key, "")}
        for k, (mod, label, key) in PROVIDERS.items()]}


@app.get("/api/models")
async def models(provider: str | None = None):
    s = workspace.load_settings()
    p = _provider(s, provider)
    return {"provider": p, "models": await PROVIDERS[p][0].list_models(s)}


# ---- tool bridge for Claude Code (called by server/mcp_bridge.py) -------------------------
@app.get("/api/tools")
async def tool_list():
    return [{"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]} for t in tools.TOOLS]


@app.post("/api/projects/{pid}/tool")
async def tool_call(pid: str, request: Request):
    turn = _turns.get(pid)
    given = request.headers.get("x-pcbstudio-token", "")
    if not turn or not secrets.compare_digest(given, turn["token"]):
        raise HTTPException(403, "no chat turn is running for this project")
    body = await request.json()
    ctx, name = turn["ctx"], body.get("name", "")
    queued = ctx.pending.get(name)
    call_id = queued.pop(0) if queued else secrets.token_hex(4)  # match the chip the stream already announced
    ok, res = await common.run_tool(ctx, call_id, name, body.get("arguments"))
    return {"ok": ok, "result": res}


# ---- projects ----------------------------------------------------------------------------
@app.get("/api/projects")
async def projects():
    return workspace.list_projects()


@app.get("/api/home")
async def home_list():
    return await asyncio.to_thread(home.summaries)


@app.post("/api/projects/{pid}/home/price")
async def home_price(pid: str):
    try:
        return await home.price(pid)
    except ValueError as e:
        _bad(e, 404)


@app.get("/api/projects/{pid}/thumb.svg")
async def home_thumb(pid: str):
    try:
        f = await home.thumbnail(pid)
    except ValueError as e:
        _bad(e, 404)
    if not f:
        raise HTTPException(404, "no board to draw yet")
    return FileResponse(f, media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})


@app.get("/api/browse")
async def browse(path: str | None = None, kind: str = "board"):
    exts = {"board": (".kicad_pcb",), "netlist": (".kicad_sch", ".net")}.get(kind, (".kicad_pcb",))
    return workspace.browse(path, exts)


@app.post("/api/projects/new")
async def new_project(request: Request):
    """Start a design from nothing: Phase 1 (requirements, parts, netlist) comes before any board."""
    body = await request.json()
    p = workspace.create_project(body.get("name", ""))

    def blank():
        import pcbnew
        b = pcbnew.CreateEmptyBoard()
        b.SetCopperLayerCount(2)
        pcbnew.SaveBoard(p["board_path"], b)
        fab.write_pro(Path(p["board_path"]), fab.get(None))
    await pcb(blank)
    return p


@app.post("/api/projects/import")
async def import_project(request: Request):
    body = await request.json()
    try:
        return workspace.import_board(body.get("path", ""), body.get("name") or None)
    except ValueError as e:
        _bad(e)


@app.get("/api/projects/{pid}/board")
async def get_board(pid: str):
    try:
        data = await pcb(lambda: _doc(pid).data())
    except ValueError as e:
        _bad(e, 404)
    return Response(json.dumps(data, separators=(",", ":")), media_type="application/json")


@app.get("/api/projects/{pid}/rev")
async def get_rev(pid: str):
    def f():
        d = _doc(pid)
        d.sync_from_disk()
        return {"rev": d.rev, "can_undo": bool(d.undo)}
    return await pcb(f)


@app.get("/api/projects/{pid}/bom")
async def get_bom(pid: str):
    return await pcb(lambda: board.bom(_doc(pid)))


@app.post("/api/projects/{pid}/drc")
async def post_drc(pid: str, refill: bool = False):
    try:
        return await drc.run(pid, refill)
    except (RuntimeError, ValueError) as e:
        _bad(e, 500)


@app.get("/api/projects/{pid}/drc")
async def get_drc(pid: str):
    return drc.last(pid) or {}


@app.get("/api/projects/{pid}/download")
async def download(pid: str):
    """The working copy with every accepted edit, as a .kicad_pcb file."""
    path = Path(workspace.board_path(pid))
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@app.post("/api/projects/{pid}/open-kicad")
async def open_kicad(pid: str):
    """Open the working copy in KiCad's PCB editor. Saves made there show up here (mtime reload)."""
    cli = workspace.kicad_cli()
    exe = Path(cli).with_name("pcbnew.exe") if cli else None
    if not exe or not exe.is_file():
        _bad(ValueError("KiCad's pcbnew.exe was not found next to kicad-cli. Set the kicad-cli path in Settings."), 404)
    path = workspace.board_path(pid)
    subprocess.Popen([str(exe), path], cwd=str(Path(path).parent), close_fds=True,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    return {"opened": path}


@app.post("/api/projects/{pid}/undo")
async def undo(pid: str):
    try:
        return await pcb(lambda: _doc(pid).undo_last())
    except board.EditError as e:
        _bad(e)


@app.post("/api/projects/{pid}/move")
async def move(pid: str, request: Request):
    """Drag-to-move from the viewer."""
    b = await request.json()
    try:
        return await pcb(lambda: board.move_component(_doc(pid), b["ref"], b.get("x_mm"), b.get("y_mm"),
                                                      b.get("rotation_deg"), b.get("side")))
    except (board.EditError, KeyError) as e:
        _bad(e)


@app.get("/api/changelist")
async def get_changelist(format: str = "json"):
    data = changelist.items()
    if format == "text":
        return Response(data["text"], media_type="text/plain; charset=utf-8")
    return data


# ---- phases (1 components -> 2 placement -> 3 routing) -------------------------------------
@app.get("/api/projects/{pid}/phases")
async def phase_state(pid: str):
    return {"flow": phases.state(pid), "components": phases.components(pid), "jlc_source": jlc.SOURCE,
            "extended_fee": selection.extended_fee()}


@app.post("/api/projects/{pid}/phases/{phase}/approve")
async def phase_approve(pid: str, phase: str, request: Request):
    """Approval is a user action only; the model has no tool for it."""
    body = await request.json() if await request.body() else {}
    if phase == "components":
        unchosen = [r["name"] for r in phases.components(pid)["roles"] if not r.get("chosen")]
        if unchosen and not body.get("force"):
            _bad(ValueError(f"no part chosen for: {', '.join(unchosen)}"))
        why = ops.check_components_ready(pid)
        if why:
            _bad(ValueError("; ".join(why)))
        nl = netlist.load(pid)
        if nl:
            nl["approved"] = True
            phases._write(pid, "netlist.json", nl)
    if phase == "placement":
        info = await pcb(lambda: {"parts": len(_doc(pid).board.GetFootprints()),
                                  "outline": layout.board_outline(_doc(pid).board) is not None})
        if not info["parts"] or not info["outline"]:
            _bad(ValueError("build the board first: it needs an outline and parts"))
    return phases.approve(pid, phase, body.get("summary", ""))


@app.post("/api/projects/{pid}/phases/{phase}/reopen")
async def phase_reopen(pid: str, phase: str):
    return phases.reopen(pid, phase)


@app.post("/api/projects/{pid}/components")
async def components_update(pid: str, request: Request):
    """Requirements text and board quantity, from the Components panel."""
    b = await request.json()
    data = phases.components(pid)
    if "requirements" in b:
        data["requirements"] = str(b["requirements"])
    if "boards" in b:
        data["boards"] = max(1, int(b["boards"]))
    phases.save_components(pid, data)
    return data


@app.post("/api/projects/{pid}/components/{role_id}/choose")
async def components_choose(pid: str, role_id: str, request: Request):
    return selection.choose(pid, role_id, (await request.json()).get("lcsc") or None)


@app.post("/api/projects/{pid}/components/{role_id}/research")
async def components_research(pid: str, role_id: str):
    """Re-run a role's saved search (fresh stock and prices once the cache expires)."""
    r = next((x for x in phases.components(pid)["roles"] if x["id"] == role_id), None)
    if r is None:
        _bad(ValueError(f"no role {role_id!r}"), 404)
    try:
        if not r.get("category") and not r.get("subcategory") and re.fullmatch(r"C\d+", r.get("query") or ""):
            return await selection.lookup_role(pid, r)
        return await selection.find_candidates(pid, name=r["name"], qty=r.get("qty", 1), category=r.get("category"),
                                               subcategory=r.get("subcategory"),
                                               filters=r.get("filters"), query=r.get("query", ""),
                                               package=r.get("package"), notes=r.get("notes", ""), role_id=role_id)
    except jlc.JlcError as e:
        _bad(e, 502)


@app.delete("/api/projects/{pid}/components/{role_id}")
async def components_delete(pid: str, role_id: str):
    data = phases.components(pid)
    data["roles"] = [r for r in data["roles"] if r["id"] != role_id]
    phases.save_components(pid, data)
    return data


@app.post("/api/projects/{pid}/bom-check")
async def bom_check(pid: str, create_roles: bool = False):
    try:
        return await selection.check_bom(pid, pcb, lambda: _doc(pid), create_roles)
    except jlc.JlcError as e:
        _bad(e, 502)


# ---- phase 1b: netlist -------------------------------------------------------------------------
@app.get("/api/projects/{pid}/netlist")
async def netlist_get(pid: str):
    return ops.netlist_state(pid)


@app.post("/api/projects/{pid}/netlist/import")
async def netlist_import(pid: str, request: Request):
    return {"netlist": await ops.import_netlist(pid, (await request.json()).get("path", ""))}


@app.delete("/api/projects/{pid}/netlist")
async def netlist_delete(pid: str):
    f = workspace.project_dir(pid) / "netlist.json"
    if f.exists():
        f.unlink()
    return ops.netlist_state(pid)


@app.get("/api/footprints")
async def footprints(q: str = "", limit: int = 40):
    return await asyncio.to_thread(netlist.find_footprints, q, min(limit, 200))


# ---- phase 2: placement --------------------------------------------------------------------------
@app.get("/api/projects/{pid}/layout")
async def layout_get(pid: str):
    return await ops.layout_state(pid, pcb, lambda: _doc(pid))


@app.post("/api/projects/{pid}/layout/outline")
async def layout_outline(pid: str, request: Request):
    b = await request.json()
    return await ops.set_outline(pid, pcb, lambda: _doc(pid), b.get("shape", "rect"), float(b.get("width_mm", 0)),
                                 float(b.get("height_mm", 0) or b.get("width_mm", 0)), float(b.get("corner_mm", 0) or 0))


@app.post("/api/projects/{pid}/layout/build")
async def layout_build(pid: str, request: Request):
    b = await request.json() if await request.body() else {}
    return await ops.build(pid, pcb, lambda: _doc(pid), b.get("fab_profile"))


@app.post("/api/projects/{pid}/layout/place")
async def layout_place(pid: str, request: Request):
    b = await request.json() if await request.body() else {}
    return await ops.place(pid, pcb, lambda: _doc(pid), int(b.get("moves", 20000)), int(b.get("seed", 1)),
                           bool(b.get("refine", False)))


@app.post("/api/projects/{pid}/layout/check")
async def layout_check(pid: str):
    c = await ops.placement_check(pid)
    ops.save_layout_cfg(pid, {"place_check": c})
    return c


# ---- phase 3: routing ------------------------------------------------------------------------------
@app.get("/api/projects/{pid}/routing")
async def routing_get(pid: str):
    return ops.routing_state(pid)


@app.post("/api/projects/{pid}/routing/start")
async def routing_start(pid: str, request: Request):
    b = await request.json() if await request.body() else {}
    return await ops.start_routing(pid, pcb, None, b.get("rounds"), b.get("max_time"))


@app.post("/api/projects/{pid}/routing/{jid}/cancel")
async def routing_cancel(pid: str, jid: str):
    return await routing.cancel(pid, jid)


@app.get("/api/projects/{pid}/routing/{jid}")
async def routing_job(pid: str, jid: str):
    return routing.load(pid, jid)


@app.get("/api/projects/{pid}/routing/{jid}/{cid}/board")
async def routing_preview(pid: str, jid: str, cid: str):
    path = routing.candidate_path(pid, jid, cid)

    def f():
        import pcbnew
        data = board.extract(pcbnew.LoadBoard(str(path)), str(path))
        data.update({"rev": -1, "can_undo": False, "preview": f"{jid}/{cid}"})
        return data
    return Response(json.dumps(await pcb(f), separators=(",", ":")), media_type="application/json")


@app.post("/api/projects/{pid}/routing/{jid}/{cid}/adopt")
async def routing_adopt(pid: str, jid: str, cid: str):
    res = await ops.adopt(pid, pcb, lambda: _doc(pid), jid, cid)
    if phases.state(pid)["phases"]["routing"]["status"] == "approved":
        phases.reopen(pid, "routing")
    return res


@app.on_event("startup")
async def _startup():
    routing.mark_interrupted()


@app.on_event("shutdown")
async def _shutdown():
    routing.shutdown()


# ---- chat --------------------------------------------------------------------------------
@app.get("/api/projects/{pid}/chats")
async def chats(pid: str):
    return workspace.list_chats(pid)


@app.get("/api/projects/{pid}/chats/{cid}")
async def chat_get(pid: str, cid: str):
    c = workspace.load_chat(pid, cid)
    return {"id": c["id"], "ui": c["ui"]}


@app.post("/api/projects/{pid}/chat")
async def chat(pid: str, request: Request):
    body = await request.json()
    text = (body.get("message") or "").strip()
    if not text:
        _bad(ValueError("empty message"))
    cid = body.get("conversation") or secrets.token_hex(6)
    try:
        convo = workspace.load_chat(pid, cid)
    except ValueError as e:
        _bad(e)
    settings = workspace.load_settings()
    provider = _provider(settings, body.get("provider"))
    mod, _label, model_key = PROVIDERS[provider]
    settings["model"] = body["model"] if isinstance(body.get("model"), str) else settings.get(model_key, "")
    if body.get("effort") in ("low", "medium", "high", "xhigh", "max"):
        settings["effort"] = body["effort"]

    def ctx_text():
        d = _doc(pid)
        data = d.data()
        sel = body.get("selection") or {}
        return (f"<board_context>file={data['name']} revision={d.rev} "
                f"size_mm={round(data['bbox'][2]-data['bbox'][0],2)}x{round(data['bbox'][3]-data['bbox'][1],2)} "
                f"footprints={data['stats']['footprints']} nets={data['stats']['nets']} "
                f"copper_layers={data['stats']['copper_layers']}"
                + (f" selected_refs={','.join(sel.get('refs', [])[:30])}" if sel.get("refs") else "")
                + (f" highlighted_net={sel['net']}" if sel.get("net") else "")
                + "</board_context>")

    try:
        context = await pcb(ctx_text)
    except ValueError as e:
        _bad(e, 404)
    convo = workspace.load_chat(pid, cid)  # re-read: a turn may have finished during the await above

    queue: asyncio.Queue = asyncio.Queue()

    async def emit(ev: dict):
        if ev.get("type") == "error":
            parts.append({"t": "error", "text": ev["message"]})  # keep errors in the saved history too
        await queue.put(ev)

    ctx = tools.ToolContext(pid=pid, pcb=pcb, doc=lambda: _doc(pid), emit=emit)
    parts = ctx.parts
    convo["ui"].append({"role": "user", "text": text, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    convo["provider"] = provider
    model_label = settings["model"] or "plan default"

    async def worker():
        usage = {}
        try:
            await emit({"type": "start", "conversation": cid, "model": model_label, "provider": provider})
            if provider == "claude_code":
                token = secrets.token_urlsafe(24)
                _turns[pid] = {"token": token, "ctx": ctx}
                usage = await mod.run_turn(convo, context, text, settings, ctx,
                                           server_url=str(request.base_url).rstrip("/"), token=token)
            else:
                usage = await mod.run_turn(convo, context, text, settings, ctx)
        except Exception as e:  # noqa: BLE001 - surface anything to the UI
            await emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            _turns.pop(pid, None)
            convo["ui"].append({"role": "assistant", "parts": parts, "model": model_label,
                                "provider": provider, "usage": usage})
            workspace.save_chat(pid, convo)
            _busy.discard(pid)
            await emit({"type": "done", "usage": usage})
            await queue.put(None)

    # No await between this check and the claim, so two quick POSTs can't both get through.
    if pid in _busy:
        _bad(ValueError("the assistant is still answering in this project"), 409)
    _busy.add(pid)
    task = asyncio.create_task(worker())

    async def stream():
        # If the browser goes away this generator stops, but `task` keeps running so the
        # turn still finishes and the transcript is saved.
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield f"data: {json.dumps(ev)}\n\n"

    _running.add(task)
    task.add_done_callback(_running.discard)
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- static frontend ---------------------------------------------------------------------
@app.get("/")
async def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=WEB), name="web")


@app.exception_handler(ValueError)
async def value_error(_request: Request, e: ValueError):
    return JSONResponse({"detail": str(e)}, status_code=400)


@app.exception_handler(board.EditError)
async def edit_error(_request: Request, e: board.EditError):
    return JSONResponse({"detail": str(e)}, status_code=400)


@app.exception_handler(RuntimeError)
async def runtime_error(_request: Request, e: RuntimeError):
    return JSONResponse({"detail": str(e)}, status_code=409 if isinstance(e, routing.RouterBusy) else 400)
