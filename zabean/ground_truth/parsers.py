"""
Deterministic static parsers.

All functions here are pure — same input always produces the same output.
No side effects, no API calls, no model involvement.

Import extraction uses regex for all supported languages. This approach is
intentionally conservative: it will miss some valid imports (multiline strings
that look like imports, heavily macro-generated code) and it will never
produce incorrect positive classifications — unknown imports go to the
"unresolved" bucket. The goal is a reliable lower bound, not completeness.

Supported languages for import extraction: python, javascript, typescript,
java, go, rust, ruby.
Unsupported languages return empty lists with a warning — they never raise.
"""

from __future__ import annotations

import os
import re


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".jsx": "javascript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".c": "c",
    ".h": "c",
    ".swift": "swift",
    ".kt": "kotlin",
    ".md": "markdown",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".sh": "shell",
    ".bash": "shell",
}


def detect_language(file_path: str) -> str:
    """Map a file path's extension to a language name.

    Returns "unknown" for unrecognized extensions.
    """
    ext = os.path.splitext(file_path)[1].lower()
    return EXTENSION_TO_LANGUAGE.get(ext, "unknown")


# ---------------------------------------------------------------------------
# Import extraction
# ---------------------------------------------------------------------------

# Python
_PY_IMPORT = re.compile(r"^import\s+([\w.]+)", re.MULTILINE)
_PY_FROM = re.compile(r"^from\s+([\.\w]+)\s+import", re.MULTILINE)

