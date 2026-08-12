"""PM side: fetch shared transcripts from the remote store and reassemble them.

The developer's client publishes a transcript as a sequence of append-only byte
ranges (R2 objects are immutable, so a growing file becomes many small chunks).
This module walks that back: list sessions changed since a cursor, fetch each
session's chunks, and lay them down IN OFFSET ORDER into::

    <store>/pulled/<author>/<session>.jsonl
    <store>/pull-state.json     cursor + per-session high-water offset

The reassembled file has to be byte-identical to what the developer has on
disk. Everything downstream -- collect, distill, the journal -- reads it as if
it were a local transcript, so a silently corrupt file would poison the
journal with confident nonsense. Every failure here is therefore loud and
non-destructive: bad bytes are never appended, and the local file is only ever
extended -- with exactly one exception.

That exception is a *generation change*. The publisher re-sends a transcript
from scratch when Claude Code rewrites it (compaction), which replaces the
ranges a previous pull already downloaded. Appending the new tail to the old
head would produce a file that is the right length and a document that never
existed, so a pull that detects a generation change deletes its copy and
fetches the session again. The generation is the chunk index itself -- a digest
over the ``(offset, length, sha256)`` of every range backing the local bytes --
so it changes precisely when those ranges do, on any transport, with no extra
field on the wire.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from .crypto import (
    ENC_SCHEME,
    GCM_TAG,
    CryptoError,
    decrypt_chunk,
    encrypt_chunk,
    unwrap_dk,
)
from .store import Store, _write_json_atomic
from .transport import parse_chunk_key
from .window import isoformat, parse_timestamp

PULL_STATE_VERSION = 1

# Cursor scope for a pull with no author filter.
ALL_AUTHORS = "*"


class Transport(Protocol):
    """The client half of the wire protocol, as this module needs it.

    A Protocol rather than a concrete class so the HTTP transport and a fake
    can be swapped without this module knowing which it has.
    """

    def list_sessions(self, since: str | None = None) -> list[dict[str, Any]]:
        """GET /v1/sessions -> session metadata rows."""

    def list_chunks(self, session: str) -> list[dict[str, Any]]:
        """GET /v1/chunks -> [{offset, length, sha256, key}]."""

    def fetch_blob(self, key: str) -> bytes:
        """GET /v1/blob -> the raw bytes of one chunk."""


@dataclass
class PullReport:
    sessions_new: int = 0
    sessions_updated: int = 0
    sessions_refetched: int = 0
    chunks: int = 0
    bytes: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# -- paths -------------------------------------------------------------------


def pulled_dir(store: Store) -> Path:
    return store.root / "pulled"


def pull_state_path(store: Store) -> Path:
    return store.root / "pull-state.json"


def pulled_path_for(store: Store, author: str, session: str) -> Path:
    return pulled_dir(store) / author / f"{session}.jsonl"


def _safe_component(value: Any) -> str | None:
    """Vet one path component that came off the wire.

    Author and session ids are chosen by a remote server, not by us, so they
    are untrusted path input: a session id of ``../../.ssh/authorized_keys``
    must not be able to name a file outside ``pulled/``.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text in (".", "..") or text.startswith("."):
        return None
    if "/" in text or "\\" in text or "\0" in text:
        return None
    return text


# -- state -------------------------------------------------------------------


def cursor_scope(authors: Iterable[str] | None, keyid: str | None = None) -> str:
    """The key a cursor is stored under.

    One cursor per filter, because a cursor means "everything up to here has
    been pulled" and that is only true of the filter that produced it. A single
    global cursor advanced by ``pull --author alice`` would silently skip
    everyone else's sessions in that window, for good.

    ``keyid`` extends the same rule to the PM keyring (contract 6.5): each
    reader key sees a different slice of the store, so one key's cursor must
    never be able to skip a window of another key's sessions. A keyring pull
    scopes its cursor as ``key:<keyid>|<author scope>``; a single-key pull
    (the developer's own) keeps the unprefixed legacy scope.
    """
    names = sorted({str(a) for a in authors or [] if str(a)})
    scope = "authors:" + ",".join(names) if names else ALL_AUTHORS
    return f"key:{keyid}|{scope}" if keyid else scope


