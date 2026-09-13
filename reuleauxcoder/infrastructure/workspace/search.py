"""Bounded local search; Git owns ignore rules when available in a repository."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import regex

from reuleauxcoder.domain.cancellation import CancellationSignal
from reuleauxcoder.domain.workspace import (
    WorkspaceError,
    WorkspaceErrorCode,
    WorkspaceSearchLimits,
    WorkspaceSearchMatch,
    WorkspaceSearchResult,
    compile_portable_glob,
)


def _git_files(base: Path, timeout: float) -> Iterator[Path]:
    command = [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-C",
        str(base),
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "--deduplicate",
        "-z",
        "--",
    ]
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    ) as process:
        timer = threading.Timer(timeout, process.kill)
        timer.start()
        try:
            assert process.stdout is not None
            pending = b""
            while chunk := process.stdout.read1(8192):
                names = (pending + chunk).split(b"\0")
                pending = names.pop()
                for name in names:
                    yield base / os.fsdecode(name)
            if process.wait() != 0:
                raise WorkspaceError(
                    WorkspaceErrorCode.IO_ERROR,
                    "Git file discovery failed or timed out",
                )
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait()


def _files(
    base: Path, excluded: set[str], include_ignored: bool, timeout: float,
    stopped: Callable[[], bool],
) -> Iterator[Path]:
    if stopped():
        return
    if base.is_file():
        yield base
        return
    if (
        not include_ignored
        and shutil.which("git")
        and any((parent / ".git").exists() for parent in (base, *base.parents))
        and subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--is-inside-work-tree"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        ).stdout.strip()
        == b"true"
    ):
        yield from _git_files(base, timeout)
        return

    def onerror(error: OSError) -> None:
        raise error

    for directory, dirs, files in os.walk(base, onerror=onerror, followlinks=False):
        if stopped():
            return
        dirs[:] = sorted(name for name in dirs if name not in excluded)
        for name in sorted(files):
            yield Path(directory) / name


def search_text(
    base: Path,
    pattern: str,
    *,
    include: str | None,
    exclude_dirs: tuple[str, ...],
    max_files: int,
    max_matches: int,
    literal: bool,
    include_ignored: bool,
    limits: WorkspaceSearchLimits,
    cancellation: CancellationSignal | None,
) -> WorkspaceSearchResult:
    if max_files < 1 or max_matches < 1:
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_PATH,
            "max_files and max_matches must be positive",
        )
    try:
        matcher = None if literal else regex.compile(pattern, regex.VERSION0)
    except regex.error as error:
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_PATH, f"invalid regex: {error}"
        ) from error
    glob = (
        compile_portable_glob(
            include if "/" in include or "\\" in include else "**/" + include
        )
        if include is not None
        else None
    )
    deadline = time.monotonic() + limits.timeout_sec
    reasons: set[str] = set()
    matches: list[WorkspaceSearchMatch] = []
    scanned_files = scanned_bytes = output_chars = visited = 0
    excluded = set(exclude_dirs)
    is_directory = base.is_dir()

    def stopped() -> bool:
        if cancellation is not None and cancellation.is_set():
            reasons.add("cancelled")
            return True
        if time.monotonic() >= deadline:
            reasons.add("timeout")
            return True
        return False

    def result() -> WorkspaceSearchResult:
        return WorkspaceSearchResult(
            tuple(matches),
            bool(reasons),
            tuple(sorted(reasons)),
            scanned_files,
            scanned_bytes,
        )

    candidates = _files(base, excluded, include_ignored, limits.timeout_sec, stopped)
    try:
        for path in candidates:
            if stopped():
                break
            visited += 1
            if visited > max_files * 4:
                reasons.add("entry_limit")
                break
            if is_directory:
                relative = path.relative_to(base)
                if excluded.intersection(relative.parts[:-1]):
                    continue
                if glob is not None and not glob.matches(relative.as_posix()):
                    continue
                # Git can list symlinks and submodules; never follow them during search.
                if path.resolve() != path:
                    continue
            try:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    continue
                if scanned_files >= max_files:
                    reasons.add("file_limit")
                    break
                scanned_files += 1
                if info.st_size > limits.max_file_bytes:
                    reasons.add("file_size")
                    continue
                with path.open("rb") as stream:
                    line_number = file_bytes = 0
                    while True:
                        if stopped():
                            return result()
                        remaining = min(
                            limits.max_file_bytes - file_bytes,
                            limits.max_scan_bytes - scanned_bytes,
                        )
                        raw = stream.readline(remaining + 1)
                        if not raw:
                            break
                        scanned_bytes += len(raw)
                        file_bytes += len(raw)
                        if scanned_bytes > limits.max_scan_bytes:
                            reasons.add("scan_bytes")
                            return result()
                        if file_bytes > limits.max_file_bytes:
                            reasons.add("file_size")
                            break
                        if b"\0" in raw:
                            break
                        for line in raw.decode("utf-8", errors="replace").splitlines():
                            line_number += 1
                            try:
                                matched = (
                                    pattern in line
                                    if matcher is None
                                    else matcher.search(
                                        line,
                                        timeout=0.1,
                                        concurrent=True,
                                    )
                                    is not None
                                )
                            except TimeoutError:
                                reasons.add("regex_timeout")
                                return result()
                            if not matched:
                                continue
                            line = line.rstrip()
                            shortened = len(line) > limits.max_line_chars
                            line = line[: limits.max_line_chars]
                            cost = (
                                len(str(path)) + len(str(line_number)) + len(line) + 4
                            )
                            if shortened:
                                cost += len(" … [line truncated]")
                            if output_chars + cost > limits.max_output_chars:
                                reasons.add("output_chars")
                                return result()
                            output_chars += cost
                            matches.append(
                                WorkspaceSearchMatch(
                                    str(path), line_number, line, shortened
                                )
                            )
                            if shortened:
                                reasons.add("line_chars")
                            if len(matches) >= max_matches:
                                reasons.add("match_limit")
                                return result()
            except FileNotFoundError:
                continue
    except subprocess.TimeoutExpired:
        reasons.add("timeout")
    except OSError as error:
        raise WorkspaceError(
            WorkspaceErrorCode.IO_ERROR, f"search failed: {error}"
        ) from error
    finally:
        candidates.close()
    return result()
