# Zabean on Zabean

This is the output of running Zabean on its own repository — the self-instrumentation
that happens automatically on every commit via the installed post-commit hook.

The raw JSON artifacts live in `output/` (gitignored). This file is the human-readable
summary.

---

## Repo ground truth — zabeandev/zabean

```
commit:      7e68258a65e9b23e0854aebbc217855153f8fed0
branch:      main
collected:   2026-05-15T20:26:51 UTC
schema:      1.0.0
```

### Structure

| Field                | Value |
|----------------------|-------|
| Total files in tree  | 16    |
| Source files         | 9     |
| Language             | Python (9 files) |
| Max directory depth  | 2     |

```
zabean/              9 files
  ground_truth/      5 files
  utils/             3 files
```

### Signals

| Signal               | Value |
|----------------------|-------|
| Has README           | yes (3,482 chars) |
| Has package manifest | yes (pip) |
| Has test directory   | yes (`tests/`) |
| Entry point candidates | none |

README headers detected: `Zabean`, `Ground truth collection`, `How to run`,
`Output`, `Running tests`, `What comes next`

### Activity (last 30 days)

**Most active files** (by commit count):
1. `zabean/__init__.py`
2. `zabean/ground_truth/__init__.py`
3. `zabean/ground_truth/collector.py`
4. `zabean/ground_truth/github_client.py`
5. `zabean/ground_truth/models.py`

**Largest files** (by line count):
1. `zabean/ground_truth/collector.py`
2. `zabean/ground_truth/parsers.py`
3. `zabean/ground_truth/github_client.py`
4. `zabean/ground_truth/models.py`
5. `zabean/utils/validation.py`

### Collection metadata

```
files_collected:  9
files_skipped:    7  (4 non-source extension, 3 in tests/)
files_failed:     0
collection_errors: []
determined_by:   [commit_history, file_tree, readme]
```

---

## How this file is maintained

This summary is updated manually when the repo ground truth changes significantly.
The hook that updates the per-file JSON artifacts fires automatically — run
`python -m zabean.agent.hook --full` at any time to regenerate from scratch.
