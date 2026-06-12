"""
Subagent base class and orchestrator.

Each subagent runs as its own short LangGraph-style tool-calling loop, reusing
the parent agent's `permissioned_tools_node` so sandboxing / permission
inheritance is automatic. The child sees only its filtered tool list and its
own system prompt. The parent only sees the final string result.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage


# ---------------------------------------------------------
# Result envelope
# ---------------------------------------------------------
def _extract_text(response) -> str:
    """Pull the user-facing text out of an AIMessage response.

    Handles both string-content and Anthropic-style list-of-blocks shapes.
    Returns "" when no text is present (only thinking / tool blocks).
    """
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(p for p in parts if p)


@dataclass
class SubagentResult:
    text: str
    timed_out: bool = False
    iterations: int = 0
    tool_call_count: int = 0
    error: str | None = None
    duration_s: float = 0.0
    usage: dict = field(default_factory=dict)


# ---------------------------------------------------------
# Subagent ABC
# ---------------------------------------------------------
# Shared header prepended to every subagent's system prompt. Keeps the
# orchestrator contract identical across types and any future subagent: act
# as an advisor that reports back, don't take state-mutating action unless
# the orchestrator explicitly told you to, ALWAYS end with a written report.
SUBAGENT_CONTRACT = """\
You are a subagent spawned by an orchestrator (the parent agent).

You MUST follow these rules; they take precedence over everything below.

1. SCOPE
   Do ONLY what the orchestrator asked. Do not extend the task, do not
   "be helpful" by tackling adjacent issues. If the request is ambiguous,
   make the most conservative interpretation and note your assumption in
   the final report.

2. NO UNAUTHORISED STATE CHANGES
   Do NOT write files, edit files, run mutating shell commands, push
   commits, install packages, send network requests with side effects,
   or take any other action that modifies state UNLESS the orchestrator's
   prompt explicitly told you to. When in doubt, read and report — let
   the orchestrator be the one to act on your findings.

3. ALWAYS END WITH A FINAL REPORT
   Your last message MUST be a text reply (no tool call) addressed to the
   orchestrator. Lead with the answer or finding. Cite concrete file paths,
   line ranges, commands, or sources where relevant. Keep it concise but
   complete — the orchestrator only sees this text, not your intermediate
   steps. If you couldn't finish, say so explicitly and report what you
   did find. Never end silently.

4. NO QUESTIONS
   You cannot ask the orchestrator clarifying questions — there is no
   reply channel. Use your best judgement and state assumptions in the
   report.