def read_cursor(state: dict[str, Any], scope: str) -> str | None:
    cursors = state.get("cursors")
    if isinstance(cursors, dict) and isinstance(cursors.get(scope), str):
        return str(cursors[scope])
    # State written before cursors were scoped kept exactly one, and it was the
    # unfiltered one by definition -- a filtered pull could only ever have made
    # it wrong.
    if scope == ALL_AUTHORS and isinstance(state.get("cursor"), str):
        return str(state["cursor"])
    return None


def write_cursor(state: dict[str, Any], scope: str, value: str) -> None:
    cursors = state.get("cursors")
    if not isinstance(cursors, dict):
        cursors = {}
        state["cursors"] = cursors
    cursors[scope] = value
    if scope == ALL_AUTHORS:
        state["cursor"] = value  # Kept readable by anything reading the old key.


def load_pull_state(store: Store) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "version": PULL_STATE_VERSION,
        "cursor": None,
        "cursors": {},
        "sessions": {},
    }
    path = pull_state_path(store)
    if not path.is_file():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return empty
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
        return empty
    if data.get("version") != PULL_STATE_VERSION:
        # Unknown state shape: re-listing everything is cheap, and the offsets
        # are re-derived from the files on disk anyway.
        return empty
    return data


def save_pull_state(store: Store, state: dict[str, Any]) -> None:
    state["version"] = PULL_STATE_VERSION
    state["updated_at"] = isoformat(datetime.now(timezone.utc))
    _write_json_atomic(pull_state_path(store), state)


# -- chunk plan --------------------------------------------------------------


@dataclass
class _Chunk:
    offset: int
    length: int
    sha256: str
    key: str

    @property
    def end(self) -> int:
        return self.offset + self.length


def _parse_chunks(rows: Iterable[Any], session: str) -> tuple[list[_Chunk], list[str]]:
    """Turn wire rows into chunks sorted by offset, dropping exact duplicates.

    A republish of the same range is a legitimate no-op on the server side, so
    an identical (offset, length, sha256) triple is collapsed rather than
    treated as a conflict. Two different bodies claiming the same offset is a
    real conflict and cannot be resolved here.

    The key is vetted here rather than at the transport, because a key is not a
    name we chose: on a shared-folder store every member can edit the index, and
    a key is fed straight back to the store as a path. It must parse as a chunk
    key *and* belong to the session being pulled, or the row is dropped.
    """
    chunks: list[_Chunk] = []
    problems: list[str] = []
    seen: dict[int, _Chunk] = {}
    for row in rows:
        if not isinstance(row, dict):
            problems.append(f"malformed chunk row: {row!r}")
            continue
        try:
            offset = int(row["offset"])
            length = int(row["length"])
        except (KeyError, TypeError, ValueError):
            problems.append(f"chunk row missing offset/length: {row!r}")
            continue
        sha = str(row.get("sha256") or "").lower()
        key = str(row.get("key") or "")
        if offset < 0 or length <= 0 or len(sha) != 64 or not key:
            problems.append(
                f"chunk at offset {offset} has an unusable descriptor "
                f"(length={length}, sha256={sha or 'missing'!r}, key={key or 'missing'!r})"
            )
            continue
        parsed = parse_chunk_key(key)
        if parsed is None or parsed.session != session:
            problems.append(
                f"chunk at offset {offset} carries a key this session cannot "
                f"own: {key!r}"
            )
            continue
        prior = seen.get(offset)
        if prior is not None:
            if prior.length != length or prior.sha256 != sha:
                problems.append(
                    f"two different chunks claim offset {offset} "
                    f"({prior.sha256[:12]} vs {sha[:12]})"
                )
            continue
        chunk = _Chunk(offset=offset, length=length, sha256=sha, key=key)
        seen[offset] = chunk
        chunks.append(chunk)
    chunks.sort(key=lambda c: c.offset)
    return chunks, problems


