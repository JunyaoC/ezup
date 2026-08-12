"""End-to-end encryption: publish -> simulated worker -> pull, fully offline.

Why the server fake is not LocalDirTransport itself: the contract (section 0)
pins LocalDirTransport as plaintext-only, and its ``put_chunk`` correctly
rejects the ``length + 16`` ciphertext bodies an encrypted session ships. The
:class:`FakeWorker` below therefore plays the Worker's part instead -- same
on-disk blob layout (``root/<chunk key>``, exactly what R2 would hold, so a
test can read the stored bytes straight off disk), a D1-shaped index in
memory, and the contract's server-side rules where they matter to the client:
sha256 of the received body, ``length + 16`` for encrypted sessions, session
scoping for readers, wrap validation, and the enc/enc_gen upsert rules.

Everything security-relevant is asserted from the *server's* side of the
boundary: the stored bytes never contain the plaintext marker, no (DK, nonce)
pair ever repeats, tampered ciphertext is refused rather than appended, and a
key that was never granted a session gets an error -- never garbage.
"""

from __future__ import annotations

import base64
import hashlib
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

from ezchangelog.config import PullView
from ezchangelog.crypto import (
    ENC_SCHEME,
    GCM_TAG,
    CryptoError,
    KeySet,
    bearer_sha256,
    chunk_aad,
    chunk_nonce,
    decrypt_chunk,
    encrypt_chunk,
    generate_key,
    new_data_key,
    parse_key,
    unwrap_dk,
    wrap_dk,
)
import ezchangelog.publish as publish_mod
from ezchangelog.publish import PublishState, publish, readers_path
from ezchangelog.pull import cursor_scope, load_pull_state, pull, pulled_path_for
from ezchangelog.store import Store
from ezchangelog.transport import ChunkRef, SessionMeta, TransportError, chunk_key
from ezchangelog.window import isoformat
from tests.support import TempHomeTestCase, sha256_file, write_lines


# -- contract vectors (E2E-CONTRACT.md sections 1-2) --------------------------
# WebCrypto generated these; reproducing them byte-exactly in Python is the
# interop proof that the CLI and the browser viewer speak one cipher.

S_HEX = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
K_AUTH_HEX = "c587d5c13882bb99c0db1bdeb631f580a6af77dd47d646a7558d3d48c23c3677"
K_ENC_HEX = "38b074ce889e57c645145ef370ba7e63478b188603a463c875cfde5f8652eef5"
BEARER = "ezw_" + K_AUTH_HEX
TOKEN_SHA256 = "01d236f19c3dfb00fa29e633cd93cc5c8f97893db5fbd0c095280156499b58d8"
KEYID = "01d236f19c3dfb00"

VEC_DK_HEX = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0"
VEC_SESSION = "sess-abc"
VEC_PLAINTEXT = b'{"type":"user","text":"hello"}\n'
VEC_NONCE_HEX = "000000010000000000001000"
VEC_AAD_HEX = "657a75702f76312f6368756e6b00736573732d61626300000000010000000000001000"
VEC_BODY_HEX = (
    "c80a96c53f560b528367d4bbe2a22c2f376bdc8b14c5abf9ddc7744b1acfb861"
    "1b649d6bd75ca706ea67a9822ef681"
)
VEC_BODY_SHA256 = "ee8a72a65a81d20b5f077efece3a672aaff85ef81d1c57485368f695b6201add"

VEC_RECIPIENT = "11111111-2222-3333-4444-555555555555"
VEC_WRAP_NONCE_HEX = "000102030405060708090a0b"
VEC_WRAP_HEX = (
    "000102030405060708090a0bc53ab5ffb0ec857074691a69eb958a4e91b3a2bb"
    "2c44c7a0a13131afd2151c07bae3d4e43e892f61c6d4b0006c779373"
)
VEC_WRAP_B64 = (
    "AAECAwQFBgcICQoLxTq1/7DshXB0aRpp65WKTpGzorssRMegoTExr9IVHAe649Tk"
    "PokvYcbUsABsd5Nz"
)


