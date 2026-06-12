"""
vibe-cli — Slash commands.
"""

from __future__ import annotations

import asyncio

from collections.abc import Callable

from src.logging_setup import emit_event, get_logger
from src.main import config, provider_configs, providers as PROVIDERS
from src.providers import PROVIDER_REGISTRY
from ui import preferences
from ui.provider_handler import (
    provider_has_key,
    provider_requires_api_key,
    provider_base_url,
    validate_api_key,
    set_api_key,
)


def _unreachable_lines(provider_name: str) -> str:
    """Return the standard 'could not reach this keyless provider' message
    for the given provider, derived from `BaseProvider.unreachable_help`.

    Used by `/model` when a non-API-key provider fails validation. Keeps
    the copy provider-agnostic — Ollama returns its own hint, future
    keyless providers can override `unreachable_help` similarly.
    """
    impl = PROVIDER_REGISTRY.get(provider_name)
    if impl is None:
        headline = f"Could not reach {provider_name}."
        hint = "Try again or choose another provider."
    else:
        headline, hint = impl.unreachable_help(provider_configs.get(provider_name, {}))
    return f"[red]●[/red] {headline}\n[dim]{hint}[/dim]"


def _keyless_providers() -> list[str]:
    """Names of providers in the registry that don't use an API key.
    Used in the `/key` footer so the help text matches the actual
    registry rather than naming Ollama specifically."""
    return [name for name in PROVIDERS if not provider_requires_api_key(name)]


_model_log = get_logger("model")
_ui_log = get_logger("ui")


# ---------------------------------------------------------
# Command registry
# ---------------------------------------------------------
COMMANDS: dict[str, tuple[str, Callable]] = {}


def command(name: str, description: str):
    """Register a slash command."""

    def decorator(func):
        COMMANDS[name] = (description, func)
        return func

    return decorator


# ---------------------------------------------------------
# /help
# ---------------------------------------------------------
@command("help", "Show available commands and keybindings")
def _cmd_help(_ctx, _args):
    lines = ["[bold]Commands[/bold]"]
    for n, (d, _) in sorted(COMMANDS.items()):
        lines.append(f"  [accent]/{n}[/accent] — {d}")
    lines.append("")
    lines.append("[bold]Keybindings[/bold]")
    lines.append("  [accent]Ctrl+C[/accent] — Quit")
    lines.append("  [accent]Ctrl+L[/accent] — Clear chat")
    return "\n".join(lines)


# ---------------------------------------------------------
# /model — select provider/model or set API key
# ---------------------------------------------------------
def _drop_pending_incompatible(ctx, provider: str, model: str) -> None:
    """Drop pending attachments incompatible with the (provider, model)."""
    from src import capabilities as _caps

    pending = ctx.get("pending_attachments") or []
    if not pending:
        return
    caps = _caps.get_capabilities(provider, model)
    kept = [a for a in pending if caps.supports(a.kind)]
    dropped = [a for a in pending if not caps.supports(a.kind)]
    if dropped:
        ctx["pending_attachments"] = kept


def _conversation_has_tool_history(ctx) -> bool:
    """True when the live message buffer contains any tool_use / tool_result
    artifact. Only meaningful with `tool_persistence` on; with it off, the
    buffer never carries tool messages across turns.
    """
    from langchain_core.messages import AIMessage as _AI, ToolMessage as _TM

    for m in ctx.get("messages", []) or []:
        if isinstance(m, _TM):
            return True
        if isinstance(m, _AI) and getattr(m, "tool_calls", None):
            return True
    return False


def _should_gate_model_switch(ctx, new_provider: str, new_model: str) -> bool:
    """True when a model/provider change should be gated behind a confirm-
    and-rotate prompt because persisted tool messages would otherwise replay
    through a different provider's serialization rules.

    Gating fires only when *all* of these hold:
      - `compaction.tool_persistence` is on
      - `compaction.enforce_session_model` is on (default true)
      - the current conversation actually carries tool history
      - either the provider or the model is changing
    """
    cfg = ctx.get("compaction_config") or {}
    if not cfg.get("tool_persistence"):
        return False
    enforce = cfg.get("enforce_session_model")
    if enforce is False:
        return False
    cur_provider = ctx.get("provider")
    cur_model = (
        provider_configs.get(cur_provider, {}).get("model") if cur_provider else None
    )
    if new_provider == cur_provider and new_model == cur_model:
        return False
    return _conversation_has_tool_history(ctx)


@command("model", "Select model provider, switch model, or set API key")
async def _cmd_model(ctx, args):
    current_provider = ctx["provider"]
    current_model = provider_configs.get(current_provider, {}).get("model", "unknown")

    # --- No args: show full menu ---
    if not args:
        # Each provider's validate() makes a remote metadata call
        # (models.list / list_foundation_models / GET /api/tags). Running
        # them serially blocks the prompt for seconds; gather them on
        # threads so the menu builds in the time of the slowest provider.
        async def _check(prov: str) -> tuple[bool, bool, bool]:
            requires_key = provider_requires_api_key(prov)
            has_key = provider_has_key(prov)
            if has_key or not requires_key:
                key_valid = await asyncio.to_thread(validate_api_key, prov)
            else:
                key_valid = False
            return requires_key, has_key, key_valid

        from ui.ui import console

        with console.status(
            "[thinking]  ◆ checking providers…[/thinking]", spinner="dots"
        ):
            results = await asyncio.gather(*(_check(p) for p in PROVIDERS))
        statuses = dict(zip(PROVIDERS, results))

        lines = [
            f"[bold]Model Selection[/bold]   "
            f"[dim]active: {current_provider}/{current_model}[/dim]",
            "",
        ]

        for prov in PROVIDERS:
            cfg = provider_configs.get(prov, {})
            requires_key, has_key, key_valid = statuses[prov]
            models = cfg.get("models", [cfg.get("model", "unknown")])

            # Provider header
            if not requires_key and key_valid:
                lines.append(f"  [bold]{prov}[/bold]  [dim](local)[/dim]")
            elif not requires_key:
                lines.append(
                    f"  [bold]{prov}[/bold]  [yellow](local server offline)[/yellow]"
                )
            elif has_key and key_valid:
                lines.append(f"  [bold]{prov}[/bold]")
            elif has_key:
                lines.append(f"  [bold]{prov}[/bold]  [yellow](invalid key)[/yellow]")
            else:
                lines.append(f"  [bold]{prov}[/bold]  [error](no API key)[/error]")

            # Model list under each provider
            for m in models:
                is_selected = prov == current_provider and m == current_model
                if is_selected:
                    dot = "[green]●[/green]"
                elif key_valid:
                    dot = "[yellow]●[/yellow]"
                else:
                    dot = "[red]●[/red]"

                label = f"  {dot} {m}"
                if is_selected:
                    label += "  [dim](selected)[/dim]"
                lines.append(label)

            lines.append("")

        lines.append(
            "[dim]Usage: /model <provider> <model>           — switch model[/dim]"
        )
        lines.append(
            "[dim]       /model <provider>                   — switch provider (keep default model)[/dim]"
        )
        lines.append(
            "[dim]       /model <provider> key <api_key>     — set API key[/dim]"
        )
        return "\n".join(lines)

    parts = args.strip().split()
    provider_name = parts[0].lower()

    # --- /model <provider> key <value> ---
    if len(parts) >= 3 and parts[1].lower() == "key":
        api_key = parts[2]
        if provider_name not in PROVIDERS:
            return (
                f"Unknown provider [bold]{provider_name}[/bold]. "
                f"Available: {', '.join(PROVIDERS)}"
            )
        if not provider_requires_api_key(provider_name):
            return (
                f"[bold]{provider_name}[/bold] does not use an API key. "
                f"Start the local server at [accent]{provider_base_url(provider_name)}[/accent] "
                f"and switch with [accent]/model {provider_name}[/accent]."
            )
        set_api_key(provider_name, api_key)
        return f"[green]●[/green] API key set for [bold]{provider_name}[/bold]"

    # --- Validate provider ---
    if provider_name not in PROVIDERS:
        return (
            f"Unknown provider [bold]{provider_name}[/bold]. "
            f"Available: {', '.join(PROVIDERS)}"
        )

    # --- /model <provider> <model> ---
    if len(parts) >= 2:
        model_name = parts[1]
        available_models = provider_configs.get(provider_name, {}).get("models", [])
        if model_name not in available_models:
            return (
                f"Unknown model [bold]{model_name}[/bold] for {provider_name}.\n"
                f"Available: {', '.join(available_models)}"
            )

        if provider_requires_api_key(provider_name):
            if not provider_has_key(provider_name):
                # Signal the main loop to prompt for API key
                ctx["pending_api_key"] = provider_name
                ctx["pending_model"] = model_name
                return (
                    f"[red]●[/red] No API key for [bold]{provider_name}[/bold].\n"
                    f"Please enter your API key below:"
                )
        elif not await asyncio.to_thread(validate_api_key, provider_name):
            return _unreachable_lines(provider_name)

        if _should_gate_model_switch(ctx, provider_name, model_name):
            ctx["pending_model_switch"] = {
                "provider": provider_name,
                "model": model_name,
            }
            return (
                f"[warning]This session has persisted tool history.[/warning]\n"
                f"Switching to [bold]{provider_name}[/bold] / [accent]{model_name}[/accent] "
                f"will start a fresh session — the current one will be saved.\n"
                f"[dim]Reason: tool message formats vary across providers, and "
                f"replaying them through a different model is not safe with "
                f"`compaction.tool_persistence` on.[/dim]\n"
                f"[dim]Confirm at the prompt below ([bold]y[/bold]/N).[/dim]"
            )

        from_prov = ctx.get("provider")
        from_model = (
            provider_configs.get(from_prov, {}).get("model") if from_prov else None
        )
        ctx["provider"] = provider_name
        provider_configs[provider_name]["model"] = model_name
        preferences.set_pref("default_provider", provider_name)
        preferences.set_selected_model(provider_name, model_name)
        emit_event(
            _model_log,
            "model.switch",
            from_provider=from_prov,
            from_model=from_model,
            to_provider=provider_name,
            to_model=model_name,
        )
        # Drop pending attachments incompatible with the new model.
        _drop_pending_incompatible(ctx, provider_name, model_name)
        return (
            f"[green]●[/green] Switched to [bold]{provider_name}[/bold] / "
            f"[accent]{model_name}[/accent]"
        )

    # --- /model <provider> (no model specified) ---
    if provider_requires_api_key(provider_name) and not provider_has_key(provider_name):
        ctx["pending_api_key"] = provider_name
        return (
            f"[red]●[/red] No API key for [bold]{provider_name}[/bold].\n"
            f"Please enter your API key below:"
        )
    if not provider_requires_api_key(provider_name) and not await asyncio.to_thread(
        validate_api_key, provider_name
    ):
        return _unreachable_lines(provider_name)

    target_model = provider_configs.get(provider_name, {}).get("model", "unknown")
    if _should_gate_model_switch(ctx, provider_name, target_model):
        ctx["pending_model_switch"] = {
            "provider": provider_name,
            "model": target_model,
        }
        return (
            f"[warning]This session has persisted tool history.[/warning]\n"
            f"Switching to [bold]{provider_name}[/bold] / [accent]{target_model}[/accent] "
            f"will start a fresh session — the current one will be saved.\n"
            f"[dim]Reason: tool message formats vary across providers, and "
            f"replaying them through a different model is not safe with "
            f"`compaction.tool_persistence` on.[/dim]\n"
            f"[dim]Confirm at the prompt below ([bold]y[/bold]/N).[/dim]"
        )

    from_prov = ctx.get("provider")
    from_model = provider_configs.get(from_prov, {}).get("model") if from_prov else None
    ctx["provider"] = provider_name
    model = provider_configs.get(provider_name, {}).get("model", "unknown")
    preferences.set_pref("default_provider", provider_name)
    emit_event(
        _model_log,
        "model.switch",
        from_provider=from_prov,
        from_model=from_model,
        to_provider=provider_name,
        to_model=model,
    )
    return (
        f"[green]●[/green] Switched to [bold]{provider_name}[/bold] / "
        f"[accent]{model}[/accent]"
    )


