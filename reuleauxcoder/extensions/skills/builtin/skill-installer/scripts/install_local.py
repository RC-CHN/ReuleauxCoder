"""Copy one reviewed skill directory without executing code or replacing files."""

import argparse
from pathlib import Path
import re
import shutil
import sys

import yaml


def install(source: Path, destination_root: Path) -> Path:
    if source.is_symlink():
        raise ValueError("Source must be a real directory, not a symlink")
    source = source.resolve(strict=True)
    raw = (source / "SKILL.md").read_text(encoding="utf-8")
    parts = re.split(r"^---\s*$", raw, maxsplit=2, flags=re.MULTILINE)
    if len(parts) != 3 or parts[0].strip():
        raise ValueError("SKILL.md requires YAML frontmatter")
    meta = yaml.safe_load(parts[1])
    if not isinstance(meta, dict):
        raise ValueError("Frontmatter must be a mapping")
    name = meta.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
        raise ValueError("Invalid skill name")
    if name != source.name:
        raise ValueError("Skill name must match its source directory")
    if not isinstance(meta.get("description"), str) or not meta["description"].strip() or not parts[2].strip():
        raise ValueError("Skill needs a description and body")
    destination = destination_root.resolve() / name
    if destination.is_relative_to(source):
        raise ValueError("Destination cannot be inside the source")
    for file in source.rglob("*"):
        if file.is_symlink():
            raise ValueError(f"Symlinks are not copied: {file.relative_to(source)}")
        if not file.is_file() and not file.is_dir():
            raise ValueError(f"Unsupported file type: {file.relative_to(source)}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects existing files, including empty directories.
    destination.mkdir()
    try:
        shutil.copytree(
            source, destination, dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
    except BaseException:
        shutil.rmtree(destination)
        raise
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination_root", type=Path)
    args = parser.parse_args()
    try:
        print(install(args.source, args.destination_root))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Installation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
