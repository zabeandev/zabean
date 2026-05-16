"""
zabean visualization server — serves ui.html and the /api/ground-truth endpoint.

Usage:
    python3 zabean/visualization/serve.py        (default port 7842)
    python3 zabean/visualization/serve.py 8000   (custom port)
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# This file lives at  zabean/visualization/serve.py
# Parent parent is the repo root.
HERE       = Path(__file__).resolve().parent   # zabean/visualization/
REPO_ROOT  = HERE.parent.parent                # repo root
OUTPUT_DIR = REPO_ROOT / "output"
UI_HTML    = HERE / "ui.html"

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7842


# ---------------------------------------------------------------------------
# Ground truth loading
# ---------------------------------------------------------------------------

def _latest_run_dir() -> Path | None:
    """Return the most-recently-modified output subdirectory that has a repo.json."""
    if not OUTPUT_DIR.exists():
        return None
    candidates = [
        p for p in OUTPUT_DIR.iterdir()
        if p.is_dir() and (p / "repo.json").exists()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _build_payload() -> dict | None:
    """Load the latest run and return a JSON-serialisable payload."""
    run_dir = _latest_run_dir()
    if run_dir is None:
        return None

    repo = json.loads((run_dir / "repo.json").read_text())

    files = []
    files_dir = run_dir / "files"
    if files_dir.exists():
        for fpath in sorted(files_dir.glob("*.json")):
            fgt = json.loads(fpath.read_text())
            # Strip raw_content — not needed in the UI; keeps the payload small.
            fgt.pop("raw_content", None)
            files.append(fgt)

    return {
        "repo":         repo,
        "files":        files,
        # RepoGroundTruth serialises to "fetched_at", not "collected_at"
        "collected_at": repo.get("fetched_at") or repo.get("collected_at"),
        "commit_sha":   repo.get("commit_sha"),
    }


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):

    # Silence the default access log — zabean prints its own startup line.
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path in ("/", "/ui.html"):
            self._serve_html()
        elif self.path == "/api/ground-truth":
            self._serve_api()
        else:
            self._respond(404, "text/plain", b"not found")

    # -------------------------------------------------------------------------

    def _serve_html(self):
        body = UI_HTML.read_bytes()
        self._respond(200, "text/html; charset=utf-8", body)

    def _serve_api(self):
        payload = _build_payload()
        if payload is None:
            body = json.dumps({"error": "no ground truth data found"}).encode()
            self._respond(404, "application/json", body)
        else:
            body = json.dumps(payload).encode()
            self._respond(200, "application/json", body)

    def _respond(self, status: int, content_type: str, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Allow the browser to reach the API even when opened as a file:// URL.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    server = HTTPServer(("localhost", PORT), Handler)
    print(f"[zabean] visualization server running at http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[zabean] server stopped")


if __name__ == "__main__":
    main()
