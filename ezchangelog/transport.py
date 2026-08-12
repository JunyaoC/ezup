"""Transports that move raw transcript bytes off this machine.

Two implementations share one interface:

``LocalDirTransport``   a directory (possibly a synced/shared folder). Zero
                        infrastructure, used for tests and for teams who just
                        want a Dropbox path.
``HttpTransport``       the ezupdate Worker, over the documented ``/v1`` wire
                        protocol.

Both address chunks by the same R2-style key so a local store can be lifted to
the Worker (or inspected next to it) without rewriting anything::

    raw/<author>/<session>/<offset padded to 12 digits>-<length>.jsonl

The zero padding is load-bearing: lexical key order must equal byte order, so a
plain directory listing reassembles the transcript.

A key is *never* a path the caller may choose. On the documented shared-folder
deployment every member can write ``index.json``, so a key read back from it is
attacker-controlled input that this module turns into a filesystem operation --
a read for ``ezcl pull``, an ``unlink`` for ``ezcl unpublish``. Every key is
therefore parsed against the grammar above (:func:`parse_chunk_key`) and the
resolved path is confirmed to stay inside the store root before anything opens
it.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .crypto import GCM_TAG, KEY_PREFIXES, CryptoError, KeySet, parse_key

# The store owns the temp-file + os.replace pattern; reuse it rather than
# keeping a second copy that could drift.
from .store import _write_json_atomic
from .window import isoformat

KEY_PREFIX = "raw"
OFFSET_DIGITS = 12

# One path component of a key. The leading character must be alphanumeric,
# which is what rules out ``.``, ``..`` and dotfiles -- the Worker's own id
# check (``[A-Za-z0-9._-]{1,128}``) happily accepts ``..``, so a key coming back
# over the wire cannot be trusted to be traversal-free just because a server
# built it.
_COMPONENT = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"

KEY_PATTERN = re.compile(
    rf"^{KEY_PREFIX}/(?P<author>{_COMPONENT})/(?P<session>{_COMPONENT})/"
    rf"(?P<offset>\d{{{OFFSET_DIGITS}}})-(?P<length>\d{{1,15}})\.jsonl$"
)

DEFAULT_TIMEOUT = 30.0
DEFAULT_UPLOAD_TIMEOUT = 120.0
MAX_TRIES = 3


class TransportError(RuntimeError):
    """A transport call failed in a way the caller should surface verbatim."""

    def __init__(self, message: str, *, status: int | None = None, url: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.url = url


@dataclass
class SessionMeta:
    """The row a session gets in the index, mirroring ``POST /v1/session``.

    ``start_offset`` is the consent watermark: the byte of the transcript the
    first published chunk begins at. A puller needs it to tell a deliberately
    watermarked session (nothing before that byte was ever shared) from an index
    with a hole punched in the front of it. The Worker ignores fields it does
    not know, so sending it costs nothing there and the HTTP puller simply keeps
    demanding coverage from byte 0.
    """

    session: str
    author: str = ""
    project: str = ""
    branch: str = ""
    cwd: str = ""
    first_ts: str = ""
    last_ts: str = ""
    title: str = ""
    level: str = "raw"
    start_offset: int = 0
    # E2E fields (contract 6.1): "" / 0 mean "say nothing" and are dropped from
    # the wire payload, so legacy servers and LocalDirTransport index rows never
    # see keys they do not know about -- and, more importantly, an encrypted
    # session's row is only ever *upgraded* by a client that set them on purpose.
    enc: str = ""
    enc_gen: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if not payload.get("enc"):
            payload.pop("enc", None)
        if not payload.get("enc_gen"):
            payload.pop("enc_gen", None)
        return payload


@dataclass
class ChunkRef:
    """One uploaded byte range, as reported back by ``GET /v1/chunks``."""

    offset: int
    length: int
    sha256: str
    key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SessionInfo:
    """One row from ``GET /v1/sessions``."""

    session: str
    author: str = ""
    project: str = ""
    branch: str = ""
    title: str = ""
    first_ts: str = ""
    last_ts: str = ""
    size: int = 0
    start_offset: int = 0
    updated_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("extra")
        payload.update(self.extra)
        return payload


@dataclass(frozen=True)
class ParsedKey:
    """The four values a well-formed key encodes."""

    author: str
    session: str
    offset: int
    length: int


def safe_component(value: str, fallback: str) -> str:
    """Coerce a name into one legal key component.

    Author names reach this from git config and session ids from Claude Code,
    so neither is guaranteed to be path-safe. Substituting rather than raising
    keeps a publish working for a developer whose git identity happens to
    contain a slash.
    """
    cleaned = "".join(ch if (ch.isascii() and (ch.isalnum() or ch in "._-")) else "-" for ch in value)
    cleaned = cleaned.lstrip("._-")[:128]
    return cleaned or fallback


def chunk_key(author: str, session: str, offset: int, length: int) -> str:
    """The canonical object key for a byte range.

    Both names are coerced to legal components, so this can never mint a key
    that :func:`parse_chunk_key` would later reject (or that would escape the
    store root when a local transport writes it).
    """
    who = safe_component(author, "unknown")
    what = safe_component(session, "unknown")
    return f"{KEY_PREFIX}/{who}/{what}/{max(offset, 0):0{OFFSET_DIGITS}d}-{max(length, 0)}.jsonl"


def parse_chunk_key(key: Any) -> ParsedKey | None:
    """The parts of ``key``, or None when it is not a key we would have written.

    This is the only definition of the key grammar; the puller vets wire keys
    with it too, so there is no second, laxer copy to drift.
    """
    if not isinstance(key, str):
        return None
    match = KEY_PATTERN.match(key)
    if match is None:
        return None
    return ParsedKey(
        author=match["author"],
        session=match["session"],
        offset=int(match["offset"]),
        length=int(match["length"]),
    )


def _as_meta(meta: SessionMeta | Mapping[str, Any]) -> SessionMeta:
    if isinstance(meta, SessionMeta):
        return meta
    known = SessionMeta.__dataclass_fields__
    return SessionMeta(**{k: v for k, v in meta.items() if k in known})


def _as_iso(since: str | datetime | None) -> str | None:
    if since is None:
        return None
    return isoformat(since) if isinstance(since, datetime) else since


class Transport(ABC):
    """Everything the publisher and the puller need from a backend."""

    @abstractmethod
    def put_session(self, meta: SessionMeta | Mapping[str, Any]) -> None:
        """Create or refresh the session row. Must be idempotent."""

    @abstractmethod
    def put_chunk(
        self, session: str, offset: int, length: int, sha256: str, data: bytes
    ) -> str:
        """Upload one byte range and return its key.

        Idempotent by contract: the same ``offset`` + ``sha256`` is a no-op.
        """

    @abstractmethod
    def delete_session(self, session: str) -> None:
        """Remove every chunk of a session and tombstone its row."""

    @abstractmethod
    def list_sessions(self, since: str | datetime | None = None) -> list[SessionInfo]:
        """Sessions updated at or after ``since`` (all of them when None)."""

    @abstractmethod
    def list_chunks(self, session: str) -> list[ChunkRef]:
        """Uploaded ranges for a session, in byte order."""

    @abstractmethod
    def get_blob(self, key: str) -> bytes:
        """Fetch one chunk's raw bytes by key."""

    def describe(self) -> str:
        """One line naming exactly where bytes would go. Shown before consent."""
        return self.__class__.__name__


