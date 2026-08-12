"""Targeted E2E regressions for the F1 downgrade pin, legacy handling, the
mint+grant backfill, and the CLI keyring.

These exercise seams the broad round-trip suite in ``test_e2e_encryption`` does
not hit head-on:

* the F1 *first-pull* downgrade -- a held wrap proves encryption before any
  session has ever been recorded ``aead-v1`` locally, which is the
  trust-on-first-use hole the record-based pin cannot close;
* a genuinely wrap-less legacy session -- skipped by default on the E2E path,
  and pulled only under ``allow_legacy`` where it is badged "unverified";
* the real ``cli._grant_history`` backfill -- a reader minted AFTER publishing
  recovers every DK from the device's own self-wraps and re-wraps them;
* the ``Keyring`` add/list/remove surface plus a two-key pull that attributes
  each author's bytes to the right key's scope.

Everything runs offline against the same in-memory ``FakeWorker`` the sibling
suite pins to the contract, so no assertion here trusts a store flag it could
not itself verify with key material.
"""

from __future__ import annotations

import base64
import hashlib
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ezchangelog import cli
from ezchangelog.config import PullView
from ezchangelog.crypto import (
    ENC_SCHEME,
    CryptoError,
    generate_key,
    parse_key,
    unwrap_dk,
)
from ezchangelog.keyring import DuplicateKeyError, Keyring, keyring_path
from ezchangelog.publish import PublishState, publish
from ezchangelog.pull import (
    cursor_scope,
    load_pull_state,
    pull,
    pulled_path_for,
    pulled_sessions,
)
from ezchangelog.store import Store
from ezchangelog.transport import SessionMeta, TransportError, chunk_key
from ezchangelog.window import isoformat

# Reuse the contract-pinned server/client fakes rather than re-deriving them:
# one definition of "what the Worker does" keeps these tests honest against the
# same rules the round-trip suite asserts.
from tests.test_e2e_encryption import (
    MARKER,
    SESSION,
    EncryptedFlowCase,
    FakeClient,
    FakeWorker,
    reader_grant,
    write_readers,
)
from tests.support import TempHomeTestCase, sha256_file, write_lines


def _plant_plaintext_session(
    worker: FakeWorker,
    session: str,
    author: str,
    device_id: str,
    body: bytes,
    *,
    enc: str = "",
    enc_gen: int = 0,
) -> None:
    """Write a legacy plaintext session straight into the server fake.

    ``publish`` with a device key always encrypts, so a genuinely legacy
    (never-encrypted, no-wrap) session cannot be produced through it -- it is
    planted here the way a pre-cutover row would already exist in the store.
    Also used to forge the plaintext *presentation* of a session a wrap exists
    for (the downgrade), by passing bytes plus enc="".
    """
    key = chunk_key(author, session, 0, len(body))
    path = worker.root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    worker.sessions[session] = {
        "session": session,
        "device_id": device_id,
        "author": author,
        "project": "legacy",
        "title": "legacy session",
        "enc": enc,
        "enc_gen": enc_gen,
        "size": len(body),
        "chunks": [
            {
                "offset": 0,
                "length": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "key": key,
            }
        ],
        "deleted": False,
        "updated_at": isoformat(datetime.now(timezone.utc)),
    }


class F1FirstPullDowngradeTests(EncryptedFlowCase):
    """F1: a wrapped key is proof of encryption on the FIRST pull, before any
    session has ever been recorded aead-v1 locally."""

    def test_held_wrap_but_plaintext_presentation_is_refused_on_first_pull(self) -> None:
        # Publish encrypted so a self-wrap for this device exists on the store.
        write_lines(self.transcript, 200, body=MARKER)
        self.publish(max_chunk=8192)
        self.assertEqual((ENC_SCHEME, 1), self.client.session_enc(SESSION))
        self.assertIn((SESSION, "dev-a"), self.worker.wrapped)

        # No pull has happened yet: there is no pinned pull-state record, so the
        # record-based downgrade pin cannot fire. The only thing that can catch
        # this is the held wrap. A malicious store now presents the session as
        # plaintext (enc null, plaintext chunk bytes) while leaving the wrap in
        # place -- the exact trust-on-first-use downgrade F1 exists to close.
        plaintext = self.transcript.read_bytes()
        _plant_plaintext_session(self.worker, SESSION, "alice", "dev-a", plaintext)
        self.assertEqual(("", 0), self.client.session_enc(SESSION))

        report = pull(PullView(self.client), self.pm_store)

        self.assertFalse(report.ok)
        self.assertTrue(
            any("downgrade" in e and "refusing" in e for e in report.errors),
            report.errors,
        )
        # Nothing was appended: the forged plaintext never reached disk.
        self.assertFalse(
            self.pulled.is_file() and self.pulled.stat().st_size > 0,
            "a downgraded session must not leave bytes on disk",
        )
        # And the state records nothing readable for the session either.
        state = load_pull_state(self.pm_store)
        self.assertEqual({}, state.get("sessions"))


