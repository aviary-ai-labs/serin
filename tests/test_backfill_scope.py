"""Importing one broker's history without touching the others.

The dedupe already makes a repeat backfill safe — imported rows carry a
``snaptrade:<id>`` reference and are skipped. But safe is not the same as
controllable: someone who built a Robinhood ledger from a statement wants to
import Fidelity without Robinhood being touched at all, and telling them to
trust the dedupe is telling them to trust it with their cost basis.
"""

from __future__ import annotations

import pytest
from backend import snaptrade

ACCOUNTS = [
    {"id": "a1", "name": "Individual", "institution": "Robinhood"},
    {"id": "a2", "name": "Roth IRA", "institution": "Robinhood"},
    {"id": "a3", "name": "Brokerage", "institution": "E*TRADE"},
    {"id": "a4", "name": "Individual", "institution": "Fidelity"},
]


@pytest.fixture
def broker(monkeypatch, tmp_path):
    from backend import db

    db.set_db_path(tmp_path / "backfill.db")
    db.init_db()
    monkeypatch.setattr(snaptrade, "get_stored_user",
                        lambda: {"userId": "u", "userSecret": "s"})
    monkeypatch.setattr(snaptrade, "list_accounts", lambda: list(ACCOUNTS))

    asked: list[str] = []

    class FakeClient:
        class account_information:
            @staticmethod
            def get_account_activities(**kwargs):
                asked.append(kwargs["account_id"])
                return type("R", (), {"body": []})()

    monkeypatch.setattr(snaptrade, "_get_client", lambda: FakeClient())
    return asked


def test_no_filter_imports_every_connected_account(broker):
    snaptrade.backfill_transactions()

    assert broker == ["a1", "a2", "a3", "a4"]


def test_naming_one_broker_touches_only_its_accounts(broker):
    snaptrade.backfill_transactions(institutions=["Robinhood"])

    assert broker == ["a1", "a2"]


def test_a_broker_with_several_accounts_imports_all_of_them(broker):
    snaptrade.backfill_transactions(institutions=["robinhood"])   # case-insensitive

    assert broker == ["a1", "a2"]


def test_several_brokers_can_be_named(broker):
    snaptrade.backfill_transactions(institutions=["Fidelity", "E*TRADE"])

    assert sorted(broker) == ["a3", "a4"]


def test_an_unknown_broker_imports_nothing_and_says_so(broker):
    """Silently importing everything would be the worst possible reading of a
    typo, given the whole point is not touching the others."""
    with pytest.raises(snaptrade.SnapTradeError, match="No connected accounts"):
        snaptrade.backfill_transactions(institutions=["Schwab"])

    assert broker == []


def test_blank_entries_are_ignored_rather_than_matching_nothing(broker):
    snaptrade.backfill_transactions(institutions=["  ", "Fidelity"])

    assert broker == ["a4"]


def test_an_empty_list_still_means_everything(broker):
    snaptrade.backfill_transactions(institutions=[])

    assert broker == ["a1", "a2", "a3", "a4"]
