"""
Main ground truth collection pipeline.

Two public functions:
    collect_repo_ground_truth — run the full pipeline for one repository.
    load_ground_truth         — reload a previously saved collection from disk.

Collection sequence
-------------------
1.  Fetch the file tree and the branch HEAD commit SHA.
2.  Detect test directories and package manifests from the tree.
3.  Filter the tree to source files; record skip reasons.
4.  Optionally limit to max_files for development/testing runs.
5.  Parallel-fetch file content and commit history (max 5 workers).
6.  Run static parsers on each file's content.
7.  Build the cross-file dependency graph — resolve internal imports and
    compute reverse dependents. Requires the complete fetched set.
8.  Assemble a FileGroundTruth per file.
9.  Assemble the RepoGroundTruth from aggregated file data.
10. Validate both levels; log any issues.
11. Persist to output_dir and update the collection manifest.
12. Return the RepoGroundTruth.

A single file failing never stops the pipeline. Errors are caught per-file,
logged, and recorded in collection_errors on the RepoGroundTruth.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from zabean.ground_truth.github_client import (
    fetch_file_commits,
    fetch_file_content,
    fetch_latest_commit_sha,
    fetch_repo_tree,
)
from zabean.ground_truth.models import (
    SCHEMA_VERSION,
    FileGroundTruth,
    RepoGroundTruth,
    _file_path_hash,
)
from zabean.ground_truth.parsers import (
    detect_language,
    extract_imports,
    extract_readme_structure,
    resolve_internal_imports,
)
from zabean.utils.logging import get_logger
from zabean.utils.validation import validate_file_ground_truth, validate_repo_ground_truth

_log = get_logger("collector")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOURCE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".java", ".go", ".rs", ".rb", ".cs",
    ".cpp", ".c", ".h", ".swift", ".kt",
})

# Any path component matching one of these causes the file to be skipped.
SKIP_PATH_COMPONENTS: frozenset[str] = frozenset({
    "node_modules", ".git", "dist", "build",
    "__pycache__", "vendor", "coverage",
    "test", "tests", "__tests__", "spec",
    "fixtures", ".cache", "tmp",
})

TEST_DIRECTORY_NAMES: frozenset[str] = frozenset({
    "test", "tests", "__tests__", "spec",
})

ENTRY_POINT_STEMS: frozenset[str] = frozenset({
    "index", "main", "app", "server",
})

# Maps manifest file names to their ecosystem label.
MANIFEST_FILENAMES: dict[str, str] = {
    "package.json": "npm",
    "requirements.txt": "pip",
    "Pipfile": "pip",
    "pyproject.toml": "pip",
    "pom.xml": "maven",
    "build.gradle": "gradle",
    "Cargo.toml": "cargo",
    "go.mod": "go",
    "Gemfile": "bundler",
    "composer.json": "composer",
}

README_NAMES: frozenset[str] = frozenset({
    "README.md", "README.rst", "README.txt", "README",
    "readme.md", "readme.rst",
})

MAX_FILE_SIZE_BYTES = 100 * 1024  # 100 KB — larger files are typically generated or minified
MAX_WORKERS = 5


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect_repo_ground_truth(
    owner: str,
    repo: str,
    token: str,
    branch: str = "main",
    max_files: Optional[int] = None,
    output_dir: str = "output",
) -> RepoGroundTruth:
    """
    Run the full ground truth collection pipeline for one repository.

    Progress is logged to stdout in real time, one line per file. Individual
    file failures are caught and recorded; they never stop the pipeline.

    Returns the assembled RepoGroundTruth and writes all artifacts to output_dir.
    """
    repo_id = f"{owner}/{repo}"
    log = _log.with_context(repo_id)
    fetched_at = datetime.now(timezone.utc)

    # -------------------------------------------------------------------------
    # Step 1 — fetch tree and HEAD commit SHA
    # -------------------------------------------------------------------------
    log.info("fetching commit SHA...")
    commit_sha = fetch_latest_commit_sha(owner, repo, token, branch)
    log.info(f"HEAD commit: {commit_sha[:12]}")

    log.info("fetching file tree...")
    tree_entries = fetch_repo_tree(owner, repo, token, branch)
    log.info(f"tree fetched — {len(tree_entries)} total blobs")

    all_paths = [e["path"] for e in tree_entries]

    # -------------------------------------------------------------------------
    # Step 2 — detect test directories, manifest, and README from the full tree
    #          (before filtering, so we capture these even if they're in skip dirs)
    # -------------------------------------------------------------------------
    test_directories = sorted({
        _directory_component(p, TEST_DIRECTORY_NAMES)
        for p in all_paths
        if _directory_component(p, TEST_DIRECTORY_NAMES)
    })
    has_test_directory = len(test_directories) > 0

    readme_path: Optional[str] = None
    manifest_type: Optional[str] = None
    for entry in tree_entries:
        name = os.path.basename(entry["path"])
        if name in README_NAMES and readme_path is None:
            readme_path = entry["path"]
        if name in MANIFEST_FILENAMES and manifest_type is None:
            manifest_type = MANIFEST_FILENAMES[name]

    # -------------------------------------------------------------------------
    # Step 3 — filter to collectable source files
    # -------------------------------------------------------------------------
    skip_reasons: dict[str, int] = {}
    source_entries: list[dict] = []

    for entry in tree_entries:
        skip = _skip_reason(entry)
        if skip:
            skip_reasons[skip] = skip_reasons.get(skip, 0) + 1
        else:
            source_entries.append(entry)

    log.info(
        f"filtered to {len(source_entries)} source files "
        f"({len(tree_entries) - len(source_entries)} skipped)"
    )

    # -------------------------------------------------------------------------
    # Step 4 — optionally cap for dev / test runs
    # -------------------------------------------------------------------------
    if max_files is not None and len(source_entries) > max_files:
        log.info(f"max_files={max_files} — truncating collection")
        source_entries = source_entries[:max_files]

    # -------------------------------------------------------------------------
    # Initialise output directory and manifest
    # -------------------------------------------------------------------------
    run_dir = _run_dir_path(output_dir, owner, repo, branch, commit_sha)
    files_dir = os.path.join(run_dir, "files")
    os.makedirs(files_dir, exist_ok=True)

    manifest = {
        "repo_id": repo_id,
        "commit_sha": commit_sha,
        "branch": branch,
        "started_at": fetched_at.isoformat(),
        "completed_at": None,
        "status": "running",
        "files_attempted": len(source_entries),
        "files_succeeded": 0,
        "files_failed": 0,
        "collection_errors": [],
    }
    _write_json(os.path.join(run_dir, "collection_manifest.json"), manifest)

    # -------------------------------------------------------------------------
    # Steps 5–6 — parallel fetch content + commits, then parse
    # -------------------------------------------------------------------------
    total = len(source_entries)
    raw_file_data: dict[str, dict] = {}  # path -> {content, blob_sha, commits, imports, language}
    collection_errors: list[str] = []

    def _fetch_and_parse(idx: int, entry: dict) -> tuple[str, Optional[dict], Optional[str]]:
        path = entry["path"]
        log.info(f"[{idx}/{total}] {path} — fetching")
        error: Optional[str] = None
        data: Optional[dict] = None

        try:
            content, blob_sha = fetch_file_content(owner, repo, path, token)
            commits = fetch_file_commits(owner, repo, path, token, days=90)
            language = detect_language(path)
            imports = extract_imports(content, language, path) if content else {
                "raw": [], "internal": [], "external": [], "unresolved": [], "warnings": [],
            }
            line_count = content.count("\n") + 1 if content else 0
            log.info(
                f"[{idx}/{total}] {path} — done "
                f"({line_count} lines, {len(commits)} commits)"
            )
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
    # Step 7 — build cross-file dependency graph
    # Requires the complete fetched set — runs after all parallel work is done.
    # -------------------------------------------------------------------------
    source_paths = list(raw_file_data.keys())

    # Resolve each file's internal imports to actual repo paths.
    resolved_imports: dict[str, dict[str, str]] = {}
    for path, fd in raw_file_data.items():
        resolution = resolve_internal_imports(
            fd["imports"]["internal"],
            path,
            source_paths,
        )
        resolved_imports[path] = resolution["resolved"]
        fd["imports"]["unresolved_paths"] = resolution["unresolved"]

    # Build reverse map: target_path -> list of files that import it.
    dependents_map: dict[str, list[str]] = defaultdict(list)
    for path, resolved in resolved_imports.items():
        for target in resolved.values():
            dependents_map[target].append(path)

    # Sort dependents lists for deterministic output.
    for key in dependents_map:
        dependents_map[key].sort()

    # -------------------------------------------------------------------------
    # Fetch README content for repo-level signals
    # -------------------------------------------------------------------------
    readme_content = ""
    readme_structure: dict = {"headers": [], "length_chars": 0, "has_installation": False,
                              "has_api_docs": False, "has_examples": False}
    if readme_path:
        readme_content, _ = fetch_file_content(owner, repo, readme_path, token)
        if readme_content:
            readme_structure = extract_readme_structure(readme_content)

    # -------------------------------------------------------------------------
    # Step 8 — assemble FileGroundTruth per file
    # -------------------------------------------------------------------------
    now = datetime.now(timezone.utc)
    file_ground_truths: list[FileGroundTruth] = []
    files_succeeded = 0
    files_failed = len(collection_errors)

    for path in sorted(raw_file_data.keys()):  # sorted for determinism
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
                log.error(f"{path} validation — {issue}")
            else:
                log.debug(f"{path} validation — {issue}")

        if is_valid:
            out_path = os.path.join(files_dir, _file_output_name(path))
            _write_json(out_path, fgt.to_dict())
            file_ground_truths.append(fgt)
            files_succeeded += 1
        else:
            hard_issues = [i for i in issues if i.startswith("hard:")]
            collection_errors.append(f"{path}: validation failed — {'; '.join(hard_issues)}")
            files_failed += 1

    # -------------------------------------------------------------------------
    # Step 9 — assemble RepoGroundTruth
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

    # -------------------------------------------------------------------------
    # Step 10 — validate and save RepoGroundTruth
    # -------------------------------------------------------------------------
    is_valid, issues = validate_repo_ground_truth(rgt)
    for issue in issues:
        if issue.startswith("hard:"):
            log.error(f"repo validation — {issue}")
        else:
            log.warning(f"repo validation — {issue}")

    _write_json(os.path.join(run_dir, "repo.json"), rgt.to_dict())

    # -------------------------------------------------------------------------
    # Step 11 — finalise manifest
    # -------------------------------------------------------------------------
    manifest.update({
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": "complete" if not collection_errors else "partial",
        "files_succeeded": files_succeeded,
        "files_failed": files_failed,
        "collection_errors": collection_errors,
    })
    _write_json(os.path.join(run_dir, "collection_manifest.json"), manifest)

    log.info(
        f"collection complete — {files_succeeded}/{total} files, "
        f"{files_failed} failed — output: {run_dir}"
    )
    return rgt


def load_ground_truth(
    output_dir: str,
    repo_id: str,
    commit_sha: str,
) -> tuple[RepoGroundTruth, list[FileGroundTruth]]:
    """
    Load a previously saved ground truth collection from output_dir.

    Searches for a run directory matching repo_id and commit_sha (prefix match
    on the SHA is accepted). Returns (RepoGroundTruth, list[FileGroundTruth]).
    Raises FileNotFoundError if no matching run is found.
    """
    owner_repo = repo_id.replace("/", "__")
    sha_prefix = commit_sha[:7]

    run_dir: Optional[str] = None
    for entry in os.listdir(output_dir):
        if owner_repo in entry and sha_prefix in entry:
            run_dir = os.path.join(output_dir, entry)
            break

    if run_dir is None:
        raise FileNotFoundError(
            f"no ground truth found for {repo_id} @ {commit_sha[:7]} in {output_dir}"
        )

    with open(os.path.join(run_dir, "repo.json"), encoding="utf-8") as f:
        rgt = RepoGroundTruth.from_dict(json.load(f))

    file_ground_truths: list[FileGroundTruth] = []
    files_dir = os.path.join(run_dir, "files")
    if os.path.isdir(files_dir):
        for name in sorted(os.listdir(files_dir)):
            if name.endswith(".json"):
                with open(os.path.join(files_dir, name), encoding="utf-8") as f:
                    file_ground_truths.append(FileGroundTruth.from_dict(json.load(f)))

    return rgt, file_ground_truths


# ---------------------------------------------------------------------------
# Assembly helpers
# ---------------------------------------------------------------------------

def _assemble_file_ground_truth(
    repo_id: str,
    commit_sha: str,
    fetched_at: datetime,
    fd: dict,
    resolved: dict[str, str],
    dependents: list[str],
) -> tuple[Optional[FileGroundTruth], Optional[str]]:
    """Build a FileGroundTruth from raw fetched data. Returns (fgt, error)."""
    try:
        path = fd["path"]
        content = fd["content"]
        commits = fd["commits"]
        language = fd["language"]
        imports = fd["imports"]
        could_not_determine: list[str] = []
        determined_by: list[str] = ["file_tree"]

        if content:
            determined_by.append("file_content")

        # Commit signals
        most_recent_commit_sha = ""
        most_recent_commit_timestamp = None
        most_recent_commit_author = ""
        most_recent_commit_message = ""
        commit_frequency_30d = 0
        commit_frequency_90d = len(commits)
        days_since_last_change = -1
        top_contributors: list[str] = []

        if commits:
            determined_by.append("commit_history")
            latest = commits[0]
            most_recent_commit_sha = latest["sha"]
            most_recent_commit_author = latest["author_name"]
            most_recent_commit_message = latest["message"]

            ts_str = latest["timestamp"]
            most_recent_commit_timestamp = datetime.fromisoformat(
                ts_str.replace("Z", "+00:00")
            )
            delta = datetime.now(timezone.utc) - most_recent_commit_timestamp
            days_since_last_change = delta.days

            cutoff_30d = datetime.now(timezone.utc) - timedelta(days=30)
            commit_frequency_30d = sum(
                1 for c in commits
                if datetime.fromisoformat(c["timestamp"].replace("Z", "+00:00")) >= cutoff_30d
            )

            author_counts: Counter[str] = Counter(c["author_name"] for c in commits)
            top_contributors = [
                author for author, _ in author_counts.most_common(3)
            ]
        else:
            could_not_determine.extend([
                "most_recent_commit_sha",
                "most_recent_commit_timestamp",
                "most_recent_commit_author",
                "most_recent_commit_message",
                "days_since_last_change",
                "top_contributors",
            ])

        # Import signals
        unresolved_paths = imports.get("unresolved_paths", [])
        imports_internal_resolved = sorted(resolved.values())
        imports_unresolved = imports.get("unresolved", []) + unresolved_paths

        if imports.get("raw"):
            determined_by.append("import_parse")

        if imports.get("warnings"):
            pass  # surfaced in parse_warnings

        # Structural signals
        depth = path.count("/")
        stem = os.path.splitext(os.path.basename(path))[0].lower()
        is_entry_point = stem in ENTRY_POINT_STEMS and depth <= 1
        is_in_test = any(
            part in TEST_DIRECTORY_NAMES for part in path.split("/")[:-1]
        )

        fgt = FileGroundTruth(
            repo_id=repo_id,
            commit_sha=commit_sha,
            file_path=path,
            blob_sha=fd["blob_sha"],
            file_path_hash=_file_path_hash(path),
            schema_version=SCHEMA_VERSION,
            fetched_at=fetched_at,
            language=language,
            raw_content=content,
            line_count=fd["line_count"],
            char_count=len(content),
            size_bytes=fd["size_bytes"],
            imports_raw=imports.get("raw", []),
            imports_internal=imports_internal_resolved,
            imports_external=sorted(set(imports.get("external", []))),
            imports_unresolved=imports_unresolved,
            dependents=dependents,
            most_recent_commit_sha=most_recent_commit_sha,
            most_recent_commit_timestamp=most_recent_commit_timestamp,
            most_recent_commit_author=most_recent_commit_author,
            most_recent_commit_message=most_recent_commit_message,
            commit_frequency_30d=commit_frequency_30d,
            commit_frequency_90d=commit_frequency_90d,
            days_since_last_change=days_since_last_change,
            top_contributors=top_contributors,
            is_entry_point_candidate=is_entry_point,
            is_in_test_directory=is_in_test,
            relative_depth=depth,
            determined_by=sorted(set(determined_by)),
            parse_warnings=imports.get("warnings", []),
            could_not_determine=could_not_determine,
        )
        return fgt, None

    except Exception as exc:
        return None, str(exc)


def _assemble_repo_ground_truth(
    repo_id: str,
    commit_sha: str,
    branch: str,
    fetched_at: datetime,
    all_paths: list[str],
    source_file_count: int,
    file_ground_truths: list[FileGroundTruth],
    test_directories: list[str],
    has_test_directory: bool,
    readme_structure: dict,
    manifest_type: Optional[str],
    skip_reasons: dict,
    files_collected: int,
    files_skipped: int,
    collection_errors: list[str],
) -> RepoGroundTruth:
    # Language distribution
    lang_dist: Counter[str] = Counter(f.language for f in file_ground_truths)

    # Directory structure — count source files per directory prefix
    dir_counts: Counter[str] = Counter()
    for f in file_ground_truths:
        parts = f.file_path.split("/")
        for depth in range(1, len(parts)):
            prefix = "/".join(parts[:depth]) + "/"
            dir_counts[prefix] += 1
    directory_structure = dict(sorted(dir_counts.items()))

    max_depth = max((f.relative_depth for f in file_ground_truths), default=0)

    # README signals
    has_readme = readme_structure["length_chars"] > 0

    # Entry point candidates
    entry_point_candidates = sorted(
        f.file_path for f in file_ground_truths if f.is_entry_point_candidate
    )

    # Activity signals — sort by count descending, then path ascending for ties
    most_active = sorted(
        file_ground_truths,
        key=lambda f: (-f.commit_frequency_30d, f.file_path),
    )[:5]
    largest = sorted(
        file_ground_truths,
        key=lambda f: (-f.line_count, f.file_path),
    )[:5]

    commit_frequency_30d = sum(f.commit_frequency_30d for f in file_ground_truths)

    determined_by = ["file_tree"]
    if any(f.commit_frequency_30d > 0 for f in file_ground_truths):
        determined_by.append("commit_history")
    if has_readme:
        determined_by.append("readme")

    return RepoGroundTruth(
        repo_id=repo_id,
        commit_sha=commit_sha,
        branch=branch,
        fetched_at=fetched_at,
        schema_version=SCHEMA_VERSION,
        total_file_count=len(all_paths),
        source_file_count=source_file_count,
        language_distribution=dict(lang_dist),
        directory_structure=directory_structure,
        max_directory_depth=max_depth,
        has_readme=has_readme,
        readme_length_chars=readme_structure["length_chars"],
        readme_headers=readme_structure["headers"],
        has_package_manifest=manifest_type is not None,
        manifest_type=manifest_type,
        has_test_directory=has_test_directory,
        test_directories=test_directories,
        entry_point_candidates=entry_point_candidates,
        most_active_files=[f.file_path for f in most_active],
        largest_files=[f.file_path for f in largest],
        commit_frequency_30d=commit_frequency_30d,
        files_collected=files_collected,
        files_skipped=files_skipped,
        skip_reasons=skip_reasons,
        collection_errors=collection_errors,
        determined_by=sorted(set(determined_by)),
    )


# ---------------------------------------------------------------------------
# File filtering
# ---------------------------------------------------------------------------

def _skip_reason(entry: dict) -> Optional[str]:
    """Return a skip reason string if this entry should be excluded, else None."""
    path = entry["path"]
    ext = os.path.splitext(path)[1].lower()

    if ext not in SOURCE_EXTENSIONS:
        return "non_source_extension"

    parts = path.split("/")
    for part in parts[:-1]:  # directory components only
        if part in SKIP_PATH_COMPONENTS:
            return f"path_component:{part}"

    if entry.get("size", 0) > MAX_FILE_SIZE_BYTES:
        return "too_large"

    return None


def _directory_component(path: str, names: frozenset) -> Optional[str]:
    """Return the first directory component of path that is in names, or None."""
    parts = path.split("/")
    for i, part in enumerate(parts[:-1]):
        if part in names:
            return "/".join(parts[: i + 1]) + "/"
    return None


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _run_dir_path(output_dir: str, owner: str, repo: str, branch: str, commit_sha: str) -> str:
    name = f"{owner}__{repo}__{branch}__{commit_sha[:7]}"
    return os.path.join(output_dir, name)


def _file_output_name(file_path: str) -> str:
    """Convert a repo-relative file path to a safe output filename."""
    sanitised = file_path.replace("/", "__").replace(".", "_")
    return f"{sanitised}.json"


def _write_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m zabean.ground_truth.collector",
        description="Collect deterministic ground truth for a GitHub repository.",
    )
    parser.add_argument("owner", help="GitHub organisation or user name")
    parser.add_argument("repo", help="Repository name")
    parser.add_argument("--branch", default="main", help="Branch to collect from (default: main)")
    parser.add_argument("--output-dir", default="output", help="Output directory (default: output)")
    parser.add_argument("--max-files", type=int, default=None, help="Limit collection to N files")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("[zabean] [collector] [error] GITHUB_TOKEN environment variable is not set", file=sys.stderr)
        sys.exit(1)

    collect_repo_ground_truth(
        owner=args.owner,
        repo=args.repo,
        token=token,
        branch=args.branch,
        max_files=args.max_files,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    _main()
