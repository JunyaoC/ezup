# ezupdate worker

The store behind `ezcl share`. Clients push byte ranges of a Claude Code
transcript; a PM pulls them back. D1 holds the index, R2 holds the bytes.

Nothing here is mutable: R2 objects are immutable and rate limited to one write
per second per key, so each chunk gets its own key derived from its byte offset.
An interrupted publish resumes from the last acknowledged offset, and a resend
of a range that already landed is answered from D1 without touching R2.

## Deploy

```sh
cd worker
npm install

# 1. Create the resources. `d1 create` prints a uuid.
npx wrangler r2 bucket create ezupdate-raw
npx wrangler d1 create ezupdate

# 2. Paste that uuid into wrangler.jsonc, replacing PLACEHOLDER_D1_ID.

# 3. Apply the schema (idempotent, safe to re-run).
npx wrangler d1 execute ezupdate --remote --file=./schema.sql

# 4. Set the admin secret. It only gates POST /v1/device.
npx wrangler secret put ADMIN_TOKEN

# 5. Ship it.
npx wrangler deploy
```

Optional type check (generates `worker-configuration.d.ts` from `wrangler.jsonc`
first, so no extra runtime-type package is needed):

```sh
npx wrangler types && npx tsc --noEmit
```

## Authentication

Every route except `POST /v1/device` requires a device token:

```
Authorization: Bearer ezu_<64 hex chars>
```

Only the SHA-256 of the token is stored, in `devices.token_sha256`. The
plaintext exists exactly once, in the response to `POST /v1/device`. Revoke by
setting `revoked_at`:

```sh
npx wrangler d1 execute ezupdate --remote \
  --command "UPDATE devices SET revoked_at = datetime('now') WHERE email = 'dev@example.com'"
```

## Authorization

A token says *who* you are. What you may touch is decided by two things.

**Ownership.** `POST /v1/session` records the calling device in
`sessions.device_id`. That device — and only that device — may write chunks to
the session, delete it, or re-register it. Another device's token gets `403`,
whatever its role. Ownership is not transferable over the API; an operator
reassigns it with SQL.

**Role.** `devices.role` decides what a token may *read*:

| role | reads | writes |
| --- | --- | --- |
| `device` (default) | its own sessions | its own sessions |
| `reader` | every live session | its own sessions |

`reader` is for the PM who pulls the whole team. It is a read grant only:
a reader still cannot delete or overwrite anyone's work.

A session the caller may not read is reported as `404 unknown session`, not
`403` — the id alone would otherwise confirm that a colleague is sharing it.
Write attempts on someone else's session do return `403`, since the caller
already knows the id it tried to publish.

A session row whose `device_id` is `NULL` predates this model (see the
migration below). It is **frozen**: reads follow the role table, but every
write, delete and re-registration is refused with `409` until an operator
assigns an owner. Letting the next writer adopt an unowned row would be exactly
the takeover ownership is here to prevent.

Errors are `{"error": "..."}` with the matching status: 400 bad input, 401 no or
unknown token, 403 bad admin token or another device's session, 404 unknown or
unreadable session/key, 405 wrong method, 409 offset already published with
different bytes / unowned session, 413 chunk over 8 MB or a body longer than it
declared, 429 too many failed admin attempts, 502 object storage write failed
(retryable), 500 otherwise.

Below, `$STORE` is the worker URL and `$TOKEN` a device token.

## `POST /v1/device`

Mints a device token. Requires `Authorization: Bearer $ADMIN_TOKEN`.

```sh
curl -X POST "$STORE/v1/device" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name":"junyao-mbp","email":"junyao@example.com"}'
# 201 {"token":"ezu_3f0a…","id":"…","role":"device"}
```

Pass `"role":"reader"` for the PM's puller:

```sh
curl -X POST "$STORE/v1/device" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name":"pm-laptop","email":"pm@example.com","role":"reader"}'
```

The row and the token are created in one statement, so a database failure never
leaves a minted token with nothing behind it — you get a `500` and no token.

Failed attempts are counted per client IP in `admin_failures`: 5 failures in 15
minutes lock that IP out of this route for 15 minutes (`429` with
`Retry-After`), and a correct token clears the counter. Failures are logged as
IP and reason only. Neither the guessed token nor its digest is ever written to
a log line, because a digest of a near-miss is still worth grinding offline.

## `POST /v1/session`

