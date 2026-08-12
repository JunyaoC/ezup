"""Consent: does this session share its transcript, and why.

Three levels, first match wins::

    1. <store>/sessions/<session-id>.share   "on" | "off"   -- set by `ezcl share`
    2. <repo>/.ez/config.json  "share"       always | ask | never
    3. built-in default                                     -- off

The default is off and installing the hook changes nothing here: sharing only
ever starts because a person ran `ezcl share on`, or because a repo committed a
policy that this machine then acknowledged. ``always`` needs that one-time
acknowledgement because a committed config file must not be able to switch on
sharing for a teammate who merely cloned the repo -- which is precisely the
failure this whole module exists to prevent.

Two rules make level 2 hold up against a hostile commit, and they are the
reason this module is not simply ``json.load``:

*The whole ancestry votes, and the most restrictive policy wins.* A policy read
from the nearest ``.ez`` alone would be defeated by ``mkdir vendor/pkg/.ez`` --
an empty directory is enough, and adding one is the cheapest thing a vendored
dependency or a drive-by PR can do. So ``never`` above a session is never
overridable from below.

*An acknowledgement is bound to the policy, not to the path.* The ack records a
hash of the exact config document that was accepted. Edit any field -- flip
``share``, repoint ``store`` -- and the acknowledgement is void and sharing
reverts to off until a person accepts the new text.

Every decision carries a human-readable ``reason``: the point of the feature is
that a developer can ask why their transcript is (or is not) leaving the
machine and get a straight answer.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .store import Store
from .window import isoformat

SHARE_MODES = ("always", "ask", "never")

# How much each mode restricts sharing. Resolution takes the maximum over the
# whole ancestor chain, which is what makes `never` non-overridable.
_RESTRICTION = {"always": 0, "ask": 1, "never": 2}

SESSION_ENV = "CLAUDE_CODE_SESSION_ID"

# Acknowledgement states, in the order they appear to a user: never accepted,
# accepted but the text has changed since, accepted and still current.
ACK_NONE = "none"
ACK_STALE = "stale"
ACK_VALID = "valid"


@dataclass
class Decision:
    """The resolved sharing state for one session, with its justification."""

    sharing: bool
    reason: str
    source: str
    repo_config: dict[str, Any] | None = None

    @property
    def state(self) -> str:
        return "on" if self.sharing else "off"


@dataclass
class Policy:
    """One committed ``share`` policy, and the repo whose config declared it."""

    mode: str
    repo: Path
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def where(self) -> str:
        """The file to quote at a user: the full path, never just the leaf.

        A nested policy and its parent can share a directory name, and "which
        config.json said that?" is the first question anyone asks.
        """
        return f"{self.repo}/.ez/config.json"

    @property
    def store(self) -> str:
        url = self.config.get("store")
        return url if isinstance(url, str) else ""


class ShareRefused(Exception):
    """Raised when a repo policy forbids what the caller asked for."""


# -- locations ---------------------------------------------------------------


def sessions_dir(store: Store) -> Path:
    return store.root / "sessions"


def marker_path(store: Store, session_id: str) -> Path:
    return sessions_dir(store) / f"{session_id}.share"


def ack_path(store: Store, repo: Path) -> Path:
    """Per-machine acknowledgement of one repo's committed policy.

    Keyed by a hash of the absolute repo path so the marker survives a repo
    being renamed on the store side and never leaks the path itself.

    The path is resolved first, because the resolver finds repos through a
    resolved cwd: on macOS ``/var`` is a symlink to ``/private/var``, and an
    ack recorded under one spelling would never match a lookup under the
    other. The failure is in the safe direction (sharing stays off) but it
    makes ``share ack`` silently not stick.
    """
    digest = hashlib.sha256(str(Path(repo).resolve()).encode("utf-8")).hexdigest()
    return store.root / "ack" / digest


def current_session_id() -> str | None:
    """The session this command is running inside, if any.

    Claude Code exports ``CLAUDE_CODE_SESSION_ID`` into the environment of the
    commands it runs, so `! ezcl share on` needs no session argument and no
    "most recent transcript" guesswork.
    """
    value = os.environ.get(SESSION_ENV, "").strip()
    return value or None


def _ancestors(cwd: str | Path | None) -> list[Path]:
    """``cwd`` and every directory above it, nearest first."""
    try:
        start = Path(cwd if cwd is not None else Path.cwd()).expanduser().resolve()
    except (OSError, ValueError):
        return []
    if start.is_file():
        start = start.parent
    return [start, *start.parents]


def find_repo(cwd: str | Path) -> Path | None:
    """The project directory governing ``cwd``.

    ``.ez/`` wins over ``.git/`` and is searched first over the whole ancestry:
    a monorepo can put its policy at the root while individual packages are
    their own git submodules, and the policy should still apply.

    This answers "which project is this?" -- for naming an upload and finding a
    store URL. It deliberately does *not* answer "may this be shared?": that is
    :func:`effective_policy`, which reads the whole chain, because the nearest
    ``.ez`` is exactly what an attacker gets to choose.
    """
    chain = _ancestors(cwd)
    for directory in chain:
        if (directory / ".ez").is_dir():
            return directory
    for directory in chain:
        if (directory / ".git").exists():
            return directory
    return None


def load_repo_config(repo: Path | None) -> dict[str, Any]:
    """Read ``<repo>/.ez/config.json``; an unreadable config is no config.

    A syntax error must not be able to turn sharing on, and must not break the
    hook either, so anything unparseable resolves to the built-in default.
    """
    if repo is None:
        return {}
    path = Path(repo) / ".ez" / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def repo_mode(config: dict[str, Any]) -> str | None:
    mode = config.get("share")
    return mode if isinstance(mode, str) and mode in SHARE_MODES else None


# -- policy chain -------------------------------------------------------------


def policy_chain(cwd: str | Path | None) -> list[Policy]:
    """Every committed policy at or above ``cwd``, nearest first.

    The walk goes all the way to the filesystem root rather than stopping at a
    git root: the point is that a checkout can be *placed inside* a locked tree
    (a vendored copy, a submodule, an unpacked dependency) and must not escape
    the policy of the tree it was placed in. Nobody can create directories above
    your checkout; anybody can create one inside it.
    """
    found: list[Policy] = []
    for directory in _ancestors(cwd):
        config = load_repo_config(directory)
        mode = repo_mode(config)
        if mode is not None:
            found.append(Policy(mode=mode, repo=directory, config=config))
    return found


def most_restrictive(chain: list[Policy]) -> Policy | None:
    """The policy that governs, out of a whole chain: the strictest one.

    Ties go to the outermost, because that is the one a hostile commit inside
    the tree cannot have written.
    """
    if not chain:
        return None
    strongest = max(_RESTRICTION[policy.mode] for policy in chain)
    return [p for p in chain if _RESTRICTION[p.mode] == strongest][-1]


def effective_policy(cwd: str | Path | None) -> Policy | None:
    """The committed policy that governs work in ``cwd``, if any."""
    return most_restrictive(policy_chain(cwd))


# -- acknowledgement ----------------------------------------------------------


def policy_fingerprint(config: Mapping[str, Any]) -> str:
    """A hash of the whole config document, not just of its ``share`` key.

    Everything is hashed, because everything can move bytes: ``store`` names the
    destination, and a key this version ignores may name one tomorrow. Keys are
    sorted and whitespace dropped first -- reformatting a file is not a change
    of policy, and nagging about it would train people to re-ack blind.
    """
    try:
        canonical = json.dumps(dict(config), sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        # Unserialisable content is still content: hash a stable repr rather
        # than treating a weird config as if it were empty.
        canonical = repr(sorted((str(k), repr(v)) for k, v in config.items()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_ack(repo: Path, store: Store) -> dict[str, Any]:
    try:
        data = json.loads(ack_path(store, repo).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def ack_status(repo: Path, store: Store) -> str:
    """``valid`` / ``stale`` / ``none`` for ``repo``'s policy on this machine.

    ``stale`` is the interesting one: the machine did accept a policy for this
    path, but not the text that is there now. Anything unrecognisable (a marker
    from an older version that recorded no fingerprint, a hand-edited file)
    counts as stale rather than valid -- an acknowledgement we cannot verify is
    not one we may act on.
    """
    repo = Path(repo).resolve()
    config = load_repo_config(repo)
    if repo_mode(config) is None:
        return ACK_NONE  # No policy: there is nothing an ack could refer to.
    record = _read_ack(repo, store)
    if not record:
        return ACK_NONE
    recorded = record.get("policy_sha256")
    if not isinstance(recorded, str) or not recorded or record.get("repo") != str(repo):
        return ACK_STALE
    return ACK_VALID if recorded == policy_fingerprint(config) else ACK_STALE


def is_acknowledged(repo: Path, store: Store) -> bool:
    return ack_status(repo, store) == ACK_VALID


def acknowledge(repo: Path, store: Store) -> Path:
    """Record that this machine accepted the policy ``repo`` declares *now*.

    The fingerprint is the whole point. An acknowledgement keyed on the path
    alone would keep applying after a teammate committed a different ``store``
    or flipped ``share`` to ``always`` -- so a `git pull` would start uploading
    with no human in the loop, which is the exact thing the ack exists to stop.

    Refused for a repo that declares no policy: accepting silence would be
    accepting, in advance, whatever that repo says later.
    """
    repo = Path(repo).resolve()
    config = load_repo_config(repo)
    mode = repo_mode(config)
    if mode is None:
        raise ShareRefused(
            f"refusing: {repo}/.ez/config.json declares no \"share\" policy, so "
            f"there is nothing to acknowledge. Acknowledging a repo that says "
            f"nothing now would accept whatever it says after the next pull."
        )
    path = ack_path(store, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "repo": str(repo),
        # Recorded for the human reading this file later; the fingerprint, not
        # these two, is what the check compares.
        "share": mode,
        "store": config.get("store") if isinstance(config.get("store"), str) else "",
        "policy_sha256": policy_fingerprint(config),
        "acknowledged_at": isoformat(datetime.now(timezone.utc)),
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path


# -- session marker -----------------------------------------------------------


def read_session(store: Store, session_id: str) -> str | None:
    """"on" / "off" from the session marker, or None when unset."""
    if not session_id:
        return None
    try:
        raw = marker_path(store, session_id).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    value = raw.strip().lower()
    return value if value in ("on", "off") else None


def set_session(
    session_id: str,
    on: bool,
    store: Store,
    *,
    cwd: str | Path | None = None,
) -> Path:
    """Opt this session in or out. Turning on is refused under ``never``.

    ``never`` is the one setting a developer cannot override from the CLI: a
    repo that declares its transcripts unshareable (customer data, regulated
    code) has to mean it, or committing the policy is pointless.
    """
    if not session_id:
        raise ShareRefused(
            "no session id: run this inside a Claude Code session, or pass one "
            "explicitly"
        )
    policy = effective_policy(cwd if cwd is not None else Path.cwd())
    if on and policy is not None and policy.mode == "never":
        raise ShareRefused(
            f"refusing: {policy.where} sets \"share\": \"never\", so sessions "
            f"under it cannot be shared -- including from a nested directory "
            f"with a policy of its own. Change the committed policy if that is "
            f"wrong."
        )
    path = marker_path(store, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("on\n" if on else "off\n", encoding="utf-8")
    return path


def clear_session(store: Store, session_id: str) -> bool:
    """Drop the explicit marker so the repo policy applies again."""
    path = marker_path(store, session_id)
    existed = path.is_file()
    path.unlink(missing_ok=True)
    return existed


# -- resolution ---------------------------------------------------------------


def resolve(session_id: str | None, cwd: str | Path | None, store: Store) -> Decision:
    """Resolve sharing for one session. Never raises; unknowns resolve to off."""
    try:
        return _resolve(session_id, cwd, store)
    except Exception as exc:  # the hook calls this on every turn
        return Decision(
            sharing=False,
            reason=f"off — sharing could not be resolved ({exc}), so nothing is shared",
            source="error",
            repo_config=None,
        )


def _resolve(session_id: str | None, cwd: str | Path | None, store: Store) -> Decision:
    chain = policy_chain(cwd)
    policy = most_restrictive(chain)
    mode = policy.mode if policy else None
    where = policy.where if policy else ".ez/config.json"
    # With no policy anywhere, the nearest project's config still supplies the
    # store URL for a session the user opted in by hand.
    config = policy.config if policy else load_repo_config(find_repo(cwd or Path.cwd()))
    # Said out loud whenever the governing policy is not the nearest one: "my
    # repo says always but nothing uploads" is otherwise unanswerable.
    override = (
        f" (the most restrictive policy above a session wins, so this overrides "
        f"the nearer {chain[0].where}, which says {chain[0].mode})"
        if policy is not None and chain[0].repo != policy.repo
        else ""
    )

    marker = read_session(store, session_id or "")
    if marker == "on":
        # The one place the ordering bends: `never` is documented as a hard off
        # and `ezcl share on` refuses to write this marker, so an "on" marker
        # under `never` can only be a stale file or a hand edit. Honouring it
        # would make the repo's strongest setting the easiest one to defeat.
        if mode == "never":
            return Decision(
                False,
                f"off — {where} says never{override}, which overrides the opt-in "
                f"marker left on this session; delete "
                f"{marker_path(store, session_id or '')} to clear it",
                "repo",
                config,
            )
        return Decision(
            True,
            "on — you turned sharing on for this session with `ezcl share on`",
            "session",
            config,
        )
    if marker == "off":
        return Decision(
            False,
            "off — you turned sharing off for this session with `ezcl share off`",
            "session",
            config,
        )

    if mode == "always" and policy is not None:
        status = ack_status(policy.repo, store)
        if status == ACK_VALID:
            return Decision(
                True,
                f"on — {where} says always, and this machine acknowledged that "
                f"exact policy{override}; run `ezcl share off` to opt this "
                f"session out",
                "repo",
                config,
            )
        if status == ACK_STALE:
            # The acknowledgement was for different text. Reverting to off is
            # the whole value of fingerprinting it: an edited config -- a new
            # `store`, a flipped `share` -- has to be accepted by a person, not
            # inherited from the one they accepted last month.
            return Decision(
                False,
                f"off — {where} says always, but its contents changed since this "
                f"machine acknowledged them, so the acknowledgement is void; "
                f"review the file and run `ezcl share ack` to accept the new "
                f"policy, or `ezcl share on` for just this session",
                "repo",
                config,
            )
        return Decision(
            False,
            f"off — {where} says always, but this machine has not acknowledged "
            f"that policy yet; run `ezcl share ack` to accept it, or "
            f"`ezcl share on` for just this session",
            "repo",
            config,
        )
    if mode == "ask":
        return Decision(
            False,
            f"off — {where} says ask{override}, and this session has not opted "
            f"in; run `ezcl share on` to share this session",
            "repo",
            config,
        )
    if mode == "never":
        return Decision(
            False,
            f"off — {where} says never{override}, so sessions under it are "
            f"never shared and `ezcl share on` will refuse",
            "repo",
            config,
        )

    return Decision(
        False,
        "off — nothing has opted this session in: no session setting and no "
        "`share` policy in .ez/config.json, and the default is off",
        "default",
        config or None,
    )


def store_url(decision: Decision) -> str | None:
    """The store endpoint the repo config names, if it names one."""
    url = (decision.repo_config or {}).get("store")
    return url if isinstance(url, str) and url else None


def project_name(cwd: str | Path | None) -> str:
    repo = find_repo(cwd if cwd is not None else Path.cwd())
    if repo is not None:
        return repo.name
    try:
        return Path(cwd or Path.cwd()).name
    except (OSError, ValueError):
        return "project"
