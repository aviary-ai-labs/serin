"""Who owns a connector's config, and what that stops on a shared deployment.

Config used to be one blob per connector, stored instance-wide. Right for a
single-user box; on Cloud it meant one customer's exchange API secret sat in a
record every other customer could read and overwrite — and that the scheduler,
which runs a sync pass per account against that shared config, could pull one
person's crypto holdings into somebody else's portfolio.

Ownership is now per *field*, because the connectors mix the two: SnapTrade's
client_id is Serin's partner credential while auto_sync_daily is a preference,
and ai_briefing holds the operator's API key next to the user's chosen style.
"""

from __future__ import annotations

import pytest
from backend import db, scope
from backend.connectors import registry

A = "user-a"
B = "user-b"


@pytest.fixture
def fresh_db(tmp_path):
    db.set_db_path(tmp_path / "ownership.db")
    db.init_db()
    yield


def _no_ambient_user() -> str:
    """Stand-in for the pack's provider, which raises when no request is in
    flight. Deliberately not `lambda: scope.current()` — that recurses, since
    current() consults the provider whenever no scope is pinned."""
    raise RuntimeError("no authenticated user in this request context")


@pytest.fixture
def multiuser():
    """Accounts switched on, as the pack does at load. Every read below must
    therefore sit inside an explicit scope.using(), exactly as a real request
    does — anything else is the fail-closed path and should be loud."""
    scope.set_scope_provider(_no_ambient_user)
    yield
    scope.set_scope_provider(None)


def test_a_users_exchange_secret_is_invisible_to_another_user(fresh_db, multiuser):
    """The finding itself."""
    with scope.using(A):
        registry.set_config("coinbase", {"api_key": "A-key", "api_secret": "A-secret"})
    with scope.using(B):
        assert registry.get_config("coinbase").get("api_key") in (None, "")
        assert registry.get_config("coinbase").get("api_secret") in (None, "")
        registry.set_config("coinbase", {"api_key": "B-key", "api_secret": "B-secret"})
    with scope.using(A):
        # B saving must not have overwritten A's.
        assert registry.get_config("coinbase")["api_key"] == "A-key"
        assert registry.get_config("coinbase")["api_secret"] == "A-secret"


def test_toggling_a_personal_connector_does_not_toggle_it_for_everyone(fresh_db, multiuser):
    """Tested against the manifest default rather than with it: coinbase ships
    default_enabled=True, so asserting B sees False after A enables it would
    pass without the switch ever being per-user. A turning it *off* is the
    direction that can only hold if the state is genuinely separate."""
    with scope.using(B):
        assert registry.is_enabled("coinbase") is True  # the default we move away from
    with scope.using(A):
        registry.set_enabled("coinbase", False)
        assert registry.is_enabled("coinbase") is False
    with scope.using(B):
        assert registry.is_enabled("coinbase") is True


def test_a_user_cannot_repoint_the_shared_market_data_provider(fresh_db, multiuser):
    """fmp.base_url is the poisoning vector: instance-owned, and previously
    writable by anyone signed in."""
    with scope.using(A):
        registry.set_config("fmp", {"api_key": "stolen", "base_url": "https://evil.example"})
    with scope.using(B):
        config = registry.get_config("fmp")
        assert config.get("base_url") in (None, "")
        assert config.get("api_key") in (None, "")


def test_a_user_cannot_switch_off_everyones_price_feed(fresh_db, multiuser):
    with scope.using(A):
        with pytest.raises(PermissionError):
            registry.set_enabled("fmp", False)


def test_operator_fields_stay_shared(fresh_db, multiuser):
    """The flip side: SnapTrade's partner credentials must reach every user,
    or nobody can link a broker."""
    with scope.using(scope.INSTANCE_SCOPE):
        registry._instance_set(
            registry._config_key("snaptrade"),
            '{"client_id": "partner-id", "consumer_key": "partner-key"}',
        )
    for who in (A, B):
        with scope.using(who):
            assert registry.get_config("snaptrade")["client_id"] == "partner-id"


def test_user_field_overlays_the_operator_blob(fresh_db, multiuser):
    """Mixed connector: shared credentials, personal preference, one merged view."""
    with scope.using(scope.INSTANCE_SCOPE):
        registry._instance_set(
            registry._config_key("snaptrade"), '{"client_id": "partner-id"}'
        )
    with scope.using(A):
        registry.set_config("snaptrade", {"auto_sync_daily": True})
        merged = registry.get_config("snaptrade")
        assert merged["client_id"] == "partner-id"   # from the deployment
        assert merged["auto_sync_daily"] is True     # from the person
    with scope.using(B):
        assert registry.get_config("snaptrade").get("auto_sync_daily") in (None, False)


# --- self-host must not notice any of this ---------------------------------


def test_single_user_install_is_unaffected(fresh_db):
    """No scope provider: one person, who is also the operator. Everything
    stays writable, including the deployment-owned fields."""
    assert registry.instance_config_is_writable() is True
    registry.set_config("fmp", {"api_key": "mine", "base_url": "https://custom"})
    registry.set_config("coinbase", {"api_key": "mine-too"})
    assert registry.get_config("fmp")["api_key"] == "mine"
    assert registry.get_config("coinbase")["api_key"] == "mine-too"
    registry.set_enabled("fmp", False)
    assert registry.is_enabled("fmp") is False


def test_values_written_before_the_split_are_still_readable(fresh_db):
    """Migration-free upgrade: an existing install stored user-owned fields in
    the instance blob. Reads fall back to it, so the value survives until it is
    next saved — no migration step, no re-entered API keys.
    """
    with scope.using(scope.INSTANCE_SCOPE):
        registry._instance_set(
            registry._config_key("coinbase"), '{"api_key": "legacy", "api_secret": "old"}'
        )
    assert registry.get_config("coinbase")["api_key"] == "legacy"
    assert registry.get_config("coinbase")["api_secret"] == "old"
