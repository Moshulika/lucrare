# Compaction pipeline

A pluggable, configurable system for keeping the LLM's working set small as
sessions grow. Lives at `src/compaction/`. Configured via the `compaction`
block in `config/config.yml`.

## Two cross-turn modes: ephemeral vs. persistent tool history

Before any of the stages below kick in, the most consequential knob is
`compaction.tool_persistence`:

- **`tool_persistence: false` (default — ephemeral mode).** Tool calls and
  tool results live only inside the LangGraph turn that produced them. After
  the turn ends, the cross-turn `messages` buffer keeps just the user prompt
  and the assistant's final text. The model cannot re-read raw tool output
  from earlier turns; its prior text response *is* the summary. Provider-safe
  (history contains no provider-specific tool message blocks). This is
  vibe-cli's historical behavior.
- **`tool_persistence: true` (persistent mode, Claude-Code-like).** Tool
  calls and tool results stay in the cross-turn buffer until threshold
  compaction trims them. The model can refer back to raw output from
  arbitrary earlier turns. Costs context fast — pair with at least
  `tool_truncate` and `importance_prune`, ideally also `bulk`, otherwise the
  buffer fills in 5–10 tool-heavy turns.

The eager and threshold stages described below run in *both* modes, but they
have very different load:
- In **ephemeral** mode, eager stages only matter for very large *single*
  tool outputs within one turn (e.g., a `read_file` returning 500 KB). There
  is no cross-turn tool history for them to compact.
- In **persistent** mode, eager stages compact cross-turn tool history as it
  accumulates, and threshold stages handle long-tail growth at ~80% of the
  context window.

### Cross-provider safety: `enforce_session_model`

When `tool_persistence` is on, switching providers mid-conversation can
corrupt the cross-turn history because tool message formats differ across
providers (OpenAI `tool_call_id` vs. Anthropic `tool_use` blocks vs. Gemini
`functionCall` parts) and per-provider `additional_kwargs` may not survive
re-submission.

`compaction.enforce_session_model: true` (default, recommended) gates any
`/model` switch behind a confirm-and-rotate prompt: once the user confirms,
the current session is saved and a fresh one is rotated in before the new
provider/model takes effect. Same-session, same-provider, same-model
selections are no-ops as before.

`enforce_session_model: false` is **experimental**. Set it only if you're
deliberately testing cross-provider replay and willing to debug wedged
sessions — vibe-cli will log a warning and silently fall back to the
ephemeral projection if it detects mismatched `tool_call ↔ tool_result`
pairing on resume, but mid-session breakage is on you to debug.

### Resume behavior

`Session.to_messages()` honours `tool_persistence` so resumed sessions match
the live shape:
- Ephemeral mode → `HumanMessage` + final `AIMessage` per turn (drops thinking
  + tool events).
- Persistent mode → groups events by `llm_call_id`, emits one `AIMessage`
  per LLM call carrying its text + `tool_calls`, followed by paired
  `ToolMessage`s. If pairing fails (orphan id, duplicate id, missing
  result) it falls back to the ephemeral shape and logs
  `session.tool_pairing_fallback` so the user can investigate.

## Why a pipeline (not a single strategy)

Old, large tool outputs aren't the same problem as old, redundant turns.
Each compaction technique solves a different shape of bloat:

| Stage              | Mode      | Targets                                  |
| ------------------ | --------- | ---------------------------------------- |
| `tool_truncate`    | eager     | one big tool result (truncate to head+tail) |
| `tool_summarize`   | eager     | one big tool result (LLM summary; truncate fallback) |
| `format_compress`  | eager     | verbose JSON tool I/O                    |
| `ref_substitute`   | eager     | very large tool results (full handoff)   |
| `dedupe`           | threshold | near-duplicate content                   |
| `importance_prune` | threshold | low-value events (thinking, intermediate tool results) |
| `bulk`             | threshold | the long tail of older turns             |

### Mutual exclusion: `tool_result_rewrite` group

`tool_truncate`, `tool_summarize`, and `ref_substitute` all rewrite the same
event slot — `event['projected']` for `tool_result` events. Enabling more
than one is meaningless: whichever runs last silently clobbers the others.
The pipeline builder (`build_eager_pipeline`) detects this at startup and
raises `CompactionConfigError`, aborting before the first turn.

Pick one based on tradeoff:
- `tool_truncate` — cheapest, no LLM call, deterministic.
- `tool_summarize` — richest projection (LLM-backed), with `tool_truncate` as
  its always-on synchronous fallback. Use when latency budget allows.
- `ref_substitute` — shortest possible projected body, but the agent must
  call `fetch_artifact` to recover. Highest agent burden.

The exclusion group is declared centrally — see
`EAGER_EXCLUSION_GROUPS` in `src/compaction/__init__.py`.

