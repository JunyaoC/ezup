"""Regressions for the consent findings (C1, C1b, C2, C3, C4, C5).

Each test here pins a fix to a critical finding, and each is written to fail
against the pre-fix behaviour:

C1   an acknowledgement is bound to the policy *text* (fingerprint), so any
     edit to the committed config voids it -- the old path-keyed ack survived
     a hostile `git pull` that repointed the store.
C1b  `share ack` refuses a repo that declares no policy -- acknowledging
     silence would pre-accept whatever the repo says after the next pull.
C2   a "token" (however spelled) in a committed .ez/config.json is ignored:
     a repo that could name the destination AND authenticate to it turns one
     malicious commit into a complete exfiltration payload.
C3   a root-level "never" governs every directory beneath it, including one
     that grew its own nested .ez -- and `ezcl share on` refuses under it.
C4   opting in mid-session watermarks the transcript at its current size, so
     only bytes written after the opt-in ever leave the machine -- even after
     a compaction reset.
C5   `sync` refuses a session the developer explicitly turned off.
"""

from __future__ import annotations

import json

from ezchangelog import cli, share
from ezchangelog.config import load_config, transport_for
from ezchangelog.publish import PublishState, publish
from ezchangelog.transport import SessionMeta, TransportError
from tests.support import TempHomeTestCase, TransportTestCase, write_lines

SESSION = "sess-consent"


class AckFingerprintTests(TempHomeTestCase):
    """C1: the ack names a policy document, not a directory."""

    def test_ack_is_void_after_the_policy_changes(self) -> None:
        repo = self.make_repo("team", share="always", store="https://good.example/v1")
        share.acknowledge(repo, self.store)
        self.assertTrue(share.resolve(SESSION, repo, self.store).sharing)

        # A teammate commits a different destination. The `share` key is
        # untouched, so a path-keyed ack would keep uploading -- to the new
        # store -- with no human in the loop.
        (repo / ".ez" / "config.json").write_text(
            json.dumps({"share": "always", "store": "https://evil.example/v1"}),
            encoding="utf-8",
        )

        self.assertEqual(share.ACK_STALE, share.ack_status(repo, self.store))
        decision = share.resolve(SESSION, repo, self.store)
        self.assertFalse(decision.sharing, "an edited policy must revert to off")
        self.assertIn("changed", decision.reason)

    def test_reformatting_the_config_is_not_a_policy_change(self) -> None:
        # The fingerprint is over canonical JSON: nagging about whitespace
        # would train people to re-ack blind.
        repo = self.make_repo("team", share="always", store="https://good.example/v1")
        share.acknowledge(repo, self.store)

        (repo / ".ez" / "config.json").write_text(
            '{\n  "store": "https://good.example/v1",\n  "share": "always"\n}\n',
            encoding="utf-8",
        )

        self.assertEqual(share.ACK_VALID, share.ack_status(repo, self.store))
        self.assertTrue(share.resolve(SESSION, repo, self.store).sharing)

    def test_flipping_a_non_share_key_still_voids_the_ack(self) -> None:
        # Every key is hashed, because a key this version ignores may move
        # bytes in the next one.
        repo = self.make_repo("team", share="always", store="https://good.example/v1")
        share.acknowledge(repo, self.store)

        (repo / ".ez" / "config.json").write_text(
            json.dumps(
                {"share": "always", "store": "https://good.example/v1", "extra": 1}
            ),
            encoding="utf-8",
        )

        self.assertEqual(share.ACK_STALE, share.ack_status(repo, self.store))
        self.assertFalse(share.resolve(SESSION, repo, self.store).sharing)


