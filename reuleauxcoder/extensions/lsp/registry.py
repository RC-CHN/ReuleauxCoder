"""Language detection, server command mapping, and workspace root markers.

Pure-data module — no external dependencies beyond stdlib.
"""

from __future__ import annotations

from enum import Enum, auto
from pathlib import Path


class LanguageId(Enum):
    """Canonical language identifiers used throughout the LSP module."""

    PYTHON = auto()
    RUST = auto()
    GO = auto()
    TYPESCRIPT = auto()
    JAVASCRIPT = auto()
    C = auto()
    CPP = auto()
    BASH = auto()
    YAML = auto()


# === Extension → Language mapping ===

_EXT_TO_LANGUAGE: dict[str, LanguageId] = {
    ".py": LanguageId.PYTHON,
    ".pyi": LanguageId.PYTHON,
    ".rs": LanguageId.RUST,
    ".go": LanguageId.GO,
    ".ts": LanguageId.TYPESCRIPT,
    ".tsx": LanguageId.TYPESCRIPT,
    ".js": LanguageId.JAVASCRIPT,
    ".jsx": LanguageId.JAVASCRIPT,
    ".mjs": LanguageId.JAVASCRIPT,
    ".cjs": LanguageId.JAVASCRIPT,
    ".c": LanguageId.C,
    ".h": LanguageId.C,
    ".cpp": LanguageId.CPP,
    ".cc": LanguageId.CPP,
    ".cxx": LanguageId.CPP,
    ".hpp": LanguageId.CPP,
    ".hxx": LanguageId.CPP,
    ".hh": LanguageId.CPP,
    ".ino": LanguageId.CPP,
    ".pde": LanguageId.CPP,
    ".sh": LanguageId.BASH,
    ".bash": LanguageId.BASH,
    ".yaml": LanguageId.YAML,
    ".yml": LanguageId.YAML,
}

# === LSP language identifier strings (LSP spec TextDocumentItem.languageId) ===

_LANGUAGE_ID_STRINGS: dict[LanguageId, str] = {
    LanguageId.PYTHON: "python",
    LanguageId.RUST: "rust",
    LanguageId.GO: "go",
    LanguageId.TYPESCRIPT: "typescript",
    LanguageId.JAVASCRIPT: "javascript",
    LanguageId.C: "c",
    LanguageId.CPP: "cpp",
    LanguageId.BASH: "shellscript",  # LSP spec uses "shellscript"
    LanguageId.YAML: "yaml",
}

# === Default server commands ===
#
# (command, args) tuples.  npx-based servers use -y for auto-install.

_SERVER_COMMANDS: dict[LanguageId, tuple[str, list[str]]] = {
    LanguageId.PYTHON: (
        "npx",
        ["-y", "--package", "pyright", "pyright-langserver", "--stdio"],
    ),
    LanguageId.RUST: ("rust-analyzer", []),
    LanguageId.GO: ("gopls", ["serve"]),
    LanguageId.TYPESCRIPT: (
        "npx",
        [
            "-y",
            "--package",
            "typescript",
            "--package",
            "typescript-language-server",
            "typescript-language-server",
            "--stdio",
        ],
    ),
    LanguageId.JAVASCRIPT: (
        "npx",
        [
            "-y",
            "--package",
            "typescript",
            "--package",
            "typescript-language-server",
            "typescript-language-server",
            "--stdio",
        ],
    ),
    LanguageId.C: ("clangd", []),
    LanguageId.CPP: ("clangd", []),
    LanguageId.BASH: ("npx", ["-y", "bash-language-server", "start"]),
    LanguageId.YAML: ("npx", ["-y", "yaml-language-server", "--stdio"]),
}

# === Workspace root markers ===
#
# Starting from the file being edited, walk up the directory tree
# looking for the first marker file.  The directory containing it
# becomes the workspace root for that language's LSP server.
#
# Languages without markers always fall back to cwd.

_ROOT_MARKERS: dict[LanguageId, list[str]] = {
    LanguageId.RUST: ["Cargo.toml"],
    LanguageId.GO: ["go.mod"],
    LanguageId.PYTHON: ["pyproject.toml", "setup.py", "setup.cfg"],
    LanguageId.TYPESCRIPT: ["tsconfig.json", "package.json"],
    LanguageId.JAVASCRIPT: ["package.json"],
    LanguageId.C: ["compile_commands.json", "Makefile", "CMakeLists.txt"],
    LanguageId.CPP: ["compile_commands.json", "Makefile", "CMakeLists.txt"],
    # Bash, YAML — no markers; always fallback to cwd
}


def detect_language(file_path: str | Path) -> LanguageId | None:
    """Map a file path to its LanguageId by extension.

    Returns None for unsupported file types.
    """
    suffix = Path(file_path).suffix.lower()
    return _EXT_TO_LANGUAGE.get(suffix)


def get_language_id_string(lang: LanguageId) -> str:
    """Return the LSP protocol language identifier string."""
    return _LANGUAGE_ID_STRINGS.get(lang, "")


def get_server_command(lang: LanguageId) -> tuple[str, list[str]]:
    """Return the default (command, args) for a language's LSP server."""
    return _SERVER_COMMANDS.get(lang, ("", []))


def get_root_markers(lang: LanguageId) -> list[str]:
    """Return the root marker filenames for a language, or empty list."""
    return _ROOT_MARKERS.get(lang, [])


def resolve_workspace_root(
    file_path: str | Path,
    lang: LanguageId,
    *,
    cwd: Path | None = None,
    override: str | None = None,
) -> Path:
    """Resolve the workspace root for a language given a file path.

    Resolution order (highest priority first):
    1. Config override (absolute or relative to cwd)
    2. Root marker file search (walk up from file_path)
    3. Fallback to cwd

    Args:
        file_path: The file being edited or queried.
        lang: Detected language.
        cwd: Agent's current working directory (fallback).
        override: Optional config-level workspace_root override.

    Returns:
        Resolved workspace root path.
    """
    if override:
        p = Path(override)
        if not p.is_absolute() and cwd:
            p = cwd / p
        return p.resolve()

    markers = get_root_markers(lang)
    if markers:
        current = Path(file_path).resolve().parent
        # Walk up to filesystem root
        root = Path(current.root)
        while current != current.parent and current != root:
            for marker in markers:
                if (current / marker).exists():
                    return current
            current = current.parent

    # Fallback to cwd
    if cwd:
        return cwd.resolve()
    return Path(file_path).resolve().parent


def iter_supported_extensions() -> list[str]:
    """Return all supported file extensions (for documentation / error messages)."""
    return sorted(_EXT_TO_LANGUAGE.keys())


def iter_supported_languages() -> list[LanguageId]:
    """Return all supported language IDs."""
    return sorted(_SERVER_COMMANDS.keys(), key=lambda lid: lid.name)