def _generation(chunks: Iterable[_Chunk]) -> str:
    """The identity of the document these ranges describe.

    Chunks are immutable once published -- a store rejects different bytes at a
    claimed offset -- so this value can only change when the publisher throws
    the old document away and re-sends. Re-chunking identical bytes changes it
    too, which costs one needless re-fetch and never risks a spliced file.
    """
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(f"{chunk.offset}:{chunk.length}:{chunk.sha256}\n".encode("utf-8"))
    return digest.hexdigest()


def _prefix_for(chunks: list[_Chunk], base: int, end: int) -> list[_Chunk] | None:
    """The ranges that tile ``[base, end)`` exactly, or None if they do not.

    "Do not" covers a range that vanished, a chunk that now straddles the end of
    the local file (the publisher re-chunked), and a hole -- all of which mean
    the bytes on disk are no longer described by what the store is offering.
    """
    prefix: list[_Chunk] = []
    cursor = base
    for chunk in chunks:
        if cursor >= end:
            break
        if chunk.offset != cursor or chunk.end > end:
            return None
        prefix.append(chunk)
        cursor = chunk.end
    return prefix if cursor == end else None


# Re-seals a local plaintext range so it can be compared against the store's
# ciphertext sha256: (offset, plaintext) -> wire body. None for plaintext
# sessions, where the local bytes hash directly.
_Sealer = Callable[[int, bytes], bytes]


def _verify_local(
    path: Path, base: int, prefix: list[_Chunk], sealer: _Sealer | None = None
) -> bool:
    """Do the bytes on disk really hash to what these ranges claim?

    The expensive answer, used only when there is no recorded generation to
    compare against -- a first pull after an upgrade, or a lost pull state. It
    is the same question the generation answers cheaply on every later run.

    For an encrypted session the store's sha256s are ciphertext hashes while
    the local file is plaintext, so ``sealer`` re-encrypts each range
    deterministically (same DK, same (gen, offset) nonce => identical bytes)
    before hashing. Any failure degrades to "stale -> refetch": safe, merely
    costs bandwidth.
    """
    try:
        with path.open("rb") as handle:
            for chunk in prefix:
                handle.seek(chunk.offset - base)
                body = handle.read(chunk.length)
                if len(body) != chunk.length:
                    return False
                if sealer is not None:
                    try:
                        body = sealer(chunk.offset, body)
                    except CryptoError:
                        return False
                if hashlib.sha256(body).hexdigest() != chunk.sha256:
                    return False
    except OSError:
        return False
    return True


def _stale_reason(
    path: Path,
    record: dict[str, Any] | None,
    chunks: list[_Chunk],
    base: int,
    have: int,
    sealer: _Sealer | None = None,
) -> str | None:
    """Why the local copy can no longer be extended, or None when it can.

    Three tiers, cheapest first. If the offered ranges cannot tile the bytes on
    disk at all, the publisher reset (or re-chunked) -- refetch. If they can,
    the recorded generation is compared: same ranges, same document. Only when
    there is no recorded generation (a pull state from before this field, or a
    lost one) do we pay to re-hash the local bytes against the store's claims.
    """
    prefix = _prefix_for(chunks, base, base + have)
    if prefix is None:
        return "the published ranges no longer cover the local bytes"
    recorded = record.get("generation") if isinstance(record, dict) else None
    if isinstance(recorded, str) and recorded:
        if _generation(prefix) == recorded:
            return None
        return "the published chunk index changed"
    if _verify_local(path, base, prefix, sealer):
        return None
    return "the local bytes do not match what the store holds"


def _plan(chunks: list[_Chunk], have: int) -> tuple[list[_Chunk], list[str]]:
    """Select the chunks that extend the local file from byte ``have`` onward.

    ``have`` is an offset in the *published* document, so for a watermarked
    session it is the watermark plus the length on disk. The result is a
    contiguous run starting exactly there. Anything else -- a hole in the
    published ranges, or a chunk that straddles ``have`` and so would need to be
    spliced -- is refused, because appending across a hole would produce a file
    that is the right size and the wrong bytes.
    """
    plan: list[_Chunk] = []
    problems: list[str] = []
    cursor = have
    for chunk in chunks:
        if chunk.end <= cursor:
            continue  # already on disk
        if chunk.offset < cursor:
            problems.append(
                f"chunk [{chunk.offset}..{chunk.end}) overlaps bytes already "
                f"written up to {cursor}; refusing to splice"
            )
            return [], problems
        if chunk.offset > cursor:
            problems.append(
                f"gap in published bytes: nothing covers [{cursor}..{chunk.offset})"
            )
            return [], problems
        plan.append(chunk)
        cursor = chunk.end
    return plan, problems


