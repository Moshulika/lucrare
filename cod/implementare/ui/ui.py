"""
vibe-cli v2 — UI components: theme, console, and display helpers.
"""

from __future__ import annotations

import difflib
import random

from rich.columns import Columns
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme


# ---------------------------------------------------------
# Theme
# ---------------------------------------------------------
VIBE_RICH_THEME = Theme(
    {
        "primary": "#a78bfa",
        "accent": "#c084fc",
        "secondary": "#6d6d8a",
        "muted": "#52525b",
        "user.glyph": "bold #a78bfa",
        "assistant.glyph": "bold #c084fc",
        "system.glyph": "bold #6d6d8a",
        "role.user": "bold #a78bfa",
        "role.assistant": "bold #c084fc",
        "role.system": "bold #6d6d8a",
        "prefix": "#6d6d8a",
        "thinking": "dim #6d6d8a",
        "status": "#52525b",
        "info": "dim",
    }
)

console = Console(theme=VIBE_RICH_THEME)


# ---------------------------------------------------------
# Display helpers
# ---------------------------------------------------------
ROLE_GLYPHS = {
    "user": "◇",
    "assistant": "◆",
    "system": "●",
}

SPLASH_ART = (
    "[#4A0E4E]██╗   ██╗██╗██████╗ ███████╗[/]\n"
    "[#5B1260]██║   ██║██║██╔══██╗██╔════╝[/]\n"
    "[#7A1C7D]██║   ██║██║██████╔╝█████╗  [/]\n"
    "[#96249C]╚██╗ ██╔╝██║██╔══██╗██╔══╝  [/]\n"
    "[#B632BD] ╚████╔╝ ██║██████╔╝███████╗[/]\n"
    "[#D34BE6]  ╚═══╝  ╚═╝╚═════╝ ╚══════╝[/]"
)

SPLASH_INFO = (
    "[bold #E0AAFF]Welcome to Vibe CLI[/]\n"
    "\n"
    "[#6d6d8a]Version[/]  [bold]0.1.0[/]\n"
    "[#6d6d8a]Engine[/]   [bold]LangGraph[/]\n"
    "\n"
    "[dim]Multi-provider AI chat agent.\n"
    "Switch between OpenAI, Claude,\n"
    "and Gemini on the fly.[/dim]"
)


def print_splash():
    art_panel = Text.from_markup(SPLASH_ART)
    info_panel = Text.from_markup(SPLASH_INFO)

    layout = Columns(
        [art_panel, info_panel],
        padding=(0, 3),
        align="center",
    )

    console.print()
    console.print(
        Panel(
            layout,
            border_style="#4A0E4E",
            padding=(1, 2),
        )
    )
    console.print()


def print_message(text: str, role: str):
    """Render a single chat message."""
    glyph = ROLE_GLYPHS.get(role, "·")
    label = "you" if role == "user" else role

    if role == "user":
        width = console.width
        console.print()
        header = Text(f" {glyph} {label}", style="role.user on #1a1a2e")
        header.pad_right(width)
        console.print(header)
        body = Text(f" ⎿ {text}", style="on #1a1a2e")
        body.pad_right(width)
        console.print(body)
        console.print()
    elif role == "assistant":
        # Indented to align with the muted "  ◆ <phrase>" elapsed footer.
        console.print(
            f"  [assistant.glyph]{glyph}[/] [role.assistant]{label}[/role.assistant]"
        )
        console.print(Text("  ⎿   ", style="prefix"), end="")
        console.print(Markdown(text))
        console.print()
    else:
        console.print(f"[{role}.glyph]{glyph}[/] [role.{role}]{label}[/role.{role}]")
        console.print(f"  {text}", style="muted")
        console.print()


def print_system(markup: str):
    """Print a system/command response with Rich markup."""
    glyph = ROLE_GLYPHS["system"]
    console.print(f"[system.glyph]{glyph}[/] [role.system]system[/role.system]")
    console.print(f"  {markup}")
    console.print()


