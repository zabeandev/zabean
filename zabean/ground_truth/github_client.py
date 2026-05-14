"""
GitHub API abstraction layer.

This is the only module in Zabean that knows about the GitHub REST API.
Everything else receives data from the four public functions below.

Swapping GitHub for GitLab, a local Git repo, or a test mock requires changing
exactly this file — nothing else touches HTTP or authentication.

Authentication is via a Bearer token passed explicitly to every function.
Tokens are never read from the environment here; that is the caller's concern.
This keeps the client stateless and trivially testable.

Rate limiting: on HTTP 429 or a GitHub secondary rate limit (403 with
X-RateLimit-Remaining: 0), the client backs off and retries automatically.
It never raises on a rate limit — it waits and continues. Individual HTTP
errors that are not rate limits are returned as-is; the caller decides whether
they are fatal.
"""

from __future__ import annotations

import base64
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from zabean.utils.logging import get_logger

_log = get_logger("github_client")

_BASE_URL = "https://api.github.com"
_MAX_RETRIES = 5
_INITIAL_BACKOFF_SECONDS = 30


def fetch_repo_tree(
    owner: str,
    repo: str,
    token: str,
    branch: str = "main",
) -> list[dict]:
    """
    Fetch the complete recursive file tree for a branch.

    Returns a list of dicts, one per blob (file), each with:
        {"path": str, "sha": str, "size": int}

    Directory entries are excluded — only file blobs are returned.
    Handles pagination automatically via the recursive tree API.
    Logs a warning if the tree is truncated (>100,000 items).
    """
    url = f"{_BASE_URL}/repos/{owner}/{repo}/git/trees/{branch}"
    response = _get(url, token, params={"recursive": "1"})
    response.raise_for_status()

    data = response.json()
    if data.get("truncated"):
        _log.warning(
            f"tree response truncated for {owner}/{repo} — "
            "repository has more than 100,000 items; some files may be missing"
        )

    return [
        {
            "path": item["path"],
            "sha": item["sha"],
            "size": item.get("size", 0),
        }
        for item in data.get("tree", [])
        if item["type"] == "blob"
    ]


def fetch_file_content(
    owner: str,
    repo: str,
    path: str,
    token: str,
) -> tuple[str, str]:
    """
    Fetch decoded file content and its blob SHA.

    Returns (content, blob_sha).
    Returns ("", "") on any non-rate-limit error or decode failure — never
    raises on content issues so the collection pipeline can continue.
    """
    url = f"{_BASE_URL}/repos/{owner}/{repo}/contents/{path}"
    response = _get(url, token)

    if not response.ok:
        return ("", "")

    data = response.json()
    blob_sha = data.get("sha", "")
    content_b64 = data.get("content", "")

    if not content_b64:
        return ("", blob_sha)

    try:
        # GitHub encodes content as base64 with embedded newlines.
        raw_bytes = base64.b64decode(content_b64.replace("\n", ""))
        content = raw_bytes.decode("utf-8")
        return (content, blob_sha)
    except (UnicodeDecodeError, ValueError):
        # Binary file or unexpected encoding — treat as unreadable.
        return ("", blob_sha)


def fetch_file_commits(
    owner: str,
    repo: str,
    path: str,
    token: str,
    days: int = 90,
) -> list[dict]:
    """
    Fetch commit history for a single file, up to `days` days back.

    Returns a list of dicts, most-recent-first, each with:
        {"sha": str, "message": str, "author_name": str, "timestamp": str}

    `message` contains only the first line of the commit message.
    `timestamp` is an ISO 8601 string as returned by the GitHub API.

    Handles pagination automatically. Returns an empty list on any error so
    the collection pipeline can treat missing history as a soft failure.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    url = f"{_BASE_URL}/repos/{owner}/{repo}/commits"

    results: list[dict] = []
    page = 1

    while True:
        response = _get(url, token, params={
            "path": path,
            "since": since,
            "per_page": 100,
            "page": page,
        })

        if not response.ok:
            break

        commits = response.json()
        if not commits:
            break

        for commit in commits:
            results.append({
                "sha": commit["sha"],
                "message": commit["commit"]["message"].split("\n")[0],
                "author_name": commit["commit"]["author"]["name"],
                "timestamp": commit["commit"]["author"]["date"],
            })

        if len(commits) < 100:
            break
        page += 1

    return results


def fetch_latest_commit_sha(
    owner: str,
    repo: str,
    token: str,
    branch: str = "main",
) -> str:
    """
    Return the full SHA of the HEAD commit on `branch`.

    Raises on failure — the commit SHA is required for ground truth identity
    and there is no safe default.
    """
    url = f"{_BASE_URL}/repos/{owner}/{repo}/commits/{branch}"
    response = _get(url, token)
    response.raise_for_status()
    return response.json()["sha"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get(
    url: str,
    token: str,
    params: Optional[dict] = None,
) -> requests.Response:
    """
    Execute a GET request with automatic retry on rate limit responses.

    Handles both HTTP 429 (explicit rate limit) and GitHub's secondary rate
    limit (HTTP 403 with X-RateLimit-Remaining: 0). Backs off exponentially
    up to 5 minutes and retries up to _MAX_RETRIES times.
    """
    backoff = _INITIAL_BACKOFF_SECONDS

    for attempt in range(_MAX_RETRIES):
        response = requests.get(
            url,
            headers=_auth_headers(token),
            params=params,
            timeout=30,
        )

        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After", backoff))
            _log.warning(f"rate limit hit — backing off {wait}s (attempt {attempt + 1}/{_MAX_RETRIES})")
            time.sleep(wait)
            backoff = min(backoff * 2, 300)
            continue

        if response.status_code == 403:
            remaining = response.headers.get("X-RateLimit-Remaining", "1")
            if remaining == "0":
                reset_ts = int(response.headers.get("X-RateLimit-Reset", 0))
                wait = max(reset_ts - int(time.time()), 0) + 5
                _log.warning(f"rate limit hit — backing off {wait}s (attempt {attempt + 1}/{_MAX_RETRIES})")
                time.sleep(wait)
                continue

        return response

    raise RuntimeError(f"exceeded {_MAX_RETRIES} retries for {url}")


def _auth_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
