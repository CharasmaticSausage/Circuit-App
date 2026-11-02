"""Claude Code provider: runs each chat turn through the user's own logged-in `claude` CLI.

This uses the Claude subscription the CLI is signed in with (Pro/Max usage limits) instead of
paid API credits. The CLI runs headless (`claude -p`, stream-json) with every built-in tool
disabled; the only tools it gets are PCB Studio's, served by `server/mcp_bridge.py`, which
forwards each call back to this server. The CLI keeps the conversation itself: the chat stores
its session id (chat["cc_session"]) and later turns `--resume` it.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

from .. import tools as T
from .. import workspace
from . import common as C

BRIDGE = Path(__file__).resolve().parent.parent / "mcp_bridge.py"
MCP_NAME = "pcbstudio"
TURN_TIMEOUT_S = 20 * 60
# Aliases resolve to the newest model of each family that the signed-in plan offers (on a Pro plan,
# 2026-11-02: default and sonnet -> Sonnet 5, opus -> Opus 5, haiku -> Haiku 4.5). Fable is left out:
# the CLI refuses it on Pro ("requires usage credits"), i.e. it would bill extra.
MODELS = [
    {"id": "", "name": "Plan default"},
    {"id": "opus", "name": "Opus (latest on plan)"},
    {"id": "sonnet", "name": "Sonnet (latest on plan)"},
    {"id": "haiku", "name": "Haiku (latest on plan)"},
]
# Variables that would switch the CLI from the subscription to API billing, or make it think it
# is nested inside another Claude Code session.
_STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
              "CLAUDE_CODE_SSE_PORT")


def cli_path(settings: dict) -> str | None:
    p = (settings.get("claude_cli") or "").strip()
    if p:
        return p if Path(p).is_file() else None
    found = shutil.which("claude")
    if found:
        return found
    for cand in (Path.home() / ".local/bin/claude.exe", Path.home() / ".claude/local/claude.exe",
                 Path(os.environ.get("APPDATA", "")) / "npm/claude.cmd"):
        if cand.is_file():
            return str(cand)
    return None


def available(settings: dict) -> bool:
    return cli_path(settings) is not None


async def list_models(settings: dict) -> list[dict]:
    return MODELS


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _args(exe: str, mcp_file: Path, settings: dict, session: str | None, new_session: str | None) -> list[str]:
    a = [exe, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
         "--tools", "",  # no Bash, file or web tools: the board tools are the whole toolbox
         "--mcp-config", str(mcp_file), "--strict-mcp-config",
         "--allowedTools", f"mcp__{MCP_NAME}",
         "--permission-mode", "dontAsk",
         "--system-prompt", C.PROMPT,
         "--disable-slash-commands", "--no-chrome",
         "--setting-sources", "project"]  # ignore the user's own hooks, plugins and CLAUDE.md
    if settings.get("model"):
        a += ["--model", settings["model"]]
    if settings.get("effort"):
        a += ["--effort", settings["effort"]]
    if session:
        a += ["--resume", session]
    else:
        a += ["--session-id", new_session]
    return a


def _plan_status(info: dict) -> str | None:
    w = (info or {}).get("unifiedWindows") or {}
    bits = [f"{label} {round(w[k]['utilization'] * 100)}%" for k, label in (("five_hour", "5 h"), ("seven_day", "7 day"))
            if isinstance(w.get(k), dict) and w[k].get("utilization") is not None]
    return "Plan usage: " + ", ".join(bits) if bits else None


async def run_turn(chat: dict, context: str, text: str, settings: dict, ctx: T.ToolContext, *,
                   server_url: str, token: str) -> dict:
    exe = cli_path(settings)
    if not exe:
        await ctx.emit({"type": "error", "message": "The Claude Code CLI (`claude`) was not found. Install it, run "
                        "`claude` once to sign in with your Claude plan, or set its path in Settings."})
        return {}
    pdir = workspace.project_dir(ctx.pid)
    mcp_file = pdir / ".claude_mcp.json"
    mcp_file.write_text(json.dumps({"mcpServers": {MCP_NAME: {
        "type": "stdio", "command": sys.executable, "args": [str(BRIDGE)],
        "env": {"PCBSTUDIO_URL": server_url, "PCBSTUDIO_PID": ctx.pid, "PCBSTUDIO_TOKEN": token}}}}),
        encoding="utf-8")
    try:
        usage = await _run(chat, f"{context}\n\n{text}", settings, ctx, exe, mcp_file, pdir, resume=True)
        return usage
    finally:
        mcp_file.unlink(missing_ok=True)  # holds this turn's token


async def _run(chat, prompt, settings, ctx, exe, mcp_file, pdir, resume: bool) -> dict:
    session = chat.get("cc_session") if resume else None
    new_session = None if session else str(uuid.uuid4())
    proc = await asyncio.create_subprocess_exec(
        *_args(exe, mcp_file, settings, session, new_session), cwd=str(pdir), env=_env(),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024)
    proc.stdin.write(prompt.encode("utf-8"))
    await proc.stdin.drain()
    proc.stdin.close()
    stderr_task = asyncio.create_task(proc.stderr.read())

    usage: dict = {}
    result = None
    try:
        async with asyncio.timeout(TURN_TIMEOUT_S):
            async for raw in proc.stdout:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                kind = msg.get("type")
                if kind == "system" and msg.get("subtype") == "init":
                    chat["cc_session"] = msg.get("session_id") or session or new_session
                    srv = next((s for s in msg.get("mcp_servers") or [] if s.get("name") == MCP_NAME), None)
                    if not srv or srv.get("status") != "connected":
                        await ctx.emit({"type": "error", "message": "The board tools did not connect to Claude Code "
                                        f"(status: {srv.get('status') if srv else 'missing'}). The model can't see the board this turn."})
                elif kind == "stream_event":
                    ev = msg.get("event") or {}
                    et = ev.get("type")
                    if et == "content_block_delta":
                        d = ev.get("delta") or {}
                        if d.get("type") == "text_delta" and d.get("text"):
                            C.add_delta(ctx.parts, "text", d["text"])
                            await ctx.emit({"type": "text", "delta": d["text"]})
                        elif d.get("type") == "thinking_delta" and d.get("thinking"):
                            C.add_delta(ctx.parts, "thinking", d["thinking"])
                            await ctx.emit({"type": "thinking", "delta": d["thinking"]})
                    elif et == "content_block_start" and (ev.get("content_block") or {}).get("type") in ("tool_use", "mcp_tool_use"):
                        cb = ev["content_block"]
                        name = cb.get("name", "").removeprefix(f"mcp__{MCP_NAME}__")
                        ctx.pending.setdefault(name, []).append(cb.get("id"))  # the bridge call reuses this id
                        await ctx.emit({"type": "tool_pending", "id": cb.get("id"), "name": name})
                elif kind == "rate_limit_event":
                    s = _plan_status(msg.get("rate_limit_info"))
                    if s:
                        await ctx.emit({"type": "status", "text": s})
                elif kind == "result":
                    result = msg
        rc = await proc.wait()
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    finally:
        err = (await stderr_task).decode("utf-8", errors="replace").strip()

    if result:
        u = result.get("usage") or {}
        usage = {"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0),
                 "cache_read": u.get("cache_read_input_tokens", 0), "cache_write": u.get("cache_creation_input_tokens", 0),
                 "billing": "subscription"}
        if result.get("session_id"):
            chat["cc_session"] = result["session_id"]
        if result.get("is_error"):
            msg = result.get("result") or "; ".join(result.get("errors") or []) or result.get("subtype", "error")
            await ctx.emit({"type": "error", "message": f"Claude Code: {msg}"})
        return usage

    if session and resume and not ctx.parts and ("No conversation found" in err or "session" in err.lower()):
        # The CLI no longer has this conversation (deleted or expired): start a fresh one.
        chat.pop("cc_session", None)
        await ctx.emit({"type": "status", "text": "Previous Claude Code session not found; starting a new one."})
        return await _run(chat, prompt, settings, ctx, exe, mcp_file, pdir, resume=False)
    await ctx.emit({"type": "error", "message": f"Claude Code exited with code {rc}: {err[-1500:] or 'no output'}"})
    return usage