class ContractVectorTests(unittest.TestCase):
    """Pinned vectors: any drift here is a browser-interop break, not a bug fix."""

    def test_kdf_vector_reproduces_bearer_enc_key_and_keyid(self) -> None:
        keys = parse_key("ezu_" + S_HEX)
        self.assertEqual("device", keys.kind)
        self.assertEqual(BEARER, keys.bearer)
        self.assertEqual(K_ENC_HEX, keys.enc_key.hex())
        self.assertEqual(KEYID, keys.keyid)
        self.assertEqual(TOKEN_SHA256, bearer_sha256("ezu_" + S_HEX))
        self.assertEqual(TOKEN_SHA256, bearer_sha256(BEARER))
        # A reader key with the same secret derives the same bearer: the
        # prefix names the role, never the derivation.
        self.assertEqual(BEARER, parse_key("ezr_" + S_HEX).bearer)

    def test_chunk_vector_encrypts_and_decrypts_byte_exactly(self) -> None:
        dk = bytes.fromhex(VEC_DK_HEX)
        self.assertEqual(31, len(VEC_PLAINTEXT))
        self.assertEqual(VEC_NONCE_HEX, chunk_nonce(1, 4096).hex())
        self.assertEqual(VEC_AAD_HEX, chunk_aad(VEC_SESSION, 1, 4096).hex())
        body = encrypt_chunk(dk, VEC_SESSION, 1, 4096, VEC_PLAINTEXT)
        self.assertEqual(VEC_BODY_HEX, body.hex())
        self.assertEqual(47, len(body))
        self.assertEqual(VEC_BODY_SHA256, hashlib.sha256(body).hexdigest())
        self.assertEqual(
            VEC_PLAINTEXT, decrypt_chunk(dk, VEC_SESSION, 1, 4096, body)
        )

    def test_wrap_vector_wraps_and_unwraps_byte_exactly(self) -> None:
        enc_key = bytes.fromhex(K_ENC_HEX)
        dk = bytes.fromhex(VEC_DK_HEX)
        blob = wrap_dk(
            enc_key, VEC_SESSION, VEC_RECIPIENT, 1, dk,
            nonce=bytes.fromhex(VEC_WRAP_NONCE_HEX),
        )
        self.assertEqual(VEC_WRAP_HEX, blob.hex())
        self.assertEqual(VEC_WRAP_B64, base64.b64encode(blob).decode("ascii"))
        self.assertEqual(dk, unwrap_dk(enc_key, VEC_SESSION, VEC_RECIPIENT, 1, blob))


class CryptoFailsClosedTests(unittest.TestCase):
    """Every wrong-key/wrong-context path must raise, never return bytes."""

    def setUp(self) -> None:
        self.dk = bytes.fromhex(VEC_DK_HEX)
        self.body = encrypt_chunk(self.dk, VEC_SESSION, 1, 4096, VEC_PLAINTEXT)

    def test_wrong_dk_raises_instead_of_returning_garbage(self) -> None:
        with self.assertRaises(CryptoError):
            decrypt_chunk(new_data_key(), VEC_SESSION, 1, 4096, self.body)

    def test_chunk_aad_binds_session_gen_and_offset(self) -> None:
        # A validly encrypted chunk moved anywhere else must die on the tag.
        for session, gen, offset in (
            ("sess-other", 1, 4096),
            (VEC_SESSION, 2, 4096),
            (VEC_SESSION, 1, 0),
        ):
            with self.assertRaises(CryptoError):
                decrypt_chunk(self.dk, session, gen, offset, self.body)

    def test_short_body_and_flipped_bit_raise(self) -> None:
        with self.assertRaises(CryptoError):
            decrypt_chunk(self.dk, VEC_SESSION, 1, 4096, self.body[:10])
        tampered = bytearray(self.body)
        tampered[0] ^= 0x01
        with self.assertRaises(CryptoError):
            decrypt_chunk(self.dk, VEC_SESSION, 1, 4096, bytes(tampered))

    def test_wrap_fails_closed_on_wrong_key_or_swapped_context(self) -> None:
        enc_key = bytes.fromhex(K_ENC_HEX)
        blob = wrap_dk(enc_key, VEC_SESSION, VEC_RECIPIENT, 3, self.dk)
        # Wrong K_enc: an ungranted reader holding the blob learns nothing.
        with self.assertRaises(CryptoError):
            unwrap_dk(parse_key("ezr_" + "ab" * 32).enc_key,
                      VEC_SESSION, VEC_RECIPIENT, 3, blob)
        # Swapped session, swapped recipient, wrong gen: all AAD-bound.
        with self.assertRaises(CryptoError):
            unwrap_dk(enc_key, "sess-other", VEC_RECIPIENT, 3, blob)
        with self.assertRaises(CryptoError):
            unwrap_dk(enc_key, VEC_SESSION, "other-recipient", 3, blob)
        with self.assertRaises(CryptoError):
            unwrap_dk(enc_key, VEC_SESSION, VEC_RECIPIENT, 4, blob)
        with self.assertRaises(CryptoError):
            unwrap_dk(enc_key, VEC_SESSION, VEC_RECIPIENT, 3, blob[:-1])

    def test_generation_zero_can_never_encrypt(self) -> None:
        # gen 0 means "plaintext state"; a nonce built from it would let a
        # caller skip the R1 bump, so the module refuses it structurally.
        with self.assertRaises(CryptoError):
            chunk_nonce(0, 0)
        with self.assertRaises(CryptoError):
            encrypt_chunk(self.dk, VEC_SESSION, 0, 0, b"x")


