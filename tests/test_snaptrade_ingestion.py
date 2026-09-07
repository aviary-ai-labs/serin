"""SnapTrade feeding the normalized ledger.

Item 10. The point of doing it last is that there is nothing bespoke left:
broker activity becomes ordinary transactions, and everything downstream —
valuation, TWR, coverage — reads them without knowing where they came from.

What needs testing is the seams: that a re-sync does not duplicate, that a
contribution is not mistaken for growth, and that the paid add-on gates only
the deployment that actually pays for it.
"""

from __future__ import annotations

import pathlib

import pytest
from backend import db, entitlements, snaptrade
from backend.models import canonical_action, is_external_flow


@pytest.fixture
def store(tmp_path):
    db.set_db_path(tmp_path / "snaptrade.db")
    db.init_db()
    yield db
    entitlements.set_verifier(None)


# --- the activity map -----------------------------------------------------


def test_a_contribution_is_external_and_a_trade_is_not():
    """The single classification the whole return calculation depends on."""
    assert snaptrade._ACTIVITY_ACTIONS["CONTRIBUTION"] == "deposit"
    assert snaptrade._ACTIVITY_ACTIONS["WITHDRAWAL"] == "withdrawal"
    assert is_external_flow(snaptrade._ACTIVITY_ACTIONS["CONTRIBUTION"]) is True
    assert is_external_flow(snaptrade._ACTIVITY_ACTIONS["BUY"]) is False
    assert is_external_flow(snaptrade._ACTIVITY_ACTIONS["SELL"]) is False


def test_a_broker_transfer_moves_no_money_across_the_boundary():
    """Moving your own money between two tracked accounts must not read as a
    contribution on one side and a withdrawal on the other."""
    assert snaptrade._ACTIVITY_ACTIONS["TRANSFER"] == "transfer"
    assert is_external_flow("transfer") is False


def test_every_mapped_action_is_one_the_ledger_understands():
    """A typo here would be stored and then silently ignored by every
    downstream sum, which is worse than failing the import."""
    from backend.models import (
        COST_ACTIONS,
        EXTERNAL_ACTIONS,
        INCOME_ACTIONS,
        INTERNAL_ACTIONS,
        NEUTRAL_ACTIONS,
    )

    known = INTERNAL_ACTIONS | EXTERNAL_ACTIONS | INCOME_ACTIONS | COST_ACTIONS | NEUTRAL_ACTIONS
    for raw, action in snaptrade._ACTIVITY_ACTIONS.items():
        assert canonical_action(action) in known, f"{raw} maps to unknown action {action}"


def test_dividend_reinvestment_is_recorded_as_a_purchase():
    assert snaptrade._ACTIVITY_ACTIONS["REI"] == "buy"


# --- idempotent re-sync ---------------------------------------------------


def test_the_same_activity_cannot_be_imported_twice(store):
    """Re-syncing is the normal case — it runs daily. Every contribution
    counted twice inflates the money someone appears to have added, which
    deflates every return computed from it."""
    from backend.models import TransactionIn

    row = TransactionIn(symbol="AAPL", broker="questrade", action="buy",
                        quantity=10, price=100.0, occurred_at="2026-01-05")
    ref = f"{snaptrade.BACKFILL_REF_PREFIX}activity-1"
    assert db.create_transaction(row, source="snaptrade", external_id=ref) is not None
    assert db.create_transaction(row, source="snaptrade", external_id=ref) is None
    assert len(db.list_transactions()) == 1


def test_two_different_activities_both_land(store):
    from backend.models import TransactionIn

    row = TransactionIn(symbol="AAPL", broker="questrade", action="buy",
                        quantity=10, price=100.0, occurred_at="2026-01-05")
    for activity_id in ("a1", "a2"):
        created = db.create_transaction(
            row, source="snaptrade",
            external_id=f"{snaptrade.BACKFILL_REF_PREFIX}{activity_id}",
        )
        assert created is not None
    assert len(db.list_transactions()) == 2


def test_repeat_backfills_are_deduped_by_the_index_not_by_scanning_notes(store):
    """The original check string-matched every row's *notes* to find its own
    prior imports. The unique index on (user_id, external_id) does that now,
    and it does it atomically — two concurrent syncs both passed the old
    read-then-write.

    The ledger is read once more, for a different question the index cannot
    answer: whether this trade already arrived from a broker CSV, which is
    fingerprinted differently and would otherwise import a second time. That
    read is a genuine read-then-write and two simultaneous backfills could
    still both pass it — an acceptable trade against the alternative, which
    was duplicating every overlapping trade on every run. It is one query for
    the whole backfill, not one per row."""
    names = snaptrade.backfill_transactions.__code__.co_names
    assert "existing_refs" not in names, "the notes scan came back"
    source = pathlib.Path("backend/snaptrade.py").read_text()
    body = source[source.index("def backfill_transactions"):]
    body = body[: body.index("\ndef ")]
    assert body.count("list_transactions(") == 1, (
        "the ledger is being read more than once per backfill — the "
        "cross-source check is meant to load it a single time"
    )
    assert ".notes" not in body.split("for activity in")[0], (
        "dedupe is reading row notes again rather than matching on external_id"
    )


# --- imported activity reaches the performance engine ---------------------


