"""
vibe-cli — Provider and API key management.

Handles API key validation, persistence, and the interactive
startup flow for selecting a provider and entering credentials.

Provider validation uses lightweight metadata endpoints (no LLM calls):
  - Ollama:  GET /api/tags
  - OpenAI:  client.models.list()
  - Claude:  client.models.list()
  - Gemini:  client.models.list(page_size=1)

Startup flow:
  1. Load saved keys from ~/.vibe-cli/preferences.json
  2. Validate each provider
  3. Invalid keys are removed from preferences automatically
  4. If the preferred provider is unavailable, fall back to
     the first available provider
  5. If no valid providers exist, prompt interactively (with validation)
"""

from __future__ import annotations

import asyncio
import os
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.patch_stdout import patch_stdout

from src.main import provider_configs, providers as PROVIDERS
from src.providers import PROVIDER_REGISTRY
from ui import preferences
from ui.ui import console, print_system


# ---------------------------------------------------------
# Provider ↔ env-var mapping
# ---------------------------------------------------------
# Maps each cloud provider to its *preferred* env var name (first entry
# in the provider's `env_var` list). The full resolution order — including
# fallbacks like AWS_BEARER_TOKEN_BEDROCK — lives on the provider itself
# (`provider.env_vars()` / `provider.resolve_env_key()`). Use ENV_KEY_MAP
# when you need a single name to write to (set_api_key) or display.
ENV_KEY_MAP = {
    name: provider.preferred_env_var()
    for name, provider in PROVIDER_REGISTRY.items()
    if provider.preferred_env_var()
}


def _capability_marker(provider: str, model: str) -> str:
    """Suffix shown next to a model in pickers. Marks only models we've
    *recorded* as not supporting a kind (red ✕). Optimistic default
    means most rows stay clean."""
    try:
        from src import capabilities as _caps

        caps = _caps.get_capabilities(provider, model)
        bad = []
        if caps.recorded("image") == "no":
            bad.append("no-image")
        if caps.recorded("pdf") == "no":
            bad.append("no-pdf")
        if not bad:
            return ""
        return f" [warning]✕ {', '.join(bad)}[/warning]"
    except Exception:
        return ""

# Full resolution order — used by headless auto-pick to scan every
# accepted env var per provider.
ENV_KEY_ALIASES = {
    name: provider.env_vars()
    for name, provider in PROVIDER_REGISTRY.items()
    if provider.env_vars()
}


def restore_api_keys() -> None:
    """Hydrate `provider_configs` with API keys from preferences + env vars.

    Mirrors the two-step restoration that the interactive and headless
    paths do at startup, but factored so non-interactive entry points
    (e.g. `vibe eval --all` discovery) can run it before they call
    `provider.validate(cfg)`. Idempotent — safe to call multiple times.

    Step 1: copy keys from `~/.vibe-cli/preferences.json` (the canonical
    persistence) into both `provider_configs[prov]["api_key"]` and the
    matching env vars (so libs like boto3 / openai pick them up too).

    Step 2: env-var fallback — for any provider that still doesn't have
    a key, walk its accepted env-var aliases (e.g. Bedrock honors both
    BEDROCK_API_KEY and AWS_BEARER_TOKEN_BEDROCK) and copy the first
    non-empty one into the config and the preferred env var.

    Does NOT validate or remove invalid keys — callers do that themselves
    via `provider.validate(cfg)`.
    """
    preferences.load()

    for prov in PROVIDERS:
        if not provider_requires_api_key(prov):
            continue
        saved_key = preferences.get_api_key(prov)
        if saved_key and not provider_has_key(prov):
            provider_configs[prov]["api_key"] = saved_key
            for env_var in ENV_KEY_ALIASES.get(prov, []):
                os.environ[env_var] = saved_key

    for prov in PROVIDERS:
        if not provider_requires_api_key(prov):
            continue
        if provider_has_key(prov):
            continue
        provider_impl = PROVIDER_REGISTRY.get(prov)
        if provider_impl is None:
            continue
        env_key = provider_impl.resolve_env_key()
        if env_key:
            provider_configs[prov]["api_key"] = env_key
            for env_var in ENV_KEY_ALIASES.get(prov, []):
                os.environ[env_var] = env_key