# -- the simulated worker ------------------------------------------------------


class FakeWorker:
    """Server-side state: blobs on disk at ``root/<chunk key>`` (the R2 view a
    test can inspect for plaintext leakage) plus D1-shaped tables in dicts."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        # session -> row: device_id, author, enc, enc_gen, size, chunks, ...
        self.sessions: dict[str, dict[str, Any]] = {}
        # (session, recipient_id) -> {"session", "enc_gen", "wrap"}
        self.wrapped: dict[tuple[str, str], dict[str, Any]] = {}
        # bearer -> device/reader row (the server never sees a pasted key)
        self.rows: dict[str, dict[str, Any]] = {}

    def register(
        self, keys: KeySet, row_id: str, *, role: str, scoped_device_id: str = ""
    ) -> None:
        self.rows[keys.bearer] = {
            "id": row_id,
            "role": role,
            "scoped_device_id": scoped_device_id,
            "revoked": False,
        }

    def readable(self, row: Mapping[str, Any], session: str) -> bool:
        rec = self.sessions.get(session)
        if rec is None or rec.get("deleted"):
            return False
        owner = rec["device_id"]
        if row["role"] == "device":
            return owner == row["id"]
        return owner == row["scoped_device_id"]

    def blob_bytes(self, key: str) -> bytes:
        return (self.root / key).read_bytes()


class FakeClient:
    """One authenticated handle on the FakeWorker: the HttpTransport surface
    publish and pull actually use, per contract 6.1/6.2/6.4."""

    def __init__(
        self, worker: FakeWorker, pasted: str, *, author: str = "", device_id: str = ""
    ) -> None:
        self.worker = worker
        self.key_set = parse_key(pasted)
        self.author = author
        self.device_id = device_id  # readers start empty and learn it (Q2)

    @property
    def recipient_id(self) -> str:
        return self.device_id

    def _row(self) -> dict[str, Any]:
        row = self.worker.rows.get(self.key_set.bearer)
        if row is None or row["revoked"]:
            raise TransportError("HTTP 401 unauthorized", status=401)
        return row

    # -- Transport surface -------------------------------------------------

    def put_session(self, meta: SessionMeta | Mapping[str, Any]) -> None:
        row = self._row()
        payload = meta.to_dict() if isinstance(meta, SessionMeta) else dict(meta)
        session = str(payload["session"])
        rec = self.worker.sessions.get(session)
        if rec is None:
            rec = {
                "session": session, "device_id": row["id"], "chunks": [],
                "size": 0, "enc": "", "enc_gen": 0,
            }
            self.worker.sessions[session] = rec
        if rec["device_id"] != row["id"]:
            raise TransportError("HTTP 403 not the owner", status=403)
        # Contract 5.1 upsert rules: enc only "" -> aead-v1, enc_gen only up.
        if "enc" in payload:
            if rec["enc"] and str(payload["enc"]) != rec["enc"]:
                raise TransportError(
                    "cannot downgrade an encrypted session", status=400
                )
            rec["enc"] = str(payload["enc"])
        if "enc_gen" in payload:
            gen = int(payload["enc_gen"])
            if gen < int(rec["enc_gen"]):
                raise TransportError("enc_gen may not decrease", status=400)
            rec["enc_gen"] = gen
        for key, value in payload.items():
            if key not in ("chunks", "size", "enc", "enc_gen", "device_id"):
                rec[key] = value
        rec["deleted"] = False
        rec["updated_at"] = isoformat(datetime.now(timezone.utc))

    def put_chunk(
        self, session: str, offset: int, length: int, sha256: str, data: bytes
    ) -> str:
        row = self._row()
        rec = self.worker.sessions.get(session)
        if rec is None or rec["device_id"] != row["id"]:
            raise TransportError("HTTP 404 unknown session", status=404)
        # The Worker's length + 16 rule: length stays plaintext addressing,
        # the body is ct||tag for an encrypted session (contract 5.1).
        expected = length + (GCM_TAG if rec.get("enc") == ENC_SCHEME else 0)
        if len(data) != expected:
            raise TransportError(
                f"body is {len(data)} bytes, expected {expected}", status=400
            )
        if hashlib.sha256(data).hexdigest() != sha256:
            raise TransportError("sha256 mismatch", status=400)
        key = chunk_key(str(rec.get("author") or self.author), session, offset, length)
        for existing in rec["chunks"]:
            if existing["offset"] == offset:
                if existing["sha256"] == sha256:
                    return key  # idempotent replay
                raise TransportError("different bytes at this offset", status=409)
        path = self.worker.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        rec["chunks"].append(
            {"offset": offset, "length": length, "sha256": sha256, "key": key}
        )
        rec["chunks"].sort(key=lambda c: c["offset"])
        rec["size"] = max(c["offset"] + c["length"] for c in rec["chunks"])
        rec["updated_at"] = isoformat(datetime.now(timezone.utc))
        return key

    def delete_session(self, session: str) -> None:
        row = self._row()
        rec = self.worker.sessions.get(session)
        if rec is None or rec["device_id"] != row["id"]:
            return
        for chunk in rec["chunks"]:
            (self.worker.root / chunk["key"]).unlink(missing_ok=True)
        rec["chunks"] = []
        rec["size"] = 0
        rec["deleted"] = True
        rec["updated_at"] = isoformat(datetime.now(timezone.utc))
        # DELETE /v1/session cascades wraps (contract 5.1).
        for pair in [p for p in self.worker.wrapped if p[0] == session]:
            del self.worker.wrapped[pair]

    def list_sessions(self, since: Any = None) -> list[dict[str, Any]]:
        row = self._row()
        out = []
        for session, rec in self.worker.sessions.items():
            if not self.worker.readable(row, session):
                continue
            out.append(
                {k: v for k, v in rec.items() if k not in ("chunks", "device_id")}
            )
        return out

    def list_chunks(self, session: str) -> list[ChunkRef]:
        row = self._row()
        if not self.worker.readable(row, session):
            raise TransportError("HTTP 404", status=404)
        rec = self.worker.sessions[session]
        return sorted(
            (ChunkRef(**chunk) for chunk in rec["chunks"]), key=lambda c: c.offset
        )

    def get_blob(self, key: str) -> bytes:
        self._row()
        try:
            return self.worker.blob_bytes(key)
        except OSError as exc:
            raise TransportError(f"no blob {key!r}") from exc

    # -- E2E surface (contract 6.1) -----------------------------------------

    def session_enc(self, session: str) -> tuple[str, int]:
        rec = self.worker.sessions.get(session)
        if rec is None or rec.get("deleted"):
            return "", 0
        return str(rec.get("enc") or ""), int(rec.get("enc_gen") or 0)

    def put_wrapped_keys(self, wraps: list[dict[str, Any]]) -> int:
        row = self._row()
        if row["role"] != "device":
            raise TransportError("HTTP 403 device role required", status=403)
        readers = {
            r["id"]: r for r in self.worker.rows.values() if r["role"] == "reader"
        }
        for index, wrap in enumerate(wraps):
            session = str(wrap.get("session") or "")
            recipient = str(wrap.get("recipient_id") or "")
            rec = self.worker.sessions.get(session)
            if rec is None or rec["device_id"] != row["id"]:
                raise TransportError(f"wrap {index}: not your session", status=400)
            reader = readers.get(recipient)
            granted = recipient == row["id"] or (
                reader is not None
                and reader["scoped_device_id"] == row["id"]
                and not reader["revoked"]
            )
            if not granted:
                raise TransportError(
                    f"wrap {index}: unknown or revoked recipient", status=400
                )
            if int(wrap.get("enc_gen") or 0) < 1:
                raise TransportError(f"wrap {index}: enc_gen < 1", status=400)
            if len(base64.b64decode(str(wrap.get("wrap") or ""))) != 60:
                raise TransportError(f"wrap {index}: not 60 bytes", status=400)
        for wrap in wraps:
            self.worker.wrapped[(str(wrap["session"]), str(wrap["recipient_id"]))] = {
                "session": str(wrap["session"]),
                "enc_gen": int(wrap["enc_gen"]),
                "wrap": str(wrap["wrap"]),
            }
        return len(wraps)

    def get_wrapped_keys(self, session: str | None = None) -> list[dict[str, Any]]:
        row = self._row()
        # The real response carries the caller's recipient_id top-level and
        # HttpTransport caches it (contract Q2); mimic that learning step.
        if not self.device_id:
            self.device_id = row["id"]
        if session is not None and not self.worker.readable(row, session):
            raise TransportError("HTTP 404", status=404)
        return [
            dict(value)
            for (sess, recipient), value in self.worker.wrapped.items()
            if recipient == row["id"]
            and self.worker.readable(row, sess)
            and (session is None or sess == session)
        ]

    def describe(self) -> str:
        return f"fake worker {self.worker.root}"


def reader_grant(keys: KeySet, reader_id: str, name: str) -> dict[str, str]:
    """One readers.json entry, as ``token mint`` would write it (contract 6.3)."""
    return {
        "reader_id": reader_id,
        "name": name,
        "keyid": keys.keyid,
        "enc_key": keys.enc_key.hex(),
        "created_at": "2026-08-12T09:00:00+00:00",
    }


def write_readers(store: Store, entries: list[dict[str, str]]) -> None:
    readers_path(store).write_text(
        json.dumps({"version": 1, "readers": entries}), encoding="utf-8"
    )


# A distinctive byte sequence planted in every plaintext line; its absence
# from the server's stored bytes is the whole point of the exercise.
MARKER = "MARKER-plaintext-canary"
SESSION = "sess-e2e"


class EncryptedFlowCase(TempHomeTestCase):
    """Device key configured, worker registered, transcript on disk."""

    def setUp(self) -> None:
        super().setUp()
        self.worker = FakeWorker(self.tmp / "worker")
        self.device_pasted, self.device_keys = generate_key("device")
        self.worker.register(self.device_keys, "dev-a", role="device")
        self.client = FakeClient(
            self.worker, self.device_pasted, author="alice", device_id="dev-a"
        )
        self.transcript = self.tmp / "raw" / f"{SESSION}.jsonl"
        self.pm_store = Store(self.tmp / "pm-store")
        self.pm_store.ensure()
        self.meta = SessionMeta(
            session=SESSION, author="alice", project="demo", title="an e2e session"
        )

    def publish(self, **kwargs: Any):
        kwargs.setdefault("scan_secrets", False)
        return publish(
            SESSION, self.transcript, self.client, self.store, self.meta, **kwargs
        )

    def mint_reader(self, reader_id: str, *, grant: bool = True) -> FakeClient:
        pasted, keys = generate_key("reader")
        self.worker.register(keys, reader_id, role="reader", scoped_device_id="dev-a")
        if grant:
            write_readers(self.store, [reader_grant(keys, reader_id, reader_id)])
        return FakeClient(self.worker, pasted)

    @property
    def pulled(self) -> Path:
        return pulled_path_for(self.pm_store, "alice", SESSION)

    def unwrap_state_dk(self) -> tuple[bytes, int]:
        """The device's own view of the live DK, straight from its state file."""
        state = PublishState.load(self.store, SESSION)
        dk = unwrap_dk(
            self.device_keys.enc_key,
            SESSION,
            "dev-a",
            state.enc_gen,
            base64.b64decode(state.dk_wrapped),
        )
        return dk, state.enc_gen


