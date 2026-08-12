"""Fixtures every test builds on.

Two invariants are enforced here rather than repeated in each test:

*The developer's real files are unreachable.* ``EZCHANGELOG_HOME`` is pointed
at a throwaway directory for the whole process, because several code paths
(the hook, the status line, the CLI) resolve the store through
``default_store()`` rather than through an argument -- so a test that only
passed a temp ``Store`` around could still reach ``~/.ezchangelog``.

*Paths are resolved.* ``share.find_repo`` resolves its input, and on macOS the
temp directory is reached through the ``/var -> /private/var`` symlink, so an
unresolved fixture path would never compare equal to what the code under test
returns.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from ezchangelog.store import Store
from ezchangelog.transport import LocalDirTransport

REPO_ROOT = Path(__file__).resolve().parent.parent

# Env vars that would otherwise leak the developer's real configuration into a
# test run (a store URL, a token, or the session id of the very session running
# these tests).
HOSTILE_ENV = (
    "EZCHANGELOG_HOME",
    "EZCHANGELOG_TOKEN",
    "EZUPDATE_STORE",
    "EZUPDATE_TOKEN",
    "EZUPDATE_AUTHOR",
    "CLAUDE_CODE_SESSION_ID",
)


class TempHomeTestCase(unittest.TestCase):
    """A test case with its own store root and a scrubbed environment."""

    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory(prefix="ezcl-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()

        saved = {name: os.environ.get(name) for name in HOSTILE_ENV}

        def restore() -> None:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore)
        for name in HOSTILE_ENV:
            os.environ.pop(name, None)

        self.store_root = self.tmp / "store"
        os.environ["EZCHANGELOG_HOME"] = str(self.store_root)
        self.store = Store(self.store_root)
        self.store.ensure()

    # -- fixtures ------------------------------------------------------------

    def make_repo(self, name: str = "repo", **config: object) -> Path:
        """A directory that looks like a project, optionally with a policy."""
        repo = self.tmp / name
        (repo / ".ez").mkdir(parents=True, exist_ok=True)
        if config:
            (repo / ".ez" / "config.json").write_text(
                json.dumps(config), encoding="utf-8"
            )
        return repo

    def make_bare_dir(self, name: str = "plain") -> Path:
        """A directory with no policy of any kind."""
        path = self.tmp / name
        path.mkdir(parents=True, exist_ok=True)
        return path


class TransportTestCase(TempHomeTestCase):
    """Adds a local-directory transport standing in for the Worker."""

    author = "alice"

    def setUp(self) -> None:
        super().setUp()
        self.remote = self.tmp / "remote"
        self.transport = LocalDirTransport(self.remote, author=self.author)

    def remote_bytes(self, session: str) -> bytes:
        """Reassemble what the remote holds, in offset order."""
        return b"".join(
            self.transport.get_blob(chunk.key)
            for chunk in self.transport.list_chunks(session)
        )


def write_lines(path: Path, count: int, *, start: int = 0, body: str = "hello") -> int:
    """Append ``count`` JSONL records; returns the resulting file size.

    Deliberately low-entropy text: the publisher scans every byte it sends for
    secrets, and random filler would trip the entropy heuristic and turn an
    unrelated assertion into a warning storm.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        for index in range(start, start + count):
            record = {
                "type": "user",
                "uuid": f"{index:08d}",
                "timestamp": "2026-08-11T08:00:00Z",
                "message": {"role": "user", "content": f"{body} " * 24},
            }
            handle.write(json.dumps(record).encode("utf-8") + b"\n")
    return path.stat().st_size


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
