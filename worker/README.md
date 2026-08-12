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

Every route except `POST /v1/device` requires a bearer:

```
Authorization: Bearer ezw_<64 hex chars>
```

Under the E2E contract (`docs/E2E-CONTRACT.md`) the pasted `ezu_`/`ezr_` key
never leaves the client. The client HKDF-derives two independent values from
it: `K_auth`, sent as the `ezw_` wire bearer above, and `K_enc`, the encryption
key the server never sees. The worker's side of this is deliberately boring —
`authenticate()` hashes whatever bearer string arrives and looks the digest up
in `devices.token_sha256`, exactly as before. What flipped is *who registers
the hash*: mint requests now carry a client-computed `token_sha256`
(`sha256("ezw_" + hex(K_auth))`), and no mint response ever contains a token.
A captured bearer (or a fully compromised worker) can impersonate the identity
on the API but can never decrypt a chunk, because `K_enc` is not derivable from
`K_auth`.

Revoke by setting `revoked_at`:

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

Below, `$STORE` is the worker URL and `$TOKEN` / `$BEARER` the derived `ezw_`
wire bearer of a device key (readers where noted).

## `POST /v1/device`

Registers a device. Requires `Authorization: Bearer $ADMIN_TOKEN`. The client
(the `device mint` CLI) generates the `ezu_` secret locally and sends only the
sha256 of its derived `ezw_` bearer; the response carries no token because the
server never had one. The returned `id` is the device's `devices.id` — the
client records it as `device_id` (it is the recipient of the device's own
wrapped keys).

```sh
curl -X POST "$STORE/v1/device" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name":"junyao-mbp","email":"junyao@example.com",
       "token_sha256":"01d236f19c3dfb00fa29e633cd93cc5c8f97893db5fbd0c095280156499b58d8"}'
# 201 {"id":"…","role":"device"}
```

`role` may only be `"device"` (or omitted). `"role":"reader"` is a `400`: the
admin-minted global reader is gone. Readers are minted exclusively by the
device whose sessions they will read (`POST /v1/token`), so every reader is
scoped and a leaked reader key can never read the whole team. A `token_sha256`
that is already registered is a `409`.

Failed attempts are counted per client IP in `admin_failures`: 5 failures in 15
minutes lock that IP out of this route for 15 minutes (`429` with
`Retry-After`), and a correct token clears the counter. Failures are logged as
IP and reason only. Neither the guessed token nor its digest is ever written to
a log line, because a digest of a near-miss is still worth grinding offline.

## `POST /v1/token`

Mints a reader scoped to the calling device (device tokens only). Same E2E
shape as `POST /v1/device`: the device generates the `ezr_` secret locally,
registers the derived bearer's hash, and the response is metadata only. The
returned `id` is what the device wraps data keys to, and what the reader's own
tooling learns back from `GET /v1/wrapped_keys`.

```sh
curl -X POST "$STORE/v1/token" \
  -H "Authorization: Bearer $BEARER" \
  -H 'content-type: application/json' \
  -d '{"name":"maria","token_sha256":"<64 lowercase hex>"}'
# 201 {"id":"…","grants":"read-only access to sessions published by device junyao-mbp"}
```

`GET /v1/tokens` lists the calling device's readers (id, name, dates — there is
no secret to leak); `DELETE /v1/token?id=…` revokes one. Revocation kills the
reader's auth immediately; its existing `wrapped_keys` rows become inert and it
can never receive a new grant.

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
       "title":"share consent model","level":"raw",
       "enc":"aead-v1","enc_gen":1}'
