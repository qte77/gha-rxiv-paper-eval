"""Tests for the Classifier ABC and backend implementations (PR #3b)."""
from __future__ import annotations

import io
import json
import os
import urllib.error
from unittest.mock import patch

import pytest

import classifiers
from classifiers import (
    AnthropicClassifier,
    Classifier,
    GeminiClassifier,
    GitHubModelsClassifier,
    get_classifier,
)


def _fake_response(payload: dict) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode())


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider,expected",
    [
        ("github-models", GitHubModelsClassifier),
        ("gemini", GeminiClassifier),
        ("anthropic", AnthropicClassifier),
    ],
)
def test_get_classifier_returns_instance(provider: str, expected: type[Classifier]) -> None:
    instance = get_classifier(provider)
    assert isinstance(instance, expected)
    assert instance.name == provider


def test_get_classifier_unknown_provider_raises() -> None:
    with pytest.raises(SystemExit) as exc:
        get_classifier("not-a-provider")
    assert "not-a-provider" in str(exc.value)


def test_classifier_is_abstract() -> None:
    with pytest.raises(TypeError):
        Classifier()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# Offline / stub mode — all backends must honor RXIV_EVAL_OFFLINE=1
# ---------------------------------------------------------------------------


@pytest.fixture
def offline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RXIV_EVAL_OFFLINE", "1")
    monkeypatch.setenv("RXIV_EVAL_STUB_MODE", "yes")
    monkeypatch.setenv("RXIV_EVAL_RETRY_BASE_SECS", "0.01")


@pytest.mark.parametrize(
    "backend_cls",
    [GitHubModelsClassifier, GeminiClassifier, AnthropicClassifier],
)
def test_classify_honors_offline(
    backend_cls: type[Classifier], offline_env: None
) -> None:
    """All backends must short-circuit to the stub when RXIV_EVAL_OFFLINE=1."""
    with patch("urllib.request.urlopen", side_effect=AssertionError("urlopen in offline")):
        result = backend_cls().classify(
            model="any-model",
            system_prompt="sys",
            user_prompt="user",
            max_tokens=4,
        )
    assert result == "YES"  # STUB_MODE=yes


# ---------------------------------------------------------------------------
# GitHubModelsClassifier
# ---------------------------------------------------------------------------


class TestGitHubModels:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GH_TOKEN", "fake-token")
        monkeypatch.setenv("RXIV_EVAL_RETRY_BASE_SECS", "0.01")
        monkeypatch.delenv("RXIV_EVAL_OFFLINE", raising=False)

    def test_returns_content_on_success(self) -> None:
        resp = _fake_response(
            {"choices": [{"message": {"role": "assistant", "content": "YES"}}]}
        )
        with patch("urllib.request.urlopen", return_value=resp):
            result = GitHubModelsClassifier().classify(
                model="openai/gpt-4o-mini",
                system_prompt="s",
                user_prompt="u",
                max_tokens=4,
            )
        assert result == "YES"

    def test_retries_on_429_then_succeeds(self) -> None:
        http_err = urllib.error.HTTPError(
            url="https://models.github.ai/inference/chat/completions",
            code=429,
            msg="rate limit",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"message":"r"}'),
        )
        ok = _fake_response(
            {"choices": [{"message": {"role": "assistant", "content": "NO"}}]}
        )
        with patch("urllib.request.urlopen", side_effect=[http_err, ok]) as mock_open:
            with patch("time.sleep"):
                result = GitHubModelsClassifier().classify(
                    model="m", system_prompt="s", user_prompt="u", max_tokens=4
                )
        assert result == "NO"
        assert mock_open.call_count == 2


# ---------------------------------------------------------------------------
# GeminiClassifier
# ---------------------------------------------------------------------------


class TestGemini:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        monkeypatch.setenv("RXIV_EVAL_RETRY_BASE_SECS", "0.01")
        monkeypatch.delenv("RXIV_EVAL_OFFLINE", raising=False)

    def test_returns_content_on_success(self) -> None:
        # Gemini REST response shape: candidates[0].content.parts[0].text
        resp = _fake_response(
            {
                "candidates": [
                    {"content": {"parts": [{"text": "YES"}]}}
                ]
            }
        )
        with patch("urllib.request.urlopen", return_value=resp):
            result = GeminiClassifier().classify(
                model="gemini-2.5-flash-lite",
                system_prompt="s",
                user_prompt="u",
                max_tokens=4,
            )
        assert result == "YES"

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(SystemExit) as exc:
            GeminiClassifier().classify(
                model="any", system_prompt="s", user_prompt="u", max_tokens=4
            )
        assert "GEMINI_API_KEY" in str(exc.value)


# ---------------------------------------------------------------------------
# AnthropicClassifier
# ---------------------------------------------------------------------------


class TestAnthropic:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
        monkeypatch.setenv("RXIV_EVAL_RETRY_BASE_SECS", "0.01")
        monkeypatch.delenv("RXIV_EVAL_OFFLINE", raising=False)

    def test_returns_content_on_success(self) -> None:
        # Anthropic messages response: content[0].text
        resp = _fake_response(
            {
                "content": [{"type": "text", "text": "YES"}],
                "role": "assistant",
            }
        )
        with patch("urllib.request.urlopen", return_value=resp):
            result = AnthropicClassifier().classify(
                model="claude-haiku-4-5",
                system_prompt="s",
                user_prompt="u",
                max_tokens=4,
            )
        assert result == "YES"

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(SystemExit) as exc:
            AnthropicClassifier().classify(
                model="any", system_prompt="s", user_prompt="u", max_tokens=4
            )
        assert "ANTHROPIC_API_KEY" in str(exc.value)
