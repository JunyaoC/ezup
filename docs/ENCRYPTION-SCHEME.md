# ezup end-to-end encryption scheme (v1, "aead-v1")

Status: DESIGN — pinned for implementation. Every byte layout, field name, info
string and signature in this document is normative. Where this document and an
implementer's instinct disagree, this document wins; where this document is
silent, match the house style of the existing code.

Scope: the Python client (`ezchangelog/`), the Worker (`worker/src/index.ts`,
`worker/schema.sql`), and the browser viewer (`worker/src/viewer.ts`).
Breaking change is accepted: tokens are re-minted, and the existing plaintext
data is marked legacy (section 8 says exactly how and why).

Notation: `||` is byte concatenation, `BE4(n)` / `BE8(n)` are big-endian
unsigned 4- and 8-byte encodings, `hex()` is lowercase hex, `utf8()` is UTF-8
bytes. All AES-GCM is AES-256-GCM with a 128-bit tag appended to the
ciphertext (`ct || tag`), which is what both `cryptography.AESGCM` and
WebCrypto `AES-GCM` produce natively.

---

## 1. Keys and derivation

### 1.1 Pasted keys (unchanged surface, new meaning)

A pasted key is still `ezu_<64 lowercase hex>` (device) or `ezr_<64 lowercase
hex>` (reader). The 32 bytes decoded from the hex are the secret **S**. S is
generated **on the client** (`secrets.token_bytes(32)` / 
`crypto.getRandomValues`), never by the server — that is what makes the rest
of this document true. The server sees only derived values.

### 1.2 HKDF

All derivation is HKDF-SHA-256 (RFC 5869) with:

- IKM  = S (the 32 **raw** bytes, hex-decoded — never the ASCII hex string)
- salt = `utf8("ezup/v1/salt")` (fixed, public; S is uniform so the salt is
  domain decoration, not a secret)
- L    = 32 bytes
- info = exactly one of the strings below (domain separation)

| output   | info string       | role                                        |
|----------|-------------------|---------------------------------------------|
| `K_auth` | `"ezup/v1/auth"`  | becomes the wire bearer credential           |
| `K_enc`  | `"ezup/v1/enc"`   | AES-256 key-encryption key, **client only**  |

HKDF's two outputs are computationally independent: possession of `K_auth`
(which the server effectively holds, since the client sends it) yields nothing
about `K_enc`, and neither yields S.

### 1.3 The wire bearer

The HTTP `Authorization` header becomes:

    Authorization: Bearer ezw_<hex(K_auth)>        (68 chars total)

The `ezw_` prefix ("w" = wire) is deliberate: a bearer found in a log or a
capture is visibly *not* a pasteable key, and grepping for `ezu_`/`ezr_` in
server-side artifacts must come up empty by construction.

The server is unchanged in mechanism: `authenticate()` hashes the bearer
string it receives and looks up `devices.token_sha256`. What is stored is now

    token_sha256 = hex(SHA-256(utf8("ezw_" + hex(K_auth))))

so the database holds a hash of a derivation of S — two one-way steps from the
paste. **Minting changes**: the server can no longer generate tokens (it would
see S). Both mint endpoints now *accept* a client-computed `token_sha256`
(section 5). The plaintext key is printed by the CLI that generated it and
exists nowhere else, ever.

### 1.4 Python surface (`ezchangelog/crypto.py`, new module)

```python
GCM_TAG = 16
WRAP_LEN = 60          # 12 nonce + 32 ct + 16 tag
ENC_SCHEME = "aead-v1"

@dataclass(frozen=True)
class KeySet:
    kind: str        # "device" for ezu_, "reader" for ezr_
    bearer: str      # "ezw_" + hex(K_auth); safe to send, useless to decrypt
    enc_key: bytes   # K_enc, 32 bytes; never serialized, never sent

def parse_key(pasted: str) -> KeySet:
    """Raise ValueError unless pasted is ezu_/ezr_ + 64 lowercase hex."""

def bearer_sha256(pasted: str) -> str:
    """hex(sha256(bearer)) — what a mint request registers server-side."""
```

