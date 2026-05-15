"""
Zabean agent — stateless Git hook that instruments a codebase automatically.

The agent fires on post-commit, collects ground truth for changed files, and
exits. No persistent process, no daemon, no global state. Every run is
independent and produces deterministic output given the same inputs.
"""
# agent component
