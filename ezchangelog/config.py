"""Where the team store is, and who this machine publishes as.

Three sources, first match wins per field::

    1. environment          EZUPDATE_STORE / EZUPDATE_TOKEN / EZUPDATE_AUTHOR
    2. <store>/config.json  this machine's settings, not in any repo
    3. <repo>/.ez/config.json  the committed policy (``store``, ``exclude``)

The repo file is last, and for credentials it is not a source at all: a
teammate who clones a repo inherits its *policy*, never a credential. A token
is read only from the environment or from the private store -- a committed
``"token"`` is ignored, because a repo config that could name the destination
*and* authenticate to it turns one malicious commit into a complete
exfiltration payload. This module also has no code path that prints a token:
:meth:`Config.describe` reports where one came from, never what it is.

Under E2E the resolved ``token`` is the pasted ``ezu_``/``ezr_`` key -- the
actual key material. It is handed to the transport, which derives the wire
bearer and the encryption key from it (``ezchangelog.crypto``); the pasted
string itself never goes on the wire and, per the rule above, is never read
from a repo. ``device_id`` (this machine's ``devices.id`` UUID, printed by
``ezup device mint``) is deliberately *not* a credential -- the server already
knows it -- but it is identity, so it resolves from the environment and the
machine-private store config only, never from a repo.

Consent (``share``) is deliberately *not* resolved here; that lives in
:mod:`ezchangelog.share`, which is the only module allowed to answer it.
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .share import find_repo, load_repo_config
from .store import Store
from .transport import ChunkRef, SessionInfo, Transport, TransportError, build_transport

STORE_ENV = "EZUPDATE_STORE"
TOKEN_ENV = "EZUPDATE_TOKEN"
AUTHOR_ENV = "EZUPDATE_AUTHOR"
DEVICE_ID_ENV = "EZUPDATE_DEVICE_ID"

# transport.build_transport falls back to this one on its own; accepting it here
# too means a machine configured for either name keeps working.
LEGACY_TOKEN_ENV = "EZCHANGELOG_TOKEN"

# Keys a checked-in repo config may never supply, whatever it calls them. These
# are stripped at the moment the repo config is read, so no later code path can
# reach one by accident -- the guarantee is a property of the document we hand
# around, not of every reader remembering to be careful.
CREDENTIAL_KEYS = frozenset(
    {
        "token",
        "auth",
        "authorization",
        "api_key",
        "apikey",
        "secret",
        "password",
        "bearer",
        "credentials",
    }
)


@dataclass
class Config:
    """The resolved destination for this machine, plus where each field came from."""

    store_url: str = ""
    token: str = ""
    author: str = ""
    device_id: str = ""
    exclude: list[str] = field(default_factory=list)
    repo: str = ""
    origins: dict[str, str] = field(default_factory=dict)
    # Credential keys the repo config tried to set. Reported, not used: a
    # developer whose upload is failing deserves to know their checked-in
    # "token" was ignored on purpose -- and so does anyone reviewing a repo that
    # ships one.
    ignored_repo_keys: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return bool(self.store_url)

    @property
    def needs_token(self) -> bool:
        """HTTP stores need a device token; a directory store never does."""
        return self.store_url.startswith(("http://", "https://"))

    def transport_settings(self) -> dict[str, Any]:
        return {
            "store": self.store_url,
            "token": self.token,
            "author": self.author,
            "device_id": self.device_id,
        }

    def describe(self) -> list[str]:
        """Human-readable settings for `ezcl status`. Never includes the token."""
        token_state = (
            f"set (from {self.origins.get('token', 'config')})"
            if self.token
            else ("MISSING" if self.needs_token else "not needed")
        )
        lines = [
            f"store    {self.store_url or 'not configured'}"
            + (f"  ({self.origins['store_url']})" if self.store_url else ""),
            f"token    {token_state}",
            f"author   {self.author}  ({self.origins.get('author', 'default')})",
        ]
        if self.ignored_repo_keys:
            lines.append(
                f"IGNORED  {', '.join(self.ignored_repo_keys)} in "
                f"{self.repo or '<repo>'}/.ez/config.json: a committed config "
                f"cannot supply credentials"
            )
        return lines


def store_config_path(store: Store) -> Path:
    return store.root / "config.json"


def _read_json(path: Path) -> dict[str, Any]:
    """An unreadable config is no config: never fail a publish over a typo."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def strip_credentials(config: dict[str, Any]) -> dict[str, Any]:
    """A repo config with every credential-shaped key removed.

    Matching is case-insensitive and ignores ``-``/``_`` so that ``API-Key`` and
    ``api_key`` cannot walk past a spelling check.
    """
    return {
        key: value
        for key, value in config.items()
        if str(key).replace("-", "_").lower() not in CREDENTIAL_KEYS
    }