# ---------------------------------------------------------
# /key — set or update an API key for a provider
# ---------------------------------------------------------
@command("key", "Set or update an API key for a provider")
def _cmd_key(ctx, args):
    if not args:
        lines = ["[bold]API Keys[/bold]", ""]
        for prov in PROVIDERS:
            if not provider_requires_api_key(prov):
                if validate_api_key(prov):
                    lines.append(
                        f"  [green]●[/green] [bold]{prov}[/bold]  "
                        f"[dim]local server at {provider_base_url(prov)}[/dim]"
                    )
                else:
                    lines.append(
                        f"  [yellow]●[/yellow] [bold]{prov}[/bold]  "
                        f"[warning]local server offline at {provider_base_url(prov)}[/warning]"
                    )
                continue

            has_key = provider_has_key(prov)
            if has_key:
                raw = provider_configs.get(prov, {}).get("api_key", "")
                masked = f"***{raw[-4:]}" if len(raw) > 4 else "***"
                valid = validate_api_key(prov)
                if valid:
                    lines.append(f"  [green]●[/green] [bold]{prov}[/bold]  {masked}")
                else:
                    lines.append(
                        f"  [yellow]●[/yellow] [bold]{prov}[/bold]  {masked}  [warning](invalid key)[/warning]"
                    )
            else:
                lines.append(f"  [red]●[/red] [bold]{prov}[/bold]  [dim]not set[/dim]")
        lines.append("")
        lines.append(
            "[dim]Usage: /key <provider>              — prompt for API key[/dim]"
        )
        lines.append(
            "[dim]       /key <provider> <api_key>    — set API key directly[/dim]"
        )
        keyless = _keyless_providers()
        if keyless:
            label = ", ".join(keyless)
            lines.append(
                f"[dim]Local providers ({label}) use their own server and do not need a key.[/dim]"
            )
        return "\n".join(lines)

    parts = args.strip().split()
    provider_name = parts[0].lower()

    if provider_name not in PROVIDERS:
        return (
            f"Unknown provider [bold]{provider_name}[/bold]. "
            f"Available: {', '.join(PROVIDERS)}"
        )

    if not provider_requires_api_key(provider_name):
        return (
            f"[bold]{provider_name}[/bold] does not use an API key. "
            f"Start the local server at [accent]{provider_base_url(provider_name)}[/accent]."
        )

    # /key <provider> <value> — set directly
    if len(parts) >= 2:
        api_key = parts[1]
        set_api_key(provider_name, api_key)
        if validate_api_key(provider_name):
            return f"[green]●[/green] API key saved for [bold]{provider_name}[/bold]"
        return (
            f"[yellow]●[/yellow] API key saved for [bold]{provider_name}[/bold] "
            f"but validation failed — check that the key is correct."
        )

    # /key <provider> — signal the main loop to prompt for the key
    ctx["pending_api_key"] = provider_name
    return f"Enter your API key for [bold]{provider_name}[/bold]:"


# ---------------------------------------------------------
# /preferences — show preferences file path
# ---------------------------------------------------------
@command("preferences", "Show the preferences file path")
def _cmd_preferences(_ctx, _args):
    from ui.preferences import PREFS_PATH

    return f"[bold]Preferences file[/bold]\n  [accent]{PREFS_PATH}[/accent]"


# ---------------------------------------------------------
# /config — show config file path
# ---------------------------------------------------------
@command("config", "Show the config file path")
def _cmd_config(_ctx, _args):
    from src.main import _config_path

    return f"[bold]Config file[/bold]\n  [accent]{_config_path}[/accent]"


# ---------------------------------------------------------
# /effort — select reasoning effort level
# ---------------------------------------------------------
EFFORT_LEVELS = {
    "low": "Fast responses, minimal reasoning",
    "medium": "Balanced speed and depth",
    "high": "Deep reasoning, thorough answers",
}


@command("effort", "Set the model reasoning effort level")
def _cmd_effort(ctx, args):
    current = ctx.get("effort", "medium")

    if not args:
        lines = [f"Current effort: [bold]{current}[/bold]", ""]
        for level, desc in EFFORT_LEVELS.items():
            marker = "[accent]>[/accent] " if level == current else "  "
            lines.append(f"  {marker}[bold]{level}[/bold] — {desc}")
        lines.append("")
        lines.append("[dim]Usage: /effort <low|medium|high>[/dim]")
        return "\n".join(lines)

    level = args.strip().lower()
    if level not in EFFORT_LEVELS:
        return (
            f"Unknown effort level [bold]{level}[/bold]. "
            f"Choose from: {', '.join(EFFORT_LEVELS)}"
        )
    previous = ctx.get("effort")
    ctx["effort"] = level
    preferences.set_pref("effort", level)
    emit_event(_ui_log, "ui.effort_change", **{"from": previous, "to": level})
    return f"Effort set to [bold]{level}[/bold] — {EFFORT_LEVELS[level]}"


# ---------------------------------------------------------
# /permissions — view and edit tool permissions
# ---------------------------------------------------------
@command("permissions", "View or edit tool approval lists")
def _cmd_permissions(_ctx, args):
    if not args:
        allow = preferences.get_always_allow()
        deny = preferences.get_always_deny()
        lines = ["[bold]Tool permissions[/bold]", ""]
        lines.append("[bold]Always allow[/bold]")
        if allow:
            for n in allow:
                lines.append(f"  [success]●[/success] {n}")
        else:
            lines.append("  [muted](none)[/muted]")
        lines.append("")
        lines.append("[bold]Always deny[/bold]")
        if deny:
            for n in deny:
                lines.append(f"  [red]●[/red] {n}")
        else:
            lines.append("  [muted](none)[/muted]")
        lines.append("")
        lines.append("[dim]Usage: /permissions allow <tool>   — always allow[/dim]")
        lines.append("[dim]       /permissions deny <tool>    — always deny[/dim]")
        lines.append(
            "[dim]       /permissions reset <tool>   — remove from both lists[/dim]"
        )
        return "\n".join(lines)

    parts = args.strip().split()
    if len(parts) < 2:
        return "[dim]Usage: /permissions allow|deny|reset <tool>[/dim]"

    action, tool_name = parts[0].lower(), parts[1]
    if action == "allow":
        preferences.add_always_allow(tool_name)
        return f"[success]●[/success] [bold]{tool_name}[/bold] will always be allowed"
    if action == "deny":
        preferences.add_always_deny(tool_name)
        return f"[red]●[/red] [bold]{tool_name}[/bold] will always be denied"
    if action == "reset":
        preferences.remove_permission(tool_name)
        return f"[bold]{tool_name}[/bold] removed from permission lists"
    return f"Unknown action [bold]{action}[/bold]. Use allow|deny|reset."


