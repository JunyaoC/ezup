-- ezupdate D1 schema. Apply with:
--   wrangler d1 execute ezupdate --remote --file=./schema.sql
-- Every statement is idempotent so re-applying after a deploy is safe.
--
-- CREATE TABLE IF NOT EXISTS does nothing to a table that already exists, so it
-- cannot add a column to a deployed database. Upgrading an existing deployment
-- takes the ALTER TABLE steps in README.md ("Migrating a deployed table").

-- A device is one machine holding one bearer token. Only the sha256 of the
-- token is ever stored; the plaintext exists once, in the create response.
--
-- `role` governs reads only. 'device' (the default) sees the sessions it
-- registered; 'reader' is the PM pulling the whole team's work. Neither role
-- grants writes to another device's session -- that is ownership, below.
--
-- `scoped_device_id` narrows a reader to one device's sessions: it is set when
-- a device mints its own operator token (POST /v1/token) and names the minting
-- device. NULL on plain devices and on the admin-minted global reader. A scoped
-- reader is strictly read-only -- it cannot write, delete, or mint -- and its
-- row lives in this same table because a token is a token: one lookup
-- authenticates everything, and revocation is the same revoked_at either way.
CREATE TABLE IF NOT EXISTS devices (
  id               TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  email            TEXT NOT NULL,
  role             TEXT NOT NULL DEFAULT 'device' CHECK (role IN ('device', 'reader')),
  scoped_device_id TEXT,
  token_sha256     TEXT NOT NULL UNIQUE,
  created_at       TEXT NOT NULL,
  revoked_at       TEXT
);

-- One row per shared Claude Code session. `size` is the published prefix length
-- in bytes (max offset+length seen), not a sum of chunk lengths, so a re-sent
-- chunk cannot inflate it. `deleted_at` tombstones instead of dropping the row,
-- which keeps a later delete idempotent and blocks reads of orphaned R2 keys.
--
-- `device_id` is the owner: the device that registered the session, and the
-- only one that may write, delete or re-register it. It is nullable purely so
-- the migration can add it to a deployed table; a NULL owner is frozen (writes
-- are refused) until an operator assigns one, because letting the next writer
-- adopt an unowned row is exactly the takeover ownership exists to prevent.
--
-- `updated_ms` is the same instant as `updated_at` in epoch milliseconds, and
-- it is what GET /v1/sessions?since= compares against. The cursor makes a round
-- trip through the client's own date formatter, and Python's isoformat
-- (microseconds, +00:00) does not order lexically against JS toISOString
-- (milliseconds, Z). A number is the only representation both agree on.
--
-- `enc` / `enc_gen` are the E2E markers (docs/E2E-CONTRACT.md §4). NULL enc =
-- legacy plaintext session; 'aead-v1' = chunk bodies are client-side
-- AES-256-GCM ciphertext, `length + 16` bytes for a `length`-byte plaintext
-- range. `enc_gen` is the current AEAD generation (0 = plaintext): clients
-- derive nonces from (enc_gen, offset), so the server's only job is to keep it
-- monotonic -- enc may go NULL -> 'aead-v1' and never back, enc_gen may never
-- decrease. The worker cannot decrypt either way; these columns exist so pull
-- and the viewer know which decode path a session takes.
CREATE TABLE IF NOT EXISTS sessions (
  session    TEXT PRIMARY KEY,
  device_id  TEXT,
  author     TEXT NOT NULL,
  project    TEXT,
  branch     TEXT,
  cwd        TEXT,
  title      TEXT,
  first_ts   TEXT,
  last_ts    TEXT,
  size       INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  updated_ms INTEGER NOT NULL DEFAULT 0,
  deleted_at TEXT,
  enc        TEXT,
  enc_gen    INTEGER NOT NULL DEFAULT 0
);

-- The append-only chunk index. PRIMARY KEY(session, offset) is what makes an
-- interrupted publish safe to retry: the same offset can only be claimed once,
-- and the claim is a guarded upsert so simultaneous retries of the same range
-- resolve to one row instead of one constraint violation.
-- "offset" is a SQLite keyword, so it is double-quoted here and in every query.
CREATE TABLE IF NOT EXISTS chunks (
  session    TEXT NOT NULL,
  "offset"   INTEGER NOT NULL,
  length     INTEGER NOT NULL,
  sha256     TEXT NOT NULL,
  key        TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (session, "offset")
);

-- One wrapped data key per (session, recipient). Opaque to the server: `wrap`
-- is base64 of a 60-byte blob (12 nonce + 32 ct + 16 tag) sealed under the
-- recipient's key-encryption key, which never exists server-side. The server
-- can neither read nor usefully forge these -- a forged wrap fails the
-- recipient's GCM tag, whose AAD binds session, recipient and generation.
-- `recipient_id` is a devices.id: the owning device itself (the self-wrap that
-- lets a device recover every DK from its pasted key alone) or a reader it
-- minted. Rotation (enc_gen bump) overwrites the row; session deletion
-- cascades in DELETE /v1/session. No foreign keys, same as everywhere else:
-- D1 batches are the consistency mechanism here.
CREATE TABLE IF NOT EXISTS wrapped_keys (
  session      TEXT NOT NULL,
  recipient_id TEXT NOT NULL,          -- devices.id (device itself or reader)
  enc_gen      INTEGER NOT NULL,
  wrap         TEXT NOT NULL,          -- base64 of the 60-byte wrap blob
  created_at   TEXT NOT NULL,
  PRIMARY KEY (session, recipient_id)
);

-- Failed POST /v1/device attempts, bucketed by client IP. One correct guess of
-- ADMIN_TOKEN mints a token, so guessing gets a budget and then a lockout.
-- Times are epoch milliseconds; the counter is reset by the upsert itself once
-- the window has passed, so nothing has to sweep this table.
CREATE TABLE IF NOT EXISTS admin_failures (
  ip              TEXT PRIMARY KEY,
  failures        INTEGER NOT NULL DEFAULT 0,
  window_start_ms INTEGER NOT NULL,
  locked_until_ms INTEGER
);

-- GET /v1/sessions?since= is the polling path a PM hits every few minutes.
CREATE INDEX IF NOT EXISTS idx_sessions_updated_ms ON sessions (updated_ms);
-- A plain device's feed is filtered to its own rows before it is ordered.
CREATE INDEX IF NOT EXISTS idx_sessions_device ON sessions (device_id, updated_ms);
CREATE INDEX IF NOT EXISTS idx_chunks_session ON chunks (session);
-- GET /v1/tokens lists the reader tokens one device minted.
CREATE INDEX IF NOT EXISTS idx_devices_scope ON devices (scoped_device_id);
-- GET /v1/blob?key= resolves a key back to its session before serving bytes.
CREATE INDEX IF NOT EXISTS idx_chunks_key ON chunks (key);
-- GET /v1/wrapped_keys (no ?session=) is the bulk recovery/backfill path: all
-- wraps addressed to the authenticated caller.
CREATE INDEX IF NOT EXISTS idx_wrapped_recipient ON wrapped_keys (recipient_id);
