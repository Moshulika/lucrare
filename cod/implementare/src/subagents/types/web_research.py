"""Web-research subagent — search the web and synthesize."""

from __future__ import annotations

from src.subagents import register
from src.subagents.base import Subagent


@register
class WebResearchSubagent(Subagent):
    name = "web_research"
    description = (
        "Web research subagent. Searches the web, reads results, and returns "
        "a synthesised brief with citations. No file system or shell access."
    )
    default_timeout_s = 120
    max_iterations = 18
    allowed_tools = ["web_search"]

    def role_prompt(self, prompt: str, parent_ctx: dict) -> str:
        return (
            "ROLE: web-research subagent.\n\n"
            "Your only tool is `web_search`. You cannot read or write the "
            "filesystem, run shell commands, or take any local action.\n\n"
            "Approach:\n"
            "  - Issue 1–3 focused queries; do not fan out indefinitely.\n"
            "  - Cross-check claims across results when the topic is "
            "    factual or time-sensitive.\n"
            "  - Be explicit about uncertainty — if the web doesn't agree, "
            "    say so.\n\n"
            "Final report format (Markdown):\n"
            "  - 1–3 paragraph synthesis (or bullets) answering the "
            "    orchestrator's request.\n"
            "  - A `Sources` section listing the URLs you actually relied on, "
            "    each with a one-line description."
        )
