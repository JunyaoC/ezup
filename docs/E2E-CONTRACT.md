# E2E implementation contract (authoritative)

Status: CONTRACT. This document reconciles `ENCRYPTION-SCHEME.md`,
`KEYRING-DESIGN.md`, and `REMOTE-RUNNER-DESIGN.md` and is the single source of
truth for the build. Precedence when documents disagree:

    E2E-CONTRACT.md  >  ENCRYPTION-SCHEME.md  >  KEYRING-DESIGN.md  >  REMOTE-RUNNER-DESIGN.md

Build agents follow this document to the byte. Where this document is silent,
`ENCRYPTION-SCHEME.md` fills in; where both are silent, match house style.
Nothing here deploys anything: the live store at ezupdate.nyf.workers.dev is
not touched by automation. A human runs every wrangler/SQL step.

Notation: `||` is byte concatenation, `BE4(n)`/`BE8(n)` are big-endian
unsigned 4-/8-byte encodings, `hex()` is lowercase hex, `utf8()` is UTF-8
bytes, `b64()` is standard padded base64. All AES-GCM is AES-256-GCM with the
128-bit tag appended (`ct || tag`) — the native output shape of both
`cryptography.hazmat.primitives.ciphers.aead.AESGCM` and WebCrypto `AES-GCM`.

---

## 0. Reconciliation decisions (the deltas from the three designs)

The three docs were written in parallel and disagree in eight places. These
are the rulings; each names the loser and why. Do not relitigate.

