"""
vibe-cli — User preferences persistence.

Stores user settings in ~/.vibe-cli/preferences.json,
including API keys for configured providers.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

PREFS_DIR = Path.home() / ".vibe-cli"
PREFS_PATH = PREFS_DIR / "preferences.json"

_defaults: dict = {
    "default_provider": None,
    "models": {},
    "effort": "medium",
    "api_keys": {},
    "mcp_servers": {},
    "tool_permissions": {
        "always_allow": [
            "read_file",
            "list_directory",
            "web_search",
            "run_skill",
            "remember",
            "spawn_subagent",
        ],
        "always_deny": [],
    },
    # Optional override for the model used by compaction stages
    # (`bulk` summarize/hybrid, `tool_summarize`). When None, the resolver
    # falls back to the user's active chat model. Set via the
    # `/compaction model` slash command. Schema:
    #   {"provider": "<name>", "model": "<model-id>"} or null
    "compaction_model": None,
    # Per-subagent model overrides. Maps subagent_type -> {provider, model}.
    # Missing key (or null) means "use the active chat model". Set via the
    # `/subagents` slash command. Schema:
    #   {"<subagent_type>": {"provider": "<name>", "model": "<model-id>"}, ...}
    "subagent_models": {},
    # Per-model multimodal capability state, learned at runtime by
    # observing provider rejections. We assume every model supports every
    # kind until a provider tells us otherwise — at which point we record
    # `"no"` here and pre-gate future attaches. Schema:
    #   {"<provider>": {"<model>": {"image": "yes"|"no", "pdf": "yes"|"no",
    #                                "_recorded_at": "<iso8601>"}}}
    "model_capabilities": {},
    # Tracing settings — preferred storage for LangFuse credentials so
    # they never end up in committed config.yml. Set via `/tracing setup`
    # (or `/tracing on` / `/tracing off` to flip enabled). Each field
    # takes precedence over the corresponding `tracing.<field>` in
    # config.yml at startup. Missing keys fall back to config / env.
    # Schema:
    #   {
    #     "enabled":     bool | null,        # null ⇒ defer to config
    #     "project":     str | null,
    #     "public_key":  str | null,
    #     "secret_key":  str | null,
    #     "host":        str | null,
    #   }
    "tracing": {},
}

_prefs: dict = {}


def load() -> dict:
    """Load preferences from disk, merging with defaults."""
    global _prefs
    _prefs = deepcopy(_defaults)

    if PREFS_PATH.exists():
        try:
            stored = json.loads(PREFS_PATH.read_text())
            _prefs.update(stored)
        except (json.JSONDecodeError, OSError):
            pass

    return _prefs


def save():
    """Write current preferences to disk."""
    PREFS_DIR.mkdir(parents=True, exist_ok=True)
    PREFS_PATH.write_text(json.dumps(_prefs, indent=2) + "\n")


def get(key: str, default=None):
    """Get a preference value."""
    if not _prefs:
        load()
    return _prefs.get(key, default)


def set_pref(key: str, value):
    """Set a preference value and save to disk."""
    if not _prefs:
        load()
    _prefs[key] = value
    save()


def get_selected_model(provider: str, provider_configs: dict) -> str:
    """
    Get the selected model for a provider.

    Priority:
    1. preferences.json models.<provider>
    2. First model in config.yml `providers.<provider>.models` list

    `provider_configs` is the flat `{name: {...}}` map exposed by
    `src.main.provider_configs` (i.e. `config["providers"]`).
    """
    if not _prefs:
        load()

    saved = _prefs.get("models", {}).get(provider)
    available = provider_configs.get(provider, {}).get("models", [])

    # Validate saved model still exists in config
    if saved and saved in available:
        return saved

    # Fall back to first in list
    return available[0] if available else "unknown"


def set_selected_model(provider: str, model: str):
    """Save the selected model for a provider."""
    if not _prefs:
        load()
    if "models" not in _prefs:
        _prefs["models"] = {}
    _prefs["models"][provider] = model
    save()


def get_api_key(provider: str) -> str | None:
    """Get a saved API key for a provider."""
    if not _prefs:
        load()
    return _prefs.get("api_keys", {}).get(provider)


def set_api_key(provider: str, api_key: str):
    """Save an API key for a provider."""
    if not _prefs:
        load()
    if "api_keys" not in _prefs:
        _prefs["api_keys"] = {}
    _prefs["api_keys"][provider] = api_key
    save()


def remove_api_key(provider: str):
    """Remove a saved API key for a provider."""
    if not _prefs:
        load()
    if "api_keys" in _prefs and provider in _prefs["api_keys"]:
        del _prefs["api_keys"][provider]
        save()


def _tool_permissions() -> dict:
    if not _prefs:
        load()
    if "tool_permissions" not in _prefs:
        _prefs["tool_permissions"] = {"always_allow": [], "always_deny": []}
    tp = _prefs["tool_permissions"]
    tp.setdefault("always_allow", [])
    tp.setdefault("always_deny", [])
    # One-shot migration: skills are read-only and the per-call prompt is
    # noisy, so auto-allow `run_skill` for any preferences file that
    # predates the skills feature.
    if "run_skill" not in tp["always_allow"] and "run_skill" not in tp["always_deny"]:
        tp["always_allow"].append("run_skill")
        save()
    # One-shot migration: spawn_subagent is permission-allowed by default
    # (children inherit the parent's sandboxing/permission ctx, so the
    # extra per-spawn prompt would be noisy without adding safety).
    if (
        "spawn_subagent" not in tp["always_allow"]
        and "spawn_subagent" not in tp["always_deny"]
    ):
        tp["always_allow"].append("spawn_subagent")
        save()
    # One-shot migration: `remember` only writes to capped VIBE.md files;
    # gating it behind a per-call prompt would be noisy without benefit.
    if "remember" not in tp["always_allow"] and "remember" not in tp["always_deny"]:
        tp["always_allow"].append("remember")
        save()
    return tp


def get_always_allow() -> list[str]:
    return list(_tool_permissions().get("always_allow", []))


def get_always_deny() -> list[str]:
    return list(_tool_permissions().get("always_deny", []))


def add_always_allow(tool_name: str):
    tp = _tool_permissions()
    if tool_name not in tp["always_allow"]:
        tp["always_allow"].append(tool_name)
    if tool_name in tp["always_deny"]:
        tp["always_deny"].remove(tool_name)
    save()


def add_always_deny(tool_name: str):
    tp = _tool_permissions()
    if tool_name not in tp["always_deny"]:
        tp["always_deny"].append(tool_name)
    if tool_name in tp["always_allow"]:
        tp["always_allow"].remove(tool_name)
    save()


def remove_permission(tool_name: str):
    tp = _tool_permissions()
    if tool_name in tp["always_allow"]:
        tp["always_allow"].remove(tool_name)
    if tool_name in tp["always_deny"]:
        tp["always_deny"].remove(tool_name)
    save()


def get_mcp_servers() -> dict:
    if not _prefs:
        load()
    return dict(_prefs.get("mcp_servers", {}))


def set_mcp_server(name: str, server_config: dict):
    if not _prefs:
        load()
    _prefs.setdefault("mcp_servers", {})[name] = server_config
    save()


def remove_mcp_server(name: str):
    if not _prefs:
        load()
    if "mcp_servers" in _prefs and name in _prefs["mcp_servers"]:
        del _prefs["mcp_servers"][name]
        save()


def toggle_mcp_server(name: str) -> bool | None:
    if not _prefs:
        load()
    servers = _prefs.get("mcp_servers", {})
    if name not in servers:
        return None
    servers[name]["enabled"] = not servers[name].get("enabled", True)
    save()
    return servers[name]["enabled"]


def get_compaction_model() -> dict | None:
    """Return the configured compaction-model override, or None.

    Shape: `{"provider": str, "model": str}`. None means "use the active
    chat model" — the historical behaviour. The resolver in `ui/app.py`
    is the single consumer.
    """
    if not _prefs:
        load()
    val = _prefs.get("compaction_model")
    if not isinstance(val, dict):
        return None
    prov = val.get("provider")
    model = val.get("model")
    if not prov or not model:
        return None
    return {"provider": str(prov), "model": str(model)}


def set_compaction_model(provider: str | None, model: str | None) -> None:
    """Persist the compaction-model override.

    Pass `None, None` (or both falsy) to clear the override and fall
    back to the active chat model.
    """
    if not _prefs:
        load()
    if not provider or not model:
        _prefs["compaction_model"] = None
    else:
        _prefs["compaction_model"] = {
            "provider": str(provider),
            "model": str(model),
        }
    save()


def get_subagent_model(subagent_type: str) -> dict | None:
    """Return the configured model override for a subagent type, or None.

    Shape: `{"provider": str, "model": str}`. None means "use the active
    chat model" — the default behaviour.
    """
    if not _prefs:
        load()
    raw = _prefs.get("subagent_models") or {}
    if not isinstance(raw, dict):
        return None
    val = raw.get(subagent_type)
    if not isinstance(val, dict):
        return None
    prov = val.get("provider")
    model = val.get("model")
    if not prov or not model:
        return None
    return {"provider": str(prov), "model": str(model)}


def set_subagent_model(
    subagent_type: str, provider: str | None, model: str | None
) -> None:
    """Persist (or clear) the model override for a subagent type.

    Pass `None, None` (or both falsy) to remove the entry and fall back
    to the active chat model.
    """
    if not _prefs:
        load()
    store = _prefs.setdefault("subagent_models", {})
    if not provider or not model:
        store.pop(subagent_type, None)
    else:
        store[subagent_type] = {"provider": str(provider), "model": str(model)}
    save()


def get_subagent_models() -> dict:
    """Return the full per-type override map (always a dict)."""
    if not _prefs:
        load()
    raw = _prefs.get("subagent_models")
    return dict(raw) if isinstance(raw, dict) else {}


def get_model_capability(provider: str, model: str, kind: str) -> str | None:
    """Return `"yes"`, `"no"`, or None (= unrecorded) for (provider, model, kind)."""
    if not _prefs:
        load()
    raw = _prefs.get("model_capabilities") or {}
    if not isinstance(raw, dict):
        return None
    prov = raw.get(provider)
    if not isinstance(prov, dict):
        return None
    entry = prov.get(model)
    if not isinstance(entry, dict):
        return None
    val = entry.get(kind)
    return val if val in ("yes", "no") else None


def set_model_capability(
    provider: str, model: str, kind: str, value: str | None
) -> None:
    """Record (or clear) a learned capability. `value` must be "yes", "no", or None."""
    if not _prefs:
        load()
    store = _prefs.setdefault("model_capabilities", {})
    if not isinstance(store, dict):
        store = {}
        _prefs["model_capabilities"] = store
    prov = store.setdefault(provider, {})
    if not isinstance(prov, dict):
        prov = {}
        store[provider] = prov
    entry = prov.setdefault(model, {})
    if not isinstance(entry, dict):
        entry = {}
        prov[model] = entry
    if value is None:
        entry.pop(kind, None)
        if not entry:
            prov.pop(model, None)
            if not prov:
                store.pop(provider, None)
    else:
        import datetime as _dt

        entry[kind] = value
        entry["_recorded_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    save()


def get_model_capabilities_all() -> dict:
    """Return the full learned-capabilities map (always a dict)."""
    if not _prefs:
        load()
    raw = _prefs.get("model_capabilities")
    return dict(raw) if isinstance(raw, dict) else {}


def clear_model_capabilities(provider: str, model: str | None = None) -> bool:
    """Remove recorded capabilities for a provider (all models if `model` is None,
    else just that one). Returns True if something was removed."""
    if not _prefs:
        load()
    store = _prefs.get("model_capabilities") or {}
    if not isinstance(store, dict) or provider not in store:
        return False
    if model is None:
        del store[provider]
        save()
        return True
    prov = store[provider]
    if not isinstance(prov, dict) or model not in prov:
        return False
    del prov[model]
    if not prov:
        del store[provider]
    save()
    return True


_TRACING_FIELDS = ("enabled", "project", "public_key", "secret_key", "host")


def get_tracing() -> dict:
    """Return the saved tracing block (always a dict; empty when unset).

    Only the five known fields are returned, so an unrelated value that
    snuck into the file can't pollute the rest of the runtime.
    """
    if not _prefs:
        load()
    raw = _prefs.get("tracing")
    if not isinstance(raw, dict):
        return {}
    return {k: raw[k] for k in _TRACING_FIELDS if k in raw}


def set_tracing(updates: dict) -> dict:
    """Merge `updates` into the saved tracing block and persist.

    Keys with value None are removed (they fall back to config / env).
    Returns the post-merge block.
    """
    if not _prefs:
        load()
    current = get_tracing()
    for k in _TRACING_FIELDS:
        if k not in updates:
            continue
        v = updates[k]
        if v is None:
            current.pop(k, None)
        else:
            current[k] = v
    _prefs["tracing"] = current
    save()
    return current


def clear_tracing() -> None:
    """Remove all tracing overrides; tracing falls back to config.yml / env."""
    if not _prefs:
        load()
    _prefs["tracing"] = {}
    save()


def get_default_provider(provider_configs: dict) -> str | None:
    """
    Get the default provider.

    Priority:
    1. preferences.json default_provider
    2. First provider in `provider_configs` that has a models list

    `provider_configs` is the flat `{name: {...}}` map exposed by
    `src.main.provider_configs` (i.e. `config["providers"]`).
    """
    if not _prefs:
        load()

    saved = _prefs.get("default_provider")
    providers = [
        k
        for k, v in provider_configs.items()
        if isinstance(v, dict) and v.get("models")
    ]

    if saved and saved in providers:
        return saved

    return providers[0] if providers else None
