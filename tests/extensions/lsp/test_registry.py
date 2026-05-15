import os
import tempfile
from pathlib import Path

from reuleauxcoder.extensions.lsp.registry import (
    LanguageId,
    _EXT_TO_LANGUAGE,
    _ROOT_MARKERS,
    detect_language,
    get_language_id_string,
    get_root_markers,
    get_server_command,
    iter_supported_extensions,
    iter_supported_languages,
    resolve_workspace_root,
)


class TestLanguageDetection:
    def test_python(self) -> None:
        assert detect_language("src/main.py") == LanguageId.PYTHON
        assert detect_language("src/stubs.pyi") == LanguageId.PYTHON

    def test_rust(self) -> None:
        assert detect_language("src/lib.rs") == LanguageId.RUST

    def test_go(self) -> None:
        assert detect_language("main.go") == LanguageId.GO

    def test_typescript(self) -> None:
        assert detect_language("app.ts") == LanguageId.TYPESCRIPT
        assert detect_language("Component.tsx") == LanguageId.TYPESCRIPT

    def test_javascript(self) -> None:
        assert detect_language("utils.js") == LanguageId.JAVASCRIPT
        assert detect_language("Button.jsx") == LanguageId.JAVASCRIPT
        assert detect_language("lib.mjs") == LanguageId.JAVASCRIPT
        assert detect_language("config.cjs") == LanguageId.JAVASCRIPT

    def test_c(self) -> None:
        assert detect_language("main.c") == LanguageId.C
        assert detect_language("header.h") == LanguageId.C

    def test_cpp(self) -> None:
        assert detect_language("main.cpp") == LanguageId.CPP
        assert detect_language("impl.cc") == LanguageId.CPP
        assert detect_language("header.hpp") == LanguageId.CPP

    def test_bash(self) -> None:
        assert detect_language("setup.sh") == LanguageId.BASH
        assert detect_language("run.bash") == LanguageId.BASH

    def test_yaml(self) -> None:
        assert detect_language("config.yaml") == LanguageId.YAML
        assert detect_language("ci.yml") == LanguageId.YAML

    def test_unsupported(self) -> None:
        assert detect_language("notes.txt") is None
        assert detect_language("Makefile") is None
        assert detect_language("Dockerfile") is None

    def test_case_insensitive(self) -> None:
        assert detect_language("Main.PY") == LanguageId.PYTHON
        assert detect_language("Config.YAML") == LanguageId.YAML


class TestLanguageIdString:
    def test_all_languages_have_ids(self) -> None:
        for lang in LanguageId:
            s = get_language_id_string(lang)
            assert isinstance(s, str)
            assert len(s) > 0, f"Missing languageId string for {lang}"

    def test_shellscript_label(self) -> None:
        """Bash maps to 'shellscript' per LSP spec, not 'bash'."""
        assert get_language_id_string(LanguageId.BASH) == "shellscript"


class TestServerCommands:
    def test_all_languages_have_commands(self) -> None:
        for lang in LanguageId:
            cmd, args = get_server_command(lang)
            assert isinstance(cmd, str)
            assert isinstance(args, list)
            assert len(cmd) > 0, f"Missing server command for {lang}"

    def test_pyright_is_npx_based(self) -> None:
        cmd, args = get_server_command(LanguageId.PYTHON)
        assert cmd == "npx"
        assert args == [
            "-y",
            "--package",
            "pyright",
            "pyright-langserver",
            "--stdio",
        ]

    def test_rust_analyzer_is_native(self) -> None:
        cmd, args = get_server_command(LanguageId.RUST)
        assert cmd == "rust-analyzer"
        assert args == []

    def test_gopls_is_native(self) -> None:
        cmd, args = get_server_command(LanguageId.GO)
        assert cmd == "gopls"
        assert "serve" in args


