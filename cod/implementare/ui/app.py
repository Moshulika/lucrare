"""
vibe-cli v2 — prompt_toolkit + rich terminal chat UI.

Replaces the Textual TUI with plain terminal scrolling.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import os
import time

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style as PTStyle

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from src import mcp_client
from src.artifacts import ArtifactStore, gc_expired, make_fetch_artifact_tool
from src.compaction import (
    CompactionConfigError,
    build_eager_pipeline,
    build_threshold_pipeline,
    run_eager_pipeline,
    run_threshold_pass,
    threshold_should_fire,
)
from src.log_context import bind_session_id, current_turn_id
from src.logging_setup import (
    configure_logging,
    emit_event,
    get_logger,
    log_app_environment,
)
from src.main import (
    graph,
    config,
    provider_configs,
    providers as PROVIDERS,
    set_active_tools,
)
from src.sandbox import Sandbox
from src.tools import ALL_TOOLS, run_command_host
from src.tracing import setup_tracing, get_tracing_info
from ui.commands import COMMANDS
from ui.sessions import (
    Session,
    KIND_HUMAN,
    KIND_ASSISTANT_TEXT,
    KIND_ASSISTANT_THINKING,
    KIND_TOOL_CALL,
    KIND_TOOL_RESULT,
    KIND_SUBAGENT_CALL,
    KIND_SUBAGENT_RESULT,
    new_turn_id,
)
from ui import preferences
from ui.pastes import configure as _configure_pastes, pastes
from ui.paths import SKILLS_DIR
from ui.profile import get_or_create_profile_id
from ui.provider_handler import (
    prompt_api_key,
    restore_and_validate_providers,
    set_api_key,
    startup_provider_setup,
)
from ui.skills import (
    DEFAULT_MAX_ACTIVE as SKILLS_DEFAULT_MAX,
    load_skills,
    render_skills_directory,
    seed_examples_if_empty,
)
from ui.updater import check_for_update
from ui.ui import (
    console,
    format_elapsed,
    print_message,
    print_splash,
    print_system,
    render_diff_panel,
)


# ---------------------------------------------------------
# Mode system prompts
# ---------------------------------------------------------
MODE_PROMPTS = {
    "plan": (
        "You are in planning mode. Before answering, first create a clear "
        "step-by-step plan. Present the plan, then ask for confirmation "
        "before proceeding with execution."
    ),
    "agent": (
        "You are in agentic mode. You can take autonomous actions, use tools, "
        "and work through multi-step tasks independently. Be proactive and "
        "thorough in completing the user's request."
    ),
}

# Always prepended. Forces the model to close every turn with a short,
# user-facing message so the UI never ends on a silent tool call.
BASE_INSTRUCTIONS = (
    "After running tools, always finish with a short message to the user "
    "summarizing what you did, or — if a tool failed — explaining what went "
    "wrong and what you tried. Never end a turn silently after a tool call.\n\n"
    "Memory: if the user expresses a durable preference about how they like "
    "to work (style, tone, naming, tools they prefer), call the `remember` "
    "tool with scope='global'. If you discover a non-obvious fact about THIS "
    "repo (an architectural decision, owner, gotcha, convention) that future "
    "sessions would benefit from knowing, call `remember` with scope='project'. "
    "Do not save ephemeral task state, recent diffs, anything derivable from "
    "git or the code itself, or secrets."
)

# Appended in headless / --skip-interactions mode. There's no user available
# to answer follow-up questions, so the agent must make a reasonable
# assumption and proceed instead of stalling for confirmation.
HEADLESS_INSTRUCTIONS = (
    "You are running in non-interactive (headless) mode. There is no user "
    "available to answer follow-up questions. Do not ask for clarification, "
    "confirmation, or choices — make a reasonable assumption, state it "
    "briefly in your final message, and proceed. If a tool fails, try an "
    "alternative or report what went wrong; do not stop to ask permission."
)

EFFORT_TEMPS = {
    "low": 0.3,
    "medium": 0.5,
    "high": 0.8,
}


# ---------------------------------------------------------
# Session recording helpers
# ---------------------------------------------------------
def _extract_usage(msg) -> dict:
    """Pull token counts off a LangChain AIMessage, normalised for our schema."""
    um = getattr(msg, "usage_metadata", None) or {}
    if not um:
        return {}
    out = {
        "input": int(um.get("input_tokens") or 0),
        "output": int(um.get("output_tokens") or 0),
        "estimated": False,
    }
    details = um.get("input_token_details") or {}
    if details:
        out["cache_read"] = int(details.get("cache_read") or 0)
        out["cache_write"] = int(details.get("cache_creation") or 0)
    return out


def _build_model_params(provider: str, ctx: dict | None = None) -> dict:
    """Snapshot the params relevant for reproducibility (recorded once per session)."""
    cfg = provider_configs.get(provider, {}) or {}
    params: dict = {
        "temperature": cfg.get("temperature"),
        "model_default": cfg.get("model"),
    }
    if ctx is not None:
        params["effort"] = ctx.get("effort")
        params["mode"] = ctx.get("mode")
    mcp_servers = (
        list(preferences.get_mcp_servers().keys())
        if hasattr(preferences, "get_mcp_servers")
        else []
    )
    if mcp_servers:
        params["mcp_servers"] = mcp_servers
    return {k: v for k, v in params.items() if v is not None}


def _refresh_active_tools(ctx: dict) -> None:
    """Recompute the active tool list, including the per-session fetch_artifact.

    Called after each session creation. The fetch_artifact tool is bound to a
    specific session-id so the agent can re-hydrate `@artifact:<sha>` handles
    produced by the compaction pipeline.
    """
    sess: Session | None = ctx.get("session")
    base = list(ALL_TOOLS) + mcp_client.get_tools()
    if sess is not None:
        base.append(make_fetch_artifact_tool(sess.id))
    # Expose `run_command_host` to the agent only when a sandbox is actually
    # running — when sandboxing is off, `run_command` already runs on the
    # host, so the second tool would be a redundant duplicate.
    if ctx.get("sandbox") is not None:
        base.append(run_command_host)
    set_active_tools(base)


def _attach_from_paste(ctx: dict, path) -> None:
    """Stage a file path pasted into the prompt as an attachment.

    Mirrors `/attach <path>`'s validation + capability-gate flow so the
    same diagnostics surface whether the user typed the command or
    pasted a path. Prints status via the standard system-message helper.
    """
    from src import attachments as _atts
    from src import capabilities as _caps

    sess = ctx.get("session")
    if sess is None:
        print_system("[red]●[/red] No active session — cannot stage attachment.")
        return

    kind = _atts.detect_kind(path)
    if kind is None:
        print_system(
            f"[red]●[/red] {path.name} is not a supported attachment type."
        )
        return

    provider = ctx.get("provider") or ""
    model = provider_configs.get(provider, {}).get("model") or ""
    caps = _caps.get_capabilities(provider, model)
    if not caps.supports(kind):
        peers = [
            p
            for p in _caps.peers_supporting(provider, kind, provider_configs)
            if p != model
        ]
        peer_str = ", ".join(peers) if peers else "(none in this provider)"
        print_system(
            f"[red]●[/red] {model} is known to not accept {kind} input. "
            f"[dim]Switch with /model. Peers that may work: {peer_str}.[/dim]"
        )
        return

    mm_cfg = (ctx.get("multimodal_config") or {}).get("pdf", {}) or {}
    threshold = int(mm_cfg.get("small_threshold_pages", 15))
    dpi = int(mm_cfg.get("render_dpi", 144))
    image_capable = caps.supports("image")
    try:
        att = _atts.load_attachment(
            path,
            session_id=sess.id,
            image_capable=image_capable,
            small_threshold_pages=threshold,
            render_dpi=dpi,
        )
    except _atts.AttachmentError as exc:
        print_system(f"[red]●[/red] {exc}")
        return

    pending = ctx.setdefault("pending_attachments", [])
    try:
        _atts.check_turn_budget(att.size, pending)
    except _atts.AttachmentError as exc:
        print_system(f"[red]●[/red] {exc}")
        return

    pending.append(att)
    extra = ""
    if att.kind == "pdf":
        if att.pdf_render_strategy == "full":
            extra = (
                f" — full mode ({att.pdf_page_count} pages: text + images inline)"
            )
        else:
            extra = (
                f" — manifest mode ({att.pdf_page_count} pages; model reads "
                "pages via read_pdf_pages)"
            )
    print_system(
        f"[success]📎[/success] Attached [bold]{att.name}[/bold] "
        f"[muted]({att.kind}, {att.size / 1024:.1f} KB)[/muted]{extra}"
    )


def _auto_attach_inline_paths(ctx: dict, user_text: str) -> str:
    """Detect existing file paths in `user_text`, attach them, and rewrite
    the references to `[attached: name]` markers so the model treats them
    as proper attachments rather than asking to read from local disk.

    Skips paths inside backtick code spans (the escape hatch when the user
    really wants to mention a path without attaching).
    """
    from src import attachments as _atts
    from src import capabilities as _caps

    if not user_text:
        return user_text
    candidates = _atts.extract_existing_file_paths(user_text)
    if not candidates:
        return user_text

    sess = ctx.get("session")
    if sess is None:
        return user_text

    mm_cfg = (ctx.get("multimodal_config") or {}).get("pdf", {}) or {}
    threshold = int(mm_cfg.get("small_threshold_pages", 15))
    dpi = int(mm_cfg.get("render_dpi", 144))

    provider = ctx.get("provider") or ""
    model = provider_configs.get(provider, {}).get("model") or ""
    caps = _caps.get_capabilities(provider, model)
    image_capable = caps.supports("image")
    pending = ctx.setdefault("pending_attachments", [])

    # Avoid re-attaching files that are already pending (e.g. user already
    # ran /attach for the same path and then re-typed it).
    already_pending = {a.sha for a in pending}

    updated = user_text
    for token, path in candidates:
        kind = _atts.detect_kind(path)
        if kind is None:
            continue
        if not caps.supports(kind):
            # Capability-gated: don't attach silently; leave the path
            # in the text so the user sees why nothing happened.
            continue
        try:
            att = _atts.load_attachment(
                path,
                session_id=sess.id,
                image_capable=image_capable,
                small_threshold_pages=threshold,
                render_dpi=dpi,
            )
        except _atts.AttachmentError as exc:
            print_system(
                f"[warning]●[/warning] couldn't auto-attach {path.name}: {exc}"
            )
            continue
        if att.sha in already_pending:
            # Same file is already staged — just rewrite the reference.
            updated = updated.replace(token, f"[attached: {path.name}]")
            continue
        try:
            _atts.check_turn_budget(att.size, pending)
        except _atts.AttachmentError as exc:
            print_system(
                f"[warning]●[/warning] couldn't auto-attach {path.name}: {exc}"
            )
            continue
        pending.append(att)
        already_pending.add(att.sha)
        updated = updated.replace(token, f"[attached: {path.name}]")
        print_system(
            f"[success]📎[/success] auto-attached [bold]{path.name}[/bold] "
            f"[muted]({att.kind}, {att.size / 1024:.1f} KB) from your "
            f"message[/muted]"
        )
    return updated


def _make_capability_filter(ctx: dict):
    """Return a callable `(kind) -> bool` for the active model."""
    from src import capabilities as _caps

    provider = ctx.get("provider") or ""
    model = provider_configs.get(provider, {}).get("model") or ""
    caps = _caps.get_capabilities(provider, model)
    return caps.supports


def _hydrate_session_attachments(ctx: dict) -> None:
    """Populate `ctx["session_attachments"]` from a freshly-loaded session.

    Called after `/resume` (and on session create) so `read_pdf_pages` and
    similar cross-turn tools can resolve attachments referenced earlier in
    the session.
    """
    sess = ctx.get("session")
    if sess is None:
        return
    registry = ctx.setdefault("session_attachments", {})
    try:
        from src.attachments import Attachment as _Attachment
    except Exception:
        return
    for evt in sess.events:
        if evt.get("kind") != KIND_HUMAN:
            continue
        content = evt.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "attachment":
                continue
            try:
                att = _Attachment.from_block(block)
            except Exception:
                continue
            if att.name:
                registry[att.name] = att


def _drop_incompatible_attachments(ctx: dict) -> int:
    """Drop pending attachments incompatible with the active model, returning
    the count dropped. Prints a warning if any were dropped."""
    from src import capabilities as _caps

    pending = ctx.get("pending_attachments") or []
    if not pending:
        return 0
    provider = ctx.get("provider") or ""
    model = provider_configs.get(provider, {}).get("model") or ""
    caps = _caps.get_capabilities(provider, model)
    kept: list = []
    dropped: list = []
    for att in pending:
        if caps.supports(att.kind):
            kept.append(att)
        else:
            dropped.append(att)
    if dropped:
        ctx["pending_attachments"] = kept
        names = ", ".join(f"{a.name} ({a.kind})" for a in dropped)
        print_system(
            f"[warning]●[/warning] Dropped {len(dropped)} attachment(s) — "
            f"{model} doesn't accept them: {names}"
        )
    return len(dropped)


def _on_session_created(ctx: dict) -> None:
    """Hook fired after `ctx['session']` is replaced.

    - Spins up a fresh per-session ArtifactStore.
    - Runs an opportunistic GC of expired artifact directories (lazy, stat-based).
    - Refreshes the agent's active-tool list to bind fetch_artifact to the
      new session.
    """
    sess: Session | None = ctx.get("session")
    if sess is None:
        ctx["artifact_store"] = None
        bind_session_id(None)
        return
    ctx["artifact_store"] = ArtifactStore(sess.id)
    bind_session_id(sess.id)
    emit_event(
        get_logger("session"),
        "session.created",
        session_id=sess.id,
        provider=ctx.get("provider"),
        model=provider_configs.get(ctx.get("provider") or "", {}).get("model"),
    )
    try:
        gc_expired()
    except Exception:
        # GC is best-effort — never let it block startup.
        pass
    _refresh_active_tools(ctx)


def _tool_persistence_enabled(ctx: dict) -> bool:
    """True when `compaction.tool_persistence` is set in config.

    Single source of truth — read from `ctx["compaction_config"]` which is
    populated once at startup. Slash commands and the turn loop both use this
    so the live message buffer and the resumed projection agree.
    """
    cfg = ctx.get("compaction_config") or {}
    return bool(cfg.get("tool_persistence"))


def _enforce_session_model_enabled(ctx: dict) -> bool:
    """True when `compaction.enforce_session_model` is on (default true).

    Only meaningful when `tool_persistence` is also on — there's nothing to
    enforce when tool messages don't cross turn boundaries.
    """
    cfg = ctx.get("compaction_config") or {}
    val = cfg.get("enforce_session_model")
    return True if val is None else bool(val)


def _resolve_compaction_llm(ctx: dict):
    """Resolve the LLM used by compaction stages (bulk + tool_summarize).

    Resolution order:

    1. **`preferences.compaction_model`** — explicit user override set via
       `/compaction model`. We instantiate the override using the same
       provider config (api_key, temperature, base_url) as the chat model
       but with the `model` field swapped to the chosen one. If the
       provider has no valid API key, we silently skip and fall back.
    2. **Active chat model** — same provider/model the user is talking to.
       Historical behaviour; safe default.

    Returns None on any failure. Threshold stages (bulk) and the
    tool_summarize background task both treat `llm is None` as a "no
    upgrade possible — fall back" signal, so a None here never crashes
    the pipeline; it just means stages run in their cheaper-fallback
    mode.

    Resolution decisions are logged once per turn at DEBUG level so
    operators can confirm which model is doing the compaction work
    without wading through full traces.
    """
    from copy import deepcopy

    from src.main import get_model
    from src.providers import get_provider

    # 1) Explicit override from preferences.
    override = preferences.get_compaction_model()
    if override is not None:
        prov = override["provider"]
        model = override["model"]
        prov_cfg = provider_configs.get(prov)
        if isinstance(prov_cfg, dict) and prov_cfg.get("api_key"):
            try:
                cfg_copy = deepcopy(prov_cfg)
                cfg_copy["model"] = model
                return get_provider(prov).create_model(cfg_copy)
            except Exception as exc:
                # Override failed to instantiate — log once and fall through.
                emit_event(
                    get_logger("compaction"),
                    "compaction.llm_resolve",
                    level=logging.WARNING,
                    source="preferences_override",
                    provider=prov,
                    model=model,
                    ok=False,
                    error_type=type(exc).__name__,
                )
        else:
            emit_event(
                get_logger("compaction"),
                "compaction.llm_resolve",
                level=logging.WARNING,
                source="preferences_override",
                provider=prov,
                model=model,
                ok=False,
                reason="no_api_key",
            )

    # 2) Fall back to the active chat model.
    try:
        return get_model(ctx["provider"])
    except Exception:
        return None


def _context_utilisation(ctx: dict) -> tuple[float, int, int]:
    """Return (fraction, current_size, limit) for the active session.

    Returns `(0.0, 0, 0)` when there's no session yet or the limit is
    unknown. Callers should guard against `limit <= 0` before showing
    a percentage to the user.
    """
    sess: Session | None = ctx.get("session")
    if sess is None:
        return 0.0, 0, 0
    m = sess.metrics.get("context") or {}
    cur = int(m.get("current_size") or 0)
    lim = int(m.get("limit") or 0)
    if lim <= 0:
        return 0.0, cur, lim
    return cur / lim, cur, lim


def _compaction_threshold(ctx: dict) -> float:
    """Configured `compaction.trigger.threshold`, or 0 when unset.

    A threshold of 0 means "no warning" — the banner and toolbar marker
    should silently no-op so users who deliberately disable compaction
    don't get noise.
    """
    cfg = ctx.get("compaction_config") or {}
    return float((cfg.get("trigger") or {}).get("threshold") or 0.0)


def _maybe_show_context_warning(ctx: dict) -> None:
    """One-shot inline banner when context crosses the configured threshold.

    Why one-shot: re-rendering it every turn becomes noise. Why a banner
    *and* a toolbar marker: the banner draws the eye the first time
    (which is what users running compaction-off experiments want); the
    toolbar marker keeps the state visible afterwards.

    Resets:
    - `/clear` and Ctrl+L → fresh session, fresh warning.
    - Threshold compaction (KIND_COMPACTION) → freed budget, fresh warning.
    """
    threshold = _compaction_threshold(ctx)
    if threshold <= 0:
        return
    util, cur, lim = _context_utilisation(ctx)
    if lim <= 0 or util < threshold:
        return
    if ctx.get("context_warning_shown"):
        return

    threshold_pipeline = ctx.get("compaction_threshold_pipeline") or []
    eager_pipeline = ctx.get("compaction_pipeline") or []
    compaction_active = bool(threshold_pipeline or eager_pipeline)

    pct = util
    cur_str = f"{cur:,}"
    lim_str = f"{lim:,}"
    if compaction_active:
        # Compaction *is* configured — the banner is informational, the
        # threshold pass should kick in shortly. Soft tone.
        body = (
            f"[bold]Context at {pct:.0%}[/bold]  "
            f"[dim]({cur_str} / {lim_str} tok)[/dim]\n"
            f"[dim]Threshold compaction will run at {threshold:.0%}.[/dim]"
        )
    else:
        # Compaction is OFF — this is the experiment-mode case the user
        # explicitly asked us to surface. Loud tone.
        body = (
            f"[bold yellow]⚠  Context at {pct:.0%}[/bold yellow]  "
            f"[dim]({cur_str} / {lim_str} tok)[/dim]\n"
            f"[bold]Compaction is disabled[/bold] — the next turn may "
            f"exceed the model's window.\n"
            f"[dim]Inspect: /compaction · force a pass: /compact · "
            f"or enable a stage in config.yml.[/dim]"
        )
    try:
        from rich.panel import Panel

        console.print()
        console.print(Panel(body, border_style="yellow", padding=(0, 1), expand=False))
    except Exception:
        # Defensive — never let a UI render failure crash the turn loop.
        print_system(body)
    ctx["context_warning_shown"] = True


def _reset_context_warning(ctx: dict) -> None:
    """Clear the one-shot guard. Called on /clear and after a compaction."""
    ctx["context_warning_shown"] = False


async def _maybe_threshold_pass(ctx: dict, messages: list) -> None:
    """Run the threshold pipeline if utilisation has crossed the trigger.

    On any compaction the in-memory `messages` list is rebuilt from the
    session's `to_messages()` projection — which now honours the just-emitted
    KIND_COMPACTION's `removed_event_ids` and any eager-stage `projected`
    fields. This keeps the live LLM context and the on-disk truth in lockstep.
    """
    sess: Session | None = ctx.get("session")
    pipeline = ctx.get("compaction_threshold_pipeline") or []
    store = ctx.get("artifact_store")
    if sess is None or store is None or not pipeline:
        return
    if not threshold_should_fire(sess, ctx.get("compaction_config")):
        return

    llm = _resolve_compaction_llm(ctx)
    report = await run_threshold_pass(
        sess,
        pipeline=pipeline,
        store=store,
        compaction_config=ctx.get("compaction_config"),
        llm=llm,
    )
    ctx["last_compaction_report"] = report
    if not report.ran:
        return

    # Replace in-memory messages with the compacted projection so the next
    # LLM call sees what the session file says happened. The projection
    # honours the tool_persistence flag so post-compaction history shape
    # matches what the live loop is producing.
    new_msgs = sess.to_messages(
        tool_persistence=_tool_persistence_enabled(ctx),
        provider=ctx.get("provider"),
        capability_filter=_make_capability_filter(ctx),
    )
    messages.clear()
    messages.extend(new_msgs)
    print_system(
        f"[dim]compacted: −{report.freed} tok across "
        f"{len(report.stages)} stage(s) → {report.after_size} tok[/dim]"
    )
    # Compaction freed budget → re-arm the one-shot context warning so
    # the user gets fresh feedback if they cross the threshold again.
    _reset_context_warning(ctx)


def _rollup_eager_metrics(sess: "Session", event: dict) -> None:
    """Update `sess.metrics['compaction_eager']` from an event's stage stamps.

    Each eager stage that fires on an event leaves a marker under
    `event['meta']['compaction'][<stage_name>]`. This function aggregates
    those markers session-wide so `/compaction` (and future analytics) can
    answer "how often has tool_truncate fired and how many bytes has it
    moved out of the LLM context."
    """
    stamps = (event.get("meta") or {}).get("compaction") or {}
    if not stamps:
        return
    rollup = sess.metrics.setdefault("compaction_eager", {})
    for stage_name, info in stamps.items():
        entry = rollup.setdefault(
            stage_name,
            {"count": 0, "original_bytes_total": 0, "last_event_id": None},
        )
        entry["count"] += 1
        original = int(info.get("original_bytes") or info.get("original_chars") or 0)
        entry["original_bytes_total"] += original
        entry["last_event_id"] = event.get("id")


async def _run_eager(ctx: dict, event: dict) -> dict:
    """Run the configured eager pipeline against `event`, persisting on change.

    Returns the event (mutated in place if any stage modified it). Caller is
    responsible for mirroring `event['projected']` back into the live
    LangChain message stream when relevant (tool results).

    Async because some stages (e.g. `tool_summarize`) may spawn an LLM
    background task. The synchronous portion still runs end-to-end before
    we return — the background tasks live in `ctx['pending_summary_tasks']`
    and are drained at the next turn boundary.
    """
    pipeline = ctx.get("compaction_pipeline") or []
    store: ArtifactStore | None = ctx.get("artifact_store")
    if not pipeline or store is None:
        return event
    sess: Session | None = ctx.get("session")

    def _on_event_updated(evt: dict) -> None:
        """Re-roll metrics + persist after a background task patches an event.

        Stages that schedule async work (e.g. `tool_summarize`) call this
        once they've replaced `evt['projected']` with the upgraded body so
        the change reaches disk and the metrics rollup notices.
        """
        if sess is None:
            return
        _rollup_eager_metrics(sess, evt)
        try:
            sess.save()
        except Exception:
            # Save failure on a background patch is non-fatal — the
            # truncated fallback is already on disk.
            pass

    modified, bg_tasks = await run_eager_pipeline(
        event,
        pipeline,
        store,
        session_id=sess.id if sess is not None else None,
        llm=_resolve_compaction_llm(ctx),
        on_event_updated=_on_event_updated,
    )
    if modified and sess is not None:
        _rollup_eager_metrics(sess, event)
        sess.save()

    if bg_tasks:
        ctx.setdefault("pending_summary_tasks", []).extend(bg_tasks)
    return event


async def _drain_pending_summary_tasks(ctx: dict, *, timeout: float = 2.0) -> None:
    """Wait up to `timeout` seconds for in-flight eager background tasks.

    Tasks that finish in time have already patched their events via the
    `on_event_updated` callback (which re-saves the session). Tasks that
    miss the budget stay scheduled — they keep running, and if they
    finish later they patch + save in place so the *next* turn sees the
    upgrade. We never cancel them: the LLM call has already been billed
    and the patch is still useful, just late.
    """
    pending = ctx.get("pending_summary_tasks") or []
    if not pending:
        return
    not_done: list = list(pending)
    try:
        done, not_done_set = await asyncio.wait(pending, timeout=timeout)
        not_done = list(not_done_set)
    except Exception:
        # Defensive — `asyncio.wait` shouldn't raise on a list of Tasks,
        # but if anything goes wrong we keep the original list and let
        # them complete on their own schedule.
        pass
    # Keep tasks that didn't finish; the next drain cycle picks them up.
    ctx["pending_summary_tasks"] = [t for t in not_done if not t.done()]


def _tool_source(name: str) -> str:
    """Classify a tool name as builtin or mcp:<server>.

    MCP tools are registered as `<server>__<tool>`. Built-in tools have
    a flat name (web_search, read_file, etc).
    """
    if "__" in name:
        return "mcp:" + name.split("__", 1)[0]
    return "builtin"


async def _record_assistant(
    sess: Session,
    ai_msg,
    turn_id: str,
    ctx: dict,
    *,
    request_started_at: str | None = None,
    duration_ms: float | None = None,
) -> None:
    """Split an AIMessage into thinking / text / tool_call events.

    Token usage from the LLM call is attached to a single assistant_text
    event so input_total / output_total are not double-counted across
    blocks. Per-block content sizes still drive the by_kind breakdown.
    """
    llm_call_id = new_turn_id()
    usage = _extract_usage(ai_msg)
    content = ai_msg.content

    text_blocks: list[str] = []
    thinking_blocks: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type", "")
                if btype == "thinking":
                    thinking_blocks.append(
                        block.get("thinking") or block.get("text") or ""
                    )
                elif btype == "text":
                    text_blocks.append(block.get("text") or "")
            else:
                text_blocks.append(str(block))
    elif content:
        text_blocks.append(content if isinstance(content, str) else str(content))

    for tb in thinking_blocks:
        evt = sess.append_event(
            KIND_ASSISTANT_THINKING, tb, turn_id=turn_id, llm_call_id=llm_call_id
        )
        await _run_eager(ctx, evt)

    text_evt = sess.append_event(
        KIND_ASSISTANT_TEXT,
        "\n".join(text_blocks),
        tokens=usage,
        turn_id=turn_id,
        llm_call_id=llm_call_id,
        request_started_at=request_started_at,
        duration_ms=duration_ms,
    )
    await _run_eager(ctx, text_evt)

    for tc in getattr(ai_msg, "tool_calls", None) or []:
        name = tc.get("name") or ""
        tc_evt = sess.append_event(
            KIND_TOOL_CALL,
            tc.get("args") or {},
            meta={
                "name": name,
                "tool_call_id": tc.get("id") or "",
                "source": _tool_source(name),
            },
            turn_id=turn_id,
            llm_call_id=llm_call_id,
        )
        await _run_eager(ctx, tc_evt)


async def _record_tool_result(sess: Session, tool_msg, turn_id: str, ctx: dict) -> None:
    content = tool_msg.content
    text = content if isinstance(content, str) else str(content)
    name = getattr(tool_msg, "name", "") or ""
    tcid = getattr(tool_msg, "tool_call_id", "") or ""
    meta: dict = {
        "name": name,
        "tool_call_id": tcid,
        "source": _tool_source(name),
    }
    # Attach precondition-denial info (set by tool_guards.check_preconditions
    # via src/main.py:_run_one_tool_call). Lets the web UI render a
    # "rejected" tag with the guard name + reason as a tooltip.
    if tcid:
        denials = ctx.get("pending_precondition_denials") or {}
        denial = denials.pop(tcid, None)
        if denial is not None:
            meta["precondition_denied"] = True
            meta["guard"] = denial.get("guard")
            meta["denial_reason"] = denial.get("reason")
    evt = sess.append_event(
        KIND_TOOL_RESULT,
        text,
        meta=meta,
        turn_id=turn_id,
    )
    await _run_eager(ctx, evt)

    # If this was a spawn_subagent call, also emit dedicated subagent events
    # with proper token attribution. The tool stashed the structured result
    # on ctx["pending_subagent_results"][tool_call_id].
    if name == "spawn_subagent" and tcid:
        pending = ctx.get("pending_subagent_results") or {}
        info = pending.pop(tcid, None)
        if info is not None:
            sub_call = sess.append_event(
                KIND_SUBAGENT_CALL,
                info.get("prompt") or "",
                meta={
                    "subagent_type": info.get("subagent_type"),
                    "tool_call_id": tcid,
                    "depth": (ctx.get("subagent_depth") or 0) + 1,
                },
                turn_id=turn_id,
            )
            usage = info.get("usage") or {}
            sess.append_event(
                KIND_SUBAGENT_RESULT,
                info.get("text") or "",
                tokens={
                    "input": int(usage.get("input") or 0),
                    "output": int(usage.get("output") or 0),
                    "cache_read": int(usage.get("cache_read") or 0),
                    "cache_write": int(usage.get("cache_write") or 0),
                },
                meta={
                    "subagent_type": info.get("subagent_type"),
                    "tool_call_id": tcid,
                    "iterations": info.get("iterations"),
                    "tool_call_count": info.get("tool_call_count"),
                    "timed_out": bool(info.get("timed_out")),
                    "error": info.get("error"),
                    "depth": (ctx.get("subagent_depth") or 0) + 1,
                },
                turn_id=turn_id,
                parent_id=sub_call["id"],
                duration_ms=(info.get("duration_s") or 0.0) * 1000.0,
            )
    # Mirror the projected (compacted) body back into the live LangChain
    # message so the *next* LLM call within this tool loop sees it.
    # NOTE: `tool_summarize`'s background task may further patch this
    # event later. The patched body is what the *next turn* sees via
    # `Session.to_messages()`; the current turn keeps the truncated
    # fallback (which is correct, just less rich).
    projected = evt.get("projected")
    if projected is not None:
        tool_msg.content = projected if isinstance(projected, str) else str(projected)


# ---------------------------------------------------------
# Slash command completer
# ---------------------------------------------------------
class SlashCompleter(Completer):
    """Suggest slash commands when the input starts with '/'."""

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor

        if not text.startswith("/"):
            return

        partial = text[1:].lower()

        for name in sorted(COMMANDS):
            if name.startswith(partial):
                desc = COMMANDS[name][0]
                yield Completion(
                    f"/{name}",
                    start_position=-len(text),
                    display=HTML(f"<b>/{name}</b>"),
                    display_meta=desc,
                )


# ---------------------------------------------------------
# Main loop
# ---------------------------------------------------------
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="vibe", add_help=True)
    parser.add_argument(
        "--skip-permissions",
        action="store_true",
        default=os.environ.get("VIBE_SKIP_PERMISSIONS") == "1",
        help="Skip the per-tool approval prompt (auto-allow every tool call).",
    )
    parser.add_argument(
        "-p",
        "--prompt",
        default=None,
        help=(
            "Run a single non-interactive turn with this prompt and exit. "
            "Use '-' to read the prompt from stdin."
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        choices=("text", "json"),
        default="text",
        help="Output format for -p/--prompt mode (default: text).",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="Override the provider for headless mode (defaults to preferences).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the model for headless mode (defaults to preferences).",
    )
    parser.add_argument(
        "--allowedTools",
        "--allowed-tools",
        dest="allowed_tools",
        default=None,
        help=(
            "Comma-separated list of tool names the agent may call in headless "
            "mode (e.g. 'read_file,list_directory'). Default: all built-in "
            "tools. Unknown names exit with an error."
        ),
    )
    parser.add_argument(
        "--max-turns",
        dest="max_turns",
        type=int,
        default=None,
        help=(
            "Cap the number of agentic turns (one LLM call + its tool batch) "
            "in headless mode. The loop stops cleanly once the cap is reached."
        ),
    )
    parser.add_argument(
        "--effort",
        choices=("low", "medium", "high"),
        default=None,
        help=(
            "Reasoning effort. Maps to a temperature via EFFORT_TEMPS "
            "(low=0.3, medium=0.5, high=0.8). Overridden by --temperature."
        ),
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help=(
            "Sampling temperature passed to the model. Overrides --effort "
            "and config.yml. Ignored by providers that don't support it."
        ),
    )
    parser.add_argument(
        "--system",
        dest="system_prompt",
        default=None,
        help=(
            "Replace the default system prompt for this run. Mutually "
            "exclusive with --append-system-prompt."
        ),
    )
    parser.add_argument(
        "--append-system-prompt",
        dest="append_system_prompt",
        default=None,
        help="Append text to the default system-prompt stack for this run.",
    )
    parser.add_argument(
        "--save-session",
        action="store_true",
        default=False,
        help=(
            "Persist a session log for headless turns (default: off, runs "
            "are hermetic). Sessions land under ~/.vibe-cli/sessions/."
        ),
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help=(
            "Stream tool calls and intermediate text to stderr in headless "
            "mode. Final stdout payload is unchanged."
        ),
    )
    parser.add_argument(
        "--skip-interactions",
        "--skipInteractions",
        dest="skip_interactions",
        action="store_true",
        default=os.environ.get("VIBE_SKIP_INTERACTIONS") == "1",
        help=(
            "Run without any interactive prompts: implies --skip-permissions "
            "and instructs the agent not to ask clarifying questions. "
            "Auto-enabled by -p/--prompt."
        ),
    )
    parser.add_argument(
        "--ollama-url",
        dest="ollama_url",
        default=None,
        help=(
            "Override the Ollama base URL for this run. Wins over the "
            "OLLAMA_URL env var and the providers.ollama.base_url in "
            "config.yml."
        ),
    )
    sub = parser.add_subparsers(dest="cmd")
    web = sub.add_parser("web", help="Launch the analytics web panel")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=5050)
    web.add_argument(
        "--no-browser", action="store_true", help="Don't auto-open the browser"
    )
    ev = sub.add_parser("eval", help="Run the evaluation suite")
    # The top-level parser only needs to accept these flags so users can
    # type `vibe eval --foo`; the eval package re-parses argv via its own
    # parser in eval/__main__.py. Keep this list in sync with that parser.
    ev.add_argument(
        "datasets",
        nargs="*",
        metavar="DATASET",
        help="Dataset files (.yaml/.json). Appended to config/eval.yml datasets.",
    )
    ev.add_argument("--config", "-c", default=None, help="Path to eval.yml")
    ev.add_argument("--provider", default=None)
    ev.add_argument("--model", default=None)
    ev.add_argument("--concurrency", type=int, default=None)
    ev.add_argument("--timeout", type=int, default=None, metavar="SECONDS")
    ev.add_argument("--output-dir", default=None)
    ev.add_argument("--report-name", default=None, metavar="NAME")
    ev.add_argument("--cases-csv", default=None, metavar="PATH")
    ev.add_argument("--models-csv", default=None, metavar="PATH")
    # `--json-out` is only used by `vibe eval merge ...`, but we declare
    # it here so the top-level parser accepts it. eval/__main__.py
    # re-parses its own argv and routes by sub-subcommand.
    ev.add_argument("--json-out", default=None, metavar="PATH")
    # `vibe eval --all` sweep mode (kept in sync with eval/__main__.py).
    ev.add_argument("--all", action="store_true")
    ev.add_argument("--no-all", action="store_true")
    ev.add_argument("--providers", default=None, metavar="LIST")
    ev.add_argument("--models-filter", default=None, metavar="REGEX")
    ev.add_argument("--skip-validation", action="store_true")
    ev.add_argument("--continue-on-error", action="store_true", default=True)
    ev.add_argument(
        "--no-continue-on-error",
        dest="continue_on_error",
        action="store_false",
    )
    ev.add_argument("--verbose", "-v", action="store_true")
    ev.add_argument(
        "--no-save", action="store_true", help="Print JSON report to stdout"
    )
    return parser.parse_args()


async def main(cli_args: argparse.Namespace | None = None):
    try:
        await _main(cli_args)
    except Exception as e:
        # Top-level safety net: log a structured error before the exception
        # tears the process down. Re-raises so existing crash semantics are
        # preserved (the operator still sees the traceback).
        try:
            get_logger("error").error(
                "error.uncaught",
                extra={
                    "event": "error.uncaught",
                    "data": {"error_type": type(e).__name__, "where": "main"},
                },
                exc_info=True,
            )
        except Exception:
            # Logging itself failing must never mask the original exception.
            pass
        raise


async def _main(cli_args: argparse.Namespace | None = None):
    if cli_args is None:
        cli_args = argparse.Namespace(skip_permissions=False)

    # -- Fail early if no providers configured --
    if not PROVIDERS:
        console.print()
        console.print("[bold red]Error: No providers configured.[/bold red]")
        console.print()
        console.print(
            "Please add at least one provider with a [bold]models[/bold] list "
            "to [accent]config.yml[/accent]."
        )
        console.print()
        console.print("[dim]Example:[/dim]")
        console.print("[dim]  openai:[/dim]")
        console.print("[dim]    api_key: ${OPENAI_API_KEY}[/dim]")
        console.print("[dim]    models:[/dim]")
        console.print("[dim]      - gpt-4o[/dim]")
        console.print("[dim]      - gpt-4o-mini[/dim]")
        console.print("[dim]    temperature: 0[/dim]")
        console.print()
        sys.exit(1)

    # -- Logging: configure once, before anything else interesting happens. --
    log_cfg = config.get("logging") or {}
    configure_logging(
        enabled=bool(log_cfg.get("enabled", True)),
        level=log_cfg.get("level"),
        log_dir=log_cfg.get("dir"),
        text_max_bytes=int(log_cfg.get("text_max_bytes") or 5 * 1024 * 1024),
        text_backups=int(log_cfg.get("text_backups") or 10),
        jsonl_retention_days=int(log_cfg.get("jsonl_retention_days") or 30),
    )
    app_log = get_logger("app")
    emit_event(
        app_log,
        "app.start",
        **log_app_environment(),
        providers_configured=list(PROVIDERS),
        skip_permissions=bool(cli_args.skip_permissions),
    )

    prefs = preferences.load()
    messages: list = []
    # Build both compaction pipelines once. All stages are disabled by default
    # in config.yml; flip them on individually to A/B compare against the
    # pre-compaction snapshots in ~/.vibe-cli/session_backups/.
    #
    # `build_eager_pipeline` enforces mutex groups (e.g. tool_truncate vs.
    # tool_summarize vs. ref_substitute — they all rewrite the same event
    # slot and only one can be live at once). On a config error we abort
    # with a red banner pointing at config.yml so the user can see exactly
    # which stages clash.
    compaction_cfg = config.get("compaction")
    try:
        eager_pipeline = build_eager_pipeline(compaction_cfg)
        threshold_pipeline = build_threshold_pipeline(compaction_cfg)
    except CompactionConfigError as exc:
        console.print()
        console.print(f"[bold red]{exc}[/bold red]")
        console.print()
        console.print("[dim]Edit config.yml and try again.[/dim]")
        console.print()
        sys.exit(1)

    # Apply config-driven paste folding limits (threshold + per-turn cap).
    pastes_cfg = (config.get("ui") or {}).get("pastes") or {}
    _configure_pastes(
        threshold=pastes_cfg.get("threshold"),
        max_per_turn=pastes_cfg.get("max_per_turn"),
    )
    # Seed example skills on first run, then load the skills index. Bodies
    # stay on disk and are loaded on demand by the run_skill tool — only the
    # name+description+when_to_use directory goes into the system prompt.
    skills_cfg = config.get("skills") or {}
    skills_max_active = int(skills_cfg.get("max_active") or SKILLS_DEFAULT_MAX)
    from pathlib import Path as _Path

    examples_dir = _Path(__file__).resolve().parent.parent / "examples" / "skills"
    seeded = seed_examples_if_empty(examples_dir)
    # Project skills (./.vibe/skills) are merged with the global index;
    # project entries override global on filename collision.
    skills_index = load_skills(max_active=skills_max_active, cwd=_Path.cwd())

    ctx = {
        "provider": PROVIDERS[0],
        "messages": messages,
        "effort": prefs.get("effort", "medium"),
        "mode": None,
        "skills_index": skills_index,
        "skip_permissions": cli_args.skip_permissions,
        "session": None,
        "compaction_pipeline": eager_pipeline,
        "compaction_threshold_pipeline": threshold_pipeline,
        "compaction_config": compaction_cfg,
        "artifact_store": None,
        "last_compaction_report": None,
        "update_info": None,
        # Background tasks spawned by eager stages (currently only
        # tool_summarize). Drained at the top of each new turn so the
        # next prompt sees patched events when the LLM upgrade lands in
        # time, but never blocks longer than `summarize_drain_timeout`.
        "pending_summary_tasks": [],
        # One-shot guard so the 80%-context warning banner only fires
        # the first time it crosses per session. Reset on /clear and on
        # any threshold compaction.
        "context_warning_shown": False,
        # Subagents config (read by spawn_subagent → run_subagent for
        # timeout caps, depth caps, per-type defaults). Models per type
        # live in preferences.json.
        "subagents_config": config.get("subagents") or {},
        # Depth counter — incremented by each child run_subagent call so
        # the registry can enforce `subagents.max_depth`.
        "subagent_depth": 0,
        # Tool guards (deterministic preconditions like read-before-write).
        # The config is read by `src/tool_guards.py:_config`; `file_reads`
        # is the per-session SHA tracker the read-before-write guard relies
        # on. Shared with subagent children via ctx shallow-copy.
        "tool_guards_config": config.get("tool_guards") or {},
        "file_reads": {},
        # Multimodal: attachments staged for the next turn (drained on submit)
        # and a session-scoped registry indexed by name so `read_pdf_pages`
        # can resolve PDFs across turns.
        "pending_attachments": [],
        "session_attachments": {},
        # PDF tier config (small_threshold_pages / render_dpi /
        # max_pages_per_call). Read by load_attachment to pick full vs
        # manifest mode, and by read_pdf_pages for the page-range cap.
        "multimodal_config": config.get("multimodal") or {},
        # VIBE.md learnings (global + project). Read in the system-prompt
        # assembly each turn so writes from `remember` show up immediately.
        # `enabled` and `max_bytes_per_file` live under config.yml `vibe_md:`.
        "vibe_md_config": config.get("vibe_md") or {},
    }

    # Kick off the git update check in the background — we let it finish
    # while the user does provider setup so it never adds startup latency.
    update_task: asyncio.Task = asyncio.create_task(check_for_update())

    # Start provider key validation in parallel with the rest of UI
    # setup (splash, prompt_toolkit, tracing, profile). Each provider's
    # validate() makes a remote metadata call worth a few hundred ms to
    # several seconds; running them concurrently hides that cost behind
    # work we'd be doing anyway.
    #
    # `create_task` only schedules — it doesn't run the coroutine until
    # the event loop gets control. The synchronous work below
    # (print_splash, prompt_toolkit setup, setup_tracing, profile I/O)
    # would otherwise hold the loop the entire time, leaving validation
    # to fall on the awaiting end. The two `sleep(0)` yields let the
    # task start and progress past `gather(to_thread(...))` — far enough
    # to actually submit each provider's validate() onto the thread pool
    # — so the OS threads run concurrently with the sync setup below.
    provider_validation_task: asyncio.Task = asyncio.create_task(
        restore_and_validate_providers()
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    print_splash()

    # -- prompt_toolkit setup --
    bindings = KeyBindings()

    ui_log = get_logger("ui")

    @bindings.add(Keys.BracketedPaste)
    def _on_paste(event):
        """Handle bracketed paste:

        1. If the paste is a single existing file path of a supported kind
           (image / PDF / text), route it to `/attach` instead of inserting
           text. Covers the macOS Finder copy → terminal paste flow.
        2. Otherwise, fold large pastes into a `[Paste #N <len> chars]`
           placeholder; small pastes are inserted as-is.

        Side-output (status messages, toolbar refresh) is wrapped in
        `run_in_terminal` because we're inside a keybinding callback —
        prompt_toolkit is actively drawing the prompt, so writing Rich
        markup straight to stdout would race the redraw and leave raw
        ANSI codes on screen. `run_in_terminal` releases the terminal
        for the duration of the helper, then restores the prompt.
        """
        data = event.data
        # File-path detection — the whole paste, stripped, must be one
        # existing path. We treat single-line, modest-length payloads as
        # candidates so we don't pay path-stat cost on every paste.
        stripped = data.strip()
        if stripped and "\n" not in stripped and len(stripped) <= 4096:
            candidate = stripped.strip('"').strip("'")
            try:
                from pathlib import Path as _Path

                p = _Path(candidate).expanduser()
                if p.is_file():
                    from src.attachments import detect_kind as _detect

                    if _detect(p) is not None:
                        # Defer the attach + Rich output to outside the
                        # prompt's active-draw window. invalidate() then
                        # forces the bottom toolbar to redraw with the
                        # new 📎 N count.
                        def _do_attach() -> None:
                            _attach_from_paste(ctx, p)

                        run_in_terminal(_do_attach)
                        event.app.invalidate()
                        emit_event(
                            ui_log,
                            "ui.paste_attach",
                            path=str(p),
                            kind=_detect(p),
                        )
                        return
            except (OSError, ValueError):
                pass

        if len(data) > pastes.threshold:
            placeholder = pastes.add(data)
            if placeholder is not None:
                event.current_buffer.insert_text(placeholder)
                emit_event(ui_log, "ui.paste_folded", paste_chars=len(data))
                return
            emit_event(
                ui_log,
                "ui.paste_raw_inserted",
                paste_chars=len(data),
                reason="per_turn_cap",
            )
        event.current_buffer.insert_text(data)

    @bindings.add("c-l")
    def _clear_screen(event):
        """Clear chat via Ctrl+L. Starts a fresh session log."""
        messages.clear()
        console.clear()
        print_splash()
        if ctx.get("session") is not None:
            from pathlib import Path

            ctx["session"] = Session.create(
                provider=ctx["provider"],
                model=provider_configs.get(ctx["provider"], {}).get("model", ""),
                cwd=Path.cwd(),
                model_params=_build_model_params(ctx["provider"], ctx),
            )
            _on_session_created(ctx)
        # Fresh session ⇒ re-arm the context-warning banner.
        _reset_context_warning(ctx)

    cwd = os.getcwd()

    def _consume_update_task() -> None:
        """If the background check has finished, stash its result in ctx."""
        if ctx.get("update_info") is not None or not update_task.done():
            return
        try:
            ctx["update_info"] = update_task.result()
        except Exception:
            ctx["update_info"] = None

    def _bottom_toolbar():
        model = provider_configs.get(ctx["provider"], {}).get("model", "")
        effort = ctx.get("effort", "medium")
        mode = ctx.get("mode")

        left = f" {ctx['provider']}/{model}"
        if mode:
            left += f"  [{mode}]"
        left += f"  effort:{effort}"
        statuses = mcp_client.get_status()
        if statuses:
            connected = sum(1 for s in statuses if s.connected)
            total = sum(1 for s in statuses if s.enabled)
            left += f"  mcp:{connected}/{total}"
        if ctx.get("skip_permissions"):
            left += "  🔓"
        if ctx.get("sandbox") is not None:
            left += "  🐳 sandbox"
        pending_atts = ctx.get("pending_attachments") or []
        if pending_atts:
            left += f"  📎 {len(pending_atts)}"
        if tracing_info:
            left += f"  trace:{tracing_info}"
        # Persistent context-utilisation marker — appears once we cross
        # the configured threshold and stays visible until a compaction
        # frees budget (or the user starts a fresh session). This is the
        # always-on counterpart to the one-shot banner: the banner draws
        # the eye, the marker keeps the state legible.
        threshold = _compaction_threshold(ctx)
        if threshold > 0:
            util, _cur, lim = _context_utilisation(ctx)
            if lim > 0 and util >= threshold:
                left += f"  ctx:{util:.0%}⚠"
        _consume_update_task()
        update_info = ctx.get("update_info")
        if update_info is not None and update_info.available:
            left += "  ⬆ update available — /update"

        right = "/help commands · ctrl+l clear · ctrl+c quit"

        padding = console.width - len(left) - len(right)
        if padding < 2:
            padding = 2
        top_row = left + " " * padding + right

        # Second row: working directory
        home = os.path.expanduser("~")
        display_cwd = cwd.replace(home, "~", 1) if cwd.startswith(home) else cwd
        bottom_row = f" 📁 {display_cwd}"

        return HTML(f'<style fg="#52525b">{top_row}\n{bottom_row}</style>')

    pt_style = PTStyle.from_dict(
        {
            "bottom-toolbar": "noreverse bg:default",
            "completion-menu": "bg:#1a1a2e fg:#d4d4d8",
            "completion-menu.completion": "bg:#1a1a2e fg:#d4d4d8",
            "completion-menu.completion.current": "bg:#a78bfa fg:#09090b",
            "completion-menu.meta.completion": "bg:#1a1a2e fg:#6d6d8a",
            "completion-menu.meta.completion.current": "bg:#a78bfa fg:#09090b",
        }
    )

    session: PromptSession = PromptSession(
        key_bindings=bindings,
        enable_history_search=False,
        bottom_toolbar=_bottom_toolbar,
        completer=SlashCompleter(),
        complete_while_typing=True,
        reserve_space_for_menu=4,
        style=pt_style,
    )
    ctx["pt_session"] = session

    # -- Tracing setup --
    tracing_kwargs = setup_tracing()
    tracing_info = get_tracing_info()
    if tracing_info:
        print_system(f"Tracing enabled: [accent]{tracing_info}[/accent]")
    # Stash a reference to the LangFuse callback handler so /score and
    # /dataset can read its `last_trace_id` after each turn. None when
    # tracing is off.
    _lf_callbacks = (tracing_kwargs.get("config") or {}).get("callbacks") or []
    ctx["langfuse_handler"] = _lf_callbacks[0] if _lf_callbacks else None

    # -- Local user profile (used as LangFuse user_id when tracing is on). --
    ctx["user_id"] = get_or_create_profile_id()

    # -- Startup API key check --
    # Validation was kicked off as `provider_validation_task` earlier so
    # it runs in parallel with splash + prompt_toolkit + tracing setup.
    # On most launches the task is already done by the time we get here.
    # If it isn't (one slow provider), show a spinner so the user knows
    # what they're waiting on instead of staring at a frozen prompt.
    if provider_validation_task.done():
        prevalidated = provider_validation_task.result()
    else:
        with console.status(
            "[thinking]  ◆ validating provider keys…[/thinking]",
            spinner="dots",
        ):
            prevalidated = await provider_validation_task
    chosen_provider, chosen_model = await startup_provider_setup(
        session, prevalidated=prevalidated
    )
    ctx["provider"] = chosen_provider

    # Per-invocation --effort / --temperature override (interactive too).
    # --temperature wins outright; --effort maps via EFFORT_TEMPS. Neither
    # is persisted — preferences.json keeps its existing value.
    cli_temperature = getattr(cli_args, "temperature", None)
    cli_effort = getattr(cli_args, "effort", None)
    if cli_temperature is not None:
        provider_configs[chosen_provider]["temperature"] = cli_temperature
    elif cli_effort is not None:
        provider_configs[chosen_provider]["temperature"] = EFFORT_TEMPS[cli_effort]
    if cli_effort is not None:
        ctx["effort"] = cli_effort

    # -- Per-project session log --
    from pathlib import Path

    ctx["session"] = Session.create(
        provider=chosen_provider,
        model=provider_configs.get(chosen_provider, {}).get("model", ""),
        cwd=Path.cwd(),
        model_params=_build_model_params(chosen_provider, ctx),
    )
    _on_session_created(ctx)
    print_system(f"[dim]session: {ctx['session'].short_id}[/dim]")

    # -- Skills disclosure: surface first-run seeding + any over-cap drops. --
    if seeded:
        print_system(
            f"[dim]seeded {seeded} example skill(s) into "
            f"[accent]{SKILLS_DIR}[/accent] — see /skills[/dim]"
        )
    if skills_index.dropped:
        print_system(
            f"[warning]●[/warning] {len(skills_index.dropped)} enabled skill(s) "
            f"dropped — over cap of {skills_index.max_active}. "
            f"Only the first {skills_index.max_active} alphabetically are "
            f"surfaced to the model. Run [accent]/skills[/accent] to see which."
        )

    # -- Sandbox bootstrap (opt-in via config.sandboxing.enabled) --
    # try_start handles every failure mode (no daemon, unsafe cwd, docker run
    # error) by returning (None, "<reason>"). On any failure we fall back to
    # host execution and the rest of the app behaves unchanged.
    sandbox_cfg = config.get("sandboxing") or {}
    if sandbox_cfg.get("enabled"):
        sbx, status = Sandbox.try_start(sandbox_cfg, Path.cwd())
        if sbx is not None:
            ctx["sandbox"] = sbx
            if status and status != "ok":
                print_system(f"[dim]{status}[/dim]")
            print_system(
                f"[green]🐳[/green] Sandbox active "
                f"[dim](container {sbx.short_id}, image {sbx.image})[/dim]"
            )
            # Re-bind active tools so run_command_host shows up.
            _refresh_active_tools(ctx)
        else:
            print_system(
                f"[warning]🐳 Sandboxing requested but unavailable[/warning] — "
                f"{status}. Falling back to host execution."
            )

    # Surface the update banner once (the toolbar keeps a persistent hint).
    try:
        ctx["update_info"] = await asyncio.wait_for(asyncio.shield(update_task), 2.0)
    except (asyncio.TimeoutError, Exception):
        pass
    update_info = ctx.get("update_info")
    if update_info is not None and update_info.available:
        extra = ""
        if update_info.ahead > 0:
            extra = f" [dim](local is ahead {update_info.ahead} — manual rebase needed)[/dim]"
        elif update_info.dirty:
            extra = " [dim](working tree dirty — commit or stash first)[/dim]"
        print_system(
            f"[accent]⬆ Update available[/accent]: "
            f"{update_info.behind_str} on "
            f"[bold]{update_info.branch}[/bold]. "
            f"Run [accent]/update[/accent] to pull and reinstall.{extra}"
        )

    # -- MCP servers --
    if preferences.get_mcp_servers():
        print_system("[dim]Connecting to MCP servers…[/dim]")
        statuses = await mcp_client.start()
        max_shown = 5
        for s in statuses[:max_shown]:
            if not s.enabled:
                print_system(f"[muted]●[/muted] [bold]{s.name}[/bold] disabled")
            elif s.connected:
                print_system(
                    f"[green]●[/green] [bold]{s.name}[/bold]  "
                    f"[dim]{s.tool_count} tools, {s.resource_count} resources, "
                    f"{s.prompt_count} prompts[/dim]"
                )
            else:
                print_system(
                    f"[red]●[/red] [bold]{s.name}[/bold]  "
                    f"[warning]{s.error or 'failed'}[/warning]"
                )
        if len(statuses) > max_shown:
            print_system(
                f"[dim]+ {len(statuses) - max_shown} more — run [/dim]"
                f"[accent]/mcp[/accent][dim] to see all[/dim]"
            )
    _refresh_active_tools(ctx)

    if cli_args.skip_permissions:
        print_system(
            "[warning]●[/warning] [bold]--skip-permissions[/bold] enabled — "
            "tool approval prompts are disabled"
        )

    prompt_msg = HTML('<style fg="#a78bfa"><b>❯ </b></style>')

    def _shutdown_sandbox() -> None:
        sbx = ctx.get("sandbox")
        if sbx is not None:
            sbx.close()
            ctx["sandbox"] = None

    while True:
        # Keep the artifact store + per-session fetch_artifact tool synced with
        # whatever the current session is — covers /clear, /resume, Ctrl+L.
        cur_sess = ctx.get("session")
        cur_store = ctx.get("artifact_store")
        if cur_sess is not None and (
            cur_store is None or cur_store.session_id != cur_sess.id
        ):
            _on_session_created(ctx)

        # Drain any background eager-stage tasks left over from prior turns
        # (currently only tool_summarize's LLM upgrade). With a 2s budget
        # we let in-flight summaries finish before the next LLM call sees
        # the events, but never block the user longer than that — the
        # truncated fallback is already on disk and is a correct (if less
        # rich) view of the tool output.
        await _drain_pending_summary_tasks(ctx, timeout=2.0)

        # Visual separator between turns.
        console.print()

        if ctx.get("pending_input"):
            user_text = ctx.pop("pending_input")
            console.print("[muted]❯ (from MCP prompt)[/muted]")
        else:
            # Discard any input buffered while the model was working so
            # keystrokes typed during processing aren't auto-submitted.
            if sys.platform != "win32":
                try:
                    import termios

                    termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
                except Exception:
                    pass
            try:
                with patch_stdout():
                    user_text = await session.prompt_async(
                        prompt_msg,
                        placeholder=HTML('<style fg="#52525b">Send a message…</style>'),
                    )
            except (EOFError, KeyboardInterrupt):
                # Best-effort drain so any in-flight summary upgrade lands
                # on disk before we exit. Short budget — Ctrl+C means the
                # user wants out *now*.
                await _drain_pending_summary_tasks(ctx, timeout=1.5)
                await mcp_client.stop()
                _shutdown_sandbox()
                emit_event(app_log, "app.stop", reason="interrupt")
                console.print("\n[muted]Goodbye.[/muted]")
                break
        user_text = user_text.strip()
        if not user_text:
            continue
        console.print()
        # Swap any [Paste #N N chars] tokens back to the original content
        # before it reaches slash-command parsing or the LLM. Reset the
        # registry afterwards so each turn starts from id #1 with the
        # full per-turn capacity available.
        user_text = pastes.expand(user_text)
        pastes.reset()

        # Inline path auto-attach: if the user mentions an existing file
        # path in their message (absolute, ~/, or file://), attach it
        # automatically and replace the token in the prompt with
        # "[attached: name]" so the model knows the file came in as an
        # attachment rather than something it should try to read from disk.
        # Paths inside backticks are left alone — that's the escape hatch.
        if not user_text.startswith("/"):
            user_text = _auto_attach_inline_paths(ctx, user_text)

        # --- Slash commands ---
        if user_text.startswith("/"):
            parts = user_text[1:].split(maxsplit=1)
            cmd_name = parts[0].lower()
            cmd_args = parts[1] if len(parts) > 1 else ""

            cmd_log = get_logger("command")
            cmd_t0 = time.monotonic()
            entry = COMMANDS.get(cmd_name)
            cmd_outcome = "ok"
            try:
                if entry:
                    _, handler = entry
                    result = handler(ctx, cmd_args)
                    if asyncio.iscoroutine(result):
                        result = await result
                else:
                    cmd_outcome = "unknown"
                    result = (
                        f"Unknown command [bold]/{cmd_name}[/bold]. "
                        f"Type [accent]/help[/accent] for a list."
                    )
            except Exception:
                cmd_outcome = "error"
                emit_event(
                    cmd_log,
                    "command.dispatch",
                    level=logging.ERROR,
                    name=cmd_name,
                    args_len=len(cmd_args),
                    outcome="error",
                    duration_ms=(time.monotonic() - cmd_t0) * 1000.0,
                    exc_info=True,
                )
                raise
            emit_event(
                cmd_log,
                "command.dispatch",
                name=cmd_name,
                args_len=len(cmd_args),
                outcome=cmd_outcome,
                duration_ms=(time.monotonic() - cmd_t0) * 1000.0,
            )

            if result is not None:
                print_system(result)

            # Handle /exit
            if ctx.get("exit"):
                # Wait briefly for any in-flight summary upgrades so we
                # don't drop work the user already paid for.
                await _drain_pending_summary_tasks(ctx, timeout=2.0)
                await mcp_client.stop()
                _shutdown_sandbox()
                emit_event(app_log, "app.stop", reason="exit_command")
                break

            # Handle pending API key prompt (from /model selecting a provider with no key)
            if ctx.get("pending_api_key"):
                prov = ctx.pop("pending_api_key")
                pending_model = ctx.pop("pending_model", None)

                api_key = await prompt_api_key(session, prov)
                if api_key:
                    set_api_key(prov, api_key)
                    ctx["provider"] = prov
                    if pending_model:
                        provider_configs[prov]["model"] = pending_model
                        preferences.set_selected_model(prov, pending_model)
                    model = provider_configs[prov].get("model", "unknown")
                    preferences.set_pref("default_provider", prov)
                    print_system(
                        f"[green]●[/green] Switched to [bold]{prov}[/bold] / "
                        f"[accent]{model}[/accent]"
                    )
                    _drop_incompatible_attachments(ctx)
                else:
                    print_system("[dim]Cancelled. Provider unchanged.[/dim]")

            # Handle pending model-switch confirmation (from /model when
            # tool_persistence + enforce_session_model are on and the live
            # session already carries tool history). On confirm, rotate to a
            # fresh session before applying the switch so cross-provider tool
            # message replay never happens.
            if ctx.get("pending_model_switch"):
                pending = ctx.pop("pending_model_switch")
                target_provider = pending["provider"]
                target_model = pending["model"]

                confirm_session = PromptSession()
                from prompt_toolkit.formatted_text import HTML as _HTML

                try:
                    with patch_stdout():
                        answer = await confirm_session.prompt_async(
                            _HTML(
                                '<style fg="#a78bfa"><b>  Rotate session and switch? </b></style>'
                                '<style fg="#71717a">[y/N] ❯ </style>'
                            ),
                        )
                except (EOFError, KeyboardInterrupt):
                    answer = ""

                if (answer or "").strip().lower() not in ("y", "yes"):
                    print_system("[dim]Cancelled. Model unchanged.[/dim]")
                    continue

                # Rotate: clear in-memory messages and start a new session
                # tagged with the new provider/model. Mirrors /clear.
                from pathlib import Path as _Path

                from ui.sessions import Session as _Session

                ctx["messages"].clear()
                if ctx.get("session") is not None:
                    cfg = provider_configs.get(target_provider, {}) or {}
                    model_params = {
                        k: v
                        for k, v in {
                            "temperature": cfg.get("temperature"),
                            "model_default": target_model,
                            "effort": ctx.get("effort"),
                            "mode": ctx.get("mode"),
                        }.items()
                        if v is not None
                    }
                    ctx["session"] = _Session.create(
                        provider=target_provider,
                        model=target_model,
                        cwd=_Path.cwd(),
                        model_params=model_params,
                    )

                from_prov = ctx.get("provider")
                from_model = (
                    provider_configs.get(from_prov, {}).get("model")
                    if from_prov
                    else None
                )
                ctx["provider"] = target_provider
                provider_configs[target_provider]["model"] = target_model
                preferences.set_pref("default_provider", target_provider)
                preferences.set_selected_model(target_provider, target_model)
                emit_event(
                    get_logger("model"),
                    "model.switch",
                    from_provider=from_prov,
                    from_model=from_model,
                    to_provider=target_provider,
                    to_model=target_model,
                    reason="rotate_for_tool_persistence",
                )
                _drop_incompatible_attachments(ctx)
                print_system(
                    f"[green]●[/green] New session. Switched to "
                    f"[bold]{target_provider}[/bold] / [accent]{target_model}[/accent]"
                )

            continue

        # --- Regular message ---
        # Build message list with system prompts (mode + base instructions
        # + skills directory). Only the directory (name + description +
        # when_to_use) goes in — bodies are loaded on demand by run_skill.
        mode = ctx.get("mode")
        state_messages = list(messages)
        prompts: list[str] = []
        if mode and mode in MODE_PROMPTS:
            prompts.append(MODE_PROMPTS[mode])
        prompts.append(BASE_INSTRUCTIONS)
        if (ctx.get("vibe_md_config") or {}).get("enabled", True):
            from ui.vibe_md import (
                read_global_vibe,
                read_project_vibe,
                render_vibe_block,
            )

            vibe_block = render_vibe_block(read_global_vibe(), read_project_vibe())
            if vibe_block:
                prompts.append(vibe_block)
        skills_index_now = ctx.get("skills_index")
        if skills_index_now is not None and skills_index_now.active:
            directory = render_skills_directory(skills_index_now.active)
            if directory:
                prompts.append(directory)
        state_messages.insert(0, SystemMessage(content="\n\n".join(prompts)))

        # Pre-submit capability gate: drop pending attachments whose kind
        # the active model has been observed to reject. If a recorded-bad
        # attachment was still pending (race: user attached, then switched
        # model, then submitted) we refuse to send the turn rather than
        # silently strip — the user attached for a reason.
        pending_atts = list(ctx.get("pending_attachments") or [])
        from src import capabilities as _caps_mod

        gate_provider = ctx.get("provider") or ""
        gate_model = provider_configs.get(gate_provider, {}).get("model") or ""
        gate_caps = _caps_mod.get_capabilities(gate_provider, gate_model)
        recorded_bad = [
            a for a in pending_atts if gate_caps.recorded(a.kind) == "no"
        ]
        if recorded_bad:
            kinds = ", ".join(sorted({a.kind for a in recorded_bad}))
            print_system(
                f"[red]●[/red] Not sending — {gate_model} is known to reject "
                f"{kinds} input. [dim]Use [accent]/attach clear[/accent] to drop, "
                f"or [accent]/model[/accent] to switch.[/dim]"
            )
            continue

        # Drain pending attachments now — they belong to this turn.
        ctx["pending_attachments"] = []
        sess: Session | None = ctx.get("session")
        session_id = sess.id if sess is not None else ""

        from src.multimodal import (
            MultimodalEncodingError,
            build_human_content,
            build_human_event_content,
        )

        try:
            human_content = build_human_content(
                user_text,
                pending_atts,
                gate_provider,
                session_id=session_id,
            )
        except MultimodalEncodingError as exc:
            print_system(
                f"[red]●[/red] Failed to encode attachments: {exc}. "
                "[dim]Turn not sent — adjust attachments and retry.[/dim]"
            )
            # Put them back so the user doesn't have to re-attach.
            ctx["pending_attachments"] = pending_atts
            continue

        event_content = build_human_event_content(user_text, pending_atts)

        messages.append(HumanMessage(content=human_content))
        state_messages.append(HumanMessage(content=human_content))

        # Register the attachments on the session-scoped registry so
        # read_pdf_pages and similar cross-turn tools can resolve them.
        for _att in pending_atts:
            ctx.setdefault("session_attachments", {})[_att.name] = _att

        if pending_atts:
            kinds_summary = ", ".join(
                f"{c} {k}{'s' if c > 1 else ''}"
                for k, c in sorted(
                    {
                        kind: sum(1 for a in pending_atts if a.kind == kind)
                        for kind in {a.kind for a in pending_atts}
                    }.items()
                )
            )
            print_system(
                f"[muted]📎 sent {kinds_summary}"
                + (
                    " (PDF manifest only — model uses read_pdf_pages)"
                    if any(a.kind == "pdf" for a in pending_atts)
                    else ""
                )
                + "[/muted]"
            )

        # Stamp this turn so every event we record below shares a turn_id.
        turn_id = new_turn_id()
        if sess is not None:
            sess.append_event(KIND_HUMAN, event_content, turn_id=turn_id)

        # Bind turn_id on the current task so every nested log record (LLM
        # call, tool call, permission prompt) inherits the correlation IDs.
        # Reset is in the outer finally below.
        _turn_token = current_turn_id.set(turn_id)
        interaction_log = get_logger("interaction")
        emit_event(
            interaction_log,
            "interaction.submit",
            prompt_len=len(user_text),
            paste_count=user_text.count("[Paste #"),
            mode=ctx.get("mode"),
            effort=ctx.get("effort"),
            provider=ctx.get("provider"),
            model=provider_configs.get(ctx["provider"], {}).get("model"),
        )
        _interaction_t0 = time.monotonic()
        _interaction_outcome = "ok"

        state = {"messages": state_messages}
        run_config = {
            "configurable": {
                "provider": ctx["provider"],
                "app_ctx": ctx,
            }
        }

        # Merge tracing callbacks into run config, plus LangFuse-specific
        # metadata so traces group by chat session and identify the user.
        # The `langfuse_*` metadata keys are picked up by the LangFuse
        # callback handler; other handlers ignore them.
        if tracing_kwargs.get("config"):
            for key, val in tracing_kwargs["config"].items():
                if key == "callbacks":
                    run_config.setdefault("callbacks", []).extend(val)
                else:
                    run_config[key] = val

            sess = ctx.get("session")
            lf_metadata: dict = {}
            if sess is not None:
                lf_metadata["langfuse_session_id"] = sess.id
            if ctx.get("user_id"):
                lf_metadata["langfuse_user_id"] = ctx["user_id"]
            tags: list[str] = []
            mode = ctx.get("mode")
            if mode and mode != "default":
                tags.append(f"mode:{mode}")
            provider = ctx.get("provider")
            if provider:
                tags.append(f"provider:{provider}")
                model = provider_configs.get(provider, {}).get("model")
                if model:
                    tags.append(f"model:{model}")
            if tags:
                lf_metadata["langfuse_tags"] = tags
            if lf_metadata:
                run_config["metadata"] = {
                    **run_config.get("metadata", {}),
                    **lf_metadata,
                }

        # Thinking indicator (manual control so permission prompts can pause it)
        status = console.status("[thinking]  ◆ thinking…[/thinking]", spinner="dots")
        status.start()
        ctx["status"] = status
        ctx["status_label"] = "thinking"
        turn_t0 = time.monotonic()
        pending_diffs: dict = ctx.setdefault("pending_diffs", {})
        pending_tool_logs: dict = ctx.setdefault("pending_tool_logs", {})

        def _format_status() -> str:
            label = ctx.get("status_label", "thinking")
            elapsed = time.monotonic() - turn_t0
            return f"[thinking]  ◆ {label}… [/thinking][muted]{elapsed:.1f}s[/muted]"

        async def _tick_status() -> None:
            try:
                while True:
                    await asyncio.sleep(0.4)
                    try:
                        status.update(_format_status())
                    except Exception:
                        # Status may be stopped (e.g. permission prompt) —
                        # ignore and keep ticking.
                        pass
            except asyncio.CancelledError:
                return

        ticker_task = asyncio.create_task(_tick_status())

        def _surface_tool_message(msg):
            """Surface tool errors/denials, diffs, and persistent log lines."""
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            tool_name = getattr(msg, "name", "tool")
            call_id = getattr(msg, "tool_call_id", "") or ""
            is_error = content.startswith("Tool error:") or content.startswith(
                "User denied"
            )

            # Pop any captured side-data for this tool call.
            diff_info = pending_diffs.pop(call_id, None) if call_id else None
            log_line = pending_tool_logs.pop(call_id, None) if call_id else None

            if is_error:
                status.stop()
                print_system(
                    f"[warning]●[/warning] [bold]{tool_name}[/bold] — "
                    f"[warning]{content}[/warning]"
                )
                status.start()
                return

            if log_line is not None:
                status.stop()
                console.print(f"  [muted]↳ {log_line}[/muted]")
                status.start()

            if diff_info is not None:
                panel = render_diff_panel(
                    diff_info["path"],
                    diff_info["before"],
                    diff_info["after"],
                )
                if panel is not None:
                    status.stop()
                    console.print(panel)
                    status.start()

        response_text = ""
        try:
            try:
                import time as _time
                from datetime import datetime as _dt, timezone as _tz

                def _now_iso():
                    return (
                        _dt.now(_tz.utc)
                        .isoformat(timespec="milliseconds")
                        .replace("+00:00", "Z")
                    )

                last_ai_content = None
                final_state_messages: list | None = None
                llm_call_started_at = _now_iso()
                llm_call_started_perf = _time.perf_counter()
                async for event in graph.astream(state, config=run_config):
                    for node_name, node_state in event.items():
                        last_msg = node_state["messages"][-1]
                        # Track the most recent full graph state so that, with
                        # tool_persistence on, we can replay everything the
                        # graph appended into the cross-turn `messages` list.
                        final_state_messages = node_state["messages"]

                        if node_name == "tools":
                            for tm in node_state["messages"]:
                                _surface_tool_message(tm)
                                if sess is not None:
                                    await _record_tool_result(sess, tm, turn_id, ctx)
                            # Reset clock — the next chatbot yield starts now.
                            llm_call_started_at = _now_iso()
                            llm_call_started_perf = _time.perf_counter()
                            continue

                        # chatbot node — capture latest assistant content.
                        duration_ms = (
                            _time.perf_counter() - llm_call_started_perf
                        ) * 1000.0
                        if sess is not None:
                            await _record_assistant(
                                sess,
                                last_msg,
                                turn_id,
                                ctx,
                                request_started_at=llm_call_started_at,
                                duration_ms=duration_ms,
                            )

                        content = last_msg.content
                        if isinstance(content, list):
                            text = "".join(
                                block.get("text", "")
                                if isinstance(block, dict)
                                else str(block)
                                for block in content
                            )
                        else:
                            text = content
                        last_ai_content = text
            finally:
                ticker_task.cancel()
                try:
                    await ticker_task
                except asyncio.CancelledError:
                    pass
                status.stop()
                ctx.pop("status", None)
                ctx.pop("status_label", None)
                # Drop any uncollected side-data so it doesn't leak into the next turn.
                pending_diffs.clear()
                pending_tool_logs.clear()
                # Capture the LangFuse trace id so /score and /dataset can
                # reference the turn we just finished.
                lf_handler = ctx.get("langfuse_handler")
                if lf_handler is not None:
                    last_id = getattr(lf_handler, "last_trace_id", None)
                    if last_id:
                        ctx["last_trace_id"] = last_id

            elapsed = time.monotonic() - turn_t0

            response_text = last_ai_content or ""
            if not response_text.strip():
                response_text = "_(no response — see any tool errors above)_"

            # When tool_persistence is on, carry every message the graph
            # appended (AIMessage(tool_calls=...), ToolMessage, final
            # AIMessage) into the cross-turn `messages` buffer so the next
            # turn's model call can see prior tool history. With it off, only
            # the assistant's final text crosses turn boundaries — historical
            # behavior, provider-safe.
            if _tool_persistence_enabled(ctx) and final_state_messages is not None:
                base_len = len(state_messages)
                appended = list(final_state_messages[base_len:])
                if appended:
                    messages.extend(appended)
                else:
                    # Defensive fallback: if the graph somehow yielded no new
                    # messages, still keep the cross-turn invariant intact.
                    messages.append(AIMessage(content=response_text))
            else:
                messages.append(AIMessage(content=response_text))
            console.print()
            print_message(response_text, role="assistant")
            console.print(format_elapsed(elapsed))

            # End-of-turn threshold pass: only runs when context utilisation
            # crosses the configured threshold AND at least one threshold stage
            # is enabled. All stages are off by default — flip them on in
            # config.yml to A/B against ~/.vibe-cli/session_backups/.
            await _maybe_threshold_pass(ctx, messages)
            # Banner check runs *after* the threshold pass so the message
            # only shows when we're still over the line (i.e. the pass
            # didn't free enough, or compaction is off).
            _maybe_show_context_warning(ctx)
        except Exception as _turn_exc:
            _interaction_outcome = "error"
            get_logger("error").error(
                "error.in_turn",
                extra={
                    "event": "error.in_turn",
                    "data": {
                        "error_type": type(_turn_exc).__name__,
                        "where": "turn_body",
                    },
                },
                exc_info=True,
            )
            # Attachment-rejection path: chatbot_node has already recorded
            # the capability "no" on the provider/model — we just need to
            # surface a friendly message and return to the prompt. The
            # rejected turn is left as-is on the session log; the user can
            # retry with a different model or with `/attach clear`.
            rejected = getattr(_turn_exc, "_vibe_attachment_kind_rejected", None)
            if rejected:
                cur_model = provider_configs.get(ctx["provider"], {}).get("model", "")
                print_system(
                    f"[red]●[/red] {cur_model} rejected {rejected} input. "
                    f"[dim]Recorded — won't be retried against this model. "
                    f"Switch with [accent]/model[/accent] (peers that may work: "
                    f"shown in `/capabilities`), or drop attachments with "
                    f"[accent]/attach clear[/accent] and resend.[/dim]"
                )
                continue
            raise
        finally:
            emit_event(
                interaction_log,
                "interaction.end",
                outcome=_interaction_outcome,
                response_chars=len(response_text),
                duration_ms=(time.monotonic() - _interaction_t0) * 1000.0,
            )
            current_turn_id.reset(_turn_token)


def run():
    """Entry point — handles graceful shutdown."""
    cli_args = _parse_args()

    if cli_args.system_prompt is not None and cli_args.append_system_prompt is not None:
        print(
            "Error: --system and --append-system-prompt are mutually exclusive.",
            file=sys.stderr,
        )
        sys.exit(2)

    # --skip-interactions umbrella: implies --skip-permissions. -p/--prompt
    # mode also auto-enables it (there's no one to interact with anyway).
    if cli_args.prompt is not None:
        cli_args.skip_interactions = True
    if cli_args.skip_interactions:
        cli_args.skip_permissions = True

    # --ollama-url is per-invocation; bake it into provider_configs early
    # so both interactive and headless paths see it before validation.
    if cli_args.ollama_url and "ollama" in provider_configs:
        provider_configs["ollama"]["base_url"] = cli_args.ollama_url.rstrip("/")

    if cli_args.cmd == "web":
        _run_web(cli_args)
        return

    if cli_args.cmd == "eval":
        _run_eval(cli_args)
        return

    if cli_args.prompt is not None:
        _run_headless(cli_args)
        return

    if sys.platform != "win32":
        loop = asyncio.new_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, loop.stop)
        loop.run_until_complete(main(cli_args))
        loop.close()
    else:
        asyncio.run(main(cli_args))


def _run_web(cli_args: argparse.Namespace) -> None:
    try:
        from web.app import create_app
    except ModuleNotFoundError as e:
        if "flask" in str(e).lower():
            print(
                "Flask isn't installed. Reinstall vibe with the web extra:\n"
                '  pip install -e ".[web]"\n'
                "or:\n"
                "  uv sync --extra web",
                file=sys.stderr,
            )
            sys.exit(1)
        raise

    host = cli_args.host
    port = cli_args.port
    url = f"http://{host}:{port}"
    print(f"vibe-cli web panel  →  {url}")

    if not cli_args.no_browser:
        import threading
        import webbrowser

        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    create_app().run(host=host, port=port, debug=False, threaded=True)


def _run_eval(cli_args: argparse.Namespace) -> None:
    try:
        from eval.__main__ import main as eval_main
    except ImportError as e:
        print(
            f"Eval module not available: {e}\n"
            "Install the eval extra:\n"
            "  uv sync --extra eval\n"
            "or:\n"
            '  pip install -e ".[eval]"',
            file=sys.stderr,
        )
        sys.exit(1)
    # eval.__main__.main() reads sys.argv itself, but we've already parsed the
    # top-level args. Re-invoke with only the eval subcommand's argv slice so
    # eval's own argparse sees the right namespace.
    import sys as _sys

    # Find the index of 'eval' in the original argv and slice past it
    argv = _sys.argv[1:]
    try:
        eval_idx = argv.index("eval")
        _sys.argv = [_sys.argv[0]] + argv[eval_idx + 1 :]
    except ValueError:
        _sys.argv = [_sys.argv[0]]
    eval_main()


# ---------------------------------------------------------
# Headless mode (`vibe -p <prompt> -o text|json`)
# ---------------------------------------------------------
def _run_headless(cli_args: argparse.Namespace) -> None:
    """Entry point for non-interactive single-turn execution.

    Produces a deterministic record of the turn (final assistant text + every
    tool call + skill invocations + token usage) for use by external eval
    harnesses. Skips the prompt_toolkit loop, MCP, tracing, and session-file
    writes so the run is hermetic.
    """
    prompt = cli_args.prompt
    if prompt == "-":
        prompt = sys.stdin.read()
    if not prompt or not prompt.strip():
        print("Error: --prompt must be a non-empty string.", file=sys.stderr)
        sys.exit(2)

    max_turns = getattr(cli_args, "max_turns", None)
    if max_turns is not None and max_turns < 1:
        print("Error: --max-turns must be >= 1.", file=sys.stderr)
        sys.exit(2)

    allowed_tools_raw = getattr(cli_args, "allowed_tools", None)
    allowed_tools: list[str] | None = None
    if allowed_tools_raw:
        allowed_tools = [t.strip() for t in allowed_tools_raw.split(",") if t.strip()]
        if not allowed_tools:
            print(
                "Error: --allowedTools must list at least one tool name.",
                file=sys.stderr,
            )
            sys.exit(2)

    try:
        asyncio.run(
            _headless_main(
                prompt=prompt,
                output_format=cli_args.output,
                provider_override=cli_args.provider,
                model_override=cli_args.model,
                allowed_tools=allowed_tools,
                max_turns=max_turns,
                effort=cli_args.effort,
                temperature=cli_args.temperature,
                system_prompt=cli_args.system_prompt,
                append_system_prompt=cli_args.append_system_prompt,
                save_session=cli_args.save_session,
                verbose=cli_args.verbose,
            )
        )
    except KeyboardInterrupt:
        sys.exit(130)


async def _headless_main(
    prompt: str,
    output_format: str,
    provider_override: str | None,
    model_override: str | None,
    allowed_tools: list[str] | None = None,
    max_turns: int | None = None,
    effort: str | None = None,
    temperature: float | None = None,
    system_prompt: str | None = None,
    append_system_prompt: str | None = None,
    save_session: bool = False,
    verbose: bool = False,
) -> None:
    import json as _json
    from langchain_core.messages import ToolMessage as _Tool

    if not PROVIDERS:
        print(
            "Error: No providers configured. Add at least one provider with a "
            "models list to config.yml.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Configure logging so tool calls / LLM events still get logged the same
    # way as interactive mode. Honour the config.yml `logging` block but stay
    # silent on stdout/stderr — headless output goes through our own writer.
    log_cfg = config.get("logging") or {}
    configure_logging(
        enabled=bool(log_cfg.get("enabled", True)),
        level=log_cfg.get("level"),
        log_dir=log_cfg.get("dir"),
        text_max_bytes=int(log_cfg.get("text_max_bytes") or 5 * 1024 * 1024),
        text_backups=int(log_cfg.get("text_backups") or 10),
        jsonl_retention_days=int(log_cfg.get("jsonl_retention_days") or 30),
    )

    # Restore saved API keys + pick a provider. No interactive prompts in
    # headless mode — if nothing validates, exit non-zero.
    preferences.load()
    from src.providers import PROVIDER_REGISTRY
    from ui.provider_handler import (
        ENV_KEY_ALIASES,
        provider_has_key,
        provider_requires_api_key,
        validate_api_key,
    )

    # 1. Restore saved keys from preferences into runtime config + env.
    for prov in PROVIDERS:
        if not provider_requires_api_key(prov):
            continue
        saved_key = preferences.get_api_key(prov)
        if saved_key and not provider_has_key(prov):
            provider_configs[prov]["api_key"] = saved_key
            for env_var in ENV_KEY_ALIASES.get(prov, []):
                os.environ[env_var] = saved_key

    # 2. Env-var fallback: pick up any of the accepted aliases (e.g.
    #    BEDROCK_API_KEY or AWS_BEARER_TOKEN_BEDROCK) for providers
    #    that don't yet have a key. Lets `vibe -p '...'` run with no
    #    prior setup as long as one of the standard env vars is set.
    for prov in PROVIDERS:
        if not provider_requires_api_key(prov):
            continue
        if provider_has_key(prov):
            continue
        provider_impl = PROVIDER_REGISTRY.get(prov)
        if provider_impl is None:
            continue
        env_key = provider_impl.resolve_env_key()
        if env_key:
            provider_configs[prov]["api_key"] = env_key
            for env_var in ENV_KEY_ALIASES.get(prov, []):
                os.environ[env_var] = env_key

    # 3. Provider selection.
    #    - explicit --provider wins
    #    - else: saved default if usable
    #    - else: first provider in config.yml order whose key/server resolves
    if provider_override:
        if provider_override not in PROVIDERS:
            print(
                f"Error: provider '{provider_override}' is not configured.",
                file=sys.stderr,
            )
            sys.exit(1)
        provider = provider_override
    else:
        default = preferences.get_default_provider(provider_configs)
        provider = None
        if default and default in PROVIDERS:
            if provider_requires_api_key(default):
                if provider_has_key(default):
                    provider = default
            else:
                provider = default
        if provider is None:
            for prov in PROVIDERS:
                if provider_requires_api_key(prov):
                    if provider_has_key(prov):
                        provider = prov
                        break
                else:
                    # Local provider (ollama) — only pick if reachable.
                    if validate_api_key(prov):
                        provider = prov
                        break
        if provider is None:
            env_var_hint = ", ".join(
                sorted({v for alias in ENV_KEY_ALIASES.values() for v in alias})
            )
            print(
                "Error: no usable provider found. Set one of: "
                f"{env_var_hint} or OLLAMA_URL (with a running ollama server), "
                "or pass --provider.",
                file=sys.stderr,
            )
            sys.exit(1)

    if provider_requires_api_key(provider) and not provider_has_key(provider):
        print(
            f"Error: provider '{provider}' has no API key. Set its env var "
            f"(one of: {', '.join(ENV_KEY_ALIASES.get(provider, []))}) "
            f"or run `vibe` interactively once to save it.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not validate_api_key(provider):
        print(
            f"Error: API key for '{provider}' failed validation.",
            file=sys.stderr,
        )
        sys.exit(1)

    if model_override:
        provider_configs[provider]["model"] = model_override
    else:
        provider_configs[provider]["model"] = preferences.get_selected_model(
            provider, provider_configs
        )
    model_name = provider_configs[provider]["model"]

    # Apply per-invocation temperature/effort overrides before model
    # construction. --temperature wins outright; --effort maps via
    # EFFORT_TEMPS. Both leave config.yml untouched.
    if temperature is not None:
        provider_configs[provider]["temperature"] = temperature
    elif effort is not None:
        provider_configs[provider]["temperature"] = EFFORT_TEMPS[effort]

    # Load skills so `run_skill` works in headless mode too — eval suites need
    # to verify skill activations.
    skills_cfg = config.get("skills") or {}
    skills_max_active = int(skills_cfg.get("max_active") or SKILLS_DEFAULT_MAX)
    from pathlib import Path as _Path_headless

    skills_index = load_skills(
        max_active=skills_max_active,
        cwd=_Path_headless.cwd(),
    )

    ctx: dict = {
        "provider": provider,
        "skills_index": skills_index,
        "skip_permissions": True,
        "session": None,
        "artifact_store": None,
        "compaction_config": config.get("compaction") or {},
        "effort": effort or preferences.get("effort", "medium"),
        "vibe_md_config": config.get("vibe_md") or {},
        "multimodal_config": config.get("multimodal") or {},
        "pending_attachments": [],
        "session_attachments": {},
    }

    # Optional session persistence for the headless turn. Off by default so
    # `vibe -p '...'` stays hermetic; flip on with --save-session when an
    # operator wants the run to land in ~/.vibe-cli/sessions/ alongside
    # interactive sessions.
    if save_session:
        from pathlib import Path as _Path

        ctx["session"] = Session.create(
            provider=provider,
            model=model_name,
            cwd=_Path.cwd(),
            model_params={
                "provider": provider,
                "model": model_name,
                "temperature": provider_configs[provider].get("temperature"),
                "effort": ctx.get("effort"),
            },
        )

    # Built-in tools only — no MCP in headless mode, keeps eval hermetic.
    # --allowedTools filters the parent agent's toolset by name. Subagent
    # types still have their own internal toolsets; the filter here only
    # constrains what the parent sees and what it can spawn into.
    active = list(ALL_TOOLS)
    if allowed_tools is not None:
        known = {getattr(t, "name", None): t for t in ALL_TOOLS}
        unknown = [n for n in allowed_tools if n not in known]
        if unknown:
            print(
                "Error: --allowedTools contains unknown tool(s): "
                f"{', '.join(unknown)}. Known tools: "
                f"{', '.join(sorted(k for k in known if k))}.",
                file=sys.stderr,
            )
            sys.exit(2)
        active = [known[n] for n in allowed_tools]
    set_active_tools(active)

    # Build the system-prompt stack.
    #   --system        → replace entirely (operator takes full control)
    #   --append-system → append after the default stack
    #   otherwise       → BASE + HEADLESS + skills directory
    if system_prompt is not None:
        system_text = system_prompt
    else:
        prompts: list[str] = [BASE_INSTRUCTIONS, HEADLESS_INSTRUCTIONS]
        if (config.get("vibe_md") or {}).get("enabled", True):
            from ui.vibe_md import (
                read_global_vibe,
                read_project_vibe,
                render_vibe_block,
            )

            vibe_block = render_vibe_block(read_global_vibe(), read_project_vibe())
            if vibe_block:
                prompts.append(vibe_block)
        if skills_index.active:
            directory = render_skills_directory(skills_index.active)
            if directory:
                prompts.append(directory)
        if append_system_prompt:
            prompts.append(append_system_prompt)
        system_text = "\n\n".join(prompts)

    state_messages = [
        SystemMessage(content=system_text),
        HumanMessage(content=prompt),
    ]

    state = {"messages": state_messages}
    run_config = {"configurable": {"provider": provider, "app_ctx": ctx}}

    tool_calls_record: list[dict] = []
    pending_calls: dict[str, dict] = {}
    final_text: str = ""
    total_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read": 0,
        "cache_write": 0,
    }
    turns = 0

    t0 = time.monotonic()
    error: dict | None = None
    max_turns_reached = False

    def _vlog(msg: str) -> None:
        if verbose:
            print(msg, file=sys.stderr, flush=True)

    _vlog(f"[vibe] provider={provider} model={model_name}")

    try:
        async for event in graph.astream(state, config=run_config):
            for node_name, node_state in event.items():
                msgs = node_state["messages"]
                last = msgs[-1]
                if node_name == "chatbot":
                    turns += 1
                    um = _extract_usage(last)
                    # `_extract_usage` returns {"input", "output", "cache_read",
                    # "cache_write"}; the headless JSON exposes the longer
                    # {"input_tokens", "output_tokens", ...} names that the
                    # eval runner / web pricing table both consume. Translate
                    # here so per-key sums don't silently zero out.
                    total_usage["input_tokens"] += int(um.get("input") or 0)
                    total_usage["output_tokens"] += int(um.get("output") or 0)
                    total_usage["cache_read"] += int(um.get("cache_read") or 0)
                    total_usage["cache_write"] += int(um.get("cache_write") or 0)
                    for tc in getattr(last, "tool_calls", None) or []:
                        pending_calls[tc["id"]] = {
                            "id": tc["id"],
                            "name": tc["name"],
                            "args": tc.get("args", {}),
                        }
                        _vlog(
                            f"[vibe] tool_call {tc['name']} args={tc.get('args', {})}"
                        )
                    content = last.content
                    if isinstance(content, list):
                        text = "".join(
                            b.get("text", "") if isinstance(b, dict) else str(b)
                            for b in content
                        )
                    else:
                        text = content
                    if text:
                        final_text = text
                        _vlog(
                            f"[vibe] assistant: {text[:200]}{'…' if len(text) > 200 else ''}"
                        )
                elif node_name == "tools":
                    for tm in msgs:
                        if not isinstance(tm, _Tool):
                            continue
                        call_id = getattr(tm, "tool_call_id", "") or ""
                        result = (
                            tm.content
                            if isinstance(tm.content, str)
                            else str(tm.content)
                        )
                        ok = not (
                            result.startswith("Tool error:")
                            or result.startswith("User denied")
                        )
                        rec = pending_calls.pop(
                            call_id,
                            {
                                "id": call_id,
                                "name": getattr(tm, "name", "tool"),
                                "args": {},
                            },
                        )
                        rec["result"] = result
                        rec["ok"] = ok
                        tool_calls_record.append(rec)
                        _vlog(
                            f"[vibe] tool_result {rec['name']} ok={ok} "
                            f"len={len(result)}"
                        )
            if max_turns is not None and turns >= max_turns:
                max_turns_reached = True
                _vlog(f"[vibe] max_turns reached ({max_turns})")
                break
    except Exception as e:
        error = {"type": type(e).__name__, "message": str(e)}
        _vlog(f"[vibe] error {error['type']}: {error['message']}")

    duration_ms = (time.monotonic() - t0) * 1000.0

    skills_used = sorted(
        {
            (tc["args"] or {}).get("skill_name", "")
            for tc in tool_calls_record
            if tc["name"] == "run_skill" and tc.get("ok")
        }
        - {""}
    )

    if output_format == "json":
        payload = {
            "provider": provider,
            "model": model_name,
            "prompt": prompt,
            "response": final_text,
            "tool_calls": tool_calls_record,
            "skills_used": skills_used,
            "usage": total_usage,
            "turns": turns,
            "duration_ms": round(duration_ms, 2),
        }
        if max_turns is not None:
            payload["max_turns"] = max_turns
            payload["max_turns_reached"] = max_turns_reached
        if ctx.get("session") is not None:
            payload["session_id"] = ctx["session"].id
        if error is not None:
            payload["error"] = error
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
        sys.exit(1 if error else 0)

    if error is not None:
        print(f"Error: {error['type']}: {error['message']}", file=sys.stderr)
        sys.exit(1)
    print(final_text)


if __name__ == "__main__":
    run()