class RoundTripAndCiphertextAtRestTests(EncryptedFlowCase):
    def test_round_trip_is_byte_identical_and_store_holds_only_ciphertext(self) -> None:
        size = write_lines(self.transcript, 400, body=MARKER)
        original = self.transcript.read_bytes()
        self.assertIn(MARKER.encode(), original, "fixture must plant the marker")

        report = self.publish(max_chunk=8192)
        self.assertGreaterEqual(len(report.chunks), 3, "need several chunks")
        self.assertEqual(size, report.bytes_sent)

        # Server side: every stored blob is exactly ct||tag -- 16 bytes over
        # the plaintext length, never containing the marker, never equal to
        # the plaintext range it addresses.
        chunks = self.client.list_chunks(SESSION)
        stored = b""
        for chunk in chunks:
            blob = self.worker.blob_bytes(chunk.key)
            self.assertEqual(chunk.length + GCM_TAG, len(blob))
            self.assertEqual(hashlib.sha256(blob).hexdigest(), chunk.sha256)
            self.assertNotEqual(
                original[chunk.offset : chunk.offset + chunk.length], blob[:-GCM_TAG]
            )
            stored += blob
        self.assertNotIn(
            MARKER.encode(), stored,
            "plaintext leaked into the server-side chunk bytes",
        )
        # The session row is aead-v1 at gen >= 1, and a self-wrap exists.
        self.assertEqual((ENC_SCHEME, 1), self.client.session_enc(SESSION))
        self.assertIn((SESSION, "dev-a"), self.worker.wrapped)

        # Pull side: decrypts back to the byte-identical transcript.
        pulled = pull(PullView(self.client), self.pm_store)
        self.assertEqual([], pulled.errors)
        self.assertEqual(size, pulled.bytes)
        self.assertEqual(sha256_file(self.transcript), sha256_file(self.pulled))

    def test_a_growing_session_round_trips_incrementally(self) -> None:
        first = write_lines(self.transcript, 200, body=MARKER)
        self.publish(max_chunk=8192)
        pull(PullView(self.client), self.pm_store)

        total = write_lines(self.transcript, 200, start=200, body=MARKER)
        self.publish(max_chunk=8192)
        second = pull(PullView(self.client), self.pm_store)

        self.assertEqual([], second.errors)
        self.assertEqual(total - first, second.bytes, "only the delta travels")
        self.assertEqual(sha256_file(self.transcript), sha256_file(self.pulled))

    def test_a_granted_reader_key_decrypts_the_session(self) -> None:
        reader = self.mint_reader("rdr-1")
        write_lines(self.transcript, 300, body=MARKER)
        self.publish(max_chunk=8192)

        report = pull(PullView(reader), self.pm_store)

        self.assertEqual([], report.errors)
        self.assertEqual("reader", reader.key_set.kind)
        self.assertEqual("rdr-1", reader.device_id, "recipient id learned from GET")
        self.assertEqual(sha256_file(self.transcript), sha256_file(self.pulled))