class LegacyPlaintextSkipTests(TempHomeTestCase):
    """F1: a genuinely legacy session (no wrap anywhere) is default-skipped on
    the E2E path and only pulled -- as unverified -- under allow_legacy."""

    LEGACY = "sess-legacy"

    def setUp(self) -> None:
        super().setUp()
        self.worker = FakeWorker(self.tmp / "worker")
        self.device_pasted, self.device_keys = generate_key("device")
        self.worker.register(self.device_keys, "dev-a", role="device")
        self.client = FakeClient(
            self.worker, self.device_pasted, author="alice", device_id="dev-a"
        )
        self.pm_store = Store(self.tmp / "pm-store")
        self.pm_store.ensure()

        # A pre-cutover plaintext session owned by this device, with no wrap for
        # anyone. The device key is on the E2E path but holds no wrap for it.
        self.body = b'{"type":"user","text":"legacy transcript line"}\n' * 20
        _plant_plaintext_session(
            self.worker, self.LEGACY, "alice", "dev-a", self.body
        )
        self.assertNotIn((self.LEGACY, "dev-a"), self.worker.wrapped)

    @property
    def pulled(self) -> Path:
        return pulled_path_for(self.pm_store, "alice", self.LEGACY)

    def test_skipped_by_default_then_pulled_unverified_with_allow_legacy(self) -> None:
        # Default: no wrap for this key -> not readable on the E2E path -> a
        # deliberate skip, not an error, and no bytes.
        default = pull(PullView(self.client), self.pm_store)
        self.assertTrue(default.ok, default.errors)
        self.assertEqual([], default.errors)
        self.assertTrue(
            any(self.LEGACY in s for s in default.skipped), default.skipped
        )
        self.assertEqual([], default.unverified)
        self.assertFalse(
            self.pulled.is_file() and self.pulled.stat().st_size > 0,
            "a skipped legacy session must not be pulled",
        )

        # Opt in: now it is pulled, byte-identical, and flagged unverified so
        # anything downstream can badge it "unverified plaintext".
        opted = pull(
            PullView(self.client),
            self.pm_store,
            since="1970-01-01T00:00:00Z",
            allow_legacy=True,
        )
        self.assertEqual([], opted.errors)
        self.assertTrue(
            any(self.LEGACY in u for u in opted.unverified), opted.unverified
        )
        self.assertTrue(self.pulled.is_file())
        self.assertEqual(self.body, self.pulled.read_bytes())

        # The persisted record carries the unverified flag (enc stays "" so it
        # can never later masquerade as a pinned encrypted session).
        state = load_pull_state(self.pm_store)
        record = state["sessions"][self.LEGACY]
        self.assertTrue(record["unverified"])
        self.assertEqual("", record["enc"])
        rows = {r["session_id"]: r for r in pulled_sessions(self.pm_store)}
        self.assertTrue(rows[self.LEGACY]["unverified"])


