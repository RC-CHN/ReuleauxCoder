"""Optional git-worktree isolation for delegated write tasks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess


@dataclass(frozen=True, slots=True)
class WorktreeLease:
    repo_root: Path
    path: Path
    job_id: str


def create_worktree(root: str | Path, job_id: str) -> WorktreeLease:
    root_path = Path(root).resolve()
    repo_root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=root_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    path = repo_root / ".rcoder" / "worktrees" / job_id
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError(f"Sub-agent worktree already exists: {path}")
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(path), "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return WorktreeLease(repo_root=repo_root, path=path, job_id=job_id)


def remove_worktree(lease: WorktreeLease | str | Path) -> None:
    if isinstance(lease, WorktreeLease):
        repo_root, path = lease.repo_root, lease.path
    else:
        path = Path(lease).resolve()
        repo_root = Path(
            subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(path)],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