Stages compose. Eager stages run as events are appended to the session log;
threshold stages run only when context utilisation crosses the configured
threshold (or `/compact` is invoked).

## Disabled by default — turn on one at a time

Every stage ships with `enabled: false`. The intent is to flip them on
**individually** and compare against the pre-compaction snapshot in
`~/.vibe-cli/session_backups/<project>/<session-id>/`. That folder holds a
byte-identical copy of the session file *before* each threshold pass —
forensic data only, never read by the live UI.

## Storage layout

```
~/.vibe-cli/
  sessions/<project>/<uuid>.json          source of truth (append-only)
  session_backups/<project>/<uuid>/
      pre-compaction-<ts>.json            snapshot before each threshold pass
  artifacts/<session-id>/<sha>.<ext>      content-addressed payloads spilled
                                          out of the LLM context (TTL 30d,
                                          deleted with the session)
```

The session file is the source of truth for what really happened. Each
event keeps its original `content` immutably. Eager stages write a parallel
`projected` field that the LLM sees instead. Threshold stages emit a
`KIND_COMPACTION` event whose `meta.removed_event_ids` mark dropped events;
`Session.to_messages()` honours both fields when building the live message
stream and when resuming.

The artifact store is content-addressed by sha256, scoped per session. A
lazy 30-day TTL GC runs opportunistically on session creation; artifact
directories are also removed when a session is deleted.

## Eager stages

Each stage's full config block lives in `config/config.yml`. Every option
shown is the default.

### `tool_truncate`

```yaml
- tool_truncate:
    enabled: false
    max_tool_tokens: 2000
    head_tail: [800, 400]
```

Caps individual `tool_result` events. Bodies above `max_tool_tokens` are
spilled to the artifact store and replaced with a head + middle marker +
tail excerpt that points at the `@artifact:<sha>` handle. The agent can
re-fetch via `fetch_artifact`.

### `tool_summarize`

```yaml
- tool_summarize:
    enabled: false
    max_tool_tokens: 2000
    head_tail: [800, 400]
    timeout_s: 30
    max_summary_tokens: 500
    model: null   # informational; the resolver always reads
                  # preferences.compaction_model with chat-model fallback
```

LLM-summarises individual oversized `tool_result` events in place. Mutex
with `tool_truncate` and `ref_substitute` (see "Mutual exclusion" above).

#### Two-phase execution

Every invocation runs in two phases that together guarantee the context
budget is bounded *before* any async work starts:

1. **Synchronous truncate.** Identical to `tool_truncate`'s output: head +
   tail excerpt with the full payload spilled to the artifact store. Goes
   into `event['projected']` immediately and the event is now safe to ship.
   Stamps `meta.compaction.tool_summarize.state = "truncated_pending"`.
2. **Async LLM upgrade.** Spawned as an `asyncio.Task` (no awaiting in the
   tool loop). On success the task replaces `event['projected']` with the
   summary and flips state to `"summarized"`. On any failure (timeout,
   exception, no LLM available) the truncated form stays put, state flips
   to `"truncated_fallback"`, and a `compaction.eager.summarize_fallback`
   event is logged.

The truncated form is on disk from step 1, so the pipeline degrades cleanly
even if the LLM is unreachable, out of quota, or rate-limited — the user's
context never blows up. This is the design constraint: `tool_summarize` is
strictly stronger than `tool_truncate` in the happy path and exactly equal
in the failure path.

#### Background-task lifecycle

Tasks spawned during a turn live in `ctx['pending_summary_tasks']` and are
drained at the **top of the next turn** with a 2-second budget
(`_drain_pending_summary_tasks`). Tasks that finish in time patch the event
on disk before the next prompt assembly; tasks that miss the budget keep
running and patch in place when they do finish — the upgrade just lands one
turn later (the *current* turn already had the truncated fallback, which
is correct context).

On `/exit` and Ctrl+C we drain again with a small budget so in-flight
upgrades the user already paid for don't get lost.

#### Compaction model

`tool_summarize` (and `bulk.summarize`/`bulk.hybrid`) resolve their LLM via
`ui/app.py:_resolve_compaction_llm`, which reads in this order:

1. `preferences.compaction_model` (set via `/compaction model`) — the user
   may route compaction to a cheaper / longer-context model.
2. The active chat model — historical default, always works.

If the override fails to instantiate (revoked key, unknown model), the
resolver falls back to the chat model and emits one
`compaction.llm_resolve` warning event.

### `format_compress`

```yaml
- format_compress:
    enabled: false
    per_tool: {}
```

Re-serialises JSON tool I/O without indentation, drops null / empty fields.
Per-tool overrides via `per_tool: {tool_name: {mode: passthrough}}`. No-ops
when compaction wouldn't actually save chars (already minified).

### `ref_substitute`