Registers or refreshes a session, and claims ownership of it. Must be called
before its first chunk: the chunk key is built from the author recorded here.
Calling it again updates the metadata (`title`, `last_ts`, …) and clears a
previous tombstone — from the owning device only.

```sh
curl -X POST "$STORE/v1/session" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"session":"0f3c1e88-...","author":"junyao","project":"ez-change-log",
       "branch":"main","cwd":"/Users/junyao/lab/ez-change-log",
       "first_ts":"2026-08-11T08:00:00Z","last_ts":"2026-08-11T09:10:00Z",
       "title":"share consent model","level":"raw"}'
# {"ok":true}
```

## `POST /v1/chunk`

Appends one byte range to a session you own. The body is the raw slice of the
transcript; `sha256` is its digest, verified against the bytes actually
received. Max 8 MB per chunk — the client splits anything larger.

```sh
dd if=~/.ezchangelog/raw/ez-change-log/$SESSION.jsonl bs=1 skip=0 count=8192 \
  > /tmp/chunk.jsonl
SHA=$(shasum -a 256 /tmp/chunk.jsonl | cut -d' ' -f1)

curl -X POST "$STORE/v1/chunk?session=$SESSION&offset=0&length=8192&sha256=$SHA" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/octet-stream' \
  --data-binary @/tmp/chunk.jsonl
# {"ok":true,"key":"raw/junyao/0f3c1e88-.../000000000000-8192.jsonl"}
```

Idempotent: replaying the same `offset` with the same `sha256` returns `ok`
without rewriting R2, and two *simultaneous* replays both return `ok` — the
offset is claimed by a guarded upsert, so the loser of the race is answered from
the winner's row instead of hitting the primary key. A different digest at an
offset that is already published returns 409 rather than forking the file's
history.

A failure inside R2 answers `502`, not `400`: the client retries 5xx and 429 and
gives up on 4xx, so a storage blip must not be dressed up as a bad request. A
body that runs past its declared `length` is the client's own error and stays a
4xx (`413`).

## `DELETE /v1/session`

Deletes every chunk from R2, drops the chunk rows, and tombstones the session so
pollers see it disappear. Owner only.

```sh
curl -X DELETE "$STORE/v1/session?session=$SESSION" \
  -H "Authorization: Bearer $TOKEN"
# {"ok":true,"chunks_deleted":37}
```

## `GET /v1/sessions`

The polling feed, ordered by update time ascending, 1000 rows per page. Pass the
last `updated_at` you saw as `since` to page forward. A `device` token sees only
its own sessions; a `reader` sees everyone's. Deleted sessions are omitted. Omit
`since` for everything.

```sh
curl "$STORE/v1/sessions?since=2026-08-11T00:00:00Z" \
  -H "Authorization: Bearer $TOKEN"
# {"sessions":[{"session":"0f3c1e88-...","author":"junyao","project":"ez-change-log",
#   "branch":"main","cwd":"/Users/junyao/lab/ez-change-log",
#   "title":"share consent model","first_ts":"...","last_ts":"...",
#   "size":81920,"updated_at":"2026-08-11T09:11:02.417Z"}]}
```

`cwd` is part of the row: a pulled session that loses its directory loses the
only thing that says which checkout it came from.

`since` is parsed to epoch milliseconds and compared against `updated_ms`, never
against `updated_at` as text. The cursor is round-tripped through the client's
own date formatter, and Python's `isoformat` (microseconds, `+00:00`) does not
order lexically against JavaScript's `toISOString` (milliseconds, `Z`); any
timestamp `Date.parse` accepts works. The cursor is **inclusive**, so the newest
row you saw comes back once more — millisecond ties are possible, and re-listing
a session costs an empty chunk manifest while skipping one loses it forever.

## `GET /v1/chunks`

The manifest for one session, ordered by offset. Concatenating the blobs in this
order reproduces the transcript byte for byte. Readable by the owner and by any
`reader`.

```sh
curl "$STORE/v1/chunks?session=$SESSION" -H "Authorization: Bearer $TOKEN"
# {"chunks":[{"offset":0,"length":8192,"sha256":"…","key":"raw/junyao/…/000000000000-8192.jsonl"}]}
```

## `GET /v1/blob`

Fetches one chunk's bytes. The key must still be claimed by a live session you
are allowed to read, so a delete revokes reads even from a client that cached
the key, and another team's key is a `404`.

```sh
curl "$STORE/v1/blob?key=raw/junyao/$SESSION/000000000000-8192.jsonl" \
  -H "Authorization: Bearer $TOKEN" >> ~/.ezchangelog/pulled/junyao/$SESSION.jsonl
```

