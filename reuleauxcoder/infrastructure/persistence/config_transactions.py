"""Private configuration candidates and recoverable, atomic file replacement."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from reuleauxcoder.domain.config.management import ConfigOperationError
from reuleauxcoder.infrastructure.fs.atomic import atomic_write


@dataclass(frozen=True, slots=True)
class ConfigPaths:
    user: Path
    workspace: Path
    explicit: Path | None = None

    def __post_init__(self):
        for name in ("user", "workspace", "explicit"):
            path = getattr(self, name)
            if path is not None:
                object.__setattr__(self, name, path.absolute())

    def layers(self) -> tuple[tuple[str, Path], ...]:
        layers = (("user", self.user), ("workspace", self.workspace))
        return (*layers, ("explicit", self.explicit)) if self.explicit else layers

    def target(self, scope: str) -> Path:
        for name, path in self.layers():
            if name == scope:
                return path
        raise ConfigOperationError(
            "invalid_scope",
            "Scope must identify an available user, workspace or explicit configuration file.",
        )


class ConfigTransactionStore:
    def __init__(self, paths: ConfigPaths, state_dir: Path):
        self.paths = paths
        self.state_dir = state_dir.absolute()
        identity = json.dumps(
            [(name, str(path.absolute())) for name, path in paths.layers()]
        )
        self.directory = self.state_dir / hashlib.sha256(identity.encode()).hexdigest()[:24]

    @contextmanager
    def locked(self):
        """One short cross-process critical section; probes never hold this lock."""
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.state_dir / "lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "r+b") as stream:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise ConfigOperationError(
                    "busy",
                    "Another configuration operation is committing; retry shortly.",
                ) from error
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def snapshot(self) -> tuple[str, dict[str, bytes | None]]:
        contents = {}
        digest = hashlib.sha256()
        for scope, path in self.paths.layers():
            try:
                with path.open("rb") as stream:
                    content = stream.read(1024 * 1024 + 1)
            except FileNotFoundError:
                content = None
            except OSError as error:
                raise ConfigOperationError(
                    "unreadable_config", f"Cannot read the {scope} configuration file."
                ) from error
            if content is not None and len(content) > 1024 * 1024:
                raise ConfigOperationError(
                    "too_large",
                    f"The {scope} configuration exceeds the 1 MiB management limit; reduce it before preparing changes.",
                )
            contents[scope] = content
            digest.update(
                json.dumps(
                    [
                        scope,
                        str(path.absolute()),
                        str(path.resolve()),
                        content is not None,
                    ]
                ).encode()
            )
            digest.update(hashlib.sha256(content or b"").digest())
        return digest.hexdigest(), contents

    def candidate_contents(
        self, contents: dict, scope: str, content: bytes | None
    ) -> dict:
        """Replacing one physical file changes every source alias of that file."""
        target = self.paths.target(scope).resolve()
        return {
            name: content if path.resolve() == target else contents[name]
            for name, path in self.paths.layers()
        }

    def read(self, change_id: str) -> dict:
        if not isinstance(change_id, str) or not re.fullmatch(
            r"[a-f0-9]{32}", change_id
        ):
            raise ConfigOperationError(
                "invalid_change", "Invalid configuration change identifier."
            )
        try:
            with (self.directory / f"{change_id}.json").open("rb") as stream:
                data = stream.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise ValueError
            return json.loads(data)
        except FileNotFoundError as error:
            raise ConfigOperationError(
                "unknown_change",
                "Configuration change was not found in this workspace context.",
            ) from error
        except (ValueError, OSError) as error:
            raise ConfigOperationError(
                "invalid_record", "Configuration change record is unreadable."
            ) from error

    def write(self, record: dict) -> None:
        content = json.dumps(record, ensure_ascii=False, allow_nan=False).encode()
        atomic_write(self.directory / f"{record['id']}.json", content)

    def records(self, limit=100) -> list[dict]:
        if not self.directory.exists():
            return []
        paths = sorted(
            self.directory.glob("*.json"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        records = [self.read(path.stem) for path in paths[:limit]]
        return sorted(records, key=lambda item: item["created_at"], reverse=True)

    def replace(self, scope: str, content: bytes | None) -> None:
        path = self.paths.target(scope)
        if path.is_symlink():
            raise ConfigOperationError(
                "symlink_target",
                "Edit the symlink target explicitly; managed replacement will not replace a configuration symlink.",
            )
        if content is None:
            path.unlink(missing_ok=True)
            if os.name != "nt" and path.parent.exists():
                descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        else:
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
            # Configurations may contain credentials; never broaden file access.
            atomic_write(path, content, mode=mode & 0o600)
