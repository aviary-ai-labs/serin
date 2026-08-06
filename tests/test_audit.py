from __future__ import annotations

from datetime import UTC, datetime, timedelta

from backend import audit, db
from backend.main import app
from backend.models import PositionIn, TaxLotIn
from fastapi.testclient import TestClient


def _issue_codes(report):
    return {issue["code"] for issue in report["issues"]}


def test_audit_returns_clean_state_without_positions(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()

    report = audit.audit_portfolio()

    assert report["status"] == "clean"
    assert report["total_issues"] == 0
    assert report["positions_checked"] == 0


def test_audit_detects_core_data_quality_issues(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    db.create_position(
        PositionIn(
            symbol="AAPL",
            broker="etrade",
            asset_type="stock",
            quantity=5,
            average_cost=0,
            current_price=200,
            sector="",
        )
    )
    db.create_position(
        PositionIn(
            symbol="GOOG",
            broker="manual",
            asset_type="stock",
            quantity=10,
            average_cost=100,
            current_price=120,
            sector="Communication Services",
        )
    )
    db.create_position(
        PositionIn(
            symbol="MSFTCALL",
            broker="manual",
            asset_type="option",
            quantity=1,
            average_cost=2,
            current_price=3,
        )
    )
    db.create_position(
        PositionIn(
            symbol="CASH",
            broker="manual",
            asset_type="cash",
            quantity=10000,
            average_cost=1,
            current_price=1,
        )
    )
    db.create_tax_lot(
        TaxLotIn(
            symbol="GOOG",
            broker="manual",
            quantity=3,
            cost_basis=100,
            acquired_at="2026-01-01",
        )
    )

    stale = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    with db.connect() as conn:
        conn.execute("UPDATE positions SET updated_at=? WHERE symbol='AAPL'", (stale,))

    report = audit.audit_portfolio()
    codes = _issue_codes(report)

    assert report["status"] == "high_risk"
    assert report["issue_counts"]["critical"] == 1
    assert "missing_cost_basis" in codes
    assert "missing_sector" in codes
    assert "stale_price" in codes
    assert "option_symbol_format" in codes
    assert "tax_lot_quantity_mismatch" in codes
    assert "high_cash_allocation" in codes


def test_audit_endpoint_returns_report(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()

    client = TestClient(app)
    response = client.get("/api/audit")

    assert response.status_code == 200
    assert response.json()["status"] == "clean"
