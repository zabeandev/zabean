"""
Agent-facing collection pipeline.

This module wraps the ground truth assembly logic for use by the hook agent.
It uses local_client.py for data fetching rather than the GitHub API, making
collection instant — no network round trips, no token required.

The assembly logic (building FileGroundTruth and RepoGroundTruth from raw
fetched data) is identical to the GitHub pipeline. Only the data source
differs.

Two public functions:
    collect_local_full        — collect all source files in the repo
    collect_local_incremental — collect a specific list of files
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

from zabean.ground_truth.local_client import (
    detect_repo_id_local,
    fetch_file_commits_local,
    fetch_file_content_local,
    fetch_latest_commit_sha_local,
    fetch_repo_tree_local,
    get_repo_branch_local,
)
from zabean.ground_truth.models import SCHEMA_VERSION, FileGroundTruth, RepoGroundTruth, _file_path_hash
from zabean.ground_truth.parsers import (
    detect_language,
    extract_imports,
    extract_readme_structure,
    resolve_internal_imports,
)
# Import shared assembly and utility functions from the GitHub pipeline.
# These are internal helpers but fully reusable — only the fetch layer differs.
from zabean.ground_truth.collector import (
    _assemble_file_ground_truth,
    _assemble_repo_ground_truth,
    _directory_component,
    _file_output_name,
    _skip_reason,
    _write_json,
    MANIFEST_FILENAMES,
    MAX_FILE_SIZE_BYTES,
    MAX_WORKERS,
    README_NAMES,
    SOURCE_EXTENSIONS,
    SKIP_PATH_COMPONENTS,
    TEST_DIRECTORY_NAMES,
)
from zabean.utils.logging import get_logger
from zabean.utils.validation import validate_file_ground_truth, validate_repo_ground_truth

_log = get_logger("agent.collector")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect_local_full(repo_root: str, output_dir: str) -> dict:
    """
    Collect ground truth for every source file in the repository.

    Reads from the local filesystem — no network required. Produces the same
    RepoGroundTruth and FileGroundTruth artifacts as the GitHub pipeline.

    Returns a summary dict:
        {
            "files_collected": int,
            "files_skipped":   int,
            "files_failed":    int,
            "run_dir":         str,
            "collection_errors": list[str],
        }
    """
    return _run_pipeline(
        repo_root=repo_root,
        output_dir=output_dir,
        target_paths=None,  # None = collect all source files
    )


def collect_local_incremental(
    file_paths: list[str],
    repo_root: str,
    output_dir: str,
) -> dict:
    """
    Collect ground truth for a specific list of files (changed in last commit).

    Fetches the full repo tree for import resolution context, but only reads
    content and commits for the files in file_paths.

    Returns the same summary dict as collect_local_full.
    """
    return _run_pipeline(
        repo_root=repo_root,
        output_dir=output_dir,
        target_paths=file_paths,
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _run_pipeline(
    repo_root: str,
    output_dir: str,
    target_paths: Optional[list[str]],
) -> dict:
    """
    Shared pipeline for full and incremental local collection.

    target_paths=None  → collect all source files (full mode).
    target_paths=[...] → collect only those files (incremental mode).
    """
    log = _log.with_context(os.path.basename(repo_root))
    fetched_at = datetime.now(timezone.utc)

    # -------------------------------------------------------------------------
    # Identity
    # -------------------------------------------------------------------------
    repo_id = detect_repo_id_local(repo_root)
    commit_sha = fetch_latest_commit_sha_local(repo_root)
    branch = get_repo_branch_local(repo_root)

    # -------------------------------------------------------------------------
    # Full tree — always fetched, needed for import resolution and repo signals
    # even in incremental mode.
    # -------------------------------------------------------------------------
    tree_entries = fetch_repo_tree_local(repo_root)
    all_paths = [e["path"] for e in tree_entries]

    # Detect test directories, README, and manifest from the full tree.
    test_directories = sorted({
        _directory_component(p, TEST_DIRECTORY_NAMES)
        for p in all_paths
        if _directory_component(p, TEST_DIRECTORY_NAMES)
    })
    has_test_directory = bool(test_directories)

    readme_path: Optional[str] = None
    manifest_type: Optional[str] = None
    for entry in tree_entries:
        name = os.path.basename(entry["path"])
        if name in README_NAMES and readme_path is None:
            readme_path = entry["path"]
        if name in MANIFEST_FILENAMES and manifest_type is None:
            manifest_type = MANIFEST_FILENAMES[name]

    # -------------------------------------------------------------------------
    # Determine which files to collect
    # -------------------------------------------------------------------------
    skip_reasons: dict[str, int] = {}
    source_entries: list[dict] = []

    if target_paths is None:
        # Full mode — filter the complete tree.
        for entry in tree_entries:
            skip = _skip_reason(entry)
            if skip:
                skip_reasons[skip] = skip_reasons.get(skip, 0) + 1
            else:
                source_entries.append(entry)
    else:
        # Incremental mode — use the provided paths, look up their tree metadata.
        tree_by_path = {e["path"]: e for e in tree_entries}
        for path in target_paths:
            if path not in tree_by_path:
                skip_reasons["not_in_tree"] = skip_reasons.get("not_in_tree", 0) + 1
                continue
            entry = tree_by_path[path]
            skip = _skip_reason(entry)
            if skip:
                skip_reasons[skip] = skip_reasons.get(skip, 0) + 1
            else:
                source_entries.append(entry)

    # -------------------------------------------------------------------------
    # Output directory
    # -------------------------------------------------------------------------
    run_dir = _local_run_dir_path(output_dir, repo_id, branch, commit_sha)
    files_dir = os.path.join(run_dir, "files")
    os.makedirs(files_dir, exist_ok=True)

    manifest = {
        "repo_id": repo_id,
        "commit_sha": commit_sha,
        "branch": branch,
        "started_at": fetched_at.isoformat(),
        "completed_at": None,
        "status": "running",
        "mode": "full" if target_paths is None else "incremental",
        "files_attempted": len(source_entries),
        "files_succeeded": 0,
        "files_failed": 0,
        "collection_errors": [],
    }
    _write_json(os.path.join(run_dir, "collection_manifest.json"), manifest)

    # -------------------------------------------------------------------------
    # Parallel fetch: content + commits for each target file
    # -------------------------------------------------------------------------
    total = len(source_entries)
    raw_file_data: dict[str, dict] = {}
    collection_errors: list[str] = []

    def _fetch_and_parse(idx: int, entry: dict) -> tuple[str, Optional[dict], Optional[str]]:
        path = entry["path"]
        error: Optional[str] = None
        data: Optional[dict] = None
        try:
            content, blob_sha = fetch_file_content_local(path, repo_root)
            commits = fetch_file_commits_local(path, repo_root, days=90)
            language = detect_language(path)
            imports = extract_imports(content, language, path) if content else {
                "raw": [], "internal": [], "external": [], "unresolved": [], "warnings": [],
            }
            line_count = content.count("\n") + 1 if content else 0
            data = {
                "path": path,
                "blob_sha": blob_sha or entry["sha"],
                "content": content,
                "size_bytes": entry.get("size", len(content.encode("utf-8"))),
                "language": language,
                "imports": imports,
                "commits": commits,
                "line_count": line_count,
            }
        except Exception as exc:
            error = f"{path}: {exc}"
            log.error(f"[{idx}/{total}] {path} — {exc}")
        return path, data, error

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_and_parse, i + 1, entry): entry
            for i, entry in enumerate(source_entries)
        }
        for future in as_completed(futures):
            path, data, error = future.result()
            if data:
                raw_file_data[path] = data
            if error:
                collection_errors.append(error)

    # -------------------------------------------------------------------------
    # Build dependency graph (serial — requires complete fetched set)
    # -------------------------------------------------------------------------
    source_paths = list(raw_file_data.keys())

    resolved_imports: dict[str, dict[str, str]] = {}
    for path, fd in raw_file_data.items():
        resolution = resolve_internal_imports(
            fd["imports"]["internal"],
            path,
            # Use ALL repo paths for resolution, not just target files.
            all_paths,
        )
        resolved_imports[path] = resolution["resolved"]
        fd["imports"]["unresolved_paths"] = resolution["unresolved"]

    dependents_map: dict[str, list[str]] = defaultdict(list)
    for path, resolved in resolved_imports.items():
        for target in resolved.values():
            dependents_map[target].append(path)
    for key in dependents_map:
        dependents_map[key].sort()

    # -------------------------------------------------------------------------
    # README content for repo-level signals
    # -------------------------------------------------------------------------
    readme_structure: dict = {
        "headers": [], "length_chars": 0,
        "has_installation": False, "has_api_docs": False, "has_examples": False,
    }
    if readme_path:
        readme_content, _ = fetch_file_content_local(readme_path, repo_root)
        if readme_content:
            readme_structure = extract_readme_structure(readme_content)

    # -------------------------------------------------------------------------
    # Assemble and save FileGroundTruth per file
    # -------------------------------------------------------------------------
    file_ground_truths: list[FileGroundTruth] = []
    files_succeeded = 0
    files_failed = len(collection_errors)

    for path in sorted(raw_file_data.keys()):
        fd = raw_file_data[path]
        fgt, error = _assemble_file_ground_truth(
            repo_id=repo_id,
            commit_sha=commit_sha,
            fetched_at=fetched_at,
            fd=fd,
            resolved=resolved_imports.get(path, {}),
            dependents=dependents_map.get(path, []),
        )
        if fgt is None:
            collection_errors.append(error or f"{path}: assembly failed")
            files_failed += 1
            continue

        is_valid, issues = validate_file_ground_truth(fgt)
        for issue in issues:
            if issue.startswith("hard:"):
                log.error(f"{path} — {issue}")

        if is_valid:
            out_path = os.path.join(files_dir, _file_output_name(path))
            _write_json(out_path, fgt.to_dict())
            file_ground_truths.append(fgt)
            files_succeeded += 1
        else:
            hard = [i for i in issues if i.startswith("hard:")]
            collection_errors.append(f"{path}: validation failed — {'; '.join(hard)}")
            files_failed += 1

    # -------------------------------------------------------------------------
    # Assemble and save RepoGroundTruth
    # -------------------------------------------------------------------------
    rgt = _assemble_repo_ground_truth(
        repo_id=repo_id,
        commit_sha=commit_sha,
        branch=branch,
        fetched_at=fetched_at,
        all_paths=all_paths,
        source_file_count=len(file_ground_truths),
        file_ground_truths=file_ground_truths,
        test_directories=test_directories,
        has_test_directory=has_test_directory,
        readme_structure=readme_structure,
        manifest_type=manifest_type,
        skip_reasons=skip_reasons,
        files_collected=files_succeeded,
        files_skipped=sum(skip_reasons.values()),
        collection_errors=collection_errors,
    )
    _write_json(os.path.join(run_dir, "repo.json"), rgt.to_dict())

    # -------------------------------------------------------------------------
    # Finalise manifest
    # -------------------------------------------------------------------------
    manifest.update({
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": "complete" if not collection_errors else "partial",
        "files_succeeded": files_succeeded,
        "files_failed": files_failed,
        "collection_errors": collection_errors,
    })
    _write_json(os.path.join(run_dir, "collection_manifest.json"), manifest)

    return {
        "files_collected": files_succeeded,
        "files_skipped": sum(skip_reasons.values()),
        "files_failed": files_failed,
        "collection_errors": collection_errors,
        "run_dir": run_dir,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _local_run_dir_path(
    output_dir: str,
    repo_id: str,
    branch: str,
    commit_sha: str,
) -> str:
    """Build the output directory path for a local collection run."""
    safe_id = repo_id.replace("/", "__")
    return os.path.join(output_dir, f"{safe_id}__{branch}__{commit_sha[:7]}")


def get_source_file_paths(repo_root: str) -> list[str]:
    """
    Return all collectable source file paths in the repo.

    Uses the same filtering logic as the full pipeline. Useful for determining
    the count before collection starts (for progress logging in the hook).
    """
    tree_entries = fetch_repo_tree_local(repo_root)
    return sorted(
        e["path"]
        for e in tree_entries
        if not _skip_reason(e)
    )


def is_collectable_source_file(path: str) -> bool:
    """
    Return True if a file path should be collected as a source file.

    Checks extension and path components against the same filter lists used
    by the collection pipeline.
    """
    import os as _os
    ext = _os.path.splitext(path)[1].lower()
    if ext not in SOURCE_EXTENSIONS:
        return False
    for part in path.split("/")[:-1]:
        if part in SKIP_PATH_COMPONENTS:
            return False
    return True