```yaml
- ref_substitute:
    enabled: false
    min_ref_tokens: 1500
    excerpt_tokens: 200
```

Stronger than `tool_truncate`: replaces the entire body with `@artifact:<sha>`
plus a small head excerpt. Because this *removes* information from the LLM's
context, only enable once the agent prompt teaches the model when to call
`fetch_artifact` to recover.

## Threshold stages

Run only when `current_size / context_limit >= compaction.trigger.threshold`.
The orchestrator (`run_threshold_pass`) snapshots the session file first,
runs every enabled stage in declared order, aggregates their declared
removals, and writes one `KIND_COMPACTION` event with a per-stage breakdown
in `meta.stages`.

### `dedupe`

```yaml
- dedupe:
    enabled: false
    similarity: 0.92
    backend: string  # string | embedding (embedding is future work)
```

Two passes:
1. Identical `tool_call`s (same name + JSON-equal args) — drop later duplicates.
2. Near-duplicate `tool_result`s — `difflib.SequenceMatcher` ratio with a
   length-based quick-reject. Older near-duplicate is the one removed.

Worst case is O(N²) over the count of `tool_result` events.

**Future work**: an `embedding` backend will catch same-meaning-different-wording
duplicates. Skipped for now to avoid a hard embedding-model dependency.

### `importance_prune`

```yaml
- importance_prune:
    enabled: false
    keep_kinds: [human, system_prompt, assistant_text, tool_result_last]
    drop_kinds: [assistant_thinking]
```

- `drop_kinds` events are unconditionally removed.
- The pseudo-kind `tool_result_last` in `keep_kinds` means: per turn, keep
  only the last `tool_result`. Earlier intermediates get dropped.
- `human` and `system_prompt` are never dropped.
- The most recent `compaction.trigger.min_turns_kept` turns are protected
  outright.

### `bulk`

```yaml
- bulk:
    enabled: false
    strategy: hybrid  # trim | summarize | hybrid
    summarize:
      model: ${COMPACTION_MODEL}
      max_summary_tokens: 1500
```

Brings session size under target by acting on whole turns:

- **trim** — drop oldest turns until under target. Cheapest, lossy.
- **summarize** — drop oldest turns *and* LLM-summarise their content into a
  single SystemMessage that the model retains.
- **hybrid** — keep the most recent `min_turns_kept` turns verbatim,
  summarise everything older.

**Auto-fallback**: if `summarize` or `hybrid` is configured but no LLM is
reachable for compaction (`tctx.llm is None`), the stage transparently
falls back to `trim` and records `notes.fallback`. The orchestrator
currently uses the active provider's model for compaction; a future
enhancement will honour `bulk.summarize.model` to route summarisation to a
cheaper / longer-context model.

## Slash commands

- `/compaction` — show pipeline state, current/peak utilisation, the
  current compaction-model override, and the last pass breakdown.
- `/compaction model` — interactive picker for the LLM that runs
  LLM-backed compaction. Lists every provider whose API key validates,
  plus a "use chat model (default)" entry. Persists to
  `~/.vibe-cli/preferences.json`.
  - `/compaction model none` clears the override.
  - `/compaction model <provider> <model>` sets non-interactively.
- `/compact` — force a threshold pass right now (bypasses the threshold
  check; still respects `min_turns_kept`). Honours the same
  compaction-model override.

## UI: 80% context warning

When session size crosses `compaction.trigger.threshold` (default 0.80)
the UI surfaces it in two places:

- **One-shot inline banner** above the prompt the first time it crosses
  per session. Loud yellow when no compaction stages are enabled
  ("compaction is disabled — the next turn may exceed the model's
  window"); informational when at least one stage is enabled
  ("threshold compaction will run at 80%").
- **Persistent toolbar marker** `ctx:NN%⚠` once over the threshold,
  visible until a compaction frees budget.

Both reset on `/clear`, Ctrl+L, and after any `KIND_COMPACTION` event.
With `compaction.trigger.threshold: 0` (or unset) both silently no-op.

## Future work

- **Embedding backend for dedupe** — pluggable embedder for cross-language
  / paraphrase dedup.
- **`tool_summarize` background pool budget** — currently each oversized
  tool result spawns its own task. With many concurrent tools we may want
  to cap concurrency or share a single worker.
- **Per-stage compaction model** — promote `tool_summarize.model` and
  `bulk.summarize.model` from informational to functional so different
  stages can use different models. Today the resolver returns one LLM
  for both.
- **Compression of artifact payloads** — gzip large stored bodies to keep
  the artifacts directory small.
- **Cross-session deduplication** — a global content-addressed pool with
  per-session reference counts. Skipped for now to keep deletion simple.
- **`/compare` command** — diff a live session against its latest
  `session_backups/` snapshot to visualise what compaction removed.