Type-specific guidance follows below.
---
"""


class Subagent(ABC):
    """Subclass and set the class-level attributes, override `system_prompt`."""

    name: str = "subagent"
    description: str = ""
    default_timeout_s: int = 120
    max_iterations: int = 20
    # Tool names this subagent can use. None = inherit parent's full set.
    allowed_tools: list[str] | None = None
    # Tools to drop after intersecting with parent's set (e.g. never spawn).
    excluded_tools: tuple[str, ...] = ("spawn_subagent",)

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}

    # ---- overridable hooks ----
    @abstractmethod
    def role_prompt(self, prompt: str, parent_ctx: dict) -> str:
        """Return the *type-specific* portion of the system prompt.

        The full system prompt sent to the LLM is `SUBAGENT_CONTRACT` (the
        shared orchestrator rules) followed by this role-specific section.
        Subclasses should focus on what makes their type unique — toolset
        conventions, output format expectations, scope notes — and trust the
        shared contract to enforce the global rules.
        """

    def system_prompt(self, prompt: str, parent_ctx: dict) -> str:
        """Final system prompt — contract + role. Not usually overridden."""
        return SUBAGENT_CONTRACT + self.role_prompt(prompt, parent_ctx)

    def tool_names(self, parent_ctx: dict) -> list[str] | None:
        """Optional override — defaults to `allowed_tools`."""
        return self.allowed_tools

    # ---- main entry point ----
    async def run(
        self,
        prompt: str,
        parent_ctx: dict,
        *,
        provider: str,
        model: str,
        timeout_s: int,
        parent_tool_call_id: str | None = None,
    ) -> SubagentResult:
        """Drive the child tool-calling loop with a timeout. Returns one result."""
        from src.logging_setup import emit_event, get_logger

        log = get_logger("subagent")
        t0 = time.perf_counter()
        emit_event(
            log,
            "subagent.start",
            subagent_type=self.name,
            provider=provider,
            model=model,
            timeout_s=timeout_s,
            depth=parent_ctx.get("subagent_depth", 0) + 1,
        )
        try:
            result = await asyncio.wait_for(
                self._drive_loop(
                    prompt, parent_ctx, provider, model, parent_tool_call_id
                ),
                timeout=timeout_s,
            )
            result.duration_s = time.perf_counter() - t0
            emit_event(
                log,
                "subagent.end",
                subagent_type=self.name,
                provider=provider,
                model=model,
                ok=result.error is None,
                iterations=result.iterations,
                tool_calls=result.tool_call_count,
                duration_ms=result.duration_s * 1000.0,
            )
            return result
        except asyncio.TimeoutError:
            dur = time.perf_counter() - t0
            emit_event(
                log,
                "subagent.end",
                level=logging.WARNING,
                subagent_type=self.name,
                provider=provider,
                model=model,
                ok=False,
                timed_out=True,
                duration_ms=dur * 1000.0,
            )
            return SubagentResult(
                text=f"[subagent '{self.name}' timed out after {timeout_s}s]",
                timed_out=True,
                duration_s=dur,
            )
        except Exception as exc:
            dur = time.perf_counter() - t0
            emit_event(
                log,
                "subagent.end",
                level=logging.ERROR,
                subagent_type=self.name,
                provider=provider,
                model=model,
                ok=False,
                error_type=type(exc).__name__,
                duration_ms=dur * 1000.0,
            )
            return SubagentResult(
                text=f"[subagent '{self.name}' error: {type(exc).__name__}: {exc}]",
                error=str(exc),
                duration_s=dur,
            )

    async def _drive_loop(
        self,
        prompt: str,
        parent_ctx: dict,
        provider: str,
        model: str,
        parent_tool_call_id: str | None = None,
    ) -> SubagentResult:
        """Run the child tool-calling loop until the model stops calling tools."""
        # Imports are local to avoid circulars (src.subagents is imported by tools.py).
        from src.main import get_active_tools, provider_configs
        from src.providers import get_provider

        # Build the model. We deep-copy the provider's config so we can swap in
        # the override `model` without polluting global state.
        prov_cfg = provider_configs.get(provider) or {}
        cfg_copy = deepcopy(prov_cfg)
        if model:
            cfg_copy["model"] = model
        llm = get_provider(provider).create_model(cfg_copy)

        # Filter parent's active tools.
        parent_tools = get_active_tools()
        allowed = self.tool_names(parent_ctx)
        excluded = set(self.excluded_tools or ())
        if allowed is None:
            tools = [t for t in parent_tools if t.name not in excluded]
        else:
            allowed_set = set(allowed)
            tools = [
                t for t in parent_tools if t.name in allowed_set and t.name not in excluded
            ]

        llm_with_tools = llm.bind_tools(tools) if tools else llm

        # Capture session id for tracing *before* dropping the Session object.
        parent_sess = parent_ctx.get("session")
        session_id_for_tracing = getattr(parent_sess, "id", None)

        # Build the child context — shallow copy + scoped fields.
        child_ctx: dict[str, Any] = dict(parent_ctx)
        child_ctx["status_label"] = "thinking"
        child_ctx["pending_tool_logs"] = {}
        child_ctx["pending_diffs"] = {}
        depth = parent_ctx.get("subagent_depth", 0) + 1
        child_ctx["subagent_depth"] = depth
        child_ctx["parent_tool_call_id"] = parent_tool_call_id
        # The child is not the chat — drop human-only state so it can't leak.
        child_ctx.pop("messages", None)
        child_ctx.pop("session", None)
        child_ctx["provider"] = provider

        # Build a run config that nests the child's LLM + tool runs under the
        # parent's LangFuse trace. When tracing is off, callbacks is an empty
        # list which LangChain treats as a no-op. The metadata keys with the
        # `langfuse_` prefix are the v3 SDK's grouping primitives.
        lf_handler = parent_ctx.get("langfuse_handler")
        callbacks = [lf_handler] if lf_handler is not None else []
        lf_metadata: dict = {
            "langfuse_tags": [
                f"subagent:{self.name}",
                f"subagent_depth:{depth}",
                f"provider:{provider}",
                f"model:{model}",
            ],
        }
        if session_id_for_tracing:
            lf_metadata["langfuse_session_id"] = session_id_for_tracing
        if parent_ctx.get("user_id"):
            lf_metadata["langfuse_user_id"] = parent_ctx["user_id"]
        child_run_config: dict[str, Any] = {
            "callbacks": callbacks,
            "metadata": lf_metadata,
            "run_name": f"subagent:{self.name}",
        }
        # Stash on child_ctx so _invoke_tools can pick it up for tool.ainvoke.
        child_ctx["_run_config"] = child_run_config

        messages: list = [
            SystemMessage(content=self.system_prompt(prompt, parent_ctx)),
            HumanMessage(content=prompt),
        ]

        tool_call_count = 0
        iterations = 0
        final_text = ""
        usage_totals = {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_write": 0,
        }

        while iterations < self.max_iterations:
            iterations += 1
            response = await llm_with_tools.ainvoke(messages, config=child_run_config)
            messages.append(response)

            um = getattr(response, "usage_metadata", None) or {}
            itd = um.get("input_token_details") or {}
            usage_totals["input"] += int(um.get("input_tokens") or 0)
            usage_totals["output"] += int(um.get("output_tokens") or 0)
            usage_totals["cache_read"] += int(itd.get("cache_read") or 0)
            usage_totals["cache_write"] += int(itd.get("cache_creation") or 0)

            tool_calls = getattr(response, "tool_calls", None) or []
            if not tool_calls:
                # No tool calls — extract text and return.
                final_text = _extract_text(response)
                break

            tool_call_count += len(tool_calls)

            tool_results = await self._invoke_tools(
                tool_calls, tools, child_ctx
            )
            messages.extend(tool_results)

            # Bound the child's working set — truncate oversized tool_result
            # bodies in place. Mirrors the eager `tool_truncate` stage; see
            # src/subagents/compaction.py for the trade-off.
            child_cfg = (
                (parent_ctx.get("subagents_config") or {}).get("child_context") or {}
            )
            mtt = int(child_cfg.get("max_tool_tokens") or 0)
            if mtt > 0:
                ht = child_cfg.get("head_tail") or [800, 400]
                from src.subagents.compaction import truncate_oversized_tool_results

                truncate_oversized_tool_results(
                    messages,
                    max_tool_tokens=mtt,
                    head_tokens=int(ht[0]),
                    tail_tokens=int(ht[1]),
                )
        else:
            # Hit max_iterations.
            return SubagentResult(
                text=(
                    f"[subagent '{self.name}' stopped after {self.max_iterations} "
                    "iterations without producing a final answer]"
                ),
                iterations=iterations,
                tool_call_count=tool_call_count,
                error="max_iterations",
                usage=usage_totals,
            )

        # Final-report enforcement: the model exited the loop cleanly (no
        # tool calls) but produced no text. Most often a small/local model
        # running out of context or generating only tool blocks. Nudge it
        # once for the report — cheaper than letting the parent re-do the
        # work and easier to spot than a silent return.
        if not final_text.strip() and iterations < self.max_iterations:
            iterations += 1
            messages.append(
                HumanMessage(
                    content=(
                        "You ended without writing a final report. As per "
                        "the system instructions, your last message MUST be "
                        "a text reply to the orchestrator. Write that report "
                        "now — even a one-sentence summary of what you did "
                        "or couldn't do. Do not call any more tools."
                    )
                )
            )
            try:
                response = await llm_with_tools.ainvoke(
                    messages, config=child_run_config
                )
                messages.append(response)
                um = getattr(response, "usage_metadata", None) or {}
                itd = um.get("input_token_details") or {}
                usage_totals["input"] += int(um.get("input_tokens") or 0)
                usage_totals["output"] += int(um.get("output_tokens") or 0)
                usage_totals["cache_read"] += int(itd.get("cache_read") or 0)
                usage_totals["cache_write"] += int(itd.get("cache_creation") or 0)
                final_text = _extract_text(response)
            except Exception:
                # Don't let the retry exception mask the real "no text"
                # state — the diagnostic below covers it.
                pass

        if not final_text.strip():
            final_text = (
                f"[subagent '{self.name}' ended without a final report — "
                "ran the requested work but did not summarise its findings. "
                "Treat any side-effects as unverified.]"
            )

        return SubagentResult(
            text=final_text,
            iterations=iterations,
            tool_call_count=tool_call_count,
            usage=usage_totals,
        )

    async def _invoke_tools(
        self, tool_calls: list, tools: list, child_ctx: dict
    ) -> list[ToolMessage]:
        """Invoke each tool call with the child ctx bound.

        Routes every call through `request_permission` so subagent tool use
        is gated identically to parent-agent tool use (same `always_allow`,
        same prompt UI). Emits `tool.start`/`tool.end` events tagged with
        `subagent_type` for log filterability.
        """
        from src.logging_setup import emit_event, get_logger, redact
        from src.permissions import request_permission
        from src.tools import reset_tool_ctx, set_tool_ctx

        tool_log = get_logger("tool_call")
        by_name = {t.name: t for t in tools}
        parent_tool_call_id = child_ctx.get("parent_tool_call_id")
        out: list[ToolMessage] = []

        def _tool_source(name: str) -> str:
            return ("mcp:" + name.split("__", 1)[0]) if "__" in name else "builtin"

        for tc in tool_calls:
            name = tc["name"]
            args = tc.get("args", {}) or {}
            call_id = tc["id"]

            tool = by_name.get(name)
            if tool is None:
                emit_event(
                    tool_log,
                    "tool.unavailable",
                    level=logging.WARNING,
                    tool=name,
                    tool_call_id=call_id,
                    source=_tool_source(name),
                    subagent_type=self.name,
                    parent_tool_call_id=parent_tool_call_id,
                )
                out.append(
                    ToolMessage(
                        content=(
                            f"Tool '{name}' is not available to subagent "
                            f"'{self.name}'."
                        ),
                        tool_call_id=call_id,
                        name=name,
                    )
                )
                continue

            emit_event(
                tool_log,
                "tool.start",
                tool=name,
                tool_call_id=call_id,
                source=_tool_source(name),
                args=redact(args),
                subagent_type=self.name,
                parent_tool_call_id=parent_tool_call_id,
            )

            decision = await request_permission(name, args, child_ctx)
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
                    subagent_type=self.name,
                    parent_tool_call_id=parent_tool_call_id,
                )
                out.append(
                    ToolMessage(
                        content=(
                            f"User denied this tool call. "
                            f"Reason: {decision.reason}"
                        ),
                        tool_call_id=call_id,
                        name=name,
                    )
                )
                continue

            # Deterministic preconditions (read-before-write, etc.). Child
            # shares the parent's ctx, so guard state (file_reads, config)
            # is consistent across parent and subagent.
            from src.tool_guards import check_preconditions, run_post_hooks

            denied = check_preconditions(name, args, child_ctx)
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
                    subagent_type=self.name,
                    parent_tool_call_id=parent_tool_call_id,
                )
                out.append(
                    ToolMessage(content=reason, tool_call_id=call_id, name=name)
                )
                continue

            t0 = time.perf_counter()
            token = set_tool_ctx(child_ctx)
            tool_error: Exception | None = None
            child_run_config = child_ctx.get("_run_config")
            # Full ToolCall envelope so tools with `InjectedToolCallId`
            # (e.g. spawn_subagent for nested spawns) get their call id.
            # When invoked this way, tool.ainvoke returns a ToolMessage
            # instead of the raw value.
            tool_call_envelope = {
                "name": name,
                "args": args,
                "id": call_id,
                "type": "tool_call",
            }
            try:
                if child_run_config is not None:
                    result = await tool.ainvoke(
                        tool_call_envelope, config=child_run_config
                    )
                else:
                    result = await tool.ainvoke(tool_call_envelope)
            except Exception as exc:
                tool_error = exc
                result = f"Tool error: {type(exc).__name__}: {exc}"
            finally:
                reset_tool_ctx(token)

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
                duration_ms=(time.perf_counter() - t0) * 1000.0,
                subagent_type=self.name,
                parent_tool_call_id=parent_tool_call_id,
            )
            if tool_error is None:
                run_post_hooks(name, args, result, child_ctx)
            out.append(ToolMessage(content=result, tool_call_id=call_id, name=name))
        return out


# ---------------------------------------------------------
# Dispatch entry point used by the spawn_subagent tool
# ---------------------------------------------------------
async def run_subagent(
    subagent_type: str,
    prompt: str,
    parent_ctx: dict,
    *,
    timeout_s: int | None = None,
    parent_tool_call_id: str | None = None,
) -> SubagentResult:
    """Look up `subagent_type`, resolve its model, run it, return the result."""
    from src.subagents import get as _get_subagent

    cls = _get_subagent(subagent_type)
    if cls is None:
        from src.subagents import list_types

        avail = ", ".join(list_types()) or "(none)"
        return SubagentResult(
            text=(
                f"Unknown subagent_type '{subagent_type}'. "
                f"Available: {avail}."
            ),
            error="unknown_type",
        )

    # Build per-type config from the subagents config block (set by ui/app.py).
    type_cfg = (parent_ctx.get("subagents_config") or {}).get("types", {}).get(
        subagent_type, {}
    ) or {}

    instance = cls(type_cfg)

    # Resolve model: preferences override → type default → active chat model.
    provider, model = _resolve_subagent_model(subagent_type, parent_ctx, type_cfg)

    # Enforce depth cap.
    max_depth = int((parent_ctx.get("subagents_config") or {}).get("max_depth", 2))
    cur_depth = int(parent_ctx.get("subagent_depth", 0))
    if cur_depth >= max_depth:
        return SubagentResult(
            text=(
                f"[subagent depth limit reached ({cur_depth} >= {max_depth}); "
                "refusing to spawn deeper subagents]"
            ),
            error="depth_limit",
        )

    # Resolve timeout (bounded by config caps).
    cfg = parent_ctx.get("subagents_config") or {}
    default_to = int(
        type_cfg.get("default_timeout_s")
        or cfg.get("default_timeout_s")
        or instance.default_timeout_s
    )
    max_to = int(cfg.get("max_timeout_s", 600))
    if timeout_s is None or timeout_s <= 0:
        effective_to = default_to
    else:
        effective_to = min(int(timeout_s), max_to)

    return await instance.run(
        prompt,
        parent_ctx,
        provider=provider,
        model=model,
        timeout_s=effective_to,
        parent_tool_call_id=parent_tool_call_id,
    )


def _resolve_subagent_model(
    subagent_type: str, parent_ctx: dict, type_cfg: dict
) -> tuple[str, str]:
    """Resolve (provider, model) for a subagent.

    Priority:
      1. preferences.subagent_models[<type>] = {provider, model}  (set via /subagents)
      2. Active chat model (parent's provider + its selected model)

    Model selection is intentionally NOT a config.yml concern — it depends on
    which providers a given user has API keys for, which is per-user state.
    Use `/subagents <type> <provider> <model>` to pin a model per type.
    `type_cfg` is still consulted for non-model defaults (max_iterations,
    default_timeout_s).
    """
    from src.main import provider_configs
    from ui import preferences

    # 1) Preferences override.
    try:
        override = preferences.get_subagent_model(subagent_type)
    except AttributeError:
        override = None
    if override and override.get("provider") and override.get("model"):
        prov = override["provider"]
        prov_cfg = provider_configs.get(prov)
        if isinstance(prov_cfg, dict) and override["model"] in (
            prov_cfg.get("models") or []
        ):
            return prov, override["model"]

    # 2) Active chat model.
    prov = parent_ctx.get("provider")
    if prov:
        model = provider_configs.get(prov, {}).get("model")
        if model:
            return prov, model
    # Last resort — pick any configured provider.
    for name, c in provider_configs.items():
        models = (c or {}).get("models") or []
        if models:
            return name, models[0]
    raise RuntimeError("No providers configured for subagent.")