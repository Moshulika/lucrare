from __future__ import annotations

import json
import os
from typing import Any
from urllib.request import Request, urlopen


ProviderConfig = dict[str, Any]


class BaseProvider:
    """Provider-specific behavior for validation and model construction.

    `env_var` is the list of environment variables that supply the API
    key, in resolution order — the first non-empty one wins. The first
    entry is the *preferred* name (used by `set_api_key` to write back
    on persist). A single string is accepted for backwards compat.
    """

    name: str
    env_var: str | list[str] | None = None
    # Soft cap on how long `validate()` may run at startup. Providers
    # whose probes touch heavier endpoints (e.g. ollama's inference path)
    # override this. The caller wraps validate() in `asyncio.wait_for`.
    validation_timeout: float = 5.0

    def env_vars(self) -> list[str]:
        if self.env_var is None:
            return []
        if isinstance(self.env_var, str):
            return [self.env_var]
        return list(self.env_var)

    def preferred_env_var(self) -> str | None:
        names = self.env_vars()
        return names[0] if names else None

    def resolve_env_key(self) -> str | None:
        """Return the first non-empty env-var value among `env_vars()`."""
        for name in self.env_vars():
            value = os.environ.get(name)
            if value:
                return value
        return None

    def requires_api_key(self) -> bool:
        return bool(self.env_vars())

    def base_url(self, cfg: ProviderConfig) -> str | None:
        return None

    def unavailable_reason(self) -> str:
        if self.requires_api_key():
            return "has no valid API key"
        return "is not reachable"

    def unreachable_help(self, cfg: ProviderConfig) -> tuple[str, str]:
        """UI copy for the no-API-key 'could not reach' branch.

        Returns (headline, hint). Only consulted for providers that don't
        use an API key (ollama, bedrock, …). Override to give better hints.
        """
        return (
            f"Could not reach {self.name}.",
            "Try again or choose another provider.",
        )

    def require_api_key(self, cfg: ProviderConfig) -> str:
        api_key = cfg.get("api_key")
        if not api_key:
            raise ValueError(
                f"API key for '{self.name}' is not set. "
                f"Please set the corresponding environment variable in config.yml."
            )
        return str(api_key)

    def validate(self, cfg: ProviderConfig) -> bool:
        raise NotImplementedError

    def create_model(self, cfg: ProviderConfig):
        raise NotImplementedError


class OpenAIProvider(BaseProvider):
    name = "openai"
    env_var = "OPENAI_API_KEY"

    def validate(self, cfg: ProviderConfig) -> bool:
        api_key = cfg.get("api_key")
        if not api_key:
            return False

        try:
            import openai

            client = openai.OpenAI(api_key=api_key)
            client.models.list()
            return True
        except Exception:
            return False

    def create_model(self, cfg: ProviderConfig):
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=cfg["model"],
            temperature=cfg["temperature"],
            api_key=self.require_api_key(cfg),
        )


class ClaudeProvider(BaseProvider):
    name = "claude"
    env_var = "ANTHROPIC_API_KEY"

    def validate(self, cfg: ProviderConfig) -> bool:
        api_key = cfg.get("api_key")
        if not api_key:
            return False

        try:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)
            client.models.list()
            return True
        except Exception:
            return False

    def create_model(self, cfg: ProviderConfig):
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=cfg["model"],
            temperature=cfg["temperature"],
            api_key=self.require_api_key(cfg),
        )


class GeminiProvider(BaseProvider):
    name = "gemini"
    env_var = "GOOGLE_API_KEY"

    def validate(self, cfg: ProviderConfig) -> bool:
        api_key = cfg.get("api_key")
        if not api_key:
            return False

        # Gemini's free tier has tight per-minute quotas (5–15 RPM
        # depending on model). When a sweep runs many cases back to
        # back, the validation `models.list()` call can be rejected
        # mid-sweep with a 429 — surfacing as "API key failed
        # validation" even though the key is fine. Retry with
        # exponential backoff so a transient quota hit doesn't tank
        # every subsequent case.
        try:
            import google.genai
        except Exception:
            return False

        client = google.genai.Client(api_key=api_key)
        delays = (0.0, 2.0, 6.0)
        last_exc: Exception | None = None
        for delay in delays:
            if delay:
                import time

                time.sleep(delay)
            try:
                list(client.models.list(config={"page_size": 1}))
                return True
            except Exception as exc:
                last_exc = exc
                msg = str(exc).lower()
                # Only retry on quota / rate-limit signals — auth errors
                # are terminal and shouldn't waste 8s on backoff.
                if not any(s in msg for s in ("429", "quota", "rate", "exhausted")):
                    return False
        _ = last_exc  # last_exc carried for future logging
        return False

    def create_model(self, cfg: ProviderConfig):
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=cfg["model"],
            temperature=cfg["temperature"],
            google_api_key=self.require_api_key(cfg),
        )


