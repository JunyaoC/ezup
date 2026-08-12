"""The hook process: exit 0, say nothing, and never stutter a session.

These run the entry point as a real subprocess rather than calling ``main()``,
because the contract Claude Code cares about is the process contract -- exit
status, stdout, stderr -- and an in-process call would not catch, say, an
import-time failure.

Every case here is a hostile one: unparseable stdin, a store that is not there,
a store that cannot be written to. All of them must end the same way: status 0,
empty stderr, and no output the developer has to read.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from ezchangelog import share
from tests.support import REPO_ROOT, TempHomeTestCase

SESSION = "sess-hook"


def _is_root() -> bool:
    return getattr(os, "geteuid", lambda: 1)() == 0


class HookEntryTestCase(TempHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.work = self.make_bare_dir("work")

    def run_hook(self, payload, *args, home=None):
        env = dict(os.environ)
        env["EZCHANGELOG_HOME"] = str(self.store_root if home is None else home)
        # The subprocess must import the package under test, not an installed
        # copy that happens to be on the interpreter's path.
        env["PYTHONPATH"] = str(REPO_ROOT)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            [sys.executable, "-m", "ezchangelog.hook_entry", *args],
            input=text,
            capture_output=True,
            text=True,
            cwd=str(self.work),
            env=env,
            timeout=60,
        )

    def payload(self, event: str = "Stop", **extra):
        body = {
            "hook_event_name": event,
            "session_id": SESSION,
            "cwd": str(self.work),
            "transcript_path": str(self.tmp / "transcript.jsonl"),
            "permission_mode": "default",
        }
        body.update(extra)
        return body

    def assertSilent(self, done) -> None:
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual("", done.stdout)
        self.assertEqual("", done.stderr)


class BadInputTests(HookEntryTestCase):
    def test_malformed_json_exits_zero_and_says_nothing(self) -> None:
        self.assertSilent(self.run_hook("{ this is not json"))

    def test_empty_stdin_exits_zero(self) -> None:
        self.assertSilent(self.run_hook(""))

    def test_a_json_array_instead_of_an_object_exits_zero(self) -> None:
        self.assertSilent(self.run_hook("[1, 2, 3]"))

    def test_binary_garbage_exits_zero(self) -> None:
        self.assertSilent(self.run_hook("\x00\x01\x02 not text"))

    def test_a_missing_store_exits_zero(self) -> None:
        self.assertSilent(
            self.run_hook(self.payload("SessionStart"), home=self.tmp / "no-such-store")
        )

    def test_an_unknown_event_exits_zero(self) -> None:
        self.assertSilent(self.run_hook(self.payload("PreCompact")))

    def test_statusline_with_malformed_json_exits_zero(self) -> None:
        self.assertSilent(self.run_hook("{ nope", "statusline"))


class SharingOffTests(HookEntryTestCase):
    """Off means zero bytes and zero noise."""

    def test_prints_nothing_on_session_start(self) -> None:
        self.assertSilent(self.run_hook(self.payload("SessionStart")))

    def test_prints_nothing_on_stop(self) -> None:
        self.assertSilent(self.run_hook(self.payload("Stop")))

    def test_starts_no_publish_when_sharing_is_off(self) -> None:
        self.run_hook(self.payload("Stop"))

        self.assertFalse((self.store_root / "publish").exists())
        self.assertFalse((self.store_root / "logs").exists())

    def test_statusline_is_empty_when_sharing_is_off(self) -> None:
        self.assertSilent(self.run_hook(self.payload("Stop"), "statusline"))

    def test_an_explicit_off_marker_is_still_silent(self) -> None:
        share.set_session(SESSION, False, self.store, cwd=self.work)

        self.assertSilent(self.run_hook(self.payload("SessionStart")))


class SharingOnTests(HookEntryTestCase):
    """The control cases: proves the silence above is consent, not breakage."""

    def setUp(self) -> None:
        super().setUp()
        share.set_session(SESSION, True, self.store, cwd=self.work)

    def test_session_start_announces_sharing(self) -> None:
        done = self.run_hook(self.payload("SessionStart"))

        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual("", done.stderr)
        message = json.loads(done.stdout)["systemMessage"]
        self.assertIn("ezup is ON", message)
        self.assertIn("ezup share off", message)

    def test_statusline_shows_the_indicator(self) -> None:
        done = self.run_hook(self.payload("Stop"), "statusline")

        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("ezup", done.stdout)
        self.assertIn("sharing", done.stdout)


class BrokenStoreTests(HookEntryTestCase):
    """Sharing is on, but the store cannot be written. Still exit 0, still silent."""

    def setUp(self) -> None:
        super().setUp()
        share.set_session(SESSION, True, self.store, cwd=self.work)

    def test_an_unwritable_log_dir_does_not_break_the_turn(self) -> None:
        # A plain file where the log directory should be: mkdir fails, and the
        # publish is never even spawned.
        logs = self.store_root / "logs"
        logs.write_text("not a directory\n", encoding="utf-8")

        done = self.run_hook(self.payload("Stop"))

        self.assertSilent(done)
        self.assertTrue(logs.is_file())

    def test_a_read_only_store_does_not_break_the_turn(self) -> None:
        if _is_root():
            self.skipTest("root ignores directory permissions")
        os.chmod(self.store_root, 0o500)
        self.addCleanup(os.chmod, self.store_root, 0o700)

        self.assertSilent(self.run_hook(self.payload("Stop")))

    def test_a_read_only_store_still_announces_on_session_start(self) -> None:
        if _is_root():
            self.skipTest("root ignores directory permissions")
        os.chmod(self.store_root, 0o500)
        self.addCleanup(os.chmod, self.store_root, 0o700)

        done = self.run_hook(self.payload("SessionStart"))

        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("ezup is ON", done.stdout)