def test_imported_activity_drives_transaction_accurate_returns(store):
    """End to end: broker rows land as ordinary transactions, and the history
    engine reads them without knowing SnapTrade exists."""
    from backend import portfolio_history as ph
    from backend.models import TransactionIn

    for i, (action, day, price) in enumerate(
        [("deposit", "2026-01-02", 10000.0), ("buy", "2026-01-03", 100.0)]
    ):
        db.create_transaction(
            TransactionIn(
                symbol="AAPL" if action == "buy" else "",
                broker="questrade", action=action,
                quantity=10 if action == "buy" else 0,
                price=price, occurred_at=day,
            ),
            source="snaptrade",
            external_id=f"{snaptrade.BACKFILL_REF_PREFIX}a{i}",
        )
    cover = ph.coverage(db.list_positions(include_closed=True), db.list_transactions())
    assert cover["external_flows"] == 1
    assert cover["trades"] == 1
    assert cover["since"] == "2026-01-02"


# --- the add-on gate ------------------------------------------------------


def test_self_host_is_never_gated(store):
    """No verifier installed means open source, and someone running their own
    instance brings their own SnapTrade credentials — they owe us nothing for
    using them."""
    entitlements.set_verifier(None)
    assert snaptrade.broker_sync_entitled() is True


def test_a_hosted_plan_without_the_add_on_is_refused(store):
    entitlements.set_verifier(lambda: {"plan": "cloud", "features": []})
    assert snaptrade.broker_sync_entitled() is False


def test_a_hosted_plan_with_the_add_on_is_allowed(store):
    entitlements.set_verifier(
        lambda: {"plan": "cloud", "features": [snaptrade.BROKER_SYNC_FEATURE]}
    )
    assert snaptrade.broker_sync_entitled() is True


def test_a_broken_verifier_does_not_lock_anyone_out(store):
    """Entitlements failing closed on an outage would mean a paying customer
    loses a feature because our own check broke."""
    def boom():
        raise RuntimeError("billing unreachable")

    entitlements.set_verifier(boom)
    # summary() degrades to open source, which this treats as ungated.
    assert snaptrade.broker_sync_entitled() is True


# --- the credential ------------------------------------------------------


def test_the_user_secret_is_encrypted_at_rest(store):
    """It authorises reading someone's brokerage holdings, and on Cloud it
    sits in a database shared by every customer."""
    snaptrade._store_user({"userId": "u-1", "userSecret": "super-secret-value"})
    raw = db.get_setting(snaptrade.USER_SETTING_KEY)
    assert "super-secret-value" not in raw, "the secret was stored in plaintext"
    assert snaptrade.get_stored_user()["userSecret"] == "super-secret-value"


def test_a_plaintext_secret_written_by_an_older_build_still_works(store):
    """Failing hard on an unencrypted value would strand a working connection
    made before this change."""
    import json

    db.set_setting(snaptrade.USER_SETTING_KEY,
                   json.dumps({"userId": "u-1", "userSecret": "legacy-plain"}))
    assert snaptrade.get_stored_user()["userSecret"] == "legacy-plain"


def test_each_serin_account_gets_its_own_snaptrade_identity(store):
    """app_settings is scoped, so one SnapTrade end-user per Serin account
    falls out of storage — the hosted requirement in the module docstring."""
    from backend import scope

    with scope.using("u_alice"):
        snaptrade._store_user({"userId": "alice-st", "userSecret": "alice-secret"})
    with scope.using("u_bob"):
        snaptrade._store_user({"userId": "bob-st", "userSecret": "bob-secret"})
    with scope.using("u_alice"):
        assert snaptrade.get_stored_user()["userId"] == "alice-st"
    with scope.using("u_bob"):
        assert snaptrade.get_stored_user()["userId"] == "bob-st"


# --- the SDK contract -----------------------------------------------------


def test_the_client_is_built_in_commercial_mode(store, monkeypatch):
    """SDK 13 replaced bare consumer_key/client_id with explicit auth modes.
    Passing the old shape raises TypeError, which is exactly how this
    integration broke: requirements allowed >=11 with no upper bound, a
    rebuild pulled 13, and every SnapTrade call started failing at client
    construction rather than anywhere informative.

    Commercial is the correct mode — Serin owns the SnapTrade account and
    registers one end-user per Serin account. Personal is a different flow.
    """
    captured = {}

    class FakeSnapTrade:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            if "auth" not in kwargs:
                raise TypeError("missing 1 required keyword-only argument: 'auth'")

    import sys
    import types

    fake_module = types.ModuleType("snaptrade_client")
    fake_module.SnapTrade = FakeSnapTrade
    fake_auth = types.ModuleType("snaptrade_client.auth")

    class SnapTradeAuth:
        @staticmethod
        def commercial_api_key(consumer_key=None, client_id=None):
            return {"mode": "commercialApiKey", "consumer_key": consumer_key,
                    "client_id": client_id}

    fake_auth.SnapTradeAuth = SnapTradeAuth
    monkeypatch.setitem(sys.modules, "snaptrade_client", fake_module)
    monkeypatch.setitem(sys.modules, "snaptrade_client.auth", fake_auth)
    monkeypatch.setattr(snaptrade, "_client", None)
    monkeypatch.setattr(snaptrade, "_client_creds", None)
    monkeypatch.setattr(snaptrade, "resolved_credentials", lambda: ("cid", "ckey"))

    snaptrade._get_client()
    assert captured["auth"]["mode"] == "commercialApiKey", "not built in Commercial mode"
    assert captured["auth"]["client_id"] == "cid"