# ---------------------------------------------------------
# Status labels & diff rendering
# ---------------------------------------------------------
def _truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


_TOOL_DISPLAY = {
    "read_file": ("Read", "path"),
    "write_file": ("Write", "path"),
    "edit_file": ("Edit", "path"),
    "list_directory": ("List", "path"),
    "run_command": ("Bash", "command"),
    "web_search": ("Search", "query"),
    "fetch_artifact": ("Fetch", "sha"),
    "run_skill": ("Skill", "skill_name"),
    "spawn_subagent": ("Agent", "subagent_type"),
}


def _tool_display(name: str, args: dict | None) -> tuple[str, str]:
    """Return (display_name, primary_arg) for a tool call.

    For built-in tools, picks a Claude-Code-style short name and the
    most relevant arg. For MCP tools (`<server>__<tool>`), uses the
    tool half of the name.
    """
    args = args or {}
    if name in _TOOL_DISPLAY:
        display, key = _TOOL_DISPLAY[name]
        primary = str(args.get(key, "")) if isinstance(args, dict) else ""
        return display, primary
    if "__" in name:
        return name.split("__", 1)[1], ""
    return name, ""


def label_for_tool(name: str, args: dict | None) -> str:
    """Spinner label, e.g. `Read(path/to/file)` or `Bash(grep -r foo)`."""
    display, primary = _tool_display(name, args)
    if not primary:
        return display
    return f"{display}({_truncate(primary, 60)})"


def tool_log_line(name: str, args: dict | None) -> str:
    """Persistent scroll-back line for a tool call. Same shape as the
    spinner label; rendered in muted style by the caller.
    """
    return label_for_tool(name, args)


_ELAPSED_PHRASES = (
    "Done in",
    "Cooked in",
    "Wrapped in",
    "Vibed in",
    "Brewed in",
    "Baked in",
    "Whipped up in",
    "Knocked out in",
    "Polished in",
    "Crafted in",
    "Shipped in",
    "Sorted in",
    "Dusted in",
    "Settled in",
)


def format_elapsed(seconds: float) -> str:
    """Rich-markup string for the per-turn elapsed line.

    Shape: `  ◆ <phrase> <X.Xs>` — phrase chosen randomly from a small
    pool to keep the UI feeling alive across turns.
    """
    phrase = random.choice(_ELAPSED_PHRASES)
    return f"[muted]  ◆ {phrase} {seconds:.1f}s[/muted]"


_DIFF_MAX_LINES = 60


def render_diff_panel(path: str, before: str, after: str):
    """Build a Rich Panel showing a unified diff for a file edit.

    Returns None if there's no actual change. Truncates large diffs to
    `_DIFF_MAX_LINES` body lines and appends a `+N more` footer.
    """
    if before == after:
        return None
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=2,
        )
    )
    if not diff_lines:
        return None

    # Strip the `--- a/...` and `+++ b/...` header lines for compactness;
    # the panel title already names the file.
    body = [
        ln for ln in diff_lines
        if not ln.startswith("--- ") and not ln.startswith("+++ ")
    ]
    added = sum(
        1 for ln in body if ln.startswith("+") and not ln.startswith("++")
    )
    removed = sum(
        1 for ln in body if ln.startswith("-") and not ln.startswith("--")
    )

    extra = 0
    if len(body) > _DIFF_MAX_LINES:
        extra = len(body) - _DIFF_MAX_LINES
        body = body[:_DIFF_MAX_LINES]

    diff_text = "".join(body).rstrip("\n")
    if extra:
        diff_text += f"\n… +{extra} more lines"

    syntax = Syntax(
        diff_text,
        "diff",
        theme="ansi_dark",
        word_wrap=False,
        background_color="default",
    )
    title = (
        f"[bold]Edited:[/bold] {path}  "
        f"[accent]+{added}[/accent] [secondary]−{removed}[/secondary]"
    )
    return Panel(
        syntax,
        title=title,
        title_align="left",
        border_style="primary",
        padding=(0, 1),
    )
