"""
Ground truth validation.

Validation runs after collection and before saving. It distinguishes between
hard failures (data that would make the artifact useless) and warnings (data
that is incomplete but still publishable).

Both functions follow the same contract:
    - Return (is_valid, issues) — never raise.
    - Hard failures set is_valid=False.
    - Warnings are appended to issues but do not affect is_valid.
"""

from __future__ import annotations

from zabean.ground_truth.models import FileGroundTruth, RepoGroundTruth


def validate_file_ground_truth(fgt: FileGroundTruth) -> tuple[bool, list[str]]:
    """
    Validate a FileGroundTruth before saving.

    Hard failures (is_valid=False):
        - file_path is empty
        - raw_content is empty (could not fetch content)
        - language is "unknown" for a source file

    Warnings (is_valid remains True):
        - no commit history available
        - no imports detected (may be legitimate for some file types)
        - could_not_determine is non-empty
    """
    issues: list[str] = []
    valid = True

    if not fgt.file_path:
        issues.append("hard: file_path is empty")
        valid = False

    if not fgt.raw_content:
        issues.append("hard: raw_content is empty — file content could not be fetched")
        valid = False

    if fgt.language == "unknown":
        issues.append("hard: language is unknown — extension not recognized")
        valid = False

    if not fgt.most_recent_commit_sha:
        issues.append("warning: no commit history available")

    if (
        not fgt.imports_raw
        and fgt.language not in ("json", "yaml", "toml", "markdown", "shell")
    ):
        issues.append("warning: no imports detected")

    if fgt.could_not_determine:
        issues.append(
            f"warning: could_not_determine is populated — {len(fgt.could_not_determine)} field(s) defaulted"
        )

    return valid, issues


def validate_repo_ground_truth(rgt: RepoGroundTruth) -> tuple[bool, list[str]]:
    """
    Validate a RepoGroundTruth before saving.

    Hard failures (is_valid=False):
        - commit_sha is empty
        - source_file_count is zero

    Warnings (is_valid remains True):
        - no README found
        - no entry point candidates
        - skip rate exceeds 50% of total files (may indicate misconfigured filters)
    """
    issues: list[str] = []
    valid = True

    if not rgt.commit_sha:
        issues.append("hard: commit_sha is empty — cannot identify collection point")
        valid = False

    if rgt.source_file_count == 0:
        issues.append("hard: source_file_count is zero — no source files were collected")
        valid = False

    if not rgt.has_readme:
        issues.append("warning: no README found")

    if not rgt.entry_point_candidates:
        issues.append("warning: no entry point candidates found")

    if rgt.files_collected > 0:
        total = rgt.files_collected + rgt.files_skipped
        skip_rate = rgt.files_skipped / total if total > 0 else 0
        if skip_rate > 0.5:
            issues.append(
                f"warning: high skip rate ({skip_rate:.0%}) — "
                f"{rgt.files_skipped} of {total} files were skipped"
            )

    return valid, issues
