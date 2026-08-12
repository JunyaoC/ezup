-- E2E migration for a D1 deployed before docs/E2E-CONTRACT.md (§4, §8).
-- Additive, no data loss: existing session rows become enc = NULL / enc_gen = 0,
-- which is exactly the "legacy plaintext" marker (decision D6, mark-legacy).
--
-- ALTER TABLE ADD COLUMN is NOT idempotent in SQLite, so this file is run ONCE,
-- and the README runs it one statement per `wrangler d1 execute --command` call
-- so a re-run after a partial failure only re-hits statements that already
-- applied (those fail with "duplicate column name", which is safe to ignore).
-- Take a backup first:
--   npx wrangler d1 export ezupdate --remote --output=./backup-pre-e2e.sql

ALTER TABLE sessions ADD COLUMN enc TEXT;
ALTER TABLE sessions ADD COLUMN enc_gen INTEGER NOT NULL DEFAULT 0;

-- The wrapped_keys table and its index are CREATE ... IF NOT EXISTS and live in
-- schema.sql; after the ALTERs above, apply the schema as usual:
--   npx wrangler d1 execute ezupdate --remote --file=./schema.sql
--
-- The re-mint / re-own steps (contract §8) happen AFTER the new worker is
-- deployed and each human has run `device mint` / `token mint` with the new
-- client -- see "Migrating to end-to-end encryption" in README.md.
