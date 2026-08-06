"""The AI provider waterfall — ordering, fallback, and what counts as usable.

The chain is the single answer to "which model runs, with whose key" for
briefings and Smart Import both. These tests pin the rules that matter: the
user's order wins, a provider without its key silently drops out, configs
written before the list existed still resolve, and env vars remain the
deployment-level fallback.
"""

from __future__ import annotations

import io

import pytest
from backend import ai_provider
from backend.config import settings

ALL_KEY_ENVS = (
    "OPENAI_API_KEY", "GEMINI_API_KEY", "XAI_API_KEY", "OPENROUTER_API_KEY",
)


@pytest.fixture
def clean_env(monkeypatch):
    """No keys anywhere, no local claude binary — a blank slate."""
    for env in ALL_KEY_ENVS:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    monkeypatch.setattr(settings, "ai_provider", "auto")
    monkeypatch.setattr("backend.config.shutil.which", lambda name: None)
    monkeypatch.setattr(ai_provider, "_portal_config", lambda: {})
    return monkeypatch


def _portal(monkeypatch, cfg):
    monkeypatch.setattr(ai_provider, "_portal_config", lambda: dict(cfg))


# --- legacy configs (no providers list) --------------------------------------


def test_blank_slate_resolves_to_nothing(clean_env):
    assert ai_provider.provider_chain() == []
    assert ai_provider.resolved_provider() == "none"


def test_legacy_auto_prefers_deepseek_then_anthropic(clean_env):
    clean_env.setattr(settings, "deepseek_api_key", "dsk")
    clean_env.setattr(settings, "anthropic_api_key", "ant")
    assert [e["id"] for e in ai_provider.provider_chain()] == ["deepseek", "anthropic"]
    assert ai_provider.resolved_provider() == "deepseek"


def test_legacy_explicit_choice_still_wins(clean_env):
    clean_env.setattr(settings, "anthropic_api_key", "ant")
    clean_env.setattr(settings, "deepseek_api_key", "dsk")
    _portal(clean_env, {"provider": "anthropic_api", "anthropic_api_key": ""})
    chain = ai_provider.provider_chain()
    assert [e["id"] for e in chain] == ["anthropic"]
    assert ai_provider.resolved_provider() == "anthropic_api"


# --- the providers list ------------------------------------------------------


def test_list_order_is_the_waterfall_order(clean_env):
    _portal(clean_env, {
        "providers": [{"id": "anthropic"}, {"id": "deepseek"}],
        "anthropic_api_key": "ant",
        "deepseek_api_key": "dsk",
    })
    assert [e["id"] for e in ai_provider.provider_chain()] == ["anthropic", "deepseek"]


def test_provider_without_its_key_drops_out(clean_env):
    _portal(clean_env, {
        "providers": [{"id": "openai"}, {"id": "deepseek"}],
        "deepseek_api_key": "dsk",
    })
    assert [e["id"] for e in ai_provider.provider_chain()] == ["deepseek"]


def test_env_var_is_the_deployment_fallback(clean_env):
    clean_env.setenv("OPENAI_API_KEY", "sk-env")
    _portal(clean_env, {"providers": [{"id": "openai"}]})
    chain = ai_provider.provider_chain()
    assert chain[0]["id"] == "openai"
    assert chain[0]["key"] == "sk-env"


def test_portal_key_beats_env(clean_env):
    clean_env.setenv("OPENAI_API_KEY", "sk-env")
    _portal(clean_env, {"providers": [{"id": "openai"}], "openai_api_key": "sk-portal"})
    assert ai_provider.provider_chain()[0]["key"] == "sk-portal"


def test_explicit_model_is_honoured_and_flagged(clean_env):
    _portal(clean_env, {
        "providers": [{"id": "deepseek", "model": "deepseek-v4-pro"}, {"id": "anthropic"}],
        "deepseek_api_key": "dsk",
        "anthropic_api_key": "ant",
    })
    chain = ai_provider.provider_chain()
    assert chain[0]["model"] == "deepseek-v4-pro"
    assert chain[0]["model_explicit"] is True
    assert chain[1]["model_explicit"] is False
    assert chain[1]["model"]  # default filled in


def test_ollama_needs_no_key_and_keeps_url_override(clean_env):
    _portal(clean_env, {
        "providers": [{"id": "ollama", "base_url": "http://box:11434/v1"}],
    })
    chain = ai_provider.provider_chain()
    assert chain[0]["id"] == "ollama"
    assert chain[0]["key"] == ""
    assert chain[0]["base_url"] == "http://box:11434/v1"


def test_unknown_and_duplicate_entries_are_ignored(clean_env):
    _portal(clean_env, {
        "providers": [
            {"id": "deepseek"}, {"id": "made-up"}, {"id": "deepseek"},
        ],
        "deepseek_api_key": "dsk",
    })
    assert [e["id"] for e in ai_provider.provider_chain()] == ["deepseek"]


def test_vision_chain_excludes_text_only_providers(clean_env):
    _portal(clean_env, {
        "providers": [{"id": "deepseek"}, {"id": "anthropic"}],
        "deepseek_api_key": "dsk",
        "anthropic_api_key": "ant",
    })
    assert [e["id"] for e in ai_provider.vision_chain()] == ["anthropic"]


# --- Smart Import's PDF rasterizer ------------------------------------------


def test_pdf_pages_become_png_images(tmp_path):
    from backend import smart_import
    from PIL import Image

    buffer = io.BytesIO()
    pages = [Image.new("RGB", (120, 60), "white") for _ in range(3)]
    pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:])

    images, total = smart_import._pdf_page_images(buffer.getvalue())
    assert total == 3
    assert len(images) == 3
    for data, mime in images:
        assert mime == "image/png"
        assert data.startswith(b"\x89PNG")


def test_pdf_page_cap(monkeypatch):
    from backend import smart_import
    from PIL import Image

    monkeypatch.setattr(smart_import, "MAX_PDF_PAGES", 2)
    buffer = io.BytesIO()
    pages = [Image.new("RGB", (60, 40), "white") for _ in range(4)]
    pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:])

    images, total = smart_import._pdf_page_images(buffer.getvalue())
    assert total == 4
    assert len(images) == 2


def test_garbage_pdf_fails_with_a_human_message():
    from backend import smart_import

    with pytest.raises(RuntimeError, match="Could not read that PDF"):
        smart_import._pdf_page_images(b"not a pdf at all")