class LocalDirTransport(Transport):
    """A plain directory. No server, no token, no network.

    ``index.json`` at the root mirrors what the Worker's D1 tables hold, so the
    read side (list_sessions / list_chunks) behaves identically to HTTP.
    """

    def __init__(self, root: Path | str, author: str = "local") -> None:
        self.root = Path(root).expanduser()
        self.author = author or "local"

    # -- keys ----------------------------------------------------------------

    def blob_path(self, key: str) -> Path:
        """Where ``key`` lives, or raise -- never a path outside the root.

        Two checks, because either alone is defeatable. The grammar rejects
        ``..`` segments and absolute keys (``Path(root) / "/x"`` is ``/x``), and
        the realpath comparison rejects a key whose directories were replaced
        with symlinks pointing somewhere else -- which is the shape the attack
        takes on a shared folder, where the index and the tree are both
        writable by everyone.
        """
        if parse_chunk_key(key) is None:
            raise TransportError(
                f"refusing to touch {key!r}: not a raw/<author>/<session>/"
                f"<offset>-<length>.jsonl chunk key"
            )
        root = Path(os.path.realpath(self.root))
        target = Path(os.path.realpath(self.root / key))
        if target != root and root not in target.parents:
            raise TransportError(
                f"refusing to touch {key!r}: it resolves to {target}, outside {root}"
            )
        return target

    # -- index ---------------------------------------------------------------

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def _load_index(self) -> dict[str, Any]:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"sessions": {}}
        if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
            return {"sessions": {}}
        return data

    def _save_index(self, index: dict[str, Any]) -> None:
        _write_json_atomic(self.index_path, index)

    def _record(self, index: dict[str, Any], session: str) -> dict[str, Any]:
        sessions: dict[str, Any] = index.setdefault("sessions", {})
        record = sessions.setdefault(session, {"session": session, "chunks": []})
        record.setdefault("chunks", [])
        return record

    # -- Transport -----------------------------------------------------------

    def put_session(self, meta: SessionMeta | Mapping[str, Any]) -> None:
        info = _as_meta(meta)
        index = self._load_index()
        record = self._record(index, info.session)
        record.update(info.to_dict())
        record["deleted"] = False
        record["updated_at"] = isoformat(datetime.now().astimezone())
        self._save_index(index)

    def put_chunk(
        self, session: str, offset: int, length: int, sha256: str, data: bytes
    ) -> str:
        if len(data) != length:
            raise TransportError(
                f"chunk length mismatch for {session}: declared {length}, got {len(data)}"
            )
        index = self._load_index()
        record = self._record(index, session)
        author = record.get("author") or self.author
        key = chunk_key(author, session, offset, length)

        existing = [c for c in record["chunks"] if c.get("offset") == offset]
        if any(c.get("sha256") == sha256 for c in existing):
            return key  # Same bytes at the same place: nothing to do.

        # The author comes out of the index, which on a shared folder anyone can
        # rewrite -- so the write target is vetted exactly like a read target.
        target = self.blob_path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

        # A re-upload at a known offset supersedes the old range (this happens
        # after a compaction reset), so drop stale entries rather than stacking.
        record["chunks"] = [c for c in record["chunks"] if c.get("offset") != offset]
        record["chunks"].append(
            {"offset": offset, "length": length, "sha256": sha256, "key": key}
        )
        record["chunks"].sort(key=lambda c: c["offset"])
        record["size"] = max(c["offset"] + c["length"] for c in record["chunks"])
        record["updated_at"] = isoformat(datetime.now().astimezone())
        record.setdefault("author", author)
        record.setdefault("session", session)
        self._save_index(index)
        return key

    def delete_session(self, session: str) -> None:
        index = self._load_index()
        record = index.get("sessions", {}).get(session)
        if record is None:
            return
        for chunk in record.get("chunks", []):
            try:
                self.blob_path(str(chunk.get("key", ""))).unlink(missing_ok=True)
            except TransportError:
                # A key that is not ours names a file we did not write, so
                # deleting it would be someone else's data loss. Drop the entry
                # from the index and leave the file alone.
                continue
        directory = (
            self.root
            / KEY_PREFIX
            / safe_component(str(record.get("author") or self.author), "unknown")
            / safe_component(session, "unknown")
        )
        if directory.is_dir():
            try:
                directory.rmdir()
            except OSError:
                pass  # Something else is in there; leaving it is harmless.
        # Tombstone rather than drop: a puller must be able to see the deletion.
        record["chunks"] = []
        record["size"] = 0
        record["deleted"] = True
        record["updated_at"] = isoformat(datetime.now().astimezone())
        self._save_index(index)

    def list_sessions(self, since: str | datetime | None = None) -> list[SessionInfo]:
        cutoff = _as_iso(since)
        known = SessionInfo.__dataclass_fields__
        out: list[SessionInfo] = []
        for record in self._load_index().get("sessions", {}).values():
            if record.get("deleted"):
                continue
            if cutoff and (record.get("updated_at") or "") < cutoff:
                continue
            out.append(
                SessionInfo(
                    **{k: v for k, v in record.items() if k in known and k != "extra"}
                )
            )
        out.sort(key=lambda s: s.updated_at, reverse=True)
        return out

    def list_chunks(self, session: str) -> list[ChunkRef]:
        record = self._load_index().get("sessions", {}).get(session) or {}
        chunks = [
            ChunkRef(
                offset=int(c["offset"]),
                length=int(c["length"]),
                sha256=str(c.get("sha256", "")),
                key=str(c.get("key", "")),
            )
            for c in record.get("chunks", [])
        ]
        chunks.sort(key=lambda c: c.offset)
        return chunks

    def get_blob(self, key: str) -> bytes:
        path = self.blob_path(key)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise TransportError(f"no such blob {key!r} under {self.root}") from exc

    def describe(self) -> str:
        return f"local directory {self.root}"