class OllamaProvider(BaseProvider):
    name = "ollama"
    default_base_url = "http://localhost:11434"
    base_url_env_var = "OLLAMA_URL"
    # Layer 2 inference probe can take a few seconds on first model load
    # (especially right after `ollama serve` starts). 10s leaves room
    # without blocking startup indefinitely.
    validation_timeout = 10.0

    def base_url(self, cfg: ProviderConfig) -> str:
        # cfg["base_url"] wins (it's set explicitly from config.yml or by a
        # CLI override). Otherwise fall back to OLLAMA_URL, then the
        # default localhost endpoint.
        raw = cfg.get("base_url") or os.environ.get(self.base_url_env_var) or self.default_base_url
        return str(raw).rstrip("/")

    def validate(self, cfg: ProviderConfig) -> bool:
        """Two-layer ollama health check:

        1. `/api/tags` — server is running and the configured model is
           actually pulled.
        2. `/api/chat` with a 1-char user message and `num_predict=1,
           keep_alive=0` — actually exercises the inference path on the
           same endpoint `langchain_ollama` uses at runtime. Catches
           broken installs where the server is up and the model is
           listed but inference can't run (e.g. missing `llama-server`
           binary after a homebrew reinstall). `/api/tags` returns 200
           in that case, and `/api/generate` with an empty prompt is
           treated as a metadata-only preload that never spawns
           llama-server — both used to let startup pass and then crash
           on the first prompt.

        Failure of either layer means "ollama isn't usable right now" —
        the caller's fallback flow picks the next valid provider, or
        drops into the interactive setup screen when none are valid.
        """
        base = self.base_url(cfg)
        model = cfg.get("model")

        # Layer 1: server reachable + model present.
        try:
            with urlopen(f"{base}/api/tags", timeout=2) as resp:
                if not (200 <= resp.status < 300):
                    return False
                payload = json.loads(resp.read())
        except Exception:
            return False

        if model:
            available = {m.get("name", "") for m in payload.get("models", [])}
            # Accept both `llama3.2` and `llama3.2:latest` style references.
            if not any(
                m == model or m == f"{model}:latest" or m.startswith(f"{model}:")
                for m in available
            ):
                return False

            # Layer 2: actually spawn llama-server via the same endpoint
            # the runtime uses. A 1-char user message + num_predict=1
            # forces real inference (an empty prompt is treated as a
            # metadata-only preload by ollama and won't spawn the binary);
            # keep_alive=0 unloads immediately so we don't burn VRAM at
            # startup. Broken installs return 500 here with the
            # `llama-server binary not found` error — we treat any
            # non-2xx (or transport error) as "not usable" and let the
            # caller fall back.
            try:
                req = Request(
                    f"{base}/api/chat",
                    data=json.dumps(
                        {
                            "model": model,
                            "messages": [{"role": "user", "content": "."}],
                            "stream": False,
                            "keep_alive": 0,
                            "options": {"num_predict": 1},
                        }
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(req, timeout=8) as resp:
                    if not (200 <= resp.status < 300):
                        return False
            except Exception:
                return False

        return True

    def create_model(self, cfg: ProviderConfig):
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=cfg["model"],
            temperature=cfg["temperature"],
            base_url=self.base_url(cfg),
        )

    def unreachable_help(self, cfg: ProviderConfig) -> tuple[str, str]:
        return (
            f"Could not reach ollama at {self.base_url(cfg)}.",
            "Start it with `ollama serve`, then try again or choose another provider.",
        )


class BedrockProvider(BaseProvider):
    """Anthropic Claude (and other Bedrock-hosted models) via Amazon Bedrock.

    Auth uses Bedrock API keys (a.k.a. bearer tokens), *not* SigV4 / IAM
    sessions. From the user's perspective the flow is identical to every
    other cloud provider:

      - The token lives in `providers.bedrock.api_key`, persisted to
        `~/.vibe-cli/preferences.json` under `api_keys.bedrock`, and
        re-exported into `os.environ["AWS_BEARER_TOKEN_BEDROCK"]` at
        startup (via the standard `ENV_KEY_MAP` flow in
        `ui/provider_handler.py:set_api_key`).
      - boto3 reads `AWS_BEARER_TOKEN_BEDROCK` directly when the
        `bedrock-runtime` client is constructed.
      - `region` lives alongside in `providers.bedrock.region` and falls
        back to `AWS_REGION` / `AWS_DEFAULT_REGION` then `us-east-1`.

    Note: `CLAUDE_CODE_USE_BEDROCK=1` is a Claude-Code-specific opt-in
    flag (it tells the Claude Code CLI to route Anthropic-API requests
    to Bedrock). vibe-cli does **not** read it; selecting `bedrock` in
    the providers list is the equivalent opt-in here.

    cfg fields:
      - `api_key`     Bedrock API key (bearer token).
      - `region`      AWS region for Bedrock (overrides env vars).
      - `model`       Bedrock model / inference-profile id.
      - `temperature` passthrough.
    """

    name = "bedrock"
    # BEDROCK_API_KEY is the canonical per-provider name; AWS_BEARER_TOKEN_BEDROCK
    # is kept as a fallback because boto3 reads it natively and existing setups
    # may rely on it. The resolved value is re-exported to AWS_BEARER_TOKEN_BEDROCK
    # in validate()/create_model() so boto3 always sees it.
    env_var = ["BEDROCK_API_KEY", "AWS_BEARER_TOKEN_BEDROCK"]
    default_region = "us-east-1"

    def _region(self, cfg: ProviderConfig) -> str:
        return str(
            cfg.get("region")
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or self.default_region
        )

    def validate(self, cfg: ProviderConfig) -> bool:
        # Standard `models.list()`-style validation: confirm the bearer
        # token authenticates against the Bedrock control plane via
        # `ListFoundationModels`. The default `AmazonBedrockLimitedAccess`
        # policy that backs Bedrock API keys includes this permission, so
        # this works for the same population that can run inference.
        api_key = cfg.get("api_key")
        if not api_key:
            return False

        # boto3 reads AWS_BEARER_TOKEN_BEDROCK from the environment when
        # building either the `bedrock` or `bedrock-runtime` client. The
        # standard `set_api_key` flow already exports it; do it again
        # here defensively so a fresh paste at startup doesn't fail just
        # because env propagation hasn't happened yet.
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = str(api_key)

        try:
            import boto3  # noqa: F401
            import langchain_aws  # noqa: F401
        except Exception:
            return False

        try:
            client = boto3.client("bedrock", region_name=self._region(cfg))
            client.list_foundation_models()
            return True
        except Exception:
            return False

    def create_model(self, cfg: ProviderConfig):
        from langchain_aws import ChatBedrockConverse

        # Defensive re-export — ensures boto3 sees the same token that
        # was validated above even if this provider is constructed
        # before `set_api_key` had a chance to run.
        api_key = self.require_api_key(cfg)
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = api_key

        kwargs: dict[str, object] = {
            "model": cfg["model"],
            "region_name": self._region(cfg),
        }
        # Anthropic Claude 4.x (Opus 4 / Sonnet 4) on Bedrock rejects
        # `temperature` outright when the model has extended-thinking
        # semantics — the Converse API returns
        # "ValidationException: ... `temperature` is deprecated for this
        # model". The safest thing is to omit the field entirely for
        # those model IDs; the others still get the configured value.
        if _bedrock_accepts_temperature(cfg["model"]):
            kwargs["temperature"] = cfg["temperature"]
        return ChatBedrockConverse(**kwargs)


def _bedrock_accepts_temperature(model: str) -> bool:
    """Return False for Bedrock Anthropic model IDs that reject `temperature`.

    Empirically: Opus 4.1 / Sonnet 4.5 / Haiku 4.5 still accept `temperature`
    over the Converse API. Opus 4.7 deprecated it (extended thinking is
    always on, and the Converse layer 400s the request with
    "ValidationException: ... `temperature` is deprecated for this model").
    We assume that pattern holds for any opus-4 minor >= 7 and any
    later Claude opus major. If a future Sonnet/Haiku does the same,
    add it here.
    """
    name = (model or "").lower()
    if "claude-opus-4-" in name:
        # Pull the minor digit after "claude-opus-4-" — e.g. "7" out of
        # "us.anthropic.claude-opus-4-7" or "claude-opus-4-12-20260101".
        tail = name.split("claude-opus-4-", 1)[1]
        digits = ""
        for ch in tail:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits and int(digits) >= 7:
            return False
    # Anything newer than the 4.x major (5.x, 6.x, …) opus also assumed
    # to share Opus-4.7's behaviour until proven otherwise.
    if any(s in name for s in ("claude-opus-5", "claude-opus-6", "claude-opus-7")):
        return False
    return True


PROVIDER_REGISTRY: dict[str, BaseProvider] = {
    provider.name: provider
    for provider in (
        OllamaProvider(),
        OpenAIProvider(),
        ClaudeProvider(),
        GeminiProvider(),
        BedrockProvider(),
    )
}


def get_provider(name: str) -> BaseProvider:
    try:
        return PROVIDER_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"Unsupported model provider: {name}") from exc
