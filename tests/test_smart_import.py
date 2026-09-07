"""Smart import — extraction validation, parsing tolerance, bulk insert."""

from __future__ import annotations

import base64

from backend import db, main, smart_import
from fastapi.testclient import TestClient


def _fresh(tmp_path):
    db.set_db_path(tmp_path / "smart.db")
    db.init_db()


# --- parsing tolerance -----------------------------------------------------

def test_strip_fences_handles_markdown():
    assert smart_import._strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert smart_import._strip_fences('```\n{"a": 1}\n```') == '{"a": 1}'
    assert smart_import._strip_fences('{"a": 1}') == '{"a": 1}'


def test_parse_response_extracts_object_from_prose():
    # Some models wrap JSON in commentary; the parser should recover.
    raw = 'Here is the result:\n\n{"positions": [{"symbol": "AAPL", "quantity": 10}]}\n\nLet me know if you need more.'
    parsed = smart_import._parse_response(raw)
    assert parsed["positions"][0]["symbol"] == "AAPL"


def test_parse_response_raises_on_garbage():
    import pytest
    with pytest.raises(RuntimeError):
        smart_import._parse_response("not json at all")


# --- row normalization -----------------------------------------------------

def test_normalize_drops_blank_symbols():
    assert smart_import._normalize_row({"symbol": "", "quantity": 10}) is None
    assert smart_import._normalize_row({"symbol": " " * 30}) is None


def test_normalize_coerces_numbers_and_defaults():
    row = smart_import._normalize_row({
        "symbol": "aapl",
        "quantity": "10",
        "average_cost": "170.5",
        "broker": "Schwab Brokerage",
    })
    assert row["symbol"] == "AAPL"
    assert row["quantity"] == 10.0
    assert row["average_cost"] == 170.5
    assert row["broker"] == "schwab_brokerage"
    assert row["asset_type"] == "stock"


def test_normalize_unknown_asset_type_defaults_to_stock():
    row = smart_import._normalize_row({"symbol": "X", "quantity": 1, "asset_type": "weird"})
    assert row["asset_type"] == "stock"


def test_normalize_clamps_negative_numbers():
    row = smart_import._normalize_row({"symbol": "X", "quantity": -5, "average_cost": -3})
    assert row["quantity"] == 0.0
    assert row["average_cost"] == 0.0


def test_normalize_tax_lot_screenshot_as_one_holding_with_three_lots():
    """The Fidelity tax-lot screenshot must not become four NFLX positions."""
    rows = smart_import._normalize_positions({
        "positions": [
            {
                "symbol": "NFLX",
                "name": "Netflix Inc.",
                "broker": "fidelity",
                "quantity": 650,
                "average_cost": 87.90,
                "current_price": 80.22,
                "asset_type": "stock",
                "tax_lots": [
                    {"quantity": 300, "cost_basis": 95.00, "acquired_at": "12/18/25"},
                    {"quantity": 200, "cost_basis": 83.18, "acquired_at": "01/21/26"},
                    {"quantity": 150, "cost_basis": 80.00, "acquired_at": "02/03/26"},
                ],
            }
        ]
    })
    assert len(rows) == 1
    assert rows[0]["quantity"] == 650
    assert rows[0]["average_cost"] == 87.90
    assert rows[0]["current_price"] == 80.22
    assert rows[0]["tax_lots"] == [
        {"quantity": 300, "cost_basis": 95, "acquired_at": "2025-12-18"},
        {"quantity": 200, "cost_basis": 83.18, "acquired_at": "2026-01-21"},
        {"quantity": 150, "cost_basis": 80, "acquired_at": "2026-02-03"},
    ]
    assert smart_import._row_warnings(rows[0], set()) == []


def test_legacy_tax_lot_rows_collapse_under_aggregate_position():
    """Tolerate providers that still emit the dated rows as positions."""
    rows = smart_import._normalize_positions({
        "positions": [
            {"symbol": "NFLX", "broker": "fidelity", "quantity": 650, "average_cost": 87.9},
            {"symbol": "NFLX", "broker": "fidelity", "quantity": 300, "average_cost": 95, "acquired_at": "12/18/25"},
            {"symbol": "NFLX", "broker": "fidelity", "quantity": 200, "average_cost": 83.18, "acquired_at": "01/21/26"},
            {"symbol": "NFLX", "broker": "fidelity", "quantity": 150, "average_cost": 80, "acquired_at": "02/03/26"},
        ]
    })
    assert len(rows) == 1
    assert rows[0]["quantity"] == 650
    assert [lot["quantity"] for lot in rows[0]["tax_lots"]] == [300, 200, 150]


# --- deterministic warnings ------------------------------------------------

def test_warnings_flag_zero_quantity():
    row = smart_import._normalize_row({"symbol": "AAPL", "quantity": 0})
    warnings = smart_import._row_warnings(row, existing_keys=set())
    assert any("zero" in w for w in warnings)


