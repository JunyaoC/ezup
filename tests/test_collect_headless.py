"""`collect --yes`: the runner's non-interactive path into the pipeline.

REMOTE-RUNNER-DESIGN.md section 3 requires a chooser-less collect that a
scheduled container can run with no TTY: every pulled+matched session in the
window is taken without a picker and handed to the pipeline. Section 3 also
requires that an empty window is a loud failure, so a broken pull can never
masquerade as a quiet week with an empty journal.

These tests drive the real argparse surface (`build_parser`) and the real
`cmd_collect`, stubbing only two things:

  * the local `~/.claude/projects` scan, so the test sees the fixture's pulled
    sessions and nothing off the developer's own machine;
  * `run_pipeline`, so the test can assert what reached the pipeline without a
    model call (the task's "stop before the model call").
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from ezchangelog import cli
from ezchangelog.pull import pull_state_path, pulled_path_for

from tests.support import TempHomeTestCase


def _pulled_transcript(path: Path, cwd: str) -> None:
    """Write a minimal but realistic transcript that scans to real facts.

    It needs a cwd (so it matches a directory), several user turns, and several
    tool uses, or the default collect filters (min_turns=1, min_tools=1) would
    drop it and the test would prove nothing.
    """
    records: list[dict[str, Any]] = []
    for index in range(3):
        records.append(
            {
                "type": "user",
                "uuid": f"u{index}",
                "timestamp": "2026-08-11T08:00:00Z",
                "cwd": cwd,
                "message": {"role": "user", "content": "hello world " * 8},
            }
        )
        records.append(
            {
                "type": "assistant",
                "uuid": f"a{index}",
                "timestamp": "2026-08-11T08:00:01Z",
                "cwd": cwd,
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": f"t{index}", "name": "Bash", "input": {}}
                    ],
                },
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


class CollectHeadlessTests(TempHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Two pulled sessions from a teammate, both inside the window.
        self.author = "alice"
        self.sessions = ["sess-aaaa", "sess-bbbb"]
        work_dir = self.make_bare_dir("teamwork")
        state_sessions: dict[str, Any] = {}
        for session in self.sessions:
            path = pulled_path_for(self.store, self.author, session)
            _pulled_transcript(path, str(work_dir))
            state_sessions[session] = {
                "author": self.author,
                "path": str(path),
                "cwd": str(work_dir),
                "project": "teamwork",
                "first_ts": "2026-08-11T08:00:00Z",
                "last_ts": "2026-08-11T08:00:01Z",
            }
        pull_state_path(self.store).write_text(
            json.dumps({"version": 1, "sessions": state_sessions}),
            encoding="utf-8",
        )

    def _run(self, argv: list[str], captured: list[list[Any]]) -> tuple[int, str]:
        """Invoke `cmd_collect` with the local scan silenced and the pipeline
        stubbed to record the selections it was given."""

        def fake_pipeline(
            selections: list, store: Any, window: Any, roots: Any, console: Any, **kw: Any
        ) -> Path:
            captured.append(list(selections))
            journal = store.root / "journals" / "fake" / "journal.html"
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text("<html></html>", encoding="utf-8")
            return journal

        args = cli.build_parser().parse_args(argv)
        buffer = io.StringIO()
        with mock.patch(
            "ezchangelog.collect.discover_transcripts", return_value=[]
        ), mock.patch.object(cli, "run_pipeline", fake_pipeline), redirect_stdout(
            buffer
        ):
            code = cli.cmd_collect(args)
        return code, buffer.getvalue()

    def test_yes_selects_all_pulled_and_reaches_the_pipeline(self) -> None:
        captured: list[list[Any]] = []
        code, out = self._run(
            [
                "--store",
                str(self.store_root),
                "collect",
                "--yes",
                "--include-pulled",
                "--json",
            ],
            captured,
        )

        self.assertEqual(code, 0)
        # The pipeline was reached exactly once...
        self.assertEqual(len(captured), 1, "run_pipeline should be called once")
        selected = captured[0]
        # ...with every pulled session, and no picker in between.
        self.assertEqual(
            {s.facts.session_id for s in selected}, set(self.sessions)
        )
        self.assertTrue(all(s.pulled for s in selected))
        # The journal path is announced on stdout for the runner to read.
        self.assertIn("journal.html", out)

    def test_yes_ignores_the_interactive_flag(self) -> None:
        # --yes wins over -i: even with both, the picker must never run (there is
        # no TTY in a scheduled container), so the pipeline still sees everything.
        captured: list[list[Any]] = []
        with mock.patch.object(
            cli, "interactive_chooser", side_effect=AssertionError("picker ran")
        ):
            code, _ = self._run(
                [
                    "--store",
                    str(self.store_root),
                    "collect",
                    "--yes",
                    "-i",
                    "--include-pulled",
                    "--json",
                ],
                captured,
            )
        self.assertEqual(code, 0)
        self.assertEqual(len(captured), 1)
        self.assertEqual(len(captured[0]), len(self.sessions))

    def test_yes_refuses_an_empty_window_instead_of_journaling(self) -> None:
        # Without --include-pulled and with the local scan empty, nothing matches.
        # A headless run must fail loudly (exit 4) and never reach the pipeline,
        # so a broken pull cannot silently emit an empty journal.
        captured: list[list[Any]] = []
        code, out = self._run(
            [
                "--store",
                str(self.store_root),
                "collect",
                "--yes",
                "--json",
            ],
            captured,
        )
        self.assertEqual(code, 4)
        self.assertEqual(captured, [], "the pipeline must not run on an empty window")
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
