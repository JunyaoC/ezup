"""Git corroboration for a project's window.

Sessions say what was attempted; commits say what actually landed. Reading
both lets an entry cite a real commit instead of only a transcript.
Everything here is read-only and degrades quietly when a path is not a repo.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

_SEP = "\x1f"
_END = "\x1e"


def _run(args: list[str], cwd: Path) -> str | None:
    try:
        done = subprocess.run(
            args, cwd=str(cwd), capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def repo_root(path: Path) -> Path | None:
    """The repository containing ``path``, or None when it is not tracked."""
    if not path.is_dir():
        return None
    out = _run(["git", "rev-parse", "--show-toplevel"], path)
    return Path(out.strip()) if out and out.strip() else None


def current_branch(repo: Path) -> str | None:
    out = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo)
    return out.strip() if out and out.strip() else None


def commits_between(
    repo: Path, since: datetime, until: datetime, limit: int = 120
) -> list[dict[str, Any]]:
    """Commits authored inside the window, each with the files it touched."""
    pretty = _SEP.join(["%H", "%h", "%an", "%aI", "%s"]) + _END
    out = _run(
        [
            "git",
            "log",
            f"--since={since.isoformat()}",
            f"--until={until.isoformat()}",
            f"--max-count={limit}",
            f"--pretty=format:{pretty}",
            "--name-only",
            "--no-merges",
        ],
        repo,
    )
    if not out:
        return []

    commits: list[dict[str, Any]] = []
    for chunk in out.split(_END):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        header, _, body = chunk.partition("\n")
        parts = header.split(_SEP)
        if len(parts) < 5:
            continue
        files = [line.strip() for line in body.splitlines() if line.strip()]
        commits.append(
            {
                "sha": parts[0],
                "short": parts[1],
                "author": parts[2],
                "ts": parts[3],
                "subject": parts[4],
                "files": files[:40],
            }
        )
    return commits


def survey(
    roots: list[Path], since: datetime, until: datetime
) -> dict[str, dict[str, Any]]:
    """Map repository root -> {branch, commits} for every distinct repo."""
    found: dict[str, dict[str, Any]] = {}
    for root in roots:
        repo = repo_root(root)
        if repo is None or str(repo) in found:
            continue
        found[str(repo)] = {
            "root": str(repo),
            "name": repo.name,
            "branch": current_branch(repo),
            "commits": commits_between(repo, since, until),
        }
    return found


def for_paths(
    survey_result: dict[str, dict[str, Any]], paths: list[str]
) -> list[dict[str, Any]]:
    """Commits whose touched files overlap any of ``paths``.

    Matching is on the path tail, because a transcript records absolute paths
    while git records repo-relative ones -- and a worktree makes the absolute
    prefixes differ anyway.
    """
    wanted = {Path(p).name for p in paths if p}
    if not wanted:
        return []
    hits: list[dict[str, Any]] = []
    for repo in survey_result.values():
        for commit in repo["commits"]:
            if any(Path(f).name in wanted for f in commit["files"]):
                hits.append({**commit, "repo": repo["name"]})
    hits.sort(key=lambda c: c["ts"])
    return hits
