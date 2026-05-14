"""
Tests for zabean.ground_truth.parsers.

All parsers are pure functions — no API calls, no mocks required.
"""

import pytest

from zabean.ground_truth.parsers import (
    detect_language,
    extract_imports,
    extract_readme_structure,
    resolve_internal_imports,
)


# ---------------------------------------------------------------------------
# detect_language
# ---------------------------------------------------------------------------

class TestDetectLanguage:
    def test_python(self):
        assert detect_language("src/utils.py") == "python"

    def test_javascript(self):
        assert detect_language("lib/index.js") == "javascript"

    def test_typescript(self):
        assert detect_language("src/app.ts") == "typescript"

    def test_jsx(self):
        assert detect_language("components/Button.jsx") == "javascript"

    def test_tsx(self):
        assert detect_language("components/Button.tsx") == "typescript"

    def test_java(self):
        assert detect_language("src/main/App.java") == "java"

    def test_go(self):
        assert detect_language("cmd/server.go") == "go"

    def test_rust(self):
        assert detect_language("src/lib.rs") == "rust"

    def test_ruby(self):
        assert detect_language("lib/app.rb") == "ruby"

    def test_csharp(self):
        assert detect_language("src/Program.cs") == "csharp"

    def test_cpp(self):
        assert detect_language("src/main.cpp") == "cpp"

    def test_c_header(self):
        assert detect_language("include/utils.h") == "c"

    def test_swift(self):
        assert detect_language("Sources/App.swift") == "swift"

    def test_kotlin(self):
        assert detect_language("src/Main.kt") == "kotlin"

    def test_unknown_extension(self):
        assert detect_language("data/config.xyz") == "unknown"

    def test_no_extension(self):
        assert detect_language("Makefile") == "unknown"

    def test_case_insensitive(self):
        assert detect_language("src/App.PY") == "python"


# ---------------------------------------------------------------------------
# extract_imports — JavaScript / TypeScript
# ---------------------------------------------------------------------------

class TestExtractImportsJavaScript:
    def test_es_module_named_import(self):
        content = "import { Router } from 'express';"
        result = extract_imports(content, "javascript", "app.js")
        assert "express" in result["external"]
        assert "express" in result["raw"]

    def test_es_module_default_import(self):
        content = "import express from 'express';"
        result = extract_imports(content, "javascript", "app.js")
        assert "express" in result["external"]

    def test_es_module_side_effect_import(self):
        content = "import './polyfills';"
        result = extract_imports(content, "javascript", "src/app.js")
        assert "./polyfills" in result["internal"]

    def test_relative_import_classified_as_internal(self):
        content = "import handler from '../middleware/auth';"
        result = extract_imports(content, "javascript", "routes/users.js")
        assert "../middleware/auth" in result["internal"]
        assert "../middleware/auth" not in result["external"]

    def test_commonjs_require_external(self):
        content = "const path = require('path');"
        result = extract_imports(content, "javascript", "index.js")
        assert "path" in result["external"]

    def test_commonjs_require_internal(self):
        content = "const router = require('./router');"
        result = extract_imports(content, "javascript", "app.js")
        assert "./router" in result["internal"]

    def test_dynamic_import_static_string(self):
        content = "const mod = await import('./lazy');"
        result = extract_imports(content, "javascript", "app.js")
        assert "./lazy" in result["internal"]

    def test_typescript_treated_same_as_javascript(self):
        content = "import { useState } from 'react';\nimport type { FC } from 'react';"
        result = extract_imports(content, "typescript", "App.tsx")
        assert "react" in result["external"]

    def test_no_duplicate_imports(self):
        content = (
            "import foo from 'lodash';\n"
            "const _ = require('lodash');\n"
        )
        result = extract_imports(content, "javascript", "util.js")
        assert result["raw"].count("lodash") == 1

    def test_multiple_imports_mixed(self):
        content = (
            "import express from 'express';\n"
            "import { join } from 'path';\n"
            "import handler from './handler';\n"
            "import utils from '../utils';\n"
        )
        result = extract_imports(content, "javascript", "src/server.js")
        assert "express" in result["external"]
        assert "path" in result["external"]
        assert "./handler" in result["internal"]
        assert "../utils" in result["internal"]


# ---------------------------------------------------------------------------
# extract_imports — Python
# ---------------------------------------------------------------------------

class TestExtractImportsPython:
    def test_simple_import(self):
        content = "import os"
        result = extract_imports(content, "python", "main.py")
        assert "os" in result["external"]

    def test_from_import(self):
        content = "from collections import Counter"
        result = extract_imports(content, "python", "main.py")
        assert "collections" in result["external"]

    def test_relative_from_import(self):
        content = "from .utils import helper"
        result = extract_imports(content, "python", "app/main.py")
        assert ".utils" in result["internal"]
        assert ".utils" not in result["external"]

    def test_parent_relative_import(self):
        content = "from ..models import User"
        result = extract_imports(content, "python", "app/routes/users.py")
        assert "..models" in result["internal"]

    def test_stdlib_import(self):
        content = "import json\nimport datetime\nfrom pathlib import Path"
        result = extract_imports(content, "python", "utils.py")
        assert "json" in result["external"]
        assert "datetime" in result["external"]
        assert "pathlib" in result["external"]

    def test_mixed_internal_and_external(self):
        content = (
            "import requests\n"
            "from .client import APIClient\n"
            "from ..config import settings\n"
        )
        result = extract_imports(content, "python", "lib/api/handler.py")
        assert "requests" in result["external"]
        assert ".client" in result["internal"]
        assert "..config" in result["internal"]