**D1 — HKDF labels: crypto doc wins.** Salt `"ezup/v1/salt"`, infos
`"ezup/v1/auth"` and `"ezup/v1/enc"`. The keyring doc's `"ezup:auth"` /
`"ezup:wrap"` / `"ezup:keyid"` labels are dead. The crypto doc's labels carry
pinned, WebCrypto-generated test vectors (section 2); the keyring doc's carry
none, and it explicitly deferred to the crypto spec ("if any of these shift,
the keyring design shifts mechanically").

**D2 — no third HKDF output; `keyid` is derived from the bearer.** The
keyring doc wanted `keyid = HKDF(S, "ezup:keyid")`. Instead:

    keyid = hex(SHA-256(utf8(bearer)))[:16]     # first 16 hex chars

i.e. the first 16 chars of the exact `token_sha256` the server stores. Why:
it needs no extra derivation, it is computable by anyone holding the pasted
key, it is public by construction (the server already stores the full value
as the auth lookup — possessing it authenticates nothing), and it lets a
human correlate a UI chip with a `devices` row when debugging. UI chips show
the first 4 chars of `keyid`.

**D3 — wraps live in a `wrapped_keys` D1 table with two endpoints, not a
JSON blob on the session row.** The keyring doc's "zero new endpoints,
`wrapped_keys TEXT` column on sessions" loses. Why: (a) a session-row JSON
map forces read-modify-write of the whole map through the session upsert —
racy between a publish and a reader backfill, and the keyring doc itself
flagged the merge ambiguity as its top uncertainty (its section 9); (b) a
new-reader backfill over 500 sessions would be 500 metadata upserts instead
of one batched wraps POST; (c) a table row keyed `(session, recipient_id)`
lets the server refuse new grants to revoked readers; (d) `GET` keyed by the
authenticated caller means the viewer never needs to know its own recipient
id to fetch its wrap. The table and endpoints are in sections 4–5.

**D4 — recipient identifier is the server-side `devices.id` UUID, not a
client-derived keyid.** The server can only validate "this wrap targets a
reader the caller actually minted, and it is not revoked" against an
identifier it recognizes. `keyid` (D2) is display/cursor vocabulary only; it
never appears in wrap AADs, wire wrap payloads, or D1.

**D5 — `enc` value is `"aead-v1"`, not `"aes-256-gcm"`.** A versioned scheme
name names the whole byte contract (nonce shape, AAD, wrap format); an
algorithm name names one ingredient. `"aead-v2"` can exist without a schema
change.

**D6 — legacy data: mark-legacy (crypto/keyring docs win; runner doc
loses).** The ~10 MB already published stays as-is: finished sessions keep
their plaintext chunks and render with an explicit "legacy: stored
unencrypted" badge; still-growing sessions are re-published encrypted via the
existing reset machinery, which deletes their plaintext chunks (section 8).
The runner doc's "re-upload everything encrypted, delete plaintext" loses
because re-encrypting bytes the operator has already seen adds zero
confidentiality — it manufactures the *appearance* of protection, which is
strictly worse than an honest badge — and it forces a coordinated re-publish
across every device to buy a "single wire format" that in practice is a
~10-line `enc IS NULL` skip-decrypt branch in pull and viewer. The runner is
unaffected either way: it consumes decrypted plaintext under `pulled/`.

**D7 — viewer key persistence: localStorage by default, with an "only this
tab" sessionStorage toggle (keyring doc wins; crypto doc's "never
localStorage" loses).** Forcing a PM to re-paste N keys every browser restart
drives keys into worse places (text files, chat scrollback). The current
viewer already persists the bearer in localStorage on the PM's own machine;
the page is self-contained under a CSP that forbids external scripts, so the
marginal attacker who can read localStorage is the worker operator serving
hostile JS — who could equally capture keys from a sessionStorage-only page.
That residual risk is real and is stated in section 7.3, not hidden.

**D8 — reader-history backfill is solved by self-wraps, not by assuming the
device kept every DK.** The keyring doc's open worry (its section 9) is
resolved by the crypto design: every session's DK is wrapped for the device
itself and stored server-side, so a device holding only its `ezu_` key can
recover every DK via `GET /v1/wrapped_keys` (bulk, section 5) and then wrap
for a new reader in batches. To make that one round trip instead of
O(sessions), the singular `GET /v1/wrapped_key?session=` from the crypto doc
is widened into `GET /v1/wrapped_keys` with an optional `session` filter.

Also pinned here, not contested but scattered across the docs:
- **POST /v1/device mints only `role='device'`.** The admin scope-NULL global
  reader path is removed (400 on `role: "reader"`). Reader minting is
  exclusively the device-scoped `POST /v1/token`. Existing scope-NULL reader
  rows survive for legacy plaintext sessions until re-mint revokes them.
- **`LocalDirTransport` stays plaintext.** Encryption is enabled exactly when
  the transport is `HttpTransport` and its token parsed as `ezu_`/`ezr_`
  (section 6.1). A raw `ezw_` bearer can authenticate but not encrypt or
  decrypt; publish and pull refuse encrypted work with a clear error in that
  configuration.
- **CLI keyring is `ezr_`-only; the viewer also accepts `ezu_`** (a dev
  checking their own sessions). Command names use the existing argparse
  surface: `token mint|show|list|revoke` (existing subcommand, behavior
  changes), new `device mint`, new `keyring add|list|remove`, and `pull`
  grows the keyring loop.

---

## 1. Keys, derivation, and the wire bearer

A pasted key is `ezu_<64 lowercase hex>` (device) or `ezr_<64 lowercase hex>`
(reader). The 32 hex-decoded bytes are the secret **S**, generated on the
client (`secrets.token_bytes(32)` / `crypto.getRandomValues`), never by the
server.

HKDF-SHA-256 (RFC 5869), IKM = S (raw 32 bytes, never the ASCII hex string),
salt = `utf8("ezup/v1/salt")`, L = 32:

| output   | info string      | role                                       |
|----------|------------------|--------------------------------------------|
| `K_auth` | `"ezup/v1/auth"` | wire bearer credential                     |
| `K_enc`  | `"ezup/v1/enc"`  | AES-256 key-encryption key, client only    |

Wire auth header:

    Authorization: Bearer ezw_<hex(K_auth)>          # 68 chars after "Bearer "

Server-side storage (mechanism unchanged — `authenticate()` hashes the bearer
string it receives):

    devices.token_sha256 = hex(SHA-256(utf8("ezw_" + hex(K_auth))))

Display/cursor fingerprint (D2):

    keyid = token_sha256[:16]

Pinned KDF vector (WebCrypto-generated; Python must reproduce byte-exactly):

    S            = 00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff
    K_auth       = c587d5c13882bb99c0db1bdeb631f580a6af77dd47d646a7558d3d48c23c3677
    K_enc        = 38b074ce889e57c645145ef370ba7e63478b188603a463c875cfde5f8652eef5
    bearer       = ezw_c587d5c13882bb99c0db1bdeb631f580a6af77dd47d646a7558d3d48c23c3677
    token_sha256 = 01d236f19c3dfb00fa29e633cd93cc5c8f97893db5fbd0c095280156499b58d8
    keyid        = 01d236f19c3dfb00

---

## 2. `ezchangelog/crypto.py` (new module) — exact surface

The only module that imports `cryptography` (the one permitted dependency,
installed by setup.sh via uv). Everything below is normative: names,
signatures, constants, exception type.

```python
"""Client-side E2E crypto: key derivation, chunk AEAD, DK wrapping.

WHY this exists as one module: the worker and viewer must never hold key
material, so every operation that touches S, K_enc, or a DK lives here, and
only here imports `cryptography`.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

GCM_TAG = 16                     # bytes appended by AES-GCM
WRAP_LEN = 60                    # 12 nonce + 32 ct + 16 tag
ENC_SCHEME = "aead-v1"           # sessions.enc value for this contract
HKDF_SALT = b"ezup/v1/salt"
INFO_AUTH = b"ezup/v1/auth"
INFO_ENC = b"ezup/v1/enc"
AAD_CHUNK = b"ezup/v1/chunk"
AAD_WRAP = b"ezup/v1/wrap"
BEARER_PREFIX = "ezw_"
KEY_PREFIXES = ("ezu_", "ezr_")  # device, reader


class CryptoError(Exception):
    """Any crypto failure a caller should surface: bad key format, failed
    GCM tag, wrong blob length. Wraps `cryptography`'s InvalidTag so callers
    never import that package."""


@dataclass(frozen=True)
class KeySet:
    """Everything derivable from one pasted key. `enc_key` never serializes."""
    kind: str        # "device" (ezu_) or "reader" (ezr_)
    bearer: str      # "ezw_" + hex(K_auth); safe to send, cannot decrypt
    enc_key: bytes   # K_enc, 32 bytes; never sent, never written to disk
    keyid: str       # hex(sha256(bearer))[:16]; public fingerprint


def parse_key(pasted: str) -> KeySet:
    """Derive a KeySet. Raise CryptoError unless `pasted` is ezu_/ezr_ + 64
    lowercase hex."""

def generate_key(kind: str) -> tuple[str, KeySet]:
    """Mint a fresh pasted key ('device' -> ezu_, 'reader' -> ezr_) from
    secrets.token_bytes(32). Returns (pasted, keyset); the pasted string is
    printed once by the CLI and exists nowhere else."""

def bearer_sha256(pasted_or_bearer: str) -> str:
    """hex(sha256(bearer)): what a mint request registers server-side.
    Accepts a pasted ezu_/ezr_ key (derives the bearer first) or a raw
    ezw_ bearer."""

def new_data_key() -> bytes:
    """secrets.token_bytes(32)."""

def chunk_nonce(gen: int, offset: int) -> bytes:
    """BE4(gen) || BE8(offset), 12 bytes."""

def chunk_aad(session: str, gen: int, offset: int) -> bytes:
    """AAD_CHUNK || 0x00 || utf8(session) || 0x00 || BE4(gen) || BE8(offset)."""

def encrypt_chunk(dk: bytes, session: str, gen: int, offset: int,
                  plaintext: bytes) -> bytes:
    """AES-256-GCM(dk, chunk_nonce(gen, offset), plaintext, chunk_aad(...)).
    Returns ct || tag: exactly len(plaintext) + GCM_TAG bytes. Deterministic
    by design (see RULE R1) so retries and reconcile reproduce identical
    ciphertext."""

def decrypt_chunk(dk: bytes, session: str, gen: int, offset: int,
                  body: bytes) -> bytes:
    """Inverse of encrypt_chunk. Raise CryptoError on tag failure or
    len(body) < GCM_TAG."""

def wrap_aad(session: str, recipient_id: str, gen: int) -> bytes:
    """AAD_WRAP || 0x00 || utf8(session) || 0x00 || utf8(recipient_id)
    || 0x00 || BE4(gen)."""

def wrap_dk(enc_key: bytes, session: str, recipient_id: str, gen: int,
            dk: bytes, *, nonce: bytes | None = None) -> bytes:
    """wrap_nonce || AES-256-GCM(enc_key, wrap_nonce, dk, wrap_aad(...)).
    60 bytes. `nonce` is 12 random bytes when None; the parameter exists only
    so tests can pin the vector below."""

def unwrap_dk(enc_key: bytes, session: str, recipient_id: str, gen: int,
              blob: bytes) -> bytes:
    """Inverse of wrap_dk. Raise CryptoError unless len(blob) == WRAP_LEN and
    the tag verifies."""
```

`recipient_id` is always a `devices.id` UUID (D4). Session ids match the
worker's `SAFE_ID` / the client's `_COMPONENT` grammar, so neither AAD
encoding can contain a NUL and the framing is unambiguous.

Pinned chunk vector:

    DK        = 0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0
    session   = "sess-abc", gen = 1, offset = 4096
    plaintext = {"type":"user","text":"hello"}\n            (31 bytes)
    nonce     = 000000010000000000001000
    aad       = 657a75702f76312f6368756e6b00736573732d61626300000000010000000000001000
    body      = c80a96c53f560b528367d4bbe2a22c2f376bdc8b14c5abf9ddc7744b1acfb861
                1b649d6bd75ca706ea67a9822ef681                (47 bytes)
    sha256    = ee8a72a65a81d20b5f077efece3a672aaff85ef81d1c57485368f695b6201add

Pinned wrap vector (nonce fixed for the vector only):

    K_enc        = 38b074ce889e57c645145ef370ba7e63478b188603a463c875cfde5f8652eef5
    DK           = 0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0
    session      = "sess-abc", gen = 1
    recipient_id = "11111111-2222-3333-4444-555555555555"
    wrap_nonce   = 000102030405060708090a0b
    wrap_blob    = 000102030405060708090a0bc53ab5ffb0ec857074691a69eb958a4e91b3a2bb
                   2c44c7a0a13131afd2151c07bae3d4e43e892f61c6d4b0006c779373  (60 bytes)
    base64       = AAECAwQFBgcICQoLxTq1/7DshXB0aRpp65WKTpGzorssRMegoTExr9IVHAe649TkPokvYcbUsABsd5Nz

---

## 3. Content encryption rules

### 3.1 Data key, generation, RULE R1

Each encrypted session has a random 32-byte data key `DK` and a generation
`gen` (uint32, first encrypted generation is >= 1). They rotate together:

> **RULE R1 (load-bearing): any path that could re-send different plaintext
> at an offset already encrypted under the current DK — compaction reset,
> 409 conflict replacement, `_verify_published` mismatch — MUST rotate to a
> fresh DK at `gen = max(local_enc_gen, server_enc_gen) + 1` before any byte
> is encrypted. `delete_on_reset=False` does not exempt a caller.**

Enforcement lives in exactly one place: `PublishState.reset()` (section 6.2).
A repeated `(DK, nonce)` with *identical* plaintext and AAD is a pure replay
(deterministic GCM emits the identical ciphertext — this is what keeps
retry/dedupe/reconcile working); a repeated `(DK, nonce)` with *different*
plaintext would break GCM and is impossible without violating R1.

### 3.2 Wire field semantics for encrypted sessions

- `offset`, `length`, chunk keys, `sessions.size`: **plaintext** addressing,
  unchanged. Key grammar (`raw/<author>/<session>/<offset 12d>-<length>.jsonl`)
  and `parse_chunk_key` untouched. The stored object is `length + 16` bytes;
  the key is an address, not a size claim.
- `sha256` everywhere (declare, dedupe, 409, reconcile, pull verify):
  **sha256 of the ciphertext body**.
- The nonce is never transmitted; both sides reconstruct it from
  `(enc_gen, offset)`.
- The body is exactly `ct || tag`. No prefix, no header; versioning rides on
  the session row (`enc = "aead-v1"`).

---

## 4. Worker: schema migration (idempotent, additive)

Append to `worker/schema.sql`, and mirror in the README's "Migrating a
deployed table" ALTER list:

```sql
-- E2E: NULL enc = legacy plaintext session; 'aead-v1' = encrypted under this
-- contract. enc_gen is the current AEAD generation (0 = plaintext).
ALTER TABLE sessions ADD COLUMN enc TEXT;
ALTER TABLE sessions ADD COLUMN enc_gen INTEGER NOT NULL DEFAULT 0;

-- One wrapped data key per (session, recipient). Opaque to the server: it
-- can neither read nor usefully forge these (a forged wrap fails the
-- recipient's GCM tag). Rotation (gen bump) overwrites the row.
CREATE TABLE IF NOT EXISTS wrapped_keys (
  session      TEXT NOT NULL,
  recipient_id TEXT NOT NULL,          -- devices.id (device itself or reader)
  enc_gen      INTEGER NOT NULL,
  wrap         TEXT NOT NULL,          -- base64 of the 60-byte wrap blob
  created_at   TEXT NOT NULL,
  PRIMARY KEY (session, recipient_id)
);
CREATE INDEX IF NOT EXISTS idx_wrapped_recipient ON wrapped_keys (recipient_id);
```

---

## 5. Worker: wire protocol (exact deltas to `worker/src/index.ts`)

### 5.1 Changed endpoints

**POST /v1/device** (admin bearer, gate unchanged): body gains required
`token_sha256` (64 lowercase hex; 400 otherwise). The server INSERTs it
verbatim and responds `{id, role}` — no `token` field ever again. `role` in
the request may only be `"device"`; `"reader"` is 400 (D8 note: the global
reader mint path is removed).

**POST /v1/token** (device bearer): body gains required `token_sha256`;
response `{id}` plus existing fields, minus any `token`. Everything else
(scoping to the minting device, `revoked_at` revocation, `GET /v1/tokens`,
`DELETE /v1/token`) unchanged.

**POST /v1/session** (owner-only upsert, unchanged gate): body may carry
`enc` (string) and `enc_gen` (integer). Rules:
- `enc` omitted / `enc_gen` omitted: stored values unchanged (legacy clients
  keep working).
- `enc` may transition NULL -> `'aead-v1'`. Any other value, or
  `'aead-v1'` -> anything else: 400 `"cannot downgrade an encrypted session"`.
  (A lying server is still caught by the client-side pin, 6.4; an honest
  server refuses.)
- `enc_gen` may only stay equal or increase; a decrease is 400. Requires
  `enc_gen >= 1` when `enc` is being set.

**POST /v1/chunk**: after loading the session row, if `enc = 'aead-v1'` the
expected body length is `length + 16`: the Content-Length check, the
`FixedLengthStream` bound, and the size cap all use `length + 16`, while the
`length` query param itself stays capped at `MAX_CHUNK`. sha256 verification
is mechanically unchanged (it hashes received bytes; they are ciphertext).

**GET /v1/sessions**: add `enc`, `enc_gen` to the selected columns and the
row objects.

**GET /v1/chunks**: response gains top-level `"enc"` and `"enc_gen"` next to
`"chunks"`.

**DELETE /v1/session**: the existing batch gains
`DELETE FROM wrapped_keys WHERE session = ?1`.

### 5.2 New endpoints (one route, two methods)

**POST /v1/wrapped_keys** — device role only.

```json
{"wraps": [{"session": "...", "recipient_id": "...", "enc_gen": 3,
            "wrap": "<base64, decodes to exactly 60 bytes>"}, ...]}
```

Max 500 entries. Validation, per entry, before anything is written:
- caller owns the session (same `writeDenied` rule as chunks);
- `recipient_id` is the caller's own `devices.id`, OR a `devices` row with
  `role = 'reader' AND scoped_device_id = <caller.id> AND revoked_at IS NULL`
  (revoked readers get no new grants);
- `enc_gen >= 1`; `wrap` base64-decodes to exactly 60 bytes.

Any invalid entry: 400 naming the index, nothing written. Otherwise upsert
all on `(session, recipient_id)` (unconditional overwrite — the owning device
is authoritative) and respond `{"ok": true, "written": n}`.

**GET /v1/wrapped_keys[?session=X]** — any authenticated role.
`recipient_id` is always the authenticated caller's device id, never a
parameter: possession of the bearer is possession of the wrap. With
`session`: the session must pass `canRead`; unknown/unreadable session is 404
(same opacity rule as `/v1/chunks`); readable but no row for the caller
returns `{"wraps": []}`. Without `session`: every row whose `recipient_id` is
the caller, restricted to sessions the caller `canRead` (this is the bulk
recovery/backfill path, D8). Response either way:

```json
{"wraps": [{"session": "...", "enc_gen": 3, "wrap": "<base64>"}, ...]}
```

No pagination in v1 (bounded by the caller's own session count). No DELETE
for wraps: revoked readers' rows are inert (auth is dead), and session
deletion cascades.

---

## 6. Python client

### 6.1 `transport.py`

`HttpTransport.__init__` gains one behaviour and one attribute:

```python
self.key_set: KeySet | None   # parse_key(token) when token starts with
                              # ezu_/ezr_; None otherwise
```

When `key_set` is not None the `Authorization` header sends
`key_set.bearer`; any other token string (`ezw_...`, test fakes) is sent
verbatim. The pasted secret continues never to appear in repo configs
(`config.py` `CREDENTIAL_KEYS` rule stands, and the keyring file below is
store-private like `config.json`).

New `HttpTransport` methods (HTTP-only, like the reader-token methods —
deliberately not on the `Transport` ABC; encryption lives above the ABC):

```python
def put_wrapped_keys(self, wraps: list[dict[str, Any]]) -> int:
    """POST /v1/wrapped_keys in batches of <= 500; returns total written."""

def get_wrapped_keys(self, session: str | None = None) -> list[dict[str, Any]]:
    """GET /v1/wrapped_keys, optionally filtered to one session."""

def mint_reader(self, name: str, token_sha256: str) -> dict[str, Any]:
    """POST /v1/token — signature change: the hash is now client-supplied."""
```

`SessionMeta` gains two fields, defaulted so `LocalDirTransport` and legacy
flows are untouched:

```python
enc: str = ""          # "" or "aead-v1"; "" is omitted from the wire payload
enc_gen: int = 0       # omitted from the wire payload when 0
```

(`to_dict()` drops the two keys when falsy so legacy servers/rows never see
them.) `SessionInfo` needs no field changes — `enc`/`enc_gen` arrive via
`extra`.

### 6.2 `publish.py`

`PublishState` gains three fields (defaults keep old state files loading):

```python
enc: str = ""               # "" = plaintext; "aead-v1" = encrypted
enc_gen: int = 0            # 0 = plaintext state; >= 1 = encrypted generation
dk_wrapped: str = ""        # base64 60-byte self-wrap (recipient = this device)
```

The plaintext DK lives in memory only; at rest it exists solely as
`dk_wrapped` (unwrappable only with the configured device key). This is
discipline, not a boundary — the plaintext transcript is in the same
directory — but it keeps DKs out of backups and diffs.

`PublishState.reset()` implements R1 mechanically:

```python
def reset(self) -> None:
    # ... existing clears ...
    if self.enc == ENC_SCHEME:
        self.enc_gen += 1       # reconciled to max(local, server) + 1 before
        self.dk_wrapped = ""    # the next DK is minted (see below)
```

Publish flow deltas, all inside `_publish_locked` / `_send`, active exactly
when `transport.key_set is not None and transport.key_set.kind == "device"`
(readers cannot publish; `LocalDirTransport` and raw-`ezw_` configs publish
plaintext as today — but a raw-`ezw_` config against a session already marked
encrypted must fail loudly, not silently downgrade):

1. **Cipher setup.** When encryption is active and (`state.enc_gen == 0` or
   `state.dk_wrapped == ""`): fetch the server's `enc_gen` (from the
   `GET /v1/chunks` response, 0 when absent) and set
   `state.enc_gen = max(state.enc_gen, server_enc_gen) + 1`. This one rule
   covers every path — fresh session (`max(0,0)+1 = 1`), lost local state
   (`server+1`), and post-`reset()` (where the local value was already
   bumped; skipping a generation number is harmless because generations need
   only be strictly increasing, never dense). The invariant it guarantees is
   **the new generation is strictly greater than anything either side has
   ever encrypted under**. Then `DK = new_data_key()`, `state.dk_wrapped =
   b64(wrap_dk(self_key, session, own_device_id, gen, DK))`, and
   `state.enc = "aead-v1"`. A first encrypted publish of a session with
   published plaintext bytes (legacy) additionally takes the reset path:
   `transport.delete_session` first (section 8).
2. **Ordering, pinned:** `delete_session` (only when replacing) ->
   `put_session` carrying `enc`/`enc_gen`/`start_offset` ->
   `put_wrapped_keys` (self + every reader in `readers.json`) -> chunks.
   The row and wraps exist before the first ciphertext byte, so a puller that
   sees a chunk can always resolve its generation and its wrap. (A puller
   that races the rotation window sees an `enc_gen` mismatch and retries —
   6.4.)
3. **`_send`:** `body = encrypt_chunk(dk, session_id, state.enc_gen,
   chunk.offset, data)`; `put_chunk(session, chunk.offset, chunk.length,
   sha256=sha256(body).hexdigest(), data=body)`. `length` stays the
   plaintext length. Everything hashed into `running` /
   `published_sha256`, the secret scan, previews, and line counts operate on
   **plaintext** (local bookkeeping and consent UX). `record_chunk` records
   the **ciphertext** hash, so state, wire, and server listings agree.
4. **`plan_chunks`** is unchanged (plaintext tiling; its internal plaintext
   hashes never leave the machine). The declared/recorded sha for an
   encrypted session is computed in `_send` from the ciphertext.
5. **`reconcile`:** for an encrypted session, first obtain DK: unwrap
   `state.dk_wrapped`, or when state is lost fetch the self-wrap
   (`get_wrapped_keys(session)`) and the server `enc_gen`. Then verify each
   remote chunk by deterministic re-encryption:
   `sha256(encrypt_chunk(dk, session, enc_gen, offset, local_bytes))` vs the
   remote `sha256`. A missing wrap, failed unwrap, or `enc_gen` disagreement
   is treated exactly like a hash mismatch: conflict path, which resets and
   rotates under R1. Plaintext sessions keep the old comparison.
6. **Own device id:** needed as `recipient_id` for the self-wrap. Source: the
   mint response `{id}` recorded at `device mint` time into
   `<store>/config.json` as `"device_id"`. `config.py` treats it as
   non-credential (it is a UUID the server knows). Publishers error clearly
   when encryption is active and `device_id` is missing.

### 6.3 Reader grants: `readers.json` and minting

`<store>/readers.json`, mode 0600, machine-private (same class as
`config.json`; never read from a repo):

```json
{"version": 1,
 "readers": [{"reader_id": "<devices.id UUID>", "name": "maria",
              "keyid": "01d236f19c3dfb00",
              "enc_key": "<hex of the reader's K_enc>",
              "created_at": "2026-08-12T09:00:00+00:00"}]}
```

Holding `enc_key` (K_enc) lets the device wrap DKs *to* the reader forever
but never authenticate *as* the reader (K_auth is HKDF-independent). A device
compromise already exposes the plaintext transcripts, so this file adds no
new exposure class.

`token mint --name maria` (existing subcommand, new behavior — all
client-side except two calls):

1. `pasted, ks = generate_key("reader")`; print `ezr_...` exactly once.
2. `transport.mint_reader(name, bearer_sha256(pasted))` -> `{id}`.
3. Append the `readers.json` entry (`reader_id = id`).
4. **History backfill (D8):** `get_wrapped_keys()` (bulk, self rows) -> for
   each `{session, enc_gen, wrap}`: `dk = unwrap_dk(self_enc_key, session,
   own_device_id, enc_gen, b64decode(wrap))` -> `wrap_dk(reader_enc_key,
   session, reader_id, enc_gen, dk)` -> accumulate; `put_wrapped_keys` in
   batches of <= 500. Sessions whose self-wrap fails to unwrap are reported
   and skipped, never fatal.

`token revoke` additionally removes the `readers.json` entry (future sessions
stop wrapping for it). `token list` never prints secrets (existing
discipline). New `device mint --name N --email E` (admin-operated: reads the
admin token from the environment, never from a repo): `generate_key("device")`,
`POST /v1/device` with `token_sha256`, print `ezu_...` once, print the
returned `id` for the device's `config.json` (`token` + `device_id`).

### 6.4 `pull.py`

For sessions whose row reports `enc == "aead-v1"` (via `list_sessions`
extras / the `list_chunks` response):

- Require `transport.key_set`; a raw-bearer config errors per session:
  `"cannot decrypt <session>: configure the pasted ezu_/ezr_ key, not a raw
  bearer"`.
- Once per session: `get_wrapped_keys(session)` -> the caller's wrap; check
  its `enc_gen` equals the chunk listing's `enc_gen` (mismatch = rotation in
  flight: per-session error, cursor held, retry next pull); `dk = unwrap_dk(
  key_set.enc_key, session, <own recipient id>, enc_gen, blob)`. The
  recipient id for unwrap AAD is the *caller's* device/reader row id — pinned:
  the GET response is the caller's own row, and the caller learns its id at
  mint time (device: `config.json` `device_id`; reader: stored in the keyring
  entry, 6.5). OPEN QUESTION Q3 covers the reader-id bootstrap corner.
