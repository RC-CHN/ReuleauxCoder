"""Bounded, read-only Git state observation for the execution overlay."""

from __future__ import annotations

from collections import Counter
import copy
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any


_CHANGE_NAMES = {
    "A": "added",
    "C": "copied",
    "D": "deleted",
    "M": "modified",
    "R": "renamed",
    "T": "type_changed",
    "U": "unmerged",
}


@dataclass(frozen=True, slots=True)
class _CommandResult:
    stdout: bytes
    returncode: int
    truncated: bool = False
    timed_out: bool = False
    launch_error: str | None = None


def _bounded_process(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    max_output_bytes: int,
) -> _CommandResult:
    """Run a process without ever retaining unbounded stdout."""
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    except FileNotFoundError:
        return _CommandResult(
            stdout=b"", returncode=127, launch_error="executable_not_found"
        )
    except PermissionError:
        return _CommandResult(
            stdout=b"", returncode=126, launch_error="permission_denied"
        )
    except (OSError, ValueError):
        return _CommandResult(stdout=b"", returncode=127, launch_error="launch_failed")

    output = bytearray()
    output_lock = threading.Lock()
    was_truncated = threading.Event()

    def _read_stdout() -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(8_192)
                if not chunk:
                    return
                with output_lock:
                    remaining = max_output_bytes + 1 - len(output)
                    if remaining > 0:
                        output.extend(chunk[:remaining])
                    if len(output) > max_output_bytes:
                        was_truncated.set()
                if was_truncated.is_set():
                    try:
                        process.terminate()
                    except OSError:
                        pass
                    return
        except (OSError, ValueError):
            return

    reader = threading.Thread(target=_read_stdout, daemon=True)
    reader.start()
    timed_out = False
    try:
        process.wait(timeout=max(0.05, timeout_seconds))
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
    reader.join(timeout=0.2)
    with output_lock:
        retained = bytes(output[:max_output_bytes])
    return _CommandResult(
        stdout=retained,
        returncode=process.returncode if process.returncode is not None else -1,
        truncated=was_truncated.is_set(),
        timed_out=timed_out,
    )


def _clip(value: str, limit: int) -> str:
    compact = value.replace("\x00", "").replace("\r", " ").replace("\n", " ")
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _path_prefix(path: str) -> str:
    normalized = path.replace("\\", "/").lstrip("./")
    head, separator, _tail = normalized.partition("/")
    return f"{head}/**" if separator else head


def _stat_signature(path: Path) -> tuple[int, int, int, int] | None:
    try:
        status = path.stat()
    except OSError:
        return None
    return (status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)


