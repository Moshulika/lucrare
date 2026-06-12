import logging
import os
import re
import time
from pathlib import Path
from typing import Annotated, TypedDict

import yaml
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

from src.logging_setup import emit_event, get_logger, redact
from src.permissions import request_permission
from src.providers import get_provider
from src.tool_guards import check_preconditions, run_post_hooks
from src.tools import ALL_TOOLS, reset_tool_ctx, set_tool_ctx


# ---------------------------------------------------------
# 0. Load Configuration
# ---------------------------------------------------------
def _resolve_env_vars(value):
    """Replace ${ENV_VAR} placeholders with actual environment variable values."""
    if isinstance(value, str):
        return re.sub(
            r"\$\{(\w+)\}",
            lambda m: os.environ.get(m.group(1), ""),
            value,
        )
    if isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env_vars(v) for v in value]
    return value


_bundled_config_path = Path(__file__).resolve().parent.parent / "config" / "config.yml"

# Seed ~/.vibe-cli/config.yml from the bundled defaults on first run, and on
# every subsequent run fill in any keys that have since been added upstream.
# User values, extras, and comments are preserved.
from ui.config_init import ensure_user_config  # noqa: E402
from ui.paths import USER_CONFIG_PATH  # noqa: E402

ensure_user_config(_bundled_config_path, USER_CONFIG_PATH)
_config_path = USER_CONFIG_PATH

with open(_config_path) as f:
    config = _resolve_env_vars(yaml.safe_load(f))

# Provider catalogs live under `providers:` in config.yml. `provider_configs`
# is exported alongside `config` so callers don't need to reach into the
# nested dict (and so we can move the storage shape later without a sweep).
provider_configs: dict[str, dict] = config.setdefault("providers", {})

# Build list of configured providers (entries that have a models list)
providers: list[str] = [
    k for k, v in provider_configs.items() if isinstance(v, dict) and v.get("models")
]

# Set the active model for each provider to the first in its models list
for _prov in providers:
    if "model" not in provider_configs[_prov]:
        provider_configs[_prov]["model"] = provider_configs[_prov]["models"][0]


# ---------------------------------------------------------
# 1. Define the Graph State
# ---------------------------------------------------------
class AgentState(TypedDict):
    # The `add_messages` reducer tells LangGraph to append new
    # messages to the list, rather than overwriting it completely.
    messages: Annotated[list, add_messages]


# ---------------------------------------------------------
# 2. Model Factory
# ---------------------------------------------------------
def get_model(provider: str):
    """Dynamically loads the requested model."""
    cfg = provider_configs.get(provider)
    if cfg is None:
        raise ValueError(f"Unsupported model provider: {provider}")

    return get_provider(provider).create_model(cfg)


# ---------------------------------------------------------
# 3. Tool registry
# ---------------------------------------------------------
# Active tools are recomputed by ui/app.py after MCP servers connect.
# Stored as a module-level list so the node closures see updates without
# recompiling the graph.
_active_tools: list = list(ALL_TOOLS)


def get_active_tools() -> list:
    return list(_active_tools)


def set_active_tools(tools: list):
    """Replace the active tool list (built-ins + MCP)."""
    _active_tools.clear()
    _active_tools.extend(tools)


def _tool_by_name(name: str):
    for t in _active_tools:
        if getattr(t, "name", None) == name:
            return t
    return None


# ---------------------------------------------------------
# 4. Define the Nodes
# ---------------------------------------------------------
_ATTACHMENT_ERROR_PHRASES = (
    "image_url",
    "image input",
    "image content",
    "vision",
    "multimodal",
    "modality",
    "media type",
    "document",
    "pdf",
    "image",
    "does not support",
    "unsupported content type",
    "invalid content type",
    "no image in",
    "not accept",
)