class HttpTransport(Transport):
    """The ezupdate Worker over HTTPS, stdlib urllib only."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        device_id: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        upload_timeout: float = DEFAULT_UPLOAD_TIMEOUT,
        max_tries: int = MAX_TRIES,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        # A pasted ezu_/ezr_ key never goes on the wire: it is parsed here,
        # once, into the derived bearer (sent) and K_enc (kept). Any other
        # token string -- a raw ezw_ bearer, a test fake -- is sent verbatim
        # and leaves key_set None, which is exactly the signal publish/pull use
        # to refuse encrypted work they could not complete.
        self.key_set: KeySet | None = None
        if token.startswith(KEY_PREFIXES):
            try:
                self.key_set = parse_key(token)
            except CryptoError as exc:
                raise TransportError(f"cannot use the configured key: {exc}") from exc
        # This machine's devices.id UUID (non-secret), needed as the wrap
        # recipient. Learned lazily from GET /v1/wrapped_keys when the config
        # does not carry it -- see get_wrapped_keys.
        self.device_id = device_id
        self.timeout = timeout
        self.upload_timeout = upload_timeout
        self.max_tries = max(1, max_tries)

    @property
    def recipient_id(self) -> str:
        """The id our own wraps are addressed to (device or reader row id)."""
        return self.device_id

    # -- plumbing ------------------------------------------------------------

    def _url(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        url = f"{self.base_url}{path}"
        if params:
            query = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
            url = f"{url}?{query}"
        return url

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
        timeout: float | None = None,
    ) -> bytes:
        url = self._url(path, params)
        bearer = self.key_set.bearer if self.key_set is not None else self.token
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json",
            "User-Agent": "ezchangelog",
        }
        if content_type:
            headers["Content-Type"] = content_type

        last: Exception | None = None
        for attempt in range(self.max_tries):
            request = urllib.request.Request(
                url, data=body, headers=headers, method=method
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=timeout or self.timeout
                ) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                detail = _error_detail(exc)
                # 4xx is a statement about the request itself: retrying sends
                # the same bad request again. 429 is the exception -- it is a
                # statement about timing.
                if exc.code < 500 and exc.code != 429:
                    raise TransportError(
                        f"{method} {url} failed: HTTP {exc.code} {exc.reason}{detail}",
                        status=exc.code,
                        url=url,
                    ) from exc
                last = exc
                delay = _retry_after(exc)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
                delay = None
            if attempt + 1 >= self.max_tries:
                break
            time.sleep(delay if delay is not None else _backoff(attempt))

        status = getattr(last, "code", None)
        raise TransportError(
            f"{method} {url} failed after {self.max_tries} tries: {last}",
            status=status,
            url=url,
        )

    def _json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raw = self._request(*args, **kwargs)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportError(f"malformed JSON from {self.base_url}: {exc}") from exc
        if not isinstance(data, dict):
            raise TransportError(f"expected a JSON object from {self.base_url}")
        return data

    # -- Transport -----------------------------------------------------------

    def put_session(self, meta: SessionMeta | Mapping[str, Any]) -> None:
        payload = json.dumps(_as_meta(meta).to_dict()).encode("utf-8")
        self._json(
            "POST", "/v1/session", body=payload, content_type="application/json"
        )

    def put_chunk(
        self, session: str, offset: int, length: int, sha256: str, data: bytes
    ) -> str:
        # ``length`` is always the *plaintext* length (contract 3.2): offsets
        # and lengths stay plaintext addressing so the chunk-key grammar and
        # coverage math never change. An encrypting caller therefore hands us
        # exactly length + 16 bytes of ct||tag; a plaintext caller exactly
        # length. Anything else is a bug worth stopping before it hits R2.
        expected = (
            (length, length + GCM_TAG) if self.key_set is not None else (length,)
        )
        if len(data) not in expected:
            raise TransportError(
                f"chunk length mismatch for {session}: declared {length}, got {len(data)}"
            )
        response = self._json(
            "POST",
            "/v1/chunk",
            params={
                "session": session,
                "offset": offset,
                "length": length,
                "sha256": sha256,
            },
            body=data,
            content_type="application/octet-stream",
            timeout=self.upload_timeout,
        )
        return str(response.get("key", ""))

    def delete_session(self, session: str) -> None:
        self._json("DELETE", "/v1/session", params={"session": session})

    def list_sessions(self, since: str | datetime | None = None) -> list[SessionInfo]:
        response = self._json("GET", "/v1/sessions", params={"since": _as_iso(since)})
        known = SessionInfo.__dataclass_fields__
        out: list[SessionInfo] = []
        for row in response.get("sessions") or []:
            if not isinstance(row, dict):
                continue
            fields = {k: v for k, v in row.items() if k in known and k != "extra"}
            extra = {k: v for k, v in row.items() if k not in known}
            out.append(SessionInfo(**fields, extra=extra))
        return out

    def list_chunks(self, session: str) -> list[ChunkRef]:
        response = self._json("GET", "/v1/chunks", params={"session": session})
        chunks = [
            ChunkRef(
                offset=int(row.get("offset", 0)),
                length=int(row.get("length", 0)),
                sha256=str(row.get("sha256", "")),
                key=str(row.get("key", "")),
            )
            for row in response.get("chunks") or []
            if isinstance(row, dict)
        ]
        chunks.sort(key=lambda c: c.offset)
        return chunks

    def get_blob(self, key: str) -> bytes:
        # No filesystem is involved here, but the same grammar applies: a key
        # the client would never have written is a key the client should never
        # ask for, and one definition of "well-formed" beats two.
        if parse_chunk_key(key) is None:
            raise TransportError(f"refusing to fetch {key!r}: not a chunk key")
        return self._request("GET", "/v1/blob", params={"key": key})

    # -- encryption metadata and wrapped keys ------------------------------
    # HTTP only, like the reader-token methods below: LocalDirTransport stays
    # plaintext by contract (D-list, section 0), so none of this belongs on
    # the Transport ABC -- encryption lives above the ABC, in publish/pull.

    def session_enc(self, session: str) -> tuple[str, int]:
        """(enc, enc_gen) as the server reports them for one session.

        Read from GET /v1/chunks' top-level fields. ("", 0) for a session the
        server has never seen or a legacy plaintext one -- the two cases are
        equivalent to a publisher: nothing encrypted exists to collide with.
        """
        try:
            data = self._json("GET", "/v1/chunks", params={"session": session})
        except TransportError as exc:
            if exc.status == 404:
                return "", 0
            raise
        try:
            gen = int(data.get("enc_gen") or 0)
        except (TypeError, ValueError):
            gen = 0
        return str(data.get("enc") or ""), gen

    def put_wrapped_keys(self, wraps: list[dict[str, Any]]) -> int:
        """POST /v1/wrapped_keys in batches of <= 500; returns total written.

        500 is the server's per-request cap; batching here means a new-reader
        history backfill over any number of sessions is a handful of round
        trips instead of one per session (D3/D8).
        """
        written = 0
        for start in range(0, len(wraps), 500):
            batch = wraps[start : start + 500]
            data = self._json(
                "POST",
                "/v1/wrapped_keys",
                body=json.dumps({"wraps": batch}).encode("utf-8"),
                content_type="application/json",
            )
            try:
                written += int(data.get("written") or 0)
            except (TypeError, ValueError):
                written += len(batch)
        return written

    def get_wrapped_keys(self, session: str | None = None) -> list[dict[str, Any]]:
        """GET /v1/wrapped_keys, optionally filtered to one session.

        The rows are always the caller's own (recipient is the authenticated
        identity, never a parameter). The response's top-level recipient_id is
        the caller's devices.id -- self-information (contract Q2) -- and is
        cached as our device_id when the config did not carry one, which is
        how a reader key learns the id its wrap AADs are bound to.
        """
        data = self._json("GET", "/v1/wrapped_keys", params={"session": session})
        learned = str(data.get("recipient_id") or "")
        if learned and not self.device_id:
            self.device_id = learned
        wraps = data.get("wraps")
        if not isinstance(wraps, list):
            return []
        return [row for row in wraps if isinstance(row, dict)]

    # -- reader tokens -----------------------------------------------------
    # Device-authenticated management of the read-only tokens a developer
    # hands to an operator. HTTP only: a local directory has no auth, so these
    # deliberately do not exist on the Transport ABC.

    def mint_reader(self, name: str, token_sha256: str) -> dict[str, Any]:
        """POST /v1/token -- the hash is client-supplied (contract 5.1).

        The reader secret is generated on the client and never sent; the
        server stores only sha256 of the derived bearer, so it can
        authenticate the reader later without ever being able to become it.
        """
        return self._json(
            "POST",
            "/v1/token",
            body=json.dumps({"name": name, "token_sha256": token_sha256}).encode(
                "utf-8"
            ),
            content_type="application/json",
        )

    def list_readers(self) -> list[dict[str, Any]]:
        data = self._json("GET", "/v1/tokens")
        tokens = data.get("tokens")
        return tokens if isinstance(tokens, list) else []

    def revoke_reader(self, token_id: str) -> dict[str, Any]:
        return self._json("DELETE", "/v1/token", params={"id": token_id})

    def describe(self) -> str:
        return f"worker {self.base_url}"


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter, so a fleet of hooks does not sync up."""
    return (0.5 * (2**attempt)) + random.uniform(0.0, 0.25)


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    value = exc.headers.get("Retry-After") if exc.headers else None
    try:
        return min(float(value), 30.0) if value else None
    except (TypeError, ValueError):
        return None


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """A snippet of the server's own words; a bare status code helps nobody."""
    try:
        body = exc.read().decode("utf-8", "replace").strip()
    except Exception:
        return ""
    if not body:
        return ""
    return f" -- {body[:400]}"


