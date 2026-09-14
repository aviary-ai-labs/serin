-- 010 — chat history (30-day retention)
--
-- Run this as the database OWNER in the Supabase SQL editor BEFORE deploying
-- the release that adds chat history. The application role deliberately has no
-- DDL rights, so it cannot create this itself; the app tolerates the table
-- being absent (chat simply keeps no history) rather than failing, which is
-- what makes deploying in either order safe.
--
-- Stores only what was on screen: the text of each turn and the names of the
-- tools consulted. Tool results are not kept — they are bulky, re-derivable,
-- and the most revealing part of the exchange.

CREATE TABLE IF NOT EXISTS chat_messages (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL DEFAULT 'local',
  role TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  tools TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_scope ON chat_messages(user_id, created_at);

-- Row-level security. Without this one statement a shared deployment would
-- serve one customer another customer's conversation, which is worse than
-- serving their holdings: a transcript records what someone asked.
ALTER TABLE chat_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_messages FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS chat_messages_scope ON chat_messages;
CREATE POLICY chat_messages_scope ON chat_messages
  FOR ALL
  USING (user_id = current_setting('serin.user_id', true))
  WITH CHECK (user_id = current_setting('serin.user_id', true));

-- The application role needs to use it, but never to alter it.
GRANT SELECT, INSERT, UPDATE, DELETE ON chat_messages TO serin_app;
GRANT USAGE, SELECT ON SEQUENCE chat_messages_id_seq TO serin_app;
