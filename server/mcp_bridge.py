"""MCP stdio server that exposes PCB Studio's board tools to a Claude Code CLI session.

The Claude Code provider starts `claude -p` with an MCP config pointing here. Each tool call is
forwarded to the running PCB Studio server over localhost HTTP, so the tools run in the same
process (and on the same pcbnew thread) as the rest of the app, and the chat UI sees them live.

Standard library only. Env: PCBSTUDIO_URL, PCBSTUDIO_PID, PCBSTUDIO_TOKEN (one token per chat turn).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("PCBSTUDIO_URL", "http://127.0.0.1:8765").rstrip("/")
PID = os.environ.get("PCBSTUDIO_PID", "")
TOKEN = os.environ.get("PCBSTUDIO_TOKEN", "")
# Never route localhost calls through a system proxy.
_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _call(method: str, path: str, body: dict | None = None, timeout: float = 600) -> dict:
    req = urllib.request.Request(URL + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-PCBStudio-Token": TOKEN})
    try:
        with _http.open(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except ValueError:
            pass
        return {"ok": False, "result": f"PCB Studio refused the call ({e.code}): {detail}"}
    except OSError as e:
        return {"ok": False, "result": f"PCB Studio is not reachable at {URL}: {e}"}


def _reply(msg_id, result=None, error=None) -> None:
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def _handle(msg: dict) -> None:
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return  # notifications (initialized, cancelled) need no answer
    if method == "initialize":
        _reply(msg_id, {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "pcbstudio", "version": "0.2"}})
    elif method == "ping":
        _reply(msg_id, {})
    elif method == "tools/list":
        tools = _call("GET", "/api/tools", timeout=30)
        if isinstance(tools, dict) and tools.get("ok") is False:
            _reply(msg_id, error={"code": -32603, "message": tools["result"]})
        else:
            _reply(msg_id, {"tools": tools})
    elif method == "tools/call":
        p = msg.get("params") or {}
        r = _call("POST", f"/api/projects/{PID}/tool", {"name": p.get("name"), "arguments": p.get("arguments") or {}})
        res = r.get("result")
        _reply(msg_id, {"content": [{"type": "text", "text": res if isinstance(res, str) else json.dumps(res)}],
                        "isError": not r.get("ok")})
    else:
        _reply(msg_id, error={"code": -32601, "message": f"method not found: {method}"})


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            _reply(None, error={"code": -32700, "message": "parse error"})
            continue
        try:
            _handle(msg)
        except Exception as e:  # noqa: BLE001 - never let one bad call kill the bridge
            if msg.get("id") is not None:
                _reply(msg["id"], error={"code": -32603, "message": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    main()
