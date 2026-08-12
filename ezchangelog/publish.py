"""Incremental publishing of a raw transcript: append-only, resumable, auditable.

A transcript is an append-only JSONL file, so publishing is a tail-follow: keep
the byte offset already sent and ship only what is past it. State lives at
``<store>/publish/<session>.json`` and is written the same atomic way the store
writes its index.

Four things make this less trivial than it sounds:

*Compaction.* Claude Code can rewrite a transcript in place (compaction, or a
resumed session replaying history). The file then shrinks, or keeps its size
while its contents change -- and a naive offset would splice two different
documents together. The guard is therefore a digest of *every byte already
published* (plus a cheap 64 KiB prefix fingerprint and the size, as early
outs). A rewrite that preserves the head and lands on the same length still
changes that digest, and a rewrite we cannot read to check at all is treated as
a rewrite: "cannot tell" must never resolve to "keep appending".

*Consent.* ``dry_run`` must describe the exact bytes that would leave the
machine -- ranges, hashes, keys and previews -- because a developer deciding
whether to share cannot consent to a number. ``start_offset`` is the same idea
persisted: a session that opts in halfway through never publishes the bytes
that came before, not even after a reset.

*Divergence.* State can be lost, restored from a backup, or overtaken by
another machine. Rather than wedging on HTTP 409 forever, the publisher asks
the store what it already holds, verifies those ranges against the local file
and resumes from the end of what genuinely matches (:func:`reconcile`). Every
recovery here is automatic, because the common caller is a hook nobody watches.

*Concurrency.* A hand-run ``ezcl publish`` and a hook-spawned one can hit the
same session at the same instant. The state file is guarded by a lockfile with
a stale timeout, so the loser waits instead of overwriting.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .store import Store, _write_json_atomic
from .transport import SessionMeta, Transport, TransportError, chunk_key
from .window import isoformat

MAX_CHUNK = 8 * 1024 * 1024
PREFIX_BYTES = 64 * 1024
READ_BLOCK = 1024 * 1024
PREVIEW_BYTES = 240

# Chunk history is an audit trail, not an index: publish never reads it back, so
# an append-only list would grow without bound on a long-lived session. Keep the
# most recent entries only.
MAX_STATE_CHUNKS = 64

# How long a publish may wait for another one to finish, and how long a lockfile
# may sit untouched before it is assumed to belong to a dead process. The upload
# refreshes its lock after every chunk, so the stale timeout only has to survive
# one chunk, not one publish.
LOCK_WAIT_SECONDS = 20.0
LOCK_STALE_SECONDS = 300.0

# How many times a publish will recover from a 409 before giving up: once by
# reconciling with the store, once by declaring the remote copy a different
# document and replacing it.
MAX_CONFLICT_RECOVERIES = 2


@dataclass
class Chunk:
    """A byte range of the transcript that still needs uploading.

    Deliberately carries no payload: a full re-upload after compaction can span
    a hundred megabytes, and holding all of it in memory to hash it would be a
    self-inflicted wound. The bytes are read again at send time.
    """

    offset: int
    length: int
    sha256: str

    @property
    def end(self) -> int:
        return self.offset + self.length

    def read(self, path: Path) -> bytes:
        with path.open("rb") as handle:
            handle.seek(self.offset)
            return handle.read(self.length)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChunkReport:
    """What happened (or would happen) to one chunk."""

    offset: int
    length: int
    sha256: str
    key: str = ""
    sent: bool = False
    lines: int = 0
    head: str = ""
    tail: str = ""

    @property
    def end(self) -> int:
        return self.offset + self.length

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PublishState:
    """Persisted at ``<store>/publish/<session>.json``.

    ``start_offset`` is the consent watermark: publishing never begins before
    it, on a first run or after a reset, so bytes written before a developer ran
    ``ezcl share on`` stay on the machine forever.

    ``published_sha256`` is the digest of the published range -- bytes
    ``[start_offset, offset)`` -- and is the authority on whether the transcript
    underneath is still the same document. ``prefix_sha256`` over
    ``prefix_len`` bytes is a cheap early-out that catches the common
    compaction; it is kept because reading 64 KiB is free and reading a
    hundred-megabyte published range is not.
    """

    session: str
    offset: int = 0
    prefix_sha256: str = ""
    prefix_len: int = 0
    published_sha256: str = ""
    size: int = 0
    start_offset: int = 0
    chunks: list[dict[str, Any]] = field(default_factory=list)
    last_published: str | None = None
    store: str = ""

    @staticmethod
    def path_for(store: Store, session: str) -> Path:
        return store.root / "publish" / f"{session}.json"

    @classmethod
    def load(cls, store: Store, session: str) -> PublishState:
        path = cls.path_for(store, session)
        try:
            data = _read_json(path)
        except OSError:
            data = None
        if not isinstance(data, dict):
            return cls(session=session)
        known = cls.__dataclass_fields__
        state = cls(**{k: v for k, v in data.items() if k in known})
        state.session = session  # The filename is authoritative, not the body.
        return state

    def save(self, store: Store) -> Path:
        path = self.path_for(store, self.session)
        _write_json_atomic(path, self.to_dict())
        return path

    @property
    def published(self) -> bool:
        """True when this session has bytes in the store to protect."""
        return self.offset > self.start_offset or bool(self.chunks)

    def record_chunk(self, offset: int, length: int, sha256: str) -> None:
        """Append one sent range, keeping the history bounded."""
        self.chunks.append({"offset": offset, "length": length, "sha256": sha256})
        if len(self.chunks) > MAX_STATE_CHUNKS:
            del self.chunks[: len(self.chunks) - MAX_STATE_CHUNKS]

    def reset(self) -> None:
        """Forget everything sent: the file underneath is a new document.

        ``start_offset`` deliberately survives. It is a consent decision about
        this session, not a fact about the document that just went away.
        """
        self.offset = 0
        self.size = 0
        self.prefix_sha256 = ""
        self.prefix_len = 0
        self.published_sha256 = ""
        self.chunks = []

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Report:
    """The result of a publish, complete enough to show a user before consent."""

    session: str
    transcript: str
    destination: str
    dry_run: bool = False
    file_size: int = 0
    start_offset: int = 0
    final_offset: int = 0
    bytes_sent: int = 0
    skipped: int = 0
    reset_reason: str | None = None
    reconciled: str | None = None
    deleted_remote: bool = False
    chunks: list[ChunkReport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def reset(self) -> bool:
        return self.reset_reason is not None

    @property
    def up_to_date(self) -> bool:
        return not self.chunks

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reset"] = self.reset
        return payload

    def describe(self) -> str:
        """Exactly what left (or would leave) this machine, line by line."""
        verb = "would send" if self.dry_run else "sent"
        lines = [
            f"session {self.session}",
            f"  transcript  {self.transcript} ({self.file_size} bytes)",
            f"  destination {self.destination}",
            f"  already published {self.skipped} bytes (offset {self.start_offset})",
        ]
        if self.reconciled:
            lines.append(f"  RECOVERED: {self.reconciled}")
        if self.reset_reason:
            lines.append(
                f"  RESET: {self.reset_reason} -- re-sending from offset "
                f"{self.start_offset}"
            )
            if self.deleted_remote:
                lines.append("  previously published chunks for this session are dropped")
        if not self.chunks:
            lines.append("  nothing to send")
        for chunk in self.chunks:
            lines.append(
                f"  {verb} bytes {chunk.offset}-{chunk.end - 1} "
                f"({chunk.length} bytes, {chunk.lines} lines) sha256={chunk.sha256[:16]}"
            )
            lines.append(f"      key  {chunk.key}")
            lines.append(f"      head {chunk.head}")
            if chunk.tail:
                lines.append(f"      tail {chunk.tail}")
        lines.append(f"  total {verb}: {self.bytes_sent} bytes in {len(self.chunks)} chunks")
        for warning in self.warnings:
            lines.append(f"  WARNING possible secret: {warning}")
        return "\n".join(lines)


def _read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _prefix_sha256(path: Path, size: int) -> tuple[str, int]:
    """Hash the first ``min(size, 64 KiB)`` bytes -- the cheap fingerprint.

    Returns the digest *and* the length it covers, because the two have to be
    stored together: a checkpoint written halfway through a publish would
    otherwise be re-checked over a different range on the next run and look
    like a rewrite.
    """
    length = min(max(size, 0), PREFIX_BYTES)
    if length <= 0:
        return "", 0
    with path.open("rb") as handle:
        data = handle.read(length)
    return hashlib.sha256(data).hexdigest(), len(data)


def _digest_range(path: Path, start: int, end: int) -> "hashlib._Hash":
    """sha256 over ``[start, end)``. Raises OSError on a short read.

    A short read means the file no longer contains the bytes we claim to have
    published, which is exactly the condition the caller must not paper over.
    """
    digest = hashlib.sha256()
    remaining = max(end - start, 0)
    if remaining <= 0:
        return digest
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining:
            block = handle.read(min(READ_BLOCK, remaining))
            if not block:
                raise OSError(f"{path} ended before byte {end}")
            digest.update(block)
            remaining -= len(block)
    return digest


def _verify_published(
    path: Path, state: PublishState, size: int | None = None
) -> tuple[str | None, "hashlib._Hash"]:
    """(reason to re-send from scratch, running digest of the published range).

    The digest is handed back rather than recomputed later so the send loop can
    keep hashing forward from it: one pass over the published bytes per publish,
    not one per chunk.

    Every failure to *check* is a reason. An unreadable or vanished transcript
    tells us nothing about whether the bytes we published are still the bytes on
    disk, and the safe answer to "I cannot tell" is to re-send, never to append
    to a document that may no longer exist.
    """
    fresh = hashlib.sha256()
    if not state.published:
        return None, fresh  # Nothing published yet: nothing to invalidate.

    if size is None:
        try:
            size = path.stat().st_size
        except OSError as exc:
            return f"transcript cannot be measured ({exc})", fresh

    if size < state.size or size < state.offset:
        return f"transcript shrank from {state.size} to {size} bytes", fresh

    prefix_len = state.prefix_len or min(PREFIX_BYTES, state.size)
    if prefix_len and state.prefix_sha256:
        try:
            current = _digest_range(path, 0, prefix_len).hexdigest()
        except OSError as exc:
            return f"transcript prefix cannot be read ({exc})", fresh
        if current != state.prefix_sha256:
            return "transcript prefix changed (rewritten or compacted)", fresh

    base = min(state.start_offset, state.offset)
    try:
        running = _digest_range(path, base, state.offset)
    except OSError as exc:
        return f"published bytes cannot be re-read ({exc})", fresh

    if not state.published_sha256:
        # State from a version that did not keep this digest. The prefix and
        # size checks above are all that could be applied; adopt the digest now
        # so the next publish gets the strong check. publish() prefers to settle
        # this against the store instead -- see _needs_reconcile.
        return None, running

    if running.hexdigest() != state.published_sha256:
        return (
            "published bytes changed underneath (transcript rewritten in place)",
            fresh,
        )
    return None, running


def detect_reset(
    path: Path, state: PublishState, size: int | None = None
) -> str | None:
    """Reason the transcript must be re-sent from scratch, or None to continue.

    ``size`` lets the caller pass the one ``stat()`` it already took: taking a
    second one here would let a transcript that grew in between look like a
    document that changed.
    """
    return _verify_published(path, state, size)[0]


def plan_start(state: PublishState) -> int:
    """The first byte a publish may send.

    Never before the consent watermark, and never before what is already
    published -- which is the same rule after a reset, because a reset zeroes
    ``offset`` and leaves ``start_offset`` alone.
    """
    return max(state.offset, state.start_offset, 0)


def plan_chunks(
    path: Path,
    state: PublishState,
    max_chunk: int = MAX_CHUNK,
    *,
    size: int | None = None,
) -> list[Chunk]:
    """The byte ranges still to upload, oldest first.

    Applies the compaction guard: when the file was rewritten the plan starts at
    the consent watermark regardless of what the state says. ``state`` is not
    mutated -- the caller decides whether to act on the plan.
    """
    if max_chunk <= 0:
        raise ValueError("max_chunk must be positive")
    if size is None:
        try:
            size = path.stat().st_size
        except OSError:
            return []

    start = min(
        state.start_offset if detect_reset(path, state, size) else plan_start(state),
        size,
    )
    if start >= size:
        return []

    chunks: list[Chunk] = []
    with path.open("rb") as handle:
        handle.seek(start)
        offset = start
        while offset < size:
            length = min(max_chunk, size - offset)
            digest = hashlib.sha256()
            remaining = length
            while remaining:
                block = handle.read(min(READ_BLOCK, remaining))
                if not block:
                    break  # File was truncated mid-plan; stop at what we read.
                digest.update(block)
                remaining -= len(block)
            actual = length - remaining
            if actual <= 0:
                break
            chunks.append(Chunk(offset=offset, length=actual, sha256=digest.hexdigest()))
            offset += actual
    return chunks


# -- concurrency --------------------------------------------------------------


class PublishBusy(TransportError):
    """Another publish holds this session's state file.

    A subclass of TransportError on purpose: ``ezcl publish`` already turns that
    into a one-line error and a non-zero exit, and a brand-new exception type
    would reach the CLI as a traceback instead.
    """


def lock_path(store: Store, session: str) -> Path:
    return store.root / "publish" / f"{session}.lock"


@contextlib.contextmanager
def state_lock(
    store: Store,
    session: str,
    *,
    wait: float = LOCK_WAIT_SECONDS,
    stale: float = LOCK_STALE_SECONDS,
) -> Iterator[Path]:
    """Hold the per-session publish lock, or raise :class:`PublishBusy`.

    ``O_CREAT | O_EXCL`` is the whole mechanism: it is atomic on every
    filesystem this store lives on, including the network shares a team store
    sits on. The stale timeout exists because a killed publisher cannot clean up
    after itself, and a lock nobody can ever break is worse than no lock.
    """
    path = lock_path(store, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(wait, 0.0)
    while True:
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                age = None  # It vanished between the two calls: try again.
            if age is not None and age > stale:
                path.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                raise PublishBusy(
                    f"another publish is already running for session {session} "
                    f"({path} is held); nothing was sent"
                ) from None
            time.sleep(0.1)
            continue
        except OSError as exc:
            # No lock means no state file either, so there is nothing to be
            # gained by continuing without one.
            raise PublishBusy(f"cannot take the publish lock {path}: {exc}") from exc

        try:
            os.write(handle, f"{os.getpid()} {isoformat(datetime.now(timezone.utc))}\n".encode())
        finally:
            os.close(handle)
        try:
            yield path
        finally:
            path.unlink(missing_ok=True)
        return


def _touch(path: Path) -> None:
    """Keep a held lock from ageing into staleness during a long upload."""
    try:
        os.utime(path, None)
    except OSError:
        pass


# -- reconciliation -----------------------------------------------------------


def reconcile(
    session_id: str,
    path: Path,
    transport: Transport,
    state: PublishState,
) -> str | None:
    """Rebuild ``state`` from what the store already holds. Returns a note.

    Used when the local state is missing, stale, or contradicted by an HTTP 409.
    The store's chunk list says which ranges it claims; each claim is checked
    against the bytes actually on disk, and the state resumes from the end of
    the longest verified run. Ranges the store holds that do *not* match the
    local file are ignored -- the transcript is the document of record, and the
    conflict path below is what deals with them.
    """
    try:
        remote = sorted(transport.list_chunks(session_id), key=lambda c: c.offset)
    except TransportError as exc:
        if getattr(exc, "status", None) == 404:
            # The store has never heard of this session -- the normal state of
            # a first publish, which registers it a moment later. Nothing
            # remote means nothing to reconcile against.
            return None
        raise
    if not remote:
        return None

    base = remote[0].offset
    cursor = base
    verified: list[dict[str, Any]] = []
    for chunk in remote:
        if chunk.offset != cursor:
            break  # A hole: nothing past it can be trusted as a prefix.
        try:
            digest = _digest_range(path, chunk.offset, chunk.offset + chunk.length)
        except OSError:
            break  # The local file is shorter than the store claims.
        if digest.hexdigest() != chunk.sha256:
            break  # The store holds different bytes here.
        verified.append(
            {"offset": chunk.offset, "length": chunk.length, "sha256": chunk.sha256}
        )
        cursor = chunk.offset + chunk.length

    if not verified:
        return (
            f"the store holds {len(remote)} chunk(s) for this session that do not "
            f"match this transcript"
        )

    state.start_offset = base
    state.offset = cursor
    state.size = cursor
    state.chunks = verified[-MAX_STATE_CHUNKS:]
    state.prefix_sha256, state.prefix_len = _prefix_sha256(path, cursor)
    state.published_sha256 = _digest_range(path, base, cursor).hexdigest()
    state.store = transport.describe()
    return (
        f"rebuilt publish state from the store: {len(remote)} chunk(s) listed, "
        f"verified through byte {cursor}"
    )


def _needs_reconcile(state: PublishState) -> bool:
    """Should we ask the store what it holds before planning anything?

    Two cases, both of which end in a permanent 409 if left alone: state that
    knows nothing (lost, or never written) while the store may hold plenty, and
    state from a version that kept no published-range digest, which cannot be
    verified locally at all.
    """
    return not state.published or not state.published_sha256


def publish(
    session_id: str,
    transcript: Path | str,
    transport: Transport,
    store: Store,
    meta: SessionMeta | Mapping[str, Any] | None = None,
    *,
    dry_run: bool = False,
    max_chunk: int = MAX_CHUNK,
    scan_secrets: bool = True,
    delete_on_reset: bool = True,
    initial_start_offset: int | None = None,
) -> Report:
    """Upload everything of ``transcript`` that is not already published.

    Nothing is written and nothing leaves the machine when ``dry_run`` is set;
    the returned Report still describes every byte that would.

    ``initial_start_offset`` seeds the consent watermark and is honoured only
    when this session has no state file yet -- it is how ``ezcl share on``
    records "share from here on", and re-running it later must not be able to
    move a watermark that is already protecting bytes.
    """
    path = Path(transcript).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"no transcript at {path}")

    if dry_run:
        # A preview writes nothing, so it takes no lock: making a hook-spawned
        # publish and a person's `--dry-run` contend would be a regression in
        # exactly the moment the person wants an answer.
        return _publish_locked(
            session_id, path, transport, store, meta,
            dry_run=True, max_chunk=max_chunk, scan_secrets=scan_secrets,
            delete_on_reset=delete_on_reset, initial_start_offset=initial_start_offset,
            lock=None,
        )
    with state_lock(store, session_id) as lock:
        return _publish_locked(
            session_id, path, transport, store, meta,
            dry_run=False, max_chunk=max_chunk, scan_secrets=scan_secrets,
            delete_on_reset=delete_on_reset, initial_start_offset=initial_start_offset,
            lock=lock,
        )


def _publish_locked(
    session_id: str,
    path: Path,
    transport: Transport,
    store: Store,
    meta: SessionMeta | Mapping[str, Any] | None,
    *,
    dry_run: bool,
    max_chunk: int,
    scan_secrets: bool,
    delete_on_reset: bool,
    initial_start_offset: int | None,
    lock: Path | None,
) -> Report:
    """The body of :func:`publish`, with the state file already exclusive."""
    info = meta if isinstance(meta, SessionMeta) else SessionMeta(
        session=session_id, **{k: v for k, v in (meta or {}).items()
                               if k in SessionMeta.__dataclass_fields__ and k != "session"}
    )
    info.session = session_id

    is_new = not PublishState.path_for(store, session_id).is_file()
    state = PublishState.load(store, session_id)
    if is_new and initial_start_offset is not None:
        state.start_offset = max(int(initial_start_offset), 0)

    report = Report(
        session=session_id,
        transcript=str(path),
        destination=transport.describe(),
        dry_run=dry_run,
    )

    # One stat for the whole publish. Taking it twice would let a transcript
    # that crossed the 64 KiB fingerprint window between the two calls look like
    # a rewritten document and trigger a needless full re-upload.
    size = path.stat().st_size

    if not dry_run and _needs_reconcile(state):
        report.reconciled = reconcile(session_id, path, transport, state)

    report.reset_reason, running = _verify_published(path, state, size)
    had_published = state.published
    if report.reset_reason:
        state.reset()
        report.deleted_remote = had_published and delete_on_reset

    report.file_size = size
    report.start_offset = min(plan_start(state), size)
    report.skipped = report.start_offset
    report.final_offset = report.start_offset

    plan = plan_chunks(path, state, max_chunk=max_chunk, size=size)
    if not plan:
        return report

    if not dry_run:
        if report.deleted_remote:
            # The old chunks describe a document that no longer exists; leaving
            # them would splice a stale tail onto the rewritten transcript.
            transport.delete_session(session_id)
        # The row must exist before its chunks do, or a puller sees orphans. The
        # watermark rides along so a puller can tell a session that starts at
        # byte 40_000 from an index with its first chunk missing.
        info.start_offset = state.start_offset
        transport.put_session(info)

    prefix, prefix_len = _prefix_sha256(path, size)
    conflicts = 0

    while plan:
        try:
            _send(
                plan, path, session_id, info, transport, store, state, report,
                running=running, prefix=prefix, prefix_len=prefix_len,
                dry_run=dry_run, scan_secrets=scan_secrets, lock=lock,
            )
            return report
        except TransportError as exc:
            if exc.status != 409 or conflicts >= MAX_CONFLICT_RECOVERIES:
                raise
            conflicts += 1
            # 409 means the store already has different bytes at an offset we
            # think is ours: local state and the store disagree about history.
            # First try to agree with the store; if that changes nothing, the
            # store is holding a document this transcript is not, and the
            # transcript wins.
            before = state.offset
            note = reconcile(session_id, path, transport, state)
            if state.offset <= before:
                transport.delete_session(session_id)
                state.reset()
                report.deleted_remote = True
                report.reset_reason = (
                    f"the store holds different bytes for this session "
                    f"(HTTP 409); replaced it from offset {state.start_offset}"
                )
                info.start_offset = state.start_offset
                transport.put_session(info)
            report.reconciled = note or report.reconciled
            state.save(store)
            report.reset_reason, running = _verify_published(path, state, size)
            if report.reset_reason:
                state.reset()
                running = hashlib.sha256()
            plan = plan_chunks(path, state, max_chunk=max_chunk, size=size)
            report.start_offset = min(plan_start(state), size)
    return report


def _send(
    plan: list[Chunk],
    path: Path,
    session_id: str,
    info: SessionMeta,
    transport: Transport,
    store: Store,
    state: PublishState,
    report: Report,
    *,
    running: "hashlib._Hash",
    prefix: str,
    prefix_len: int,
    dry_run: bool,
    scan_secrets: bool,
    lock: Path | None,
) -> None:
    """Ship every chunk of ``plan``, checkpointing the state as it goes."""
    for chunk in plan:
        data = chunk.read(path)
        if scan_secrets:
            for warning in secret_scan(data):
                if warning not in report.warnings:
                    report.warnings.append(warning)

        key = chunk_key(info.author, session_id, chunk.offset, chunk.length)
        entry = ChunkReport(
            offset=chunk.offset,
            length=chunk.length,
            sha256=chunk.sha256,
            key=key,
            lines=data.count(b"\n"),
            head=_preview(data[:PREVIEW_BYTES]),
            tail=_preview(data[-PREVIEW_BYTES:]) if chunk.length > PREVIEW_BYTES else "",
        )

        if not dry_run:
            entry.key = transport.put_chunk(
                session_id, chunk.offset, chunk.length, chunk.sha256, data
            ) or key
            entry.sent = True
            running.update(data)
            state.record_chunk(chunk.offset, chunk.length, chunk.sha256)
            state.offset = chunk.end
            state.size = max(state.size, chunk.end)
            state.prefix_sha256 = prefix
            state.prefix_len = prefix_len
            state.published_sha256 = running.hexdigest()
            state.last_published = isoformat(datetime.now(timezone.utc))
            state.store = transport.describe()
            # Checkpoint per chunk: a crash halfway through a large re-upload
            # must not restart the whole transfer.
            state.save(store)
            if lock is not None:
                _touch(lock)

        report.chunks.append(entry)
        report.bytes_sent += chunk.length
        report.final_offset = chunk.end


def _preview(data: bytes) -> str:
    """A single-line, printable rendering of a byte range."""
    text = data.decode("utf-8", "replace")
    return text.replace("\n", "\\n").replace("\r", "")


# -- secret scanning ---------------------------------------------------------

_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("api key (sk-...)", re.compile(rb"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("github token", re.compile(rb"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}")),
    ("github fine-grained token", re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("aws access key id", re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private key block", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("pem block", re.compile(rb"-----BEGIN (?!.*PRIVATE KEY)[A-Z ]+-----")),
    ("slack token", re.compile(rb"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("google api key", re.compile(rb"\bAIza[0-9A-Za-z_\-]{35}\b")),
    (
        "database url with credentials",
        re.compile(rb"\b(?:postgres|postgresql|mysql|mongodb(?:\+srv)?|redis)://[^\s:@/]+:[^\s@/]+@"),
    ),
)

_B64ISH = re.compile(rb"[A-Za-z0-9+/=_\-]{40,}")
_HEX = re.compile(rb"^[0-9a-fA-F]+$")
_MAX_FINDINGS = 25
_ENTROPY_FLOOR = 4.2


def secret_scan(data: bytes) -> list[str]:
    """Flag byte ranges that look like credentials.

    Advisory only, by design: this never blocks a publish and never touches the
    bytes. Redacting a transcript would make it disagree with what the developer
    actually saw, and a false positive must not be able to silence a session.
    """
    findings: list[str] = []
    for label, pattern in _PATTERNS:
        for match in pattern.finditer(data):
            findings.append(f"{label} at byte {match.start()}: {_redact(match.group())}")
            if len(findings) >= _MAX_FINDINGS:
                return findings

    for match in _B64ISH.finditer(data):
        token = match.group()
        # Hex digests top out at 4.0 bits/char, so the floor above already
        # excludes them; the explicit check keeps long checksums quiet even at
        # the boundary.
        if _HEX.match(token) or _shannon(token) < _ENTROPY_FLOOR:
            continue
        findings.append(
            f"high-entropy string at byte {match.start()}: {_redact(token)}"
        )
        if len(findings) >= _MAX_FINDINGS:
            break
    return findings


def _shannon(token: bytes) -> float:
    counts: dict[int, int] = {}
    for byte in token:
        counts[byte] = counts.get(byte, 0) + 1
    total = len(token)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def _redact(token: bytes) -> str:
    """Show enough to locate the secret, never enough to use it."""
    text = token.decode("utf-8", "replace")
    return text[:6] + "..." if len(text) > 10 else "..."


def iter_warnings(reports: Iterable[Report]) -> list[str]:
    """Flatten warnings across reports, preserving order and dropping repeats."""
    seen: list[str] = []
    for report in reports:
        for warning in report.warnings:
            if warning not in seen:
                seen.append(warning)
    return seen


__all__ = [
    "Chunk",
    "ChunkReport",
    "MAX_CHUNK",
    "MAX_STATE_CHUNKS",
    "PREFIX_BYTES",
    "PublishBusy",
    "PublishState",
    "Report",
    "detect_reset",
    "iter_warnings",
    "lock_path",
    "plan_chunks",
    "plan_start",
    "publish",
    "reconcile",
    "secret_scan",
    "state_lock",
]