# {"ok":true}
```

`enc` and `enc_gen` are the E2E markers, both optional so legacy clients keep
working (omitted means "leave the stored values alone"):

- `enc` may transition `NULL → 'aead-v1'` and never back. Any other value —
  including an explicit `null` on a session already marked `aead-v1` — is
  `400 cannot downgrade an encrypted session`. Setting `enc` requires
  `enc_gen >= 1`.
- `enc_gen` may only stay equal or increase; a decrease is the same `400`. The
  guard is inside the upsert statement itself, so two racing requests cannot
  land a stale generation after a newer one — nonces are derived from
  `(enc_gen, offset)` client-side, and a rolled-back generation would reuse
  them.

## `POST /v1/chunk`

Appends one byte range to a session you own. The body is the raw slice of the
transcript; `sha256` is its digest, verified against the bytes actually
received. Max 8 MB per chunk — the client splits anything larger.

For a session registered with `enc = 'aead-v1'` the body is AES-256-GCM
ciphertext: exactly `length + 16` bytes for a `length`-byte plaintext range
(the appended GCM tag). `offset` and `length` stay plaintext addressing — the
R2 key is an address, not a size claim — and `sha256` is the digest of the
ciphertext body, so dedupe, 409 conflicts and pull verification are
mechanically unchanged. The worker checks the shape and the hash; it cannot
check, and never sees, the plaintext.

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
#   "size":81920,"updated_at":"2026-08-11T09:11:02.417Z",
#   "enc":"aead-v1","enc_gen":1}]}
```

Rows carry `enc` (`null` = legacy plaintext, `"aead-v1"` = encrypted) and
`enc_gen`, so a puller or the viewer knows which decode path each session
takes before fetching anything.

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
# {"chunks":[{"offset":0,"length":8192,"sha256":"…","key":"raw/junyao/…/000000000000-8192.jsonl"}],
#  "enc":"aead-v1","enc_gen":1}
```

The manifest carries the session's `enc` and `enc_gen` at top level: an
encrypted puller reconstructs each chunk's nonce as
`BE4(enc_gen) || BE8(offset)` and needs the generation next to the offsets it
applies to. For an encrypted session each blob is `length + 16` bytes and
`sha256` is over the ciphertext; concatenating the *decrypted* chunks in offset
order reproduces the transcript.

## `GET /v1/blob`

Fetches one chunk's bytes. The key must still be claimed by a live session you
are allowed to read, so a delete revokes reads even from a client that cached
the key, and another team's key is a `404`.

```sh
curl "$STORE/v1/blob?key=raw/junyao/$SESSION/000000000000-8192.jsonl" \
  -H "Authorization: Bearer $TOKEN" >> ~/.ezchangelog/pulled/junyao/$SESSION.jsonl
```

## `POST /v1/wrapped_keys`

Stores wrapped data keys — device tokens only. Each encrypted session has a
random 32-byte data key DK; the device wraps it (AES-256-GCM under the
recipient's key-encryption key) once for itself and once per reader it minted,
and stores the wraps here. A wrap is an opaque base64 blob decoding to exactly
60 bytes (12 nonce + 32 ct + 16 tag); the worker validates that shape,
ownership, and the recipient — nothing more, because nothing more is checkable
without a key it must not have.

```sh
curl -X POST "$STORE/v1/wrapped_keys" \
  -H "Authorization: Bearer $BEARER" \
  -H 'content-type: application/json' \
  -d '{"wraps":[{"session":"0f3c1e88-...","recipient_id":"<devices.id>",
                 "enc_gen":1,"wrap":"<base64, 60 bytes>"}]}'
# {"ok":true,"written":1}
```

At most 500 entries per request. Per entry: the session must be a live one the
caller owns, `recipient_id` must be the caller's own `devices.id` or an
**unrevoked** reader the caller minted (revoked readers never receive new
grants), `enc_gen >= 1`. Any invalid entry is a `400` naming its index and
nothing is written; otherwise every entry is upserted on
`(session, recipient_id)` in one atomic batch — the owning device is
authoritative, so a rotation's re-wrap simply overwrites.

## `GET /v1/wrapped_keys`

Returns the wraps addressed to the authenticated caller. There is no
`recipient_id` parameter: possession of the bearer *is* possession of the
wraps, so asking for someone else's rows is not expressible. The caller's own
`devices.id` is returned as `recipient_id` because it is a component of the
unwrap AAD and a freshly-onboarded reader has no other way to learn it
(self-information only).

```sh
# One session (must be readable; unknown/unreadable is 404, readable-but-no-
# wrap is an empty list):
curl "$STORE/v1/wrapped_keys?session=$SESSION" -H "Authorization: Bearer $BEARER"
# {"recipient_id":"<caller devices.id>",
#  "wraps":[{"session":"0f3c1e88-...","enc_gen":1,"wrap":"<base64>"}]}