class NonceUniquenessTests(EncryptedFlowCase):
    def test_no_dk_nonce_pair_repeats_across_growth_and_compaction_reset(self) -> None:
        seen: dict[tuple[str, str], str] = {}
        real = publish_mod.encrypt_chunk

        def recording(dk: bytes, session: str, gen: int, offset: int,
                      plaintext: bytes) -> bytes:
            pair = (bytes(dk).hex(), chunk_nonce(gen, offset).hex())
            self.assertNotIn(
                pair, seen,
                f"(DK, nonce) reused at gen {gen} offset {offset}",
            )
            seen[pair] = hashlib.sha256(plaintext).hexdigest()
            return real(dk, session, gen, offset, plaintext)

        with mock.patch.object(publish_mod, "encrypt_chunk", recording):
            # Publish, then grow: same DK, disjoint offsets.
            write_lines(self.transcript, 150, body=MARKER)
            self.publish(max_chunk=8192)
            dk_before, gen_before = self.unwrap_state_dk()
            write_lines(self.transcript, 150, start=150, body=MARKER)
            self.publish(max_chunk=8192)
            pull(PullView(self.client), self.pm_store)

            # Compaction: the transcript is rewritten in place. The same
            # offsets will be re-sent with different plaintext, which is only
            # safe because RULE R1 rotates DK + generation first.
            self.transcript.unlink()
            write_lines(self.transcript, 220, body="rewritten-after-compaction")
            report = self.publish(max_chunk=8192)

        self.assertTrue(report.reset_reason, "the rewrite must trigger a reset")
        self.assertTrue(report.deleted_remote)
        self.assertGreater(len(seen), 0)

        dk_after, gen_after = self.unwrap_state_dk()
        self.assertEqual(1, gen_before)
        self.assertGreater(gen_after, gen_before, "R1: generation strictly grows")
        self.assertNotEqual(dk_before, dk_after, "R1: the DK rotated")
        # The server's wrap row moved to the new generation with the reset.
        self.assertEqual(
            gen_after, self.worker.wrapped[(SESSION, "dev-a")]["enc_gen"]
        )

        # The puller survives the rotation: one refetch, byte-identical result.
        after = pull(PullView(self.client), self.pm_store)
        self.assertEqual([], after.errors)
        self.assertEqual(1, after.sessions_refetched)
        self.assertEqual(sha256_file(self.transcript), sha256_file(self.pulled))