def provider_requires_api_key(provider: str) -> bool:
    """Return True when a provider is backed by a remote API key."""
    provider_impl = PROVIDER_REGISTRY.get(provider)
    return provider_impl.requires_api_key() if provider_impl else True


def provider_base_url(provider: str) -> str:
    """Return the configured local provider base URL."""
    provider_impl = PROVIDER_REGISTRY.get(provider)
    cfg = provider_configs.get(provider, {})
    if provider_impl:
        return provider_impl.base_url(cfg) or ""
    return str(cfg.get("base_url", "")).rstrip("/")


# ---------------------------------------------------------
# Key queries
# ---------------------------------------------------------
def provider_has_key(provider: str) -> bool:
    """Return True if the provider has a non-empty API key in the runtime config."""
    if not provider_requires_api_key(provider):
        return False
    return bool(provider_configs.get(provider, {}).get("api_key"))


# ---------------------------------------------------------
# Key validation (no LLM calls — models-list only)
# ---------------------------------------------------------
def validate_api_key(provider: str) -> bool:
    """Check if a provider is usable without making an LLM call."""
    provider_impl = PROVIDER_REGISTRY.get(provider)
    if not provider_impl:
        return False

    cfg = provider_configs.get(provider, {})
    return provider_impl.validate(cfg)


# ---------------------------------------------------------
# Key mutation
# ---------------------------------------------------------
def set_api_key(provider: str, api_key: str):
    """Set an API key in the runtime config, env, and preferences.

    Exports the key to every accepted env var alias for the provider so
    downstream consumers (e.g. boto3 which reads AWS_BEARER_TOKEN_BEDROCK
    natively) see it regardless of which alias they prefer.
    """
    provider_configs[provider]["api_key"] = api_key
    for env_var in ENV_KEY_ALIASES.get(provider, []):
        os.environ[env_var] = api_key
    preferences.set_api_key(provider, api_key)


def clear_api_key(provider: str):
    """Remove an API key from the runtime config, env, and preferences."""
    provider_configs[provider]["api_key"] = ""
    for env_var in ENV_KEY_ALIASES.get(provider, []):
        if env_var in os.environ:
            del os.environ[env_var]
    preferences.remove_api_key(provider)


# ---------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------
async def prompt_api_key(session: PromptSession, provider: str) -> str | None:
    """Prompt the user to paste an API key. Returns the key or None on cancel."""
    prompt_label = HTML(
        f'<style fg="#a78bfa"><b>  API key for {provider} ❯ </b></style>'
    )
    # Use a separate session so is_password=True doesn't stick on the main one.
    pw_session = PromptSession()
    try:
        with patch_stdout():
            key = await pw_session.prompt_async(
                prompt_label,
                placeholder=HTML('<style fg="#52525b">paste your key here…</style>'),
                is_password=True,
            )
        return key.strip() if key.strip() else None
    except (EOFError, KeyboardInterrupt):
        return None


