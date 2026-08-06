"""Backup & restore — full JSON export/import + positions CSV.

The export is a portable snapshot of everything the user typed or synced:
positions, tax lots, transactions, accounts, briefing history, and settings
(including *encrypted* connector secrets — useless without the key file /
SERIN_SECRET_KEY, see SECURITY.md). The re-fetchable price-history cache and
FX cache are deliberately excluded.

Restore replaces data wholesale inside one transaction: it either fully
applies or leaves the database untouched.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from backend import db
from backend.config import APP_VERSION
from backend.models import utcnow_iso

BACKUP_MARKER = "serin_backup"
BACKUP_FORMAT = 1

# Tables included in a backup, in restore order (no FK dependencies between
# them, but stable order keeps diffs readable).
_TABLES = ("positions", "tax_lots", "transactions", "accounts", "briefings", "app_settings")

# Settings rows that are machine-local state rather than user data.
_SETTINGS_SKIP_PREFIXES = ("connector_auto_sync_state",)


def export_data() -> dict[str, Any]:
    payload: dict[str, Any] = {
        BACKUP_MARKER: BACKUP_FORMAT,
        "app_version": APP_VERSION,
        "exported_at": utcnow_iso(),
        "note": (
            "Secret connector values are encrypted (enc:v1:...) and only "
            "decrypt with the originating SERIN_SECRET_KEY / .serin-key."
        ),
    }
    with db.connect() as conn:
        for table in _TABLES:
            rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]  # noqa: S608 — fixed table list
            if table == "app_settings":
                rows = [r for r in rows if not str(r.get("key", "")).startswith(_SETTINGS_SKIP_PREFIXES)]
            payload[table] = rows
    return payload


def restore_data(payload: dict[str, Any]) -> dict[str, int]:
    """Replace all user data with the backup's contents. All-or-nothing."""
    if not isinstance(payload, dict) or payload.get(BACKUP_MARKER) != BACKUP_FORMAT:
        raise ValueError("Not a Serin backup file (missing or unknown format marker).")

    counts: dict[str, int] = {}
    with db.connect() as conn:
        conn.execute("BEGIN")
        try:
            for table in _TABLES:
                rows = payload.get(table) or []
                if not isinstance(rows, list):
                    raise ValueError(f"Backup section '{table}' is malformed.")
                conn.execute(f"DELETE FROM {table}")  # noqa: S608 — fixed table list
                inserted = 0
                for row in rows:
                    if not isinstance(row, dict) or not row:
                        continue
                    # Only columns that exist in the live schema — lets old
                    # backups restore into newer schemas (new cols take their
                    # defaults via the migration chain).
                    live_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                    cols = [c for c in row.keys() if c in live_cols]
                    if not cols:
                        continue
                    placeholders = ", ".join("?" for _ in cols)
                    col_list = ", ".join(cols)
                    conn.execute(
                        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",  # noqa: S608
                        [row[c] for c in cols],
                    )
                    inserted += 1
                counts[table] = inserted
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return counts


def positions_csv() -> str:
    """Positions as a broker-agnostic CSV (also a fine spreadsheet export)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["symbol", "name", "broker", "asset_type", "quantity", "average_cost",
         "current_price", "currency", "sector", "market_value", "unrealized_gain"]
    )
    for p in db.list_positions():
        writer.writerow(
            [p.symbol, p.name, p.broker, p.asset_type, p.quantity, p.average_cost,
             p.current_price, p.currency, p.sector, round(p.market_value, 2), round(p.unrealized_gain, 2)]
        )
    return buffer.getvalue()


def parse_backup_bytes(raw: bytes) -> dict[str, Any]:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not parse backup file: {exc}") from exc
