"""OpenAI-compatible provider: any server that speaks /v1/chat/completions with tool calls.

That covers local, free models (Ollama, LM Studio, llama.cpp server, vLLM) and hosted ones
(OpenRouter, OpenAI). Standard library only: the HTTP stream is read on a worker thread and fed
to the event loop through a queue. The transcript is kept in OpenAI's own message format
(chat["oa"]), separate from the provider-neutral UI transcript.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import urllib.error
import urllib.request

from .. import tools as T
from . import common as C

DEFAULT_BASE = "http://localhost:11434/v1"  # Ollama
MAX_STEPS = 30
_END = object()


def _base(settings: dict) -> str:
    return (settings.get("openai_base_url") or DEFAULT_BASE).rstrip("/")


def _key(settings: dict) -> str:
    return settings.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")


def _opener(url: str):
    local = url.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0] in ("localhost", "127.0.0.1", "[::1]")
    # Local model servers must not go through a system proxy; remote ones may need it.
    return urllib.request.build_opener(urllib.request.ProxyHandler({})) if local else urllib.request.build_opener()


def _request(settings: dict, path: str, body: dict | None = None) -> urllib.request.Request:
    h = {"Content-Type": "application/json", "Accept": "text/event-stream" if body and body.get("stream") else "application/json"}
    if _key(settings):
        h["Authorization"] = f"Bearer {_key(settings)}"
    if "openrouter.ai" in _base(settings):
        h["X-Title"] = "PCB Studio"
    return urllib.request.Request(_base(settings) + path, data=None if body is None else json.dumps(body).encode(),
                                  headers=h, method="POST" if body is not None else "GET")


def available(settings: dict) -> bool:
    return True


def _list_sync(settings: dict) -> list[dict]:
    req = _request(settings, "/models")
    with _opener(req.full_url).open(req, timeout=5) as r:
        data = json.loads(r.read())
    ids = sorted({m["id"] for m in data.get("data", []) if m.get("id")})
    return [{"id": i, "name": i} for i in ids]


async def list_models(settings: dict) -> list[dict]:
    try:
        models = await asyncio.to_thread(_list_sync, settings)
    except (OSError, ValueError):
        models = []
    want = settings.get("openai_model")
    if want and want not in {m["id"] for m in models}:
        models.insert(0, {"id": want, "name": want})
    return models


def _tool_defs() -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}} for t in T.TOOLS]


def _stream(settings: dict, body: dict, loop: asyncio.AbstractEventLoop, q: asyncio.Queue, stop: threading.Event) -> None:
    """Worker thread: POST the request and push each SSE data payload (or an exception) to q."""
    def put(x):
        loop.call_soon_threadsafe(q.put_nowait, x)
    try:
        req = _request(settings, "/chat/completions", body)
        with _opener(req.full_url).open(req, timeout=600) as r:
            for raw in r:
                if stop.is_set():
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    put(json.loads(data))
                except ValueError:
                    continue
    except urllib.error.HTTPError as e:
        put(RuntimeError(f"HTTP {e.code} from {_base(settings)}: {e.read().decode(errors='replace')[:600]}"))
    except OSError as e:
        put(RuntimeError(f"Could not reach the model server at {_base(settings)} ({e}). Is it running?"))
    except Exception as e:  # noqa: BLE001
        put(e)
    finally:
        put(_END)


def _close_dangling(msgs: list) -> None:
    """Answer tool calls that never ran so the transcript stays valid."""
    if msgs and msgs[-1]["role"] == "assistant":
        for tc in msgs[-1].get("tool_calls") or []:
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": "Not run: the turn was interrupted."})


async def run_turn(chat: dict, context: str, text: str, settings: dict, ctx: T.ToolContext) -> dict:
    msgs: list = chat.setdefault("oa", [])
    _close_dangling(msgs)
    msgs.append({"role": "user", "content": f"{context}\n\n{text}"})
    model = settings.get("model") or settings.get("openai_model")
    if not model:
        await ctx.emit({"type": "error", "message": "No model chosen. Pick one in the header, or set a model name in Settings."})
        return {}
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    loop = asyncio.get_running_loop()

    for _step in range(MAX_STEPS):
        body = {"model": model, "stream": True, "stream_options": {"include_usage": True},
                "messages": [{"role": "system", "content": C.PROMPT}, *msgs], "tools": _tool_defs()}
        q: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()
        threading.Thread(target=_stream, args=(settings, body, loop, q, stop), daemon=True).start()
        content, calls, finish = "", {}, None
        try:
            while True:
                item = await q.get()
                if item is _END:
                    break
                if isinstance(item, BaseException):
                    await ctx.emit({"type": "error", "message": str(item)})
                    _close_dangling(msgs)
                    return usage
                if item.get("usage"):
                    usage["input"] += item["usage"].get("prompt_tokens") or 0
                    usage["output"] += item["usage"].get("completion_tokens") or 0
                if item.get("error"):
                    await ctx.emit({"type": "error", "message": f"Model server error: {item['error']}"})
                    return usage
                for ch in item.get("choices") or []:
                    d = ch.get("delta") or {}
                    think = d.get("reasoning_content") or d.get("reasoning")
                    if isinstance(think, str) and think:
                        C.add_delta(ctx.parts, "thinking", think)
                        await ctx.emit({"type": "thinking", "delta": think})
                    if d.get("content"):
                        content += d["content"]
                        C.add_delta(ctx.parts, "text", d["content"])
                        await ctx.emit({"type": "text", "delta": d["content"]})
                    for tc in d.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                            await ctx.emit({"type": "tool_pending", "id": slot["id"], "name": slot["name"]})
                        slot["args"] += fn.get("arguments") or ""
                    finish = ch.get("finish_reason") or finish
        finally:
            stop.set()

        tool_calls = [{"id": c["id"] or f"call_{i}", "type": "function",
                       "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                      for i, c in sorted(calls.items()) if c["name"]]
        msg = {"role": "assistant", "content": content or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        msgs.append(msg)
        if not tool_calls:
            if finish == "length":
                await ctx.emit({"type": "error", "message": "The reply hit the model's output limit."})
            return usage
        for tc in tool_calls:
            try:
                args = json.loads(tc["function"]["arguments"])
                ok, res = await C.run_tool(ctx, tc["id"], tc["function"]["name"], args)
            except ValueError:
                ok, res = False, "Invalid JSON in tool arguments; send the call again with valid JSON."
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": C.result_text(res)})
    await ctx.emit({"type": "error", "message": f"Stopped after {MAX_STEPS} tool steps."})
    return usage
