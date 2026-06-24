from __future__ import annotations

import json
import os
from typing import Any
from urllib.request import Request, urlopen

ProviderConfig = dict[str, Any]

class BaseProvider:
    name: str
    env_var: str | list[str] | None = None
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
                if not any(s in msg for s in ("429", "quota", "rate", "exhausted")):
                    return False
        _ = last_exc 
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
    validation_timeout = 10.0

    def base_url(self, cfg: ProviderConfig) -> str:
        raw = cfg.get("base_url") or os.environ.get(self.base_url_env_var) or self.default_base_url
        return str(raw).rstrip("/")

    def validate(self, cfg: ProviderConfig) -> bool:
        base = self.base_url(cfg)
        model = cfg.get("model")

        try:
            with urlopen(f"{base}/api/tags", timeout=2) as resp:
                if not (200 <= resp.status < 300):
                    return False
                payload = json.loads(resp.read())
        except Exception:
            return False

        if model:
            available = {m.get("name", "") for m in payload.get("models", [])}
            if not any(
                m == model or m == f"{model}:latest" or m.startswith(f"{model}:")
                for m in available
            ):
                return False

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

    name = "bedrock"
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
        api_key = cfg.get("api_key")
        if not api_key:
            return False
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = str(api_key)

        try:
            import boto3  
            import langchain_aws  
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

        api_key = self.require_api_key(cfg)
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = api_key

        kwargs: dict[str, object] = {
            "model": cfg["model"],
            "region_name": self._region(cfg),
        }
        if _bedrock_accepts_temperature(cfg["model"]):
            kwargs["temperature"] = cfg["temperature"]
        return ChatBedrockConverse(**kwargs)

def _bedrock_accepts_temperature(model: str) -> bool:
    name = (model or "").lower()
    if "claude-opus-4-" in name:
        tail = name.split("claude-opus-4-", 1)[1]
        digits = ""
        for ch in tail:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits and int(digits) >= 7:
            return False
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