class TestRootMarkers:
    def test_rust_marker(self) -> None:
        assert "Cargo.toml" in get_root_markers(LanguageId.RUST)

    def test_go_marker(self) -> None:
        assert "go.mod" in get_root_markers(LanguageId.GO)

    def test_bash_no_markers(self) -> None:
        assert get_root_markers(LanguageId.BASH) == []

    def test_yaml_no_markers(self) -> None:
        assert get_root_markers(LanguageId.YAML) == []

    def test_c_and_cpp_have_compile_commands(self) -> None:
        for lang in (LanguageId.C, LanguageId.CPP):
            markers = get_root_markers(lang)
            assert "compile_commands.json" in markers


class TestWorkspaceRootResolution:
    def test_marker_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cargo_dir = Path(tmp) / "my-crate"
            (cargo_dir / "src").mkdir(parents=True)
            (cargo_dir / "Cargo.toml").touch()
            src_file = cargo_dir / "src" / "main.rs"

            root = resolve_workspace_root(src_file, LanguageId.RUST)
            assert root == cargo_dir.resolve()

    def test_marker_in_parent(self) -> None:
        """Marker can be several directories up."""
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "project"
            (proj / "src" / "lib" / "deep").mkdir(parents=True)
            (proj / "go.mod").touch()
            src_file = proj / "src" / "lib" / "deep" / "code.go"

            root = resolve_workspace_root(src_file, LanguageId.GO)
            assert root == proj.resolve()

    def test_first_marker_wins_python(self) -> None:
        """pyproject.toml beats setup.py when both present."""
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            (proj / "pyproject.toml").touch()
            (proj / "setup.py").touch()
            (proj / "src").mkdir()
            src_file = proj / "src" / "main.py"

            root = resolve_workspace_root(src_file, LanguageId.PYTHON)
            assert root == proj.resolve()

    def test_fallback_to_cwd_when_no_marker(self) -> None:
        """Bash has no markers → always fallback to cwd."""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "workdir"
            cwd.mkdir()
            script = cwd / "script.sh"
            script.touch()

            root = resolve_workspace_root(script, LanguageId.BASH, cwd=cwd)
            assert root == cwd.resolve()

    def test_fallback_no_cwd(self) -> None:
        """Without cwd, fallback to file's parent directory."""
        root = resolve_workspace_root(
            "/some/path/script.sh",
            LanguageId.BASH,
        )
        assert root == Path("/some/path").resolve()

    def test_config_override_absolute(self) -> None:
        root = resolve_workspace_root(
            "/any/file.rs",
            LanguageId.RUST,
            override="/custom/root",
        )
        assert root == Path("/custom/root").resolve()

    def test_config_override_relative(self) -> None:
        cwd = Path("/home/user/project")
        root = resolve_workspace_root(
            "/any/file.rs",
            LanguageId.RUST,
            override="subproject",
            cwd=cwd,
        )
        assert root == Path("/home/user/project/subproject").resolve()

    def test_override_skips_marker_search(self) -> None:
        """Even if a marker exists, override wins."""
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "with_marker"
            (proj / "src").mkdir(parents=True)
            (proj / "Cargo.toml").touch()

            root = resolve_workspace_root(
                proj / "src" / "main.rs",
                LanguageId.RUST,
                override="/forced/path",
            )
            assert root == Path("/forced/path").resolve()


class TestIterSupported:
    def test_iter_supported_extensions_includes_all(self) -> None:
        exts = iter_supported_extensions()
        assert ".py" in exts
        assert ".rs" in exts
        assert ".go" in exts
        assert ".ts" in exts
        assert ".tsx" in exts
        assert ".js" in exts
        assert ".yaml" in exts
        assert ".yml" in exts
        assert ".sh" in exts
        # All 9 language groups represented
        language_sets = set()
        for ext in exts:
            lang = detect_language(f"file{ext}")
            if lang:
                language_sets.add(lang)
        assert len(language_sets) == 9

    def test_iter_supported_languages(self) -> None:
        langs = iter_supported_languages()
        assert LanguageId.PYTHON in langs
        assert LanguageId.RUST in langs
        assert LanguageId.GO in langs
        assert LanguageId.YAML in langs
