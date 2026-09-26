"""Validate a self-contained skill directory without executing its contents."""

import argparse
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

import yaml


def validate(root: Path) -> list[str]:
    root = root.resolve()
    errors = []
    entry = root / "SKILL.md"
    try:
        text = entry.read_text(encoding="utf-8")
        parts = re.split(r"^---\s*$", text, maxsplit=2, flags=re.MULTILINE)
        if len(parts) != 3 or parts[0].strip():
            return ["SKILL.md requires YAML frontmatter delimited by --- lines"]
        meta = yaml.safe_load(parts[1])
        if not isinstance(meta, dict):
            return ["Frontmatter must be a mapping"]
        name = meta.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
            errors.append("name must contain lowercase words/numbers separated by hyphens")
        elif name != root.name or len(name) > 64:
            errors.append("name must match the directory and be at most 64 characters")
        if not isinstance(meta.get("description"), str) or not meta["description"].strip():
            errors.append("description must be nonempty text")
        if not parts[2].strip():
            errors.append("Skill body is empty")
        for file in root.rglob("*"):
            if file.is_symlink():
                errors.append(f"Symlink is not self-contained: {file.relative_to(root)}")
                continue
            if not file.is_file() or file.suffix != ".md":
                continue
            content = file.read_text(encoding="utf-8")
            # Inline Markdown links; code fences are examples, not resource links.
            content = re.sub(r"^```[^\n]*\n.*?^```\s*$", "", content, flags=re.MULTILINE | re.DOTALL)
            for raw in re.findall(r"\]\(([^)]+)\)", content):
                target = raw.strip().strip("<>")
                url = urlsplit(target)
                if url.scheme or not url.path:
                    continue
                path = (file.parent / unquote(url.path)).resolve()
                if not path.is_relative_to(root):
                    errors.append(f"Resource escapes skill: {file.relative_to(root)} -> {target}")
                elif not path.is_file():
                    errors.append(f"Missing resource: {file.relative_to(root)} -> {target}")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        errors.append(str(exc))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    errors = validate(args.directory)
    for error in errors:
        print(error, file=sys.stderr)
    if not errors:
        print(f"Valid self-contained skill: {args.directory.name}")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
