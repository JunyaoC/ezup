"""The PM keyring: several reader keys, one store-private file, one pull loop.

A developer shares a session by handing a PM one pasted ``ezr_`` key. A PM who
reads for several developers therefore holds several keys, each unlocking a
different slice of the store. This module is where those keys live between
pulls: ``<store>/keyring.json`` (mode 0600, ``ezr_``-only, contract 6.5), plus
the small amount of logic that a ``pull`` loop needs on top of it.

WHY a dedicated file and not ``config.json``: a keyring is a *set* of reader
credentials with per-key bookkeeping (which reader row each key is, when it was
last pulled, which store it belongs to), while ``config.json`` names this
machine's single publish identity. Mixing them would force the config's
one-token model to grow a list, and would put reader secrets on the same page
as the device's own. Same protection class as ``config.json`` / ``readers.json``
(machine-private, never read from a repo), enforced here with a 0600 write.

Two rules carry the security argument:

* Only ``ezr_`` keys are accepted (:meth:`Keyring.add` refuses ``ezu_``). A
  device key in a PM's keyring would hand a reader-shaped tool the ability to
  *publish*, and the wrap-backfill path in ``token mint`` assumes the keyring
  holds readers, never devices.

* The file stores the pasted secret (it is what derives the bearer each pull),
  so no code path here prints it back: :meth:`Keyring.list` and
  :meth:`KeyEntry.redacted` expose the keyid -- a public fingerprint -- and
  never the token, mirroring ``Config.describe``'s discipline.

``reader_id`` (the ``devices.id`` UUID an unwrap AAD is bound to, contract 6.4)
is not derivable from a key. It is learned from the first ``GET /v1/wrapped_keys``
response at add-time when a wrap already exists, and otherwise left empty and
filled on the first successful pull -- so this module keeps it, but never
requires it to be present.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .crypto import CryptoError, KeySet, parse_key
from .store import Store

KEYRING_VERSION = 1

# The runner (contract 6.5) points at its own keyring with this; a PM on their
# own machine uses <store>/keyring.json. Resolved in keyring_path().
KEYRING_ENV = "EZUP_KEYRING"

# Reader keys only: a device key in a reader's keyring is a category error (see
# the module docstring). parse_key already refuses non-ezu_/ezr_ strings; this
# is the narrower "readers, not devices" gate.
_READER_PREFIX = "ezr_"


def keyring_path(store: Store) -> Path:
    """Where this machine's keyring lives: ``$EZUP_KEYRING`` if set, else
    ``<store>/keyring.json``. The env override is how the remote runner points
    a headless pull at a keyring outside any one store directory."""
    override = os.environ.get(KEYRING_ENV, "").strip()
    return Path(override).expanduser() if override else store.root / "keyring.json"


@dataclass
class KeyEntry:
    """One reader key and its bookkeeping. ``token`` is the pasted secret and
    never leaves this object except to derive a bearer -- it is excluded from
    every human-facing view (:meth:`redacted`)."""

    token: str
    keyid: str
    reader_id: str = ""      # devices.id the wrap AAD binds to; learned lazily
    label: str = ""          # human tag, defaults to the probed author
    store: str = ""          # the store URL this key reads from
    added_at: str = ""
    last_pull_status: str = ""
    last_pull_at: str = ""

    @property
    def key_set(self) -> KeySet:
        """Derive the bearer/K_enc/keyid on demand. Not stored: a KeySet holds
        K_enc, which must never be serialised (contract section 2)."""
        return parse_key(self.token)

    def redacted(self) -> dict[str, Any]:
        """Everything about this key except the secret. What ``keyring list``
        and any status view may print."""
        return {
            "keyid": self.keyid,
            "reader_id": self.reader_id,
            "label": self.label,
            "store": self.store,
            "added_at": self.added_at,
            "last_pull_status": self.last_pull_status,
            "last_pull_at": self.last_pull_at,
        }

    def to_dict(self) -> dict[str, Any]:
        """The on-disk row -- includes the token, so it is written only through
        :meth:`Keyring.save`'s 0600 file, never logged."""
        return {
            "token": self.token,
            "keyid": self.keyid,
            "reader_id": self.reader_id,
            "label": self.label,
            "store": self.store,
            "added_at": self.added_at,
            "last_pull_status": self.last_pull_status,
            "last_pull_at": self.last_pull_at,
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "KeyEntry | None":
        """Parse one on-disk row, or None if it is unusable. A single broken
        entry must not make the whole keyring unreadable -- the same
        degrade-don't-fail rule ``_load_readers`` follows."""
        token = str(row.get("token") or "")
        if not token.startswith(_READER_PREFIX):
            return None
        try:
            keyid = parse_key(token).keyid
        except CryptoError:
            return None
        return cls(
            token=token,
            # The keyid is derived, never trusted from disk: a hand-edited file
            # cannot make a key claim a fingerprint that is not its own.
            keyid=keyid,
            reader_id=str(row.get("reader_id") or ""),
            label=str(row.get("label") or ""),
            store=str(row.get("store") or ""),
            added_at=str(row.get("added_at") or ""),
            last_pull_status=str(row.get("last_pull_status") or ""),
            last_pull_at=str(row.get("last_pull_at") or ""),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json_0600(path: Path, payload: Any) -> None:
    """Atomic write at mode 0600. Unlike ``store._write_json_atomic`` the temp
    file is created with the restrictive mode from the outset, so the secret is
    never briefly world-readable between write and chmod."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=False)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class DuplicateKeyError(Exception):
    """add() of a key already in the ring (same keyid). Caller decides whether
    that is an error or a no-op; the keyring never silently keeps two copies."""


@dataclass
class Keyring:
    """The set of reader keys on this machine, plus the file they persist to.

    Ordered: :meth:`resolve` returns the first entry that can open a session, so
    add-order is the tie-break when two keys both hold a wrap for it.
    """

    path: Path
    entries: list[KeyEntry] = field(default_factory=list)

    # -- persistence ---------------------------------------------------------

    @classmethod
    def load(cls, store: Store, *, path: Path | None = None) -> "Keyring":
        """Load the keyring for ``store`` (or an explicit ``path``). A missing
        or malformed file is an empty ring, never an error: a PM whose keyring
        is broken should get "no keys", not a crash mid-pull."""
        target = path if path is not None else keyring_path(store)
        entries: list[KeyEntry] = []
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return cls(path=target, entries=entries)
        if not isinstance(data, dict) or data.get("version") != KEYRING_VERSION:
            return cls(path=target, entries=entries)
        for row in data.get("keys") or []:
            if isinstance(row, dict):
                entry = KeyEntry.from_dict(row)
                if entry is not None:
                    entries.append(entry)
        return cls(path=target, entries=entries)

    def save(self) -> None:
        """Persist at mode 0600. The only writer of the token to disk."""
        _write_json_0600(
            self.path,
            {"version": KEYRING_VERSION, "keys": [e.to_dict() for e in self.entries]},
        )

    # -- mutation ------------------------------------------------------------

    def add(
        self,
        token: str,
        *,
        label: str = "",
        store: str = "",
        reader_id: str = "",
    ) -> KeyEntry:
        """Add a reader key. Refuses anything but a valid ``ezr_`` key, and
        refuses a keyid already present (a device key or a second copy is a
        mistake, not something to silently absorb). Does not save -- the caller
        saves once after any probing that fills in ``reader_id``/``label``."""
        keys = parse_key(token)  # raises CryptoError on a malformed key
        if keys.kind != "reader":
            raise CryptoError(
                "keyring holds reader keys only: an ezu_ device key cannot be "
                "added (it would let a reader-shaped tool publish)"
            )
        if self.get(keys.keyid) is not None:
            raise DuplicateKeyError(
                f"a key with keyid {keys.keyid} is already in the keyring"
            )
        entry = KeyEntry(
            token=token,
            keyid=keys.keyid,
            reader_id=reader_id,
            label=label,
            store=store,
            added_at=_now(),
        )
        self.entries.append(entry)
        return entry

    def remove(self, selector: str) -> bool:
        """Remove the entry whose keyid or label matches ``selector``. Returns
        True if one was removed. Does not save; the caller saves. Deleting an
        entry stops this machine pulling with the key -- it is not revocation:
        already-pulled ``pulled/`` transcripts stay, and cutting a developer's
        sharing still requires that developer's ``token revoke`` (contract 6.5).
        """
        keep = [
            e for e in self.entries
            if e.keyid != selector and (not e.label or e.label != selector)
        ]
        removed = len(keep) != len(self.entries)
        self.entries = keep
        return removed

    # -- queries -------------------------------------------------------------

    def list(self) -> list[KeyEntry]:
        """The entries, in add-order. Callers that render them use
        :meth:`KeyEntry.redacted` so no token is ever printed."""
        return list(self.entries)

    def get(self, keyid_or_label: str) -> KeyEntry | None:
        """One entry by keyid (exact) or label (exact). keyid wins, since it is
        unique by construction while labels are free text."""
        for entry in self.entries:
            if entry.keyid == keyid_or_label:
                return entry
        for entry in self.entries:
            if entry.label and entry.label == keyid_or_label:
                return entry
        return None

    def resolve(
        self, session: str, wrapped_by: Mapping[str, Iterable[str]]
    ) -> KeyEntry | None:
        """Which key unlocks ``session`` -- the one that holds its wrap.

        A wrapped key is the only thing that can open an encrypted session
        (contract 6.4), so "which key holds a wrap for this session" *is* "which
        key unlocks it". ``wrapped_by`` maps a key's keyid to the sessions that
        key holds a wrap for -- the pull loop builds it by asking each key's
        ``GET /v1/wrapped_keys`` (bulk) once, the same set :func:`pull` already
        fetches. Returns the first entry in add-order whose wrap set contains
        ``session``, or None when no held key can open it.
        """
        for entry in self.entries:
            sessions = wrapped_by.get(entry.keyid)
            if sessions and session in set(sessions):
                return entry
        return None

    def mark_pull(self, keyid: str, status: str) -> None:
        """Record the outcome of a pull for one key (``ok``/``unauthorized``/...)
        so ``keyring list`` can show last-pull status. Does not save; the pull
        loop saves once at the end. A no-op for an unknown keyid."""
        entry = self.get(keyid)
        if entry is not None:
            entry.last_pull_status = status
            entry.last_pull_at = _now()

    def learn_reader_id(self, keyid: str, reader_id: str) -> bool:
        """Fill in a key's ``reader_id`` once a pull has learned it from the
        store (the add-time probe may have had no wrap to learn it from).
        Returns True if it changed something worth saving."""
        entry = self.get(keyid)
        if entry is not None and reader_id and entry.reader_id != reader_id:
            entry.reader_id = reader_id
            return True
        return False


__all__ = [
    "DuplicateKeyError",
    "KEYRING_ENV",
    "KEYRING_VERSION",
    "KeyEntry",
    "Keyring",
    "keyring_path",
]


def load_keyring(store: Store, *, path: Path | None = None) -> Keyring:
    """Module-level entry point the CLI resolves by name.

    The CLI looks up ``keyring.load_keyring`` rather than calling
    ``Keyring.load`` directly so the whole module can be optional at import;
    keeping the two in lockstep is why this thin wrapper exists.
    """
    return Keyring.load(store, path=path)
