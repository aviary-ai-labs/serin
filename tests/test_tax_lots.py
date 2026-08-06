from __future__ import annotations

from datetime import date, timedelta

from backend import db
from backend.models import PositionIn, TaxLotIn


def test_tax_lots_create_list_and_delete(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    db.create_position(
        PositionIn(
            symbol="GOOG",
            name="Alphabet",
            broker="etrade",
            asset_type="stock",
            quantity=10,
            average_cost=100,
            current_price=120,
            sector="Communication Services",
        )
    )

    lot = db.create_tax_lot(
        TaxLotIn(
            symbol="goog",
            broker="ETrade",
            quantity=3,
            cost_basis=100,
            acquired_at=(date.today() - timedelta(days=20)).isoformat(),
        )
    )

    assert lot.symbol == "GOOG"
    assert lot.broker == "etrade"
    assert lot.market_value == 360
    assert lot.unrealized_gain == 60
    assert lot.holding_period == "short-term"

    lots = db.list_tax_lots(symbol="GOOG", broker="etrade")
    assert [item.id for item in lots] == [lot.id]

    assert db.delete_tax_lot(lot.id) is True
    assert db.list_tax_lots(symbol="GOOG", broker="etrade") == []


def test_tax_lot_marks_long_term_after_one_year(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    db.create_position(
        PositionIn(
            symbol="AAPL",
            broker="manual",
            asset_type="stock",
            quantity=1,
            average_cost=100,
            current_price=90,
            sector="Technology",
        )
    )

    lot = db.create_tax_lot(
        TaxLotIn(
            symbol="AAPL",
            broker="manual",
            quantity=1,
            cost_basis=100,
            acquired_at=(date.today() - timedelta(days=400)).isoformat(),
        )
    )

    assert lot.holding_period == "long-term"
    assert lot.days_to_long_term == 0
    assert lot.unrealized_gain == -10