# -- pull --------------------------------------------------------------------


def pull(
    transport: Transport,
    store: Store,
    since: str | None = None,
    authors: list[str] | None = None,
    keyid: str | None = None,
) -> PullReport:
    """Fetch every session changed since the cursor and reassemble it locally.

    ``since`` overrides the stored cursor (an explicit re-listing); without it
    the cursor from ``pull-state.json`` is used, so a re-run only pays for
    sessions that actually moved. ``keyid`` scopes the cursor to one keyring
    entry (see :func:`cursor_scope`); a developer's own pull passes None.
    """
    report = PullReport()
    state = load_pull_state(store)
    sessions_state: dict[str, Any] = state.setdefault("sessions", {})
    scope = cursor_scope(authors, keyid)
    cursor = since if since is not None else read_cursor(state, scope)

    try:
        rows = transport.list_sessions(cursor)
    except Exception as exc:  # transport failures are reported, not raised
        report.errors.append(f"listing sessions failed: {exc}")
        return report

    wanted = {a for a in authors} if authors else None
    highest_ok: datetime | None = None
    earliest_failed: datetime | None = None

    pulled_dir(store).mkdir(parents=True, exist_ok=True)

    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            report.errors.append(f"malformed session row: {row!r}")
            continue
        session = _safe_component(row.get("session"))
        author = _safe_component(row.get("author"))
        if session is None or author is None:
            report.errors.append(
                f"session row has an unusable session/author id: {row!r}"
            )
            continue
        if wanted is not None and author not in wanted:
            continue

        updated_at = parse_timestamp(row.get("updated_at")) or parse_timestamp(
            row.get("last_ts")
        )
        before = len(report.errors)
        record = sessions_state.get(session)
        written, base, generation, pin = _pull_one(
            transport, store, row, session, author, report, record
        )
        # A partially written session is still incomplete even though the bytes
        # it did write are verified, so any error at all holds the cursor back.
        failed = written is None or len(report.errors) > before

        if written is not None:
            if record is None:
                report.sessions_new += 1
            elif written > 0:
                report.sessions_updated += 1
            # The record is written even after a partial failure: it records
            # what is genuinely on disk, which is what the next run resumes from.
            sessions_state[session] = _session_record(
                store, row, session, author, base=base, generation=generation,
                pin=pin,
            )

        if failed:
            if updated_at is not None and (
                earliest_failed is None or updated_at < earliest_failed
            ):
                earliest_failed = updated_at
        elif updated_at is not None and (highest_ok is None or updated_at > highest_ok):
            highest_ok = updated_at

    # A failed session must be re-listed next time, so the cursor never moves
    # past one. Successful sessions re-listed alongside it cost nothing: their
    # chunks are already on disk and the plan comes back empty. Written under
    # the scope that produced it (author filter and, for a keyring pull, the
    # key) so no other filter's window is ever skipped on its behalf.
    if earliest_failed is not None:
        write_cursor(state, scope, isoformat(earliest_failed))
    elif highest_ok is not None:
        write_cursor(state, scope, isoformat(highest_ok))

    save_pull_state(store, state)
    return report


