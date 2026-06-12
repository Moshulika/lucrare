# src/subagents

Subagent framework — short-lived child agents the main agent can spawn via
the `spawn_subagent` tool. Full reference: [`docs/SUBAGENTS.md`](../../docs/SUBAGENTS.md).
This README is the quick map for editing this directory.

## Layout

```
src/subagents/
├── __init__.py         # registry + public API (Subagent, run_subagent, get, ...)
├── base.py             # Subagent ABC + SubagentResult + orchestrator
├── compaction.py       # in-memory ToolMessage truncator (no Session needed)
├── types/
│   ├── general.py      # full-toolset research agent
│   ├── code_search.py  # read-only code lookup
│   └── web_research.py # web search + synthesis
└── README.md           # you are here
```

## Adding a new subagent type

1. Create a file in `types/` and subclass `Subagent`:

   ```python
   from src.subagents import register
   from src.subagents.base import Subagent

   @register
   class MyAgent(Subagent):
       name = "my_agent"
       description = "One-liner shown in /subagents and in the tool docstring."
       default_timeout_s = 120
       max_iterations = 20
       # Tool whitelist. None = inherit the parent's full set (minus `excluded_tools`).
       allowed_tools = ["read_file", "list_directory"]

       def role_prompt(self, prompt: str, parent_ctx: dict) -> str:
           # Type-specific section only — the shared SUBAGENT_CONTRACT (scope,
           # no unauthorised state changes, always end with a report, no
           # questions) is prepended automatically by Subagent.system_prompt.
           # Focus this method on what makes your type unique: toolset
           # conventions, output format, scope notes.
           return "ROLE: my_agent — <task framing>. <output format>."
   ```

   Override `system_prompt` directly only if you genuinely need to replace
   the shared contract; for normal extension just override `role_prompt`.

2. Eagerly import it from `__init__.py` so the registry is populated on import:

   ```python
   from .types import my_agent as _my_agent  # noqa
   ```

3. Optionally add a config entry under `subagents.types.<name>` in `config.yml`
   for per-type defaults (`model_override`, `max_iterations`, `default_timeout_s`).
   The seed-on-first-run loader (`ui/config_init.py`) will merge new keys into
   existing user configs automatically.

That's it. The new type is immediately:

- Listed by `/subagents`
- Callable as `spawn_subagent("my_agent", "...")`
- Configurable via `/subagents my_agent <provider> <model>`
- Visible in the web dashboard's Subagents card with its own row
- Eligible for `subagent_match` / `subagent_output_similarity` eval metrics

## Key contracts

- `Subagent.run()` is the entry point. The default implementation handles
  timeout (`asyncio.wait_for`), depth-cap enforcement (via `run_subagent`),
  iteration cap, exception capture, and a one-shot "write your final
  report" retry when the loop exits with empty text. Subclasses rarely
  need to override it — `role_prompt` and the class-level attributes are
  usually enough.
- `SUBAGENT_CONTRACT` in `base.py` is the shared system-prompt prefix
  applied to every type (scope, no unauthorised state changes, always
  end with a report, no questions). This is the universal orchestrator
  contract — don't subvert it from a subclass unless you have a very
  good reason.
- Child tool calls MUST go through `request_permission` and emit
  `tool.start` / `tool.end` events. `_invoke_tools` in `base.py` already
  does this; preserve the pattern if you override.
- Children share the parent's runtime ctx (sandbox, `always_allow`,
  `pt_session`, MCP tools). Do not pop or replace fields the parent depends
  on — the parent's ctx is shallow-copied in `_drive_loop` precisely so
  scoped fields (e.g. `status_label`) can be mutated without leaking back.

## When NOT to add a subagent type

If the only difference from `general` is the system prompt and the model,
consider whether a **skill** (see `docs/SKILLS.md`) would be a better fit.
Skills are file-backed, user-editable, and progressively disclosed — no
code change needed. Reserve new subagent types for cases where the toolset
itself needs to be narrower, or where you want the tool to surface a
dedicated UI label (`Agent(<your_name>)`).
