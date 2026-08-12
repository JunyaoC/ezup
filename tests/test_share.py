"""The consent table: what is shared, and why.

This is the feature's whole point, so the tests here are exhaustive about the
three levels rather than representative: every cell of the table is asserted,
including the ones that only exist to stop a mistake (an "on" marker under a
``never`` repo, a committed ``always`` on a machine that never agreed to it).
"""

from __future__ import annotations

import json

from ezchangelog import share
from tests.support import TempHomeTestCase


class DefaultsTests(TempHomeTestCase):
    def test_default_is_off_with_no_config_anywhere(self) -> None:
        decision = share.resolve("sess-1", self.make_bare_dir(), self.store)
        self.assertFalse(decision.sharing)
        self.assertEqual("default", decision.source)
        self.assertEqual("off", decision.state)

    def test_no_session_id_is_off(self) -> None:
        decision = share.resolve(None, self.make_bare_dir(), self.store)
        self.assertFalse(decision.sharing)

    def test_unparseable_repo_config_is_treated_as_no_config(self) -> None:
        repo = self.make_repo()
        (repo / ".ez" / "config.json").write_text("{ not json", encoding="utf-8")
        decision = share.resolve("sess-1", repo, self.store)
        self.assertFalse(decision.sharing)
        self.assertEqual("default", decision.source)

    def test_unknown_share_mode_is_ignored(self) -> None:
        repo = self.make_repo("weird", share="sometimes")
        decision = share.resolve("sess-1", repo, self.store)
        self.assertFalse(decision.sharing)
        self.assertEqual("default", decision.source)

    def test_resolve_never_raises(self) -> None:
        decision = share.resolve("sess-1", self.tmp / "does" / "not" / "exist", self.store)
        self.assertFalse(decision.sharing)


class RepoPolicyTests(TempHomeTestCase):
    def test_ask_is_off_until_the_session_opts_in(self) -> None:
        repo = self.make_repo("asking", share="ask")
        decision = share.resolve("sess-1", repo, self.store)
        self.assertFalse(decision.sharing)
        self.assertEqual("repo", decision.source)
        self.assertIn("ezup share on", decision.reason)

    def test_never_is_off(self) -> None:
        repo = self.make_repo("locked", share="never")
        decision = share.resolve("sess-1", repo, self.store)
        self.assertFalse(decision.sharing)
        self.assertEqual("repo", decision.source)

    def test_always_needs_this_machines_acknowledgement(self) -> None:
        repo = self.make_repo("teamrepo", share="always")

        before = share.resolve("sess-1", repo, self.store)
        self.assertFalse(before.sharing, "a committed policy alone must not share")
        self.assertEqual("repo", before.source)
        self.assertIn("ezup share ack", before.reason)

        share.acknowledge(repo, self.store)

        after = share.resolve("sess-1", repo, self.store)
        self.assertTrue(after.sharing)
        self.assertEqual("repo", after.source)

    def test_acknowledgement_is_per_repo(self) -> None:
        first = self.make_repo("one", share="always")
        second = self.make_repo("two", share="always")
        share.acknowledge(first, self.store)

        self.assertTrue(share.resolve("sess-1", first, self.store).sharing)
        self.assertFalse(
            share.resolve("sess-1", second, self.store).sharing,
            "acknowledging one repo must not enable another",
        )

    def test_store_url_comes_from_the_repo_config(self) -> None:
        repo = self.make_repo("withstore", share="ask", store="https://ez.example/v1")
        decision = share.resolve("sess-1", repo, self.store)
        self.assertEqual("https://ez.example/v1", share.store_url(decision))

    def test_no_store_url_when_the_config_omits_it(self) -> None:
        repo = self.make_repo("nostore", share="ask")
        self.assertIsNone(share.store_url(share.resolve("sess-1", repo, self.store)))


