"""Claude provider: a streaming tool loop over the Anthropic Messages API.

The native transcript (chat["native"]) holds the API's own content blocks, appended
and never edited, so thinking blocks stay valid across turns and the prompt cache
keeps hitting. The UI keeps its own provider-neutral transcript (chat["ui"]).
"""
from __future__ import annotations

import anthropic

from .. import tools as T
from . import common as C

PROMPT = C.PROMPT

# Models that accept server-side refusal fallbacks ("default" routing by refusal category).
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
DEFAULT_MODELS = [
    {"id": "claude-opus-5-5", "name": "Claude Opus 5.5"},
    {"id": "claude-sonnet-5-5", "name": "Claude Sonnet 5.5"},
    {"id": "claude-haiku-5-5", "name": "Claude Haiku 5.5"},
    {"id": "claude-fable-5-1", "name": "Claude Fable 5.1"},
]
# Adaptive thinking is the only mode this loop sends; these older ids need budget_tokens instead.
_NO_ADAPTIVE = ("claude-3", "claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-5", "claude-opus-4-1", "claude-opus-4-0", "claude-sonnet-4-0")
MAX_STEPS = 30


def client(api_key: str | None) -> anthropic.AsyncAnthropic:
    # With no key the SDK resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile.
    return anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()


def available(settings: dict) -> bool:
    return True  # the key may also come from the environment or an `ant` profile, so always offer it


async def list_models(settings: dict) -> list[dict]:
    try:
        out = []
        async for m in client(settings.get("anthropic_api_key") or None).models.list(limit=100):
            if m.id.startswith("claude-") and not m.id.startswith(_NO_ADAPTIVE):
                out.append({"id": m.id, "name": m.display_name})
        return out or DEFAULT_MODELS
    except (anthropic.AnthropicError, TypeError):  # TypeError: no credentials configured at all
        return DEFAULT_MODELS


def _tool_defs() -> list[dict]:
    # Byte-stable across requests (fixed order, no volatile content) so the prompt cache hits.
    return [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"],
             "eager_input_streaming": True} for t in T.TOOLS]


def _no_credentials(e: BaseException) -> bool:
    # The SDK raises a bare TypeError at request time when no key, token or profile is configured.
    return isinstance(e, TypeError) and "authentication method" in str(e)


def _friendly(e: Exception) -> str:
    if _no_credentials(e):
        return ("No Anthropic API key is set. Add one in Settings (gear icon) or set ANTHROPIC_API_KEY, or switch "
                "Provider to Claude Code to use your Claude plan instead.")
    if isinstance(e, anthropic.AuthenticationError):
        return "Anthropic rejected the API key. Set a valid key in Settings or in ANTHROPIC_API_KEY."
    if isinstance(e, anthropic.PermissionDeniedError):
        return "This API key can't use the selected model."
    if isinstance(e, anthropic.NotFoundError):
        return "Unknown model id. Pick another model in the header."
    if isinstance(e, anthropic.RateLimitError):
        return "Rate limited by the Anthropic API. Wait a moment and send again."
    if isinstance(e, anthropic.BadRequestError):
        return f"The API rejected the request: {e.message}"
    if isinstance(e, anthropic.APIStatusError):
        return f"Anthropic API error {e.status_code}: {e.message}"
    if isinstance(e, anthropic.APIConnectionError):
        return "Could not reach the Anthropic API. Check the network connection."
    if isinstance(e, anthropic.AnthropicError):
        return f"No usable Anthropic credentials ({e}). Set a key in Settings or ANTHROPIC_API_KEY."
    return f"{type(e).__name__}: {e}"


def _close_dangling(native: list) -> None:
    """If the last assistant turn asked for tools that never ran, answer them with errors
    so the next request is still a valid conversation."""
    if not native or native[-1]["role"] != "assistant":
        return
    ids = [b["id"] for b in native[-1]["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
    if ids:
        native.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": i, "is_error": True, "content": "Not run: the turn was interrupted."}
            for i in ids]})


async def run_turn(chat: dict, context: str, text: str, settings: dict, ctx: T.ToolContext) -> dict:
    """Run one user turn to completion. Streams events through ctx.emit; mutates chat["native"]."""
    native: list = chat.setdefault("native", [])
    ui_parts = ctx.parts
    _close_dangling(native)
    # The board context rides in the user turn (not the system prompt) so the cached prefix stays stable.
    native.append({"role": "user", "content": [{"type": "text", "text": context}, {"type": "text", "text": text}]})

    model = settings.get("model") or "claude-opus-5-5"
    params = dict(
        model=model, max_tokens=32000, system=PROMPT, tools=_tool_defs(),
        thinking={"type": "adaptive", "display": "summarized"},
        output_config={"effort": settings.get("effort") or "high"},
        cache_control={"type": "ephemeral"},
    )
    if model in FALLBACK_MODELS:
        params.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")

    api = client(settings.get("anthropic_api_key") or None)
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    json_retries = 0
    try:
        for _step in range(MAX_STEPS):
            start = len(ui_parts)
            try:
                async with api.beta.messages.stream(messages=native, **params) as stream:
                    async for ev in stream:
                        if ev.type == "text":
                            C.add_delta(ui_parts, "text", ev.text)
                            await ctx.emit({"type": "text", "delta": ev.text})
                        elif ev.type == "thinking":
                            C.add_delta(ui_parts, "thinking", ev.thinking)
                            await ctx.emit({"type": "thinking", "delta": ev.thinking})
                        elif ev.type == "content_block_start" and ev.content_block.type == "tool_use":
                            await ctx.emit({"type": "tool_pending", "id": ev.content_block.id, "name": ev.content_block.name})
                    msg = await stream.get_final_message()
                json_retries = 0
            except ValueError:
                # Tool input JSON the SDK could not parse at all: re-issue the turn (bounded).
                json_retries += 1
                if json_retries > 2:
                    raise
                del ui_parts[start:]
                await ctx.emit({"type": "status", "text": "Retrying a garbled tool call..."})
                continue

            u = msg.usage
            usage["input"] += u.input_tokens or 0
            usage["output"] += u.output_tokens or 0
            usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
            usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0

            native.append({"role": "assistant", "content": [b.to_dict() for b in msg.content]})

            if msg.stop_reason == "pause_turn":
                continue
            if msg.stop_reason == "refusal":
                cat = getattr(getattr(msg, "stop_details", None), "category", None)
                await ctx.emit({"type": "error", "message": f"The model declined this request{f' ({cat})' if cat else ''}."})
                break
            tool_uses = [b for b in msg.content if b.type == "tool_use"]
            if not tool_uses:
                break
            if msg.stop_reason == "max_tokens":
                _close_dangling(native)
                await ctx.emit({"type": "error", "message": "The reply hit the output limit mid tool call; nothing was run."})
                break

            results = []
            for b in tool_uses:
                ok, res = await C.run_tool(ctx, b.id, b.name, b.input)
                results.append({"type": "tool_result", "tool_use_id": b.id, "is_error": not ok,
                                "content": C.result_text(res)})
            native.append({"role": "user", "content": results})
        else:
            await ctx.emit({"type": "error", "message": f"Stopped after {MAX_STEPS} tool steps."})
    except anthropic.AnthropicError as e:
        _close_dangling(native)
        await ctx.emit({"type": "error", "message": _friendly(e)})
    except BaseException as e:
        _close_dangling(native)
        if _no_credentials(e):
            await ctx.emit({"type": "error", "message": _friendly(e)})
            return usage
        raise
    return usage