def test_warnings_flag_suspiciously_high_price():
    row = smart_import._normalize_row({"symbol": "AAPL", "quantity": 10, "average_cost": 99999})
    warnings = smart_import._row_warnings(row, existing_keys=set())
    assert any("50k" in w for w in warnings)


def test_warnings_flag_duplicates(tmp_path):
    _fresh(tmp_path)
    existing = {("AAPL", "manual", "stock")}
    row = smart_import._normalize_row({"symbol": "AAPL", "quantity": 10, "broker": "manual"})
    warnings = smart_import._row_warnings(row, existing)
    assert any("dup" in w.lower() or "overwrite" in w.lower() for w in warnings)


def test_cash_rows_skip_quantity_warnings():
    row = smart_import._normalize_row({
        "symbol": "CASH", "quantity": 5000, "average_cost": 1, "asset_type": "cash"
    })
    warnings = smart_import._row_warnings(row, existing_keys=set())
    assert warnings == []  # high cash balance is fine


def test_warnings_flag_tax_lot_quantity_mismatch():
    row = smart_import._normalize_row({
        "symbol": "NFLX",
        "quantity": 650,
        "tax_lots": [
            {"quantity": 300, "cost_basis": 95, "acquired_at": "2025-12-18"},
            {"quantity": 200, "cost_basis": 83.18, "acquired_at": "2026-01-21"},
        ],
    })
    warnings = smart_import._row_warnings(row, existing_keys=set())
    assert any("tax lots total 500 shares" in warning for warning in warnings)


# --- bulk insert -----------------------------------------------------------

def test_bulk_insert_creates_new_positions(tmp_path):
    _fresh(tmp_path)
    result = smart_import.bulk_insert(
        [
            {"symbol": "AAPL", "quantity": 10, "average_cost": 170},
            {"symbol": "MSFT", "quantity": 5, "average_cost": 410, "broker": "fidelity"},
        ]
    )
    assert result["inserted"] == 2
    assert result["skipped"] == 0
    assert {p.symbol for p in db.list_positions()} == {"AAPL", "MSFT"}


def test_bulk_insert_skips_duplicates_when_replace_false(tmp_path):
    _fresh(tmp_path)
    smart_import.bulk_insert([{"symbol": "AAPL", "quantity": 10, "average_cost": 170}])
    result = smart_import.bulk_insert(
        [{"symbol": "AAPL", "quantity": 99, "average_cost": 9999}]
    )
    assert result["inserted"] == 0
    assert result["skipped"] == 1
    aapl = next(p for p in db.list_positions() if p.symbol == "AAPL")
    assert aapl.quantity == 10  # original kept


def test_bulk_insert_replaces_when_flag_set(tmp_path):
    _fresh(tmp_path)
    smart_import.bulk_insert([{"symbol": "AAPL", "quantity": 10, "average_cost": 170}])
    result = smart_import.bulk_insert(
        [{"symbol": "AAPL", "quantity": 20, "average_cost": 175}], replace=True
    )
    assert result["inserted"] == 1
    aapl = next(p for p in db.list_positions() if p.symbol == "AAPL")
    assert aapl.quantity == 20


def test_bulk_insert_rejects_invalid_rows(tmp_path):
    _fresh(tmp_path)
    result = smart_import.bulk_insert(
        [
            {"symbol": "", "quantity": 10},  # blank symbol fails validation
            {"symbol": "GOOD", "quantity": 5, "average_cost": 100},
        ]
    )
    assert result["inserted"] == 1
    assert result["skipped"] == 1
    assert db.list_positions()[0].symbol == "GOOD"


def test_bulk_insert_creates_tax_lots_with_position(tmp_path):
    _fresh(tmp_path)
    result = smart_import.bulk_insert([{
        "symbol": "NFLX",
        "name": "Netflix Inc.",
        "broker": "fidelity",
        "asset_type": "stock",
        "quantity": 650,
        "average_cost": 87.90,
        "current_price": 80.22,
        "tax_lots": [
            {"quantity": 300, "cost_basis": 95.00, "acquired_at": "2025-12-18"},
            {"quantity": 200, "cost_basis": 83.18, "acquired_at": "2026-01-21"},
            {"quantity": 150, "cost_basis": 80.00, "acquired_at": "2026-02-03"},
        ],
    }])
    assert result["inserted"] == 1
    assert result["tax_lots_inserted"] == 3
    assert result["tax_lots_skipped"] == 0
    lots = db.list_tax_lots(symbol="NFLX", broker="fidelity")
    assert {(lot.acquired_at, lot.quantity, lot.cost_basis) for lot in lots} == {
        ("2025-12-18", 300, 95),
        ("2026-01-21", 200, 83.18),
        ("2026-02-03", 150, 80),
    }