- Body length check: `len(body) == chunk.length + GCM_TAG`.
- sha256 verification unchanged (ciphertext both sides). After it passes:
  `plaintext = decrypt_chunk(dk, session, enc_gen, chunk.offset, body)`;
  append plaintext. `CryptoError` -> same "NOT appended" error shape as a
  checksum failure; cursor held back.
- `_generation` (digest over `(offset, length, sha256)`) is untouched and
  gets stronger: rotation changes every ciphertext hash, forcing the refetch
  it requires.
- `_verify_local`: for encrypted sessions, verify by deterministic
  re-encryption of the local range; any unwrap/fetch failure degrades to
  "stale -> refetch" (safe, costs bandwidth).
- **Downgrade pin:** the per-session pull-state record gains `enc`,
  `enc_gen`, and `keyid` (of the key that fetched it). A session once
  recorded `aead-v1` that the server later reports as plaintext or with a
  lower `enc_gen` is an **error, never a refetch** — that shape is a
  malicious or corrupted store, and accepting plaintext would let the
  operator substitute forged transcripts.
- `pulled/<author>/<session>.jsonl` stays plaintext: `pulled/` is already the
  puller's trust domain, and collect/distill/journal read it unchanged.
  Legacy sessions (`enc` null, never pinned) pull exactly as today.

