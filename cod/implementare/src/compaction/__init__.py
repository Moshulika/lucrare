"""
vibe-cli — Compaction pipeline.

A *pipeline* is an ordered list of stages that transform the projected view
of a conversation. Each stage operates either:

  * **eager** — runs immediately when a new event is appended to the session
    log (e.g. a tool result lands and we immediately truncate it).
  * **threshold** — runs only when context utilisation crosses a configured
    threshold; mutates whole turns at once and may issue an LLM call.

By design every stage is **disabled by default**. Stages are flipped on one
at a time via `config.yml` so their effects can be A/B compared against the
pre-compaction snapshot in `~/.vibe-cli/session_backups/`.

Public entry points:

    build_eager_pipeline(config)        -> list[Stage]
    run_eager_pipeline(event, ...)      -> bool

    build_threshold_pipeline(config)    -> list[ThresholdStage]
    run_threshold_pass(session, ...)    -> ThresholdPassReport (async)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from src.artifacts import ArtifactStore
from src.compaction.base import (
    CompactionConfigError,
    EagerContext,
    Stage,
    ThresholdContext,
    ThresholdResult,
    ThresholdStage,
)
from src.logging_setup import emit_event, get_logger

_log = get_logger("compaction")
from src.compaction.stages.bulk import Bulk
from src.compaction.stages.dedupe import Dedupe
from src.compaction.stages.format_compress import FormatCompress
from src.compaction.stages.importance_prune import ImportancePrune
from src.compaction.stages.ref_substitute import RefSubstitute
from src.compaction.stages.tool_summarize import ToolSummarize
from src.compaction.stages.tool_truncate import ToolTruncate

EAGER_REGISTRY: dict[str, type[Stage]] = {
    "tool_truncate": ToolTruncate,
    "tool_summarize": ToolSummarize,
    "format_compress": FormatCompress,
    "ref_substitute": RefSubstitute,
}

THRESHOLD_REGISTRY: dict[str, type[ThresholdStage]] = {
    "dedupe": Dedupe,
    "importance_prune": ImportancePrune,
    "bulk": Bulk,
}

# Eager stages that fight over the same event slot (the tool_result body).
# Enabling more than one of these at once is meaningless — the later one
# would just clobber the earlier one's `projected` field — so we hard-fail
# at startup and ask the user to pick one. This is the *only* mutex
# enforced today; document any new ones below.
EAGER_EXCLUSION_GROUPS: dict[str, set[str]] = {
    "tool_result_rewrite": {"tool_truncate", "tool_summarize", "ref_substitute"},
}

# Threshold-side overlaps that aren't strictly broken but are usually
# redundant. The pipeline keeps running; the orchestrator just emits a
# warning event the first time both run together so the user knows.
THRESHOLD_SOFT_OVERLAPS: dict[tuple[str, str], str] = {
    ("bulk", "importance_prune"): (
        "both drop old turns; bulk acts first and importance_prune may "
        "find nothing left to remove. Usually you want one or the other."
    ),
}


def build_eager_pipeline(compaction_config: dict | None) -> list[Stage]:
    """Build the ordered list of *enabled* eager stages from config.

    Raises `CompactionConfigError` if the configuration violates an
    exclusion group (see `EAGER_EXCLUSION_GROUPS`). The error message names
    the offending stages and the group they conflict in so the user can
    fix `config.yml` without hunting.
    """
    if not compaction_config:
        return []
    pipeline_cfg = compaction_config.get("pipeline") or []
    out: list[Stage] = []
    enabled_names: list[str] = []
    for entry in pipeline_cfg:
        if not isinstance(entry, dict) or not entry:
            continue
        name, stage_cfg = next(iter(entry.items()))
        if name in THRESHOLD_REGISTRY:
            continue  # belongs to the threshold pipeline
        cls = EAGER_REGISTRY.get(name)
        if cls is None:
            continue
        stage = cls(stage_cfg or {})
        if stage.enabled:
            out.append(stage)
            enabled_names.append(name)

    _validate_eager_exclusions(enabled_names)
    return out


def _validate_eager_exclusions(enabled_names: list[str]) -> None:
    """Hard-fail if more than one stage from the same exclusion group is on.

    The pipeline applies stages in order, each writing to `event["projected"]`.
    When two stages claim the same event-slot, the later one silently wins
    — a footgun that's almost always a config mistake. Instead of letting
    that happen, surface it loudly at startup with a pointer to the group.
    """
    enabled = set(enabled_names)
    for group_name, members in EAGER_EXCLUSION_GROUPS.items():
        clashes = sorted(enabled & members)
        if len(clashes) > 1:
            raise CompactionConfigError(
                f"Compaction config error: stages {clashes} are mutually "
                f"exclusive (group '{group_name}'). They all rewrite the "
                f"same event-slot, so enabling more than one is meaningless. "
                f"Pick one (or none) in config.yml under "
                f"compaction.pipeline."
            )


def build_threshold_pipeline(
    compaction_config: dict | None,
) -> list[ThresholdStage]:
    """Build the ordered list of *enabled* threshold stages from config.

    Emits a warning event for soft overlaps (see `THRESHOLD_SOFT_OVERLAPS`)
    but never raises — these combinations work, they're just redundant.
    """
    if not compaction_config:
        return []
    pipeline_cfg = compaction_config.get("pipeline") or []
    out: list[ThresholdStage] = []
    enabled_names: list[str] = []
    for entry in pipeline_cfg:
        if not isinstance(entry, dict) or not entry:
            continue
        name, stage_cfg = next(iter(entry.items()))
        if name in EAGER_REGISTRY:
            continue
        cls = THRESHOLD_REGISTRY.get(name)
        if cls is None:
            continue
        stage = cls(stage_cfg or {})
        if stage.enabled:
            out.append(stage)
            enabled_names.append(name)

    enabled_set = set(enabled_names)
    for (a, b), reason in THRESHOLD_SOFT_OVERLAPS.items():
        if a in enabled_set and b in enabled_set:
            emit_event(
                _log,
                "compaction.threshold_overlap",
                level=logging.WARNING,
                stages=[a, b],
                reason=reason,
            )
    return out


async def run_eager_pipeline(
    event: dict,
    pipeline: Iterable[Stage],
    store: ArtifactStore,
    *,
    session_id: str | None = None,
    llm: Any | None = None,
    on_event_updated: "Callable[[dict], None] | None" = None,
) -> tuple[bool, list]:
    """Run all enabled eager stages on a freshly-appended event.

    Returns
    -------
    (modified, background_tasks)
        `modified` is True if any stage altered `event` synchronously.
        `background_tasks` is the list of `asyncio.Task`s spawned by
        stages that opted to do async work (e.g. `tool_summarize`'s LLM
        upgrade). The caller is responsible for draining these at turn
        boundary so the next prompt sees the patched events.
    """
    ectx = EagerContext(
        event=event,
        store=store,
        session_id=session_id,
        llm=llm,
        on_event_updated=on_event_updated,
    )
    modified = False
    for stage in pipeline:
        if await stage.apply_eager_async(ectx):
            modified = True
            emit_event(
                _log,
                "compaction.eager",
                level=logging.DEBUG,
                stage=getattr(stage, "name", stage.__class__.__name__),
                event_kind=event.get("kind"),
                event_id=event.get("id"),
            )
    return modified, ectx.background_tasks


# ---------------------------------------------------------
# Threshold pass orchestrator
# ---------------------------------------------------------
@dataclass
class ThresholdPassReport:
    """Outcome of a single threshold-mode pass."""

    ran: bool = False
    backup_path: Path | None = None
    stages: list[dict] = field(default_factory=list)
    removed_event_ids: list[str] = field(default_factory=list)
    summary: str = ""
    before_size: int = 0
    after_size: int = 0
    freed: int = 0


def threshold_should_fire(
    session,
    compaction_config: dict | None,
) -> bool:
    """Decide whether the threshold pass should run after a turn."""
    if not compaction_config:
        return False
    threshold = float(
        (compaction_config.get("trigger") or {}).get("threshold") or 0.0
    )
    if threshold <= 0:
        return False
    limit = int(session.metrics["context"].get("limit") or 0)
    if limit <= 0:
        return False
    cur = int(session.metrics["context"].get("current_size") or 0)
    return cur / limit >= threshold


async def run_threshold_pass(
    session,
    *,
    pipeline: list[ThresholdStage],
    store: ArtifactStore,
    compaction_config: dict | None,
    llm=None,
    force: bool = False,
) -> ThresholdPassReport:
    """Take a backup, run threshold stages in order, record one compaction event.

    The session's `to_messages()` projection (which honours removed_event_ids)
    becomes the new live message stream after this returns. Caller is
    responsible for replacing the in-memory `messages` list.
    """
    report = ThresholdPassReport()
    if not pipeline:
        return report

    storage_cfg = (compaction_config or {}).get("storage") or {}
    trigger_cfg = (compaction_config or {}).get("trigger") or {}
    target_ratio = float(trigger_cfg.get("threshold") or 0.8) - 0.1
    min_turns_kept = int(trigger_cfg.get("min_turns_kept") or 4)

    limit = int(session.metrics["context"].get("limit") or 0)
    cur = int(session.metrics["context"].get("current_size") or 0)
    target = int(limit * target_ratio) if limit > 0 else cur

    if not force and limit > 0:
        if cur / limit < float(trigger_cfg.get("threshold") or 0.8):
            return report

    pass_t0 = time.perf_counter()
    emit_event(
        _log,
        "compaction.threshold_start",
        before_size=cur,
        limit=limit,
        target_size=target,
        stage_count=len(pipeline),
        forced=bool(force),
    )

    # Snapshot first — never lose pre-compaction state.
    if storage_cfg.get("backup", True):
        keep = storage_cfg.get("keep_backups")
        report.backup_path = session.snapshot_backup(
            keep=int(keep) if keep else None
        )

    tctx = ThresholdContext(
        events=session.events,
        current_size=cur,
        target_size=target,
        min_turns_kept=min_turns_kept,
        store=store,
        llm=llm,
    )

    aggregated_removed: list[str] = []
    summary_parts: list[str] = []

    for stage in pipeline:
        stage_t0 = time.perf_counter()
        try:
            result = await stage.apply_threshold(tctx)
        except Exception as exc:  # noqa: BLE001 — never let one stage abort the rest
            report.stages.append(
                {"name": stage.name, "error": str(exc), "removed_event_ids": []}
            )
            emit_event(
                _log,
                "compaction.stage",
                level=logging.WARNING,
                stage=stage.name,
                ok=False,
                error_type=type(exc).__name__,
                duration_ms=(time.perf_counter() - stage_t0) * 1000.0,
            )
            continue
        report.stages.append(
            {
                "name": stage.name,
                "removed_event_ids": list(result.removed_event_ids),
                "freed_estimate": result.freed_estimate,
                "notes": result.notes,
            }
        )
        emit_event(
            _log,
            "compaction.stage",
            stage=stage.name,
            ok=True,
            removed_count=len(result.removed_event_ids),
            freed_estimate=int(result.freed_estimate),
            duration_ms=(time.perf_counter() - stage_t0) * 1000.0,
        )
        aggregated_removed.extend(result.removed_event_ids)
        if result.summary.strip():
            summary_parts.append(f"[{stage.name}] {result.summary.strip()}")

    # Deduplicate while preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for eid in aggregated_removed:
        if eid not in seen:
            seen.add(eid)
            deduped.append(eid)

    report.removed_event_ids = deduped
    report.summary = "\n\n".join(summary_parts)
    report.before_size = cur
    report.ran = True

    # Estimate freed by summing approx tokens of removed events' (projected
    # or content) bodies — cheap heuristic; the metrics rollup will catch up
    # when the next LLM call lands.
    from src.compaction.base import approx_tokens, stringify

    by_id = {e["id"]: e for e in session.events}
    freed_est = 0
    for eid in deduped:
        evt = by_id.get(eid)
        if evt is None:
            continue
        body = evt.get("projected") or evt.get("content")
        freed_est += approx_tokens(stringify(body))

    report.after_size = max(0, cur - freed_est)
    report.freed = freed_est

    # Record one compaction event with a `stages` breakdown in meta.
    if deduped or report.summary:
        evt = session.record_compaction(
            before_size=report.before_size,
            after_size=report.after_size,
            removed_event_ids=deduped,
            summary=report.summary,
        )
        # Augment meta with per-stage breakdown + a back-pointer to the
        # pre-compaction snapshot so anyone reading the session file can
        # find the corresponding `session_backups/` artifact directly.
        evt_meta = evt.setdefault("meta", {})
        evt_meta["stages"] = report.stages
        if report.backup_path is not None:
            evt_meta["backup"] = str(report.backup_path)
        session.save()

    emit_event(
        _log,
        "compaction.threshold_end",
        before_size=report.before_size,
        after_size=report.after_size,
        freed=report.freed,
        removed_count=len(report.removed_event_ids),
        stage_count=len(report.stages),
        duration_ms=(time.perf_counter() - pass_t0) * 1000.0,
    )
    return report


__all__ = [
    "Stage",
    "ThresholdStage",
    "ThresholdContext",
    "ThresholdResult",
    "ThresholdPassReport",
    "EagerContext",
    "CompactionConfigError",
    "EAGER_REGISTRY",
    "THRESHOLD_REGISTRY",
    "EAGER_EXCLUSION_GROUPS",
    "THRESHOLD_SOFT_OVERLAPS",
    "build_eager_pipeline",
    "build_threshold_pipeline",
    "run_eager_pipeline",
    "run_threshold_pass",
    "threshold_should_fire",
]