# Everything (recovery/backfill: a device holding only its pasted key rebuilds
# every DK from its self-wraps; a reader bootstraps its whole history):
curl "$STORE/v1/wrapped_keys" -H "Authorization: Bearer $BEARER"
```

The bulk form is restricted to sessions the caller may read and is unpaginated
by contract — it is bounded by the caller's own session count. There is no
DELETE: a revoked reader's rows are inert (its auth is dead), and
`DELETE /v1/session` cascades the session's wraps.

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

## Migrating to end-to-end encryption

The E2E cutover (contract: `docs/E2E-CONTRACT.md`, §8) is **mark-legacy**:
sessions already published in plaintext stay as they are and render with a
"legacy: stored unencrypted" badge — re-encrypting bytes the operator has
already seen would add zero confidentiality and only manufacture the
appearance of protection. Still-growing sessions re-publish encrypted through
the client's normal reset path, which deletes their plaintext chunks. Every
token is re-minted, because the old scheme sent the secret itself as the
bearer. In order:

**1. Schema first** (additive, no data loss; existing rows become
`enc = NULL` / `enc_gen = 0`, which *is* the legacy marker):

```sh
npx wrangler d1 export ezupdate --remote --output=./backup-pre-e2e.sql

# One statement per call, same rationale as above: ALTER is not idempotent,
# and a re-run only re-hits statements that already applied ("duplicate
# column name", safe). The statements also live in worker/migrate-e2e.sql.
npx wrangler d1 execute ezupdate --remote \
  --command "ALTER TABLE sessions ADD COLUMN enc TEXT"
npx wrangler d1 execute ezupdate --remote \
  --command "ALTER TABLE sessions ADD COLUMN enc_gen INTEGER NOT NULL DEFAULT 0"

# wrapped_keys and its index are CREATE ... IF NOT EXISTS:
npx wrangler d1 execute ezupdate --remote --file=./schema.sql
```

**2. Deploy the new worker.** Old clients keep working against it: plaintext
sessions take the legacy code paths, and the only routes that changed
incompatibly are the mints.

**3. Re-mint everything.** Each dev runs the new `device mint` (generates
`ezu_` locally, registers `token_sha256`, records the returned `device_id`),
then `token mint` per reader. Then revoke every pre-cutover row and remap
session ownership to the new device ids — re-minted devices get new
`devices.id`s, and a legacy session left pointing at the old id freezes under
the foreign-owner rule:

```sh
npx wrangler d1 execute ezupdate --remote \
  --command "SELECT id, name, email, role, created_at FROM devices WHERE revoked_at IS NULL"

# For each old/new device pair:
npx wrangler d1 execute ezupdate --remote \
  --command "UPDATE sessions SET device_id = '<new-uuid>' WHERE device_id = '<old-uuid>'"

# Then kill the old tokens (pick the cutover timestamp):
npx wrangler d1 execute ezupdate --remote \
  --command "UPDATE devices SET revoked_at = datetime('now') WHERE created_at < '<cutover-iso>'"
```

This is the accepted breaking change: old bearers 401 from here on.

**4. Nothing else.** Finished legacy sessions stay plaintext with the badge
until their dev unpublishes them. A still-growing legacy session is handled by
the client: its first encrypted publish sees plaintext already on the server
and takes the reset path (`DELETE /v1/session`, which removes the plaintext
chunks, then a full encrypted re-send from the consent watermark).

**5. Verify.** Pull a migrated session with a reader key and diff it
byte-identical against the dev's local transcript; `GET /v1/blob` of a new
chunk returns high-entropy bytes of length `length + 16`; an old bearer 401s;
the viewer's legacy badge appears on exactly the pre-cutover sessions.

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
