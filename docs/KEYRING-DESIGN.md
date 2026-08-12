# PM keyring: multi-key viewer and CLI over client-side E2E

Status: design. Companion to the E2E crypto spec (being pinned in parallel);
this document assumes its primitives and designs the layer a PM actually
touches: many reader keys, one merged team view, no server-side merging.

## 1. What this design assumes from the crypto layer

The crypto spec owns the primitives; the keyring only consumes them. Assumed
contract, restated so this document is self-contained:

- The pasted API key IS the key material. `ezu_<64 hex>` (device) and
  `ezr_<64 hex>` (reader) are 32 random bytes in hex behind a prefix. Call the
  whole pasted string `S`.
- Three independent values are derived from `S` with HKDF-SHA256 under
  distinct `info` labels, so holding one never yields another:
  - `auth`  = HKDF(S, info="ezup:auth")   -- sent as `Authorization: Bearer
    <auth>`; the server stores only sha256(auth). The server never sees `S`.
  - `wrap`  = HKDF(S, info="ezup:wrap")   -- AES-256 key-wrapping key. Never
    leaves the client in any form.
  - `keyid` = HKDF(S, info="ezup:keyid"), first 8 bytes, hex -- a public,
    non-sensitive fingerprint used to label wrapped keys and UI chips.
- Every session has a random per-session data key `DK` (AES-256-GCM). Chunk
  bodies on the wire are ciphertext; offsets/lengths/sha256 in the chunk index
  describe the ciphertext, so the worker's existing verification and the
  puller's existing generation/watermark machinery run unchanged on opaque
  bytes.
- `DK` is wrapped once per recipient: `wrapped_keys[keyid] = wrap_alg(wrap,
  DK)`. The recipient set is the publishing device itself plus every reader it
  has granted. The wrapped set travels on the session row as an opaque JSON
  blob the worker stores and returns but cannot open.
- Token minting moves client-side (a consequence of E2E, specified in the
  crypto doc): the device generates the `ezr_` secret locally and registers
  only the derived `auth` value with `POST /v1/token`. The server can no
  longer mint a secret, because a server-minted secret is a server that held
  key material.

If any of these shift in the final crypto spec, the keyring design shifts with
them mechanically; nothing below depends on the specific wrap algorithm.

## 2. The keyring, in one sentence

A keyring is a client-side list of reader keys; every operation that used to
take "the key" takes the list, unions the results, and remembers which entry
answered -- the server is never told the keys are related.

Two keyrings exist, same semantics, different homes:

| where       | file / storage                          | trust domain      |
|-------------|-----------------------------------------|-------------------|
| CLI         | `<store>/keyring.json`                  | PM's machine      |
| viewer      | `localStorage["ezup_keyring"]`          | PM's browser      |

Both live where the device token already lives today (`<store>/config.json`,
`localStorage["ezup_token"]`), so this adds no new class of secret-at-rest --
and the CLI file is the exact artifact the next-phase remote runner mounts
into the PM's own container: one file, whole team scope, still zero server
trust.

A keyring entry:

```json
{
  "token":    "ezr_...",
  "keyid":    "3f9a1c02d4e5b6a7",
  "label":    "alice",
  "store":    "https://ezupdate.nyf.workers.dev",
  "added_at": "2026-08-12T09:00:00+00:00"
}
```

`token` is the secret (the file is chmod 0600, and it is under the store, which
`config.py` already treats as machine-private -- a keyring is never read from a
repo, for exactly the reason `CREDENTIAL_KEYS` exists). `keyid` is derived,
cached for display. `label` is the PM's name for the human; it defaults to the
author name discovered on first successful list. `store` pins the entry to one
worker so a keyring can, later, span stores without ambiguity.

## 3. Viewer (worker/src/viewer.ts)

### 3.1 Login becomes "keys"

The single-token login card becomes the empty state of a key list:

```
+----------------------------------------------+
|  reader keys                                 |
|  [ ezr_... paste a key            ] [ add ]  |
|  ( ) keep keys only for this tab             |
|                                              |
|  no keys yet - each teammate runs            |
|  /ezup token <your-name> and sends you one   |
+----------------------------------------------+
```

Adding a key:

1. Derive `auth` and `keyid` from the pasted string with WebCrypto HKDF
   (SubtleCrypto `deriveBits`; all native, no libraries -- the CSP already
   forbids external scripts).
2. Probe `GET /v1/sessions` with `Bearer <auth>`. A 401 shows "that key was
   refused" inline and stores nothing. Success appends the entry to the
   keyring and re-renders the union in place -- no page reload, the new
   author's rows slide into the existing table.
3. The keyring persists in `localStorage` by default (a PM's own machine is
   the assumption, matching the current "remember on this device" behaviour);
   the "only for this tab" toggle keeps it in `sessionStorage` instead. The
   raw pasted string is what is stored, because the browser must re-derive
   `wrap` to decrypt -- storing only `auth` would make the keyring a viewer of
   ciphertext.

