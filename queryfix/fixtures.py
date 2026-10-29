"""Recorded pull-request fixture support for offline CLI reviews.

The on-disk format is the recorded GitHub response already used by the ingest
tests: ``sandbox-pull-request.json``, ``sandbox-pull-request-files.json``, and
the full head tree under ``sandbox-head/``.  It is deliberately not a new CLI
fixture format; this module merely makes the existing stand-in available outside
pytest so the installed command can exercise the real ingest pipeline offline.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from github import Github

__all__ = ["FixtureError", "RecordedGitHub", "RecordedPullRequest", "load_recorded_fixture"]


class FixtureError(ValueError):
    """The selected path is not a complete recorded pull-request fixture."""


@dataclass(frozen=True)
class RecordedPullRequest:
    """The narrow portion of the existing recorded GitHub payload that ingest reads."""

    repo: str
    number: int
    base_sha: str
    head_sha: str
    entries: tuple[Mapping[str, Any], ...]
    head_text: Mapping[str, str]


def load_recorded_fixture(path: Path) -> tuple[RecordedPullRequest, Github]:
    """Load the existing fixture directory and return its read-only GitHub stand-in."""
    try:
        pull = json.loads((path / "sandbox-pull-request.json").read_text(encoding="utf-8"))
        entries = json.loads((path / "sandbox-pull-request-files.json").read_text(encoding="utf-8"))
        head_root = path / "sandbox-head"
        head_text = {
            item.relative_to(head_root).as_posix(): item.read_text(encoding="utf-8")
            for item in head_root.rglob("*")
            if item.is_file()
        }
        recorded = RecordedPullRequest(
            repo=str(pull["base"]["repo"]["full_name"]),
            number=int(pull["number"]),
            base_sha=str(pull["base"]["sha"]),
            head_sha=str(pull["head"]["sha"]),
            entries=tuple(cast(list[Mapping[str, Any]], entries)),
            head_text=head_text,
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        msg = f"invalid recorded PR fixture at {path}: {type(error).__name__}"
        raise FixtureError(msg) from None

    return recorded, RecordedGitHub(recorded).client


class RecordedContents:
    """The PyGithub content surface narrowed to bytes consumed by ingest."""

    def __init__(self, text: str) -> None:
        self.decoded_content = text.encode("utf-8")


class RecordedFile:
    """The PyGithub changed-file surface narrowed to fields consumed by ingest."""

    def __init__(self, entry: Mapping[str, Any]) -> None:
        self.filename = str(entry["filename"])
        self.status = str(entry["status"])
        self.previous_filename = cast(str | None, entry.get("previous_filename"))
        self.patch = cast(str | None, entry.get("patch"))
        self.additions = int(entry["additions"])
        self.deletions = int(entry["deletions"])


class RecordedRef:
    """The SHA-bearing half of a PyGithub pull-request ref."""

    def __init__(self, sha: str) -> None:
        self.sha = sha


@dataclass
class RecordedGitHub:
    """Socket-free, read-only PyGithub stand-in backed solely by a recording."""

    recorded: RecordedPullRequest
    repo_lookups: list[str] = field(default_factory=list)
    head_reads: list[tuple[str, str]] = field(default_factory=list)

    @property
    def client(self) -> Github:
        return cast(Github, self)

    def get_repo(self, full_name: str) -> RecordedRepo:
        self.repo_lookups.append(full_name)
        if full_name != self.recorded.repo:
            msg = f"fixture records {self.recorded.repo!r}, not {full_name!r}"
            raise FixtureError(msg)
        return RecordedRepo(self)


class RecordedRepo:
    """Read-only repository surface used by fetch/ingest."""

    def __init__(self, owner: RecordedGitHub) -> None:
        self._owner = owner

    def get_pull(self, number: int) -> RecordedPull:
        if number != self._owner.recorded.number:
            msg = f"fixture records pull request #{self._owner.recorded.number}, not #{number}"
            raise FixtureError(msg)
        return RecordedPull(self._owner)

    def get_contents(self, path: str, ref: str | None = None) -> RecordedContents:
        self._owner.head_reads.append((path, ref or ""))
        text = self._owner.recorded.head_text.get(path)
        if text is None:
            msg = f"fixture has no recorded head content for {path!r}"
            raise FixtureError(msg)
        return RecordedContents(text)


class RecordedPull:
    """Read-only pull-request surface; deliberately has no comment-write methods."""

    def __init__(self, owner: RecordedGitHub) -> None:
        self._owner = owner
        self.base = RecordedRef(owner.recorded.base_sha)
        self.head = RecordedRef(owner.recorded.head_sha)

    def get_files(self) -> list[RecordedFile]:
        return [RecordedFile(entry) for entry in self._owner.recorded.entries]
