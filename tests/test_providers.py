from __future__ import annotations

import asyncio

import pytest
from backend.briefings import (
    DEEPSEEK_MAX_TOKENS,
    call_deepseek,
    estimate_model_cost_usd,
    parse_openai_chat_response,
)
from backend.config import settings


@pytest.fixture
def clean_providers(monkeypatch):
    monkeypatch.setattr(settings, "ai_provider", "auto")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    return monkeypatch


def test_auto_prefers_anthropic_over_deepseek_and_cli(clean_providers):
    clean_providers.setattr(settings, "anthropic_api_key", "sk-ant-test")
    clean_providers.setattr(settings, "deepseek_api_key", "sk-ds-test")
    assert settings.resolved_ai_provider == "anthropic_api"
    assert settings.ai_model == settings.anthropic_model


def test_auto_prefers_deepseek_over_cli(clean_providers):
    clean_providers.setattr(settings, "deepseek_api_key", "sk-ds-test")
    assert settings.resolved_ai_provider == "deepseek"
    assert settings.ai_model == settings.deepseek_model


def test_explicit_deepseek_without_key_is_none(clean_providers):
    clean_providers.setattr(settings, "ai_provider", "deepseek")
    assert settings.resolved_ai_provider == "none"


def test_parse_openai_chat_response_extracts_text_and_usage():
    data = {
        "model": "deepseek-v4-flash",
        "choices": [{"message": {"role": "assistant", "content": "# Briefing\nhello"}}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 400},
    }
    text, usage = parse_openai_chat_response(data, "deepseek-v4-flash", "deepseek")
    assert text.startswith("# Briefing")
    assert usage["input_tokens"] == 1200
    assert usage["output_tokens"] == 400
    assert usage["provider"] == "deepseek"


def test_parse_openai_chat_response_rejects_empty():
    with pytest.raises(RuntimeError):
        parse_openai_chat_response({"choices": []}, "deepseek-v4-flash", "deepseek")


def test_parse_openai_chat_response_explains_reasoning_without_final_text():
    data = {
        "model": "deepseek-v4-flash",
        "choices": [
            {
                "finish_reason": "length",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "Internal reasoning text should not be used as the briefing.",
                },
            }
        ],
        "usage": {
            "prompt_tokens": 5308,
            "completion_tokens": 1200,
            "completion_tokens_details": {"reasoning_tokens": 1200},
        },
    }

    with pytest.raises(RuntimeError) as exc_info:
        parse_openai_chat_response(data, "deepseek-v4-flash", "deepseek")

    message = str(exc_info.value)
    assert "final text" in message
    assert "finish_reason=length" in message
    assert "reasoning_content_present=true" in message


def test_call_deepseek_uses_larger_completion_budget(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "model": "deepseek-v4-flash",
                "choices": [{"message": {"role": "assistant", "content": "# Briefing\nhello"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, headers, json):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json
            return FakeResponse()

    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-v4-flash")
    monkeypatch.setattr(settings, "deepseek_base_url", "https://example.test")
    monkeypatch.setattr("backend.briefings.httpx.AsyncClient", FakeAsyncClient)

    text, _usage = asyncio.run(call_deepseek("prompt"))

    assert text.startswith("# Briefing")
    assert captured["body"]["max_tokens"] == DEEPSEEK_MAX_TOKENS
    assert captured["body"]["max_tokens"] >= 10000


def test_cost_estimate_uses_deepseek_pricing():
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "model": "deepseek-v4-flash"}
    assert estimate_model_cost_usd(usage) == pytest.approx(0.14 + 0.28)


def test_friendly_error_for_insufficient_balance(monkeypatch):
    from backend.briefings import friendly_model_error

    monkeypatch.setattr(settings, "ai_provider", "deepseek")
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test")
    error = friendly_model_error(
        RuntimeError('DeepSeek API returned 402: {"error":{"message":"Insufficient Balance"}}')
    )
    assert "platform.deepseek.com/top_up" in error
    assert "402" not in error


def test_cost_estimate_uses_claude_pricing():
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "model": "claude-sonnet-4-6"}
    assert estimate_model_cost_usd(usage) == pytest.approx(3.0 + 15.0)
