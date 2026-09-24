"""Connection-owned unsaved editor state, independent of approval policy."""

import os
from pathlib import Path
import threading

from reuleauxcoder.infrastructure.rpc.peer import RpcError


class EditorDocuments:
    def __init__(self):
        self._lock = threading.Lock()
        self._revision = -1
        self._dirty: frozenset[str] = frozenset()

    def update(self, revision, paths):
        if (
            type(revision) is not int or revision < 0
            or not isinstance(paths, list) or len(paths) > 4096
            or any(not isinstance(path, str) or len(path) > 4096 or "\0" in path or not os.path.isabs(path) for path in paths)
        ):
            raise RpcError(-32602, "Invalid editor document state")
        normalized = frozenset(os.path.normcase(str(Path(path).resolve())) for path in paths)
        with self._lock:
            if revision > self._revision:
                self._revision, self._dirty = revision, normalized
            return self._revision

    def guard(self, path: str) -> str | None:
        with self._lock:
            dirty = os.path.normcase(path) in self._dirty
        if dirty:
            return f"Unsaved editor changes in {path}. Ask the user to save or resolve them before proposing this edit again."
        return None
