"""General-purpose subagent — research / multi-step tasks."""

from __future__ import annotations

from src.subagents import register
from src.subagents.base import Subagent


@register
class GeneralSubagent(Subagent):
    name = "general"
    description = (
        "General-purpose research and task subagent. Has access to all parent "
        "tools (except spawning further subagents). Use for open-ended "
        "questions, multi-step investigations, and tasks that span files. "
        "By default acts as an advisor — does not modify state unless the "
        "orchestrator explicitly asks."
    )
    default_timeout_s = 180
    max_iterations = 25
    # None ⇒ inherit all parent tools (with `excluded_tools` removed).
    allowed_tools = None

    def role_prompt(self, prompt: str, parent_ctx: dict) -> str:
        return (
            "ROLE: general-purpose research / task subagent.\n\n"
            "You have access to the parent's tool set (read, list, search, "
            "shell, plus any MCP tools). Use it to investigate the task and "
            "produce a single final report.\n\n"
            "Default posture is **advisory**: read, search, run non-mutating "
            "commands, gather facts, then report. Only write or edit files, "
            "run mutating commands, or otherwise change state if the "
            "orchestrator's prompt explicitly says to (e.g. \"write a file\", "
            "\"fix this code\", \"run the migration\"). When the prompt is "
            "ambiguous on this point, treat it as advisory and let the "
            "orchestrator do the writing based on your report.\n\n"
            "Stop as soon as you have enough to answer — do not keep "
            "exploring. Then write the final report:\n"
            "  - Lead with the answer or finding.\n"
            "  - Cite file paths / line ranges / commands.\n"
            "  - If you took any state-changing action, list it explicitly "
            "    so the orchestrator can verify."
        )
