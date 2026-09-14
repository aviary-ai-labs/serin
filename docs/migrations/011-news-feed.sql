-- 011 — news feed (30-day retention)
--
-- Run as the database OWNER in the Supabase SQL editor before deploying the
-- release that adds the news feed. The application role has no DDL rights, and
-- the app tolerates the table being absent (News falls back to whatever the
-- current poll returned) rather than failing.
--
-- Deliberately UNSCOPED, and so deliberately without a row-level security
-- policy: a headline is the same headline for every customer, exactly like
-- price_history and fundamentals. Scoping it would store one copy per account
-- and re-fetch per account. Which items are "yours" is decided at read time by
-- matching titles against your holdings — that matching happens in the app,
-- against rows that are already scoped.

CREATE TABLE IF NOT EXISTS news_items (
  link TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  published TEXT NOT NULL DEFAULT '',
  first_seen TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_news_items_published ON news_items(published DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON news_items TO serin_app;

-- Say the RLS state rather than assume it.
--
-- Supabase's dashboard offers a one-click "Enable RLS" on any table that lacks
-- it, and on this table that is a trap: RLS enabled with *no policy* denies
-- everything, so the app role could neither read nor insert, and the whole
-- News tab failed. Every other shared cache here (price_history, fundamentals,
-- fx_rates, quotes, tracked_symbols) has RLS off for exactly this reason.
--
-- This table has no user_id and nothing to filter on. It is shared on purpose:
-- a headline is the same headline for every customer, and which items are
-- "yours" is decided in the app by matching against your holdings, which are
-- themselves scoped and RLS-protected. Turning RLS on here protects nothing
-- and breaks the feed.
ALTER TABLE news_items DISABLE ROW LEVEL SECURITY;