class AckRefusesSilenceTests(TempHomeTestCase):
    """C1b: there is nothing to acknowledge in a repo that says nothing."""

    def test_ack_refuses_a_repo_with_no_config_at_all(self) -> None:
        repo = self.make_repo("silent")  # .ez exists, no config.json
        with self.assertRaises(share.ShareRefused):
            share.acknowledge(repo, self.store)
        self.assertFalse(share.ack_path(self.store, repo).exists())

    def test_ack_refuses_a_config_without_a_share_key(self) -> None:
        repo = self.make_repo("storeonly", store="https://ez.example/v1")
        with self.assertRaises(share.ShareRefused):
            share.acknowledge(repo, self.store)
        self.assertFalse(share.ack_path(self.store, repo).exists())
        self.assertEqual(share.ACK_NONE, share.ack_status(repo, self.store))

    def test_a_refused_ack_leaves_sharing_off_when_a_policy_appears_later(self) -> None:
        # The attack the refusal exists to stop: ack an empty repo today,
        # inherit whatever it commits tomorrow.
        repo = self.make_repo("silent")
        with self.assertRaises(share.ShareRefused):
            share.acknowledge(repo, self.store)

        (repo / ".ez" / "config.json").write_text(
            json.dumps({"share": "always"}), encoding="utf-8"
        )

        self.assertFalse(share.resolve(SESSION, repo, self.store).sharing)


class CommittedTokenTests(TempHomeTestCase):
    """C2: a committed config supplies policy, never credentials."""

    def _repo_with_token(self) -> object:
        repo = self.make_repo("tokened")
        (repo / ".ez" / "config.json").write_text(
            json.dumps(
                {
                    "share": "ask",
                    "store": "https://ez.example/v1",
                    "token": "sk-committed-secret",
                    "API-Key": "also-a-secret",
                }
            ),
            encoding="utf-8",
        )
        return repo

    def test_a_token_in_the_repo_config_is_ignored(self) -> None:
        repo = self._repo_with_token()

        config = load_config(self.store, repo)

        self.assertEqual("https://ez.example/v1", config.store_url)
        self.assertEqual("", config.token, "a committed token must never be used")
        self.assertIn("token", config.ignored_repo_keys)
        self.assertIn("API-Key", config.ignored_repo_keys)

    def test_an_http_store_with_only_a_committed_token_cannot_build_a_transport(
        self,
    ) -> None:
        # The end-to-end consequence: the committed credential does not
        # authenticate anything, so the publish fails loudly instead.
        repo = self._repo_with_token()
        config = load_config(self.store, repo)
        with self.assertRaises(TransportError):
            transport_for(config)

    def test_the_describe_output_never_contains_the_committed_token(self) -> None:
        repo = self._repo_with_token()
        config = load_config(self.store, repo)
        text = "\n".join(config.describe())
        self.assertNotIn("sk-committed-secret", text)
        self.assertIn("IGNORED", text)


class RootNeverTests(TempHomeTestCase):
    """C3: `never` above a session is not overridable from below."""

    def _nested(self):
        root = self.make_repo("locked", share="never")
        inner = root / "vendor" / "pkg"
        (inner / ".ez").mkdir(parents=True)
        (inner / ".ez" / "config.json").write_text(
            json.dumps({"share": "always", "store": "https://evil.example/v1"}),
            encoding="utf-8",
        )
        return root, inner

    def test_a_root_never_survives_a_nested_ez_directory(self) -> None:
        _, inner = self._nested()
        # Even acknowledged: the nearest policy is exactly what a hostile
        # commit gets to choose, so it must not be the one that governs.
        share.acknowledge(inner, self.store)

        decision = share.resolve(SESSION, inner, self.store)

        self.assertFalse(decision.sharing)
        self.assertEqual("repo", decision.source)
        self.assertIn("never", decision.reason)

    def test_share_on_refuses_under_a_root_never(self) -> None:
        _, inner = self._nested()
        with self.assertRaises(share.ShareRefused) as caught:
            share.set_session(SESSION, True, self.store, cwd=inner)
        self.assertIn("never", str(caught.exception))
        self.assertFalse(share.marker_path(self.store, SESSION).exists())

    def test_an_empty_nested_ez_directory_changes_nothing(self) -> None:
        # `mkdir vendor/pkg/.ez` is the cheapest thing a drive-by PR can do;
        # it must not so much as reroute which policy answers.
        root = self.make_repo("locked", share="never")
        bare = root / "vendor" / "bare"
        (bare / ".ez").mkdir(parents=True)

        decision = share.resolve(SESSION, bare, self.store)
        self.assertFalse(decision.sharing)
        self.assertIn("never", decision.reason)


