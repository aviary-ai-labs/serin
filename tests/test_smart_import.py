"""Smart import — extraction validation, parsing tolerance, bulk insert."""

from __future__ import annotations

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
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "cost_usd": 0.0009,
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
    assert body["model"] == "deepseek-v4-flash"


def test_extract_endpoint_requires_input():
    client = TestClient(main.app)
    r = client.post("/api/import/extract")
    assert r.status_code == 400