class MintGrantBackfillTests(TempHomeTestCase):
    """Contract D8 / 6.3: a reader minted AFTER publishing recovers every DK
    from the device's own self-wraps and re-wraps it -- a granted reader can
    then decrypt every session; an ungranted one cannot."""

    SESS_A = "sess-a"
    SESS_B = "sess-b"

    def setUp(self) -> None:
        super().setUp()
        self.worker = FakeWorker(self.tmp / "worker")
        self.device_pasted, self.device_keys = generate_key("device")
        self.worker.register(self.device_keys, "dev-a", role="device")
        self.client = FakeClient(
            self.worker, self.device_pasted, author="alice", device_id="dev-a"
        )
        self.transcripts: dict[str, Path] = {}
        for sess in (self.SESS_A, self.SESS_B):
            transcript = self.tmp / "raw" / f"{sess}.jsonl"
            write_lines(transcript, 150, body=f"{sess}-{MARKER}")
            self.transcripts[sess] = transcript
            publish(
                sess,
                transcript,
                self.client,
                self.store,
                SessionMeta(session=sess, author="alice", project="demo"),
                scan_secrets=False,
                max_chunk=8192,
            )
        # Both sessions have a self-wrap for the device and nothing else yet.
        self.assertIn((self.SESS_A, "dev-a"), self.worker.wrapped)
        self.assertIn((self.SESS_B, "dev-a"), self.worker.wrapped)

    def _pull_store(self) -> Store:
        store = Store(self.tmp / f"pm-{id(object())}")
        store.ensure()
        return store

    def test_backfill_grants_all_sessions_to_a_new_reader(self) -> None:
        # Mint the reader AFTER both publishes: it was registered too late for
        # any publish to have wrapped for it, so only the backfill can grant it.
        reader_pasted, reader_keys = generate_key("reader")
        self.worker.register(
            reader_keys, "rdr-1", role="reader", scoped_device_id="dev-a"
        )
        self.assertNotIn((self.SESS_A, "rdr-1"), self.worker.wrapped)

        granted, skipped = cli._grant_history(
            self.client, "rdr-1", reader_keys.enc_key, say=lambda *a, **k: None
        )
        self.assertEqual(2, granted)
        self.assertEqual([], skipped)
        # Both sessions are now wrapped for the reader, at the same generation.
        for sess in (self.SESS_A, self.SESS_B):
            self.assertIn((sess, "rdr-1"), self.worker.wrapped)
            self.assertEqual(
                self.worker.wrapped[(sess, "dev-a")]["enc_gen"],
                self.worker.wrapped[(sess, "rdr-1")]["enc_gen"],
            )

        # The granted reader decrypts BOTH sessions to byte-identical plaintext.
        reader = FakeClient(self.worker, reader_pasted)
        pm_store = self._pull_store()
        report = pull(PullView(reader), pm_store)
        self.assertEqual([], report.errors)
        self.assertEqual("rdr-1", reader.device_id, "recipient id learned from GET")
        for sess in (self.SESS_A, self.SESS_B):
            path = pulled_path_for(pm_store, "alice", sess)
            self.assertTrue(path.is_file(), f"{sess} not decrypted")
            self.assertEqual(sha256_file(self.transcripts[sess]), sha256_file(path))

    def test_a_different_ungranted_reader_cannot_decrypt(self) -> None:
        # Grant rdr-1 only, then try to read with rdr-2 (registered, scoped to
        # the same device so it can list, but never backfilled).
        _, r1_keys = generate_key("reader")
        self.worker.register(
            r1_keys, "rdr-1", role="reader", scoped_device_id="dev-a"
        )
        cli._grant_history(
            self.client, "rdr-1", r1_keys.enc_key, say=lambda *a, **k: None
        )

        r2_pasted, r2_keys = generate_key("reader")
        self.worker.register(
            r2_keys, "rdr-2", role="reader", scoped_device_id="dev-a"
        )
        self.assertNotIn((self.SESS_A, "rdr-2"), self.worker.wrapped)

        reader = FakeClient(self.worker, r2_pasted)
        pm_store = self._pull_store()
        report = pull(PullView(reader), pm_store)

        self.assertFalse(report.ok)
        self.assertTrue(
            any("cannot open this session" in e for e in report.errors),
            report.errors,
        )
        for sess in (self.SESS_A, self.SESS_B):
            path = pulled_path_for(pm_store, "alice", sess)
            self.assertFalse(
                path.is_file() and path.stat().st_size > 0,
                f"{sess} must leave no bytes for an ungranted key",
            )