# ---------------------------------------------------------
# /mcp — MCP server configuration
# ---------------------------------------------------------
@command("mcp", "Configure MCP (Model Context Protocol) servers")
async def _cmd_mcp(ctx, args):
    from src import mcp_client
    from src.main import set_active_tools
    from src.tools import ALL_TOOLS

    def _refresh_active_tools():
        set_active_tools(list(ALL_TOOLS) + mcp_client.get_tools())

    servers = preferences.get_mcp_servers()

    if not args:
        statuses = {s.name: s for s in mcp_client.get_status()}
        lines = ["[bold]MCP Servers[/bold]"]
        if not servers:
            lines.append("  [dim]No servers configured[/dim]")
        for name, cfg in servers.items():
            transport = cfg.get("transport", "stdio")
            target = cfg.get("url") or " ".join(
                [cfg.get("command", "")] + cfg.get("args", [])
            )
            s = statuses.get(name)
            if not cfg.get("enabled", True):
                badge = "[muted]disabled[/muted]"
            elif s and s.connected:
                badge = (
                    f"[success]connected[/success]  "
                    f"[dim]{s.tool_count} tools, "
                    f"{s.resource_count} resources, "
                    f"{s.prompt_count} prompts[/dim]"
                )
            elif s and s.error:
                badge = f"[red]failed[/red] [dim]{s.error}[/dim]"
            else:
                badge = "[muted]not connected[/muted]"
            lines.append(
                f"  [bold]{name}[/bold] [dim]({transport})[/dim] — "
                f"[accent]{target}[/accent]  {badge}"
            )
        lines.append("")
        lines.append("[dim]Usage:[/dim]")
        lines.append(
            "[dim]  /mcp add stdio <name> <command> [args...]    — add stdio server[/dim]"
        )
        lines.append(
            "[dim]  /mcp add http  <name> <url>                  — add streamable_http server[/dim]"
        )
        lines.append(
            "[dim]  /mcp add sse   <name> <url>                  — add SSE server[/dim]"
        )
        lines.append(
            "[dim]  /mcp remove <name>                           — remove[/dim]"
        )
        lines.append(
            "[dim]  /mcp toggle <name>                           — enable/disable[/dim]"
        )
        lines.append(
            "[dim]  /mcp reload [name]                           — reconnect[/dim]"
        )
        lines.append(
            "[dim]  /mcp tools  [name]                           — list tools[/dim]"
        )
        return "\n".join(lines)

    parts = args.strip().split()
    action = parts[0].lower()

    if action == "add" and len(parts) >= 4:
        kind, name = parts[1].lower(), parts[2]
        if kind == "stdio":
            cfg = {
                "transport": "stdio",
                "command": parts[3],
                "args": parts[4:],
                "enabled": True,
            }
        elif kind in ("http", "streamable_http"):
            cfg = {"transport": "streamable_http", "url": parts[3], "enabled": True}
        elif kind == "sse":
            cfg = {"transport": "sse", "url": parts[3], "enabled": True}
        else:
            return f"Unknown transport [bold]{kind}[/bold]. Use stdio|http|sse."
        preferences.set_mcp_server(name, cfg)
        await mcp_client.start()
        _refresh_active_tools()
        return f"Added MCP server [bold]{name}[/bold] and reconnected."

    if action == "remove" and len(parts) >= 2:
        name = parts[1]
        if name not in servers:
            return f"Server [bold]{name}[/bold] not found"
        preferences.remove_mcp_server(name)
        await mcp_client.start()
        _refresh_active_tools()
        return f"Removed MCP server [bold]{name}[/bold]"

    if action == "toggle" and len(parts) >= 2:
        name = parts[1]
        new_state = preferences.toggle_mcp_server(name)
        if new_state is None:
            return f"Server [bold]{name}[/bold] not found"
        await mcp_client.start()
        _refresh_active_tools()
        state = "enabled" if new_state else "disabled"
        return f"Server [bold]{name}[/bold] is now [accent]{state}[/accent]"

    if action == "reload":
        target = parts[1] if len(parts) >= 2 else None
        await mcp_client.reload(target)
        _refresh_active_tools()
        return "Reloaded."

    if action == "tools":
        tools = mcp_client.get_tools()
        target = parts[1] if len(parts) >= 2 else None
        lines = ["[bold]MCP tools[/bold]"]
        for t in tools:
            if target and not t.name.startswith(f"{target}__"):
                continue
            desc = (t.description or "").split("\n")[0][:80]
            lines.append(f"  [accent]{t.name}[/accent]  [dim]{desc}[/dim]")
        if len(lines) == 1:
            lines.append("  [muted](none)[/muted]")
        return "\n".join(lines)

    return "[dim]Usage: /mcp add|remove|toggle|reload|tools …[/dim]"


# ---------------------------------------------------------
# /resources — list and read MCP resources
# ---------------------------------------------------------
@command("resources", "List or read MCP resources")
async def _cmd_resources(_ctx, args):
    from src import mcp_client

    resources = mcp_client.get_resources()

    if not args:
        if not resources:
            return "[muted](no MCP resources)[/muted]"
        lines = ["[bold]MCP resources[/bold]"]
        for r in resources:
            uri = getattr(r, "metadata", {}).get("uri") or getattr(r, "path", "")
            srv = getattr(r, "_server", "")
            mime = getattr(r, "mimetype", "") or getattr(r, "metadata", {}).get(
                "mime_type", ""
            )
            lines.append(f"  [accent]{uri}[/accent]  [dim]{srv}  {mime}[/dim]")
        lines.append("")
        lines.append("[dim]Usage: /resources <uri>   — fetch a resource[/dim]")
        return "\n".join(lines)

    uri = args.strip()
    text = await mcp_client.mcp_read_resource.ainvoke({"uri": uri})
    return f"[bold]{uri}[/bold]\n\n{text}"


# ---------------------------------------------------------
# /prompts — list and use MCP prompts
# ---------------------------------------------------------
@command("prompts", "List or use MCP prompts (seeds next message)")
async def _cmd_prompts(ctx, args):
    from src import mcp_client

    by_server = mcp_client.get_prompts_by_server()

    if not args:
        if not any(by_server.values()):
            return "[muted](no MCP prompts)[/muted]"
        lines = ["[bold]MCP prompts[/bold]"]
        for srv, names in by_server.items():
            if not names:
                continue
            lines.append(f"  [bold]{srv}[/bold]")
            for n in names:
                lines.append(f"    [accent]{n}[/accent]")
        lines.append("")
        lines.append("[dim]Usage: /prompts <server> <prompt> [k=v ...][/dim]")
        return "\n".join(lines)

    parts = args.strip().split()
    if len(parts) < 2:
        return "[dim]Usage: /prompts <server> <prompt> [k=v ...][/dim]"
    server, prompt = parts[0], parts[1]
    arg_kvs: dict = {}
    for p in parts[2:]:
        if "=" in p:
            k, v = p.split("=", 1)
            arg_kvs[k] = v

    text = await mcp_client.fetch_prompt(server, prompt, arg_kvs or None)
    if not text:
        return f"[red]●[/red] Prompt [bold]{server}/{prompt}[/bold] returned nothing"
    ctx["pending_input"] = text
    return (
        f"[success]●[/success] Loaded prompt [bold]{server}/{prompt}[/bold] — "
        f"will send on next turn"
    )


# ---------------------------------------------------------
# /skills — manage skills (file-backed, progressively disclosed)
# ---------------------------------------------------------
def _scope_tag(skill) -> str:
    """Format the scope of a skill for display (global / project)."""
    scope = getattr(skill, "scope", "global")
    if scope == "project":
        return " [dim](project)[/dim]"
    return " [dim](global)[/dim]"


def _format_skills_index(index) -> str:
    """Render the active / dropped / disabled sections."""
    lines = []
    n_enabled = len(index.active) + len(index.dropped)
    if index.dropped:
        lines.append(
            f"[bold]Skills[/bold]   "
            f"[dim]{n_enabled} enabled, {len(index.dropped)} dropped — "
            f"cap is {index.max_active}[/dim]"
        )
    else:
        lines.append(
            f"[bold]Skills[/bold]   "
            f"[dim]{n_enabled} enabled / {len(index.disabled)} disabled "
            f"(cap {index.max_active})[/dim]"
        )
    lines.append("")

    if index.active:
        for s in index.active:
            lines.append(
                f"  [success]on [/success] [bold]{s.name}[/bold]{_scope_tag(s)} — "
                f"{s.description or '[dim](no description)[/dim]'}"
            )
    else:
        lines.append("  [muted](none enabled)[/muted]")

    if index.disabled:
        lines.append("")
        lines.append("[bold]Disabled[/bold]")
        for s in index.disabled:
            lines.append(
                f"  [muted]off[/muted] [bold]{s.name}[/bold]{_scope_tag(s)} — "
                f"{s.description or '[dim](no description)[/dim]'}"
            )

    if index.dropped:
        lines.append("")
        lines.append(
            f"[bold]Dropped[/bold]   "
            f"[dim]over cap of {index.max_active}, not visible to the model — "
            f"first {index.max_active} alphabetically are kept[/dim]"
        )
        for s in index.dropped:
            lines.append(
                f"  [warning]drp[/warning] [bold]{s.name}[/bold]{_scope_tag(s)} — "
                f"{s.description or '[dim](no description)[/dim]'}"
            )
        lines.append(
            "[dim]Raise [/dim][accent]skills.max_active[/accent][dim] in config.yml "
            "or disable a skill above to surface a dropped one.[/dim]"
        )

    lines.append("")
    lines.append("[dim]Usage: /skills <name>           — toggle a skill[/dim]")
    lines.append("[dim]       /skills enable <name>    — enable[/dim]")
    lines.append("[dim]       /skills disable <name>   — disable[/dim]")
    lines.append("[dim]       /skills reload           — re-read from disk[/dim]")
    lines.append("[dim]       /skills show <name>      — print the body[/dim]")
    return "\n".join(lines)


def _reload_skills(ctx) -> None:
    """Rebuild ctx['skills_index'] from disk using the configured cap.

    Includes per-project skills under `<cwd>/.vibe/skills` (project entries
    override global on filename collision).
    """
    from pathlib import Path as _Path

    from ui.skills import load_skills

    cfg = config.get("skills") or {}
    max_active = cfg.get("max_active")
    if max_active is None:
        from ui.skills import DEFAULT_MAX_ACTIVE

        max_active = DEFAULT_MAX_ACTIVE
    ctx["skills_index"] = load_skills(
        max_active=int(max_active),
        cwd=_Path.cwd(),
    )