Keys leave the browser in exactly one form: the derived `auth` value in the
Authorization header to this origin. `S` and `wrap` never appear in any
request. The footer keeps saying so.

### 3.2 Fetching the union

On load (and on add/remove), for each keyring entry:

- `GET /v1/sessions` with that entry's `auth`. Each key sees its own scope
  (the sessions of the device that minted it); the union across keys is
  computed in the page.
- Rows are deduplicated by session id. Overlap is possible (the same dev
  minted the PM two keys, or a legacy global reader sits next to scoped
  keys); the first entry that listed a session becomes its "unlocked by"
  key, and every entry that listed it is remembered as a fallback for
  decryption.
- A key whose fetch fails with 401 is not silently dropped: its row in the
  key drawer shows "revoked or refused" in red, with its sessions absent.
  Revocation by a dev is a state the PM must be able to *see*, not infer.

Decryption of a session: take its `wrapped_keys` map off the session row,
look up each held key's `keyid`, unwrap `DK` with the first match, then fetch
chunk ciphertext via `/v1/chunks` + `/v1/blob` exactly as today and AES-GCM
decrypt per chunk before the existing JSONL turn parser runs. All WebCrypto,
all in-page.

A session with no `wrapped_keys` entry for any held key renders locked
("no key on this page can open this session") -- reachable when a listing key
exists but the dev minted it before the session, revoked the grant, or the
row is legacy plaintext (section 7 -- those render with a "plaintext
(legacy)" badge instead of a lock, since there is nothing to unlock).

### 3.3 The team view

One screen, two levels, no navigation to see "the team":

```
 ezup  team view                                    [ keys (3) ] [ refresh ]

 +----------- alice ----------+  +------------ bob -----------+
 | 12 sessions   48.2 MB      |  | 7 sessions   9.1 MB        |
 | last: 2026-08-12 09:41     |  | last: 2026-08-11 22:03     |
 +----------------------------+  +----------------------------+

 v alice                                        12 sessions - 48.2 MB
   09:41  ez-change-log  Fix compaction guard         3.1 MB  [k:3f9a]
   08:12  ez-change-log  Wire viewer keyring          1.8 MB  [k:3f9a]
   ...
 > bob                                           7 sessions - 9.1 MB
```

- **Author cards** across the top: session count, total bytes (summed from
  the `size` the rows already carry), most recent activity. Clicking a card
  scrolls to / toggles that author's group. This is the per-author totals
  requirement and doubles as the "is anyone stuck / silent" glance.
- **Groups** are collapsible per-author sections of the existing session
  table, newest first inside each group, groups ordered by most recent
  activity. Grouping key is the row's `author` field -- authoritative,
  because it is also the path component every pulled file lands under.
- **Key chip** `[k:3f9a]` on each row: the first 4 hex of the keyid that
  unlocked it, colored consistently per key. Hover shows the label
  ("alice -- added 2026-08-12"). The same chip appears in the transcript
  header: "decrypted with alice (3f9a1c02)". This is the
  which-key-unlocked-this affordance, and it makes a mis-sent key (bob's
  sessions unlocking under a chip labeled alice) visually loud.
- **Key drawer** (the `keys (3)` button): one row per keyring entry --
  label, keyid, authors it currently unlocks, session count, added date,
  status (ok / refused), and a `forget` button. Forgetting re-renders the
  union without that entry; it never calls the server (removal from a
  keyring is not revocation -- revocation is the dev's `ezup token revoke`,
  and the drawer says so in one line of small print).

A single-key keyring degrades to today's flat table with the group headers
hidden -- a dev pasting their own `ezu_` key to check their own sessions sees
no PM furniture. (`ezu_` keys are accepted in the viewer for exactly that
case; the CLI keyring below is stricter.)

## 4. CLI keyring

### 4.1 Commands

```
ezup keyring add <ezr_...> [--label alice]   # validate against the store, save
ezup keyring list                            # labels + keyids, never tokens
ezup keyring remove <label|keyid>
```

- `add` derives `auth`, probes `GET /v1/sessions`, refuses a key the store
  refuses, and refuses non-`ezr_` prefixes: the CLI keyring is a *team read*
  construct, and a device token in it would blur "my own publishing identity"
  (config.json) with "grants other people gave me". Label defaults to the
  author of the sessions the probe returned.
- `list` prints label, keyid, store, added date, and last-pull status. It has
  no code path that prints the token, same discipline as `Config.describe`.
- `remove` deletes the entry and says explicitly that already-pulled
  transcripts under `pulled/` remain on disk (they are the PM's plaintext by
  then) and that stopping the *dev's* sharing requires the dev's revoke.

### 4.2 Pull over the keyring

`ezup pull` gains a loop, not a new pipeline:

```
for entry in keyring (or [device-token] if the keyring is empty):
    transport = HttpTransport(entry.store, derive_auth(entry.token))
    pull_sessions(PullView(transport), store, scope=..., key=entry)
```

- **Back-compat**: an empty keyring falls back to the configured device token
  exactly as today, so a dev's own `ezup pull` is unchanged.
- **Cursor scope** becomes `key:<keyid>|<author-scope>` (extending
  `pull.cursor_scope`). Each key lists a different subset, so a cursor
  advanced by alice's key must not skip a window of bob's -- the same
  reasoning that already made cursors per-author-filter makes them per-key.
- **Attribution** needs no new mechanism: the session row's `author` is
  already the directory the transcript reassembles into
  (`pulled/<author>/<session>.jsonl`), and the union across keys merges on
  disk for free because authors namespace the tree. The pull-state session
  record additionally stores the `keyid` that fetched it, for provenance and
  for the viewer-style "which key" answer in `ezup status`.
- **Errors are per-key**: one refused or revoked key reports
  `ERROR alice (3f9a1c02): unauthorized` and holds only its own cursor back;
  the other keys' pulls complete. A team pull must not go all-or-nothing on
  one revocation.

### 4.3 Where decryption happens

In `_pull_one`, per chunk, after the existing sha256 verification of the
ciphertext body and before the bytes are appended: unwrap `DK` once per
session (from `wrapped_keys` via the pulling entry's `wrap` key, using the
`cryptography` package -- the one permitted dependency), then decrypt each
verified chunk and append the *plaintext*.

So `pulled/` holds plaintext, exactly as today. Rationale: `pulled/` is
already the PM's trust domain (their disk, their machine), and every
downstream consumer -- collect, distill, the journal, `--include-pulled` --
reads it as a local transcript. Decrypting at the pull boundary keeps crypto
in one module instead of smearing "maybe ciphertext" handling across every
reader. The integrity ledger (chunk index, generation, watermark) stays in
ciphertext space, untouched.

A session whose `DK` no held key can unwrap is reported
(`ERROR alice/abc123: no key in the keyring can decrypt this session`) and
skipped without advancing past it -- same loud-and-non-destructive posture as
every other pull failure. Legacy plaintext rows (section 7) skip the decrypt
step entirely and pull as today.

## 5. Onboarding, end to end

One dev, one PM, three one-liners:

```
dev  (in Claude Code):   /ezup token maria
       -> prints once:   ezr_9f2c...   (read-only, this device's sessions)
dev  -> PM: sends the one line over a channel they already trust
PM   (terminal):         ezup keyring add ezr_9f2c... --label alice
     (or browser):       paste into the viewer's "add a key" box
```

N devs = N pastes on the PM's side, nothing else: no admin, no server config,
no shared secret distribution. The PM's keyring grows one entry per teammate
and the team view assembles itself from the union.

What `/ezup token maria` (i.e. `ezup token mint --name maria`) now does under
E2E, all client-side except one registration call:

1. Generate the `ezr_` secret locally; derive `auth`, `wrap`, `keyid`.
2. `POST /v1/token` with the *derived auth value* and the name. The server
   stores sha256(auth) and the name; it never sees the secret.
3. Grant history: for every currently-shared session, unwrap the session's
   `DK` with the device's own `wrap` key, wrap it for the new `keyid`, and
   re-register the session (`POST /v1/session` upsert) with the extended
   `wrapped_keys`. New sessions wrap for every entry in the device's local
   grant list at publish time.
4. Persist locally (in the device's machine-private store state, never a
   repo): `{keyid, name, wrap_key}` -- the *derived* wrap key only, not the
   secret. That is what future publishes wrap `DK`s under. Holding a wrap key
   cannot impersonate the reader (auth derives from `S` under a different
   label), so a stolen grant list can decrypt nothing on its own and
   authenticate as no one.

### Why per-dev grants beat the old admin-minted global reader

The old model: whoever held `ADMIN_TOKEN` minted a `role=reader,
scoped_device_id NULL` token that read every device's sessions -- minted
without any device's participation, invisible to devs (`ezup token list`
shows only tokens the device minted), and revocable only by the admin.