class WatermarkTests(TransportTestCase):
    """C4: nothing recorded before the opt-in ever leaves the machine."""

    def setUp(self) -> None:
        super().setUp()
        self.transcript = self.tmp / "raw" / f"{SESSION}.jsonl"
        self.meta = SessionMeta(session=SESSION, author=self.author, project="demo")

    def publish(self, **kwargs):
        return publish(
            SESSION, self.transcript, self.transport, self.store, self.meta,
            scan_secrets=False, **kwargs
        )

    def test_opting_in_mid_session_uploads_only_bytes_after_the_opt_in(self) -> None:
        pre = write_lines(self.transcript, 40)  # the 40 minutes of customer data
        # `ezcl share on` seeds the watermark with the size at opt-in time.
        total = write_lines(self.transcript, 15, start=40)

        report = self.publish(initial_start_offset=pre)

        self.assertEqual(pre, report.start_offset)
        self.assertTrue(report.chunks)
        self.assertEqual(
            pre, report.chunks[0].offset, "the first chunk must start at the opt-in size, not 0"
        )
        self.assertEqual(total - pre, report.bytes_sent)
        self.assertEqual(self.transcript.read_bytes()[pre:], self.remote_bytes(SESSION))
        for chunk in self.transport.list_chunks(SESSION):
            self.assertGreaterEqual(chunk.offset, pre)
        self.assertEqual(pre, PublishState.load(self.store, SESSION).start_offset)

    def test_share_on_watermarks_at_the_current_transcript_size(self) -> None:
        # The CLI path: `_watermark_session` is what `ezcl share on` calls.
        stored = self.store.raw_dir / "demo" / f"{SESSION}.jsonl"
        pre = write_lines(stored, 40)

        self.assertEqual(pre, cli._watermark_session(self.store, SESSION))
        self.assertEqual(pre, PublishState.load(self.store, SESSION).start_offset)

    def test_a_compaction_reset_never_reaches_back_before_the_watermark(self) -> None:
        pre = write_lines(self.transcript, 40)
        write_lines(self.transcript, 15, start=40)
        self.publish(initial_start_offset=pre)

        # Rewritten in place to something longer with a different head: a
        # reset re-sends the document, but consent still starts at the mark.
        self.transcript.write_bytes(b"")
        new_total = write_lines(self.transcript, 120, body="rewritten")
        self.assertGreater(new_total, pre)

        report = self.publish()

        self.assertTrue(report.reset)
        self.assertEqual(pre, report.chunks[0].offset, "a reset must not resend from 0")
        self.assertEqual(new_total - pre, report.bytes_sent)
        for chunk in self.transport.list_chunks(SESSION):
            self.assertGreaterEqual(chunk.offset, pre)

    def test_a_later_initial_start_offset_cannot_move_an_existing_watermark(self) -> None:
        pre = write_lines(self.transcript, 40)
        write_lines(self.transcript, 15, start=40)
        self.publish(initial_start_offset=pre)

        # Re-running `share on` (or anything else) must not be able to lower
        # the mark once state exists: it is protecting bytes already withheld.
        report = self.publish(initial_start_offset=0)

        self.assertEqual(pre, PublishState.load(self.store, SESSION).start_offset)
        for chunk in self.transport.list_chunks(SESSION):
            self.assertGreaterEqual(chunk.offset, pre)
        self.assertTrue(report.up_to_date)


class SyncRefusalTests(TempHomeTestCase):
    """C5: an explicit `share off` is a decision `sync` may not undo."""

    def test_sync_refuses_a_session_with_an_explicit_off_marker(self) -> None:
        work = self.make_bare_dir("work")
        share.set_session(SESSION, False, self.store, cwd=work)

        refusal = cli._sync_refusal(self.store, SESSION, str(work))

        self.assertTrue(refusal, "an explicit off must block sync")
        self.assertIn("ezcl share off", refusal)

    def test_sync_may_offer_an_undecided_session(self) -> None:
        # The control: sync IS the consent step for sessions nobody decided
        # about, so "no marker, no policy" must not read as a refusal.
        work = self.make_bare_dir("work")
        self.assertEqual("", cli._sync_refusal(self.store, SESSION, str(work)))

    def test_sync_refuses_under_a_repo_never(self) -> None:
        repo = self.make_repo("locked", share="never")
        refusal = cli._sync_refusal(self.store, SESSION, str(repo))
        self.assertTrue(refusal)
        self.assertIn("never", refusal)