async def prompt_provider_and_key(session: PromptSession) -> tuple[str, str]:
    """Interactive flow: pick a provider/model and configure access if needed."""
    console.print()
    console.print("[bold]No valid providers found.[/bold]")
    console.print(
        "[dim]Select a provider. Cloud providers need an API key; "
        "Ollama needs a running local server.[/dim]"
    )
    console.print()

    while True:
        for i, prov in enumerate(PROVIDERS, 1):
            models = provider_configs.get(prov, {}).get("models", [])
            model_list = ", ".join(models[:3])
            console.print(
                f"  [accent]{i}[/accent]. [bold]{prov}[/bold]  [dim]({model_list})[/dim]"
            )
        console.print()

        provider_prompt = HTML(
            '<style fg="#a78bfa"><b>  Select provider (number) ❯ </b></style>'
        )
        chosen_provider = None
        while chosen_provider is None:
            try:
                with patch_stdout():
                    choice = await session.prompt_async(provider_prompt)
                idx = int(choice.strip()) - 1
                if 0 <= idx < len(PROVIDERS):
                    chosen_provider = PROVIDERS[idx]
                else:
                    console.print(f"  [error]Choose 1–{len(PROVIDERS)}[/error]")
            except ValueError:
                console.print(f"  [error]Enter a number 1–{len(PROVIDERS)}[/error]")
            except (EOFError, KeyboardInterrupt):
                console.print("\n[muted]Goodbye.[/muted]")
                sys.exit(0)

        available_models = provider_configs.get(chosen_provider, {}).get("models", [])
        chosen_model = available_models[0] if available_models else "unknown"

        if len(available_models) > 1:
            console.print()
            for i, m in enumerate(available_models, 1):
                default_tag = " [dim](default)[/dim]" if i == 1 else ""
                console.print(
                    f"  [accent]{i}[/accent]. {m}{_capability_marker(chosen_provider, m)}"
                    f"{default_tag}"
                )
            console.print()

            model_prompt = HTML(
                '<style fg="#a78bfa"><b>  Select model (number, enter for default) ❯ </b></style>'
            )
            try:
                with patch_stdout():
                    choice = await session.prompt_async(model_prompt)
                choice = choice.strip()
                if choice:
                    idx = int(choice) - 1
                    if 0 <= idx < len(available_models):
                        chosen_model = available_models[idx]
            except (ValueError, EOFError, KeyboardInterrupt):
                pass

        if not provider_requires_api_key(chosen_provider):
            provider_configs[chosen_provider]["model"] = chosen_model
            if validate_api_key(chosen_provider):
                preferences.set_pref("default_provider", chosen_provider)
                preferences.set_selected_model(chosen_provider, chosen_model)
                return chosen_provider, chosen_model

            provider_impl = PROVIDER_REGISTRY.get(chosen_provider)
            if provider_impl:
                headline, hint = provider_impl.unreachable_help(
                    provider_configs.get(chosen_provider, {})
                )
            else:
                headline = f"Could not reach {chosen_provider}."
                hint = "Try again or choose another provider."

            console.print()
            console.print(f"  [error]{headline}[/error]")
            console.print(f"  [dim]{hint}[/dim]")
            console.print()
            continue

        console.print()
        api_key = None
        while not api_key:
            api_key = await prompt_api_key(session, chosen_provider)
            if api_key is None:
                console.print("  [error]API key is required to continue.[/error]")
                continue
            # Validate before accepting
            provider_configs[chosen_provider]["api_key"] = api_key
            if not validate_api_key(chosen_provider):
                console.print("  [error]API key is invalid. Please try again.[/error]")
                provider_configs[chosen_provider]["api_key"] = ""
                api_key = None

        set_api_key(chosen_provider, api_key)
        provider_configs[chosen_provider]["model"] = chosen_model
        preferences.set_pref("default_provider", chosen_provider)
        preferences.set_selected_model(chosen_provider, chosen_model)

        return chosen_provider, chosen_model


# ---------------------------------------------------------
# Startup: restore, validate, and select a provider
# ---------------------------------------------------------
async def pick_compaction_model(
    session: PromptSession,
    *,
    validation_cache: dict[str, bool] | None = None,
) -> tuple[str | None, str | None] | None:
    """Interactive picker for the compaction-model override.

    Lists every provider whose API key validates *plus* a "use chat
    model (default)" entry that clears the override. Returns:

    - `(provider, model)` when the user picks a specific model
    - `(None, None)` when the user picks "use chat model"
    - `None` when the user cancels (Ctrl+C / empty input)

    `validation_cache` is consulted before each `validate_api_key` call
    so we don't re-issue network calls for the same providers when this
    picker is reopened during a session. The caller is expected to
    persist the cache in `ctx`.

    The result is *not* persisted here — the caller decides whether to
    `preferences.set_compaction_model(...)`. Keeping persistence out
    keeps this function unit-testable.
    """
    if validation_cache is None:
        validation_cache = {}

    # Gather providers with valid keys / reachable local endpoints.
    available: list[tuple[str, list[str]]] = []
    for prov in PROVIDERS:
        if prov not in validation_cache:
            validation_cache[prov] = validate_api_key(prov)
        if not validation_cache[prov]:
            continue
        models = provider_configs.get(prov, {}).get("models", []) or []
        if not models:
            continue
        available.append((prov, list(models)))

    if not available:
        console.print()
        console.print(
            "[warning]No providers with valid API keys.[/warning] "
            "[dim]Run [accent]/model[/accent] to log in first.[/dim]"
        )
        console.print()
        return None

    # Build a flat numbered list: 0 = use chat model, then provider/model rows.
    console.print()
    console.print("[bold]Compaction model[/bold]")
    console.print(
        "[dim]Pick the model that runs LLM-backed compaction "
        "(bulk summarize/hybrid + tool_summarize).[/dim]"
    )
    console.print()
    console.print("  [accent]0[/accent]. [dim]use chat model (default)[/dim]")

    flat: list[tuple[str, str]] = []
    idx = 1
    for prov, models in available:
        for m in models:
            console.print(f"  [accent]{idx}[/accent]. [bold]{prov}[/bold] / {m}")
            flat.append((prov, m))
            idx += 1
    console.print()

    prompt = HTML(
        '<style fg="#a78bfa"><b>  Select compaction model (number) ❯ </b></style>'
    )
    try:
        with patch_stdout():
            choice = await session.prompt_async(prompt)
    except (EOFError, KeyboardInterrupt):
        return None

    choice = (choice or "").strip()
    if not choice:
        return None
    try:
        n = int(choice)
    except ValueError:
        console.print(f"  [error]Enter a number 0–{len(flat)}[/error]")
        return None

    if n == 0:
        return (None, None)
    if 1 <= n <= len(flat):
        return flat[n - 1]
    console.print(f"  [error]Choose 0–{len(flat)}[/error]")
    return None


