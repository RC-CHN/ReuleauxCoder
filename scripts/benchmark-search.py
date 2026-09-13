"""Reproducible local search benchmark; optionally compare a trusted Git revision.

Run: python scripts/benchmark-search.py --baseline-ref e62dbb5
The baseline is loaded from that revision's local workspace adapter. Timings run
without tracing; peak Python allocation is measured in a separate search.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reuleauxcoder.extensions.tools.builtin.grep import _SKIP_DIRS
from reuleauxcoder.infrastructure.workspace import LocalWorkspacePort


def measure(port, root: Path, pattern: str, runs: int) -> dict:
    durations = []
    for _ in range(runs):
        started = time.perf_counter()
        result = port(root).search_text(pattern, ".", exclude_dirs=tuple(_SKIP_DIRS))
        durations.append(time.perf_counter() - started)
    tracemalloc.start()
    port(root).search_text(pattern, ".", exclude_dirs=tuple(_SKIP_DIRS))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "median_ms": round(statistics.median(durations) * 1000, 3),
        "peak_python_mib": round(peak / 1024**2, 3),
        "matches": len(result.matches),
        "truncated": result.truncated,
        "returned_chars": sum(len(m.line) for m in result.matches),
        "reasons": getattr(result, "reasons", None),
        "scanned_bytes": getattr(result, "scanned_bytes", None),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--pattern", default="class.*Missing")
    args = parser.parse_args()
    implementations = {"current": LocalWorkspacePort}
    if args.baseline_ref:
        source = subprocess.run(
            [
                "git",
                "show",
                f"{args.baseline_ref}:reuleauxcoder/infrastructure/workspace/local.py",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        namespace = {"__name__": "search_benchmark_baseline"}
        exec(compile(source, "<baseline local workspace>", "exec"), namespace)  # noqa: S102 — explicit trusted revision benchmark
        implementations["baseline"] = namespace["LocalWorkspacePort"]
    if args.workspace:
        print(
            json.dumps(
                {
                    name: measure(
                        port, args.workspace.resolve(), args.pattern, args.runs
                    )
                    for name, port in implementations.items()
                },
                indent=2,
            )
        )
        return
    with tempfile.TemporaryDirectory(prefix="rcoder-search-bench-") as directory:
        root = Path(directory)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / ".gitignore").write_text("generated/\nnode_modules/\n")
        (root / "generated").mkdir()
        (root / "generated" / "bundle.js.map").write_text("x" * (16 * 1024**2))
        source = root / "src"
        source.mkdir()
        for i in range(500):
            (source / f"file-{i:04}.py").write_text("class Example: pass\n" * 200)
        print(
            json.dumps(
                {
                    name: measure(port, root, args.pattern, args.runs)
                    for name, port in implementations.items()
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