# JavaScript / TypeScript
# Matches: import ... from '...', import '...', export ... from '...'
_JS_STATIC = re.compile(
    r"""(?:import|export)\s+(?:(?:[\w\s{},*]+|['"][^'"]+['"])\s+from\s+)?['"]([^'"]+)['"]""",
    re.MULTILINE,
)
# Matches: require('...')
_JS_REQUIRE = re.compile(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""")
# Matches: import('...') — static string only; dynamic expressions go to unresolved
_JS_DYNAMIC = re.compile(r"""import\s*\(\s*['"]([^'"]+)['"]\s*\)""")

# Java — fully qualified class names
_JAVA_IMPORT = re.compile(r"^import\s+([\w.]+)\s*;", re.MULTILINE)

# Go — single-line and block imports
_GO_SINGLE = re.compile(r'^import\s+"([^"]+)"', re.MULTILINE)
_GO_BLOCK = re.compile(r"import\s*\(([^)]+)\)", re.DOTALL)
_GO_PATH = re.compile(r'"([^"]+)"')

# Rust
_RUST_USE = re.compile(r"^use\s+([\w:]+(?:::\w+)*)", re.MULTILINE)
_RUST_MOD = re.compile(r"^(?:pub\s+)?mod\s+(\w+)\s*;", re.MULTILINE)

# Ruby
_RUBY_REQUIRE = re.compile(r"""require(?:_relative)?\s+['"]([^'"]+)['"]""", re.MULTILINE)

_RUST_INTERNAL_PREFIXES = ("crate::", "self::", "super::")


def extract_imports(content: str, language: str, file_path: str) -> dict:
    """
    Extract import statements from source content.

    Returns:
        {
            "raw":        list of import strings exactly as they appear in source,
            "internal":   relative paths or module references within the repo,
            "external":   third-party packages and standard library,
            "unresolved": strings that could not be classified,
            "warnings":   non-fatal parse issues,
        }

    Supported: python, javascript, typescript, java, go, rust, ruby.
    Unsupported languages return empty lists with a warning and never raise.
    """
    result: dict = {"raw": [], "internal": [], "external": [], "unresolved": [], "warnings": []}

    try:
        extractors = {
            "python": _extract_python,
            "javascript": _extract_js,
            "typescript": _extract_js,
            "java": _extract_java,
            "go": _extract_go,
            "rust": _extract_rust,
            "ruby": _extract_ruby,
        }
        extractor = extractors.get(language)
        if extractor is None:
            result["warnings"].append(f"import extraction not supported for language: {language}")
        else:
            extractor(content, result)
    except Exception as exc:
        result["warnings"].append(f"import extraction failed with exception: {exc}")

    return result


def _extract_python(content: str, result: dict) -> None:
    for m in _PY_IMPORT.finditer(content):
        module = m.group(1)
        result["raw"].append(f"import {module}")
        # Plain `import` statements cannot be relative in Python — always external.
        result["external"].append(module)

    for m in _PY_FROM.finditer(content):
        module = m.group(1)
        result["raw"].append(f"from {module} import ...")
        if module.startswith("."):
            result["internal"].append(module)
        else:
            result["external"].append(module)


def _extract_js(content: str, result: dict) -> None:
    seen: set[str] = set()

    def _classify(path: str) -> None:
        if path in seen:
            return
        seen.add(path)
        result["raw"].append(path)
        if path.startswith(("./", "../", "/")):
            result["internal"].append(path)
        else:
            result["external"].append(path)

    for m in _JS_STATIC.finditer(content):
        _classify(m.group(1))
    for m in _JS_REQUIRE.finditer(content):
        _classify(m.group(1))
    for m in _JS_DYNAMIC.finditer(content):
        _classify(m.group(1))


def _extract_java(content: str, result: dict) -> None:
    for m in _JAVA_IMPORT.finditer(content):
        fqcn = m.group(1)
        result["raw"].append(f"import {fqcn}")
        result["external"].append(fqcn)


def _extract_go(content: str, result: dict) -> None:
    seen: set[str] = set()

    def _record(path: str) -> None:
        if path in seen:
            return
        seen.add(path)
        result["raw"].append(path)
        result["external"].append(path)

    for m in _GO_SINGLE.finditer(content):
        _record(m.group(1))
    for block in _GO_BLOCK.finditer(content):
        for pm in _GO_PATH.finditer(block.group(1)):
            _record(pm.group(1))


def _extract_rust(content: str, result: dict) -> None:
    for m in _RUST_USE.finditer(content):
        path = m.group(1)
        result["raw"].append(f"use {path}")
        if any(path.startswith(p) for p in _RUST_INTERNAL_PREFIXES):
            result["internal"].append(path)
        else:
            result["external"].append(path)

    for m in _RUST_MOD.finditer(content):
        name = m.group(1)
        result["raw"].append(f"mod {name}")
        # `mod foo;` declares a submodule — always internal.
        result["internal"].append(f"crate::{name}")


def _extract_ruby(content: str, result: dict) -> None:
    for m in _RUBY_REQUIRE.finditer(content):
        path = m.group(1)
        result["raw"].append(path)
        if path.startswith(("./", "../")):
            result["internal"].append(path)
        else:
            result["external"].append(path)


# ---------------------------------------------------------------------------
# Import resolution
# ---------------------------------------------------------------------------

# Extensions to probe when a JS/TS import omits the extension.
_JS_PROBE_SUFFIXES = [
    ".js", ".ts", ".jsx", ".tsx",
    "/index.js", "/index.ts", "/index.jsx", "/index.tsx",
]


def resolve_internal_imports(
    imports: list[str],
    file_path: str,
    file_tree: list[str],
) -> dict:
    """
    Resolve relative import strings to actual file paths in the repo.

    Uses directory-relative normalization. Tries common JS/TS extension
    suffixes when an import omits the extension. Does not resolve dynamic
    imports where the path is not a string literal.

    Returns:
        {
            "resolved":   {"./router": "lib/router.js", ...},
            "unresolved": ["./dynamic-" + name, ...],
        }
    """
    file_dir = os.path.dirname(file_path)
    tree_set = set(file_tree)

    resolved: dict[str, str] = {}
    unresolved: list[str] = []

    for imp in imports:
        # Dynamic expressions cannot be resolved statically.
        if any(c in imp for c in ("+", "${", "`")):
            unresolved.append(imp)
            continue

        candidate = _try_resolve(imp, file_dir, tree_set)
        if candidate is not None:
            resolved[imp] = candidate
        else:
            unresolved.append(imp)

    return {"resolved": resolved, "unresolved": unresolved}


def _try_resolve(import_path: str, file_dir: str, tree_set: set) -> str | None:
    """Attempt to map a single import string to a repo-relative file path."""
    if import_path.startswith(("./", "../")):
        raw = os.path.normpath(os.path.join(file_dir, import_path))
    elif import_path.startswith("/"):
        raw = import_path.lstrip("/")
    else:
        return None

    # Normalize to forward slashes (os.path.normpath uses OS separator).
    raw = raw.replace("\\", "/")

    if raw in tree_set:
        return raw

    for suffix in _JS_PROBE_SUFFIXES:
        candidate = raw + suffix
        if candidate in tree_set:
            return candidate

    return None


# ---------------------------------------------------------------------------
# README structure extraction
# ---------------------------------------------------------------------------

_MD_H1_H2 = re.compile(r"^#{1,2}\s+(.+)", re.MULTILINE)
_INSTALL_RE = re.compile(r"\binstall", re.IGNORECASE)
_API_RE = re.compile(r"\b(?:api|reference|docs?)\b", re.IGNORECASE)
_EXAMPLES_RE = re.compile(r"\b(?:example|usage|getting[ -]started)\b", re.IGNORECASE)


def extract_readme_structure(content: str) -> dict:
    """
    Extract structural information from README content deterministically.

    Detection is pattern-matching only — no inference. has_installation,
    has_api_docs, and has_examples are True only when their keyword appears
    in a h1 or h2 header.

    Returns:
        {
            "headers":          list of h1/h2 header text strings,
            "length_chars":     int,
            "has_installation": bool,
            "has_api_docs":     bool,
            "has_examples":     bool,
        }
    """
    headers = [m.group(1).strip() for m in _MD_H1_H2.finditer(content)]
    headers_joined = " ".join(headers)

    return {
        "headers": headers,
        "length_chars": len(content),
        "has_installation": bool(_INSTALL_RE.search(headers_joined)),
        "has_api_docs": bool(_API_RE.search(headers_joined)),
        "has_examples": bool(_EXAMPLES_RE.search(headers_joined)),
    }