def build_transport(config: Mapping[str, Any] | str | Path | None) -> Transport:
    """Pick a transport from config: an http(s) URL means the Worker.

    ``config`` may be a bare destination or a mapping carrying ``store`` (the
    destination), ``token`` and ``author``. Anything that is not an http(s) URL
    is a filesystem path -- including ``file://`` URLs.
    """
    if config is None:
        raise TransportError("no store configured; set 'store' in .ez/config.json")

    if isinstance(config, (str, Path)):
        settings: Mapping[str, Any] = {"store": str(config)}
    else:
        settings = config

    destination = str(
        settings.get("store") or settings.get("url") or settings.get("base_url") or ""
    ).strip()
    if not destination:
        raise TransportError("no store configured; set 'store' in .ez/config.json")

    if destination.startswith(("http://", "https://")):
        token = str(settings.get("token") or os.environ.get("EZCHANGELOG_TOKEN") or "")
        if not token:
            raise TransportError(
                f"{destination} needs a device token; run 'ezcl device' or set "
                f"EZCHANGELOG_TOKEN"
            )
        return HttpTransport(
            destination,
            token,
            device_id=str(settings.get("device_id") or ""),
            timeout=float(settings.get("timeout", DEFAULT_TIMEOUT)),
            upload_timeout=float(settings.get("upload_timeout", DEFAULT_UPLOAD_TIMEOUT)),
            max_tries=int(settings.get("max_tries", MAX_TRIES)),
        )

    if destination.startswith("file://"):
        destination = urllib.parse.urlparse(destination).path
    author = str(settings.get("author") or "local")
    return LocalDirTransport(Path(destination).expanduser(), author=author)
