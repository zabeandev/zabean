"""
Local filesystem client — the agent-side counterpart to github_client.py.

Same interface contract as github_client.py, implemented against the local
filesystem and local Git history rather than the GitHub REST API.

This is the only module that knows how to read from local Git. Everything
in the agent pipeline receives data from these functions — swapping to a
different source (a bare repo, a worktree, a mock) requires changing only
this file.

All functions use subprocess to call Git directly. Git is always available
in a hook context. Subprocess failures are caught and returned as empty
values — callers treat missing data as a soft failure, log it, and continue.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Optional

from zabean.utils.logging import get_logger

_log = get_logger("local_client")

# git log format: sha|author|iso-timestamp|subject
_GIT_LOG_FORMAT = "%H|%an|%ai|%s"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_file_content_local(file_path: str, repo_root: str) -> tuple[str, str]:
    """
    Read file content from the local filesystem and return its Git blob SHA.

    Returns (content, blob_sha).
    Returns ("", "") on read failure or binary content — never raises.
    blob_sha is computed via `git hash-object`, matching the SHA GitHub would
    return for the same content.
    """
    full_path = os.path.join(repo_root, file_path)

    try:
        with open(full_path, encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        return ("", "")
    except OSError:
        return ("", "")

    blob_sha = _git_hash_object(file_path, repo_root)
    return (content, blob_sha)


def fetch_file_commits_local(
    file_path: str,
    repo_root: str,
    days: int = 90,
) -> list[dict]:
    """
    Get commit history for a file from local Git log, up to `days` days back.

    Returns a list of dicts, most-recent-first, each with:
        {"sha": str, "message": str, "author_name": str, "timestamp": str}

    Matches the structure returned by github_client.fetch_file_commits so the
    downstream assembly pipeline is identical regardless of data source.

    Returns an empty list on any Git failure — callers treat it as a soft failure.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )

    result = subprocess.run(
        [
            "git", "log",
            "--follow",
            f"--format={_GIT_LOG_FORMAT}",
            f"--since={since}",
            "-n", "50",
            "--",
            file_path,
        ],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )

    if result.returncode != 0:
        _log.warning(f"git log failed for {file_path}: {result.stderr.strip()}")
        return []

    commits: list[dict] = []
    for line in result.stdout.splitlines():
        parts = line.split("|", 3)
        if len(parts) < 4:
            continue
        sha, author, timestamp, message = parts
        commits.append({
            "sha": sha.strip(),
            "author_name": author.strip(),
            "timestamp": timestamp.strip(),
            "message": message.strip(),
        })

    return commits


def fetch_repo_tree_local(repo_root: str) -> list[dict]:
    """
    List all blobs in the current HEAD tree.

    Returns a list of dicts, one per file:
        {"path": str, "sha": str, "size": int}

    Uses `git ls-tree -r -l HEAD` which includes blob size in the output.
    Returns an empty list on any Git failure.
    """
    result = subprocess.run(
        ["git", "ls-tree", "-r", "-l", "HEAD"],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )

    if result.returncode != 0:
        _log.warning(f"git ls-tree failed: {result.stderr.strip()}")
        return []

    entries: list[dict] = []
    for line in result.stdout.splitlines():
        # Format: "<mode> SP <type> SP <sha> SP <size> TAB <path>"
        if "\t" not in line:
            continue
        meta, path = line.split("\t", 1)
        parts = meta.split()
        if len(parts) < 4 or parts[1] != "blob":
            continue
        sha = parts[2]
        try:
            size = int(parts[3])
        except ValueError:
            size = 0
        entries.append({"path": path.strip(), "sha": sha, "size": size})

    return entries


def fetch_latest_commit_sha_local(repo_root: str) -> str:
    """
    Return the full SHA of HEAD.

    Raises RuntimeError if git fails — the commit SHA is required for ground
    truth identity and there is no safe default.
    """
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD failed: {result.stderr.strip()}")
    return result.stdout.strip()


def get_repo_branch_local(repo_root: str) -> str:
    """Return the current branch name, or 'HEAD' if in detached HEAD state."""
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    return result.stdout.strip() if result.returncode == 0 else "HEAD"


def get_changed_files_local(repo_root: str) -> tuple[list[str], bool]:
    """
    Return the list of files changed in the most recent commit.

    Returns (changed_files, is_first_commit).
    is_first_commit is True when HEAD~1 does not exist (very first commit on
    the branch), in which case changed_files is empty and the caller should
    fall back to a full collection.
    """
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )

    if result.returncode != 0:
        # HEAD~1 doesn't exist — this is the first commit.
        return [], True

    files = [f for f in result.stdout.splitlines() if f.strip()]
    return files, False


def detect_repo_id_local(repo_root: str) -> str:
    """
    Derive a repo_id string from the Git remote URL, falling back to the
    directory name if no remote is configured.

    Returns a string in "owner/repo" form when a GitHub/GitLab remote is
    detected, or just "repo_name" for repos with no remote.
    """
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    if result.returncode == 0:
        url = result.stdout.strip()
        m = re.search(r"[:/]([^/]+/[^/.]+?)(?:\.git)?$", url)
        if m:
            return m.group(1)
    return os.path.basename(repo_root)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _git_hash_object(file_path: str, repo_root: str) -> str:
    """Compute the Git blob SHA for a file without writing to the object store."""
    result = subprocess.run(
        ["git", "hash-object", file_path],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    return result.stdout.strip() if result.returncode == 0 else ""