def test_bulk_insert_adds_new_lots_to_existing_position_without_replacing(tmp_path):
    _fresh(tmp_path)
    smart_import.bulk_insert([{
        "symbol": "NFLX", "broker": "fidelity", "quantity": 650, "average_cost": 87.9,
    }])
    row = {
        "symbol": "NFLX",
        "broker": "fidelity",
        "quantity": 650,
        "average_cost": 87.9,
        "tax_lots": [{"quantity": 300, "cost_basis": 95, "acquired_at": "2025-12-18"}],
    }
    first = smart_import.bulk_insert([row])
    second = smart_import.bulk_insert([row])
    assert first["inserted"] == 0
    assert first["skipped"] == 1
    assert first["tax_lots_inserted"] == 1
    assert second["tax_lots_inserted"] == 0
    assert second["tax_lots_skipped"] == 1
    assert len(db.list_tax_lots(symbol="NFLX", broker="fidelity")) == 1


# --- API surface (no LLM call — we mock smart_import.extract) -------------

def test_bulk_endpoint(tmp_path):
    _fresh(tmp_path)
    client = TestClient(main.app)
    r = client.post(
        "/api/positions/bulk",
        json={"rows": [{"symbol": "tsla", "quantity": 3, "average_cost": 200}]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["inserted"] == 1
    assert body["skipped"] == 0


def test_bulk_endpoint_rejects_empty(tmp_path):
    _fresh(tmp_path)
    client = TestClient(main.app)
    r = client.post("/api/positions/bulk", json={"rows": []})
    assert r.status_code == 400


def test_extract_endpoint_routes_through_smart_import(tmp_path, monkeypatch):
    """Without a real API key, the endpoint should still wire through and
    surface a useful error rather than crashing."""
    _fresh(tmp_path)

    async def fake_extract(**kwargs):
        return {
            "rows": [
                {
                    "symbol": "AAPL", "name": "Apple", "broker": "manual",
                    "asset_type": "stock", "quantity": 10, "average_cost": 170,
                    "current_price": 0, "sector": "", "warnings": [],
                }
            ],
            "row_count": 1,
            "notes": "",
            "transactions": [],
            "transaction_count": 0,
            "notice": "Test provider notice",
        }

    monkeypatch.setattr(smart_import, "extract", fake_extract)
    client = TestClient(main.app)
    r = client.post(
        "/api/v1/import/extract",
        files={"file": ("portfolio.csv", b"symbol,qty\nAAPL,10\n", "text/csv")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 1
    assert body["rows"][0]["symbol"] == "AAPL"
    # Model, provider and cost are deliberately absent: the import screen is
    # for reviewing what was read out of a statement, not model telemetry.
    assert "model" not in body
    assert "provider" not in body
    assert "cost_usd" not in body


def test_extract_endpoint_requires_input():
    client = TestClient(main.app)
    r = client.post("/api/import/extract")
    assert r.status_code == 400


def test_extract_endpoint_accepts_json_file_fallback(tmp_path, monkeypatch):
    """Mobile Safari's multipart fallback must reach extraction with the
    original bytes, MIME type, filename-derived image classification, and hint."""
    _fresh(tmp_path)
    captured = {}

    async def fake_extract(**kwargs):
        captured.update(kwargs)
        return {"rows": [], "row_count": 0, "provider": "test", "model": "test", "cost_usd": 0}

    monkeypatch.setattr(smart_import, "extract", fake_extract)
    image = b"\xff\xd8\xff\xe0fake-jpeg"
    client = TestClient(main.app)
    r = client.post(
        "/api/v1/import/extract",
        json={
            "filename": "tax-lots.jpg",
            "content_type": "image/jpeg",
            "file_base64": base64.b64encode(image).decode("ascii"),
            "hint": "broker: Fidelity",
        },
    )
    assert r.status_code == 200, r.text
    assert captured["image_bytes"] == image
    assert captured["image_mime"] == "image/jpeg"
    assert captured["hint"] == "broker: Fidelity"
    assert captured["text"] is None


def test_extract_endpoint_rejects_invalid_json_file():
    client = TestClient(main.app)
    r = client.post(
        "/api/v1/import/extract",
        json={"filename": "broken.png", "content_type": "image/png", "file_base64": "not-base64!"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "Could not decode the uploaded file."


def test_bulk_insert_reprices_from_the_quote_cache(tmp_path):
    """A statement is authoritative about holdings, never about today's
    price — imported rows must immediately pick up the freshest known quote
    (the real case: a screenshot's months-old prices sat on the rows until
    someone happened to press Refresh)."""
    _fresh(tmp_path)
    db.cache_quotes([("HOOD", "stock", 95.56, "Financial Services")])
    result = smart_import.bulk_insert(
        [{"symbol": "HOOD", "quantity": 388, "average_cost": 117, "current_price": 69.36}]
    )
    assert result["refreshed"] == 1
    position = next(p for p in db.list_positions() if p.symbol == "HOOD")
    assert position.current_price == 95.56


def test_bulk_insert_survives_a_failing_price_refresh(tmp_path, monkeypatch):
    """Pricing is a courtesy on top of the import — its failure must never
    fail the commit that just succeeded."""
    from backend import prices

    _fresh(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(prices, "refresh_prices", boom)
    result = smart_import.bulk_insert(
        [{"symbol": "AAPL", "quantity": 10, "average_cost": 170, "current_price": 150}]
    )
    assert result["inserted"] == 1
    assert result["refreshed"] == 0