class GitMonitor:
    """Observe one project repository with strict time and output budgets."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        timeout_seconds: float = 1.0,
        max_output_bytes: int = 64 * 1024,
        sample_limit: int = 3,
        group_limit: int = 2,
        path_limit: int = 96,
        cache_ttl_seconds: float = 0.5,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=False)
        self.timeout_seconds = max(0.05, float(timeout_seconds))
        self.max_output_bytes = max(128, int(max_output_bytes))
        self.sample_limit = max(1, int(sample_limit))
        self.group_limit = max(1, int(group_limit))
        self.path_limit = max(24, int(path_limit))
        self.cache_ttl_seconds = max(0.0, float(cache_ttl_seconds))
        self._lock = threading.Lock()
        self._repo_root: Path | None = None
        self._discovery_failed_at: float | None = None
        self._discovery_failed_turn_id: str | None = None
        self._discovery_failure_reason: str | None = None
        self._last_head: str | None = None
        self._head_initialized = False
        self._notice_turn_id: str | None = None
        self._head_notice: dict[str, Any] | None = None
        self._head_cache_signature: tuple | None = None
        self._head_cache_result: tuple[dict[str, str] | None, bool] | None = None
        self._snapshot_cache_at: float | None = None
        self._snapshot_cache_turn_id: str | None = None
        self._snapshot_cache: dict[str, Any] | None = None

    def invalidate(self) -> None:
        """Discard the short-lived status cache after a possible workspace write."""
        with self._lock:
            self._snapshot_cache_at = None
            self._snapshot_cache_turn_id = None
            self._snapshot_cache = None

    def _git(
        self,
        *args: str,
        cwd: Path | None = None,
        max_output_bytes: int | None = None,
    ) -> _CommandResult:
        return _bounded_process(
            ["git", "-c", "core.quotepath=false", *args],
            cwd=cwd or self.workspace_root,
            timeout_seconds=self.timeout_seconds,
            max_output_bytes=max_output_bytes or self.max_output_bytes,
        )

    def _discover_repo(self, *, turn_id: str) -> Path | None:
        if self._repo_root is not None:
            return self._repo_root
        now = time.monotonic()
        if (
            self._discovery_failed_at is not None
            and self._discovery_failed_turn_id == turn_id
            and now - self._discovery_failed_at < 5.0
        ):
            return None
        result = self._git("rev-parse", "--show-toplevel", max_output_bytes=4_096)
        if result.timed_out:
            self._discovery_failed_at = now
            self._discovery_failed_turn_id = turn_id
            self._discovery_failure_reason = "git_timed_out"
            return None
        if result.launch_error == "executable_not_found":
            self._discovery_failed_at = now
            self._discovery_failed_turn_id = turn_id
            self._discovery_failure_reason = "git_not_installed"
            return None
        if result.launch_error is not None or result.truncated:
            self._discovery_failed_at = now
            self._discovery_failed_turn_id = turn_id
            self._discovery_failure_reason = "git_unavailable"
            return None
        if result.returncode != 0:
            self._discovery_failed_at = now
            self._discovery_failed_turn_id = turn_id
            self._discovery_failure_reason = "not_initialized"
            return None
        text = result.stdout.decode("utf-8", errors="replace").strip()
        if not text:
            self._discovery_failed_at = now
            self._discovery_failed_turn_id = turn_id
            self._discovery_failure_reason = "not_initialized"
            return None
        self._repo_root = Path(text).resolve(strict=False)
        self._discovery_failed_at = None
        self._discovery_failed_turn_id = None
        self._discovery_failure_reason = None
        return self._repo_root

    def snapshot(self, *, turn_id: str | None = None) -> dict[str, Any] | None:
        """Return one JSON-safe repository snapshot, or None outside Git."""
        with self._lock:
            normalized_turn_id = str(turn_id or "no-active-turn")
            now = time.monotonic()
            if (
                self._snapshot_cache is not None
                and self._snapshot_cache_at is not None
                and self._snapshot_cache_turn_id == normalized_turn_id
                and now - self._snapshot_cache_at < self.cache_ttl_seconds
            ):
                return copy.deepcopy(self._snapshot_cache)
            repo_root = self._discover_repo(turn_id=normalized_turn_id)
            if repo_root is None:
                return self._remember_snapshot(
                    {
                        "repository_root": _clip(
                            str(self.workspace_root), max(96, self.path_limit * 2)
                        ),
                        "available": False,
                        "reason": self._discovery_failure_reason or "not_initialized",
                        "truncated": False,
                    },
                    turn_id=normalized_turn_id,
                    observed_at=now,
                )
            status_result = self._git(
                "status",
                "--porcelain=v1",
                "-z",
                "--branch",
                "--untracked-files=normal",
                "--no-renames",
                cwd=repo_root,
            )
            if status_result.timed_out:
                return self._remember_snapshot(
                    {
                        "repository_root": str(repo_root),
                        "available": False,
                        "reason": "status_timed_out",
                        "truncated": True,
                    },
                    turn_id=normalized_turn_id,
                    observed_at=now,
                )
            if status_result.returncode != 0 and not status_result.truncated:
                return self._remember_snapshot(
                    {
                        "repository_root": str(repo_root),
                        "available": False,
                        "reason": "status_failed",
                        "truncated": False,
                    },
                    turn_id=normalized_turn_id,
                    observed_at=now,
                )

            branch, staged, unstaged, untracked = self._parse_status(
                status_result.stdout,
                output_complete=not status_result.truncated,
            )
            head, head_observation_valid = self._read_head(repo_root, branch=branch)
            notice = self._observe_head_change(
                repo_root,
                head,
                observation_valid=head_observation_valid,
                turn_id=normalized_turn_id,
            )
            result: dict[str, Any] = {
                "repository_root": _clip(str(repo_root), max(96, self.path_limit * 2)),
                "available": True,
                "branch": branch,
                "head": (
                    f"{head['short_oid']} {head['summary']}"
                    if head is not None
                    else "no commits"
                    if head_observation_valid
                    else "unavailable"
                ),
                "changes": {
                    "staged": self._summarize(
                        staged, status_result.truncated, collapsed_directories=False
                    ),
                    "unstaged": self._summarize(
                        unstaged, status_result.truncated, collapsed_directories=False
                    ),
                    "untracked": self._summarize(
                        untracked, status_result.truncated, collapsed_directories=True
                    ),
                },
                "status_output_truncated": status_result.truncated,
            }
            if notice is not None:
                result["head_change"] = notice
            return self._remember_snapshot(
                result,
                turn_id=normalized_turn_id,
                observed_at=now,
            )

    def _remember_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        turn_id: str,
        observed_at: float,
    ) -> dict[str, Any]:
        self._snapshot_cache = copy.deepcopy(snapshot)
        self._snapshot_cache_at = max(observed_at, time.monotonic())
        self._snapshot_cache_turn_id = turn_id
        return snapshot

    def _parse_status(
        self, output: bytes, *, output_complete: bool
    ) -> tuple[
        str | None,
        list[tuple[str, str]],
        list[tuple[str, str]],
        list[tuple[str, str]],
    ]:
        records = output.split(b"\x00")
        if not output_complete and output and not output.endswith(b"\x00"):
            records = records[:-1]
        branch: str | None = None
        staged: list[tuple[str, str]] = []
        unstaged: list[tuple[str, str]] = []
        untracked: list[tuple[str, str]] = []
        for raw in records:
            if not raw:
                continue
            record = raw.decode("utf-8", errors="replace")
            if record.startswith("## "):
                branch = _clip(record[3:], self.path_limit)
                continue
            if len(record) < 4:
                continue
            x, y, path = record[0], record[1], record[3:]
            if x == "?" and y == "?":
                untracked.append((path, "untracked"))
                continue
            if x not in {" ", "?", "!"}:
                staged.append((path, _CHANGE_NAMES.get(x, "changed")))
            if y not in {" ", "?", "!"}:
                unstaged.append((path, _CHANGE_NAMES.get(y, "changed")))
        return branch, staged, unstaged, untracked

    def _summarize(
        self,
        entries: list[tuple[str, str]],
        command_truncated: bool,
        *,
        collapsed_directories: bool,
    ) -> dict[str, Any]:
        shown = entries[: self.sample_limit]
        items: list[str] = []
        for path, change in shown:
            suffix = (
                " (directory collapsed; contents not scanned)"
                if collapsed_directories and path.endswith("/")
                else ""
            )
            items.append(f"{change} {_clip(path, self.path_limit)}{suffix}")

        omitted_observed = max(0, len(entries) - len(shown))
        if command_truncated or omitted_observed:
            omitted_entries = entries[len(shown) :]
            groups = Counter(_path_prefix(path) for path, _change in omitted_entries)
            group_text = ", ".join(
                _clip(prefix, self.path_limit)
                for prefix, _count in sorted(
                    groups.items(), key=lambda item: (-item[1], item[0])
                )[: self.group_limit]
            )
            if command_truncated:
                if omitted_observed:
                    marker = (
                        f"… and at least {omitted_observed} more observed; "
                        "total unknown (scan truncated)"
                    )
                else:
                    marker = "… additional entries may exist (scan truncated)"
            else:
                marker = f"… and {omitted_observed} more"
            if group_text:
                marker += f" (mostly {group_text})"
            items.append(marker)
        return {
            "count": len(entries) if not command_truncated else f">={len(entries)}",
            "items": items,
        }

    def _read_head(
        self, repo_root: Path, *, branch: str | None
    ) -> tuple[dict[str, str] | None, bool]:
        signature = self._head_signature(repo_root, branch=branch)
        if (
            signature is not None
            and signature == self._head_cache_signature
            and self._head_cache_result is not None
        ):
            return self._head_cache_result
        result = self._git(
            "show",
            "-s",
            "--format=%H%x00%h%x00%s",
            "HEAD",
            cwd=repo_root,
            max_output_bytes=4_096,
        )
        if result.returncode != 0 or result.timed_out or result.truncated:
            # Porcelain status identifies an unborn branch explicitly. Other
            # failures are transient/unknown and must not look like HEAD was
            # deleted, otherwise a slow Git process creates stale notices.
            outcome = (None, bool(branch and branch.startswith("No commits yet on ")))
            if signature is not None and outcome[1]:
                self._head_cache_signature = signature
                self._head_cache_result = outcome
            return outcome
        parts = result.stdout.rstrip(b"\r\n").split(b"\x00", 2)
        if len(parts) != 3:
            return None, False
        outcome = (
            {
                "oid": parts[0].decode("ascii", errors="replace"),
                "short_oid": parts[1].decode("ascii", errors="replace"),
                "summary": _clip(
                    parts[2].decode("utf-8", errors="replace"), self.path_limit
                ),
            },
            True,
        )
        if signature is not None:
            self._head_cache_signature = signature
            self._head_cache_result = outcome
        return outcome

    @staticmethod
    def _head_signature(repo_root: Path, *, branch: str | None) -> tuple | None:
        """Describe the Git HEAD/ref files without spawning another process."""
        marker = repo_root / ".git"
        git_dir: Path
        if marker.is_dir():
            git_dir = marker
        elif marker.is_file():
            try:
                prefix, separator, target = (
                    marker.read_text(encoding="utf-8", errors="replace")
                    .strip()
                    .partition(":")
                )
            except OSError:
                return None
            if not separator or prefix.strip().lower() != "gitdir":
                return None
            candidate = Path(target.strip())
            git_dir = (
                candidate if candidate.is_absolute() else (repo_root / candidate)
            ).resolve(strict=False)
        elif (repo_root / "HEAD").is_file():
            git_dir = repo_root
        else:
            return None

        common_dir = git_dir
        common_marker = git_dir / "commondir"
        if common_marker.is_file():
            try:
                candidate = Path(common_marker.read_text(encoding="utf-8").strip())
            except OSError:
                return None
            common_dir = (
                candidate if candidate.is_absolute() else git_dir / candidate
            ).resolve(strict=False)

        head_path = git_dir / "HEAD"
        try:
            head_content = head_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        signatures: list[object] = [
            branch,
            head_content,
            _stat_signature(head_path),
        ]
        if head_content.startswith("ref:"):
            ref_name = head_content[4:].strip()
            ref_path = common_dir / ref_name
            if not ref_path.exists():
                ref_path = git_dir / ref_name
            signatures.extend(
                (
                    ref_name,
                    _stat_signature(ref_path),
                    _stat_signature(common_dir / "packed-refs"),
                )
            )
        return tuple(signatures)

    def _observe_head_change(
        self,
        repo_root: Path,
        head: dict[str, str] | None,
        *,
        observation_valid: bool,
        turn_id: str,
    ) -> dict[str, Any] | None:
        current = head.get("oid") if head is not None else None
        if turn_id != self._notice_turn_id:
            self._notice_turn_id = turn_id
            self._head_notice = None
        if not observation_valid:
            return self._head_notice
        previous = self._last_head
        if self._head_initialized and previous and current and previous != current:
            self._head_notice = self._describe_head_change(
                repo_root, previous=previous, current=current
            )
        elif self._head_initialized and previous and current is None:
            self._head_notice = {
                "kind": "head_removed",
                "from": previous[:12],
            }
        elif self._head_initialized and previous is None and current:
            self._head_notice = {"kind": "head_created", "to": current[:12]}
        self._last_head = current
        self._head_initialized = True
        return self._head_notice

    def _describe_head_change(
        self, repo_root: Path, *, previous: str, current: str
    ) -> dict[str, Any]:
        ancestor = self._git(
            "merge-base",
            "--is-ancestor",
            previous,
            current,
            cwd=repo_root,
            max_output_bytes=256,
        )
        if ancestor.returncode != 0:
            return {
                "kind": "rewritten_or_diverged",
                "from": previous[:12],
                "to": current[:12],
            }
        count_result = self._git(
            "rev-list",
            "--count",
            f"{previous}..{current}",
            cwd=repo_root,
            max_output_bytes=128,
        )
        try:
            commit_count = int(count_result.stdout.strip())
        except (TypeError, ValueError):
            commit_count = 0
        log = self._git(
            "log",
            "--format=%H%x00%h%x00%s",
            "-z",
            "-n",
            "5",
            f"{previous}..{current}",
            cwd=repo_root,
            max_output_bytes=8_192,
        )
        commits: list[str] = []
        if log.returncode == 0 and not log.timed_out:
            parts = [part for part in log.stdout.split(b"\x00") if part]
            for index in range(0, len(parts) - 2, 3):
                commits.append(
                    parts[index + 1].decode("ascii", errors="replace")
                    + " "
                    + _clip(
                        parts[index + 2].decode("utf-8", errors="replace"),
                        self.path_limit,
                    )
                )
        if commit_count > len(commits):
            commits.append(f"… and {commit_count - len(commits)} more")
        return {
            "kind": "new_commits",
            "from": previous[:12],
            "to": current[:12],
            "count": commit_count,
            "commits": commits,
        }

    @staticmethod
    def compact(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
        """Shrink a previously observed snapshot for an overlay pressure fallback."""
        if not snapshot or not snapshot.get("available"):
            return snapshot
        compacted = dict(snapshot)
        changes: dict[str, Any] = {}
        for name, value in dict(snapshot.get("changes") or {}).items():
            bucket = dict(value)
            items = list(bucket.get("items") or [])
            bucket["items"] = (
                items[:1] + items[-1:]
                if len(items) > 1 and items[-1].startswith("…")
                else items[:1]
            )
            changes[name] = bucket
        compacted["changes"] = changes
        notice = compacted.get("head_change")
        if isinstance(notice, dict) and "commits" in notice:
            compact_notice = dict(notice)
            commits = list(notice.get("commits") or [])
            compact_notice["commits"] = (
                commits[:1] + commits[-1:]
                if len(commits) > 1 and commits[-1].startswith("…")
                else commits[:2]
            )
            compacted["head_change"] = compact_notice
        return compacted
