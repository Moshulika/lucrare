from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Any
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.patch_stdout import patch_stdout
from rich.panel import Panel
from rich.text import Text
from src.logging_setup import emit_event, get_logger
from ui import preferences
from ui.ui import console

_perm_log = get_logger("permission")

@dataclass
class Decision:
    allow: bool
    reason: str = ""

_session_allow: set[str] = set()

def reset_session():
    _session_allow.clear()

def session_allow(tool_name: str):
    _session_allow.add(tool_name)

def is_session_allowed(tool_name: str) -> bool:
    return tool_name in _session_allow

def _format_args(tool_args: Any) -> str:
    try:
        s = json.dumps(tool_args, indent=2, default=str)
    except (TypeError, ValueError):
        s = str(tool_args)
    if len(s) > 1500:
        s = s[:1500] + "\n… (truncated)"
    return s

def _render_prompt_panel(tool_name: str, tool_args: Any):
    body = Text()
    body.append("Tool: ", style="muted")
    body.append(f"{tool_name}\n\n", style="bold accent")
    body.append("Arguments:\n", style="muted")
    body.append(_format_args(tool_args))
    body.append("\n\n")
    body.append("  1", style="bold accent")
    body.append("  Yes, run this call\n")
    body.append("  2", style="bold accent")
    body.append("  Yes, and don't ask again this session\n")
    body.append("  a", style="bold accent")
    body.append("  Always allow this tool (persist)\n")
    body.append("  3", style="bold accent")
    body.append("  No (provide a reason)")

    console.print()
    console.print(
        Panel(
            body,
            title="[bold]Tool permission required[/bold]",
            border_style="primary",
            padding=(1, 2),
        )
    )

async def _prompt_choice(session: PromptSession | None) -> str:
    prompt_msg = HTML('<style fg="#a78bfa"><b>permission ❯ </b></style>')
    if session is None:
        session = PromptSession()
    with patch_stdout():
        return (await session.prompt_async(prompt_msg)).strip().lower()

async def _prompt_reason(session: PromptSession | None) -> str:
    prompt_msg = HTML('<style fg="#a78bfa"><b>reason ❯ </b></style>')
    if session is None:
        session = PromptSession()
    with patch_stdout():
        return (await session.prompt_async(prompt_msg)).strip()

async def request_permission(
    tool_name: str,
    tool_args: Any,
    ctx: dict | None = None,
) -> Decision:
    ctx = ctx or {}

    if ctx.get("skip_permissions"):
        emit_event(
            _perm_log, "permission.auto_allow", tool=tool_name, scope="skip_permissions"
        )
        return Decision(allow=True)

    if tool_name in preferences.get_always_deny():
        emit_event(
            _perm_log, "permission.auto_deny", tool=tool_name, scope="always_deny"
        )
        return Decision(
            allow=False,
            reason="Tool is on the user's permanent deny list.",
        )

    if tool_name in preferences.get_always_allow():
        emit_event(
            _perm_log, "permission.auto_allow", tool=tool_name, scope="always_allow"
        )
        return Decision(allow=True)

    if is_session_allowed(tool_name):
        emit_event(
            _perm_log, "permission.auto_allow", tool=tool_name, scope="session"
        )
        return Decision(allow=True)

    session: PromptSession | None = ctx.get("pt_session")
    status = ctx.get("status")
    if status is not None:
        try:
            status.stop()
        except Exception:
            pass

    emit_event(_perm_log, "permission.prompt", tool=tool_name)
    try:
        return await _interactive_prompt(tool_name, tool_args, session)
    finally:
        if status is not None:
            try:
                status.start()
            except Exception:
                pass

async def _interactive_prompt(
    tool_name: str,
    tool_args: Any,
    session: PromptSession | None,
) -> Decision:
    while True:
        _render_prompt_panel(tool_name, tool_args)
        try:
            choice = await _prompt_choice(session)
        except (EOFError, KeyboardInterrupt):
            emit_event(
                _perm_log,
                "permission.decision",
                tool=tool_name,
                allow=False,
                scope="cancelled",
            )
            return Decision(
                allow=False,
                reason="User cancelled the permission prompt.",
            )

        if choice in ("1", "y", "yes"):
            emit_event(_perm_log, "permission.decision", tool=tool_name, allow=True, scope="once")
            return Decision(allow=True)
        if choice == "2":
            session_allow(tool_name)
            emit_event(_perm_log, "permission.decision", tool=tool_name, allow=True, scope="session")
            return Decision(allow=True)
        if choice == "a":
            preferences.add_always_allow(tool_name)
            emit_event(_perm_log, "permission.decision", tool=tool_name, allow=True, scope="always_allow")
            return Decision(allow=True)
        if choice in ("3", "n", "no"):
            try:
                reason = await _prompt_reason(session)
            except (EOFError, KeyboardInterrupt):
                reason = ""
            emit_event(
                _perm_log,
                "permission.decision",
                tool=tool_name,
                allow=False,
                scope="denied",
                reason_len=len(reason),
            )
            return Decision(
                allow=False,
                reason=reason or "User denied this tool call without a reason.",
            )

        console.print("[muted]Please choose 1, 2, a, or 3.[/muted]")