@command("skills", "List, toggle, or reload skills")
def _cmd_skills(ctx, args):
    from ui.skills import set_enabled

    index = ctx.get("skills_index")
    if index is None:
        _reload_skills(ctx)
        index = ctx["skills_index"]

    if not args:
        return _format_skills_index(index)

    parts = args.strip().split(maxsplit=1)
    sub = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub == "reload":
        _reload_skills(ctx)
        new_index = ctx["skills_index"]
        head = (
            f"[green]●[/green] Reloaded {len(new_index.all)} skill(s) "
            f"from [accent]~/.vibe-cli/skills/[/accent] + "
            f"[accent].vibe/skills/[/accent]"
        )
        return head + "\n\n" + _format_skills_index(new_index)

    if sub == "show":
        if not rest:
            return "[dim]Usage: /skills show <name>[/dim]"
        skill = index.get(rest)
        if skill is None:
            return f"No skill named [bold]{rest}[/bold]."
        body = skill.body or "[dim](empty body)[/dim]"
        return (
            f"[bold]{skill.name}[/bold]   [dim]{skill.path}[/dim]\n"
            f"[dim]{skill.description}[/dim]\n\n{body}"
        )

    if sub in ("enable", "disable"):
        if not rest:
            return f"[dim]Usage: /skills {sub} <name>[/dim]"
        target = sub == "enable"
        from pathlib import Path as _Path

        updated = set_enabled(rest, target, cwd=_Path.cwd())
        if updated is None:
            return (
                f"No skill file at [accent]~/.vibe-cli/skills/{rest}.md[/accent] "
                f"or [accent].vibe/skills/{rest}.md[/accent]. "
                "Create one or run [accent]/skills reload[/accent]."
            )
        _reload_skills(ctx)
        if target:
            return f"Skill [bold]{rest}[/bold] [success]enabled[/success]"
        return f"Skill [bold]{rest}[/bold] [muted]disabled[/muted]"

    # Otherwise treat the first arg as a skill name to toggle.
    name = sub
    skill = index.get(name)
    if skill is None:
        return (
            f"No skill named [bold]{name}[/bold]. "
            f"Run [accent]/skills[/accent] to see what's loaded."
        )
    new_state = not skill.enabled
    from pathlib import Path as _Path

    set_enabled(name, new_state, cwd=_Path.cwd())
    _reload_skills(ctx)
    if new_state:
        return f"Skill [bold]{name}[/bold] [success]enabled[/success]"
    return f"Skill [bold]{name}[/bold] [muted]disabled[/muted]"


# ---------------------------------------------------------
# /plan — toggle planning mode
# ---------------------------------------------------------
@command("plan", "Toggle planning mode")
def _cmd_plan(ctx, _args):
    current = ctx.get("mode")

    if current == "plan":
        ctx["mode"] = None
        emit_event(_ui_log, "ui.mode_toggle", mode="plan", enabled=False)
        return (
            "Planning mode [muted]disabled[/muted]\n"
            "[dim]The model will respond directly.[/dim]"
        )

    ctx["mode"] = "plan"
    emit_event(_ui_log, "ui.mode_toggle", mode="plan", enabled=True)
    return (
        "Planning mode [success]enabled[/success]\n"
        "[dim]The model will create a step-by-step plan before acting.[/dim]"
    )


# ---------------------------------------------------------
# /agent — toggle agentic mode
# ---------------------------------------------------------
@command("agent", "Toggle agentic mode")
def _cmd_agent(ctx, _args):
    current = ctx.get("mode")

    if current == "agent":
        ctx["mode"] = None
        emit_event(_ui_log, "ui.mode_toggle", mode="agent", enabled=False)
        return (
            "Agentic mode [muted]disabled[/muted]\n"
            "[dim]The model will respond as a simple chat assistant.[/dim]"
        )

    ctx["mode"] = "agent"
    emit_event(_ui_log, "ui.mode_toggle", mode="agent", enabled=True)
    return (
        "Agentic mode [success]enabled[/success]\n"
        "[dim]The model can use tools and take autonomous actions.[/dim]"
    )


# ---------------------------------------------------------
# /clear — clear conversation
# ---------------------------------------------------------
@command("clear", "Clear the current conversation")
def _cmd_clear(ctx, _args):
    from pathlib import Path

    from ui.sessions import Session
    from ui.ui import console, print_splash

    ctx["messages"].clear()
    console.clear()
    print_splash()
    if ctx.get("session") is not None:
        provider = ctx["provider"]
        cfg = provider_configs.get(provider, {}) or {}
        model_params = {
            k: v
            for k, v in {
                "temperature": cfg.get("temperature"),
                "model_default": cfg.get("model"),
                "effort": ctx.get("effort"),
                "mode": ctx.get("mode"),
            }.items()
            if v is not None
        }
        ctx["session"] = Session.create(
            provider=provider,
            model=cfg.get("model", ""),
            cwd=Path.cwd(),
            model_params=model_params,
        )
    return None


# ---------------------------------------------------------
# /resume + /sessions — per-project session management
# ---------------------------------------------------------
def _format_session_line(idx: int, s) -> str:
    ts = s.updated_at or s.created_at
    date_str = ts.replace("T", " ")[:16] if ts else ""
    preview = s.first_human_preview(60) or "[dim]empty[/dim]"
    metrics = s.metrics
    peak = metrics["context"].get("peak_size", 0)
    shares = metrics["tokens"].get("shares", {})
    tools_pct = int(shares.get("tools", 0) * 100)
    thinking_pct = int(shares.get("thinking", 0) * 100)
    head = (
        f"  [accent]{idx}[/accent]. [bold]{s.short_id}[/bold]  "
        f"{date_str}  [dim]{s.provider}/{s.model}[/dim]"
    )
    body = (
        f"     {preview} "
        f"[dim]· {len(s.events)} events · peak {peak} tok"
        f" · tools {tools_pct}% · thinking {thinking_pct}%[/dim]"
    )
    return head + "\n" + body


def _replay_session(ctx, sess) -> None:
    from langchain_core.messages import AIMessage as _AI, HumanMessage as _HM

    from ui.ui import console, print_message, print_splash

    from src import capabilities as _caps

    cfg = ctx.get("compaction_config") or {}
    tool_persistence = bool(cfg.get("tool_persistence"))
    provider = sess.provider if sess.provider and sess.provider in config else ctx.get("provider")
    model = provider_configs.get(provider or "", {}).get("model") if provider else None
    cap_filter = _caps.get_capabilities(provider or "", model or "").supports if provider else None
    msgs = sess.to_messages(
        tool_persistence=tool_persistence,
        provider=provider,
        capability_filter=cap_filter,
    )
    ctx["messages"][:] = msgs
    ctx["session"] = sess

    # Rehydrate session_attachments so cross-turn tools (read_pdf_pages)
    # can resolve PDFs / images attached earlier in the session.
    registry: dict = ctx.setdefault("session_attachments", {})
    try:
        from src.attachments import Attachment as _Att
        from ui.sessions import KIND_HUMAN as _KH

        for evt in sess.events:
            if evt.get("kind") != _KH:
                continue
            content = evt.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "attachment":
                    try:
                        att = _Att.from_block(block)
                        if att.name:
                            registry[att.name] = att
                    except Exception:
                        continue
    except Exception:
        pass
    if sess.provider and sess.provider in config:
        ctx["provider"] = sess.provider

    console.clear()
    print_splash()
    for m in msgs:
        # In persistent mode the projection includes AIMessage(tool_calls=...)
        # and ToolMessage entries. For the on-screen replay we only render the
        # human prompts and the assistant's final text per turn, matching what
        # the user saw live. Tool details still surface from the session file
        # via /compaction et al.
        if isinstance(m, _HM):
            role = "user"
        elif isinstance(m, _AI) and isinstance(m.content, str) and m.content.strip():
            role = "assistant"
        else:
            continue
        text = m.content if isinstance(m.content, str) else str(m.content)
        print_message(text, role=role)


@command("resume", "Resume a session (no args = latest, <id> = specific)")
def _cmd_resume(ctx, args):
    from ui.sessions import find_session, latest_session

    arg = (args or "").strip()
    if not arg:
        sess = latest_session()
        if sess is None:
            return (
                "[dim]No sessions found in this project. "
                "Try [/dim][accent]/sessions[/accent][dim] once you've chatted here.[/dim]"
            )
        _replay_session(ctx, sess)
        return (
            f"Resumed [bold]{sess.short_id}[/bold] "
            f"[dim]({len(sess.events)} events, {len(ctx['messages'])} messages)[/dim]"
        )

    match = find_session(arg)
    if match is None:
        return (
            f"No session in this project starts with [bold]{arg}[/bold]. "
            f"Use [accent]/sessions[/accent] to list."
        )
    if isinstance(match, list):
        lines = [
            f"[bold]{arg}[/bold] is ambiguous — matches {len(match)} sessions:",
            "",
        ]
        for i, s in enumerate(match[:10], 1):
            lines.append(_format_session_line(i, s))
        return "\n".join(lines)

    _replay_session(ctx, match)
    return (
        f"Resumed [bold]{match.short_id}[/bold] "
        f"[dim]({len(match.events)} events, {len(ctx['messages'])} messages)[/dim]"
    )


