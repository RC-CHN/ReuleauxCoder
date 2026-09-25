"""Read configuration files and compare revisions; never write or create state."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from reuleauxcoder.domain.config.management import ConfigOperationError

MAX_CONFIG_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ConfigPaths:
    user: Path
    workspace: Path
    explicit: Path | None = None

    def __post_init__(self):
        for name in ("user", "workspace", "explicit"):
            if (path := getattr(self, name)) is not None:
                object.__setattr__(self, name, path.absolute())

    def layers(self):
        layers = (("user", self.user), ("workspace", self.workspace))
        return (*layers, ("explicit", self.explicit)) if self.explicit else layers

    def target(self, scope: str) -> Path:
        for name, path in self.layers():
            if name == scope:
                return path
        raise ConfigOperationError("invalid_scope", "Select an available configuration source.")


class ConfigFiles:
    def __init__(self, paths: ConfigPaths):
        self.paths = paths

    def snapshot(self) -> tuple[str, dict[str, bytes | None]]:
        contents, digest = {}, hashlib.sha256()
        for scope, path in self.paths.layers():
            try:
                with path.open("rb") as stream:
                    content = stream.read(MAX_CONFIG_BYTES + 1)
            except FileNotFoundError:
                content = None
            except OSError as error:
                raise ConfigOperationError("unreadable_config", f"Cannot read the {scope} configuration file.") from error
            if content is not None and len(content) > MAX_CONFIG_BYTES:
                raise ConfigOperationError("too_large", f"The {scope} configuration exceeds 1 MiB.")
            contents[scope] = content
            digest.update(json.dumps([scope, str(path), str(path.resolve()), content is not None]).encode())
            digest.update(hashlib.sha256(content or b"").digest())
        return digest.hexdigest(), contents

    def with_documents(self, contents: dict, documents: list | None) -> dict:
        """Validate editor buffers in memory, including aliases of the same file."""
        if documents is None:
            return contents
        if not isinstance(documents, list) or not 1 <= len(documents) <= 3:
            raise ConfigOperationError("invalid_documents", "Supply one to three configuration buffers.")
        replacements = {}
        for item in documents:
            if not isinstance(item, dict) or set(item) != {"scope", "content"} or not isinstance(item["content"], str):
                raise ConfigOperationError("invalid_documents", "Each buffer requires scope and YAML text content.")
            path = self.paths.target(item["scope"]).resolve()
            content = item["content"].encode("utf-8")
            if len(content) > MAX_CONFIG_BYTES:
                raise ConfigOperationError("too_large", "Configuration buffer exceeds 1 MiB.")
            if path in replacements and replacements[path] != content:
                raise ConfigOperationError("invalid_documents", "Conflicting buffers refer to the same configuration file.")
            replacements[path] = content
        return {scope: replacements.get(path.resolve(), contents[scope]) for scope, path in self.paths.layers()}
