-- Migration 9 — per-account add-ons.
--
-- Run as the database OWNER in the Supabase SQL editor. The app role has no
-- DDL rights, so the pack's own ALTER is attempted, refused, and carried past.
--
-- Until this runs, `users.features` is absent and NO account can hold an
-- add-on. That is deliberately the safe direction: brokerage sync costs Serin
-- a per-connected-user fee, so a deployment that cannot record who paid for it
-- grants it to nobody rather than to everybody. The grant endpoint reports the
-- missing column as an error instead of a hollow success.
--
-- The `users` table belongs to the Cloud accounts pack, so this only applies
-- to a hosted deployment. A self-hosted instance has no users table, resolves
-- to the open-source plan, and is never gated on add-ons at all.
--
-- Idempotent; running it twice is harmless.

ALTER TABLE users ADD COLUMN IF NOT EXISTS features TEXT NOT NULL DEFAULT '';

-- The app role reads and writes this column through the admin grant endpoint.
-- No user-facing route touches it: a customer being able to write their own
-- entitlements would be a hole straight through the add-on's revenue.
GRANT SELECT, UPDATE ON users TO serin_app;