def _session_record(
    store: Store,
    row: dict[str, Any],
    session: str,
    author: str,
    base: int = 0,
    generation: str = "",
    pin: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = pulled_path_for(store, author, session)
    size = path.stat().st_size if path.is_file() else 0
    return {
        "session": session,
        "author": author,
        # The downgrade pin (contract 6.4): once a session is recorded as
        # aead-v1 at some generation, a store that later reports it plaintext
        # or at a lower generation is refused, never refetched.
        "enc": str((pin or {}).get("enc") or ""),
        "enc_gen": int((pin or {}).get("enc_gen") or 0),
        "keyid": str((pin or {}).get("keyid") or ""),
        "project": row.get("project"),
        "branch": row.get("branch"),
        "title": row.get("title"),
        "cwd": row.get("cwd"),
        "first_ts": row.get("first_ts"),
        "last_ts": row.get("last_ts"),
        "updated_at": row.get("updated_at"),
        "reported_size": row.get("size"),
        "offset": size,
        # Where in the published document byte 0 of the local file sits. A
        # consented session starts at its opt-in watermark, not at zero.
        "base": base,
        "generation": generation,
        "path": str(path),
        "pulled_at": isoformat(datetime.now(timezone.utc)),
    }


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _decrypt_context(
    transport: Transport,
    session: str,
    label: str,
    row_gen: int,
    report: PullReport,
) -> tuple[bytes, int, str] | None:
    """(DK, generation, keyid) for one encrypted session, or None (reported).

    Everything here is duck-typed off the transport because the pull Protocol
    predates encryption: a transport that cannot supply key material simply
    cannot decrypt, and says so per session instead of failing the whole pull.
    """
    key_set = getattr(transport, "key_set", None)
    if key_set is None:
        report.errors.append(
            f"{label}: cannot decrypt: configure the pasted ezu_/ezr_ key, "
            f"not a raw bearer"
        )
        return None

    # The chunk listing's generation is fresher than the session row's; use it
    # when the transport can supply it, so a rotation between the two requests
    # is caught here as a mismatch rather than later as a tag failure.
    gen = row_gen
    probe = getattr(transport, "session_enc", None)
    if probe is not None:
        try:
            _, listed_gen = probe(session)
        except Exception as exc:
            report.errors.append(f"{label}: reading encryption metadata failed: {exc}")
            return None
        gen = listed_gen or gen

    fetch = getattr(transport, "get_wrapped_keys", None)
    if fetch is None:
        report.errors.append(
            f"{label}: this transport cannot fetch wrapped keys; cannot decrypt"
        )
        return None
    try:
        wraps = fetch(session)
    except Exception as exc:
        report.errors.append(f"{label}: fetching the wrapped key failed: {exc}")
        return None
    if not wraps:
        report.errors.append(
            f"{label}: no wrapped key for this key on the store -- this key "
            f"cannot open this session"
        )
        return None
    wrap_row = wraps[0]
    wrap_gen = _int_or_zero(wrap_row.get("enc_gen"))
    if gen and wrap_gen != gen:
        # A rotation in flight: the wrap and the chunks describe different
        # generations. Per-session error, cursor held, next pull retries.
        report.errors.append(
            f"{label}: wrapped key is for generation {wrap_gen} but the "
            f"session is at {gen} (rotation in flight) -- retrying next pull"
        )
        return None
    gen = wrap_gen if wrap_gen else gen
    if gen < 1:
        report.errors.append(f"{label}: encrypted session reports no generation")
        return None

    # The unwrap AAD is bound to *our* recipient id (device or reader row
    # UUID). A device carries it in config; a reader learns it from the GET
    # response itself (transport caches it -- contract Q2).
    recipient = str(getattr(transport, "recipient_id", "") or "")
    if not recipient:
        report.errors.append(
            f"{label}: cannot determine this key's recipient id for unwrap"
        )
        return None
    try:
        dk = unwrap_dk(
            key_set.enc_key,
            session,
            recipient,
            gen,
            base64.b64decode(str(wrap_row.get("wrap") or "")),
        )
    except (CryptoError, ValueError) as exc:
        report.errors.append(f"{label}: cannot unwrap the session data key: {exc}")
        return None
    return dk, gen, str(getattr(key_set, "keyid", "") or "")


def _pull_one(
    transport: Transport,
    store: Store,
    row: dict[str, Any],
    session: str,
    author: str,
    report: PullReport,
    record: dict[str, Any] | None,
) -> tuple[int | None, int, str, dict[str, Any]]:
    """Append this session's new bytes.

    Returns ``(written, base, generation, pin)`` -- written is None on error.
    ``base`` is where the published document begins: a session shared mid-way
    starts at its opt-in watermark, so the leading gap ``[0, base)`` is the
    consent boundary, not missing data. Holes *between* chunks stay fatal.
    ``pin`` is the encryption state to record (see the downgrade pin below).
    """
    label = f"{author}/{session}"
    target = pulled_path_for(store, author, session)
    target.parent.mkdir(parents=True, exist_ok=True)

    enc = str(row.get("enc") or "")
    row_gen = _int_or_zero(row.get("enc_gen"))
    no_pin: dict[str, Any] = {}

    # Downgrade pin (contract 6.4): a session once pulled as aead-v1 that the
    # store later reports as plaintext, or at a lower generation, is an ERROR
    # -- never a refetch. That shape is a malicious or corrupted store, and
    # accepting plaintext would let the operator substitute forged
    # transcripts for bytes the GCM tags used to authenticate.
    pinned_enc = str(record.get("enc") or "") if isinstance(record, dict) else ""
    pinned_gen = _int_or_zero(record.get("enc_gen")) if isinstance(record, dict) else 0
    if pinned_enc == ENC_SCHEME and enc != ENC_SCHEME:
        report.errors.append(
            f"{label}: this session was pulled encrypted but the store now "
            f"reports it as plaintext -- refusing (possible tampering)"
        )
        return None, 0, "", no_pin

    try:
        chunk_rows = transport.list_chunks(session)
    except Exception as exc:
        report.errors.append(f"{label}: listing chunks failed: {exc}")
        return None, 0, "", no_pin

    chunks, problems = _parse_chunks(
        chunk_rows if isinstance(chunk_rows, list) else [], session
    )
    for problem in problems:
        report.errors.append(f"{label}: {problem}")
    if problems:
        return None, 0, "", no_pin
    if not chunks:
        return 0, 0, "", no_pin

    dk: bytes | None = None
    gen = 0
    keyid = ""
    if enc == ENC_SCHEME:
        context = _decrypt_context(transport, session, label, row_gen, report)
        if context is None:
            return None, 0, "", no_pin
        dk, gen, keyid = context
        if pinned_enc == ENC_SCHEME and gen < pinned_gen:
            report.errors.append(
                f"{label}: the store reports generation {gen} but generation "
                f"{pinned_gen} was already pulled -- refusing (possible tampering)"
            )
            return None, 0, "", no_pin
    pin = {"enc": enc, "enc_gen": gen, "keyid": keyid} if enc else no_pin

    sealer: _Sealer | None = None
    if dk is not None:
        held_dk, held_gen = dk, gen
        sealer = lambda offset, data: encrypt_chunk(  # noqa: E731
            held_dk, session, held_gen, offset, data
        )

    base = chunks[0].offset

    # The file on disk -- not the recorded offset -- is the authority on what
    # we already hold. If a previous run died between appending and saving
    # state, the state is stale and the file is still correct.
    have = target.stat().st_size if target.is_file() else 0

    if have:
        recorded_base = record.get("base", 0) if isinstance(record, dict) else 0
        stale = None
        if not isinstance(recorded_base, int) or recorded_base != base:
            # The document's origin moved: a backfill extended coverage below
            # the old watermark, or a reset re-published from scratch. Either
            # way the local file's byte 0 no longer means what it did.
            stale = "the published document now starts at a different offset"
        else:
            stale = _stale_reason(target, record, chunks, base, have, sealer)
        if stale:
            # Splicing across a generation would produce one document's head
            # on another's tail; a refetch is cheap and provably right. Counted,
            # not an error: holding the cursor back for a recovery that is
            # about to succeed would re-list this session forever.
            report.sessions_refetched += 1
            target.unlink(missing_ok=True)
            have = 0

    plan, gaps = _plan(chunks, base + have)
    for gap in gaps:
        report.errors.append(f"{label}: {gap}")
    if gaps:
        return None, base, "", pin
    if not plan:
        return 0, base, _generation_on_disk(chunks, base, have), pin

    written = 0
    with target.open("ab") as handle:
        for chunk in plan:
            try:
                body = transport.fetch_blob(chunk.key)
            except Exception as exc:
                report.errors.append(
                    f"{label}: fetching chunk at offset {chunk.offset} failed: {exc}"
                )
                break
            # Offsets and lengths are plaintext addressing on every session;
            # an encrypted body carries a 16-byte GCM tag on top (contract 3.2).
            expected = chunk.length + (GCM_TAG if dk is not None else 0)
            if len(body) != expected:
                report.errors.append(
                    f"{label}: chunk at offset {chunk.offset} is {len(body)} bytes, "
                    f"expected {expected} -- NOT appended"
                )
                break
            digest = hashlib.sha256(body).hexdigest()
            if digest != chunk.sha256:
                report.errors.append(
                    f"{label}: chunk at offset {chunk.offset} failed its checksum "
                    f"(got {digest[:12]}, expected {chunk.sha256[:12]}) -- NOT appended"
                )
                break
            if dk is not None:
                # The checksum above proved transport integrity of the
                # ciphertext; the GCM tag now proves authenticity and binds
                # the bytes to (session, gen, offset), so a chunk moved
                # between sessions or offsets dies here.
                try:
                    body = decrypt_chunk(dk, session, gen, chunk.offset, body)
                except CryptoError as exc:
                    report.errors.append(
                        f"{label}: chunk at offset {chunk.offset} failed "
                        f"decryption ({exc}) -- NOT appended"
                    )
                    break
            handle.write(body)
            # Flushed per chunk so the file length always reflects verified
            # bytes only: a crash mid-run leaves a short file, never a torn one.
            handle.flush()
            written += len(body)
            report.chunks += 1
            report.bytes += len(body)

    final = target.stat().st_size if target.is_file() else 0
    reported = row.get("size")
    # The session row reports the full document length; the local file holds
    # only the bytes above the watermark, so the comparison is base-relative.
    if isinstance(reported, int) and base + final > reported:
        report.errors.append(
            f"{label}: reassembled {final} bytes above base {base} but the "
            f"session reports only {reported} -- the local copy may contain "
            f"duplicated bytes"
        )
        return None, base, "", pin
    generation = _generation_on_disk(chunks, base, final)
    if written == 0:
        return (None, base, "", pin) if plan else (0, base, generation, pin)
    return written, base, generation, pin


def _generation_on_disk(chunks: list[_Chunk], base: int, size: int) -> str:
    """Generation of exactly the prefix the local file holds.

    Recorded per pull rather than over the whole chunk list, so a partial
    fetch (network died mid-plan) still records a generation that matches what
    is on disk, and the next run resumes instead of refetching.
    """
    if size <= 0:
        return ""
    prefix = _prefix_for(chunks, base, base + size)
    return _generation(prefix) if prefix else ""


# -- readback ----------------------------------------------------------------


def pulled_sessions(store: Store) -> list[dict[str, Any]]:
    """Metadata for everything pulled so far, newest activity first.

    Keys mirror ``SessionFacts`` where they overlap (``session_id``, ``cwd``,
    ``first_timestamp``, ``last_timestamp``, ``project_slug``) so a caller can
    build Selection-like rows without a translation table, plus the pull-only
    fields (``author``, ``path``) that a local scan has no notion of.
    """
    state = load_pull_state(store)
    rows: list[dict[str, Any]] = []
    for session, record in state.get("sessions", {}).items():
        if not isinstance(record, dict):
            continue
        author = str(record.get("author") or "")
        path = Path(record.get("path") or pulled_path_for(store, author, session))
        exists = path.is_file()
        project = record.get("project")
        rows.append(
            {
                "session_id": session,
                "author": author,
                "project": project,
                # A pulled transcript has no ~/.claude project directory, so
                # the slug falls back to the project name the publisher sent.
                "project_slug": str(project or author or "pulled"),
                "cwd": record.get("cwd"),
                "git_branch": record.get("branch"),
                "title": record.get("title"),
                "first_timestamp": record.get("first_ts"),
                "last_timestamp": record.get("last_ts"),
                "updated_at": record.get("updated_at"),
                "path": str(path),
                "source_path": str(path),
                "size": path.stat().st_size if exists else 0,
                "reported_size": record.get("reported_size"),
                "present": exists,
                "pulled_at": record.get("pulled_at"),
            }
        )
    rows.sort(key=lambda r: str(r.get("last_timestamp") or ""), reverse=True)
    return rows