def _classify_attachment_error(exc: BaseException) -> str | None:
    """Return "image" / "pdf" / "text" if `exc` looks like a provider rejecting
    an attachment kind; None otherwise.

    Bulletproof in the sense that any unmatched exception falls through to
    the existing error path — we never swallow real bugs.
    """
    msg = " ".join(str(arg) for arg in getattr(exc, "args", ())) or str(exc)
    msg_low = msg.lower()
    # Provider responses sometimes nest the real message in `body` / `response`.
    for attr in ("body", "response", "message"):
        v = getattr(exc, attr, None)
        if v:
            msg_low += " " + str(v).lower()

    if not any(p in msg_low for p in _ATTACHMENT_ERROR_PHRASES):
        return None

    # Prefer the most specific kind we can identify.
    if "pdf" in msg_low or "document" in msg_low:
        return "pdf"
    if "image" in msg_low or "vision" in msg_low or "image_url" in msg_low:
        return "image"
    if "multimodal" in msg_low or "modality" in msg_low:
        # Generic — assume image since that's the dominant case.
        return "image"
    return None


def _human_message_has_kind(messages: list, kind: str) -> bool:
    """Did the most recent HumanMessage contain a block of the given kind?

    Used to scope the "we just got rejected" attribution: only record an
    "image" / "pdf" no-go if the turn that failed actually had attachments
    of that kind.
    """
    for msg in reversed(messages):
        if not isinstance(msg, HumanMessage):
            continue
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            return False
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if kind == "image" and btype in ("image", "image_url"):
                return True
            if kind == "pdf" and (
                btype == "document"
                or block.get("kind") == "pdf"
                or "pdf" in str(block.get("media_type", "")).lower()
            ):
                return True
        return False
    return False


async def chatbot_node(state: AgentState, config: RunnableConfig):
    """Invoke the LLM with active tools bound."""
    provider = config.get("configurable", {}).get("provider", "openai")
    # The parameter shadows the module-level `config`; fetch via globals().
    _module_provider_configs = globals().get("provider_configs") or {}
    model_name = _module_provider_configs.get(provider, {}).get("model")
    llm_log = get_logger("llm")

    llm = get_model(provider)
    llm_with_tools = llm.bind_tools(get_active_tools())

    emit_event(
        llm_log,
        "llm.start",
        provider=provider,
        model=model_name,
        message_count=len(state["messages"]),
    )
    t0 = time.perf_counter()
    try:
        response = await llm_with_tools.ainvoke(state["messages"])
    except Exception as e:
        # Detect "model rejected the attachment" before we surface the
        # error — record a capability "no" so we pre-gate next time.
        kind = _classify_attachment_error(e)
        if kind and model_name and _human_message_has_kind(state["messages"], kind):
            try:
                from src import capabilities as _caps

                _caps.record_unsupported(
                    provider, model_name, kind, reason=str(e)[:200]
                )
            except Exception:
                # Capability recording must never mask the original error.
                pass
            # Annotate the exception with a marker the app loop can read
            # off so it knows to print a friendlier message.
            try:
                setattr(e, "_vibe_attachment_kind_rejected", kind)
            except Exception:
                pass
        emit_event(
            llm_log,
            "llm.end",
            level=logging.ERROR,
            provider=provider,
            model=model_name,
            ok=False,
            error_type=type(e).__name__,
            attachment_kind_rejected=kind,
            duration_ms=(time.perf_counter() - t0) * 1000.0,
            exc_info=True,
        )
        raise

    um = getattr(response, "usage_metadata", None) or {}
    input_token_details = um.get("input_token_details") or {}
    tool_calls = getattr(response, "tool_calls", None) or []
    emit_event(
        llm_log,
        "llm.end",
        provider=provider,
        model=model_name,
        ok=True,
        input_tokens=int(um.get("input_tokens") or 0),
        output_tokens=int(um.get("output_tokens") or 0),
        cache_read=int(input_token_details.get("cache_read") or 0),
        cache_write=int(input_token_details.get("cache_creation") or 0),
        tool_calls=len(tool_calls),
        stop_reason=getattr(response, "response_metadata", {}).get("stop_reason"),
        duration_ms=(time.perf_counter() - t0) * 1000.0,
    )
    return {"messages": [response]}


