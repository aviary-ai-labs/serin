-- Migration 8 — ledger dedupe + tax-lot lifecycle.
--
-- Run as the database OWNER in the Supabase SQL editor. The app role has no
-- DDL rights, so backend/db.py's migration is effectively a no-op on Cloud:
-- it attempts these statements, is refused, and carries on. Until this runs,
-- re-importing a statement will duplicate transactions and closed lots have
-- nowhere to record their disposal.
--
-- Every statement is idempotent; running it twice is harmless.

ALTER TABLE transactions ADD COLUMN IF NOT EXISTS external_id TEXT NOT NULL DEFAULT '';

-- Partial on purpose: '' means "entered by hand", and hand-entered rows must
-- stay free to repeat — buying the same thing twice in a day is ordinary.
-- Only fingerprinted imports are held unique.
CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_external
  ON transactions(user_id, external_id) WHERE external_id <> '';

ALTER TABLE tax_lots ADD COLUMN IF NOT EXISTS remaining_quantity REAL NOT NULL DEFAULT -1;
ALTER TABLE tax_lots ADD COLUMN IF NOT EXISTS disposed_at TEXT NOT NULL DEFAULT '';
ALTER TABLE tax_lots ADD COLUMN IF NOT EXISTS proceeds REAL NOT NULL DEFAULT 0;
ALTER TABLE tax_lots ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'USD';
ALTER TABLE tax_lots ADD COLUMN IF NOT EXISTS external_id TEXT NOT NULL DEFAULT '';

-- -1 is the "never filled in" sentinel. Every lot that exists today is open,
-- so all of it remains. Safe to re-run.
UPDATE tax_lots SET remaining_quantity = quantity WHERE remaining_quantity < 0;

CREATE UNIQUE INDEX IF NOT EXISTS idx_tax_lots_external
  ON tax_lots(user_id, external_id) WHERE external_id <> '';
CREATE INDEX IF NOT EXISTS idx_tax_lots_symbol ON tax_lots(user_id, symbol);
