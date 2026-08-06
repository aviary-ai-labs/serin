from __future__ import annotations

from backend.csv_import import parse_positions_csv


def test_csv_import_accepts_common_headers():
    csv_text = (
        "Symbol,Description,Quantity,Cost Basis Per Share,Market Price,Security Type,Sector\n"
        "MSFT,Microsoft,4,$250.00,$300.00,stock,Technology\n"
    )

    positions = parse_positions_csv(csv_text, broker="csv")

    assert len(positions) == 1
    assert positions[0].symbol == "MSFT"
    assert positions[0].name == "Microsoft"
    assert positions[0].broker == "csv"
    assert positions[0].quantity == 4
    assert positions[0].average_cost == 250
    assert positions[0].current_price == 300
    assert positions[0].sector == "Technology"


def test_csv_import_uses_broker_column_when_present():
    csv_text = (
        "symbol,name,broker,asset_type,quantity,average_cost,current_price\n"
        "CASH,Cash,etrade,cash,100,1,1\n"
        "CASH,Cash,robinhood,cash,200,1,1\n"
    )

    positions = parse_positions_csv(csv_text, broker="csv")

    assert [position.broker for position in positions] == ["etrade", "robinhood"]