`HttpTransport` gains one behavioural rule: when the configured token starts
with `ezu_`/`ezr_` it calls `parse_key` and sends `KeySet.bearer`; a token
already starting with `ezw_` (or anything else) is sent verbatim, so tests and
raw-bearer configs keep working. The pasted secret still never appears in any
repo config (`config.py` rule stands).

Pinned KDF test vector (generated with WebCrypto, the viewer's implementation,
so Python must agree with it):

    S            = 00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff
    K_auth       = c587d5c13882bb99c0db1bdeb631f580a6af77dd47d646a7558d3d48c23c3677
    K_enc        = 38b074ce889e57c645145ef370ba7e63478b188603a463c875cfde5f8652eef5
    bearer       = ezw_c587d5c13882bb99c0db1bdeb631f580a6af77dd47d646a7558d3d48c23c3677
    token_sha256 = 01d236f19c3dfb00fa29e633cd93cc5c8f97893db5fbd0c095280156499b58d8

---

## 2. Content encryption

### 2.1 Data key and generation

Each session has a random **data key** `DK` (32 bytes, client-generated) and a
**generation** `gen` (uint32, starts at 1). The pair is rotated together:

> **RULE R1 (load-bearing): every `PublishState.reset()` — compaction, 409
> replacement, any path that may re-send different bytes at an offset already
> encrypted — MUST set `gen = max(local_gen, server_enc_gen) + 1` and generate
> a fresh random DK before any chunk is encrypted.** `delete_on_reset=False`
> does not exempt a caller from R1.

`gen` lives in `PublishState` (new field `enc_gen: int = 0`, 0 meaning
"plaintext/legacy state") and server-side in `sessions.enc_gen` so a client
with lost state can recover it (section 6.3).

### 2.2 Nonce — deterministic, offset-derived

    nonce = BE4(gen) || BE8(plaintext_offset)          # 12 bytes

**Why a counter and not random:** determinism is what keeps the existing
idempotency machinery alive. The server dedupes and 409s on the sha256 of the
body; `reconcile()` proves remote ranges against local bytes by hashing. With
`nonce = f(gen, offset)` and a fixed DK, re-encrypting the same plaintext range
reproduces the identical ciphertext, so a retried upload still no-ops, a
post-state-loss reconcile can still verify, and `LocalDirTransport`'s
same-sha-same-offset dedupe still collapses. Random nonces would turn every
retry into a 409 and every reconcile into a forced reset.