@command("sessions", "List sessions saved for the current project")
def _cmd_sessions(_ctx, _args):
    from pathlib import Path

    from ui.sessions import list_sessions

    sessions = list_sessions()
    if not sessions:
        return (
            f"[dim]No sessions yet for[/dim] [accent]{Path.cwd()}[/accent]\n"
            "[dim]Start chatting and one will be created automatically.[/dim]"
        )
    lines = [
        f"[bold]Sessions in[/bold] [accent]{Path.cwd()}[/accent]   "
        f"[dim]({len(sessions)} total)[/dim]",
        "",
    ]
    for i, s in enumerate(sessions, 1):
        lines.append(_format_session_line(i, s))
    lines.append("")
    lines.append("[dim]Usage: /resume                — resume the most recent[/dim]")
    lines.append("[dim]       /resume <id-prefix>    — resume a specific session[/dim]")
    return "\n".join(lines)


# ---------------------------------------------------------
# /compaction model — pick the LLM used by LLM-backed compaction
# ---------------------------------------------------------
async def _handle_compaction_model(ctx, sub_args: str) -> str:
    """Implement the `/compaction model [...]` sub-actions.

    The compaction model is what `bulk` (summarize/hybrid) and
    `tool_summarize` use for their LLM calls. By default it tracks
    whatever chat model the user is using. Setting an override lets
    users route compaction to a cheaper or longer-context model.

    Sub-actions:
      (no further args)            interactive picker
      "none"                       clear the override (use chat model)
      "<provider> <model>"         set non-interactively
    """
    from ui import preferences as _prefs
    from ui.provider_handler import (
        pick_compaction_model,
        validate_api_key,
    )
    from src.main import provider_configs as _provider_configs, providers as _providers

    sub_args = (sub_args or "").strip()

    # Branch 1 — explicit clear.
    if sub_args.lower() == "none":
        _prefs.set_compaction_model(None, None)
        return (
            "[green]●[/green] Compaction model cleared. "
            "[dim]Falling back to the active chat model.[/dim]"
        )

    # Branch 2 — non-interactive set: "<provider> <model>".
    if sub_args:
        parts = sub_args.split(maxsplit=1)
        if len(parts) != 2:
            return (
                "[warning]Usage:[/warning] /compaction model "
                "[dim]| /compaction model none | /compaction model "
                "<provider> <model>[/dim]"
            )
        prov, model = parts[0], parts[1].strip()
        if prov not in _providers:
            return (
                f"[error]Unknown provider [bold]{prov}[/bold].[/error] "
                f"[dim]Available: {', '.join(_providers)}[/dim]"
            )
        models = _provider_configs.get(prov, {}).get("models") or []
        if model not in models:
            return (
                f"[error]Model [bold]{model}[/bold] not in {prov}'s catalogue.[/error] "
                f"[dim]Available: {', '.join(models)}[/dim]"
            )
        if not validate_api_key(prov):
            return (
                f"[error]No valid API key for [bold]{prov}[/bold].[/error] "
                f"[dim]Run /model to log in first.[/dim]"
            )
        _prefs.set_compaction_model(prov, model)
        return (
            f"[green]●[/green] Compaction model set to [bold]{prov}[/bold] / "
            f"[accent]{model}[/accent]."
        )

    # Branch 3 — interactive picker.
    pt_session = ctx.get("pt_session")
    if pt_session is None:
        # Defensive: should always be set in app.py before commands run.
        return "[error]Interactive picker unavailable.[/error]"

    # Cache validation results across pickers within a single session
    # so we don't re-issue network calls on every reopening.
    cache = ctx.setdefault("_compaction_model_validation_cache", {})
    result = await pick_compaction_model(pt_session, validation_cache=cache)
    if result is None:
        return "[dim]Cancelled. Compaction model unchanged.[/dim]"
    prov, model = result
    _prefs.set_compaction_model(prov, model)
    if prov is None:
        return (
            "[green]●[/green] Compaction model cleared. "
            "[dim]Falling back to the active chat model.[/dim]"
        )
    return (
        f"[green]●[/green] Compaction model set to [bold]{prov}[/bold] / "
        f"[accent]{model}[/accent]."
    )


# ---------------------------------------------------------
# /compaction — show pipeline state
# ---------------------------------------------------------
@command(
    "compaction",
    "Show compaction pipeline state. /compaction model picks the LLM used "
    "for LLM-backed compaction (bulk + tool_summarize).",
)
async def _cmd_compaction(ctx, args):
    """Sub-actions:
    /compaction              show pipeline + context utilisation (default)
    /compaction model        interactive picker for the override LLM
    /compaction model none   clear the override (use chat model)
    /compaction model <provider> <model>   set non-interactively
    """
    args_str = (args or "").strip()
    if args_str.startswith("model"):
        return await _handle_compaction_model(ctx, args_str[len("model") :].strip())

    eager = ctx.get("compaction_pipeline") or []
    threshold = ctx.get("compaction_threshold_pipeline") or []
    cfg = ctx.get("compaction_config") or {}
    sess = ctx.get("session")
    report = ctx.get("last_compaction_report")

    lines = ["[bold]Compaction[/bold]"]
    trig = cfg.get("trigger") or {}
    lines.append(
        f"  trigger: threshold={trig.get('threshold', 0):.0%} "
        f"min_turns_kept={trig.get('min_turns_kept', 0)}"
    )

    # Surface the current compaction-model override so users can see at a
    # glance whether `/compaction model` has been used.
    from ui import preferences as _prefs

    override = _prefs.get_compaction_model()
    if override:
        lines.append(
            f"  compaction model: [accent]{override['provider']}[/accent] / "
            f"[accent]{override['model']}[/accent] "
            f"[dim](override — change with /compaction model)[/dim]"
        )
    else:
        lines.append(
            "  compaction model: [dim]use chat model (default — set with "
            "/compaction model)[/dim]"
        )

    lines.append("  [bold]Eager stages[/bold] (run on every event):")
    if not eager:
        lines.append("    [dim]none enabled[/dim]")
    for s in eager:
        lines.append(f"    [accent]●[/accent] {s.name}")

    lines.append("  [bold]Threshold stages[/bold] (run when over threshold):")
    if not threshold:
        lines.append("    [dim]none enabled[/dim]")
    for s in threshold:
        extra = f" strategy={getattr(s, 'strategy', '')}" if s.name == "bulk" else ""
        lines.append(f"    [accent]●[/accent] {s.name}{extra}")

    if sess is not None:
        m = sess.metrics["context"]
        cur, peak, lim = (
            m.get("current_size", 0),
            m.get("peak_size", 0),
            m.get("limit", 0),
        )
        util = (cur / lim) if lim else 0.0
        lines.append(f"  context: {cur} / {lim} tok ({util:.0%}) — peak {peak}")

        # Per-eager-stage rollup: how often each stage has fired this session
        # and total original bytes it has processed.
        eager_rollup = sess.metrics.get("compaction_eager") or {}
        if eager_rollup:
            lines.append("  [bold]Eager activity[/bold]:")
            for stage_name, info in sorted(eager_rollup.items()):
                lines.append(
                    f"    · {stage_name}: fired {info['count']}× "
                    f"(over {info['original_bytes_total']} original bytes)"
                )

        # Threshold pass history (one entry per pass).
        comps = sess.metrics.get("compactions") or []
        if comps:
            last = comps[-1]
            lines.append(
                f"  threshold passes: {len(comps)} "
                f"(last freed {last['freed']} tok, "
                f"removed {last['removed_events']} events)"
            )

    if report and report.ran:
        lines.append("  [bold]Last pass[/bold]:")
        lines.append(
            f"    freed −{report.freed} tok "
            f"({report.before_size} → {report.after_size}) "
            f"across {len(report.stages)} stage(s)"
        )
        for s in report.stages:
            err = f" ERROR: {s['error']}" if "error" in s else ""
            lines.append(
                f"    · {s['name']}: removed "
                f"{len(s.get('removed_event_ids') or [])} events{err}"
            )
        if report.backup_path:
            lines.append(f"    backup: [dim]{report.backup_path}[/dim]")

    return "\n".join(lines)


# ---------------------------------------------------------
# /compact — manually force a threshold pass
# ---------------------------------------------------------
@command("compact", "Force a compaction threshold pass now")
async def _cmd_compact(ctx, _args):
    from src.compaction import run_threshold_pass

    sess = ctx.get("session")
    pipeline = ctx.get("compaction_threshold_pipeline") or []
    store = ctx.get("artifact_store")
    if sess is None:
        return "[warning]No active session.[/warning]"
    if not pipeline:
        return (
            "[warning]No threshold stages enabled.[/warning] "
            "[dim]Enable one in config.yml under compaction.pipeline.[/dim]"
        )

    # Build the LLM only if any enabled stage might need it. We piggy-back
    # on `ui/app.py:_resolve_compaction_llm` so the preferences override
    # (set via `/compaction model`) is honoured here too — keeping
    # /compact and the auto-fired threshold pass in lockstep.
    llm = None
    if any(getattr(s, "strategy", "trim") in ("summarize", "hybrid") for s in pipeline):
        try:
            from ui.app import _resolve_compaction_llm

            llm = _resolve_compaction_llm(ctx)
        except Exception:
            llm = None

    report = await run_threshold_pass(
        sess,
        pipeline=pipeline,
        store=store,
        compaction_config=ctx.get("compaction_config"),
        llm=llm,
        force=True,
    )
    ctx["last_compaction_report"] = report
    if not report.ran:
        return "[dim]No stages produced changes.[/dim]"

    # Refresh in-memory messages from the (now compacted) session.
    from src import capabilities as _caps

    cfg = ctx.get("compaction_config") or {}
    _prov = ctx.get("provider") or ""
    _model = provider_configs.get(_prov, {}).get("model") or ""
    new_msgs = sess.to_messages(
        tool_persistence=bool(cfg.get("tool_persistence")),
        provider=_prov,
        capability_filter=_caps.get_capabilities(_prov, _model).supports,
    )
    ctx["messages"][:] = new_msgs

    return (
        f"[green]●[/green] Compacted: −{report.freed} tok "
        f"({report.before_size} → {report.after_size}) "
        f"across {len(report.stages)} stage(s)."
    )


