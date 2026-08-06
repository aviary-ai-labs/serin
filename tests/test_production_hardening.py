"""Production hardening: app lock, backup/restore, migrations, journey smoke."""

from __future__ import annotations

import sqlite3

import pytest
from backend import backup, db, secrets_store
from backend.config import settings
from backend.main import app
from backend.models import PositionIn, TaxLotIn, TransactionIn
from fastapi.testclient import TestClient


@pytest.fixture
def fresh(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    secrets_store.reset_key_cache()
    yield tmp_path
    secrets_store.reset_key_cache()


# --- app lock -----------------------------------------------------------------

def test_auth_disabled_by_default_everything_open(fresh, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", "")
    client = TestClient(app)
    assert client.get("/api/portfolio").status_code == 200
    status = client.get("/api/auth/status").json()
    assert status == {"auth_enabled": False, "authorized": True}


def test_auth_locks_api_until_login(fresh, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", "hunter2")
    client = TestClient(app)

    assert client.get("/api/portfolio").status_code == 401
    # Public allowlist still reachable (health + the login endpoint itself).
    assert client.get("/api/v1/version").status_code == 200

    wrong = client.post("/api/auth/login", json={"password": "nope"})
    assert wrong.status_code == 401

    ok = client.post("/api/auth/login", json={"password": "hunter2"})
    assert ok.status_code == 200
    token = ok.json()["token"]
    assert token

    # Cookie session (web): TestClient keeps the cookie jar.
    assert client.get("/api/portfolio").status_code == 200

    # Bearer token (mobile): a fresh client with only the header.
    bare = TestClient(app)
    assert bare.get("/api/portfolio").status_code == 401
    assert bare.get("/api/portfolio", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_spa_shell_stays_public_when_locked(fresh, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", "hunter2")
    client = TestClient(app)
    # The shell (login screen) must stay reachable; only /api data is gated.
    # Self-host builds carry no landing page, so / answers with a redirect to
    # /app rather than a page of its own.
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (200, 302, 307)
    if response.status_code == 302:
        assert response.headers["location"] == "/app"


# --- backup / restore -----------------------------------------------------------

def _seed(fresh):
    db.create_position(PositionIn(symbol="AAPL", broker="manual", asset_type="stock",
                                  quantity=3, average_cost=100, current_price=180))
    db.create_tax_lot(TaxLotIn(symbol="AAPL", broker="manual", quantity=3, cost_basis=300,
                               acquired_at="2025-05-01"))
    db.create_transaction(TransactionIn(symbol="AAPL", action="buy", quantity=3, price=100,
                                        occurred_at="2025-05-01"))
    db.set_setting("display_currency", "EUR")


def test_backup_roundtrip(fresh):
    _seed(fresh)
    payload = backup.export_data()
    assert payload["serin_backup"] == 1
    assert len(payload["positions"]) == 1
    assert len(payload["transactions"]) == 1

    # Wipe, then restore.
    with db.connect() as conn:
        for table in ("positions", "tax_lots", "transactions"):
            conn.execute(f"DELETE FROM {table}")
    assert db.list_positions() == []

    counts = backup.restore_data(payload)
    assert counts["positions"] == 1
    restored = db.list_positions()[0]
    assert restored.symbol == "AAPL"
    assert restored.quantity == 3
    assert db.get_setting("display_currency") == "EUR"


def test_restore_rejects_garbage(fresh):
    with pytest.raises(ValueError):
        backup.restore_data({"nope": True})


def test_backup_endpoints(fresh):
    _seed(fresh)
    client = TestClient(app)

    dl = client.get("/api/backup")
    assert dl.status_code == 200
    assert "serin-backup-" in dl.headers["content-disposition"]

    csv_out = client.get("/api/backup/positions.csv")
    assert csv_out.status_code == 200
    assert "AAPL" in csv_out.text

    restored = client.post(
        "/api/restore",
        files={"file": ("backup.json", dl.content, "application/json")},
    )
    assert restored.status_code == 200
    assert restored.json()["restored"]["positions"] == 1


# --- migrations -------------------------------------------------------------------

def test_legacy_database_migrates_to_current_version(tmp_path):
    """A pre-versioning DB (no schema_version, old positions shape) upgrades in
    place and records every migration."""
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.executescript(
        """
        CREATE TABLE positions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          symbol TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
          broker TEXT NOT NULL DEFAULT 'manual', asset_type TEXT NOT NULL DEFAULT 'stock',
          quantity REAL NOT NULL DEFAULT 0, average_cost REAL NOT NULL DEFAULT 0,
          current_price REAL NOT NULL DEFAULT 0, sector TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL, UNIQUE(symbol, broker, asset_type)
        );
        CREATE TABLE briefings (
          id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL,
          input_snapshot_json TEXT NOT NULL DEFAULT '{}', summary TEXT NOT NULL DEFAULT '',
          output_markdown TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
          model_cost_usd REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
          completed_at TEXT, error TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO positions (symbol, updated_at) VALUES ('OLD', '2026-01-01T00:00:00Z');
        """
    )
    conn.close()

    db.set_db_path(legacy)
    db.init_db()

    assert db.schema_version() == db.MIGRATIONS[-1][0]  # fully migrated
    position = db.list_positions()[0]
    assert position.symbol == "OLD"
    assert position.currency == "USD"   # added by migration 4
    assert position.source == "manual"  # added by migration 3


# --- journey smoke ------------------------------------------------------------------

def test_full_api_journey(fresh, monkeypatch):
    """Import → refresh → history/analytics → backup → restore, end to end."""
    from backend.providers import fmp as fmp_provider

    monkeypatch.setattr(settings, "market_data_provider", "fmp")
    monkeypatch.setattr(settings, "fmp_api_key", "test-key")
    monkeypatch.setattr(settings, "auth_password", "")
    client = TestClient(app)

    # 1. Smart-import commit path (bulk insert of user-confirmed rows).
    bulk = client.post("/api/v1/positions/bulk", json={
        "rows": [{"symbol": "AAPL", "quantity": 5, "average_cost": 100,
                  "broker": "manual", "asset_type": "stock"}],
        "replace": False,
    })
    assert bulk.status_code == 200
    assert bulk.json()["inserted"] == 1

    # 2. Record the buy so real returns activate.
    txn = client.post("/api/transactions", json={
        "symbol": "AAPL", "action": "buy", "quantity": 5, "price": 100,
        "occurred_at": "2026-06-01",
    })
    assert txn.status_code == 200

    # 3. Price refresh via the (mocked) provider.
    def fake_get(path, params, *args, **kwargs):
        if path == "stable/profile":
            return [{"price": 190.0, "sector": "Technology"}], None
        return [
            {"date": "2026-06-27", "price": 185.0},
            {"date": "2026-06-30", "price": 190.0},
        ], None

    monkeypatch.setattr(fmp_provider, "_get", fake_get)
    refreshed = client.post("/api/prices/refresh", json={})
    assert refreshed.status_code == 200
    assert refreshed.json()["updated"] == 1

    # 4. History lands in the cache; analytics sees it.
    history = client.get("/api/price-history?period=1w").json()
    assert "AAPL" in history["history"]
    performance = client.get("/api/v1/performance").json()
    assert performance["accurate"]["available"] is True

    # 5. Backup, wipe, restore — data survives.
    dump = client.get("/api/backup")
    with db.connect() as conn:
        conn.execute("DELETE FROM positions")
        conn.execute("DELETE FROM transactions")
    restored = client.post("/api/restore", files={"file": ("b.json", dump.content, "application/json")})
    assert restored.status_code == 200
    positions = client.get("/api/positions").json()
    assert [p["symbol"] for p in positions] == ["AAPL"]