async def pick_subagent_model(
    session: PromptSession,
    subagent_type: str,
    *,
    validation_cache: dict[str, bool] | None = None,
) -> tuple[str | None, str | None] | None:
    """Interactive picker for a subagent's model override.

    Same shape as `pick_compaction_model`: lists every provider with valid
    creds, plus a "use chat model (default)" entry that clears the override.

    Returns:
      - `(provider, model)` when a specific model is picked
      - `(None, None)` when "use chat model" is picked
      - `None` when the user cancels (Ctrl+C / empty input)
    """
    if validation_cache is None:
        validation_cache = {}

    available: list[tuple[str, list[str]]] = []
    for prov in PROVIDERS:
        if prov not in validation_cache:
            validation_cache[prov] = validate_api_key(prov)
        if not validation_cache[prov]:
            continue
        models = provider_configs.get(prov, {}).get("models", []) or []
        if not models:
            continue
        available.append((prov, list(models)))

    if not available:
        console.print()
        console.print(
            "[warning]No providers with valid API keys.[/warning] "
            "[dim]Run [accent]/model[/accent] to log in first.[/dim]"
        )
        console.print()
        return None

    console.print()
    console.print(f"[bold]Subagent model — {subagent_type}[/bold]")
    console.print(
        "[dim]Pick the model this subagent type will use. "
        "Default falls back to the active chat model.[/dim]"
    )
    console.print()
    console.print("  [accent]0[/accent]. [dim]use chat model (default)[/dim]")

    flat: list[tuple[str, str]] = []
    idx = 1
    for prov, models in available:
        for m in models:
            console.print(f"  [accent]{idx}[/accent]. [bold]{prov}[/bold] / {m}")
            flat.append((prov, m))
            idx += 1
    console.print()

    prompt = HTML(
        f'<style fg="#a78bfa"><b>  Model for {subagent_type} (number) ❯ </b></style>'
    )
    try:
        with patch_stdout():
            choice = await session.prompt_async(prompt)
    except (EOFError, KeyboardInterrupt):
        return None

    choice = (choice or "").strip()
    if not choice:
        return None
    try:
        n = int(choice)
    except ValueError:
        console.print(f"  [error]Enter a number 0–{len(flat)}[/error]")
        return None

    if n == 0:
        return (None, None)
    if 1 <= n <= len(flat):
        return flat[n - 1]
    console.print(f"  [error]Choose 0–{len(flat)}[/error]")
    return None