class TamperAndFailClosedTests(EncryptedFlowCase):
    def test_tampered_ciphertext_is_never_appended(self) -> None:
        write_lines(self.transcript, 400, body=MARKER)
        self.publish(max_chunk=8192)
        chunks = self.client.list_chunks(SESSION)
        self.assertGreater(len(chunks), 2)
        original = self.transcript.read_bytes()
        victim = self.worker.root / chunks[1].key

        # Stage 1: a flipped byte dies on the ciphertext checksum.
        good = victim.read_bytes()
        tampered = bytearray(good)
        tampered[7] ^= 0x40
        victim.write_bytes(bytes(tampered))

        report = pull(PullView(self.client), self.pm_store)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("checksum" in e and "NOT appended" in e for e in report.errors),
            report.errors,
        )
        self.assertEqual(original[: chunks[0].length], self.pulled.read_bytes())

        # Stage 2: a malicious store fixes the sha256 to match the tampered
        # bytes. The checksum now passes -- only the GCM tag stands, and it
        # must refuse rather than append plausible-length garbage.
        self.worker.sessions[SESSION]["chunks"][1]["sha256"] = hashlib.sha256(
            bytes(tampered)
        ).hexdigest()

        report = pull(PullView(self.client), self.pm_store)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("failed decryption" in e and "NOT appended" in e for e in report.errors),
            report.errors,
        )
        # Only the verified head is on disk; nothing corrupt after it.
        self.assertEqual(original[: chunks[0].length], self.pulled.read_bytes())

    def test_an_ungranted_reader_key_cannot_decrypt(self) -> None:
        # Registered (can authenticate, can list) but never granted: publish
        # ran before the grant existed, so no wrap targets this reader.
        reader = self.mint_reader("rdr-2", grant=False)
        write_lines(self.transcript, 100, body=MARKER)
        self.publish()
        self.assertNotIn((SESSION, "rdr-2"), self.worker.wrapped)

        report = pull(PullView(reader), self.pm_store)

        self.assertFalse(report.ok)
        self.assertTrue(
            any("cannot open this session" in e for e in report.errors), report.errors
        )
        self.assertFalse(
            self.pulled.is_file() and self.pulled.stat().st_size > 0,
            "an undecryptable session must not leave bytes on disk",
        )

    def test_a_wrap_of_the_wrong_dk_fails_closed_at_decrypt(self) -> None:
        # A malicious store hands the reader a perfectly-unwrappable wrap of
        # the WRONG data key. Unwrap succeeds; decryption must still refuse.
        write_lines(self.transcript, 100, body=MARKER)
        self.publish()
        pasted, keys = generate_key("reader")
        self.worker.register(keys, "rdr-3", role="reader", scoped_device_id="dev-a")
        gen = self.client.session_enc(SESSION)[1]
        self.worker.wrapped[(SESSION, "rdr-3")] = {
            "session": SESSION,
            "enc_gen": gen,
            "wrap": base64.b64encode(
                wrap_dk(keys.enc_key, SESSION, "rdr-3", gen, new_data_key())
            ).decode("ascii"),
        }
        reader = FakeClient(self.worker, pasted)

        report = pull(PullView(reader), self.pm_store)

        self.assertFalse(report.ok)
        self.assertTrue(
            any("failed decryption" in e for e in report.errors), report.errors
        )
        have = self.pulled.read_bytes() if self.pulled.is_file() else b""
        self.assertEqual(b"", have, "wrong-key decryption must yield no bytes")

    def test_the_pull_pin_refuses_a_plaintext_downgrade(self) -> None:
        write_lines(self.transcript, 100, body=MARKER)
        self.publish()
        clean = pull(PullView(self.client), self.pm_store)
        self.assertEqual([], clean.errors)
        digest = sha256_file(self.pulled)

        # The store (maliciously) re-reports the session as plaintext. The
        # honest worker refuses this transition; a lying one is caught here.
        self.worker.sessions[SESSION]["enc"] = ""
        self.worker.sessions[SESSION]["enc_gen"] = 0
        self.worker.sessions[SESSION]["updated_at"] = isoformat(
            datetime.now(timezone.utc)
        )

        report = pull(PullView(self.client), self.pm_store, since="1970-01-01T00:00:00Z")

        self.assertFalse(report.ok)
        self.assertTrue(
            any("plaintext" in e and "refusing" in e for e in report.errors),
            report.errors,
        )
        self.assertEqual(digest, sha256_file(self.pulled), "local copy untouched")


