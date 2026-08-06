from __future__ import annotations

from backend import ai_provider, db
from backend.config import settings
from backend.connectors import registry


def _fresh_db(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()


def _no_env_keys(monkeypatch):
    monkeypatch.setattr(settings, "ai_provider", "auto")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "deepseek_api_key", "")


def test_portal_key_is_seen_without_env(tmp_path, monkeypatch):
    """A key saved in the AI-briefing connector portal works with no env vars —
    this was the bug: briefings only read env while smart import read portal."""
    _fresh_db(tmp_path)
    _no_env_keys(monkeypatch)
    registry.set_config("ai_briefing", {"deepseek_api_key": "portal-ds-key"})

    assert ai_provider.resolved_deepseek_key() == "portal-ds-key"
    assert ai_provider.deepseek_available()
    assert ai_provider.resolved_provider() == "deepseek"


def test_env_key_is_fallback_when_portal_empty(tmp_path, monkeypatch):
    _fresh_db(tmp_path)
    _no_env_keys(monkeypatch)
    monkeypatch.setattr(settings, "deepseek_api_key", "env-ds-key")

    assert ai_provider.resolved_deepseek_key() == "env-ds-key"


def test_portal_provider_choice_beats_env_choice(tmp_path, monkeypatch):
    _fresh_db(tmp_path)
    _no_env_keys(monkeypatch)
    monkeypatch.setattr(settings, "ai_provider", "deepseek")
    registry.set_config(
        "ai_briefing",
        {"provider": "anthropic_api", "anthropic_api_key": "a-key", "deepseek_api_key": "d-key"},
    )

    assert ai_provider.resolved_provider() == "anthropic_api"


def test_auto_prefers_deepseek_with_portal_keys(tmp_path, monkeypatch):
    """Auto picks on measured cost, not on brand.

    Benchmarked against the real briefing prompt, deepseek-v4-flash cost about
    a fortieth of claude-sonnet-4-6 per run, finished in a third of the time,
    and was the only model to do the arithmetic on a planted sector-total
    inconsistency. Auto only decides for someone who configured both keys and
    stated no preference; anyone who wants otherwise says so, and both routes
    for saying so are asserted below.
    """
    _fresh_db(tmp_path)
    _no_env_keys(monkeypatch)
    registry.set_config(
        "ai_briefing",
        {"provider": "auto", "anthropic_api_key": "a-key", "deepseek_api_key": "d-key"},
    )

    assert ai_provider.resolved_provider() == "deepseek"


def test_auto_still_reaches_anthropic_when_it_is_the_only_key(tmp_path, monkeypatch):
    """Preferring DeepSeek must not mean ignoring the alternative — an
    Anthropic-only install has to keep working untouched."""
    _fresh_db(tmp_path)
    _no_env_keys(monkeypatch)
    registry.set_config(
        "ai_briefing", {"provider": "auto", "anthropic_api_key": "a-key"}
    )

    assert ai_provider.resolved_provider() == "anthropic_api"


def test_an_explicit_anthropic_choice_overrides_the_default(tmp_path, monkeypatch):
    """The escape hatch the privacy policy points people at: someone who does
    not want their holdings processed by DeepSeek says so, and is obeyed even
    with both keys present."""
    _fresh_db(tmp_path)
    _no_env_keys(monkeypatch)
    registry.set_config(
        "ai_briefing",
        {"provider": "anthropic_api", "anthropic_api_key": "a-key", "deepseek_api_key": "d-key"},
    )

    assert ai_provider.resolved_provider() == "anthropic_api"