# ---------------------------------------------------------
# /update — pull latest from git and reinstall
# ---------------------------------------------------------
@command("update", "Pull the latest version from git and reinstall")
async def _cmd_update(ctx, _args):
    from ui.ui import console
    from ui.updater import apply_update, check_for_update, launch_command

    info = ctx.get("update_info")
    if info is None:
        info = await check_for_update()
        ctx["update_info"] = info

    if info is None:
        return (
            "[warning]●[/warning] Couldn't reach the remote — "
            "check your network or git config and try again."
        )
    if not info.available:
        return f"[dim]Already up to date on [bold]{info.branch}[/bold].[/dim]"
    if info.ahead > 0:
        return (
            f"[warning]●[/warning] Local branch is ahead by {info.ahead} commit(s). "
            "Push or rebase manually before updating."
        )
    if info.dirty:
        return (
            "[warning]●[/warning] Working tree has uncommitted changes. "
            "Commit or stash them, then run [accent]/update[/accent] again."
        )

    console.print(
        f"[dim]Pulling {info.behind_str} on [bold]{info.branch}[/bold]…[/dim]"
    )
    result = await apply_update()
    if not result.ok:
        msg = f"[warning]●[/warning] {result.message}"
        if result.details:
            msg += f"\n[dim]{result.details}[/dim]"
        return msg

    ctx["exit"] = True
    return (
        f"[green]●[/green] {result.message}\n"
        f"[dim]Run [/dim][accent]{launch_command()}[/accent][dim] to relaunch.[/dim]"
    )


# ---------------------------------------------------------
# ---------------------------------------------------------
# /score — score the most recent turn's LangFuse trace
# ---------------------------------------------------------
# Forms:
#   /score good                              — manual=1
#   /score bad <comment...>                  — manual=0, comment="<comment>"
#   /score thumbs-up | thumbs-down           — aliases for good/bad
#   /score <name> <numeric value> [comment]  — custom score dimension
_SCORE_KEYWORDS = {
    "good": ("manual", 1.0),
    "bad": ("manual", 0.0),
    "thumbs-up": ("manual", 1.0),
    "thumbs-down": ("manual", 0.0),
    "up": ("manual", 1.0),
    "down": ("manual", 0.0),
}


def _parse_score_args(args: str) -> tuple[str, float, str | None] | str:
    """Parse `/score` args into (name, value, comment) or return an error string."""
    args = args.strip()
    if not args:
        return (
            "Usage: /score good | bad [comment] | <name> <value> [comment]\n"
            "  /score good\n"
            "  /score bad missed permission prompt\n"
            "  /score relevance 0.7 model picked wrong tool"
        )

    parts = args.split(maxsplit=1)
    first = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    # Keyword form: good / bad / thumbs-up / thumbs-down
    if first in _SCORE_KEYWORDS:
        name, value = _SCORE_KEYWORDS[first]
        return name, value, (rest or None)

    # Custom form: <name> <value> [comment...]
    if not rest:
        return f"[warning]Need a numeric value for score '{first}'[/warning]"
    value_parts = rest.split(maxsplit=1)
    try:
        value = float(value_parts[0])
    except ValueError:
        return f"[warning]'{value_parts[0]}' is not a number[/warning]"
    comment = value_parts[1].strip() if len(value_parts) > 1 else None
    return first, value, comment


@command("score", "Score the most recent turn (good/bad/<name> <value>)")
def _cmd_score(ctx, args):
    from src.tracing import post_score

    parsed = _parse_score_args(args)
    if isinstance(parsed, str):
        return parsed

    name, value, comment = parsed
    trace_id = ctx.get("last_trace_id", "")
    ok, msg = post_score(trace_id, name, value, comment)
    return f"[dim]↑ {msg}[/dim]" if ok else f"[warning]{msg}[/warning]"


# ---------------------------------------------------------
# /dataset — capture turns into LangFuse datasets for replay
# ---------------------------------------------------------
# Forms:
#   /dataset add <dataset-name> [note...]   — add latest turn to the named dataset
#   /dataset list                            — list datasets in the LangFuse project
def _latest_turn_io(ctx) -> tuple[str | None, str | None]:
    """Return (latest_human_text, latest_assistant_text) from the live session."""
    sess = ctx.get("session")
    if sess is None:
        return None, None
    human, assistant = None, None
    for evt in reversed(sess.events):
        kind = evt.get("kind")
        if assistant is None and kind == "assistant_text":
            assistant = evt.get("content")
        elif human is None and kind == "human":
            human = evt.get("content")
        if human is not None and assistant is not None:
            break
    return human, assistant


@command("dataset", "Add the latest turn to a LangFuse dataset, or list datasets")
def _cmd_dataset(ctx, args):
    from src.tracing import add_dataset_item, list_datasets

    args = args.strip()
    if not args:
        return "Usage:\n  /dataset add <name> [note...]\n  /dataset list"

    sub, _, rest = args.partition(" ")
    sub = sub.lower()
    rest = rest.strip()

    if sub == "list":
        ok, payload = list_datasets()
        if not ok:
            return f"[warning]{payload}[/warning]"
        if not payload:
            return "[dim]No datasets in this LangFuse project yet.[/dim]"
        lines = ["[bold]Datasets[/bold]"]
        for ds in payload:
            count = ds["item_count"]
            count_str = f" ({count} items)" if count is not None else ""
            desc = f" — {ds['description']}" if ds["description"] else ""
            lines.append(f"  [accent]{ds['name']}[/accent]{count_str}{desc}")
        return "\n".join(lines)

    if sub != "add":
        return f"[warning]Unknown subcommand '{sub}' — try 'add' or 'list'[/warning]"

    if not rest:
        return "[warning]Usage: /dataset add <name> [note...][/warning]"

    name_parts = rest.split(maxsplit=1)
    dataset_name = name_parts[0]
    note = name_parts[1].strip() if len(name_parts) > 1 else ""

    human, assistant = _latest_turn_io(ctx)
    if not human:
        return "[warning]No turn to capture yet — send a message first.[/warning]"

    sess = ctx.get("session")
    provider = ctx.get("provider", "")
    metadata = {
        "trace_id": ctx.get("last_trace_id", ""),
        "session_id": sess.id if sess else "",
        "user_id": ctx.get("user_id", ""),
        "provider": provider,
        "model": (
            provider_configs.get(provider, {}).get("model", "") if provider else ""
        ),
        "mode": ctx.get("mode") or "default",
    }
    if note:
        metadata["note"] = note

    input_payload = {"messages": [{"role": "user", "content": human}]}
    expected_output = {"role": "assistant", "content": assistant} if assistant else None

    ok, msg = add_dataset_item(dataset_name, input_payload, expected_output, metadata)
    return f"[dim]↑ {msg}[/dim]" if ok else f"[warning]{msg}[/warning]"


# ---------------------------------------------------------
# /subagents — list and configure subagents
# ---------------------------------------------------------
def _subagent_active_model(
    subagent_type: str, type_cfg: dict, ctx
) -> tuple[str, str, str]:
    """Resolve the (provider, model, source) the subagent will actually use.

    Source is one of: "preferences", "chat-model". `type_cfg` is accepted for
    API symmetry but ignored — model selection is preferences-only by design
    (see docs/SUBAGENTS.md). Non-model per-type defaults still live in config.
    """
    del type_cfg  # noqa: F841 — kept for caller-side symmetry
    override = preferences.get_subagent_model(subagent_type)
    if override:
        return override["provider"], override["model"], "preferences"

    prov = ctx.get("provider") or ""
    model = provider_configs.get(prov, {}).get("model", "") if prov else ""
    return prov, model, "chat-model"


def _format_subagents_index(ctx) -> str:
    from src.subagents import SUBAGENT_REGISTRY

    cfg_block = config.get("subagents") or {}
    types_cfg = cfg_block.get("types") or {}

    lines = ["[bold]Subagents[/bold]"]
    if not cfg_block.get("enabled", True):
        lines.append(
            "  [warning]disabled in config.yml[/warning] "
            "[dim](set subagents.enabled: true to use)[/dim]"
        )
    lines.append(
        f"  [dim]default timeout {cfg_block.get('default_timeout_s', 120)}s · "
        f"max timeout {cfg_block.get('max_timeout_s', 600)}s · "
        f"max depth {cfg_block.get('max_depth', 2)}[/dim]"
    )
    lines.append("")

    if not SUBAGENT_REGISTRY:
        lines.append("  [muted](no subagent types registered)[/muted]")
        return "\n".join(lines)

    for name in sorted(SUBAGENT_REGISTRY):
        cls = SUBAGENT_REGISTRY[name]
        type_cfg = types_cfg.get(name) or {}
        prov, model, source = _subagent_active_model(name, type_cfg, ctx)
        model_str = (
            f"[bold]{prov}[/bold] / [accent]{model}[/accent]"
            if prov and model
            else "[dim](unresolved)[/dim]"
        )
        lines.append(f"  [bold]{name}[/bold] — {cls.description}")
        lines.append(f"    model: {model_str}  [dim]({source})[/dim]")
    lines.append("")
    lines.append("[dim]Usage:[/dim]")
    lines.append("[dim]  /subagents                          — list all types[/dim]")
    lines.append(
        "[dim]  /subagents <type>                   — pick model interactively[/dim]"
    )
    lines.append(
        "[dim]  /subagents <type> none              — clear override (use chat model)[/dim]"
    )
    lines.append(
        "[dim]  /subagents <type> <provider> <model> — set non-interactively[/dim]"
    )
    return "\n".join(lines)


