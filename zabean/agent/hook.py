"""
Post-commit hook — the entry point that Git calls after every commit.

Design constraints:
  - Stateless. Every run is fully independent.
  - Non-blocking. If collection fails for any reason, the commit still
    succeeds. The hook logs the error and exits cleanly.
  - Honest. Silent failures are not permitted. Every error is logged explicitly.
  - Fast. Incremental runs target under two seconds for typical repos.

Usage (called by Git automatically after installation):
    python -m zabean.agent.hook

Usage (manual full collection):
    python -m zabean.agent.hook --full
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from typing import Optional

from zabean.agent.collector import (
    collect_local_full,
    collect_local_incremental,
    get_source_file_paths,
    is_collectable_source_file,
)
from zabean.ground_truth.local_client import get_changed_files_local


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_hook(full: bool = False) -> None:
    """
    Execute the post-commit hook.

    Detects mode (initial vs incremental), collects ground truth for the
    appropriate set of files, and logs the result with wall-clock timing.
    All errors are caught — the function always returns cleanly.
    """
    start = time.monotonic()

    # -------------------------------------------------------------------------
    # Step 1 — locate repo root
    # -------------------------------------------------------------------------
    try:
        repo_root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        _print("[error] not inside a git repository — hook cannot run")
        return

    output_dir = os.path.join(repo_root, "output")

    # -------------------------------------------------------------------------
    # Step 2 — get the current commit SHA for the log header
    # -------------------------------------------------------------------------
    try:
        commit_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            cwd=repo_root,
            stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        _print("[error] could not determine HEAD commit — hook cannot run")
        return

    short_sha = commit_sha[:7]

    # -------------------------------------------------------------------------
    # Step 3 — detect mode
    # -------------------------------------------------------------------------
    if full or not _has_existing_ground_truth(output_dir):
        _run_initial(repo_root, output_dir, short_sha, start, forced=full)
    else:
        _run_incremental(repo_root, output_dir, short_sha, start)


# ---------------------------------------------------------------------------
# Mode handlers
# ---------------------------------------------------------------------------

def _run_initial(
    repo_root: str,
    output_dir: str,
    short_sha: str,
    start: float,
    forced: bool = False,
) -> None:
    """Full collection — all source files in the repository."""
    _print(f"post-commit hook fired — {short_sha} (initial collection)")
    if forced:
        _print("--full flag set — running full collection")
    else:
        _print("no existing ground truth found — running full collection")

    # Count files before collecting so we can log the target before starting.
    try:
        source_paths = get_source_file_paths(repo_root)
    except Exception as exc:
        _print(f"[error] could not read file tree: {exc}")
        return

    _print(f"collecting ground truth for {len(source_paths)} source files")

    try:
        _ensure_output_dir(output_dir)
        summary = collect_local_full(repo_root=repo_root, output_dir=output_dir)
    except Exception as exc:
        _print(f"[error] collection failed: {exc}")
        return

    elapsed = time.monotonic() - start
    n = summary["files_collected"]
    errors = summary["files_failed"]
    suffix = f", {errors} failed" if errors else ""
    _print(f"done — {n} artifacts created ({elapsed:.1f}s{suffix})")


def _run_incremental(
    repo_root: str,
    output_dir: str,
    short_sha: str,
    start: float,
) -> None:
    """Incremental collection — only files changed in the most recent commit."""
    _print(f"post-commit hook fired — {short_sha}")

    # -------------------------------------------------------------------------
    # Get changed files; fall back to initial if this is the first commit.
    # -------------------------------------------------------------------------
    try:
        changed_files, is_first_commit = get_changed_files_local(repo_root)
    except Exception as exc:
        _print(f"[error] could not determine changed files: {exc}")
        return

    if is_first_commit:
        _print("first commit — no parent exists, falling back to full collection")
        _run_initial(repo_root, output_dir, short_sha, start)
        return

    # -------------------------------------------------------------------------
    # Filter to source files using the same rules as the collection pipeline.
    # -------------------------------------------------------------------------
    source_changed = [f for f in changed_files if is_collectable_source_file(f)]

    if not source_changed:
        _print("no source files changed — skipping collection")
        return

    _print(f"{len(source_changed)} source file(s) changed")
    _print(f"collecting ground truth for {', '.join(source_changed)}")

    # -------------------------------------------------------------------------
    # Run incremental collection.
    # -------------------------------------------------------------------------
    try:
        _ensure_output_dir(output_dir)
        summary = collect_local_incremental(
            file_paths=source_changed,
            repo_root=repo_root,
            output_dir=output_dir,
        )
    except Exception as exc:
        _print(f"[error] collection failed: {exc}")
        return

    elapsed = time.monotonic() - start
    n = summary["files_collected"]
    errors = summary["files_failed"]
    suffix = f", {errors} failed" if errors else ""
    _print(f"done — {n} artifact(s) updated ({elapsed:.1f}s{suffix})")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_existing_ground_truth(output_dir: str) -> bool:
    """Return True if any previous collection run exists in output_dir."""
    if not os.path.isdir(output_dir):
        return False
    for entry in os.listdir(output_dir):
        if os.path.isfile(os.path.join(output_dir, entry, "repo.json")):
            return True
    return False


def _ensure_output_dir(output_dir: str) -> None:
    """Create the output directory if it doesn't exist, or fail with a clear error."""
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"output directory is not writable: {output_dir} — {exc}") from exc


def _print(msg: str) -> None:
    """Print a structured hook log line to stdout."""
    print(f"[zabean] {msg}", flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m zabean.agent.hook",
        description="Run the Zabean post-commit hook manually.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Force a full collection regardless of existing ground truth.",
    )
    args = parser.parse_args()
    run_hook(full=args.full)


if __name__ == "__main__":
    _main()
