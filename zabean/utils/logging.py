"""
Structured logging for the Zabean pipeline.

All log lines follow the format:
    [zabean] [component] [context] message
    [zabean] [component] [context] [warning] message
    [zabean] [component] [context] [error] message

Warnings and errors include their level tag so `grep '[error]'` finds every failure
across a run instantly. Info lines omit the level tag to keep normal output readable.
"""

from __future__ import annotations

import logging
import sys


def _configure_root() -> None:
    """Attach a stdout handler to the root zabean logger exactly once."""
    root = logging.getLogger("zabean")
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)


_configure_root()


class ZabeanLogger:
    """
    Component-scoped logger that emits consistently structured lines.

    `component` is the module name (e.g. "collector", "github_client").
    `context` is an optional per-run identifier (e.g. "expressjs/express").
    """

    def __init__(self, component: str, context: str = "") -> None:
        self._component = component
        self._context = context
        self._logger = logging.getLogger(f"zabean.{component}")

    def _prefix(self) -> str:
        parts = ["[zabean]", f"[{self._component}]"]
        if self._context:
            parts.append(f"[{self._context}]")
        return " ".join(parts)

    def info(self, msg: str) -> None:
        self._logger.info(f"{self._prefix()} {msg}")

    def warning(self, msg: str) -> None:
        self._logger.warning(f"{self._prefix()} [warning] {msg}")

    def error(self, msg: str) -> None:
        self._logger.error(f"{self._prefix()} [error] {msg}")

    def debug(self, msg: str) -> None:
        self._logger.debug(f"{self._prefix()} {msg}")

    def with_context(self, context: str) -> ZabeanLogger:
        """Return a new logger with the same component but a different context."""
        return ZabeanLogger(self._component, context)


def get_logger(component: str, context: str = "") -> ZabeanLogger:
    """Get a structured logger for `component`, optionally scoped to `context`."""
    return ZabeanLogger(component, context)
