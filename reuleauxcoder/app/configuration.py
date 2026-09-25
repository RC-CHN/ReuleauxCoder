"""One configuration use-case service for RPC, CLI and model tools.

Persistent changes activate on the next core start. The running Agent is never
reconfigured halfway through its own configuration tool call.
"""

from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Callable
import uuid

import yaml

from reuleauxcoder import __version__
from reuleauxcoder.domain.config.management import ConfigActor, ConfigOperationError
from reuleauxcoder.infrastructure.persistence.config_transactions import (
    ConfigPaths,
    ConfigTransactionStore,
)
from reuleauxcoder.services.config.definition import (
    config_schema,
    pointer,
    redact,
    sensitive_path,
)
from reuleauxcoder.services.config.loader import ConfigLoader
from reuleauxcoder.services.config.probe import run_probe
from reuleauxcoder.services.config.validation import (
    json_document,
    parse_document,
    resolve_layers,
)

_TTL = 3600
_PROBE_TTL = 300


def _encode(content: bytes | None) -> str | None:
    return base64.b64encode(content).decode() if content is not None else None


def _decode(content: str | None) -> bytes | None:
    return base64.b64decode(content, validate=True) if content is not None else None


def _parts(path: str) -> tuple[str, ...]:
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or len(path) > 2048
        or re.search(r"~(?![01])", path)
    ):
        raise ConfigOperationError(
            "invalid_path", "Use an RFC 6901 JSON pointer to a configuration field."
        )
    parts = tuple(
        part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")
    )
    if not all(parts) or len(parts) > 20:
        raise ConfigOperationError(
            "invalid_path", "Configuration field path is empty or too deep."
        )
    return parts


def _diff(before, after, parts=()) -> list[dict]:
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            if key in before and key in after:
                result.extend(_diff(before[key], after[key], (*parts, key)))
            else:
                result.append(
                    {
                        "path": pointer((*parts, key)),
                        "before": redact(before.get(key), (*parts, key)),
                        "after": redact(after.get(key), (*parts, key)),
                        "removed": key not in after,
                    }
                )
        return result
    if type(before) is type(after) and before == after:
        return []
    return [
        {
            "path": pointer(parts),
            "before": redact(before, parts),
            "after": redact(after, parts),
            "removed": False,
        }
    ]


def _model_identity(config) -> str | None:
    if config is None:
        return None
    values = {name: getattr(config, name) for name in ConfigLoader._LLM_PARAM_FIELDS}
    values["responses"] = config.responses.to_dict()
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _secret_fields(value, parts=()) -> dict:
    if sensitive_path(parts):
        return {parts: value}
    if isinstance(value, dict):
        return {
            key: item
            for name, child in value.items()
            for key, item in _secret_fields(child, (*parts, name)).items()
        }
    return {}