class KeyringCrudAndUnionTests(TempHomeTestCase):
    """The Keyring add/list/remove surface, plus a two-key pull that keeps each
    author's bytes and cursor under the unlocking key's own scope."""

    def setUp(self) -> None:
        super().setUp()
        self.worker = FakeWorker(self.tmp / "worker")
        self.pm_store = Store(self.tmp / "pm-store")
        self.pm_store.ensure()

        self.sessions = {"alice": "sess-alice", "bob": "sess-bob"}
        self.reader_tokens: dict[str, str] = {}
        self.reader_keys: dict[str, Any] = {}
        self.transcripts: dict[str, Path] = {}

        # Two independent developers, each with a device and a scoped reader.
        # Each device publishes one encrypted session and grants its reader up
        # front (readers.json before publish), so the reader holds the wrap.
        for author, dev_id in (("alice", "dev-a"), ("bob", "dev-b")):
            store = Store(self.tmp / f"store-{author}")
            store.ensure()
            dev_pasted, dev_keys = generate_key("device")
            self.worker.register(dev_keys, dev_id, role="device")
            client = FakeClient(
                self.worker, dev_pasted, author=author, device_id=dev_id
            )
            r_pasted, r_keys = generate_key("reader")
            rid = f"rdr-{author}"
            self.worker.register(
                r_keys, rid, role="reader", scoped_device_id=dev_id
            )
            write_readers(store, [reader_grant(r_keys, rid, author)])
            self.reader_tokens[author] = r_pasted
            self.reader_keys[author] = r_keys

            session = self.sessions[author]
            transcript = self.tmp / "raw" / f"{session}.jsonl"
            write_lines(transcript, 120, body=f"{author}-secret-material")
            self.transcripts[author] = transcript
            publish(
                session,
                transcript,
                client,
                store,
                SessionMeta(session=session, author=author, project=author),
                scan_secrets=False,
            )

    def test_add_list_remove_never_expose_the_token(self) -> None:
        ring = Keyring.load(self.pm_store)
        self.assertEqual([], ring.list())

        alice = ring.add(
            self.reader_tokens["alice"], label="alice", store="https://store"
        )
        bob = ring.add(self.reader_tokens["bob"], label="bob", store="https://store")
        self.assertEqual(2, len(ring.list()))

        # keyid is derived from the token, never trusted from input.
        self.assertEqual(self.reader_keys["alice"].keyid, alice.keyid)
        # A human-facing view carries the fingerprint but never the secret.
        for entry in ring.list():
            self.assertNotIn("token", entry.redacted())
            self.assertTrue(entry.keyid)

        # Lookup by keyid and by label both resolve.
        self.assertIs(alice, ring.get(alice.keyid))
        self.assertIs(bob, ring.get("bob"))

        # A device key and a duplicate are both refused, never silently kept.
        device_pasted, _ = generate_key("device")
        with self.assertRaises(CryptoError):
            ring.add(device_pasted)
        with self.assertRaises(DuplicateKeyError):
            ring.add(self.reader_tokens["alice"])

        # Persisted file is 0600 and round-trips without leaking a fingerprint
        # into a claim it cannot back up.
        ring.save()
        self.assertEqual("0o600", oct(keyring_path(self.pm_store).stat().st_mode & 0o777))
        reloaded = Keyring.load(self.pm_store)
        self.assertEqual(
            {self.reader_keys["alice"].keyid, self.reader_keys["bob"].keyid},
            {e.keyid for e in reloaded.list()},
        )

        # Remove is by keyid or label; a miss returns False and changes nothing.
        self.assertTrue(reloaded.remove("alice"))
        self.assertEqual(1, len(reloaded.list()))
        self.assertEqual("bob", reloaded.list()[0].label)
        self.assertFalse(reloaded.remove("nobody"))
        self.assertEqual(1, len(reloaded.list()))

    def test_two_key_pull_reassembles_each_scope_and_attributes_authors(self) -> None:
        ring = Keyring.load(self.pm_store)
        for author in ("alice", "bob"):
            ring.add(self.reader_tokens[author], label=author, store="https://store")

        # The contract's pull loop: one HttpTransport per keyring entry, each
        # pull scoped to that key's keyid so no key's cursor can skip another's
        # window (contract 6.5 / cursor_scope).
        for entry in ring.list():
            client = FakeClient(self.worker, entry.token)
            report = pull(PullView(client), self.pm_store, keyid=entry.keyid)
            self.assertEqual([], report.errors, f"{entry.label} pulled with errors")

        # Union: both authors' sessions land, each byte-identical and attributed
        # to the right author directory.
        for author, session in self.sessions.items():
            path = pulled_path_for(self.pm_store, author, session)
            self.assertTrue(path.is_file(), f"missing {author}/{session}")
            self.assertEqual(sha256_file(self.transcripts[author]), sha256_file(path))

        # pulled_sessions attributes each session to its own author, not a mix.
        attributed = {r["session_id"]: r["author"] for r in pulled_sessions(self.pm_store)}
        self.assertEqual(
            {self.sessions["alice"]: "alice", self.sessions["bob"]: "bob"},
            attributed,
        )

        # Each key's cursor lives under its own keyid scope.
        state = load_pull_state(self.pm_store)
        for author in ("alice", "bob"):
            scope = cursor_scope(None, self.reader_keys[author].keyid)
            self.assertIn(scope, state["cursors"], state["cursors"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
