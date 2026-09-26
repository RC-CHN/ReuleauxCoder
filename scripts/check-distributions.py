"""Reject release artifacts that omit bundled runtime resources or entry points."""

from pathlib import Path
import sys
import tarfile
import zipfile


directory = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
required = {
    f"reuleauxcoder/_tui/{name}"
    for name in (
        "cli.mjs",
        "manifest.json",
        "THIRD_PARTY_NOTICES.txt",
    )
}
required.update(
    f"reuleauxcoder/extensions/skills/builtin/rcoder-config/{name}"
    for name in (
        "SKILL.md",
        "references/configuration.md",
        "references/reasoning.md",
        "references/workflows.md",
    )
)
wheels = list(directory.glob("*.whl"))
sdists = list(directory.glob("*.tar.gz"))
assert wheels and sdists, "Build both wheel and sdist before checking distributions"
for wheel in wheels:
    with zipfile.ZipFile(wheel) as archive:
        assert required <= set(archive.namelist()), f"Bundled resources missing in {wheel}"
        entrypoints = next(
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/entry_points.txt")
        )
        text = archive.read(entrypoints).decode()
        for name in ("rcoder", "rcoder-cli", "rcoder-tui"):
            assert f"{name} = " in text, f"{name} missing in {wheel}"
for sdist in sdists:
    with tarfile.open(sdist) as archive:
        names = {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}
        assert required <= names, f"Bundled resources missing in {sdist}"
        assert not any("node_modules/" in name for name in names), (
            f"node_modules leaked into {sdist}"
        )
        assert "reuleauxcoder-agent/reuleauxcoder-agent" not in names, (
            f"Peer binary leaked into {sdist}"
        )
print("Wheel and sdist contain the bundled TUI and configuration skill; all frontend entry points are present.")
