"""LLM classifier backends for the rxiv eval pipeline.

The base ``Classifier`` owns retry/backoff and the offline-stub guard; each
subclass implements only its provider-specific REST call in ``_do_call``.

Default backend is ``github-models`` (free, OpenAI-style chat completions via
GitHub Models). ``gemini`` is the designated free-tier backup. ``anthropic`` is
paid and opt-in only.
"""
from __future__ import annotations

import abc
import json
import os
import urllib.request
from typing import ClassVar

from eval_papers import (
    GITHUB_MODELS_URL,
    Settings,
    _stub_response,
    _with_retry,
)


class Classifier(abc.ABC):
    """Provider-agnostic relevance classifier. Subclasses implement ``_do_call``."""

    name: ClassVar[str]

    @abc.abstractmethod
    def _do_call(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> str:
        """Single shot against the underlying provider. May raise HTTPError/URLError."""

    def classify(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> str:
        """Return the assistant's text content. Honors offline mode + retries."""
        settings = Settings()
        if settings.offline:
            return _with_retry(lambda: _stub_response(user_prompt), settings)
        return _with_retry(
            lambda: self._do_call(
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=max_tokens,
            ),
            settings,
        )


class GitHubModelsClassifier(Classifier):
    name = "github-models"

    def _do_call(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> str:
        token = os.environ.get("GH_TOKEN")
        if not token:
            raise SystemExit("GH_TOKEN is required for provider=github-models")
        payload = {
            "model": model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        req = urllib.request.Request(
            GITHUB_MODELS_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.load(resp)
        return body["choices"][0]["message"]["content"]


class GeminiClassifier(Classifier):
    name = "gemini"
    _BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

    def _do_call(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> str:
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise SystemExit("GEMINI_API_KEY is required for provider=gemini")
        url = f"{self._BASE_URL}/{model}:generateContent?key={key}"
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"parts": [{"text": user_prompt}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens},
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.load(resp)
        return body["candidates"][0]["content"]["parts"][0]["text"]


class AnthropicClassifier(Classifier):
    """Paid; opt-in only."""

    name = "anthropic"
    _URL = "https://api.anthropic.com/v1/messages"
    _API_VERSION = "2023-06-01"

    def _do_call(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> str:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise SystemExit("ANTHROPIC_API_KEY is required for provider=anthropic")
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        req = urllib.request.Request(
            self._URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "x-api-key": key,
                "anthropic-version": self._API_VERSION,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.load(resp)
        return body["content"][0]["text"]


_REGISTRY: dict[str, type[Classifier]] = {
    GitHubModelsClassifier.name: GitHubModelsClassifier,
    GeminiClassifier.name: GeminiClassifier,
    AnthropicClassifier.name: AnthropicClassifier,
}


def get_classifier(provider: str) -> Classifier:
    if provider not in _REGISTRY:
        raise SystemExit(
            f"unknown provider: {provider} (choices: {sorted(_REGISTRY)})"
        )
    return _REGISTRY[provider]()