class KeyringUnionTests(TempHomeTestCase):
    """Two devices, two scoped reader keys, one PM store: the union view."""

    def setUp(self) -> None:
        super().setUp()
        self.worker = FakeWorker(self.tmp / "worker")
        self.pm_store = Store(self.tmp / "pm-store")
        self.pm_store.ensure()
        self.store_b = Store(self.tmp / "store-b")
        self.store_b.ensure()

        self.sessions = {"alice": "sess-alice", "bob": "sess-bob"}
        self.readers: dict[str, FakeClient] = {}
        self.reader_keys: dict[str, KeySet] = {}
        self.transcripts: dict[str, Path] = {}

        for author, dev_id, store in (
            ("alice", "dev-a", self.store),
            ("bob", "dev-b", self.store_b),
        ):
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
            self.readers[author] = FakeClient(self.worker, r_pasted)
            self.reader_keys[author] = r_keys

            session = self.sessions[author]
            transcript = self.tmp / "raw" / f"{session}.jsonl"
            write_lines(transcript, 120, body=f"{author}-secret-material")
            self.transcripts[author] = transcript
            publish(
                session, transcript, client, store,
                SessionMeta(session=session, author=author, project=author),
                scan_secrets=False,
            )

    def test_union_of_scoped_keys_with_independent_cursors(self) -> None:
        for author, reader in self.readers.items():
            report = pull(
                PullView(reader), self.pm_store,
                keyid=self.reader_keys[author].keyid,
            )
            self.assertEqual([], report.errors, f"{author}'s key must pull cleanly")

        # Union: both authors' sessions land, each byte-identical.
        for author, session in self.sessions.items():
            path = pulled_path_for(self.pm_store, author, session)
            self.assertTrue(path.is_file(), f"missing {author}'s session")
            self.assertEqual(
                sha256_file(self.transcripts[author]), sha256_file(path)
            )

        # Each key's cursor lives under its own scope: one key advancing must
        # never be able to skip a window of the other key's sessions.
        state = load_pull_state(self.pm_store)
        for author in self.readers:
            scope = cursor_scope(None, self.reader_keys[author].keyid)
            self.assertIn(scope, state["cursors"], state["cursors"])

    def test_each_key_sees_and_decrypts_only_its_own_scope(self) -> None:
        alice_reader = self.readers["alice"]
        listed = {row["session"] for row in alice_reader.list_sessions()}
        self.assertEqual({self.sessions["alice"]}, listed)

        # Bob's session is opaque to alice's key: 404 on the wrap fetch, and
        # even the raw wrap blob (were the server to leak it) is undecryptable.
        with self.assertRaises(TransportError):
            alice_reader.get_wrapped_keys(self.sessions["bob"])
        bob_wrap = self.worker.wrapped[(self.sessions["bob"], "rdr-bob")]
        with self.assertRaises(CryptoError):
            unwrap_dk(
                self.reader_keys["alice"].enc_key,
                self.sessions["bob"],
                "rdr-bob",
                int(bob_wrap["enc_gen"]),
                base64.b64decode(bob_wrap["wrap"]),
            )

    def test_a_revoked_key_fails_alone_without_blocking_the_other(self) -> None:
        self.worker.rows[self.readers["alice"].key_set.bearer]["revoked"] = True

        failed = pull(
            PullView(self.readers["alice"]), self.pm_store,
            keyid=self.reader_keys["alice"].keyid,
        )
        ok = pull(
            PullView(self.readers["bob"]), self.pm_store,
            keyid=self.reader_keys["bob"].keyid,
        )

        self.assertFalse(failed.ok)
        self.assertTrue(any("401" in e for e in failed.errors), failed.errors)
        self.assertEqual([], ok.errors, "one dead key must not block another")
        bob = pulled_path_for(self.pm_store, "bob", self.sessions["bob"])
        self.assertEqual(sha256_file(self.transcripts["bob"]), sha256_file(bob))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
