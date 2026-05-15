"""
Hook installer and uninstaller.

install()   — writes the Zabean post-commit hook into .git/hooks/
uninstall() — removes it cleanly, preserving any other hooks in the file

Entry points:
    python -m zabean.install
    python -m zabean.uninstall
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys


# The line written into the hook file. Used as a sentinel for clean removal.
_HOOK_LINE = f"{sys.executable} -m zabean.agent.hook\n"
_HOOK_SENTINEL = "# zabean hook"
_HOOK_BLOCK = f"{_HOOK_SENTINEL}\n{_HOOK_LINE}"

_HOOK_SCRIPT = f"""#!/bin/sh
{_HOOK_SENTINEL}
{_HOOK_LINE}"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def install() -> None:
    """
    Install the Zabean post-commit hook into the current repository.

    Must be run from inside a Git repository. If a post-commit hook already
    exists, asks whether to overwrite or append — never overwrites silently.
    """
    repo_root = _require_git_repo()
    hook_path = os.path.join(repo_root, ".git", "hooks", "post-commit")

    # -------------------------------------------------------------------------
    # Handle existing hook
    # -------------------------------------------------------------------------
    if os.path.exists(hook_path):
        with open(hook_path, encoding="utf-8") as f:
            existing = f.read()

        if _HOOK_SENTINEL in existing:
            print("[zabean] post-commit hook already contains Zabean — nothing to do")
            print(f"[zabean] hook is active at {hook_path}")
            return

        print(f"[zabean] a post-commit hook already exists at {hook_path}")
        print("[zabean] options: (o)verwrite  (a)ppend  (c)ancel")
        choice = input("[zabean] choice: ").strip().lower()

        if choice == "o":
            _write_hook(hook_path, _HOOK_SCRIPT)
            print("[zabean] existing hook overwritten")
        elif choice == "a":
            appended = existing.rstrip("\n") + "\n\n" + _HOOK_BLOCK
            _write_hook(hook_path, appended)
            print("[zabean] Zabean hook appended to existing hook")
        else:
            print("[zabean] installation cancelled")
            return
    else:
        _write_hook(hook_path, _HOOK_SCRIPT)

    # -------------------------------------------------------------------------
    # Confirm and guide next steps
    # -------------------------------------------------------------------------
    print(f"[zabean] installed — hook active at {hook_path}")
    print("[zabean] zabean will collect ground truth automatically on every commit")
    print("[zabean] run 'python -m zabean.agent.hook --full' to collect ground truth for the full repository now")


def uninstall() -> None:
    """
    Remove the Zabean hook from .git/hooks/post-commit.

    If the file contains only the Zabean hook, the file is deleted.
    If other content exists alongside it, only the Zabean lines are removed
    and the rest is preserved.
    """
    repo_root = _require_git_repo()
    hook_path = os.path.join(repo_root, ".git", "hooks", "post-commit")

    if not os.path.exists(hook_path):
        print("[zabean] no post-commit hook found — nothing to uninstall")
        return

    with open(hook_path, encoding="utf-8") as f:
        content = f.read()

    if _HOOK_SENTINEL not in content:
        print("[zabean] post-commit hook does not contain a Zabean entry — nothing to uninstall")
        return

    # Remove the Zabean block from the file.
    cleaned = _remove_hook_block(content)

    if not cleaned.strip() or cleaned.strip() == "#!/bin/sh":
        # Nothing left but the shebang — delete the file.
        os.remove(hook_path)
        print(f"[zabean] hook file removed: {hook_path}")
    else:
        _write_hook(hook_path, cleaned)
        print(f"[zabean] Zabean entry removed from {hook_path} — other hooks preserved")

    print("[zabean] uninstalled")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_git_repo() -> str:
    """
    Return the repo root path, or print an error and exit if not in a Git repo.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("[zabean] [error] not inside a git repository", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def _write_hook(hook_path: str, content: str) -> None:
    """Write hook content to disk and make it executable."""
    os.makedirs(os.path.dirname(hook_path), exist_ok=True)
    with open(hook_path, "w", encoding="utf-8") as f:
        f.write(content)
    # chmod +x
    current = os.stat(hook_path).st_mode
    os.chmod(hook_path, current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _remove_hook_block(content: str) -> str:
    """
    Remove the Zabean sentinel line and the hook invocation line from content.
    Strips any blank lines that result from the removal.
    """
    lines = content.splitlines(keepends=True)
    cleaned: list[str] = []
    skip_next = False

    for line in lines:
        if _HOOK_SENTINEL in line:
            skip_next = True
            continue
        if skip_next:
            skip_next = False
            continue
        cleaned.append(line)

    # Collapse multiple consecutive blank lines left by the removal.
    result: list[str] = []
    prev_blank = False
    for line in cleaned:
        is_blank = not line.strip()
        if is_blank and prev_blank:
            continue
        result.append(line)
        prev_blank = is_blank

    return "".join(result)
