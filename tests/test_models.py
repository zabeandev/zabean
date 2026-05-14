"""
Tests for zabean.ground_truth.models.

Covers serialization round-trips, the file_path_hash helper, and field
defaults to ensure the data contracts are stable.
"""

from datetime import datetime, timezone

import pytest

from zabean.ground_truth.models import (
    SCHEMA_VERSION,
    FileGroundTruth,
    RepoGroundTruth,
    _file_path_hash,
)


# ---------------------------------------------------------------------------
# _file_path_hash
# ---------------------------------------------------------------------------

class TestFilePathHash:
    def test_returns_12_hex_characters(self):
        h = _file_path_hash("lib/router.js")
        assert len(h) == 12
        assert all(c in "0123456789abcdef" for c in h)

    def test_deterministic(self):
        assert _file_path_hash("lib/router.js") == _file_path_hash("lib/router.js")

    def test_different_paths_produce_different_hashes(self):
        assert _file_path_hash("lib/router.js") != _file_path_hash("lib/middleware.js")

    def test_empty_string(self):
        h = _file_path_hash("")
        assert len(h) == 12


# ---------------------------------------------------------------------------
# FileGroundTruth — construction and round-trip
# ---------------------------------------------------------------------------

def _make_file_gt(**overrides) -> FileGroundTruth:
    now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    defaults = dict(
        repo_id="owner/repo",
        commit_sha="a" * 40,
        file_path="lib/app.js",
        blob_sha="b" * 40,
        file_path_hash=_file_path_hash("lib/app.js"),
        schema_version=SCHEMA_VERSION,
        fetched_at=now,
        language="javascript",
        raw_content="const x = 1;\n",
        line_count=1,
        char_count=14,
        size_bytes=14,
        imports_raw=["express"],
        imports_internal=[],
        imports_external=["express"],
        imports_unresolved=[],
        dependents=[],
        most_recent_commit_sha="c" * 40,
        most_recent_commit_timestamp=now,
        most_recent_commit_author="Alice",
        most_recent_commit_message="initial commit",
        commit_frequency_30d=3,
        commit_frequency_90d=7,
        days_since_last_change=5,
        top_contributors=["Alice", "Bob"],
        is_entry_point_candidate=False,
        is_in_test_directory=False,
        relative_depth=1,
        determined_by=["file_content", "commit_history"],
        parse_warnings=[],
        could_not_determine=[],
    )
    defaults.update(overrides)
    return FileGroundTruth(**defaults)


class TestFileGroundTruthSerialization:
    def test_to_dict_returns_dict(self):
        fgt = _make_file_gt()
        d = fgt.to_dict()
        assert isinstance(d, dict)

    def test_fetched_at_serialized_as_isoformat(self):
        now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        fgt = _make_file_gt(fetched_at=now)
        d = fgt.to_dict()
        assert d["fetched_at"] == now.isoformat()

    def test_commit_timestamp_serialized_as_isoformat(self):
        now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        fgt = _make_file_gt(most_recent_commit_timestamp=now)
        d = fgt.to_dict()
        assert d["most_recent_commit_timestamp"] == now.isoformat()

    def test_commit_timestamp_none_serialized_as_none(self):
        fgt = _make_file_gt(most_recent_commit_timestamp=None)
        d = fgt.to_dict()
        assert d["most_recent_commit_timestamp"] is None

    def test_round_trip(self):
        original = _make_file_gt()
        restored = FileGroundTruth.from_dict(original.to_dict())
        assert restored.repo_id == original.repo_id
        assert restored.file_path == original.file_path
        assert restored.fetched_at == original.fetched_at
        assert restored.most_recent_commit_timestamp == original.most_recent_commit_timestamp

    def test_round_trip_with_none_timestamp(self):
        original = _make_file_gt(
            most_recent_commit_timestamp=None,
            could_not_determine=["most_recent_commit_timestamp"],
        )
        restored = FileGroundTruth.from_dict(original.to_dict())
        assert restored.most_recent_commit_timestamp is None
        assert "most_recent_commit_timestamp" in restored.could_not_determine

    def test_all_list_fields_preserved(self):
        fgt = _make_file_gt(
            imports_raw=["express", "path"],
            imports_external=["express", "path"],
            dependents=["src/server.js"],
            top_contributors=["Alice"],
            parse_warnings=["some warning"],
            could_not_determine=["days_since_last_change"],
        )
        restored = FileGroundTruth.from_dict(fgt.to_dict())
        assert restored.imports_raw == ["express", "path"]
        assert restored.dependents == ["src/server.js"]
        assert restored.parse_warnings == ["some warning"]
        assert "days_since_last_change" in restored.could_not_determine

    def test_schema_version_preserved(self):
        fgt = _make_file_gt()
        assert fgt.to_dict()["schema_version"] == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# RepoGroundTruth — construction and round-trip
# ---------------------------------------------------------------------------

def _make_repo_gt(**overrides) -> RepoGroundTruth:
    now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    defaults = dict(
        repo_id="owner/repo",
        commit_sha="a" * 40,
        branch="main",
        fetched_at=now,
        schema_version=SCHEMA_VERSION,
        total_file_count=50,
        source_file_count=23,
        language_distribution={"javascript": 21, "python": 2},
        directory_structure={"lib/": 8, "src/": 15},
        max_directory_depth=3,
        has_readme=True,
        readme_length_chars=1024,
        readme_headers=["My Project", "Installation", "Usage"],
        has_package_manifest=True,
        manifest_type="npm",
        has_test_directory=False,
        test_directories=[],
        entry_point_candidates=["index.js"],
        most_active_files=["lib/router.js", "src/app.js"],
        largest_files=["src/app.js"],
        commit_frequency_30d=42,
        files_collected=23,
        files_skipped=27,
        skip_reasons={"non_source_extension": 20, "path_component:node_modules": 7},
        collection_errors=[],
        determined_by=["file_tree", "commit_history", "readme"],
    )
    defaults.update(overrides)
    return RepoGroundTruth(**defaults)


class TestRepoGroundTruthSerialization:
    def test_to_dict_returns_dict(self):
        rgt = _make_repo_gt()
        d = rgt.to_dict()
        assert isinstance(d, dict)

    def test_fetched_at_serialized_as_isoformat(self):
        now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        rgt = _make_repo_gt(fetched_at=now)
        d = rgt.to_dict()
        assert d["fetched_at"] == now.isoformat()

    def test_round_trip(self):
        original = _make_repo_gt()
        restored = RepoGroundTruth.from_dict(original.to_dict())
        assert restored.repo_id == original.repo_id
        assert restored.commit_sha == original.commit_sha
        assert restored.fetched_at == original.fetched_at
        assert restored.language_distribution == original.language_distribution

    def test_manifest_type_none_round_trip(self):
        original = _make_repo_gt(has_package_manifest=False, manifest_type=None)
        restored = RepoGroundTruth.from_dict(original.to_dict())
        assert restored.manifest_type is None

    def test_nested_dicts_preserved(self):
        rgt = _make_repo_gt()
        restored = RepoGroundTruth.from_dict(rgt.to_dict())
        assert restored.directory_structure == rgt.directory_structure
        assert restored.skip_reasons == rgt.skip_reasons

    def test_schema_version_preserved(self):
        rgt = _make_repo_gt()
        assert rgt.to_dict()["schema_version"] == SCHEMA_VERSION
