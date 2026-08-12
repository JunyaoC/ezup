"""Incremental publishing: the delta, the cap, and the compaction guard.

The guard is the subtle one. A transcript is treated as append-only, so the
publisher resumes from a stored byte offset -- and if Claude Code rewrites the
file underneath (compaction, a resumed session replaying history), resuming
would splice the tail of one document onto the head of another and produce a
file that is the right size and the wrong bytes. Both detectable forms of that
rewrite are tested here, together with the growth case that must NOT be
mistaken for one.
"""

from __future__ import annotations

from pathlib import Path

from ezchangelog.publish import (
    MAX_CHUNK,
    PREFIX_BYTES,
    PublishState,
    detect_reset,
    plan_chunks,
    publish,
)
from ezchangelog.transport import SessionMeta
from tests.support import TransportTestCase, write_lines

SESSION = "sess-abc"


class PublishTestCase(TransportTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.transcript = self.tmp / "raw" / f"{SESSION}.jsonl"
        self.meta = SessionMeta(
            session=SESSION,
            author=self.author,
            project="demo",
            branch="main",
            cwd=str(self.tmp),
            first_ts="2026-08-11T08:00:00Z",
            last_ts="2026-08-11T09:00:00Z",
            title="a session",
        )

    def publish(self, **kwargs):
        return publish(
            SESSION, self.transcript, self.transport, self.store, self.meta, **kwargs
        )

    def state(self) -> PublishState:
        return PublishState.load(self.store, SESSION)

    def write_exactly(self, size: int, *, fill: bytes = b"a", head: bytes = b"") -> None:
        """Replace the transcript with ``size`` bytes, optionally with a new head."""
        body = head + fill * (size - len(head))
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.write_bytes(body[:size])


class DeltaTests(PublishTestCase):
    def test_first_publish_sends_the_whole_file(self) -> None:
        size = write_lines(self.transcript, 50)

        report = self.publish()

        self.assertEqual(0, report.start_offset)
        self.assertEqual(size, report.bytes_sent)
        self.assertEqual(size, report.final_offset)
        self.assertFalse(report.reset)
        self.assertEqual(self.transcript.read_bytes(), self.remote_bytes(SESSION))
        self.assertEqual(size, self.state().offset)

    def test_republishing_an_unchanged_file_sends_nothing(self) -> None:
        write_lines(self.transcript, 50)
        self.publish()
        before = self.remote_bytes(SESSION)

        report = self.publish()

        self.assertTrue(report.up_to_date)
        self.assertEqual(0, report.bytes_sent)
        self.assertEqual([], report.chunks)
        self.assertFalse(report.reset)
        self.assertEqual(before, self.remote_bytes(SESSION))

    def test_growth_publishes_only_the_delta(self) -> None:
        first = write_lines(self.transcript, 50)
        self.publish()
        total = write_lines(self.transcript, 10, start=50)

        report = self.publish()

        self.assertEqual(first, report.start_offset)
        self.assertEqual(first, report.skipped)
        self.assertEqual(total - first, report.bytes_sent)
        self.assertEqual(1, len(report.chunks))
        self.assertEqual(first, report.chunks[0].offset)
        self.assertEqual(self.transcript.read_bytes(), self.remote_bytes(SESSION))

    def test_many_small_appends_reassemble_exactly(self) -> None:
        for round_index in range(6):
            write_lines(self.transcript, 7, start=round_index * 7)
            self.publish()
        self.assertEqual(self.transcript.read_bytes(), self.remote_bytes(SESSION))
        self.assertEqual(self.transcript.stat().st_size, self.state().offset)

    def test_dry_run_sends_nothing_and_records_nothing(self) -> None:
        size = write_lines(self.transcript, 50)

        report = self.publish(dry_run=True)

        self.assertTrue(report.dry_run)
        self.assertEqual(size, report.bytes_sent, "the report still describes every byte")
        self.assertTrue(all(not chunk.sent for chunk in report.chunks))
        self.assertEqual(0, self.state().offset)
        self.assertEqual([], self.transport.list_chunks(SESSION))
        self.assertFalse(PublishState.path_for(self.store, SESSION).exists())

    def test_missing_transcript_is_an_error(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.publish()


class ChunkCapTests(PublishTestCase):
    def test_chunks_never_exceed_the_cap(self) -> None:
        size = write_lines(self.transcript, 200)
        cap = 4096
        self.assertGreater(size, cap * 3, "fixture must actually need several chunks")

        report = self.publish(max_chunk=cap)

        self.assertGreater(len(report.chunks), 3)
        for chunk in report.chunks:
            self.assertLessEqual(chunk.length, cap)
        self.assertEqual(self.transcript.read_bytes(), self.remote_bytes(SESSION))

    def test_chunks_are_contiguous_and_cover_the_file(self) -> None:
        size = write_lines(self.transcript, 200)

        report = self.publish(max_chunk=4096)

        cursor = 0
        for chunk in report.chunks:
            self.assertEqual(cursor, chunk.offset)
            cursor = chunk.end
        self.assertEqual(size, cursor)

    def test_the_default_cap_is_eight_megabytes(self) -> None:
        self.assertEqual(8 * 1024 * 1024, MAX_CHUNK)

    def test_a_delta_larger_than_the_cap_is_split(self) -> None:
        write_lines(self.transcript, 10)
        self.publish(max_chunk=4096)
        write_lines(self.transcript, 200, start=10)

        report = self.publish(max_chunk=4096)

        self.assertGreater(len(report.chunks), 3)
        for chunk in report.chunks:
            self.assertLessEqual(chunk.length, 4096)
        self.assertEqual(self.transcript.read_bytes(), self.remote_bytes(SESSION))

    def test_a_zero_cap_is_rejected(self) -> None:
        write_lines(self.transcript, 5)
        with self.assertRaises(ValueError):
            plan_chunks(self.transcript, self.state(), max_chunk=0)


class CompactionGuardTests(PublishTestCase):
    """A rewritten transcript must be re-sent from byte zero, always."""

    def test_a_shrinking_transcript_triggers_a_full_reupload(self) -> None:
        write_lines(self.transcript, 200)
        first = self.publish()
        self.assertGreater(first.bytes_sent, 0)

        # Compaction: the same session, a much shorter file.
        self.transcript.write_bytes(b"")
        size = write_lines(self.transcript, 20, body="compacted")
        self.assertLess(size, first.file_size)

        report = self.publish()

        self.assertTrue(report.reset)
        self.assertIn("shrank", report.reset_reason or "")
        self.assertEqual(0, report.start_offset)
        self.assertEqual(0, report.chunks[0].offset)
        self.assertEqual(size, report.bytes_sent, "every byte is re-sent, not just the tail")
        self.assertTrue(report.deleted_remote)

    def test_a_changed_prefix_triggers_a_full_reupload(self) -> None:
        # Larger than the 64 KiB fingerprint window, so the guard is exercised
        # on a file where only a fraction of the bytes are ever hashed.
        self.write_exactly(PREFIX_BYTES * 2)
        first = self.publish()
        self.assertGreater(first.bytes_sent, PREFIX_BYTES)

        # Rewritten in place: same length, different document.
        self.write_exactly(PREFIX_BYTES * 2, head=b"REWRITTEN")

        report = self.publish()

        self.assertTrue(report.reset)
        self.assertIn("prefix changed", report.reset_reason or "")
        self.assertEqual(0, report.start_offset)
        self.assertEqual(PREFIX_BYTES * 2, report.bytes_sent)
        self.assertEqual(self.transcript.read_bytes(), self.remote_bytes(SESSION))

    def test_a_changed_prefix_on_a_longer_file_still_resets(self) -> None:
        self.write_exactly(PREFIX_BYTES * 2)
        self.publish()
        self.write_exactly(PREFIX_BYTES * 3, head=b"REWRITTEN")

        report = self.publish()

        self.assertTrue(report.reset)
        self.assertEqual(PREFIX_BYTES * 3, report.bytes_sent)

    def test_a_change_inside_the_window_of_a_small_file_resets(self) -> None:
        # The fingerprint covers min(size, 64 KiB), so for a small file it is
        # the whole file: any in-place edit is caught.
        self.write_exactly(4096)
        self.publish()
        self.write_exactly(4096, head=b"X")

        report = self.publish()

        self.assertTrue(report.reset)
        self.assertEqual(4096, report.bytes_sent)

    def test_growth_past_the_fingerprint_window_is_not_a_reset(self) -> None:
        # The regression this guards: re-hashing 64 KiB of a file that was only
        # 10 KiB when it was published would compare different byte ranges and
        # declare every growing transcript compacted.
        self.write_exactly(10 * 1024)
        first = self.publish()
        self.assertEqual(10 * 1024, first.bytes_sent)

        with self.transcript.open("ab") as handle:
            handle.write(b"b" * (100 * 1024))

        report = self.publish()

        self.assertFalse(report.reset, report.reset_reason)
        self.assertEqual(10 * 1024, report.start_offset)
        self.assertEqual(100 * 1024, report.bytes_sent)
        self.assertEqual(self.transcript.read_bytes(), self.remote_bytes(SESSION))

    def test_repeated_growth_across_the_window_boundary_stays_intact(self) -> None:
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(5):
            with self.transcript.open("ab") as handle:
                handle.write(b"c" * 30_000)
            report = self.publish()
            self.assertFalse(report.reset, report.reset_reason)
        self.assertEqual(self.transcript.read_bytes(), self.remote_bytes(SESSION))

    def test_a_reset_replaces_the_remote_copy_rather_than_appending(self) -> None:
        write_lines(self.transcript, 200)
        self.publish(max_chunk=4096)
        self.assertGreater(len(self.transport.list_chunks(SESSION)), 3)

        self.transcript.write_bytes(b"")
        write_lines(self.transcript, 5, body="compacted")
        self.publish(max_chunk=4096)

        self.assertEqual(
            self.transcript.read_bytes(),
            self.remote_bytes(SESSION),
            "stale ranges from the pre-compaction document must be gone",
        )
        chunks = self.transport.list_chunks(SESSION)
        self.assertEqual(0, chunks[0].offset)
        self.assertEqual(
            self.transcript.stat().st_size, chunks[-1].offset + chunks[-1].length
        )

    def test_reset_state_is_forgotten_before_the_reupload(self) -> None:
        write_lines(self.transcript, 100)
        self.publish()
        self.transcript.write_bytes(b"tiny\n")

        self.publish()

        state = self.state()
        self.assertEqual(5, state.offset)
        self.assertEqual(5, state.size)
        self.assertEqual([{"offset": 0, "length": 5, "sha256": state.chunks[0]["sha256"]}],
                         state.chunks)

    def test_nothing_published_yet_is_never_a_reset(self) -> None:
        write_lines(self.transcript, 10)
        self.assertIsNone(detect_reset(self.transcript, PublishState(session=SESSION)))

    def test_detect_reset_reports_a_truncation_below_the_offset(self) -> None:
        write_lines(self.transcript, 100)
        self.publish()
        state = self.state()
        self.transcript.write_bytes(b"short\n")
        self.assertIsNotNone(detect_reset(self.transcript, state))

    def test_plan_starts_at_zero_after_a_reset(self) -> None:
        write_lines(self.transcript, 100)
        self.publish()
        state = self.state()
        self.transcript.write_bytes(b"rewritten from scratch\n")

        plan = plan_chunks(self.transcript, state)

        self.assertEqual(1, len(plan))
        self.assertEqual(0, plan[0].offset)

    def test_a_vanished_transcript_plans_nothing(self) -> None:
        self.assertEqual([], plan_chunks(Path(self.tmp / "gone.jsonl"), PublishState(session=SESSION)))


class StateTests(PublishTestCase):
    def test_state_survives_a_reload(self) -> None:
        size = write_lines(self.transcript, 30)
        self.publish()

        state = PublishState.load(self.store, SESSION)
        self.assertEqual(SESSION, state.session)
        self.assertEqual(size, state.offset)
        self.assertEqual(size, state.size)
        self.assertEqual(64, len(state.prefix_sha256))
        self.assertTrue(state.last_published)
        self.assertEqual(self.transport.describe(), state.store)

    def test_unknown_keys_in_a_state_file_are_ignored(self) -> None:
        write_lines(self.transcript, 30)
        self.publish()
        path = PublishState.path_for(self.store, SESSION)
        payload = path.read_text(encoding="utf-8").rstrip().rstrip("}")
        path.write_text(payload + ', "from_a_future_version": 1}', encoding="utf-8")

        state = PublishState.load(self.store, SESSION)
        self.assertGreater(state.offset, 0)

    def test_a_corrupt_state_file_starts_over(self) -> None:
        write_lines(self.transcript, 30)
        self.publish()
        PublishState.path_for(self.store, SESSION).write_text("{{{", encoding="utf-8")

        state = PublishState.load(self.store, SESSION)
        self.assertEqual(0, state.offset)
        self.assertEqual(SESSION, state.session)

    def test_the_filename_wins_over_the_session_in_the_body(self) -> None:
        write_lines(self.transcript, 30)
        self.publish()
        path = PublishState.path_for(self.store, SESSION)
        path.write_text(path.read_text(encoding="utf-8").replace(SESSION, "other"), encoding="utf-8")

        self.assertEqual(SESSION, PublishState.load(self.store, SESSION).session)
