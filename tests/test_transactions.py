"""Transactions table — schema, sign conventions, summary, API."""

from __future__ import annotations

from backend import db, main
from fastapi.testclient import TestClient


def _fresh(tmp_path):
    db.set_db_path(tmp_path / "tx.db")
    db.init_db()


def test_transactions_table_exists(tmp_path):
    _fresh(tmp_path)
    with db.connect() as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "transactions" in names
    assert "accounts" in names


def test_buy_amount_is_negative(tmp_path):
    _fresh(tmp_path)
    tx = db.create_transaction(
        db.TransactionIn(
            symbol="AAPL", broker="manual", asset_type="stock", action="buy",
            quantity=10, price=200, fee=1, currency="USD", occurred_at="2026-06-20",
        )
    )
    # Buys are negative (cash leaves the account) and the fee is added to cost.
    assert tx.amount == -2001.0


def test_sell_amount_is_positive(tmp_path):
    _fresh(tmp_path)
    tx = db.create_transaction(
        db.TransactionIn(
            symbol="AAPL", broker="manual", asset_type="stock", action="sell",
            quantity=5, price=210, fee=1, currency="USD", occurred_at="2026-06-22",
        )
    )
    assert tx.amount == 1049.0  # +5 * 210 - 1


def test_dividend_amount_is_positive(tmp_path):
    _fresh(tmp_path)
    tx = db.create_transaction(
        db.TransactionIn(
            symbol="MSFT", broker="manual", action="dividend",
            quantity=0, price=24.50, occurred_at="2026-06-15",
        )
    )
    assert tx.amount == 24.50


def test_transaction_summary_aggregates(tmp_path):
    _fresh(tmp_path)
    db.create_transaction(db.TransactionIn(
        symbol="AAPL", action="buy", quantity=10, price=200, occurred_at="2026-01-10"))
    db.create_transaction(db.TransactionIn(
        symbol="AAPL", action="sell", quantity=5, price=220, occurred_at="2026-04-01"))
    db.create_transaction(db.TransactionIn(
        symbol="AAPL", action="dividend", price=8.0, occurred_at="2026-05-01"))
    summary = db.transaction_summary()["by_symbol"]
    aapl = next(s for s in summary if s["symbol"] == "AAPL")
    assert aapl["buys"] == 2000.0
    assert aapl["sells"] == 1100.0
    assert aapl["dividends"] == 8.0
    assert aapl["shares_bought"] == 10
    assert aapl["shares_sold"] == 5


def test_transaction_api_round_trip(tmp_path):
    _fresh(tmp_path)
    client = TestClient(main.app)
    r = client.post("/api/transactions", json={
        "symbol": "tsla", "action": "buy", "quantity": 2, "price": 180,
        "occurred_at": "2026-06-20",
    })
    assert r.status_code == 200
    assert r.json()["symbol"] == "TSLA"  # normalized
    listed = client.get("/api/v1/transactions").json()
    assert len(listed["transactions"]) == 1
    summary = client.get("/api/transactions/summary").json()
    assert summary["by_symbol"][0]["symbol"] == "TSLA"
    deleted = client.delete(f"/api/transactions/{listed['transactions'][0]['id']}")
    assert deleted.status_code == 200
    assert client.get("/api/transactions").json()["transactions"] == []