### 6.5 CLI keyring

`<store>/keyring.json`, mode 0600, `ezr_`-only:

```json
{"version": 1,
 "keys": [{"token": "ezr_...", "keyid": "01d236f19c3dfb00",
           "reader_id": "<devices.id UUID, learned at add-time probe>",
           "label": "alice", "store": "https://ezupdate.nyf.workers.dev",
           "added_at": "2026-08-12T09:00:00+00:00"}]}
```

Env override for the runner: `EZUP_KEYRING=<path>`.

Commands (new `keyring` subparser):

- `keyring add <ezr_...> [--label alice]` — `parse_key` (refuse non-`ezr_`),
  probe `GET /v1/sessions` with the derived bearer (refuse on 401), probe
  `GET /v1/wrapped_keys` once to learn `reader_id` if any wrap exists (else
  store `""` and backfill on first successful pull), default the label to the
  probed sessions' author, save. Never prints the token back.
- `keyring list` — label, keyid, store, added date, last-pull status. No code
  path prints tokens (same discipline as `Config.describe`).
- `keyring remove <label|keyid>` — deletes the entry; states explicitly that
  `pulled/` transcripts remain and that stopping a dev's sharing requires the
  dev's `token revoke`.

`pull` becomes a loop, not a new pipeline: for each keyring entry (falling
back to `[configured device token]` when the keyring is empty — a dev's own
pull is unchanged), build an `HttpTransport` against the entry's own `store`
and run the existing pull. Cursor scope extends `cursor_scope`:

```python
def cursor_scope(authors: Iterable[str] | None, keyid: str | None = None) -> str:
    # keyid prefix: "key:<keyid>|" + existing scope. One key's cursor must
    # never skip a window of another key's sessions.
```

Errors are per-key: one revoked key reports
`ERROR alice (01d236f19c3dfb00): unauthorized`, holds only its own cursors,
and the other keys' pulls complete.

---

## 7. Viewer (`worker/src/viewer.ts`)

All crypto via native WebCrypto (`importKey("raw", S, "HKDF")` ->
`deriveBits`; `AES-GCM`; `crypto.subtle.digest`). BE8 offsets need `BigInt`
-> `DataView.setBigUint64`. The section-1/2 vectors are the conformance
check; the viewer generated them, so agreement is interop.

1. **Login becomes a key list.** Paste box accepts `ezr_` and `ezu_` keys;
   derive bearer + K_enc + keyid in-page; probe `GET /v1/sessions`; a 401
   shows "that key was refused" inline and stores nothing. Persist the
   keyring in `localStorage["ezup_keyring"]` by default with an "only this
   tab" toggle that uses `sessionStorage` instead (D7). Keys leave the page
   in exactly one form: the derived `ezw_` bearer to this origin.
2. **Team view.** Per key: `GET /v1/sessions`; dedupe rows by session id
   (first lister owns the row; all listers are decrypt fallbacks). Author
   cards (session count, total bytes from `size`, last activity) over
   collapsible per-author groups, groups ordered by recency. Each row carries
   a `[k:XXXX]` chip — first 4 chars of the unlocking key's keyid, colored
   consistently; hover shows the label. A key drawer lists label / keyid /
   authors / status ("ok" / "revoked or refused" — a 401-ing key renders red,
   never vanishes) with a `forget` button and one line of small print:
   forgetting is not revocation. A single-key keyring renders today's flat
   table with group furniture hidden.
