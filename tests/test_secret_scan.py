"""The secret scanner: advisory, loud, and strictly read-only.

The scanner exists to warn a developer that a credential is about to leave the
machine. It must never edit the bytes: a redacted transcript would disagree
with what the developer actually saw, and the journal built from it would
describe a session that never happened. So every test here checks both halves
-- that the warning is raised, and that the payload came through untouched.
"""

from __future__ import annotations

from ezchangelog.publish import publish, secret_scan
from ezchangelog.transport import SessionMeta
from tests.support import TransportTestCase

SESSION = "sess-secret"

API_KEY = b"sk-AbCdEf0123456789GhIjKlMnOpQr"


class SecretScanTests(TransportTestCase):
    def test_flags_an_obvious_api_key(self) -> None:
        findings = secret_scan(b'export OPENAI_API_KEY="' + API_KEY + b'"')
        self.assertTrue(findings)
        self.assertTrue(any("api key" in f for f in findings), findings)

    def test_flags_other_well_known_credential_shapes(self) -> None:
        cases = {
            "github token": b"token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123",
            "aws access key id": b"AKIAIOSFODNN7EXAMPLE ",
            "private key block": b"-----BEGIN RSA PRIVATE KEY-----",
            "slack token": b"xoxb-1234567890-abcdefghijkl",
            "database url with credentials": b"postgres://user:hunter2@db.internal/app",
        }
        for label, payload in cases.items():
            with self.subTest(label=label):
                findings = secret_scan(payload)
                self.assertTrue(any(label in f for f in findings), findings)

    def test_does_not_mutate_the_payload(self) -> None:
        payload = b'{"content":"my key is ' + API_KEY + b'"}'
        untouched = bytes(payload)

        secret_scan(payload)

        self.assertEqual(untouched, payload)

    def test_reports_where_the_secret_is_without_reprinting_it(self) -> None:
        findings = secret_scan(b'key="' + API_KEY + b'"')
        joined = " ".join(findings)
        self.assertNotIn(API_KEY.decode(), joined, "a warning must not leak the secret")
        self.assertIn("byte", joined, "a warning must say where to look")

    def test_ordinary_prose_is_not_flagged(self) -> None:
        self.assertEqual(
            [],
            secret_scan(
                b'{"role":"user","content":"please refactor the collector so it '
                b'skips transcripts that have not changed since the last run"}\n'
            ),
        )

    def test_a_hex_digest_is_not_flagged(self) -> None:
        digest = b"a" * 0 + b"3b1f8c2d4e5a6b7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e"
        self.assertEqual(64, len(digest))
        self.assertEqual([], secret_scan(b'"sha256":"' + digest + b'"'))

    def test_findings_are_capped(self) -> None:
        payload = b" ".join(b"sk-%030d" % index for index in range(60))
        self.assertLessEqual(len(secret_scan(payload)), 25)


class PublishScanTests(TransportTestCase):
    """The scanner as the publisher uses it: warn, then send anyway."""

    def setUp(self) -> None:
        super().setUp()
        self.transcript = self.tmp / "raw" / f"{SESSION}.jsonl"
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.write_bytes(
            b'{"type":"user","message":"run it"}\n'
            b'{"type":"assistant","message":"exporting OPENAI_API_KEY=' + API_KEY + b'"}\n'
        )
        self.meta = SessionMeta(session=SESSION, author=self.author, project="demo")

    def publish(self, **kwargs):
        return publish(
            SESSION, self.transcript, self.transport, self.store, self.meta, **kwargs
        )

    def test_a_secret_warns_but_the_bytes_go_up_verbatim(self) -> None:
        report = self.publish()

        self.assertTrue(report.warnings, "an obvious key must be reported")
        self.assertTrue(any("api key" in w for w in report.warnings), report.warnings)
        self.assertEqual(
            self.transcript.read_bytes(),
            self.remote_bytes(SESSION),
            "the scanner must not redact, truncate or reorder anything",
        )

    def test_a_dry_run_warns_without_sending(self) -> None:
        report = self.publish(dry_run=True)

        self.assertTrue(report.warnings)
        self.assertEqual([], self.transport.list_chunks(SESSION))

    def test_scanning_can_be_turned_off(self) -> None:
        report = self.publish(scan_secrets=False)

        self.assertEqual([], report.warnings)
        self.assertEqual(self.transcript.read_bytes(), self.remote_bytes(SESSION))

    def test_warnings_are_deduplicated_across_chunks(self) -> None:
        # Parenthesised: without them the * 200 binds to the closing brace
        # alone and the payload fits inside a single 4 KiB chunk.
        self.transcript.write_bytes(
            (b'{"key":"' + API_KEY + b'"}\n') * 200
        )

        report = self.publish(max_chunk=4096)

        self.assertGreater(len(report.chunks), 1)
        self.assertEqual(len(set(report.warnings)), len(report.warnings))