def _read_text_safe(path: str) -> str | None:
    """Best-effort text read for diff capture. None on failure."""
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return ""
        if not p.is_file():
            return None
        return p.read_text(errors="replace")
    except Exception:
        return None


def _tool_source(name: str) -> str:
    return ("mcp:" + name.split("__", 1)[0]) if "__" in name else "builtin"


async def _run_one_tool_call(tc: dict, app_ctx: dict) -> ToolMessage:
    """Run a single tool call end-to-end: permission, invoke, log, diff capture.

    Sequential path used by `permissioned_tools_node` for ordinary tools.
    Spawn-subagent calls reuse this helper via the parallel batch path —
    they share the exact same plumbing (permissions, logging, ctx binding),
    just dispatched concurrently when multiple are requested in one turn.
    """
    from ui.ui import label_for_tool, tool_log_line

    tool_log = get_logger("tool_call")
    name = tc["name"]
    args = tc.get("args", {}) or {}
    call_id = tc["id"]

    tool = _tool_by_name(name)
    if tool is None:
        emit_event(
            tool_log,
            "tool.unavailable",
            level=logging.WARNING,
            tool=name,
            tool_call_id=call_id,
            source=_tool_source(name),
        )
        return ToolMessage(
            content=f"Tool '{name}' is not available.",
            tool_call_id=call_id,
            name=name,
        )

    emit_event(
        tool_log,
        "tool.start",
        tool=name,
        tool_call_id=call_id,
        source=_tool_source(name),
        args=redact(args),
    )

    decision = await request_permission(name, args, app_ctx)
    if not decision.allow:
        emit_event(
            tool_log,
            "tool.end",
            tool=name,
            tool_call_id=call_id,
            source=_tool_source(name),
            ok=False,
            denied=True,
            reason_len=len(decision.reason or ""),
        )
        return ToolMessage(
            content=f"User denied this tool call. Reason: {decision.reason}",
            tool_call_id=call_id,
            name=name,
        )

    # Tool guards: deterministic preconditions enforced by the framework
    # (e.g. read-before-write). A denial short-circuits the call and the
    # error string becomes the tool result so the LLM self-corrects.
    denied = check_preconditions(name, args, app_ctx)
    if denied is not None:
        guard_name, reason = denied
        emit_event(
            tool_log,
            "tool.precondition_denied",
            tool=name,
            tool_call_id=call_id,
            source=_tool_source(name),
            guard=guard_name,
            reason_len=len(reason),
        )
        # Stash for the session-event recorder so the web UI can flag this
        # tool_result as guard-rejected without parsing the content string.
        app_ctx.setdefault("pending_precondition_denials", {})[call_id] = {
            "guard": guard_name,
            "reason": reason,
        }
        return ToolMessage(content=reason, tool_call_id=call_id, name=name)

    _tool_t0 = time.perf_counter()
    app_ctx["status_label"] = label_for_tool(name, args)

    if name not in ("write_file", "edit_file"):
        app_ctx.setdefault("pending_tool_logs", {})[call_id] = tool_log_line(name, args)

    before_content: str | None = None
    if name in ("write_file", "edit_file"):
        path_arg = args.get("path") if isinstance(args, dict) else None
        if isinstance(path_arg, str) and path_arg:
            before_content = _read_text_safe(path_arg)

    ctx_token = set_tool_ctx(app_ctx)
    tool_error: Exception | None = None
    # Pass the full ToolCall envelope rather than just `args` so tools
    # that declare `InjectedToolCallId` parameters (e.g. spawn_subagent)
    # receive their call id. With this shape tool.ainvoke returns a
    # ToolMessage; otherwise it returns the raw value.
    tool_call_envelope = {
        "name": name,
        "args": args,
        "id": call_id,
        "type": "tool_call",
    }
    try:
        result = await tool.ainvoke(tool_call_envelope)
    except Exception as e:
        tool_error = e
        result = f"Tool error: {e}"
    finally:
        reset_tool_ctx(ctx_token)

    if isinstance(result, ToolMessage):
        result = result.content
    if not isinstance(result, str):
        result = str(result)
    emit_event(
        tool_log,
        "tool.end",
        tool=name,
        tool_call_id=call_id,
        source=_tool_source(name),
        ok=tool_error is None,
        error_type=type(tool_error).__name__ if tool_error else None,
        result_bytes=len(result),
        duration_ms=(time.perf_counter() - _tool_t0) * 1000.0,
    )

    # Post-hooks: track state used by future preconditions (e.g. file SHA).
    # Only on a clean invocation — tool errors leave guard state untouched.
    if tool_error is None:
        run_post_hooks(name, args, result, app_ctx)

    if (
        name in ("write_file", "edit_file")
        and before_content is not None
        and (result.startswith("Written ") or result.startswith("Edited "))
    ):
        path_arg = args.get("path") if isinstance(args, dict) else None
        after_content = (
            _read_text_safe(path_arg) if isinstance(path_arg, str) else None
        )
        if after_content is not None:
            app_ctx.setdefault("pending_diffs", {})[call_id] = {
                "path": path_arg,
                "before": before_content,
                "after": after_content,
            }

    app_ctx["status_label"] = "thinking"
    return ToolMessage(content=result, tool_call_id=call_id, name=name)


