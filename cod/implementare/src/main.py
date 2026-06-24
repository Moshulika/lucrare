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

def _resolve_env_vars(value):
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

from ui.config_init import ensure_user_config  
from ui.paths import USER_CONFIG_PATH  

ensure_user_config(_bundled_config_path, USER_CONFIG_PATH)
_config_path = USER_CONFIG_PATH

with open(_config_path) as f:
    config = _resolve_env_vars(yaml.safe_load(f))

provider_configs: dict[str, dict] = config.setdefault("providers", {})
providers: list[str] = [
    k for k, v in provider_configs.items() if isinstance(v, dict) and v.get("models")
]

for _prov in providers:
    if "model" not in provider_configs[_prov]:
        provider_configs[_prov]["model"] = provider_configs[_prov]["models"][0]


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]


def get_model(provider: str):
    cfg = provider_configs.get(provider)
    if cfg is None:
        raise ValueError(f"Unsupported model provider: {provider}")
    return get_provider(provider).create_model(cfg)

_active_tools: list = list(ALL_TOOLS)

def get_active_tools() -> list:
    return list(_active_tools)

def set_active_tools(tools: list):
    _active_tools.clear()
    _active_tools.extend(tools)

def _tool_by_name(name: str):
    for t in _active_tools:
        if getattr(t, "name", None) == name:
            return t
    return None

_ATTACHMENT_ERROR_PHRASES = (
    "image_url","image input","image content",
    "vision","multimodal","modality",
    "media type","document","pdf","image",
    "does not support","unsupported content type",
    "invalid content type","no image in","not accept",
)

def _classify_attachment_error(exc: BaseException) -> str | None:
    msg = " ".join(str(arg) for arg in getattr(exc, "args", ())) or str(exc)
    msg_low = msg.lower()
    for attr in ("body", "response", "message"):
        v = getattr(exc, attr, None)
        if v:
            msg_low += " " + str(v).lower()

    if not any(p in msg_low for p in _ATTACHMENT_ERROR_PHRASES):
        return None

    if "pdf" in msg_low or "document" in msg_low:
        return "pdf"
    if "image" in msg_low or "vision" in msg_low or "image_url" in msg_low:
        return "image"
    if "multimodal" in msg_low or "modality" in msg_low:
        return "image"
    return None

def _human_message_has_kind(messages: list, kind: str) -> bool:
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
    provider = config.get("configurable", {}).get("provider", "openai")
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
        kind = _classify_attachment_error(e)
        if kind and model_name and _human_message_has_kind(state["messages"], kind):
            try:
                from src import capabilities as _caps

                _caps.record_unsupported(
                    provider, model_name, kind, reason=str(e)[:200]
                )
            except Exception:
                pass
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
    import asyncio

    last = state["messages"][-1]
    tool_calls = getattr(last, "tool_calls", None) or []
    app_ctx = config.get("configurable", {}).get("app_ctx", {})

    sub_cfg = (app_ctx.get("subagents_config") or {})
    max_parallel = max(1, int(sub_cfg.get("max_parallel", 3)))

    results: dict[int, ToolMessage] = {}
    i = 0
    n = len(tool_calls)
    while i < n:
        tc = tool_calls[i]
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

    injections = app_ctx.pop("pending_image_injections", None) or []
    if injections:
        synth_msg = _build_synthetic_image_human_message(injections)
        if synth_msg is not None:
            out_messages.append(synth_msg)
            _record_synthetic_human_event(app_ctx, injections, synth_msg)

    return {"messages": out_messages}


def _build_synthetic_image_human_message(injections: list[dict]):
    blocks: list[dict] = []
    summary_parts: list[str] = []
    for inj in injections:
        name = inj.get("pdf_name") or "<pdf>"
        start = inj.get("page_start")
        end = inj.get("page_end")
        if start is not None and end is not None and start != end:
            summary_parts.append(f"{name} pages {start}-{end}")
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
    sess = app_ctx.get("session")
    if sess is None:
        return
    try:
        from ui.sessions import KIND_HUMAN as _KIND_HUMAN

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
        pass

def should_continue(state: AgentState):
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return END

builder = StateGraph(AgentState)

builder.add_node("chatbot", chatbot_node)
builder.add_node("tools", permissioned_tools_node)

builder.add_edge(START, "chatbot")
builder.add_conditional_edges("chatbot", should_continue, {"tools": "tools", END: END})
builder.add_edge("tools", "chatbot")

graph = builder.compile()