def _git_author(cwd: Path) -> str:
    """``user.email`` of the repo, which is how a PM already knows this person."""
    try:
        done = subprocess.run(
            ["git", "config", "--get", "user.email"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def default_author(cwd: str | Path | None = None) -> str:
    """A stable, recognisable name for this machine's uploads.

    The author becomes a path component of every uploaded object key, so it has
    to be filesystem-safe; the email's local part is both safe and legible.
    """
    here = Path(cwd).expanduser() if cwd else Path.cwd()
    email = _git_author(here if here.is_dir() else Path.home())
    candidate = email.split("@")[0] if email else ""
    if not candidate:
        try:
            candidate = getpass.getuser()
        except Exception:
            candidate = "unknown"
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in candidate)
    return safe.strip("-.") or "unknown"


def load_config(store: Store, cwd: str | Path | None = None) -> Config:
    """Resolve the publish destination for work happening in ``cwd``."""
    repo = find_repo(cwd if cwd is not None else Path.cwd())
    committed = load_repo_config(repo)
    # The one place a checked-in document enters this module, and the one place
    # its credentials are dropped.
    repo_config = strip_credentials(committed)
    machine = _read_json(store_config_path(store))

    origins: dict[str, str] = {}

    def pick(key: str, env: str, *extra_env: str) -> str:
        """First non-empty of: environment, this machine's store config, repo.

        ``repo_config`` has already had its credentials stripped, so the repo
        arm of this fallback can only ever answer for a non-secret field.
        """
        for name in (env, *extra_env):
            value = os.environ.get(name, "").strip()
            if value:
                origins[key] = f"${name}"
                return value
        value = str(machine.get(key.replace("store_url", "store")) or "").strip()
        if value:
            origins[key] = str(store_config_path(store))
            return value
        value = str(repo_config.get(key.replace("store_url", "store")) or "").strip()
        if value:
            origins[key] = f"{repo}/.ez/config.json" if repo else ".ez/config.json"
            return value
        return ""

    store_url = pick("store_url", STORE_ENV)
    token = pick("token", TOKEN_ENV, LEGACY_TOKEN_ENV)
    author = pick("author", AUTHOR_ENV)
    if not author:
        author = default_author(cwd)
        origins["author"] = "git user.email"

    # device_id resolves from the environment and the machine-private config
    # only -- deliberately not via pick(), whose fallback chain ends at the
    # repo. It is not a secret (the server knows it), but it is *identity*:
    # the wrap recipient every published data key is addressed to. A committed
    # config must not be able to redirect that.
    device_id = os.environ.get(DEVICE_ID_ENV, "").strip()
    if device_id:
        origins["device_id"] = f"${DEVICE_ID_ENV}"
    else:
        device_id = str(machine.get("device_id") or "").strip()
        if device_id:
            origins["device_id"] = str(store_config_path(store))

    # `exclude` is a repo-level statement about the repo's own files, so it is
    # not something a machine-level config should be able to weaken.
    patterns = repo_config.get("exclude")
    exclude = [p for p in patterns if isinstance(p, str)] if isinstance(patterns, list) else []

    return Config(
        store_url=store_url,
        token=token,
        author=author,
        device_id=device_id,
        exclude=exclude,
        repo=str(repo) if repo else "",
        origins=origins,
        ignored_repo_keys=sorted(set(committed) - set(repo_config)),
    )


def transport_for(config: Config) -> Transport:
    """Build the transport ``config`` names, with actionable errors."""
    if not config.configured:
        raise TransportError(
            f"no store configured; set ${STORE_ENV}, or a \"store\" key in "
            f"<store>/config.json or <repo>/.ez/config.json"
        )
    if config.needs_token and not config.token:
        raise TransportError(
            f"{config.store_url} needs a device token; set ${TOKEN_ENV} "
            f"(or put \"token\" in <store>/config.json)"
        )
    return build_transport(config.transport_settings())


class PullView:
    """Adapts a :class:`~ezchangelog.transport.Transport` to what pull expects.

    :mod:`ezchangelog.pull` types its backend as a Protocol of three methods
    returning plain dicts (``list_sessions``, ``list_chunks``, ``fetch_blob``),
    while the transport returns dataclasses and names the blob getter
    ``get_blob``. Rather than bend either module to the other, the seam is one
    small adapter -- so both sides keep the shape that suits them.
    """

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def list_sessions(self, since: str | None = None) -> list[dict[str, Any]]:
        rows = self.transport.list_sessions(since)
        return [row.to_dict() if isinstance(row, SessionInfo) else dict(row) for row in rows]

    def list_chunks(self, session: str) -> list[dict[str, Any]]:
        rows = self.transport.list_chunks(session)
        return [row.to_dict() if isinstance(row, ChunkRef) else dict(row) for row in rows]

    def fetch_blob(self, key: str) -> bytes:
        return self.transport.get_blob(key)

    # -- E2E surface, forwarded when the underlying transport has it ---------
    # pull's decrypt path duck-types these off its backend; a LocalDirTransport
    # (or an old fake) simply lacks them, which pull reads as "cannot decrypt".

    @property
    def key_set(self) -> Any:
        return getattr(self.transport, "key_set", None)

    @property
    def recipient_id(self) -> str:
        return str(getattr(self.transport, "device_id", "") or "")

    def session_enc(self, session: str) -> tuple[str, int]:
        probe = getattr(self.transport, "session_enc", None)
        return probe(session) if probe is not None else ("", 0)

    def get_wrapped_keys(self, session: str | None = None) -> list[dict[str, Any]]:
        fetch = getattr(self.transport, "get_wrapped_keys", None)
        return fetch(session) if fetch is not None else []

    def describe(self) -> str:
        return self.transport.describe()


__all__ = [
    "AUTHOR_ENV",
    "CREDENTIAL_KEYS",
    "Config",
    "DEVICE_ID_ENV",
    "PullView",
    "STORE_ENV",
    "TOKEN_ENV",
    "default_author",
    "load_config",
    "store_config_path",
    "strip_credentials",
    "transport_for",
]
