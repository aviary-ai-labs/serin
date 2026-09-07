"""The transactions view: paging, filtering, correcting, deleting.

Until this existed you could import a broker export and then never see what
landed. 201 rows went into the ledger with no way to list them, no way to fix a
misread one, and no way to undo the import — which made a wrong row permanent
and invisible at the same time. These tests pin the properties that make the
view trustworthy rather than merely present.
"""

from __future__ import annotations

import pytest
from backend import db
from backend.main import app
from backend.models import TransactionIn
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    db.set_db_path(tmp_path / "txn.db")
    db.init_db()
    return TestClient(app)


def add(client, **kw):
    body = {"symbol": "TQQQ", "broker": "robinhood", "action": "buy", "quantity": 10,
            "price": 50.0, "occurred_at": "2026-03-01", "asset_type": "stock"}
    body.update(kw)
    response = client.post("/api/v1/transactions", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# --- paging ----------------------------------------------------------------


def test_a_page_reports_the_total_it_is_a_page_of(client):
    """A table showing the first 200 of 512 rows without saying so is worse
    than one that admits it — the missing 312 look like they never imported."""
    for i in range(25):
        add(client, occurred_at=f"2026-03-{i + 1:02d}")
    payload = client.get("/api/v1/transactions?limit=10").json()
    assert len(payload["transactions"]) == 10
    assert payload["total"] == 25
    assert payload["limit"] == 10 and payload["offset"] == 0


def test_offset_walks_the_ledger_without_repeating_or_skipping(client):
    for i in range(25):
        add(client, occurred_at=f"2026-03-{i + 1:02d}")
    seen = []
    for offset in (0, 10, 20):
        page = client.get(f"/api/v1/transactions?limit=10&offset={offset}").json()
        seen.extend(t["id"] for t in page["transactions"])
    assert len(seen) == 25
    assert len(set(seen)) == 25, "paging repeated a row"


def test_limit_is_clamped_so_one_request_cannot_pull_the_whole_ledger(client):
    add(client)
    assert client.get("/api/v1/transactions?limit=100000").json()["limit"] == 500
    assert client.get("/api/v1/transactions?limit=0").json()["limit"] == 1


# --- filtering -------------------------------------------------------------


def test_filters_compose(client):
    add(client, symbol="TQQQ", action="buy", occurred_at="2026-03-01")
    add(client, symbol="TQQQ", action="sell", occurred_at="2026-05-01")
    add(client, symbol="HOOD", action="buy", occurred_at="2026-05-01")
    got = client.get("/api/v1/transactions?symbol=TQQQ&action=sell").json()
    assert got["total"] == 1
    assert got["transactions"][0]["symbol"] == "TQQQ"


def test_the_until_filter_includes_the_whole_of_its_last_day(client):
    """occurred_at can carry a time. Comparing it whole against a bare date
    silently drops everything that happened on the boundary day — the most
    recent rows, which is exactly where someone looks first."""
    add(client, occurred_at="2026-08-20T15:30:00")
    add(client, occurred_at="2026-08-21")
    got = client.get("/api/v1/transactions?until=2026-08-20").json()
    assert got["total"] == 1, "a timestamped row was dropped by its own end date"


def test_since_and_until_bracket_a_range(client):
    for day in ("2026-01-05", "2026-03-05", "2026-06-05"):
        add(client, occurred_at=day)
    got = client.get("/api/v1/transactions?since=2026-02-01&until=2026-04-01").json()
    assert got["total"] == 1


def test_facets_are_read_from_the_data_not_the_type(client):
    """A ledger imported from one broker should not offer a menu of twelve
    others, and a symbol never traded is a filter that can only return zero."""
    add(client, symbol="TQQQ", broker="robinhood")
    add(client, symbol="HOOD", broker="robinhood", action="dividend")
    facets = client.get("/api/v1/transactions/facets").json()
    assert facets["symbols"] == ["HOOD", "TQQQ"]
    assert facets["brokers"] == ["robinhood"]
    assert set(facets["actions"]) == {"buy", "dividend"}


def test_a_cash_row_with_no_symbol_does_not_become_a_blank_filter_option(client):
    add(client, symbol="", action="interest", quantity=0, price=0)
    assert client.get("/api/v1/transactions/facets").json()["symbols"] == []


# --- correcting ------------------------------------------------------------


def test_editing_recomputes_the_signed_cash_impact(client):
    """The whole reason edit exists. A buy read as a sell has the wrong sign on
    its cash impact, and carrying the old amount through an edit would leave
    every return built on it wrong while the row on screen looked right."""
    created = add(client, action="buy", quantity=10, price=50.0)
    assert created["amount"] < 0
    fixed = client.put(
        f"/api/v1/transactions/{created['id']}",
        json={"symbol": "TQQQ", "broker": "robinhood", "action": "sell",
              "quantity": 10, "price": 50.0, "occurred_at": "2026-03-01"},
    ).json()
    assert fixed["action"] == "sell"
    assert fixed["amount"] > 0, "cash impact kept the old direction after an edit"


def test_editing_does_not_free_the_row_to_be_imported_again(client):
    """external_id identifies the source row. Editing a misread value must not
    make the same statement importable a second time."""
    row = db.create_transaction(
        TransactionIn(symbol="TQQQ", action="buy", quantity=1, price=1,
                      occurred_at="2026-03-01"),
        source="import", external_id="rh:abc123",
    )
    db.update_transaction(
        row.id,
        TransactionIn(symbol="TQQQ", action="dividend", quantity=0, price=5,
                      occurred_at="2026-03-01"),
    )
    again = db.create_transaction(
        TransactionIn(symbol="TQQQ", action="buy", quantity=1, price=1,
                      occurred_at="2026-03-01"),
        source="import", external_id="rh:abc123",
    )
    assert again is None, "an edited row let its source statement re-import"


def test_editing_a_row_that_does_not_exist_is_404_not_a_silent_no_op(client):
    response = client.put(
        "/api/v1/transactions/99999",
        json={"symbol": "X", "action": "buy", "quantity": 1, "price": 1,
              "occurred_at": "2026-03-01"},
    )
    assert response.status_code == 404


def test_one_users_edit_cannot_reach_anothers_row(client):
    """Every query is scoped; an id guessed from another tenant must miss."""
    from backend import scope

    mine = add(client)
    with scope.using("someone-else"):
        assert db.update_transaction(
            mine["id"],
            TransactionIn(symbol="EVIL", action="sell", quantity=1, price=1,
                          occurred_at="2026-03-01"),
        ) is None
    assert client.get("/api/v1/transactions").json()["transactions"][0]["symbol"] == "TQQQ"


# --- deleting --------------------------------------------------------------


def test_delete_removes_the_row_and_the_count_follows(client):
    created = add(client)
    add(client, symbol="HOOD")
    assert client.delete(f"/api/v1/transactions/{created['id']}").status_code == 200
    remaining = client.get("/api/v1/transactions").json()
    assert remaining["total"] == 1
    assert remaining["transactions"][0]["symbol"] == "HOOD"


def test_deleting_twice_is_404_the_second_time(client):
    created = add(client)
    assert client.delete(f"/api/v1/transactions/{created['id']}").status_code == 200
    assert client.delete(f"/api/v1/transactions/{created['id']}").status_code == 404
