"""Provider fallback chain + incremental (never re-pull cached dates)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from backend import connectors, db, prices
from backend.models import PositionIn


def _days_ago(n: int) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).date().isoformat()


class FakeConn:
    """Records the periods it's asked for and returns canned history."""

    def __init__(self, history=None, quote_val=None):
        self._history = history or {}
        self._quote = quote_val
        self.periods: list[str] = []

    def fetch_history(self, period, symbols, positions_by_symbol):
        self.periods.append(period)
        return {"history": {s: self._history[s] for s in symbols if s in self._history}, "errors": []}

    def quote(self, symbol, asset_type):
        return self._quote


def _init(tmp_path, monkeypatch, symbol="AAPL"):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    db.create_position(
        PositionIn(symbol=symbol, broker="manual", asset_type="stock",
                   quantity=1, average_cost=1, current_price=1)
    )
    monkeypatch.setattr(connectors, "active_crypto_data", lambda: None)


# --- fallback chain --------------------------------------------------------

def test_chain_falls_through_on_empty(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    d1, d2 = _days_ago(2), _days_ago(1)
    empty = FakeConn(history={})
    filler = FakeConn(history={"AAPL": {"dates": [d1, d2], "closes": [10.0, 11.0]}})
    monkeypatch.setattr(connectors, "market_data_chain", lambda: [("empty", empty), ("filler", filler)])

    result = prices.fetch_price_history("1w")

    assert result["history"]["AAPL"]["closes"] == [10.0, 11.0]
    assert empty.periods and filler.periods  # active tried first, then the fallback


def test_symbol_history_chain_fallback(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch, symbol="JD")
    d1, d2 = _days_ago(2), _days_ago(1)
    empty = FakeConn(history={})
    filler = FakeConn(history={"JD": {"dates": [d1, d2], "closes": [26.0, 27.0]}})
    monkeypatch.setattr(connectors, "market_data_chain", lambda: [("empty", empty), ("filler", filler)])

    result = prices.fetch_symbol_history("JD", "stock", "1y")

    assert result["closes"] == [26.0, 27.0]


def test_quote_chain_fallback(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    none_conn = FakeConn(quote_val=None)
    good = FakeConn(quote_val={"symbol": "AAPL", "price": 42.0})
    monkeypatch.setattr(connectors, "market_data_chain", lambda: [("none", none_conn), ("good", good)])

    assert prices.fetch_quote("AAPL", "stock")["price"] == 42.0


# --- incremental: never re-pull dates already cached -----------------------

def test_incremental_only_fetches_recent_tail(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    # Cache covers 120d..2d ago — reaches back past the 3m window start, latest recent.
    dates = [(datetime.now(UTC) - timedelta(days=n)).date().isoformat() for n in range(120, 1, -1)]
    closes = [100.0 + i for i in range(len(dates))]
    db.cache_price_history({"AAPL": {"dates": dates, "closes": closes}})

    rec = FakeConn(history={"AAPL": {"dates": [_days_ago(1)], "closes": [999.0]}})
    monkeypatch.setattr(connectors, "market_data_chain", lambda: [("rec", rec)])

    result = prices.fetch_price_history("3m", refresh=True)

    # Asked for just the recent gap ("1w"), NOT the full 3m of cached dates.
    assert rec.periods == ["1w"]
    # ...and the response still returns the full window (cached + new tail merged).
    assert len(result["history"]["AAPL"]["dates"]) > 50
    assert result["history"]["AAPL"]["dates"][-1] == _days_ago(1)


def test_incremental_full_backfill_when_older_missing(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    # Cache only the last 3 days — doesn't reach the 3m window start.
    recent = [_days_ago(3), _days_ago(2), _days_ago(1)]
    db.cache_price_history({"AAPL": {"dates": recent, "closes": [1.0, 2.0, 3.0]}})

    rec = FakeConn(history={"AAPL": {"dates": recent, "closes": [1.0, 2.0, 3.0]}})
    monkeypatch.setattr(connectors, "market_data_chain", lambda: [("rec", rec)])

    prices.fetch_price_history("3m", refresh=True)

    # Missing older data → full window requested to backfill.
    assert rec.periods == ["3m"]
