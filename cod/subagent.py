@tool
async def spawn_subagent(
    subagent_type: str,
    prompt: str,
    timeout_s: int | None = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> str:
    """Spawn a specialised subagent to handle a focused sub-task in isolation.

    The subagent runs its own tool-calling loop with a filtered toolset and a
    dedicated system prompt, then returns a single final report. You only see
    that report -- the subagent's intermediate steps are not surfaced. Use
    this to parallelise independent work or to delegate a deep dive without
    polluting your own context.

    Pick a `subagent_type` that matches the work:
      - `general`      -- open-ended research / multi-step tasks (full toolset)
      - `code_search`  -- read-only code lookups (paths, symbols, snippets)
      - `web_research` -- web search + synthesis (no file/shell access)

    Call this tool multiple times in one assistant turn to spawn subagents
    in parallel -- they run independently.

    Args:
        subagent_type: One of the registered types above.
        prompt: The task description for the subagent. Be specific: it has
            none of your conversation context.
        timeout_s: Optional hard timeout in seconds. Defaults to the type's
            built-in default; capped by config.
    """
    
    from src.subagents import run_subagent

    parent_ctx = _ctx()
    result = await run_subagent(
        subagent_type=(subagent_type or "").strip(),
        prompt=(prompt or "").strip(),
        parent_ctx=parent_ctx,
        timeout_s=timeout_s,
        parent_tool_call_id=tool_call_id or None,
    )
    # Surface result + usage to the parent via the ctx so the tools node
    # can log a SUBAGENT_RESULT session event with proper token attribution.
    if tool_call_id:
        parent_ctx.setdefault("pending_subagent_results", {})[tool_call_id] = {
            "subagent_type": (subagent_type or "").strip(),
            "prompt": (prompt or "").strip(),
            "text": result.text,
            "iterations": result.iterations,
            "tool_call_count": result.tool_call_count,
            "timed_out": result.timed_out,
            "error": result.error,
            "duration_s": result.duration_s,
            "usage": result.usage,
        }
    return result.text