3. **Open a session.** `enc` null: render directly with a visible
   "legacy: stored unencrypted" badge. `enc = "aead-v1"`:
   `GET /v1/wrapped_keys?session=X` under the owning key's bearer -> unwrap
   (`wrap_aad` needs the recipient id — it is not in the GET response; the
   viewer fetches it once per key via the same call's row or stores it after
   first unwrap success; see OPEN QUESTION Q3) -> DK; `GET /v1/chunks` ->
   `GET /v1/blob` per chunk -> sha256 check -> `decrypt_chunk` semantics with
   `nonce = BE4(enc_gen) || BE8(offset)` -> concatenate -> existing JSONL
   turn parser. Tag failure renders an explicit integrity error, not a blank.
   A session no held key can unwrap renders locked: "no key on this page can
   open this session".

**Residual risk, stated:** the viewer is JavaScript served by the store
operator. A malicious operator can ship a key-stealing viewer to a browser
user; browser E2E is therefore conditional on the served code being honest at
load time. The CLI (`pull` + keyring) is the trust-anchored path. This is
accepted for v1 and printed in the viewer footer ("keys never leave this page
— verify with the CLI for a store you do not trust").

---

## 8. Migration of the live store (human-run; automation writes scripts only)

Decision D6: mark-legacy. In order:

1. **Schema + Worker deploy first.** Apply section 4 (additive, idempotent;
   existing rows become `enc = NULL`, `enc_gen = 0`), deploy the new Worker.
   Old clients keep working: plaintext sessions take legacy code paths.
2. **Re-mint everything.** New `device mint` per device (client-generated
   `ezu_`, registers `token_sha256`, records `device_id`); new `token mint`
   per reader. Revoke every pre-cutover row
   (`UPDATE devices SET revoked_at = <now> WHERE created_at < <cutover>`).
   Re-minted devices get new `devices.id`s, so run the one-time ownership
   remap `UPDATE sessions SET device_id = <new> WHERE device_id = <old>`
   per device — otherwise legacy rows freeze under the null/foreign-owner
   rule. This is the accepted breaking change.
3. **Finished legacy sessions:** untouched; list and render plaintext with
   the legacy badge forever (or until their dev runs `unpublish`).
4. **Still-growing legacy sessions:** the first publish under the new client
   sees `state.enc_gen == 0` with published plaintext and takes the reset
   path: `delete_session` (plaintext chunks leave the store), fresh DK,
   full encrypted re-send from the consent watermark. The dev's disk still
   holds the transcript; this is a plain re-publish.
5. **Verify:** pull a migrated session with a reader key, diff byte-identical
   against the dev's local transcript; `GET /v1/blob` of a new chunk returns
   high-entropy bytes of length `length + 16`; an old bearer 401s; the
   viewer's legacy badge appears on exactly the pre-cutover sessions.

---

## 9. Threat model (normative summary)

The operator CAN: read all session metadata (author/project/branch/cwd/title/
timestamps — deliberately plaintext for listing and consent UX), see exact
plaintext lengths/offsets/timing, withhold or delete data, refuse service,
attempt a downgrade (caught client-side by the pull pin, 6.4).
The operator CANNOT: decrypt any chunk, forge or splice a chunk any reader
accepts (GCM tag + AAD binding session/gen/offset), recover any pasted key
from stored hashes, or derive K_enc from a captured bearer.
A captured `ezw_` bearer: full API impersonation for that identity; zero
decryption capability.
A leaked `ezr_` key: that one device's shared sessions, past and future,
until revoked; structurally cannot cross devices.
Revocation: immediate for fetches. Honest gap: a revoked reader colluding
with the operator can still decrypt future chunks of sessions whose DK it
already holds, until those sessions' gen rotates (OPEN QUESTION Q1).
A leaked `ezu_` key: game over for that device's data, as today.

---

## 10. Test checklist (build agents: all must pass; suite stays green)

- Section 1/2 vectors reproduced exactly in Python (`cryptography`) and the
  viewer (WebCrypto): KDF, keyid, chunk, wrap.
- Round trip: publish encrypted -> pull -> byte-identical file (existing
  round-trip tests re-run against an encrypting HTTP fake).
- R1: force a compaction reset; assert `enc_gen` bumped past the server's,
  DK changed (new self-wrap), old chunks deleted, and the fake observed no
  `(dk, nonce)` reuse with differing plaintext.
- Retry determinism: re-send after state loss -> identical ciphertext,
  server no-op.
- Downgrade pin: fake flips `enc` to null / lowers `enc_gen` after an
  encrypted pull -> error, not refetch.
- Tag failure: flipped ciphertext bit -> "NOT appended", cursor held.
- Wrap AAD: swap two sessions' wraps -> unwrap fails; swap recipient ids ->
  unwrap fails.
- Keyring pull: two fake keys with overlapping sessions -> per-key cursors
  independent; one 401-ing key doesn't block the other; union lands under
  `pulled/<author>/`.
- Mint backfill: new reader over N fake sessions -> N wraps uploaded in
  <= ceil(N/500) POSTs; revoked reader in a batch -> 400 naming the index.
- Worker: `length + 16` body enforcement; enc/enc_gen upsert rules;
  wrapped_keys validation matrix; DELETE /v1/session cascades wraps;
  POST /v1/device rejects `role: "reader"` and missing `token_sha256`.
- `python -m unittest discover -s tests -t .` green throughout.

---

## 11. OPEN QUESTIONS (explicitly unresolved — do not paper over)

**Q1 — rekey-on-revoke.** A revoked reader plus a colluding operator can
decrypt future chunks of sessions whose DK it already unwrapped, until each
session's gen rotates. `token revoke --rekey` (force R1 rotation across the
device's live sessions at revoke time) is designed-for but NOT in v1 scope.
v1 documents the gap (section 9). Decide before GA whether the gap is
acceptable or `--rekey` moves into scope.

**Q2 — reader `keyid`/`reader_id` delivery to the PM.** The dev's mint
prints only the `ezr_` line (one-paste onboarding). The PM's tooling derives
`keyid` locally, but `reader_id` (needed in the unwrap AAD, 6.4) is not
derivable from the key. The contract's answer — learn it from the first
`GET /v1/wrapped_keys` response — requires that response to carry it, but
section 5.2's response shape omits `recipient_id` (it is always the caller).
RESOLUTION REQUIRED at build time; recommended: add `"recipient_id"` (the
caller's id) as a top-level field of the GET response:
`{"recipient_id": "...", "wraps": [...]}`. This is self-information, leaks
nothing, and removes the bootstrap corner for both viewer and CLI. Flagged
rather than silently added because it widens a response the crypto doc pinned.

**Q3 — `token show` under E2E.** The existing `token show` subcommand can no
longer show a secret the machine does not store. Decide: repurpose to show
metadata (name, keyid, created, revoked) or remove. Recommended: metadata
only. Low stakes, but the build must pick one.

**Q4 — bulk `GET /v1/wrapped_keys` growth.** Unpaginated by contract; bounded
by the caller's own session count (worked bound ~500 rows, ~40 KB). If a
device ever exceeds ~10k sessions, add `since`-style paging then. Recorded so
nobody mistakes the absence of paging for an oversight.

**Q5 — encrypted journal publish (runner phase 5).** The runner doc's
"synthetic session under a PM key" idea is compatible with this contract
(nothing here special-cases authorship) but is deliberately unspecified.
Out of scope until the PM asks; do not build speculatively.

## Known limitations (post-review, accepted)

Two findings from the second adversarial review are accepted rather than fixed,
because both fail closed (an error, never forged plaintext) and both are narrow:

- **Mint racing compaction (review #2, LOW-MED).** If a reader is granted at the
  instant a session compacts (R1 rotation), the reader's wrap can be stranded a
  generation behind. A later publish self-heals it; a session that goes idle
  right after the race leaves that reader unable to decrypt it (a permanent
  generation mismatch — an error, never garbage). Re-minting the reader, or any
  further activity on the session, resolves it.
- **Wrap-withholding under legacy opt-in (review #3, LOW, by design).** A
  malicious store can omit a session's wrap AND mark it plaintext; a client
  cannot distinguish "never encrypted" from "encrypted, wrap withheld." Default
  paths skip/lock it; only the explicit `--allow-legacy` / "show unverified
  legacy" opt-in renders it, always badged unverified. This is the ceiling on
  what "unverified legacy" promises — never trust a legacy badge as authentic.