async def permissioned_tools_node(state: AgentState, config: RunnableConfig):
    """Tool execution with per-call permission checks.

    Reads the runtime ctx from config["configurable"]["app_ctx"]. Ordinary
    tool calls run sequentially (their side effects often conflict).
    `spawn_subagent` calls that arrive in the same assistant turn are
    batched and run in parallel via `asyncio.gather`, bounded by
    `subagents.max_parallel` (default 3) — the parent's tool loop blocks
    on the batch, but each child runs independently.

    Output order preserves the order of `tool_calls` in the input message
    so tool-id pairing remains stable downstream.
    """
    import asyncio

    last = state["messages"][-1]
    tool_calls = getattr(last, "tool_calls", None) or []
    app_ctx = config.get("configurable", {}).get("app_ctx", {})

    sub_cfg = (app_ctx.get("subagents_config") or {})
    max_parallel = max(1, int(sub_cfg.get("max_parallel", 3)))

    # Index → ToolMessage map; fills in as calls complete.
    results: dict[int, ToolMessage] = {}
    i = 0
    n = len(tool_calls)
    while i < n:
        tc = tool_calls[i]
        # Greedy run of consecutive spawn_subagent calls becomes a batch.
        if tc["name"] == "spawn_subagent":
            batch_indices: list[int] = []
            while (
                i < n
                and tool_calls[i]["name"] == "spawn_subagent"
                and len(batch_indices) < max_parallel
            ):
                batch_indices.append(i)
                i += 1
            batch_tcs = [tool_calls[idx] for idx in batch_indices]
            app_ctx["status_label"] = (
                f"Agent×{len(batch_tcs)} running"
                if len(batch_tcs) > 1
                else app_ctx.get("status_label") or "thinking"
            )
            batch_results = await asyncio.gather(
                *(_run_one_tool_call(t, app_ctx) for t in batch_tcs),
                return_exceptions=False,
            )
            for idx, tm in zip(batch_indices, batch_results):
                results[idx] = tm
            app_ctx["status_label"] = "thinking"
            continue

        results[i] = await _run_one_tool_call(tc, app_ctx)
        i += 1

    out_messages: list = [results[k] for k in sorted(results)]

    # Drain pending image injections (set by tools like read_pdf_pages
    # when as_image=True). For every injection, append a synthesized
    # HumanMessage carrying the rendered image blocks. The next
    # chatbot call sees them in a user-shaped message, which is the
    # one provider-portable place to deliver images mid-conversation.
    injections = app_ctx.pop("pending_image_injections", None) or []
    if injections:
        synth_msg = _build_synthetic_image_human_message(injections)
        if synth_msg is not None:
            out_messages.append(synth_msg)
            _record_synthetic_human_event(app_ctx, injections, synth_msg)

    return {"messages": out_messages}