@command(
    "subagents",
    "List subagent types and configure their models",
)
async def _cmd_subagents(ctx, args):
    """Sub-actions:
    /subagents                          show all registered types + their models
    /subagents <type>                   interactive model picker for a type
    /subagents <type> none              clear the override (use chat model)
    /subagents <type> <provider> <model>  set non-interactively
    """
    from src.subagents import SUBAGENT_REGISTRY
    from ui.provider_handler import pick_subagent_model, validate_api_key

    args_str = (args or "").strip()
    if not args_str:
        return _format_subagents_index(ctx)

    parts = args_str.split()
    subagent_type = parts[0]
    if subagent_type not in SUBAGENT_REGISTRY:
        avail = ", ".join(sorted(SUBAGENT_REGISTRY)) or "(none)"
        return (
            f"[error]Unknown subagent type [bold]{subagent_type}[/bold].[/error] "
            f"[dim]Available: {avail}[/dim]"
        )

    # /subagents <type> none
    if len(parts) == 2 and parts[1].lower() == "none":
        preferences.set_subagent_model(subagent_type, None, None)
        return (
            f"[green]●[/green] Model override cleared for [bold]{subagent_type}[/bold]. "
            "[dim]Will use the active chat model.[/dim]"
        )

    # /subagents <type> <provider> <model>
    if len(parts) >= 3:
        prov, model = parts[1], " ".join(parts[2:]).strip()
        if prov not in PROVIDERS:
            return (
                f"[error]Unknown provider [bold]{prov}[/bold].[/error] "
                f"[dim]Available: {', '.join(PROVIDERS)}[/dim]"
            )
        models = provider_configs.get(prov, {}).get("models") or []
        if model not in models:
            return (
                f"[error]Model [bold]{model}[/bold] not in {prov}'s catalogue.[/error] "
                f"[dim]Available: {', '.join(models)}[/dim]"
            )
        if not validate_api_key(prov):
            return (
                f"[error]No valid API key for [bold]{prov}[/bold].[/error] "
                "[dim]Run /model to log in first.[/dim]"
            )
        preferences.set_subagent_model(subagent_type, prov, model)
        return (
            f"[green]●[/green] [bold]{subagent_type}[/bold] will use "
            f"[bold]{prov}[/bold] / [accent]{model}[/accent]."
        )

    # /subagents <type>  — interactive picker
    pt_session = ctx.get("pt_session")
    if pt_session is None:
        return "[error]Interactive picker unavailable.[/error]"

    cache = ctx.setdefault("_subagent_model_validation_cache", {})
    result = await pick_subagent_model(
        pt_session, subagent_type, validation_cache=cache
    )
    if result is None:
        return "[dim]Cancelled. Subagent model unchanged.[/dim]"
    prov, model = result
    preferences.set_subagent_model(subagent_type, prov, model)
    if prov is None:
        return (
            f"[green]●[/green] Model override cleared for [bold]{subagent_type}[/bold]. "
            "[dim]Will use the active chat model.[/dim]"
        )
    return (
        f"[green]●[/green] [bold]{subagent_type}[/bold] will use "
        f"[bold]{prov}[/bold] / [accent]{model}[/accent]."
    )


# ---------------------------------------------------------
# /exit — quit the application
# ---------------------------------------------------------
# /tracing — show / configure / toggle LangFuse tracing
# ---------------------------------------------------------
def _mask_key(value: str) -> str:
    if not value:
        return "[dim]not set[/dim]"
    if len(value) <= 8:
        return "***"
    return f"***{value[-4:]}"


def _tracing_field_source(field: str) -> str:
    """Return where a tracing field is currently being read from."""
    import os as _os

    prefs_block = preferences.get_tracing()
    if prefs_block.get(field) not in (None, ""):
        return "preferences"
    cfg_block = config.get("tracing") or {}
    if cfg_block.get(field) not in (None, ""):
        return "config.yml"
    env_var = {
        "public_key": "LANGFUSE_PUBLIC_KEY",
        "secret_key": "LANGFUSE_SECRET_KEY",
        "host": "LANGFUSE_HOST",
    }.get(field)
    if env_var and _os.environ.get(env_var):
        return f"${env_var}"
    return "—"


@command(
    "tracing",
    "Show / toggle / configure LangFuse tracing. /tracing setup runs the "
    "interactive credential prompt; /tracing on|off flips the toggle.",
)
async def _cmd_tracing(ctx, args):
    """Sub-actions:
    /tracing               show current state, masked keys, source per field
    /tracing on|off        flip preferences.tracing.enabled (restart to apply)
    /tracing setup         interactive prompts for project / host / keys
    /tracing test          post a no-op trace to verify creds (live, no restart)
    /tracing clear         remove all preferences-side overrides
    """
    from src.tracing import (
        resolve_tracing_settings,
        last_resolved_settings,
        post_score,
        _active_backend,
    )

    sub = (args or "").strip().lower()

    # ----- /tracing                show state -----
    if not sub:
        live = last_resolved_settings() or resolve_tracing_settings()
        wired = (
            "[green]wired[/green]"
            if _active_backend == "langfuse"
            else "[dim]not wired[/dim]"
        )
        lines = [
            f"[bold]Tracing[/bold]   {wired} "
            f"[dim](LangFuse — restart needed for `enabled` / `setup` changes)[/dim]",
            "",
            f"  enabled    : [bold]{'true' if live.get('enabled') else 'false'}[/bold]",
            f"  project    : {live.get('project') or '[dim]—[/dim]'}  "
            f"[dim]({_tracing_field_source('project')})[/dim]",
            f"  host       : {live.get('host') or '[dim]—[/dim]'}  "
            f"[dim]({_tracing_field_source('host')})[/dim]",
            f"  public_key : {_mask_key(live.get('public_key') or '')}  "
            f"[dim]({_tracing_field_source('public_key')})[/dim]",
            f"  secret_key : {_mask_key(live.get('secret_key') or '')}  "
            f"[dim]({_tracing_field_source('secret_key')})[/dim]",
            "",
            "[dim]Usage: /tracing on|off | /tracing setup | /tracing test | /tracing clear[/dim]",
            "[dim]Secrets are persisted to ~/.vibe-cli/preferences.json (gitignored).[/dim]",
        ]
        return "\n".join(lines)

    # ----- /tracing on|off          toggle -----
    if sub in ("on", "off"):
        preferences.set_tracing({"enabled": sub == "on"})
        was = _active_backend == "langfuse"
        will = sub == "on"
        if was == will:
            return (
                f"[green]●[/green] Tracing preference set to [bold]{sub}[/bold]. "
                "[dim]No effect this session — already in that state.[/dim]"
            )
        return (
            f"[green]●[/green] Tracing preference set to [bold]{sub}[/bold]. "
            "[warning]Restart vibe-cli for this to take effect.[/warning]"
        )

    # ----- /tracing test            send a no-op score -----
    if sub == "test":
        if _active_backend != "langfuse":
            return (
                "[error]Tracing isn't wired in this session.[/error] "
                "[dim]Run /tracing setup, then restart.[/dim]"
            )
        trace_id = ctx.get("last_trace_id")
        if not trace_id:
            return (
                "[warning]No recent trace yet.[/warning] "
                "[dim]Send any message first, then run /tracing test.[/dim]"
            )
        ok, msg = post_score(
            trace_id, "tracing.test", 1.0, comment="vibe-cli /tracing test"
        )
        return ("[green]●[/green] " if ok else "[error]✗[/error] ") + msg

    # ----- /tracing clear           wipe preferences overrides -----
    if sub == "clear":
        preferences.clear_tracing()
        return (
            "[green]●[/green] Cleared preferences.tracing — falling back to "
            "config.yml + LANGFUSE_* env vars on next launch. "
            "[warning]Restart to apply.[/warning]"
        )

    # ----- /tracing setup           interactive wizard -----
    if sub == "setup":
        pt_session = ctx.get("pt_session")
        if pt_session is None:
            return "[error]Interactive setup unavailable.[/error]"

        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.patch_stdout import patch_stdout

        current = resolve_tracing_settings()

        async def ask(
            label: str, *, default: str = "", password: bool = False
        ) -> str | None:
            placeholder = (
                HTML(f'<style fg="#52525b">{default}</style>') if default else None
            )
            sess = PromptSession() if password else pt_session
            prompt = HTML(f'<style fg="#a78bfa"><b>  {label} ❯ </b></style>')
            try:
                with patch_stdout():
                    val = await sess.prompt_async(
                        prompt,
                        placeholder=placeholder,
                        is_password=password,
                    )
            except (EOFError, KeyboardInterrupt):
                return None
            val = (val or "").strip()
            return val or default

        from ui.ui import console

        console.print()
        console.print("[bold]LangFuse setup[/bold]")
        console.print(
            "[dim]Press Enter to keep the current value (shown as placeholder). "
            "Ctrl+C to cancel.[/dim]"
        )
        console.print()

        host = await ask(
            "Host", default=current.get("host") or "https://cloud.langfuse.com"
        )
        if host is None:
            return "[dim]Cancelled. Tracing settings unchanged.[/dim]"
        project = await ask("Project", default=current.get("project") or "")
        if project is None:
            return "[dim]Cancelled. Tracing settings unchanged.[/dim]"
        public_key = await ask(
            "Public key (pk-lf-…)",
            default=current.get("public_key") or "",
            password=True,
        )
        if public_key is None:
            return "[dim]Cancelled. Tracing settings unchanged.[/dim]"
        secret_key = await ask(
            "Secret key (sk-lf-…)",
            default=current.get("secret_key") or "",
            password=True,
        )
        if secret_key is None:
            return "[dim]Cancelled. Tracing settings unchanged.[/dim]"

        preferences.set_tracing(
            {
                "enabled": True,
                "host": host or None,
                "project": project or None,
                "public_key": public_key or None,
                "secret_key": secret_key or None,
            }
        )
        return (
            "[green]●[/green] Saved to preferences.json. "
            "[warning]Restart vibe-cli to wire LangFuse in (or skip restart "
            "if it was already wired in this session).[/warning]"
        )

    return (
        f"[error]Unknown subcommand `/tracing {sub}`.[/error] "
        "[dim]Try /tracing, /tracing on|off, /tracing setup, /tracing test, /tracing clear.[/dim]"
    )


