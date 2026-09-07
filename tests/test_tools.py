"""The agent tool layer — registry behaviour and each tool's contract.

Two properties carry most of the weight here. The registry is **read-only by
construction**, so no future tool can quietly gain the ability to mutate a
portfolio; and tools **answer questions rather than returning tables**, because
a model handed forty rows will sum them itself and be subtly wrong about
somebody's money.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from backend import db, tools
from backend.models import PositionIn, TaxLotIn, TransactionIn


@pytest.fixture
def book(tmp_path):
    db.set_db_path(tmp_path / "tools.db")
    db.init_db()
    db.create_position(PositionIn(
        symbol="AAPL", name="Apple", broker="robinhood", asset_type="stock",
        quantity=100, average_cost=100.0, current_price=150.0, sector="Technology",
    ))
    db.create_position(PositionIn(
        symbol="AAPL", name="Apple", broker="fidelity", asset_type="stock",
        quantity=50, average_cost=120.0, current_price=150.0, sector="Technology",
    ))
    db.create_position(PositionIn(
        symbol="VTI", name="Vanguard Total", broker="fidelity", asset_type="etf",
        quantity=10, average_cost=200.0, current_price=220.0, sector="Diversified",
    ))
    db.create_position(PositionIn(
        symbol="CASH", name="Cash", broker="robinhood", asset_type="cash",
        quantity=5000, average_cost=1.0, current_price=1.0,
    ))
    db.create_transaction(TransactionIn(
        symbol="AAPL", broker="robinhood", action="buy",
        quantity=100, price=100.0, occurred_at="2025-01-15",
    ))
    db.create_transaction(TransactionIn(
        symbol="AAPL", broker="robinhood", action="sell",
        quantity=40, price=140.0, occurred_at="2025-06-20",
    ))
    return db


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_the_registry_refuses_a_write_tool():
    """The read-only guarantee is structural, not a convention to remember."""
    writer = tools.Tool(
        name="delete_everything",
        description="no",
        input_schema={"type": "object", "properties": {}},
        handler=lambda: None,
        read_only=False,
    )

    with pytest.raises(ValueError, match="read-only"):
        tools.register(writer)

    assert tools.get("delete_everything") is None


def test_every_registered_tool_is_read_only():
    assert all(tool.read_only for tool in tools.all_tools())


def test_duplicate_registration_is_refused():
    existing = tools.all_tools()[0]
    clone = tools.Tool(
        name=existing.name,
        description="shadow",
        input_schema={"type": "object", "properties": {}},
        handler=lambda: None,
    )

    with pytest.raises(ValueError, match="already registered"):
        tools.register(clone)


def test_describe_is_mcp_shaped():
    for entry in tools.describe():
        assert set(entry) == {"name", "description", "inputSchema"}
        assert entry["inputSchema"]["type"] == "object"
        assert isinstance(entry["description"], str) and entry["description"]


def test_the_expected_tools_are_registered():
    assert {tool.name for tool in tools.all_tools()} == {
        "find_data_gaps",
        "get_performance",
        "get_portfolio_summary",
        "get_position",
        "get_price_history",
        "get_realized_gains",
        "list_positions",
        "list_transactions",
    }


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


def test_unknown_tool_lists_what_exists():
    with pytest.raises(tools.ToolError) as excinfo:
        tools.call("get_the_future")

    assert "get_the_future" in str(excinfo.value)
    assert "get_portfolio_summary" in str(excinfo.value)


def test_unknown_argument_is_an_error_not_silently_dropped(book):
    """Dropping it would answer a different question and label it the asked one."""
    with pytest.raises(tools.ToolError, match="Unknown argument"):
        tools.call("get_realized_gains", {"yr": "2025"})


def test_missing_required_argument_is_reported(book):
    with pytest.raises(tools.ToolError, match="requires symbol"):
        tools.call("get_position", {})


def test_enum_violation_is_reported(book):
    with pytest.raises(tools.ToolError, match="must be one of"):
        tools.call("get_performance", {"period": "SINCE_FOREVER"})


def test_numeric_strings_are_coerced(book):
    """Models send "5", not 5. Rejecting that is correct and useless."""
    result = tools.call("list_positions", {"limit": "2"})

    assert result["returned"] == 2


def test_boolean_strings_are_coerced(book):
    result = tools.call("list_positions", {"include_closed": "false"})

    assert result["count"] >= 1


def test_uncoercible_value_is_reported(book):
    with pytest.raises(tools.ToolError, match="must be a integer"):
        tools.call("list_positions", {"limit": "lots"})


def test_handler_failure_becomes_a_tool_error(book, monkeypatch):
    monkeypatch.setattr(db, "portfolio_summary", lambda: 1 / 0)

    with pytest.raises(tools.ToolError, match="get_portfolio_summary failed"):
        tools.call("get_portfolio_summary")


# ---------------------------------------------------------------------------
# get_portfolio_summary
# ---------------------------------------------------------------------------


def test_portfolio_summary_computes_totals(book):
    result = tools.call("get_portfolio_summary")

    # 100×150 + 50×150 + 10×220 + 5000 cash
    assert result["total_value"] == pytest.approx(15000 + 7500 + 2200 + 5000)
    assert result["cash_value"] == pytest.approx(5000)
    assert result["position_count"] == 4


def test_portfolio_summary_answers_rather_than_listing(book):
    """The summary must not become a position dump with extra steps."""
    result = tools.call("get_portfolio_summary", {"top_holdings": 2})

    assert len(result["top_holdings"]) == 2
    assert "positions" not in result
    assert result["top_holdings"][0]["market_value"] >= result["top_holdings"][1]["market_value"]


def test_portfolio_summary_weights_are_percentages_of_the_whole(book):
    result = tools.call("get_portfolio_summary")

    top = result["top_holdings"][0]
    assert top["weight_pct"] == pytest.approx(top["market_value"] / result["total_value"] * 100, rel=1e-3)


def test_portfolio_summary_excludes_cash_from_top_holdings(book):
    result = tools.call("get_portfolio_summary")

    assert "CASH" not in {h["symbol"] for h in result["top_holdings"]}


# ---------------------------------------------------------------------------
# Freshness — a stale price quoted as current is the failure that matters
# ---------------------------------------------------------------------------


def test_fresh_prices_report_no_staleness(book):
    result = tools.call("get_portfolio_summary")

    assert result["freshness"]["stale_positions"] == 0
    assert result["freshness"]["prices_updated_at"] is not None


def test_old_prices_are_named_as_stale(book):
    old = (datetime.now(UTC) - timedelta(days=9)).isoformat()
    with db.connect() as conn:
        conn.execute("UPDATE positions SET updated_at=? WHERE symbol='VTI'", (old,))

    freshness = tools.call("get_portfolio_summary")["freshness"]

    assert freshness["stale_positions"] == 1
    assert freshness["stale_symbols"] == ["VTI"]


# ---------------------------------------------------------------------------
# get_position
# ---------------------------------------------------------------------------


def test_get_position_aggregates_across_brokers(book):
    result = tools.call("get_position", {"symbol": "AAPL"})

    assert result["quantity"] == pytest.approx(150)
    assert result["market_value"] == pytest.approx(22500)
    assert {row["broker"] for row in result["held_at"]} == {"robinhood", "fidelity"}


def test_get_position_normalises_the_ticker(book):
    assert tools.call("get_position", {"symbol": "  aapl "})["symbol"] == "AAPL"


def test_get_position_reports_a_missing_symbol(book):
    with pytest.raises(tools.ToolError, match="No position found for TSLA"):
        tools.call("get_position", {"symbol": "TSLA"})


def test_get_position_includes_tax_lots(book):
    db.create_tax_lot(TaxLotIn(
        symbol="AAPL", broker="robinhood", quantity=100,
        cost_basis=100.0, acquired_at="2025-01-15",
    ))

    result = tools.call("get_position", {"symbol": "AAPL"})

    assert len(result["tax_lots"]) == 1
    assert result["tax_lots"][0]["holding_period"] in {"short-term", "long-term"}


# ---------------------------------------------------------------------------
# list_positions
# ---------------------------------------------------------------------------


def test_list_positions_filters_by_broker(book):
    result = tools.call("list_positions", {"broker": "fidelity"})

    assert {p["broker"] for p in result["positions"]} == {"fidelity"}
    assert result["count"] == 2


def test_list_positions_filters_by_asset_type(book):
    result = tools.call("list_positions", {"asset_type": "etf"})

    assert {p["symbol"] for p in result["positions"]} == {"VTI"}


def test_list_positions_is_capped(book):
    result = tools.call("list_positions", {"limit": 10_000})

    assert result["returned"] <= tools.portfolio.MAX_ROWS


def test_list_positions_is_largest_first(book):
    values = [p["market_value"] for p in tools.call("list_positions")["positions"]]

    assert values == sorted(values, reverse=True)


# ---------------------------------------------------------------------------
# Realized gains / transactions
# ---------------------------------------------------------------------------


def test_realized_gains_states_its_matching_assumption(book):
    """A broker on specific-lot will disagree; the answer has to say so."""
    result = tools.call("get_realized_gains")

    assert "FIFO" in result["basis"]
    assert "available_years" in result


def test_realized_gains_filters_by_year(book):
    result = tools.call("get_realized_gains", {"year": "2025"})

    assert isinstance(result.get("available_years"), list)


def test_list_transactions_filters_and_caps(book):
    result = tools.call("list_transactions", {"symbol": "aapl", "action": "sell"})

    assert result["returned"] == 1
    assert result["transactions"][0]["action"] == "sell"
    assert result["limit"] <= tools.portfolio.MAX_ROWS


def test_list_transactions_cap_survives_a_huge_limit(book):
    result = tools.call("list_transactions", {"limit": 100_000})

    assert result["limit"] == tools.portfolio.MAX_ROWS


# ---------------------------------------------------------------------------
# Price history
# ---------------------------------------------------------------------------


def test_price_history_precomputes_the_summary(book):
    db.cache_price_history({"AAPL": {
        "dates": ["2026-01-02", "2026-01-03", "2026-01-06"],
        "closes": [100.0, 120.0, 110.0],
    }})

    result = tools.call("get_price_history", {"symbol": "AAPL"})

    assert result["points"] == 3
    assert result["first_close"] == 100.0
    assert result["last_close"] == 110.0
    assert result["high"] == 120.0
    assert result["low"] == 100.0
    assert result["change_pct"] == pytest.approx(10.0)


def test_price_history_honours_start_date(book):
    db.cache_price_history({"AAPL": {
        "dates": ["2026-01-02", "2026-01-03", "2026-01-06"],
        "closes": [100.0, 120.0, 110.0],
    }})

    result = tools.call("get_price_history", {"symbol": "AAPL", "start_date": "2026-01-03"})

    assert result["points"] == 2
    assert result["start_date"] == "2026-01-03"


def test_price_history_says_when_it_has_nothing(book):
    with pytest.raises(tools.ToolError, match="No cached price history for NVDA"):
        tools.call("get_price_history", {"symbol": "NVDA"})


# ---------------------------------------------------------------------------
# Data gaps
# ---------------------------------------------------------------------------


def test_find_data_gaps_returns_actionable_shape(book):
    result = tools.call("find_data_gaps")

    assert set(result) >= {"complete", "gaps", "note", "value_affected"}
    assert isinstance(result["gaps"], list)