def _build_synthetic_image_human_message(injections: list[dict]):
    """Combine one or more pending-injection records into a single
    HumanMessage with a small text preamble + every image block."""
    blocks: list[dict] = []
    summary_parts: list[str] = []
    for inj in injections:
        name = inj.get("pdf_name") or "<pdf>"
        start = inj.get("page_start")
        end = inj.get("page_end")
        if start is not None and end is not None and start != end:
            summary_parts.append(f"{name} pages {start}–{end}")
        elif start is not None:
            summary_parts.append(f"{name} page {start}")
        else:
            summary_parts.append(name)
        blocks.extend(inj.get("image_blocks") or [])
    if not blocks:
        return None
    preamble = "Rendered images: " + ", ".join(summary_parts) + "."
    return HumanMessage(content=[{"type": "text", "text": preamble}, *blocks])


def _record_synthetic_human_event(app_ctx: dict, injections: list[dict], msg) -> None:
    """Append a KIND_HUMAN session event for the synthetic injection so
    /resume + dashboard can render it as a non-user message."""
    sess = app_ctx.get("session")
    if sess is None:
        return
    try:
        # Lazy import to avoid a circular dependency on the UI layer.
        from ui.sessions import KIND_HUMAN as _KIND_HUMAN

        # Build a serialisable content list: keep the preamble text and a
        # compact placeholder per image block referencing the artifact sha
        # (the actual bytes already live in the artifact store).
        page_refs: list[dict] = []
        for inj in injections:
            for sha in inj.get("page_shas") or []:
                page_refs.append(
                    {
                        "type": "rendered_page",
                        "pdf_name": inj.get("pdf_name") or "",
                        "sha": sha,
                    }
                )
        content = list(getattr(msg, "content", []) or [])
        # Replace base64 image blocks with the lightweight refs so the
        # session file stays small. Keep the preamble text block as-is.
        compact = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
        compact.extend(page_refs)
        meta = {
            "synthesized": True,
            "source": "read_pdf_pages",
            "injections": [
                {
                    "pdf_name": inj.get("pdf_name"),
                    "page_start": inj.get("page_start"),
                    "page_end": inj.get("page_end"),
                    "page_count": len(inj.get("page_shas") or []),
                }
                for inj in injections
            ],
        }
        sess.append_event(_KIND_HUMAN, compact, meta=meta)
    except Exception:
        # Recording is best-effort — never fail the turn because of a
        # session-log hiccup.
        pass


def should_continue(state: AgentState):
    """Route to tools if the last message has tool calls, otherwise end."""
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return END


# ---------------------------------------------------------
# 5. Build and Compile the Graph
# ---------------------------------------------------------
builder = StateGraph(AgentState)

builder.add_node("chatbot", chatbot_node)
builder.add_node("tools", permissioned_tools_node)

# START -> chatbot -> (tools -> chatbot)* -> END
builder.add_edge(START, "chatbot")
builder.add_conditional_edges("chatbot", should_continue, {"tools": "tools", END: END})
builder.add_edge("tools", "chatbot")

graph = builder.compile()

# ---------------------------------------------------------
# 6. Execution Example
# ---------------------------------------------------------
if __name__ == "__main__":
    import asyncio

    async def main():
        from ui.preferences import get_default_provider

        provider = get_default_provider(provider_configs) or providers[0]

        user_input = "Write a one-line Python function to reverse a string."
        initial_state = {"messages": [HumanMessage(content=user_input)]}

        run_config = {
            "configurable": {
                "provider": provider,
                "app_ctx": {"skip_permissions": True},
            }
        }

        print(f"Routing to {run_config['configurable']['provider']}...\n")

        async for event in graph.astream(initial_state, config=run_config):
            for node_name, node_state in event.items():
                latest_message = node_state["messages"][-1].content
                print(f"[{node_name}]: {latest_message}")

    asyncio.run(main())