# Zabean

Zabean is a knowledge infrastructure layer for software engineering organizations. It instruments a codebase and produces structured, versioned, portable knowledge artifacts — ground truth packages that live independently of any existing tool and serve both human and machine consumers. The core principle is a clean separation between what can be known deterministically and what requires interpretation. Zabean builds the former first, completely, and honestly.

---

## Ground truth collection

This module implements the deterministic ground truth collector. It fetches everything that can be known about a repository from the GitHub API without any model involvement: file structure, file content, import graphs, commit history, contributor signals, entry points. Every run given the same repository and commit SHA produces identical output.

What it collects:
- **RepoGroundTruth** — one per repository: structure, language distribution, README signals, manifest detection, test directories, entry point candidates, and activity signals derived from commit frequency.
- **FileGroundTruth** — one per source file: full content, static import graph (raw, internal, external, and unresolved), commit history, contributor signals, and structural signals.

Why determinism matters: downstream components — the interpreter, the artifact generator — build on this data. If the ground truth is reproducible, their outputs are auditable. If the ground truth is honest about what it could not determine, failures are visible rather than silently wrong.

The `could_not_determine` field on every artifact is explicit: if a field defaulted because commit history was unavailable, or because an import could not be statically resolved, that is recorded. The artifact always knows the limits of its own knowledge.

---

## How to run

```bash
pip install -r requirements.txt

export GITHUB_TOKEN=your_token

python -m zabean.ground_truth.collector owner repo
```

Options:

```
--branch BRANCH       Branch to collect from (default: main)
--output-dir DIR      Output directory (default: output)
--max-files N         Limit collection to N files (useful for testing)
```

Example:

```bash
python -m zabean.ground_truth.collector expressjs express --branch master
```

---

## Output

Each collection run produces a self-contained directory under `output/`:

```
output/
  expressjs__express__master__c9ecf7b/
    repo.json                 ← RepoGroundTruth for the repository
    files/
      lib__express_js.json    ← FileGroundTruth for lib/express.js
      lib__router_js.json
      ...
    collection_manifest.json  ← start time, end time, files collected, errors
```

The manifest is written at the start of the run and updated at completion. If the pipeline crashes mid-run, the manifest records the partial state.

All JSON uses `indent=2` and ISO 8601 UTC timestamps.

---

## Running tests

```bash
pip install pytest
pytest tests/
```

Tests cover the static parsers (`detect_language`, `extract_imports`, `resolve_internal_imports`, `extract_readme_structure`) and model serialization round-trips. All tests run without network access or mocks — parsers are pure functions.

---

## What comes next

The ground truth feeds an interpretation pipeline that enriches the raw data with model-generated analysis and produces structured knowledge artifacts — architecture maps, dependency explanations, contributor context — consumable by both humans and downstream agents.