class ConfigurationService:
    def __init__(
        self,
        store: ConfigTransactionStore,
        *,
        runtime: Callable[[], dict] | None = None,
        probe=run_probe,
        mutation_guard: Callable[[str], str | None] | None = None,
    ):
        self.store = store
        self.runtime = runtime
        self.probe = probe
        self.mutation_guard = mutation_guard

    def execute(
        self, operation: str, parameters: dict, *, actor: ConfigActor = "user"
    ) -> dict:
        if (
            operation
            not in (
                "describe",
                "inspect",
                "prepare",
                "validate",
                "apply",
                "history",
                "revert",
                "recover",
            )
            or "actor" in parameters
        ):
            raise ConfigOperationError(
                "invalid_operation",
                "Unknown configuration operation or caller override.",
            )
        if operation in ("prepare", "validate", "apply", "revert", "recover"):
            parameters = {**parameters, "actor": actor}
        try:
            return getattr(self, operation)(**parameters)
        except TypeError as error:
            raise ConfigOperationError(
                "invalid_parameters", "Invalid configuration operation parameters."
            ) from error

    @classmethod
    def for_workspace(
        cls,
        workspace: Path,
        *,
        home: Path | None = None,
        explicit: Path | None = None,
        runtime=None,
        mutation_guard=None,
    ):
        home = (home or Path.home()).absolute()
        return cls(
            ConfigTransactionStore(
                ConfigPaths(
                    home / ".rcoder/config.yaml",
                    workspace.absolute() / ".rcoder/config.yaml",
                    explicit.absolute() if explicit else None,
                ),
                home / ".rcoder/config-management",
            ),
            runtime=runtime,
            mutation_guard=mutation_guard,
        )

    def describe(self, section: str | None = None) -> dict:
        schema = config_schema()
        if section is not None:
            if not isinstance(section, str) or section not in schema["properties"]:
                raise ConfigOperationError(
                    "invalid_section", "Unknown configuration section."
                )
            schema = schema["properties"][section]
        return {
            "api_version": 1,
            "core_version": __version__,
            "schema": schema,
            "scopes": [name for name, _ in self.store.paths.layers()],
            "activation": "next_start",
            "candidate_ttl_seconds": _TTL,
            "operations": [
                "describe",
                "inspect",
                "prepare",
                "validate",
                "apply",
                "history",
                "revert",
                "recover",
            ],
            "checks": {
                "static": "File, field and merged configuration validation; no side effects.",
                "startup": "Isolated configuration and provider construction; no sessions, hooks or external tools.",
                "model": "One bounded model request without workspace data; may consume provider tokens.",
            },
            "model_restricted_sections": ["approval", "modes", "remote_exec", "meta"],
            "sensitive_values": "Values are redacted; models cannot submit credential fields.",
        }

    def _layers(self, contents):
        layers, issues = [], []
        for scope, content in contents.items():
            data, found = parse_document(content, scope)
            layers.append((scope, data))
            issues.extend(found)
        return layers, issues

    def inspect(self) -> dict:
        revision, contents = self.store.snapshot()
        layers, issues = self._layers(contents)
        config, found = resolve_layers(layers)
        issues.extend(found)
        return {
            "api_version": 1,
            "revision": revision,
            "sources": [
                {
                    "scope": scope,
                    "path": str(path),
                    "exists": contents[scope] is not None,
                    "values": redact(dict(layers)[scope]),
                }
                for scope, path in self.store.paths.layers()
            ],
            "next_start": redact(asdict(config))
            if config and not any(issue.severity == "error" for issue in issues)
            else None,
            "runtime": redact(self.runtime()) if self.runtime else None,
            "diagnostics": [issue.to_dict() for issue in issues],
            "valid": not any(issue.severity == "error" for issue in issues),
        }

    @staticmethod
    def _authorize(actor: ConfigActor, before: dict, after: dict):
        if actor not in ("user", "model"):
            raise ConfigOperationError("invalid_actor", "Unknown configuration caller.")
        if actor == "model":
            for section in ("approval", "modes", "remote_exec", "meta"):
                if before.get(section) != after.get(section):
                    raise ConfigOperationError(
                        "user_required",
                        "This configuration section may only be changed through a human management interface.",
                    )
            if _secret_fields(before) != _secret_fields(after):
                raise ConfigOperationError(
                    "user_required",
                    "Credential and process argument fields must be supplied through a human management interface.",
                )

    def prepare(
        self,
        *,
        scope="workspace",
        changes=None,
        document=None,
        base_revision=None,
        actor: ConfigActor = "user",
    ) -> dict:
        self.store.paths.target(scope)
        if (changes is None) == (document is None):
            raise ConfigOperationError(
                "invalid_request", "Provide exactly one of changes or document."
            )
        revision, contents = self.store.snapshot()
        if base_revision is not None and base_revision != revision:
            raise ConfigOperationError(
                "conflict",
                "Configuration changed; inspect it and prepare a new candidate.",
            )
        before, errors = parse_document(contents[scope], scope)
        if errors and document is None:
            raise ConfigOperationError(
                "invalid_document",
                "Repair this file with a complete replacement document.",
            )
        after = deepcopy(before)
        if document is not None:
            if actor == "model":
                raise ConfigOperationError(
                    "user_required",
                    "Models must submit field changes, not replacement documents.",
                )
            after = json_document(document)
        else:
            if not isinstance(changes, list) or not 1 <= len(changes) <= 64:
                raise ConfigOperationError(
                    "invalid_request", "Provide between 1 and 64 field changes."
                )
            for change in changes:
                if (
                    not isinstance(change, dict)
                    or set(change) not in ({"path", "value"}, {"path", "remove"})
                    or ("remove" in change and change["remove"] is not True)
                ):
                    raise ConfigOperationError(
                        "invalid_request",
                        "Each change requires path and either value or remove=true.",
                    )
                parts = _parts(change["path"])
                if actor == "model" and sensitive_path(parts):
                    raise ConfigOperationError(
                        "user_required",
                        "Models cannot submit credential or process argument values.",
                    )
                target = after
                for part in parts[:-1]:
                    target = target.setdefault(part, {})
                    if not isinstance(target, dict):
                        raise ConfigOperationError(
                            "invalid_path",
                            "Field path crosses a non-object; replace the enclosing field.",
                        )
                if change.get("remove"):
                    target.pop(parts[-1], None)
                else:
                    target[parts[-1]] = deepcopy(change["value"])
            after = json_document(after)
        self._authorize(actor, before, after)
        return self._prepare_content(scope, contents, revision, after, actor)

    def _prepare_content(self, scope, contents, revision, after, actor):
        before, _ = parse_document(contents[scope], scope)
        content = (
            yaml.safe_dump(after, allow_unicode=True, sort_keys=False).encode()
            if after is not None
            else None
        )
        candidate_contents = self.store.candidate_contents(contents, scope, content)
        layers, issues = self._layers(candidate_contents)
        config, found = resolve_layers(layers)
        issues.extend(found)
        old_layers, _ = self._layers(contents)
        old_config, _ = resolve_layers(old_layers)
        record = {
            "id": uuid.uuid4().hex,
            "scope": scope,
            "target_path": str(self.store.paths.target(scope)),
            "actor": actor,
            "base_revision": revision,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": time.time() + _TTL,
            "status": "prepared",
            "activation": "next_start",
            "before": _encode(contents[scope]),
            "after": _encode(content),
            "diff": _diff(before, after or {}),
            "diagnostics": [issue.to_dict() for issue in issues],
            "checks": [],
            "requires_model_probe": _model_identity(config)
            != _model_identity(old_config),
            "model_preview": redact(
                {
                    "model": config.model,
                    "provider": config.provider,
                    "request_mode": config.request_mode,
                    "base_url": config.base_url,
                }
            )
            if config
            else None,
        }
        with self.store.locked():
            if self.store.snapshot()[0] != revision:
                raise ConfigOperationError(
                    "conflict", "Configuration changed while preparing the candidate."
                )
            pending = 0
            for saved in self.store.records(limit=256):
                if saved["status"] == "prepared":
                    if saved["expires_at"] < time.time():
                        (self.store.directory / f"{saved['id']}.json").unlink(
                            missing_ok=True
                        )
                    else:
                        pending += 1
            if pending >= 128:
                raise ConfigOperationError(
                    "candidate_limit",
                    "Too many unexpired configuration candidates; apply existing candidates or wait for expiry.",
                )
            self.store.write(record)
        return self._public(record)

    def _candidate(self, change_id, actor):
        record = self.store.read(change_id)
        if actor == "model" and record["actor"] != "model":
            raise ConfigOperationError(
                "user_required",
                "Models cannot use changes prepared by a human interface.",
            )
        if record["status"] == "prepared" and record["expires_at"] < time.time():
            raise ConfigOperationError(
                "expired", "Configuration candidate expired; prepare it again."
            )
        before, _ = parse_document(_decode(record["before"]), record["scope"])
        after, _ = parse_document(_decode(record["after"]), record["scope"])
        self._authorize(actor, before, after)
        return record

    def _check_revision(self, record):
        revision, contents = self.store.snapshot()
        if revision != record["base_revision"]:
            raise ConfigOperationError(
                "conflict",
                "Configuration changed after preparation; prepare a new candidate.",
            )
        return contents

    def review(self, change_id: str, *, actor: ConfigActor = "user") -> dict:
        record = self._candidate(change_id, actor)
        self._check_revision(record)
        return self._public(record)

    def validate(
        self, *, change_id=None, checks=None, actor: ConfigActor = "user"
    ) -> dict:
        checks = ["static"] if checks is None else checks
        if (
            not isinstance(checks, list)
            or any(check not in ("static", "startup", "model") for check in checks)
            or len(checks) > 3
        ):
            raise ConfigOperationError(
                "invalid_check", "Checks must be static, startup or model."
            )
        record = self._candidate(change_id, actor) if change_id else None
        contents = self._check_revision(record) if record else self.store.snapshot()[1]
        if record:
            if record["status"] != "prepared":
                raise ConfigOperationError(
                    "invalid_state", "Only prepared changes may be validated."
                )
            contents = self.store.candidate_contents(
                contents, record["scope"], _decode(record["after"])
            )
        layers, issues = self._layers(contents)
        _, found = resolve_layers(layers)
        issues.extend(found)
        valid = not any(issue.severity == "error" for issue in issues)
        results = [
            {
                "check": "static",
                "status": "passed" if valid else "failed",
                "code": "ok" if valid else "invalid_config",
            }
        ]
        if valid:
            for check in dict.fromkeys(checks):
                if check != "static":
                    results.append(self.probe(layers, check))
        if record:
            with self.store.locked():
                self._check_revision(record)
                latest = self._candidate(change_id, actor)
                if latest["status"] != "prepared":
                    raise ConfigOperationError(
                        "invalid_state", "Configuration candidate was already applied."
                    )
                previous = {item["check"]: item for item in latest["checks"]}
                previous.update(
                    {
                        item["check"]: {**item, "checked_at": time.time()}
                        for item in results
                    }
                )
                latest["checks"] = list(previous.values())
                latest["diagnostics"] = [issue.to_dict() for issue in issues]
                self.store.write(latest)
        return {
            "change_id": change_id,
            "valid": valid,
            "checks": results,
            "diagnostics": [issue.to_dict() for issue in issues],
        }

    def apply(
        self, *, change_id, allow_unverified_model=False, actor: ConfigActor = "user"
    ) -> dict:
        if type(allow_unverified_model) is not bool or (
            allow_unverified_model and actor != "user"
        ):
            raise ConfigOperationError(
                "user_required",
                "Only a human interface may explicitly save an unverified model configuration.",
            )
        record = self._candidate(change_id, actor)
        if record["status"] == "committing":
            with self.store.locked():
                record = self._candidate(change_id, actor)
                revision, contents = self.store.snapshot()
                current = contents[record["scope"]]
                if current == _decode(record["after"]):
                    record.update(status="applied", revision=revision)
                elif revision == record["base_revision"]:
                    record["status"] = "prepared"
                else:
                    raise ConfigOperationError(
                        "conflict",
                        "Interrupted change conflicts with later edits; prepare a repair.",
                    )
                self.store.write(record)
        if record["status"] == "applied":
            return self._public(record)
        # Provider construction is isolated and cannot block the commit lock.
        self.validate(change_id=change_id, checks=["startup"], actor=actor)
        with self.store.locked():
            record = self._candidate(change_id, actor)
            self._check_revision(record)
            if self.mutation_guard and self.mutation_guard(
                str(self.store.paths.target(record["scope"]).resolve())
            ):
                raise ConfigOperationError(
                    "unsaved_document",
                    "Save or discard unsaved editor changes before applying this configuration.",
                )
            passed = {
                item["check"]
                for item in record["checks"]
                if item["status"] == "passed"
                and time.time() - item["checked_at"] < _PROBE_TTL
            }
            required = {"static", "startup"} | (
                {"model"}
                if record["requires_model_probe"] and not allow_unverified_model
                else set()
            )
            if not required <= passed:
                raise ConfigOperationError(
                    "validation_required",
                    "Candidate requires successful static/startup checks and, when changing the active model, a recent model connection test.",
                )
            record["status"] = "committing"
            record["model_verified"] = (
                "model" in passed if record["requires_model_probe"] else None
            )
            self.store.write(record)
            self.store.replace(record["scope"], _decode(record["after"]))
            record["status"] = "applied"
            record["applied_at"] = datetime.now(timezone.utc).isoformat()
            record["revision"] = self.store.snapshot()[0]
            self.store.write(record)
        return self._public(record)

    def history(self, *, limit=20) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ConfigOperationError(
                "invalid_limit", "History limit must be between 1 and 100."
            )
        return {
            "changes": [self._public(record) for record in self.store.records(limit)]
        }

    def revert(self, *, change_id, actor: ConfigActor = "user") -> dict:
        record = self._candidate(change_id, actor)
        if record["status"] not in ("applied", "committing"):
            raise ConfigOperationError(
                "invalid_state", "Only applied or interrupted changes may be reverted."
            )
        revision, contents = self.store.snapshot()
        if contents[record["scope"]] != _decode(record["after"]):
            raise ConfigOperationError(
                "conflict",
                "The changed file was edited again; inspect and prepare a targeted repair.",
            )
        before, errors = parse_document(_decode(record["before"]), record["scope"])
        if errors:
            raise ConfigOperationError(
                "invalid_backup",
                "The previous file was invalid; prepare a valid repair document instead.",
            )
        after, _ = parse_document(contents[record["scope"]], record["scope"])
        self._authorize(actor, after, before)
        return self._prepare_content(
            record["scope"],
            contents,
            revision,
            before if record["before"] is not None else None,
            actor,
        )

    def recover(
        self, *, change_id, base_revision, side="before", actor: ConfigActor = "user"
    ) -> dict:
        """Prepare an explicitly selected backup even when current YAML is broken."""
        if actor != "user":
            raise ConfigOperationError(
                "user_required", "Recovery must be requested through a human interface."
            )
        if side not in ("before", "after"):
            raise ConfigOperationError(
                "invalid_side", "Recovery side must be before or after."
            )
        record = self.store.read(change_id)
        if record["status"] not in ("applied", "committing"):
            raise ConfigOperationError(
                "invalid_state", "Recovery requires an applied or interrupted change."
            )
        revision, contents = self.store.snapshot()
        if base_revision != revision:
            raise ConfigOperationError(
                "conflict",
                "Configuration changed; inspect before selecting a recovery snapshot.",
            )
        content = _decode(record[side])
        document, errors = parse_document(content, record["scope"])
        if errors:
            raise ConfigOperationError(
                "invalid_backup", "Selected backup contains invalid YAML."
            )
        return self._prepare_content(
            record["scope"],
            contents,
            revision,
            document if content is not None else None,
            actor,
        )

    @staticmethod
    def _public(record):
        return {
            key: value
            for key, value in record.items()
            if key not in ("before", "after", "actor")
        }
