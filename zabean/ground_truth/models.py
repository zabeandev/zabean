"""
Data contracts for the Zabean ground truth system.

RepoGroundTruth and FileGroundTruth are the canonical output types for the
collection pipeline. Every downstream component — the interpreter, the artifact
generator — depends on these schemas. They are versioned explicitly; bump
SCHEMA_VERSION when any field is added, removed, or changes meaning.

Design principles:
  - could_not_determine is always populated when a field defaulted due to a
    fetch or parse failure. The ground truth is honest about its own limits.
  - Datetimes are stored as UTC-aware datetime objects and serialized as
    ISO 8601 strings with timezone offset.
  - to_dict / from_dict are the only serialization surface — nothing else
    should reach into the dataclass fields directly for persistence.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


SCHEMA_VERSION = "1.0.0"


def _file_path_hash(file_path: str) -> str:
    """Return the first 12 hex characters of the SHA-256 of the file path.

    Stable across runs for the same path; suitable as a short identifier.
    """
    return hashlib.sha256(file_path.encode()).hexdigest()[:12]


@dataclass
class FileGroundTruth:
    # -------------------------------------------------------------------------
    # Identity
    # -------------------------------------------------------------------------
    repo_id: str            # "owner/repo"
    commit_sha: str         # full SHA of the repo at collection time
    file_path: str          # path relative to repo root, forward-slash separated
    blob_sha: str           # Git content hash — changes iff file content changes
    file_path_hash: str     # sha256(file_path)[:12] — stable short identifier
    schema_version: str     # bump on breaking schema changes
    fetched_at: datetime    # UTC timestamp of collection

    # -------------------------------------------------------------------------
    # Content
    # -------------------------------------------------------------------------
    language: str           # detected from file extension via EXTENSION_TO_LANGUAGE
    raw_content: str        # full decoded file text
    line_count: int
    char_count: int
    size_bytes: int

    # -------------------------------------------------------------------------
    # Static dependency extraction
    # Resolved without executing the code — regex and lightweight AST only.
    # -------------------------------------------------------------------------
    imports_raw: list[str]          # import strings exactly as they appear in source
    imports_internal: list[str]     # resolved to actual paths within the repo
    imports_external: list[str]     # third-party packages and standard library
    imports_unresolved: list[str]   # raw strings that could not be classified
    dependents: list[str]           # other repo files that import this file (reverse map)

    # -------------------------------------------------------------------------
    # Commit signals
    # Populated from the GitHub Commits API. Fields default and are added to
    # could_not_determine when history is unavailable.
    # -------------------------------------------------------------------------
    most_recent_commit_sha: str                     # "" if unavailable
    most_recent_commit_timestamp: Optional[datetime]  # None if unavailable
    most_recent_commit_author: str                  # "" if unavailable
    most_recent_commit_message: str                 # first line only; "" if unavailable
    commit_frequency_30d: int                       # commits touching this file in last 30 days
    commit_frequency_90d: int                       # commits touching this file in last 90 days
    days_since_last_change: int                     # -1 if unavailable
    top_contributors: list[str]                     # up to 3 authors by commit count, most-first

    # -------------------------------------------------------------------------
    # Structural signals
    # -------------------------------------------------------------------------
    is_entry_point_candidate: bool  # stem is index/main/app/server and depth <= 1
    is_in_test_directory: bool      # path contains a recognized test directory name
    relative_depth: int             # directory nesting depth (root files = 0)

    # -------------------------------------------------------------------------
    # Collection metadata
    # -------------------------------------------------------------------------
    determined_by: list[str]        # data sources used: ["file_content", "commit_history", "import_parse"]
    parse_warnings: list[str]       # non-fatal issues encountered during static parse
    could_not_determine: list[str]  # fields that defaulted due to parse or fetch failure

    # -------------------------------------------------------------------------
    # Serialization
    # -------------------------------------------------------------------------

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()}
        d["fetched_at"] = self.fetched_at.isoformat()
        d["most_recent_commit_timestamp"] = (
            self.most_recent_commit_timestamp.isoformat()
            if self.most_recent_commit_timestamp is not None
            else None
        )
        return d

    @classmethod
    def from_dict(cls, d: dict) -> FileGroundTruth:
        d = dict(d)
        d["fetched_at"] = datetime.fromisoformat(d["fetched_at"])
        ts = d.get("most_recent_commit_timestamp")
        d["most_recent_commit_timestamp"] = datetime.fromisoformat(ts) if ts else None
        return cls(**d)


@dataclass
class RepoGroundTruth:
    # -------------------------------------------------------------------------
    # Identity
    # -------------------------------------------------------------------------
    repo_id: str            # "owner/repo"
    commit_sha: str         # full SHA of the branch HEAD at collection time
    branch: str
    fetched_at: datetime    # UTC timestamp of collection
    schema_version: str     # bump on breaking schema changes

    # -------------------------------------------------------------------------
    # Structure
    # Derived from the GitHub tree API — no file content required.
    # -------------------------------------------------------------------------
    total_file_count: int           # all blobs in the tree, including non-source
    source_file_count: int          # blobs passing extension + path filtering
    language_distribution: dict     # {"javascript": 21, "python": 4}
    directory_structure: dict       # {"lib/": 8, "lib/router/": 4} — entry count per prefix
    max_directory_depth: int        # deepest nesting level found in the tree

    # -------------------------------------------------------------------------
    # Signals
    # -------------------------------------------------------------------------
    has_readme: bool
    readme_length_chars: int        # 0 if no README present
    readme_headers: list[str]       # h1/h2 header text extracted from README
    has_package_manifest: bool      # package.json, requirements.txt, Cargo.toml, etc.
    manifest_type: Optional[str]    # "npm" | "pip" | "maven" | "cargo" | ...; None if absent
    has_test_directory: bool        # tree contains at least one recognized test directory
    test_directories: list[str]     # paths of detected test directories

    # Entry points: names index/main/app/server at or near root — heuristic but deterministic
    entry_point_candidates: list[str]

    # -------------------------------------------------------------------------
    # Activity signals
    # Aggregated from per-file commit history.
    # -------------------------------------------------------------------------
    most_active_files: list[str]    # top 5 by commit_frequency_30d; ties broken by path
    largest_files: list[str]        # top 5 by line count; ties broken by path
    commit_frequency_30d: int       # sum of all per-file commit_frequency_30d values

    # -------------------------------------------------------------------------
    # Collection metadata
    # -------------------------------------------------------------------------
    files_collected: int
    files_skipped: int
    skip_reasons: dict              # {"too_large": 3, "path_component:node_modules": 14}
    collection_errors: list[str]    # non-fatal per-file errors recorded during collection
    determined_by: list[str]        # data sources used: ["file_tree", "commit_history", "readme"]

    # -------------------------------------------------------------------------
    # Serialization
    # -------------------------------------------------------------------------

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()}
        d["fetched_at"] = self.fetched_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> RepoGroundTruth:
        d = dict(d)
        d["fetched_at"] = datetime.fromisoformat(d["fetched_at"])
        return cls(**d)