async def restore_and_validate_providers() -> tuple[list[str], list[str]]:
    """Restore keys from preferences/env and validate every provider in
    parallel. Returns `(valid, invalid)`.

    Side effects: mutates `provider_configs` and `os.environ` with
    restored keys; clears invalid remote keys from preferences. Safe to
    call concurrently with the rest of UI setup — needs no PromptSession
    — so callers can kick this off as a background task and only await
    it when the result is actually needed.
    """
    preferences.load()

    # Restore saved API keys into the runtime config
    for prov in PROVIDERS:
        if not provider_requires_api_key(prov):
            continue
        saved_key = preferences.get_api_key(prov)
        if saved_key and not provider_has_key(prov):
            provider_configs[prov]["api_key"] = saved_key
            for env_var in ENV_KEY_ALIASES.get(prov, []):
                os.environ[env_var] = saved_key

    # Env-var fallback: if a provider still has no key, check every
    # accepted alias on the provider (e.g. bedrock honors both
    # BEDROCK_API_KEY and AWS_BEARER_TOKEN_BEDROCK). Lets users run
    # without ever touching preferences.json or config.yml.
    for prov in PROVIDERS:
        if not provider_requires_api_key(prov):
            continue
        if provider_has_key(prov):
            continue
        provider_impl = PROVIDER_REGISTRY.get(prov)
        if provider_impl is None:
            continue
        env_key = provider_impl.resolve_env_key()
        if env_key:
            provider_configs[prov]["api_key"] = env_key
            for env_var in ENV_KEY_ALIASES.get(prov, []):
                os.environ[env_var] = env_key

    # Validate all available providers in parallel and purge invalid
    # remote keys. Each provider's validate() makes a remote metadata
    # call; running them serially dominated startup latency (≈ sum of
    # all providers). asyncio.gather + to_thread runs them concurrently
    # so startup waits only for the slowest one. A per-provider timeout
    # caps the worst case — Bedrock control-plane / Anthropic models.list
    # can hang for tens of seconds on a stale token or flaky network,
    # which would otherwise block the prompt from ever appearing.
    candidates = [
        prov
        for prov in PROVIDERS
        if not (provider_requires_api_key(prov) and not provider_has_key(prov))
    ]

    async def _validate_with_timeout(prov: str) -> bool:
        impl = PROVIDER_REGISTRY.get(prov)
        timeout = getattr(impl, "validation_timeout", 5.0) if impl else 5.0
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(validate_api_key, prov),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return False
        except Exception:
            return False

    validity = await asyncio.gather(
        *(_validate_with_timeout(p) for p in candidates)
    )

    invalid_providers: list[str] = []
    valid_providers: list[str] = []
    for prov, ok in zip(candidates, validity):
        if ok:
            valid_providers.append(prov)
        elif provider_requires_api_key(prov):
            invalid_providers.append(prov)
            clear_api_key(prov)

    return valid_providers, invalid_providers


async def startup_provider_setup(
    session: PromptSession,
    *,
    prevalidated: tuple[list[str], list[str]] | None = None,
) -> tuple[str, str]:
    """
    Restore saved keys, validate providers, and return (provider, model).

    1. Load saved API keys from preferences into the runtime config.
    2. Validate each provider via a lightweight metadata endpoint.
    3. Remove invalid keys from preferences and report them.
    4. Return the preferred provider if valid, otherwise fall back
       to the first valid provider, otherwise prompt interactively.

    `prevalidated` lets the caller pre-run the validation phase as a
    background task (in parallel with the rest of startup) and pass the
    result in here. When omitted, validation runs inline.
    """
    if prevalidated is None:
        valid_providers, invalid_providers = await restore_and_validate_providers()
    else:
        valid_providers, invalid_providers = prevalidated

    for prov in invalid_providers:
        print_system(
            f"[red]●[/red] API key for [bold]{prov}[/bold] is invalid — removed from preferences."
        )

    default = preferences.get_default_provider(provider_configs)

    # 1. Preferred provider has a valid key?
    if default and default in valid_providers:
        model = preferences.get_selected_model(default, provider_configs)
        provider_configs[default]["model"] = model
        return default, model

    # 2. Any provider with a valid key?
    if valid_providers:
        fallback = valid_providers[0]
        model = preferences.get_selected_model(fallback, provider_configs)
        provider_configs[fallback]["model"] = model
        if default and default not in valid_providers:
            provider_impl = PROVIDER_REGISTRY.get(default)
            reason = (
                provider_impl.unavailable_reason()
                if provider_impl
                else "is unavailable"
            )
            print_system(
                f"Preferred provider [bold]{default}[/bold] {reason}. "
                f"Falling back to [bold]{fallback}[/bold]."
            )
        return fallback, model

    # 3. No valid keys — interactive setup
    chosen_provider, chosen_model = await prompt_provider_and_key(session)

    console.print()
    print_system(
        f"[green]●[/green] Configured [bold]{chosen_provider}[/bold] / "
        f"[accent]{chosen_model}[/accent]"
    )

    return chosen_provider, chosen_model