## Migrating a deployed table

`schema.sql` is written with `CREATE TABLE IF NOT EXISTS`, which silently does
nothing to a table that already exists — it will not add the new columns. A
database deployed before ownership existed needs these statements, once, in this
order. Take a backup first (`npx wrangler d1 export ezupdate --remote
--output=./backup.sql`).

```sh
# One statement per call: --command takes a single statement, and running them
# separately means a re-run after a partial failure only re-hits the ones that
# already applied (those fail with "duplicate column name", which is safe).
npx wrangler d1 execute ezupdate --remote \
  --command "ALTER TABLE devices ADD COLUMN role TEXT NOT NULL DEFAULT 'device'"
npx wrangler d1 execute ezupdate --remote \
  --command "ALTER TABLE sessions ADD COLUMN device_id TEXT"
npx wrangler d1 execute ezupdate --remote \
  --command "ALTER TABLE sessions ADD COLUMN updated_ms INTEGER NOT NULL DEFAULT 0"
```

`ALTER TABLE ADD COLUMN` cannot carry a `CHECK`, so the role constraint applies
only to databases created fresh from `schema.sql`. The worker validates the
value on the way in and treats anything unrecognised as `device`, so a migrated
table is not less safe, only less self-describing.

Then create the new table and indexes — this part is just `schema.sql` again:

```sh
npx wrangler d1 execute ezupdate --remote --file=./schema.sql
```

Backfill `updated_ms` from the existing text timestamps. `julianday` returns
NULL for anything it cannot parse, and those rows keep `0`, which only means
they are always listed:

```sh
npx wrangler d1 execute ezupdate --remote \
  --command "UPDATE sessions SET updated_ms = COALESCE(CAST((julianday(updated_at) - 2440587.5) * 86400000.0 AS INTEGER), 0) WHERE updated_ms = 0"
```

Now assign ownership. Until a row has a `device_id` it is frozen: the device
that has been publishing it will get `409` on its next chunk. There is no
reliable automatic mapping — `sessions.author` is a person, `devices.name` is a
machine, and one person may have several — so do it deliberately. List what you
have:

```sh
npx wrangler d1 execute ezupdate --remote \
  --command "SELECT id, name, email, role FROM devices WHERE revoked_at IS NULL"
npx wrangler d1 execute ezupdate --remote \
  --command "SELECT author, COUNT(*) FROM sessions WHERE device_id IS NULL GROUP BY author"
```

and claim each author's sessions for the right device:

```sh
npx wrangler d1 execute ezupdate --remote \
  --command "UPDATE sessions SET device_id = '<device-uuid>' WHERE device_id IS NULL AND author = 'junyao'"
```

If an author's device is gone (token revoked, laptop replaced), mint a new
device for them and point the rows at it — the token is what the human holds,
the `id` is what the rows reference.

Finally, promote the PM's existing token to a reader, or mint a fresh one:

```sh
npx wrangler d1 execute ezupdate --remote \
  --command "UPDATE devices SET role = 'reader' WHERE email = 'pm@example.com'"
```

Everyone else stays `device` and keeps exactly what they had before, minus the
ability to read the rest of the team.

The old `idx_sessions_updated_at` index is no longer used by any query and can
be dropped (`DROP INDEX IF EXISTS idx_sessions_updated_at`) once the migration
is confirmed.

## Notes

- Authorization is per session: ownership for writes, role for reads. The
  consent decision still lives on the client, and a session that was never
  shared never reaches this worker at all.
- `sessions.size` is the published prefix length (`max(offset + length)`), not a
  sum of chunk lengths, so replays cannot inflate it.
- R2 keys are zero-padded to 12 digits, so lexical key order is byte order.
- Session ids and authors must match `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` — the
  same grammar the puller validates keys against. The leading-alphanumeric rule
  keeps `.`, `..` and dotfiles out of a key that a client will eventually turn
  back into a local path.
- Request bodies are capped while they are read, not by trusting
  `content-length`: a chunked request declares no length at all, so a header
  check alone would let an unbounded body be buffered before anyone looked.
- Two clients publishing *different* bytes at the same offset with the same
  length derive the same R2 key, so the second upload overwrites the first
  before the 409 is decided. The chunk row still holds the winner's digest and
  the puller verifies every blob against it, so the outcome is a reported
  mismatch on pull, never a silently corrupted transcript. Re-publishing the
  session (`DELETE` then publish) repairs it.