Per-dev grants are strictly better on every axis that matters here:

- **Consent is enforced by math, not policy.** A reader key decrypts a
  session only if the *dev's device* wrapped that session's `DK` for it. Under
  E2E a global reader is not merely disallowed -- it is impossible to mint,
  because the server holds no key material to grant. The admin, the worker,
  and Cloudflare together cannot manufacture read access; only the dev's
  client can.
- **Revocation is in the dev's hands and has a blast radius of one.** `ezup
  token revoke maria` kills the auth server-side and drops the wrap entry
  from future sessions -- one dev, one grant, done. Revoking the old global
  reader logged the PM out of the entire team at once, so in practice it was
  never revoked.
- **The audit trail sits with the person it is about.** `ezup token list` on
  the dev's machine is the complete, authoritative list of who can read that
  dev's sessions. Under the global reader the honest answer was "ask the
  admin, and also anyone who ever saw ADMIN_TOKEN".
- **Compromise is contained.** A leaked `ezr_` key exposes one dev's shared
  sessions until revoked; the leaked global reader exposed everyone's,
  including sessions shared after the leak.

The `role='reader', scoped_device_id NULL` rows and the `ADMIN_TOKEN` mint
path for them become legacy: existing rows keep working against legacy
plaintext sessions until re-minting (which the accepted breaking change
forces anyway), and the worker stops minting scope-NULL readers.

## 6. Server surface: what this actually needs

**New endpoints: zero.** Merging, grouping, decryption, and attribution are
all client-side; N keys means N plain `GET /v1/sessions` calls the worker
already serves. The keyring itself is invisible to the server -- it never
learns that two reader tokens are held by the same person, which is itself a
small privacy property worth keeping.

Deltas to *existing* surface, all shared with the E2E work rather than
keyring-specific, listed so this document is honest about its foundations:

1. `sessions` gains two opaque columns (worker never parses either):
   - `enc TEXT` -- `'aes-256-gcm'` for encrypted sessions, NULL for legacy
     plaintext (section 7).
   - `wrapped_keys TEXT` -- the JSON map `{keyid: wrapped_DK}`.
   Both are set through the existing `POST /v1/session` upsert (owner-only,
   already enforced by the guarded upsert) and returned by the existing
   `GET /v1/sessions`. One migration, no new routes.
2. `POST /v1/token` accepts a client-supplied derived `auth` value instead of
   generating a secret server-side, and stores sha256(auth). Same route, same
   auth, same response minus the plaintext token (which the client already
   has, having generated it).
3. `POST /v1/device` moves the same direction for device enrolment (crypto
   spec's scope; noted for completeness).

Chunk upload, blob fetch, listing, deletion, ownership, tombstones, cursors:
untouched. Ciphertext chunks are exactly the opaque bytes the worker already
stores and sha256-verifies without reading.

## 7. Legacy data: marked plaintext, not re-encrypted

Decision: the ~10 MB already published stays as-is and is marked
legacy-plaintext (`enc` NULL), rendered with an explicit "plaintext (legacy)"
badge in the viewer and pulled without a decrypt step by the CLI.

Why not re-upload encrypted: the server has already seen those bytes --
encrypting them now cannot un-see them, and would instead (a) force every
device to still hold every historical transcript locally, (b) rewrite every
chunk index and trip the puller's generation machinery into refetching the
world, and (c) dress sessions in an encryption they never actually had, which
is worse than an honest badge. The breaking token re-mint already draws the
line: everything published with a new-format token is encrypted (the worker
can even refuse `enc`-less registrations from new-format devices to keep the
line crisp); everything before it is visibly legacy. Devs who want history
gone have `ezup unpublish` / `DELETE /v1/session` today.

## 8. Revocation and key-lifecycle corner cases

- **Dev revokes a reader**: server auth dies immediately (existing
  `revoked_at`), so the reader can no longer fetch ciphertext -- old wrapped
  `DK` entries become inert decorations on rows the key can't list. The
  device also drops the grant so future sessions never wrap for it. Already-
  pulled plaintext on the PM's disk is out of scope by definition, as with
  any read access ever granted.
- **PM loses a key**: the dev re-runs `/ezup token maria`; history re-wraps
  for the new keyid (section 5 step 3), and the PM replaces the entry. Old
  key revoked with one more command.
- **Same dev, two PMs**: two mints, two grant entries; each session's
  `wrapped_keys` carries three entries (self + two PMs). The map is small --
  48-odd bytes per recipient -- so this scales to any plausible team shape.

## 9. What I am least sure about

The **mint-time history backfill** (section 5, step 3): granting a new reader
access to *existing* sessions requires the device to unwrap and re-wrap every
session's `DK` and re-register each row. That assumes the device still holds
(or can re-derive) every historical `DK` in its local publish state -- a
device that lost that state (new laptop, deleted store dir) can authenticate
fine but can never again grant its old sessions to anyone, and the failure
surfaces at the worst time: while onboarding a PM. It is also O(sessions)
`POST /v1/session` calls in the middle of an interactive command. The
fallback posture ("new sessions only; old ones stay locked to older keys")
is safe but surprising. This interaction sits exactly on the seam with the
crypto spec (where `DK`s live at rest on the device, and whether the upsert
carries the full `wrapped_keys` map or merges) and should be reconciled with
it before implementation.
