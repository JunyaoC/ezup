"""settings.json surgery: touch our entries, and nothing else, ever.

``~/.claude/settings.json`` is the user's file. The tests below hold install
and uninstall to the strictest workable standard -- after an install followed
by an uninstall the file must be byte-for-byte what it was -- because anything
weaker allows a tool that quietly reformats or reorders someone's
configuration every time it runs.

``hooks.DEFAULT_SETTINGS`` is repointed for the whole test case: every call
here passes an explicit path, and the patch is there so that a mistake in a
test can still never reach the developer's real settings file.
"""

from __future__ import annotations

import json
from pathlib import Path

from ezchangelog import hooks, share
from tests.support import TempHomeTestCase

FOREIGN_STOP_HOOK = {
    "hooks": [{"type": "command", "command": "some-other-tool --on-stop"}]
}

ORIGINAL = {
    "permissions": {"allow": ["Bash(ls:*)"], "deny": ["Bash(rm:*)"]},
    "env": {"FOO": "bar"},
    "outputStyle": "Explanatory",
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "other-tool guard"}],
            }
        ]
    },
}


def dumps(payload: dict) -> str:
    """Exactly the formatting ``store._write_json_atomic`` produces."""
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


class SettingsTestCase(TempHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        unreachable = self.tmp / "must-never-be-written.json"
        real_default = hooks.DEFAULT_SETTINGS
        hooks.DEFAULT_SETTINGS = unreachable
        self.addCleanup(setattr, hooks, "DEFAULT_SETTINGS", real_default)
        self.unreachable = unreachable

        self.settings = self.tmp / "claude" / "settings.json"
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.write(ORIGINAL)

    def tearDown(self) -> None:
        self.assertFalse(
            self.unreachable.exists(), "a test wrote to the default settings path"
        )
        super().tearDown()

    def write(self, payload: dict) -> None:
        self.settings.write_text(dumps(payload), encoding="utf-8")
        self.original_bytes = self.settings.read_bytes()

    def load(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def commands_for(self, event: str) -> list[str]:
        groups = self.load().get("hooks", {}).get(event, [])
        return [
            command.get("command", "")
            for group in groups
            for command in group.get("hooks", [])
        ]

    def backups(self) -> list[Path]:
        return sorted(self.settings.parent.glob(f"{self.settings.name}.bak-*"))


class InstallTests(SettingsTestCase):
    def test_install_wires_up_every_event(self) -> None:
        info = hooks.install(self.settings)

        self.assertTrue(info["changed"])
        self.assertEqual(sorted(hooks.HOOK_EVENTS), sorted(info["added_events"]))
        for event in hooks.HOOK_EVENTS:
            with self.subTest(event=event):
                self.assertTrue(
                    any(hooks.MARKER in command for command in self.commands_for(event))
                )

    def test_install_preserves_unrelated_keys(self) -> None:
        hooks.install(self.settings)
        data = self.load()

        self.assertEqual(ORIGINAL["permissions"], data["permissions"])
        self.assertEqual(ORIGINAL["env"], data["env"])
        self.assertEqual(ORIGINAL["outputStyle"], data["outputStyle"])
        self.assertEqual(ORIGINAL["hooks"]["PreToolUse"], data["hooks"]["PreToolUse"])

    def test_install_is_idempotent(self) -> None:
        hooks.install(self.settings)
        after_first = self.settings.read_bytes()

        info = hooks.install(self.settings)

        self.assertFalse(info["changed"])
        self.assertEqual([], info["added_events"])
        self.assertEqual(after_first, self.settings.read_bytes())
        for event in hooks.HOOK_EVENTS:
            with self.subTest(event=event):
                ours = [c for c in self.commands_for(event) if hooks.MARKER in c]
                self.assertEqual(1, len(ours), "duplicate hook entry")

    def test_install_keeps_a_foreign_hook_on_one_of_our_events(self) -> None:
        payload = json.loads(json.dumps(ORIGINAL))
        payload["hooks"]["Stop"] = [FOREIGN_STOP_HOOK]
        self.write(payload)

        hooks.install(self.settings)

        self.assertIn("some-other-tool --on-stop", self.commands_for("Stop"))
        self.assertTrue(any(hooks.MARKER in c for c in self.commands_for("Stop")))

    def test_install_keeps_a_foreign_status_line(self) -> None:
        payload = json.loads(json.dumps(ORIGINAL))
        payload["statusLine"] = {"type": "command", "command": "my-own-prompt"}
        self.write(payload)

        info = hooks.install(self.settings)

        self.assertEqual("kept-existing", info["statusline"])
        self.assertEqual(
            {"type": "command", "command": "my-own-prompt"}, self.load()["statusLine"]
        )

    def test_install_adds_a_status_line_when_there_is_none(self) -> None:
        info = hooks.install(self.settings)

        self.assertEqual("added", info["statusline"])
        self.assertIn("command", self.load()["statusLine"])

    def test_install_creates_a_missing_settings_file(self) -> None:
        fresh = self.tmp / "fresh" / "settings.json"

        info = hooks.install(fresh)

        self.assertTrue(info["changed"])
        self.assertIsNone(info["backup"], "there was nothing to back up")
        self.assertTrue(fresh.is_file())
        self.assertEqual(sorted(hooks.HOOK_EVENTS), sorted(json.loads(fresh.read_text())["hooks"]))

    def test_install_backs_the_original_up_once(self) -> None:
        hooks.install(self.settings)

        self.assertEqual(1, len(self.backups()))
        self.assertEqual(self.original_bytes, self.backups()[0].read_bytes())

        hooks.uninstall(self.settings)
        self.assertEqual(1, len(self.backups()), "the first backup is the original")

    def test_unreadable_settings_are_refused_rather_than_overwritten(self) -> None:
        self.settings.write_text("{ this is not json", encoding="utf-8")

        with self.assertRaises(RuntimeError):
            hooks.install(self.settings)

        self.assertEqual("{ this is not json", self.settings.read_text(encoding="utf-8"))

    def test_installing_does_not_start_sharing(self) -> None:
        hooks.install(self.settings)

        decision = share.resolve("sess-1", self.make_bare_dir(), self.store)
        self.assertFalse(decision.sharing)
        self.assertEqual("default", decision.source)
        self.assertFalse((self.store.root / "sessions").exists())
        self.assertFalse((self.store.root / "publish").exists())


class UninstallTests(SettingsTestCase):
    def test_uninstall_restores_the_file_byte_for_byte(self) -> None:
        hooks.install(self.settings)
        self.assertNotEqual(self.original_bytes, self.settings.read_bytes())

        info = hooks.uninstall(self.settings)

        self.assertTrue(info["changed"])
        self.assertEqual(self.original_bytes, self.settings.read_bytes())

    def test_uninstall_restores_a_file_that_had_a_foreign_status_line(self) -> None:
        payload = json.loads(json.dumps(ORIGINAL))
        payload["statusLine"] = {"type": "command", "command": "my-own-prompt"}
        self.write(payload)
        hooks.install(self.settings)

        hooks.uninstall(self.settings)

        self.assertEqual(self.original_bytes, self.settings.read_bytes())

    def test_uninstall_restores_a_file_that_had_a_foreign_stop_hook(self) -> None:
        payload = json.loads(json.dumps(ORIGINAL))
        payload["hooks"]["Stop"] = [FOREIGN_STOP_HOOK]
        self.write(payload)
        hooks.install(self.settings)

        hooks.uninstall(self.settings)

        self.assertEqual(self.original_bytes, self.settings.read_bytes())
        self.assertEqual(["some-other-tool --on-stop"], self.commands_for("Stop"))

    def test_uninstall_is_idempotent(self) -> None:
        hooks.install(self.settings)
        hooks.uninstall(self.settings)
        restored = self.settings.read_bytes()

        info = hooks.uninstall(self.settings)

        self.assertFalse(info["changed"])
        self.assertEqual([], info["removed_events"])
        self.assertEqual(restored, self.settings.read_bytes())

    def test_uninstall_on_a_never_installed_file_changes_nothing(self) -> None:
        info = hooks.uninstall(self.settings)

        self.assertFalse(info["changed"])
        self.assertEqual(self.original_bytes, self.settings.read_bytes())

    def test_uninstall_leaves_an_empty_settings_object(self) -> None:
        fresh = self.tmp / "fresh" / "settings.json"
        hooks.install(fresh)

        hooks.uninstall(fresh)

        self.assertEqual({}, json.loads(fresh.read_text(encoding="utf-8")))


class StatusTests(SettingsTestCase):
    def test_status_before_and_after_install(self) -> None:
        before = hooks.status(self.settings)
        self.assertFalse(before["installed"])
        self.assertFalse(any(before["events"].values()))
        self.assertFalse(before["statusline_installed"])

        hooks.install(self.settings)

        after = hooks.status(self.settings)
        self.assertTrue(after["installed"])
        self.assertTrue(all(after["events"].values()))
        self.assertTrue(after["statusline_installed"])
        self.assertEqual(str(self.settings), after["settings_path"])

    def test_status_survives_unreadable_settings(self) -> None:
        self.settings.write_text("{ not json", encoding="utf-8")

        info = hooks.status(self.settings)

        self.assertFalse(info["settings_readable"])
        self.assertFalse(info["installed"])

    def test_status_reports_a_missing_file(self) -> None:
        info = hooks.status(self.tmp / "nowhere" / "settings.json")

        self.assertFalse(info["settings_exists"])
        self.assertFalse(info["installed"])