**Uniqueness proof.** Fix one DK. By R1, a DK is used only within a single
`(session, gen)`. Within one generation the publisher emits chunks at strictly
increasing, non-overlapping plaintext offsets (`plan_chunks` tiles
`[start, size)` contiguously), and the server's `PRIMARY KEY (session,
"offset")` refuses a second, different body at a claimed offset (409). So for
one DK, each *distinct* plaintext chunk gets a distinct `offset`, hence a
distinct nonce. The only repeat the system can produce is a re-send of the
*same* offset in the same generation — which, because R1 forces a new
generation before any plaintext can differ, is byte-identical plaintext with
identical AAD, and deterministic AES-GCM then emits the *identical* ciphertext:
`Enc(K, N, P, A)` repeated is a pure replay, revealing nothing an observer did
not already have. The catastrophic case (same key+nonce, different plaintext)
requires violating R1; R1 is therefore enforced in one place —
`PublishState.reset()` itself rotates `enc_gen` and clears the wrapped DK — so
no future caller can forget it.

Capacity: 2^32 generations, 2^64 offsets, both unreachable in practice; GCM's
2^32-block per-invocation limit is irrelevant at chunks <= 8 MiB.

### 2.3 AAD and body layout

    chunk_aad  = utf8("ezup/v1/chunk") || 0x00 || utf8(session_id) || 0x00
                 || BE4(gen) || BE8(offset)
    body       = AES-256-GCM-Encrypt(DK, nonce, plaintext_chunk, chunk_aad)
               = ct || tag                          # len = plaintext len + 16

Session ids match `SAFE_ID` (no NUL possible), so the encoding is unambiguous.
The AAD binds each ciphertext to its session, generation and position: a
malicious store cannot re-file a ciphertext under another session/offset that
the same DK might reach, and cross-generation splices fail the tag.

The nonce is **not transmitted** — both sides reconstruct it from `(gen,
offset)`, which they already know. Nothing else is prepended: the body is
exactly `ct || tag`, versioning is carried by the session row (`enc =
"aead-v1"`), and `len(body) == length + 16` always.

### 2.4 What the wire fields mean now

- `offset`, `length` (query params, chunk keys, `chunks` table, session
  `size`): **plaintext** byte addressing, unchanged. The chunk key
  `raw/<author>/<session>/<offset 12d>-<length>.jsonl` keeps plaintext
  lengths, so key grammar, lexical ordering and `parse_chunk_key` are
  untouched. (For encrypted sessions the stored object is 16 bytes longer than
  the key's length component; the key is an address, not a size claim.)
- `sha256` (query param, `chunks.sha256`, dedupe, reconcile, pull
  verification): **sha256 of the ciphertext body**. The Worker's streaming
  hash check is mechanically unchanged — it hashes the bytes it receives; those
  bytes are now ciphertext. The Worker never needs plaintext hashes and never
  sees plaintext.
- Worker length enforcement: for a session with `enc = 'aead-v1'`, expected
  body length is `length + 16` (`FixedLengthStream(length + 16)`, cap
  `MAX_CHUNK + 16`); plaintext-legacy sessions keep `length`.

### 2.5 Python surface

```python
def new_data_key() -> bytes: ...                       # secrets.token_bytes(32)
def chunk_nonce(gen: int, offset: int) -> bytes: ...   # BE4 || BE8, 12 bytes
def chunk_aad(session: str, gen: int, offset: int) -> bytes: ...
def encrypt_chunk(dk: bytes, session: str, gen: int, offset: int,
                  plaintext: bytes) -> bytes: ...      # returns ct||tag
def decrypt_chunk(dk: bytes, session: str, gen: int, offset: int,
                  body: bytes) -> bytes: ...           # raises on tag failure
```

Pinned chunk vector (WebCrypto-generated; Python must reproduce it exactly):

    DK        = 0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0
    session   = "sess-abc", gen = 1, offset = 4096
    plaintext = {"type":"user","text":"hello"}\n            (31 bytes)
    nonce     = 000000010000000000001000
    aad       = 657a75702f76312f6368756e6b00736573732d61626300000000010000000000001000
    body      = c80a96c53f560b528367d4bbe2a22c2f376bdc8b14c5abf9ddc7744b1acfb861
                1b649d6bd75ca706ea67a9822ef681                (47 bytes)
    sha256    = ee8a72a65a81d20b5f077efece3a672aaff85ef81d1c57485368f695b6201add

---

## 3. Envelope: wrapping DK for recipients

### 3.1 Wrap format

DK is wrapped with AES-256-GCM under a recipient's `K_enc` (uniform with the
content cipher; both runtimes have it natively; AES-KW was rejected because it
cannot carry AAD, and the AAD below is what stops a malicious store from
shuffling wraps between sessions and recipients):

    wrap_nonce = 12 random bytes                    # per wrap; wraps are rare,
                                                    # 96-bit random collision
                                                    # risk is negligible at
                                                    # <= millions of wraps
    wrap_aad   = utf8("ezup/v1/wrap") || 0x00 || utf8(session_id) || 0x00
                 || utf8(recipient_id) || 0x00 || BE4(gen)
    wrap_blob  = wrap_nonce || AES-256-GCM-Encrypt(K_enc, wrap_nonce, DK, wrap_aad)
                                                    # 12 + 32 + 16 = 60 bytes

`recipient_id` is the recipient's `devices.id` (a UUID — the device's own row,
or the reader token's row). Stored/transmitted as **base64** (standard, padded)
in JSON and D1.

```python
def wrap_dk(enc_key: bytes, session: str, recipient_id: str, gen: int,
            dk: bytes) -> bytes: ...                 # 60 bytes
