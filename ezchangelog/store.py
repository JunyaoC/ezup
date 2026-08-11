"""The ``~/.ezchangelog`` store: verbatim raw copies plus a pointer index.

Layout::

    ~/.ezchangelog/
      index.json                              pointers + ingest state
      raw/<project-slug>/<sessionId>.jsonl    full-fidelity verbatim copy
      runs/<run-id>.json                      manifest of one collect run

The index exists so repeat scrapes are cheap: a transcript whose size and mtime
are unchanged since last ingest is never reopened.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .window import isoformat

DEFAULT_STORE = Path.home() / ".ezchangelog"
INDEX_VERSION = 4


def default_store() -> Path:
    override = os.environ.get("EZCHANGELOG_HOME")
    return Path(override).expanduser() if override else DEFAULT_STORE


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    )
    try:
        with handle:
            json.dump(payload, handle, indent=2, sort_keys=False)
            handle.write("\n")
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


@dataclass
class Store:
    root: Path

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def agent_dir(self) -> Path:
        """Working directory for the pipeline's own ``claude -p`` calls.

        Each headless call writes its own transcript into ~/.claude/projects.
        Running them from here parks those transcripts under one known project
        path, which collection then excludes -- otherwise the tool journals
        itself journaling.
        """
        return self.root / ".agent"

    def ensure(self) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.agent_dir.mkdir(parents=True, exist_ok=True)

    # -- index ---------------------------------------------------------------

    def load_index(self) -> dict[str, Any]:
        if not self.index_path.is_file():
            return {"version": INDEX_VERSION, "updated_at": None, "sources": {}}
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"version": INDEX_VERSION, "updated_at": None, "sources": {}}
        if not isinstance(data, dict) or "sources" not in data:
            return {"version": INDEX_VERSION, "updated_at": None, "sources": {}}
        if data.get("version") != INDEX_VERSION:
            # Forward/backward incompatible index: start clean rather than
            # trusting stale pointer shapes.
            return {"version": INDEX_VERSION, "updated_at": None, "sources": {}}
        return data

    def save_index(self, index: dict[str, Any]) -> None:
        index["version"] = INDEX_VERSION
        index["updated_at"] = isoformat(datetime.now(timezone.utc))
        _write_json_atomic(self.index_path, index)

    # -- raw copies ----------------------------------------------------------

    def raw_path_for(self, project_slug: str, session_id: str) -> Path:
        return self.raw_dir / project_slug / f"{session_id}.jsonl"

    def copy_raw(self, source: Path, project_slug: str, session_id: str) -> Path:
        """Copy a transcript verbatim into the store (full-fidelity passthrough)."""
        target = self.raw_path_for(project_slug, session_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            dir=target.parent, delete=False, suffix=".tmp"
        )
        try:
            with handle, source.open("rb") as src:
                shutil.copyfileobj(src, handle)
            os.replace(handle.name, target)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise
        return target

    # -- run manifests -------------------------------------------------------

    def write_run(self, run_id: str, manifest: dict[str, Any]) -> Path:
        path = self.runs_dir / f"{run_id}.json"
        _write_json_atomic(path, manifest)
        return path

    def latest_run(self) -> dict[str, Any] | None:
        if not self.runs_dir.is_dir():
            return None
        runs = sorted(self.runs_dir.glob("*.json"))
        if not runs:
            return None
        try:
            return json.loads(runs[-1].read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
