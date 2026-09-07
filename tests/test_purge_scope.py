"""Deleting an account has to actually delete it.

App Store guideline 5.1.1(v) and Play's data-deletion rule both ask for the
real thing rather than a hidden flag, so the tests here are about the two ways
a purge goes wrong: leaving rows behind, and taking rows that were not its to
take.
"""

from __future__ import annotations

import pytest
from backend import db, scope
from backend.models import PositionIn, TransactionIn


def _seed(user: str) -> None:
    with scope.using(user):
        db.create_position(
            PositionIn(
                symbol="AAPL", name="Apple", broker="fidelity", asset_type="stock",
                quantity=5, average_cost=100.0, current_price=150.0,
            )
        )
        db.create_transaction(
            TransactionIn(symbol="AAPL", action="buy", quantity=5, price=100.0, fee=0.0, occurred_at="2026-01-02")
        )
        db.set_setting("display_currency", "USD")


@pytest.fixture
def store(tmp_path):
    db.set_db_path(tmp_path / "purge.db")
    db.init_db()
    return db


def test_purge_removes_every_scoped_table_for_that_account(store):
    _seed("u_alice")
    removed = db.purge_scope("u_alice")
    assert sum(removed.values()) > 0
    with scope.using("u_alice"):
        assert db.list_positions() == []
        assert db.list_transactions() == []
        assert db.get_setting("display_currency", "") == ""


def test_purge_leaves_other_accounts_untouched(store):
    """The failure that would matter most: one deletion emptying a neighbour."""
    _seed("u_alice")
    _seed("u_bob")
    db.purge_scope("u_alice")
    with scope.using("u_bob"):
        assert len(db.list_positions()) == 1
        assert len(db.list_transactions()) == 1
        assert db.get_setting("display_currency", "") == "USD"


def test_purge_spares_the_shared_market_cache(store):
    """quotes/price_history/fundamentals belong to nobody. Evicting them on a
    delete would make one person leaving slow the app down for everyone."""
    _seed("u_alice")
    db.cache_price_history(
        {"AAPL": {"dates": ["2026-01-02", "2026-01-03"], "closes": [150.0, 152.0]}}
    )
    db.purge_scope("u_alice")
    assert db.get_cached_price_history(["AAPL"]), "shared price history was destroyed"


def test_every_scoped_table_is_covered(store):
    """A new table with a user_id that nobody added to SCOPED_TABLES would
    leave that column of someone's data behind forever. Fail here instead."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        with_user = set()
        for (name,) in rows:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({name})")}
            if "user_id" in cols:
                with_user.add(name)
    missing = with_user - set(db.SCOPED_TABLES)
    assert not missing, f"scoped tables missing from SCOPED_TABLES: {sorted(missing)}"


def test_purge_refuses_an_empty_scope(store):
    """An empty scope id under a permissive WHERE could match far too much."""
    with pytest.raises(ValueError):
        db.purge_scope("")