class SessionMarkerTests(TempHomeTestCase):
    def test_marker_on_beats_ask(self) -> None:
        repo = self.make_repo("asking", share="ask")
        share.set_session("sess-1", True, self.store, cwd=repo)

        decision = share.resolve("sess-1", repo, self.store)
        self.assertTrue(decision.sharing)
        self.assertEqual("session", decision.source)

    def test_marker_off_beats_acknowledged_always(self) -> None:
        repo = self.make_repo("teamrepo", share="always")
        share.acknowledge(repo, self.store)
        share.set_session("sess-1", False, self.store, cwd=repo)

        decision = share.resolve("sess-1", repo, self.store)
        self.assertFalse(decision.sharing)
        self.assertEqual("session", decision.source)

    def test_marker_is_per_session(self) -> None:
        repo = self.make_repo("asking", share="ask")
        share.set_session("sess-1", True, self.store, cwd=repo)
        self.assertTrue(share.resolve("sess-1", repo, self.store).sharing)
        self.assertFalse(share.resolve("sess-2", repo, self.store).sharing)

    def test_marker_off_with_no_repo_policy(self) -> None:
        plain = self.make_bare_dir()
        share.set_session("sess-1", False, self.store, cwd=plain)
        decision = share.resolve("sess-1", plain, self.store)
        self.assertFalse(decision.sharing)
        self.assertEqual("session", decision.source)

    def test_marker_file_contents_and_location(self) -> None:
        plain = self.make_bare_dir()
        path = share.set_session("sess-1", True, self.store, cwd=plain)
        self.assertEqual(self.store.root / "sessions" / "sess-1.share", path)
        self.assertEqual("on", path.read_text(encoding="utf-8").strip())
        self.assertEqual("on", share.read_session(self.store, "sess-1"))

    def test_garbage_in_the_marker_falls_through_to_the_repo(self) -> None:
        repo = self.make_repo("teamrepo", share="always")
        share.acknowledge(repo, self.store)
        share.marker_path(self.store, "sess-1").parent.mkdir(parents=True, exist_ok=True)
        share.marker_path(self.store, "sess-1").write_text("maybe\n", encoding="utf-8")

        self.assertIsNone(share.read_session(self.store, "sess-1"))
        decision = share.resolve("sess-1", repo, self.store)
        self.assertTrue(decision.sharing)
        self.assertEqual("repo", decision.source)

    def test_clear_session_restores_the_repo_policy(self) -> None:
        repo = self.make_repo("teamrepo", share="always")
        share.acknowledge(repo, self.store)
        share.set_session("sess-1", False, self.store, cwd=repo)
        self.assertFalse(share.resolve("sess-1", repo, self.store).sharing)

        self.assertTrue(share.clear_session(self.store, "sess-1"))
        self.assertFalse(share.clear_session(self.store, "sess-1"), "second clear is a no-op")

        decision = share.resolve("sess-1", repo, self.store)
        self.assertTrue(decision.sharing)
        self.assertEqual("repo", decision.source)


class NeverTests(TempHomeTestCase):
    def test_set_session_on_is_refused_under_never(self) -> None:
        repo = self.make_repo("locked", share="never")
        with self.assertRaises(share.ShareRefused) as caught:
            share.set_session("sess-1", True, self.store, cwd=repo)
        self.assertIn("never", str(caught.exception))
        self.assertFalse(share.marker_path(self.store, "sess-1").exists())

    def test_set_session_off_is_allowed_under_never(self) -> None:
        repo = self.make_repo("locked", share="never")
        path = share.set_session("sess-1", False, self.store, cwd=repo)
        self.assertEqual("off", path.read_text(encoding="utf-8").strip())

    def test_a_stale_on_marker_cannot_defeat_never(self) -> None:
        # `ezcl share on` refuses to write this, so it can only be a leftover
        # from before the policy landed, or a hand edit.
        repo = self.make_repo("locked", share="never")
        marker = share.marker_path(self.store, "sess-1")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("on\n", encoding="utf-8")

        decision = share.resolve("sess-1", repo, self.store)
        self.assertFalse(decision.sharing)
        self.assertEqual("repo", decision.source)

    def test_never_beats_an_acknowledgement(self) -> None:
        repo = self.make_repo("locked", share="never")
        share.acknowledge(repo, self.store)
        self.assertFalse(share.resolve("sess-1", repo, self.store).sharing)

    def test_empty_session_id_is_refused(self) -> None:
        with self.assertRaises(share.ShareRefused):
            share.set_session("", True, self.store, cwd=self.make_bare_dir())


class RepoDiscoveryTests(TempHomeTestCase):
    def test_ez_wins_over_a_nearer_git_dir(self) -> None:
        root = self.make_repo("mono", share="ask")
        package = root / "packages" / "api"
        package.mkdir(parents=True)
        (package / ".git").mkdir()

        self.assertEqual(root, share.find_repo(package))
        decision = share.resolve("sess-1", package, self.store)
        self.assertEqual("repo", decision.source)

    def test_policy_applies_to_subdirectories(self) -> None:
        repo = self.make_repo("teamrepo", share="always")
        share.acknowledge(repo, self.store)
        deep = repo / "src" / "inner"
        deep.mkdir(parents=True)
        self.assertTrue(share.resolve("sess-1", deep, self.store).sharing)

    def test_config_is_read_from_the_repo_root(self) -> None:
        repo = self.make_repo("teamrepo", share="ask", store="https://ez.example")
        self.assertEqual(
            {"share": "ask", "store": "https://ez.example"},
            share.load_repo_config(repo),
        )
        self.assertEqual({}, share.load_repo_config(None))

    def test_project_name_is_the_repo_name(self) -> None:
        repo = self.make_repo("teamrepo", share="ask")
        deep = repo / "src"
        deep.mkdir()
        self.assertEqual("teamrepo", share.project_name(deep))


class AcknowledgementFileTests(TempHomeTestCase):
    def test_ack_marker_does_not_contain_the_repo_path_in_its_name(self) -> None:
        repo = self.make_repo("secret-project-name", share="always")
        path = share.acknowledge(repo, self.store)
        self.assertNotIn("secret-project-name", path.name)
        self.assertTrue(share.is_acknowledged(repo, self.store))
        # The body is allowed to name the repo; only the filename is a hash.
        self.assertEqual(str(repo), json.loads(path.read_text(encoding="utf-8"))["repo"])
