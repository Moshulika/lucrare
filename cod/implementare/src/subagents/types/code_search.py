"""Read-only code-search subagent — fast lookups across the codebase."""

from __future__ import annotations

from src.subagents import register
from src.subagents.base import Subagent


@register
class CodeSearchSubagent(Subagent):
    name = "code_search"
    description = (
        "Fast read-only code search. Finds files/functions/symbols and "
        "reports paths + line ranges + relevant snippets. Cannot write or "
        "execute side-effectful commands."
    )
    default_timeout_s = 90
    max_iterations = 15
    # Read-only toolset. `run_command` is included for grep/rg/find use.
    allowed_tools = ["read_file", "list_directory", "run_command", "fetch_artifact"]

    def role_prompt(self, prompt: str, parent_ctx: dict) -> str:
        return (
            "ROLE: read-only code-search subagent.\n\n"
            "Your job is to LOCATE code that matches the orchestrator's "
            "request and report findings concisely. You are an advisor only "
            "— even though `run_command` is available, it is for read-only "
            "searches (rg / grep / find / ls / cat / wc). NEVER invoke "
            "installers, builds, test runs, package managers, or any "
            "command that mutates state.\n\n"
            "Stop searching once you have enough to answer; do not enumerate "
            "exhaustively.\n\n"
            "Final report format:\n"
            "  - For each match: `path:line` + a one-line description.\n"
            "  - When useful, include a short snippet (≤6 lines).\n"
            "  - End with a one-sentence takeaway for the orchestrator."
        )