# ---------------------------------------------------------------------------
# extract_imports — unsupported language
# ---------------------------------------------------------------------------

class TestExtractImportsUnsupported:
    def test_unsupported_language_returns_empty_with_warning(self):
        result = extract_imports("some content", "unknown", "file.xyz")
        assert result["raw"] == []
        assert result["internal"] == []
        assert result["external"] == []
        assert len(result["warnings"]) > 0

    def test_never_raises_on_unsupported(self):
        # Should not raise regardless of content
        result = extract_imports("<<<malformed>>>", "cobol", "file.cob")
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# resolve_internal_imports
# ---------------------------------------------------------------------------

class TestResolveInternalImports:
    FILE_TREE = [
        "lib/router.js",
        "lib/middleware/auth.js",
        "lib/middleware/logger.js",
        "lib/utils/index.js",
        "index.js",
        "app.js",
    ]

    def test_simple_relative_resolution(self):
        result = resolve_internal_imports(
            ["./router"],
            "lib/express.js",
            self.FILE_TREE,
        )
        assert result["resolved"]["./router"] == "lib/router.js"
        assert "./router" not in result["unresolved"]

    def test_resolution_from_nested_directory(self):
        result = resolve_internal_imports(
            ["../utils"],
            "lib/middleware/auth.js",
            self.FILE_TREE,
        )
        # lib/middleware/../utils -> lib/utils, probed as lib/utils/index.js
        assert result["resolved"]["../utils"] == "lib/utils/index.js"

    def test_index_file_resolution(self):
        result = resolve_internal_imports(
            ["./utils"],
            "lib/router.js",
            self.FILE_TREE,
        )
        assert result["resolved"]["./utils"] == "lib/utils/index.js"

    def test_exact_path_match(self):
        result = resolve_internal_imports(
            ["./middleware/auth"],
            "lib/router.js",
            self.FILE_TREE,
        )
        assert result["resolved"]["./middleware/auth"] == "lib/middleware/auth.js"

    def test_unresolvable_dynamic_import(self):
        result = resolve_internal_imports(
            ["./routes/" + "users"],  # dynamic concatenation pattern
            "app.js",
            self.FILE_TREE,
        )
        # The + operator is not in the string at runtime, but we simulate with a path
        # that doesn't exist in the tree.
        assert "./routes/users" in result["unresolved"]

    def test_dynamic_expression_never_resolved(self):
        result = resolve_internal_imports(
            ["'./dynamic-' + name"],
            "app.js",
            self.FILE_TREE,
        )
        assert "'./dynamic-' + name" in result["unresolved"]
        assert len(result["resolved"]) == 0

    def test_non_relative_import_not_resolved(self):
        result = resolve_internal_imports(
            ["express", "react"],
            "app.js",
            self.FILE_TREE,
        )
        # External packages should not be passed to this function, but if they
        # are they end up unresolved rather than incorrectly resolved.
        assert "express" in result["unresolved"]
        assert "react" in result["unresolved"]

    def test_empty_imports(self):
        result = resolve_internal_imports([], "app.js", self.FILE_TREE)
        assert result == {"resolved": {}, "unresolved": []}


# ---------------------------------------------------------------------------
# extract_readme_structure
# ---------------------------------------------------------------------------

class TestExtractReadmeStructure:
    def test_extracts_h1_headers(self):
        content = "# My Project\n\nSome text.\n"
        result = extract_readme_structure(content)
        assert "My Project" in result["headers"]

    def test_extracts_h2_headers(self):
        content = "# Project\n\n## Installation\n\n## Usage\n"
        result = extract_readme_structure(content)
        assert "Installation" in result["headers"]
        assert "Usage" in result["headers"]

    def test_ignores_h3_and_deeper(self):
        content = "# Top\n\n### Deep section\n"
        result = extract_readme_structure(content)
        assert "Top" in result["headers"]
        assert "Deep section" not in result["headers"]

    def test_has_installation_true(self):
        content = "# My App\n\n## Installation\n\nRun npm install.\n"
        result = extract_readme_structure(content)
        assert result["has_installation"] is True

    def test_has_installation_false(self):
        content = "# My App\n\n## Overview\n"
        result = extract_readme_structure(content)
        assert result["has_installation"] is False

    def test_has_api_docs_true(self):
        content = "# My App\n\n## API Reference\n"
        result = extract_readme_structure(content)
        assert result["has_api_docs"] is True

    def test_has_examples_true(self):
        content = "# My App\n\n## Usage Examples\n"
        result = extract_readme_structure(content)
        assert result["has_examples"] is True

    def test_length_chars(self):
        content = "# Hello\n"
        result = extract_readme_structure(content)
        assert result["length_chars"] == len(content)

    def test_empty_readme(self):
        result = extract_readme_structure("")
        assert result["headers"] == []
        assert result["length_chars"] == 0
        assert result["has_installation"] is False
        assert result["has_api_docs"] is False
        assert result["has_examples"] is False