def unwrap_dk(enc_key: bytes, session: str, recipient_id: str, gen: int,
              blob: bytes) -> bytes: ...             # raises on tag failure
```

Pinned wrap vector (nonce fixed for the vector only; real wraps use random):

    K_enc        = 38b074ce889e57c645145ef370ba7e63478b188603a463c875cfde5f8652eef5
    DK           = 0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0
    session      = "sess-abc", gen = 1
    recipient_id = "11111111-2222-3333-4444-555555555555"
    wrap_nonce   = 000102030405060708090a0b
    wrap_blob    = 000102030405060708090a0bc53ab5ffb0ec857074691a69eb958a4e91b3a2bb
                   2c44c7a0a13131afd2151c07bae3d4e43e892f61c6d4b0006c779373  (60 bytes)
    base64       = AAECAwQFBgcICQoLxTq1/7DshXB0aRpp65WKTpGzorssRMegoTExr9IVHAe649TkPokvYcbUsABsd5Nz

### 3.2 Server storage

```sql
-- One wrapped data key per (session, recipient). Opaque to the server: it can
-- neither read nor usefully forge these (a forged wrap fails the recipient's
-- GCM tag). Rotation (gen bump) overwrites the row; enc_gen is recorded so a
-- client can detect a stale wrap without attempting a decrypt.
CREATE TABLE IF NOT EXISTS wrapped_keys (
  session      TEXT NOT NULL,
  recipient_id TEXT NOT NULL,
  enc_gen      INTEGER NOT NULL,
  wrap         TEXT NOT NULL,       -- base64 of the 60-byte wrap_blob
  created_at   TEXT NOT NULL,
  PRIMARY KEY (session, recipient_id)
);
CREATE INDEX IF NOT EXISTS idx_wrapped_recipient ON wrapped_keys (recipient_id);
```

### 3.3 Who wraps for whom, and when

The device that owns a session is the only holder of its DK, so it does all
wrapping. It wraps for:

1. **itself** (`recipient_id` = its own device id) — this is the disaster
   recovery path: a device that lost `PublishState` fetches its own wrap back
   and unwraps with its own `K_enc`;
2. **every active reader it has minted** — at session creation and at every
   gen rotation.

To wrap for a reader later than mint time, the device must retain the reader's
`K_enc` (not R, not the bearer): at mint the CLI derives `R_enc` and stores
`{reader_id, name, enc_key: hex(R_enc)}` in `<store>/readers.json` (mode
0600). Holding `R_enc` lets the device encrypt *to* the reader forever but
does not let it authenticate *as* the reader (no `K_auth`). A compromise of
the device machine already exposes the plaintext transcripts themselves, so
this file creates no new class of exposure. `ezcl reader revoke` deletes the
entry, which is what stops future sessions being wrapped for that reader.

The admin-minted **global** reader (`scoped_device_id NULL`) can no longer
decrypt anything: no device holds its `K_enc`, so nobody wraps for it. It
degrades to a metadata-and-ciphertext reader. This is by design — the approved
team view is a **PM keyring** of per-device `ezr_` keys, each of which decrypts
exactly its own device's sessions, merged purely client-side.

### 3.4 Cost bound

Rows = sessions x (1 + active readers of the owning device). Row size <= ~200
bytes (two ids, 80-byte base64, timestamps). Worked bound: 10 devices x 500
sessions x (1 self + 5 readers) = 30,000 rows ~ 6 MB — noise against D1's
10 GB. Minting a new reader over N existing sessions costs N wraps, uploaded
in batches of <= 500 per request: 500 sessions = 1 round trip. Rotation of one
session re-uploads (1 + readers) wraps: single-digit rows.

---

## 4. Wire protocol changes (Worker)

### 4.1 Schema migration (idempotent, additive)

```sql
ALTER TABLE sessions ADD COLUMN enc TEXT;                       -- NULL = plaintext-legacy
ALTER TABLE sessions ADD COLUMN enc_gen INTEGER NOT NULL DEFAULT 0;
-- plus the wrapped_keys table and index from section 3.2
```

### 4.2 Changed endpoints

- **POST /v1/device** (admin bearer, unchanged gate): body gains required
  `token_sha256` (64 lowercase hex). The server INSERTs it verbatim and
  responds `{id, role}` — **no `token` field, ever again**. The CLI
  (`ezcl device mint`, admin-operated) generates S locally, prints
  `ezu_<hex(S)>` once, and registers only the hash.
- **POST /v1/token** (device bearer): body gains required `token_sha256`;
  response is `{id, grants}` with **no `token`**. The minting CLI prints
  `ezr_<hex(S)>` locally and follows up with the wrap backfill (4.3).
- **POST /v1/session**: body may carry `enc` (`"aead-v1"`) and `enc_gen`
  (int >= 1). Upsert rules: `enc` may go NULL -> `'aead-v1'` and never back
  (refuse with 400 `"cannot downgrade an encrypted session"` — a lying server
  is still detected client-side, but an honest server should refuse);
  `enc_gen` may only stay or increase.
- **POST /v1/chunk**: when the session row has `enc = 'aead-v1'`, expected
  body length is `length + 16` (content-length check, `FixedLengthStream`,
  and the `MAX_CHUNK` cap becomes `MAX_CHUNK + 16` on the body while `length`
  stays capped at `MAX_CHUNK`). `sha256` is verified over the received
  (cipher)bytes exactly as today.
- **GET /v1/sessions**, **GET /v1/chunks**: rows/response echo `enc` and
  `enc_gen` (add `enc, enc_gen` to the `columns` list; `/v1/chunks` returns
  them as top-level fields next to `chunks`).

### 4.3 New endpoints

- **POST /v1/wrapped_keys** (device role only; owner-only per session):
  `{"wraps": [{"session", "recipient_id", "enc_gen", "wrap"}, ...]}`, max 500
  entries, each wrap exactly 60 bytes after base64-decode. For each entry the
  server verifies the caller owns the session (same `writeDenied` rule as
  chunks), verifies `recipient_id` is the caller's own id or a reader row with
  `scoped_device_id = caller.id` (revoked readers are refused — no new grants
  to a revoked key), then upserts on `(session, recipient_id)`. Response:
  `{"ok": true, "written": n}`.
- **GET /v1/wrapped_key?session=X** (any authenticated role): returns the
  **caller's own** row only — `recipient_id` is taken from the authenticated
  device, never from a parameter, so possession of the bearer is possession of
  the wrap. The session must pass `canRead`. Response:
  `{"session", "enc_gen", "wrap"}` or 404 (same "unknown session" opacity rule
  as `/v1/chunks`).

No delete endpoint for wraps: a revoked reader's rows are dead weight (auth is
revoked, and the reader already knows its own `K_enc`), and `DELETE
/v1/session` should cascade `DELETE FROM wrapped_keys WHERE session = ?1` in
the same batch as the chunks delete.

---

## 5. Client changes: publish path

`PublishState` gains three fields:

```python
enc_gen: int = 0            # 0 = plaintext state (legacy); >= 1 = encrypted
dk_wrapped: str = ""        # base64 wrap_blob for THIS device (recipient=self)
enc: str = ""               # "" or "aead-v1"
```

The plaintext DK is held in memory only; at rest it exists solely as
`dk_wrapped` (unwrappable with the configured device key). The plaintext
transcript sits in the same directory, so this is discipline rather than a
security boundary — but it keeps DKs out of casual backups and diffs.

Publish flow deltas (all inside `_publish_locked` / `_send`):

1. **Session setup.** If `state.enc_gen == 0` (new session, or first encrypted
   publish of a legacy one): treat as reset (R1) — `gen = server_enc_gen + 1`
   (server value from `GET /v1/chunks` or 0), fresh `DK = new_data_key()`,
   wrap for self + every reader in `readers.json`, `POST /v1/wrapped_keys`,
   and send `enc`/`enc_gen` in `put_session`. A legacy session with published
   plaintext also takes `delete_session` first (section 8).
2. **`PublishState.reset()`** implements R1 itself: bumps `enc_gen`, clears
   `dk_wrapped`. (It cannot know the server's gen; `_publish_locked` reconciles
   to `max(local, server) + 1` before minting the new DK.)
3. **Chunk planning.** `plan_chunks` still tiles plaintext offsets and hashes
   plaintext internally for its own use, but the `Chunk.sha256` that is
   *declared, recorded and compared* becomes the **ciphertext** hash:
   `sha256(encrypt_chunk(dk, session, gen, chunk.offset, chunk_bytes))`.
   Deterministic encryption makes this reproducible at plan time and send time.
   (Implementation note: encrypt once in `_send`, hash the result, and let the
   plan carry plaintext hashes only as internal detail — the state file and
   the wire always carry ciphertext hashes so they match server listings.)
4. **`_send`**: `data_ct = encrypt_chunk(...)`; call
   `put_chunk(session, offset, length, sha256=ct_hash, data=data_ct)` with
   `length` still the plaintext length. Secret scan, previews, line counts,
   `running` digest and `published_sha256` all keep operating on **plaintext**
   (they are local-file bookkeeping and consent UX — encrypting them would
   blind the person consenting).
5. **`reconcile`**: remote `chunk.sha256` is now a ciphertext hash. To verify
   a remote range against the local file: fetch `enc_gen` (from
   `GET /v1/chunks`) and the device's own wrap (`GET /v1/wrapped_key`), unwrap
   DK, then for each remote chunk compute
   `sha256(encrypt_chunk(dk, session, enc_gen, offset, local_bytes))` and
   compare. A missing wrap or failed unwrap means the store's copy cannot be
   verified — treat exactly like a hash mismatch (conflict path, which resets
   and rotates under R1). Plaintext-legacy sessions keep the old plaintext
   comparison.

`LocalDirTransport` stays plaintext: it is the trusted-shared-folder
deployment with no server to distrust and no auth to derive from, and wraps
have no natural home in `index.json`. `build_transport` therefore enables
encryption exactly when the transport is HTTP. (The Transport ABC is
unchanged; encryption lives above it, in publish/pull.)

---

## 6. Client changes: pull path and viewer

### 6.1 pull.py

For sessions whose row says `enc == "aead-v1"` (echoed through
`list_sessions` / `list_chunks`):

- fetch the caller's wrap once per session, unwrap with the configured key's
  `K_enc` -> DK; check the wrap's `enc_gen` equals the session row's (a
  mismatch means rotation is in flight — error, retry next pull);
- body length check becomes `len(body) != chunk.length + GCM_TAG`;
- sha256 verification is **unchanged in code**: hash the fetched body, compare
  to `chunk.sha256` (both are ciphertext-side);
- after the hash passes, `decrypt_chunk(dk, session, enc_gen, chunk.offset,
  body)` and append the returned plaintext. Tag failure = "NOT appended"
  error, same shape as a checksum failure. Reassembly stays byte-identical to
  the developer's file because decryption is the exact inverse and offsets are
  plaintext offsets throughout.
- `_generation` (the chunk-index digest over `(offset, length, sha256)`) works
  untouched — and gets *stronger*: a DK/gen rotation changes every ciphertext
  hash, so the generation changes precisely when a refetch is required.
- `_verify_local` (used only when the recorded generation is missing): for
  encrypted sessions, re-encrypt the local range deterministically and compare
  ciphertext hashes; on any unwrap/fetch failure fall back to "stale ->
  refetch", which is safe and merely costs bandwidth.
- **Downgrade pin**: the per-session pull-state record stores `enc` and
  `enc_gen`. A session once recorded as `aead-v1` that the server later
  reports as plaintext (or with a *lower* `enc_gen`) is an **error**, never a
  refetch — that shape is a malicious or corrupted store, and quietly
  accepting plaintext would let the operator substitute forged transcripts.

### 6.2 Viewer (worker/src/viewer.ts)

Login accepts one or more pasted keys (keyring). Per key, all with native
WebCrypto (`importKey("raw", S, "HKDF")` -> `deriveBits`, `AES-GCM`,
`crypto.subtle.digest`):

1. derive bearer + `K_enc` (section 1 vectors are the conformance check);
2. `GET /v1/sessions` under that bearer; tag each row with the key it came
   from; the table renders the union, grouped by author (team view);
3. on open: `GET /v1/wrapped_key` -> unwrap -> DK; `GET /v1/chunks` ->
   `GET /v1/blob` per chunk -> verify sha256 -> decrypt with
   `nonce = BE4(enc_gen) || BE8(offset)` and the chunk AAD -> concatenate ->
   render. Legacy sessions (`enc` null) render directly with a visible
   "legacy: stored unencrypted" badge.

Keys live in memory (and at most `sessionStorage`, never `localStorage`);
merging is pure client-side — no request ever carries more than one bearer.

### 6.3 Device state recovery

A device restored onto a new machine (same pasted key, no `PublishState`)
recovers: `GET /v1/chunks` gives `enc_gen`; `GET /v1/wrapped_key` gives its
own wrap; unwrap yields DK; deterministic re-encryption lets `reconcile`
verify what the store holds against the local transcript. If the pasted key
itself is lost, the DKs wrapped only for it are lost too — the session is
re-published under a new device key from the local plaintext (which the
device, by definition, still has). Nothing is ever unrecoverable that was
recoverable before.

---

## 7. Threat model, plainly

**A fully malicious store operator CAN:** read all session **metadata**
(author, project, branch, cwd, title, timestamps — metadata is deliberately
plaintext so listing, grouping and consent UX work); see exact plaintext
lengths, offsets, chunk cadence and upload timing (traffic analysis: it knows
*when* you worked and *how much*, not *what*); withhold, delete, or serve
stale data; refuse service; attempt downgrade by relabeling a session
plaintext (detected by the client-side pin, 6.1).

**The operator CANNOT:** decrypt any chunk (it never sees S, `K_enc`, or DK;
wraps are AES-GCM blobs under keys derived from secrets that never left
clients); forge or splice a chunk that any reader will accept (GCM tag under
DK, with AAD binding session/gen/offset); recover any pasted key from
`token_sha256` (SHA-256 of an HKDF output of a 256-bit random secret); use its
knowledge of bearers to decrypt (bearer = `K_auth` line; `K_enc` is HKDF-
independent).

**A wire capture (TLS broken or logged bearer) yields:** the `ezw_` bearer —
full API impersonation (read ciphertext/metadata; for a device bearer, also
write forged *ciphertext* that no key will authenticate as valid GCM). It
yields zero plaintext and zero decryption capability. Rotation = re-mint.

**A leaked reader key (`ezr_`) exposes:** everything that reader could read —
all past and future sessions of the one device that minted it, until revoked.
Scope is structural: wraps for other devices' sessions were made under other
keys, so a leaked key cannot cross devices even with the operator's help.

**Revocation:** the server refuses the bearer, so the revoked reader fetches
nothing new. What it already fetched it keeps — physics, not policy. The
sharper edge, stated honestly: DKs the revoked reader already unwrapped stay
valid for their sessions, so a revoked reader **colluding with the store
operator** (or holding captured ciphertext) can decrypt *future* chunks of
those already-known sessions. New sessions (and any session whose gen rotates,
since rotation re-wraps only for the current reader set) are safe. `ezcl
reader revoke --rekey` may later force a gen bump across the device's live
sessions to close this; v1 documents it instead of pretending.

**A leaked device key (`ezu_`)** is game over for that device's data — it
derives everything. It lives only in the developer's local config, exactly as
the current token does.

---

## 8. Migration of the live store (ezupdate.nyf.workers.dev)

**Decision: mark-legacy. Do not re-upload the existing ~10 MB encrypted.**
Reason: those bytes already crossed the server in plaintext; a malicious or
compromised operator could have retained them. Encrypting them now converts
nothing — it would only *look* protected, which is worse than a truthful
"legacy (plaintext)" badge. Confidentiality starts at the first encrypted
byte, and the UI must say so.

Steps, in order (a human runs the wrangler/SQL steps; nothing here is done by
this repo's automation):

1. **Schema**: apply section 4.1's ALTERs + `wrapped_keys`. Additive and
   idempotent; existing rows get `enc = NULL` (legacy), `enc_gen = 0`. Deploy
   the new Worker (it must go first: old clients keep working against it —
   plaintext sessions take the legacy code paths).
2. **Re-mint**: for each device and reader, run the new client-side mint
   (`ezcl device mint` / `ezcl reader mint`), which registers `token_sha256`
   values; then revoke every old row (`UPDATE devices SET revoked_at = ...`
   for rows created before the cutover). Old pasted tokens die here — this is
   the accepted breaking change. Each device writes its new key into local
   config; each PM replaces keyring entries.
3. **Legacy sessions, finished**: leave untouched. They list and render as
   plaintext with the legacy badge. (Ownership rows survive: re-minted devices
   get new `devices.id`s, so run the one-time
   `UPDATE sessions SET device_id = <new id> WHERE device_id = <old id>`
   mapping as part of step 2 — otherwise legacy rows freeze under the
   null/foreign-owner rule.)
4. **Legacy sessions, still growing**: the first publish under the new client
   sees `state.enc_gen == 0` with published plaintext, and takes the existing
   reset machinery: `delete_session` (plaintext chunks are removed from the
   store), fresh DK at `gen = 1`, full re-send **encrypted** from the consent
   watermark. The transcript is still on the developer's disk, so this is a
   plain re-publish, and the plaintext copy stops existing server-side.
5. **Verify**: pull each migrated session with a reader key and diff against
   the device's local transcript (byte-identical); confirm `GET /v1/blob` of a
   new chunk returns high-entropy bytes of length `length + 16`; confirm the
   old bearer 401s.

---

## 9. Test checklist for the implementer

- KDF, chunk, and wrap vectors in sections 1-3 reproduced exactly in Python
  (`cryptography`) and in the viewer (WebCrypto) — they were generated with
  WebCrypto, so Python agreement proves cross-runtime interop.
- Round trip: publish encrypted -> pull -> byte-identical file (existing
  round-trip tests re-run with an encrypting HTTP fake).
- R1: force a compaction reset mid-session; assert `enc_gen` bumped, DK
  changed (new wrap uploaded), old chunks deleted, and no two uploads ever
  shared a `(dk, nonce)` with different plaintext (assertable in the fake).
- Retry determinism: re-send the same chunk after state loss; assert identical
  ciphertext and a server no-op.
- Downgrade pin: fake server flips `enc` to NULL after an encrypted pull;
  assert error, not refetch.
- Tag failure: flip one ciphertext bit; assert "NOT appended" and cursor
  held back.
- Wrap AAD: swap two sessions' wraps server-side; assert unwrap fails.
- `python -m unittest discover -s tests -t .` stays green throughout.
