"""Read-only configuration description, inspection and checks for every frontend."""

from dataclasses import asdict
from pathlib import Path

from reuleauxcoder import __version__
from reuleauxcoder.domain.config.management import ConfigOperationError
from reuleauxcoder.infrastructure.persistence.config_files import ConfigFiles, ConfigPaths
from reuleauxcoder.services.config.definition import config_schema, redact
from reuleauxcoder.services.config.probe import run_probe
from reuleauxcoder.services.config.targets import model_targets
from reuleauxcoder.services.config.validation import parse_document, resolve_layers

OPERATIONS = ("describe", "inspect", "check")
API_VERSION = 2


class ConfigurationService:
    def __init__(self, files: ConfigFiles, *, runtime=None, probe=run_probe):
        self.files, self.runtime, self.probe = files, runtime, probe

    @classmethod
    def for_workspace(cls, workspace: Path, *, home=None, explicit=None, runtime=None):
        return cls(ConfigFiles(ConfigPaths(
            (home or Path.home()) / ".rcoder/config.yaml",
            workspace / ".rcoder/config.yaml", explicit,
        )), runtime=runtime)

    def execute(self, operation: str, parameters: dict) -> dict:
        if operation not in OPERATIONS:
            raise ConfigOperationError("invalid_operation", "Configuration supports describe, inspect and check only.")
        try:
            return getattr(self, operation)(**parameters)
        except TypeError as error:
            raise ConfigOperationError("invalid_parameters", "Invalid configuration operation parameters.") from error

    def describe(self, section=None) -> dict:
        schema = config_schema()
        if section is not None:
            if not isinstance(section, str) or section not in schema["properties"]:
                raise ConfigOperationError("invalid_section", "Unknown configuration section.")
            schema = schema["properties"][section]
        return {
            "api_version": API_VERSION, "core_version": __version__,
            "operations": list(OPERATIONS), "capabilities": ["buffer_checks", "profile_probes"],
            "schema": schema, "scopes": [name for name, _ in self.files.paths.layers()],
            "activation": "next_start",
            "checks": {
                "static": "Shared startup validation; no side effects.",
                "startup": "Isolated provider construction; no Agent, sessions, hooks or external tools.",
                "model": "Optional bounded text request per selected profile; may consume provider tokens.",
            },
        }

    @staticmethod
    def _resolve(contents):
        layers, issues = [], []
        for scope, content in contents.items():
            data, found = parse_document(content, scope)
            layers.append((scope, data))
            issues.extend(found)
        if issues:
            return layers, None, issues
        config, issues = resolve_layers(layers)
        return layers, config, issues

    def inspect(self) -> dict:
        revision, contents = self.files.snapshot()
        layers, config, issues = self._resolve(contents)
        valid = not any(issue.severity == "error" for issue in issues)
        return {
            "api_version": API_VERSION, "revision": revision, "valid": valid,
            "sources": [{"scope": scope, "path": str(path), "exists": contents[scope] is not None,
                         "values": redact(dict(layers)[scope])} for scope, path in self.files.paths.layers()],
            "next_start": redact(asdict(config)) if valid and config else None,
            "runtime": redact(self.runtime()) if self.runtime else None,
            "model_targets": redact(model_targets(config)) if valid else [],
            "diagnostics": [issue.to_dict() for issue in issues],
        }

    def check(self, *, checks=None, profiles=None, documents=None, base_revision=None) -> dict:
        checks = ["static", "startup"] if checks is None else checks
        if (not isinstance(checks, list) or not 1 <= len(checks) <= 3
                or any(item not in ("static", "startup", "model") for item in checks)):
            raise ConfigOperationError("invalid_checks", "Choose static, startup or model checks.")
        if profiles is not None and ("model" not in checks or not isinstance(profiles, list)
                or not 1 <= len(profiles) <= 8 or any(not isinstance(name, str) for name in profiles)):
            raise ConfigOperationError("invalid_profile", "Select one to eight profiles for a model check.")
        revision, contents = self.files.snapshot()
        if base_revision is not None and base_revision != revision:
            raise ConfigOperationError("conflict", "Configuration changed on disk. Check again.")
        layers, config, issues = self._resolve(self.files.with_documents(contents, documents))
        valid = not any(issue.severity == "error" for issue in issues)
        results = [{"check": "static", "status": "passed" if valid else "failed", "code": "ok" if valid else "invalid_config"}]
        if valid:
            names = profiles or [config.active_main_model_profile]
            if "model" in checks and any(name not in config.model_profiles for name in names):
                raise ConfigOperationError("invalid_profile", "Selected model profile does not exist.")
            for check in dict.fromkeys(checks):
                if check == "startup":
                    results.append(self.probe(layers, check))
                elif check == "model":
                    for name in dict.fromkeys(names):
                        results.append({**self.probe(layers, check, profile=name), "profile": name,
                                        "coverage": "text_request_with_configured_parameters",
                                        "not_checked": ["tools", "images", "mcp", "lsp"]})
        if self.files.snapshot()[0] != revision:
            raise ConfigOperationError("conflict", "Configuration changed during the check. Check again.")
        return {"revision": revision, "valid": valid, "buffer_check": bool(documents), "checks": results,
                "model_targets": redact(model_targets(config)) if valid else [],
                "diagnostics": [issue.to_dict() for issue in issues]}
