"""Publish then pull a multi-megabyte transcript and demand byte-identity.

The whole pull side exists so a PM's copy can be fed to the same collector the
developer's own transcript would go through. A copy that is off by one byte
would still parse, still summarise, and quietly report on something that never
happened -- so the assertion here is a sha256 of the whole file, not a size or
a line count.

Two stores are used deliberately: ``self.store`` is the publisher's (it holds
the publish offsets) and ``pm_store`` is a different machine's (it holds the
pull cursor and the reassembled file). Sharing one store between them would
hide a bug where one side reads the other's state.
"""

from __future__ import annotations

import hashlib

from ezchangelog.config import PullView
from ezchangelog.publish import publish
from ezchangelog.pull import pull, pulled_path_for, pulled_sessions
from ezchangelog.store import Store
from ezchangelog.transport import SessionMeta
from tests.support import TransportTestCase, sha256_file, write_lines

SESSION = "sess-roundtrip"

# ~200 bytes a line, so this clears three megabytes and forces several chunks
# at the 1 MiB cap used below.
BIG_LINES = 16_000


class RoundTripTests(TransportTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.transcript = self.tmp / "raw" / f"{SESSION}.jsonl"
        self.pm_store = Store(self.tmp / "pm-store")
        self.pm_store.ensure()
        self.view = PullView(self.transport)
        self.meta = SessionMeta(
            session=SESSION,
            author=self.author,
            project="demo",
            branch="main",
            cwd=str(self.tmp),
            first_ts="2026-08-11T08:00:00Z",
            last_ts="2026-08-11T09:00:00Z",
            title="a long session",
        )

    def publish(self, **kwargs):
        return publish(
            SESSION, self.transcript, self.transport, self.store, self.meta, **kwargs
        )

    @property
    def pulled(self):
        return pulled_path_for(self.pm_store, self.author, SESSION)

    def test_multi_megabyte_transcript_survives_the_round_trip(self) -> None:
        size = write_lines(self.transcript, BIG_LINES)
        self.assertGreater(size, 3 * 1024 * 1024, "fixture must be multi-megabyte")

        report = self.publish(max_chunk=1024 * 1024, scan_secrets=False)
        self.assertGreaterEqual(len(report.chunks), 3)
        self.assertEqual(size, report.bytes_sent)

        pulled = pull(self.view, self.pm_store)

        self.assertEqual([], pulled.errors)
        self.assertTrue(pulled.ok)
        self.assertEqual(1, pulled.sessions_new)
        self.assertEqual(size, pulled.bytes)
        self.assertEqual(len(report.chunks), pulled.chunks)

        self.assertTrue(self.pulled.is_file())
        self.assertEqual(size, self.pulled.stat().st_size)
        self.assertEqual(
            sha256_file(self.transcript),
            sha256_file(self.pulled),
            "the reassembled transcript is not byte-identical to the original",
        )

    def test_a_second_round_trip_appends_only_the_delta(self) -> None:
        first = write_lines(self.transcript, BIG_LINES)
        self.publish(max_chunk=1024 * 1024, scan_secrets=False)
        pull(self.view, self.pm_store)

        total = write_lines(self.transcript, 2_000, start=BIG_LINES)
        self.publish(max_chunk=1024 * 1024, scan_secrets=False)

        second = pull(self.view, self.pm_store)

        self.assertEqual([], second.errors)
        self.assertEqual(total - first, second.bytes, "only the new bytes are fetched")
        self.assertEqual(1, second.sessions_updated)
        self.assertEqual(sha256_file(self.transcript), sha256_file(self.pulled))

    def test_pulling_again_with_no_change_fetches_nothing(self) -> None:
        write_lines(self.transcript, 500)
        self.publish(max_chunk=1024 * 1024)
        pull(self.view, self.pm_store)
        digest = sha256_file(self.pulled)

        again = pull(self.view, self.pm_store)

        self.assertEqual([], again.errors)
        self.assertEqual(0, again.chunks)
        self.assertEqual(0, again.bytes)
        self.assertEqual(digest, sha256_file(self.pulled))

    def test_pull_records_the_session_metadata(self) -> None:
        write_lines(self.transcript, 500)
        self.publish()
        pull(self.view, self.pm_store)

        rows = pulled_sessions(self.pm_store)
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual(SESSION, row["session_id"])
        self.assertEqual(self.author, row["author"])
        self.assertEqual("demo", row["project"])
        self.assertEqual("main", row["git_branch"])
        self.assertTrue(row["present"])
        self.assertEqual(self.transcript.stat().st_size, row["size"])

    def test_only_requested_authors_are_pulled(self) -> None:
        write_lines(self.transcript, 100)
        self.publish()

        skipped = pull(self.view, self.pm_store, authors=["someone-else"])

        self.assertEqual([], skipped.errors)
        self.assertEqual(0, skipped.sessions_new)
        self.assertFalse(self.pulled.exists())

    def test_a_corrupted_chunk_is_never_appended(self) -> None:
        write_lines(self.transcript, 400)
        self.publish(max_chunk=8192)
        chunks = self.transport.list_chunks(SESSION)
        self.assertGreater(len(chunks), 2)

        # Corrupt the second chunk in place, leaving its length (and so the
        # index) intact: only the checksum can catch this.
        victim = self.remote / chunks[1].key
        body = bytearray(victim.read_bytes())
        body[0] ^= 0xFF
        victim.write_bytes(bytes(body))

        report = pull(self.view, self.pm_store)

        self.assertFalse(report.ok)
        self.assertTrue(any("checksum" in error for error in report.errors), report.errors)
        # Everything before the bad chunk is verified, so it is kept; the bad
        # bytes and everything after them are not on disk.
        self.assertEqual(chunks[0].length, self.pulled.stat().st_size)
        self.assertEqual(
            hashlib.sha256(self.transcript.read_bytes()[: chunks[0].length]).hexdigest(),
            sha256_file(self.pulled),
        )

    def test_a_truncated_chunk_is_never_appended(self) -> None:
        write_lines(self.transcript, 400)
        self.publish(max_chunk=8192)
        chunks = self.transport.list_chunks(SESSION)
        victim = self.remote / chunks[1].key
        victim.write_bytes(victim.read_bytes()[:-10])

        report = pull(self.view, self.pm_store)

        self.assertFalse(report.ok)
        self.assertTrue(any("expected" in error for error in report.errors), report.errors)
        self.assertEqual(chunks[0].length, self.pulled.stat().st_size)

    def test_a_hole_in_the_published_ranges_is_refused(self) -> None:
        write_lines(self.transcript, 400)
        self.publish(max_chunk=8192)

        # Drop the first range from the index, leaving a gap at byte 0.
        index = self.transport._load_index()
        record = index["sessions"][SESSION]
        record["chunks"] = record["chunks"][1:]
        self.transport._save_index(index)

        report = pull(self.view, self.pm_store)

        self.assertFalse(report.ok)
        self.assertTrue(any("gap" in error for error in report.errors), report.errors)
        self.assertFalse(self.pulled.exists() and self.pulled.stat().st_size > 0)

    def test_a_failed_session_holds_the_cursor_back(self) -> None:
        write_lines(self.transcript, 400)
        self.publish(max_chunk=8192)
        chunks = self.transport.list_chunks(SESSION)
        victim = self.remote / chunks[1].key
        good = victim.read_bytes()
        victim.write_bytes(good[:-10])

        self.assertFalse(pull(self.view, self.pm_store).ok)

        # Repair the store; the next pull must still consider this session.
        victim.write_bytes(good)
        recovered = pull(self.view, self.pm_store)

        self.assertEqual([], recovered.errors)
        self.assertEqual(sha256_file(self.transcript), sha256_file(self.pulled))

    def test_a_hostile_session_id_cannot_escape_the_pulled_directory(self) -> None:
        write_lines(self.transcript, 10)
        self.publish()
        index = self.transport._load_index()
        record = index["sessions"].pop(SESSION)
        record["session"] = "../../escaped"
        index["sessions"]["../../escaped"] = record
        self.transport._save_index(index)

        report = pull(self.view, self.pm_store)

        self.assertFalse(report.ok)
        self.assertFalse((self.tmp / "escaped.jsonl").exists())
        self.assertFalse((self.pm_store.root.parent / "escaped.jsonl").exists())