# ---------------------------------------------------------
# ---------------------------------------------------------
# /attach — add an image / PDF / text file to the next turn
# ---------------------------------------------------------
@command("attach", "Attach a file (image / PDF / text) to the next turn")
def _cmd_attach(ctx, args):
    from pathlib import Path

    from src import attachments as _atts
    from src import capabilities as _caps

    raw = (args or "").strip()
    if not raw or raw == "list":
        pending = ctx.get("pending_attachments") or []
        if not pending:
            return (
                "[dim]No pending attachments. Use `/attach <path>` to add one, "
                "or paste a file path from Finder.[/dim]"
            )
        lines = ["[bold]Pending attachments[/bold]"]
        total = 0
        for att in pending:
            lines.append(
                f"  [accent]{att.kind}[/accent]  {att.name} "
                f"[muted]({att.size / 1024:.1f} KB, {att.mime_type})[/muted]"
            )
            total += att.size
        mb = total / (1024 * 1024)
        lines.append(f"[muted]Total: {mb:.2f} MB[/muted]")
        return "\n".join(lines)

    if raw == "clear":
        n = len(ctx.get("pending_attachments") or [])
        ctx["pending_attachments"] = []
        return f"[success]●[/success] Cleared {n} pending attachment(s)."

    # Otherwise: treat raw as a path
    path = Path(raw).expanduser()
    sess = ctx.get("session")
    session_id = sess.id if sess is not None else ""
    if not session_id:
        return "[red]●[/red] No active session — cannot stage attachment."

    # Pre-detect kind to make the capability check before reading bytes.
    kind = _atts.detect_kind(path)
    if kind is None:
        return (
            f"[red]●[/red] Unsupported file type: {path.name}\n"
            "[dim]Supported: images (PNG/JPG/JPEG/WebP/GIF), PDF, "
            "common text/code files.[/dim]"
        )

    provider = ctx.get("provider") or ""
    model = provider_configs.get(provider, {}).get("model") or ""
    caps = _caps.get_capabilities(provider, model)
    if not caps.supports(kind):
        peers = _caps.peers_supporting(provider, kind, provider_configs)
        peers = [p for p in peers if p != model]
        peer_str = ", ".join(peers) if peers else "(none in this provider)"
        return (
            f"[red]●[/red] {model} is known to not accept {kind} input.\n"
            f"[dim]Not attaching. Other models in '{provider}' that may work: "
            f"{peer_str}. Use `/model` to switch.[/dim]"
        )

    mm_cfg = (ctx.get("multimodal_config") or {}).get("pdf", {}) or {}
    threshold = int(mm_cfg.get("small_threshold_pages", 15))
    dpi = int(mm_cfg.get("render_dpi", 144))
    image_capable = caps.supports("image")
    try:
        att = _atts.load_attachment(
            path,
            session_id=session_id,
            image_capable=image_capable,
            small_threshold_pages=threshold,
            render_dpi=dpi,
        )
    except _atts.AttachmentError as exc:
        return f"[red]●[/red] {exc}"

    pending = ctx.setdefault("pending_attachments", [])
    try:
        _atts.check_turn_budget(att.size, pending)
    except _atts.AttachmentError as exc:
        return f"[red]●[/red] {exc}"
    pending.append(att)

    extra = ""
    if att.kind == "pdf":
        if att.pdf_render_strategy == "full":
            extra = (
                f" — full mode ({att.pdf_page_count} pages: text + page "
                "images sent inline)"
            )
        else:
            tail_hint = ""
            if (
                att.pdf_page_count is not None
                and att.pdf_page_count <= threshold
                and image_capable
                and not _atts._pymupdf_available()
            ):
                tail_hint = (
                    " [dim](install pymupdf for inline rendering of "
                    "small PDFs: `uv pip install pymupdf`)[/dim]"
                )
            extra = (
                f" — manifest mode ({att.pdf_page_count} pages; model "
                "reads pages on demand via read_pdf_pages)" + tail_hint
            )
    elif att.kind == "text":
        extra = " — content will be inlined"
    return (
        f"[success]📎[/success] Attached [bold]{att.name}[/bold] "
        f"[muted]({att.kind}, {att.size / 1024:.1f} KB)[/muted]{extra}"
    )


# ---------------------------------------------------------
# /paste — attach an image from the clipboard
# ---------------------------------------------------------
@command("paste", "Attach an image from your clipboard")
def _cmd_paste(ctx, _args):
    """Grab an image off the OS clipboard and attach it to the next turn.

    Use this for screenshots: ⌘⌃⇧4 (or your platform's screenshot
    shortcut) copies the image to the clipboard, then `/paste` here picks
    it up. Terminal emulators don't usually translate clipboard image
    data into pasted input, so this is the explicit path.
    """
    import tempfile
    from pathlib import Path

    from src import attachments as _atts
    from src import capabilities as _caps
    from src.clipboard import clipboard_help_hint, read_clipboard_image

    sess = ctx.get("session")
    if sess is None:
        return "[red]●[/red] No active session — cannot stage attachment."

    data = read_clipboard_image()
    if not data:
        return (
            "[red]●[/red] No image found on the clipboard.\n"
            f"[dim]Copy an image first (e.g. ⌘⌃⇧4 on macOS), "
            f"then re-run /paste. {clipboard_help_hint()}.[/dim]"
        )

    # Capability gate before we touch disk.
    provider = ctx.get("provider") or ""
    model = provider_configs.get(provider, {}).get("model") or ""
    caps = _caps.get_capabilities(provider, model)
    if not caps.supports("image"):
        peers = [
            p
            for p in _caps.peers_supporting(provider, "image", provider_configs)
            if p != model
        ]
        peer_str = ", ".join(peers) if peers else "(none in this provider)"
        return (
            f"[red]●[/red] {model} is known to not accept image input.\n"
            f"[dim]Not attaching. Switch model with /model. Peers that may work: "
            f"{peer_str}.[/dim]"
        )

    # Write the PNG to a temp file so load_attachment can size-check + spill
    # to the artifact store using the same code path as a real file.
    with tempfile.NamedTemporaryFile(
        prefix="vibe-clip-", suffix=".png", delete=False
    ) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)

    try:
        att = _atts.load_attachment(tmp_path, session_id=sess.id)
    except _atts.AttachmentError as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return f"[red]●[/red] {exc}"

    pending = ctx.setdefault("pending_attachments", [])
    try:
        _atts.check_turn_budget(att.size, pending)
    except _atts.AttachmentError as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return f"[red]●[/red] {exc}"
    # Give the attachment a friendlier display name than the temp file.
    att.name = "clipboard.png"
    pending.append(att)
    try:
        tmp_path.unlink(missing_ok=True)
    except Exception:
        pass

    return (
        f"[success]📎[/success] Attached [bold]{att.name}[/bold] from clipboard "
        f"[muted]({att.size / 1024:.1f} KB)[/muted]"
    )


# ---------------------------------------------------------
# /capabilities — inspect / clear learned model capabilities
# ---------------------------------------------------------
@command("capabilities", "Show or clear learned multimodal capabilities")
def _cmd_capabilities(_ctx, args):
    from ui import preferences as _prefs

    parts = (args or "").strip().split()
    if parts and parts[0] == "clear":
        if len(parts) < 2:
            return (
                "[red]●[/red] Usage: /capabilities clear <provider> [model]"
            )
        provider = parts[1]
        model = parts[2] if len(parts) >= 3 else None
        ok = _prefs.clear_model_capabilities(provider, model)
        target = f"{provider}/{model}" if model else provider
        if ok:
            return f"[success]●[/success] Cleared learned capabilities for {target}."
        return f"[muted]No learned capabilities recorded for {target}.[/muted]"

    raw = _prefs.get_model_capabilities_all()
    if not raw:
        return (
            "[dim]No learned capabilities yet. vibe-cli assumes every model "
            "supports images / PDFs; we'll record any provider rejections "
            "we observe at runtime.[/dim]"
        )

    lines = ["[bold]Learned model capabilities[/bold]"]
    for provider in sorted(raw.keys()):
        lines.append(f"[accent]{provider}[/accent]")
        for model in sorted((raw[provider] or {}).keys()):
            entry = raw[provider][model]
            flags = []
            for k in ("image", "pdf"):
                v = entry.get(k) if isinstance(entry, dict) else None
                if v == "no":
                    flags.append(f"[red]{k}:no[/red]")
                elif v == "yes":
                    flags.append(f"[success]{k}:yes[/success]")
            recorded = (
                entry.get("_recorded_at", "") if isinstance(entry, dict) else ""
            )
            tail = f" [muted]({recorded})[/muted]" if recorded else ""
            lines.append(f"  {model} — {' '.join(flags) or '(no flags)'}{tail}")
    lines.append("")
    lines.append(
        "[dim]Use `/capabilities clear <provider> [model]` to reset.[/dim]"
    )
    return "\n".join(lines)


@command("exit", "Exit the application")
def _cmd_exit(ctx, _args):
    ctx["exit"] = True
    return "[dim]Saving conversation and exiting…[/dim]"
