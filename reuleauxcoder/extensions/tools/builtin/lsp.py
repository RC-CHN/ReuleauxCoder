"""Active LSP tools — goToDefinition, findReferences, documentSymbol.

A single ``lsp`` tool dispatches on *operation*; all operations share the
same input shape (filePath / line / character).  The real LSP requests are
sent through ``LspManager.send_request_sync()`` which bridges to the worker
thread.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from reuleauxcoder.extensions.lsp.client import (
    LspClientError,
    LspFailureFacts,
    LspRequestCancelled,
    LspRequestTimedOut,
    render_lsp_failure,
)
from reuleauxcoder.extensions.lsp.diagnostic_outcomes import (
    DiagnosticOutcome,
    DiagnosticOutcomeStatus,
    render_diagnostic_outcome,
)
from reuleauxcoder.extensions.lsp.diagnostics import render_blocks
from reuleauxcoder.extensions.lsp.manager import (
    LspManager,
    LspStatusSnapshot,
    LspStatusTransportSnapshot,
    LspTransportState,
)
from reuleauxcoder.extensions.lsp.tool_helpers import (
    format_document_symbols,
    format_locations,
    format_references,
    resolve_file_path,
    validate_position,
)
from reuleauxcoder.extensions.tools.base import InterruptMode, Tool
from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)

# ── helpers ───────────────────────────────────────────────────────────────

_OPERATIONS = frozenset({"goToDefinition", "findReferences", "documentSymbol"})

# ── tools ──────────────────────────────────────────────────────────────────


class _BoundLspTool(Tool):
    """Share instance-scoped manager binding without a process-global registry."""

    parallel_safe: ClassVar[bool] = True
    effect_class: ClassVar[str] = "read_only_internal"

    def __init__(self, backend: Any = None, *, lsp_manager: LspManager | None = None):
        super().__init__(backend=backend)
        self.lsp_manager = lsp_manager

    def bind_lsp_manager(self, manager: LspManager | None) -> None:
        self.lsp_manager = manager

    def clone_for_scope(self, scope: str) -> "_BoundLspTool":
        clone_backend = getattr(self.backend, "clone_for_scope", None)
        backend = clone_backend(scope) if callable(clone_backend) else self.backend
        return type(self)(backend=backend, lsp_manager=self.lsp_manager)


class LspTool(_BoundLspTool):
    """Single tool that dispatches LSP operations.

    Supported operations:
    - goToDefinition: Find where a symbol at filePath:line:character is defined
    - findReferences: Find all references to the symbol at the position
    - documentSymbol: List all symbols (classes, methods, etc.) in the file
    """

    name: ClassVar[str] = "lsp"
    interrupt_mode: ClassVar[InterruptMode] = InterruptMode.CANCEL_WITH_PARTIAL
    description: ClassVar[str] = (
        "Interact with Language Server Protocol (LSP) servers for code intelligence.\n"
        "\n"
        "Supported operations:\n"
        "- goToDefinition: Find where a symbol is defined\n"
        "- findReferences: Find all references to a symbol across the codebase\n"
        "- documentSymbol: Get all symbols (functions, classes, variables) in a file\n"
        "\n"
        "All operations require:\n"
        "- filePath: The absolute or relative path to the file\n"
        "- line: The line number (1-based, as shown in editors)\n"
        "- character: The character offset (1-based, as shown in editors)\n"
        "\n"
        "Note: An LSP server must be available for the file type. "
        "If no server is running, an error will be returned."
    )

    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["goToDefinition", "findReferences", "documentSymbol"],
                "description": "The LSP operation to perform.",
            },
            "filePath": {
                "type": "string",
                "description": "The absolute or relative path to the file.",
            },
            "line": {
                "type": "integer",
                "description": "The line number (1-based, as shown in editors).",
            },
            "character": {
                "type": "integer",
                "description": "The character offset (1-based, as shown in editors).",
            },
        },
        "required": ["operation", "filePath", "line", "character"],
    }

    def execute(
        self,
        *,
        operation: str,
        filePath: str,
        line: int,
        character: int,
    ) -> ToolOutcome:
        # 1. Validate operation
        if operation not in _OPERATIONS:
            return _lsp_failure(
                f"Unknown operation: {operation}. "
                f"Supported: {', '.join(sorted(_OPERATIONS))}."
            )

        # 2. Resolve file and language
        try:
            lang, path = resolve_file_path(filePath)
        except FileNotFoundError as e:
            return _lsp_failure(str(e))
        except ValueError as e:
            return _lsp_failure(str(e))

        # 3. Position validation (skip for documentSymbol — line/char are
        #    ignored by the server anyway, they only exist to keep the schema
        #    uniform)
        if operation != "documentSymbol":
            try:
                validate_position(path, line, character)
            except ValueError as e:
                return _lsp_failure(str(e))

        # 4. Get LSP manager
        manager = self.lsp_manager
        if manager is None:
            return _lsp_failure("LSP infrastructure is not available")

        # 5. Build LSP method + params
        if operation == "goToDefinition":
            method = "textDocument/definition"
            params = _position_params(path, line, character)
        elif operation == "findReferences":
            method = "textDocument/references"
            params = {
                **_position_params(path, line, character),
                "context": {"includeDeclaration": True},
            }
        elif operation == "documentSymbol":
            method = "textDocument/documentSymbol"
            params = {"textDocument": {"uri": path.resolve().as_uri()}}
        else:
            return _lsp_failure(f"Unknown operation: {operation}")

        # 6. Send request through worker thread
        try:
            raw = manager.send_request_sync(
                path,
                method,
                params,
                cancellation=self.current_cancellation_signal(),
            )
        except LspRequestCancelled as e:
            return _lsp_failure(
                "LSP request cancelled; " + _safe_failure_message("request", e),
                status=ToolOutcomeStatus.CANCELLED,
                error_kind=ToolErrorKind.INTERRUPTED,
            )
        except LspRequestTimedOut as e:
            return _lsp_failure(
                "LSP request timed out; " + _safe_failure_message("request", e),
                status=ToolOutcomeStatus.TIMED_OUT,
                error_kind=ToolErrorKind.INTERRUPTED,
            )
        except LspClientError as e:
            return _lsp_failure(_safe_failure_message("request", e))
        except Exception as e:
            return _lsp_failure(_safe_failure_message("request", e))

        # 7. Format result
        try:
            if operation == "goToDefinition":
                return _lsp_success(
                    operation, format_locations(raw, file_path=str(path))
                )
            if operation == "findReferences":
                return _lsp_success(
                    operation, format_references(raw, file_path=str(path))
                )
            if operation == "documentSymbol":
                return _lsp_success(
                    operation, format_document_symbols(raw, file_path=str(path))
                )
        except Exception as e:
            return _lsp_failure(_safe_failure_message("format", e))

        return _lsp_failure(f"Unknown operation: {operation}")


class LspStatusTool(_BoundLspTool):
    """Report current lazy LSP state without probing or starting a server."""

    name: ClassVar[str] = "lsp_status"
    description: ClassVar[str] = (
        "Show configured LSP languages, current transport states, and bounded "
        "availability/diagnostic counters. This is observational: it does not "
        "probe PATH or start a language server."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def execute(self) -> ToolOutcome:
        manager = self.lsp_manager
        if manager is None:
            payload = {
                "manager_bound": False,
                "state": "unavailable",
            }
            return ToolOutcome(
                summary="LSP infrastructure is unavailable",
                content=json.dumps(payload, sort_keys=True, separators=(",", ":")),
                metadata={"operation": "status", "manager_bound": False},
            )

        snapshot = _validated_status_snapshot(manager.status_snapshot())

        payload = {
            "availability_metrics": dict(snapshot.availability_metrics),
            "configured_languages": list(snapshot.configured_languages),
            "diagnostic_batch_metrics": dict(snapshot.diagnostic_batch_metrics),
            "enabled": snapshot.enabled,
            "manager_bound": True,
            "transports": [
                {
                    "error_phase": transport.error_phase,
                    "error_type": transport.error_type,
                    "generation": transport.generation,
                    "language": transport.language,
                    "protocol_error_code": transport.protocol_error_code,
                    "retry_scheduled": transport.retry_scheduled,
                    "return_code": transport.return_code,
                    "root_hash": transport.root_hash,
                    "state": transport.state.value,
                }
                for transport in snapshot.transports
            ],
        }
        return ToolOutcome(
            summary=(
                f"LSP status: {len(snapshot.configured_languages)} configured "
                f"language(s), {len(snapshot.transports)} transport(s)"
            ),
            content=json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            metadata={
                "operation": "status",
                "manager_bound": True,
                "enabled": snapshot.enabled,
                "configured_language_count": len(snapshot.configured_languages),
                "transport_count": len(snapshot.transports),
            },
        )


class LspDiagnosticsTool(_BoundLspTool):
    """Request the current diagnostics for one document explicitly."""

    name: ClassVar[str] = "lsp_diagnostics"
    interrupt_mode: ClassVar[InterruptMode] = InterruptMode.CANCEL_WITH_PARTIAL
    description: ClassVar[str] = (
        "Request current LSP diagnostics for a file. The document is synced "
        "before the server's pull result or next publish is observed. A timeout "
        "is reported explicitly and is never treated as a clean diagnostic result."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "filePath": {
                "type": "string",
                "description": "The absolute or workspace-relative file path.",
            },
        },
        "required": ["filePath"],
        "additionalProperties": False,
    }

    def execute(self, *, filePath: str) -> ToolOutcome:
        try:
            _language, path = resolve_file_path(filePath)
        except (FileNotFoundError, ValueError) as error:
            return _lsp_failure(str(error))

        manager = self.lsp_manager
        if manager is None:
            return _lsp_failure("LSP infrastructure is not available")

        try:
            outcome = manager.request_diagnostics_sync(
                path,
                cancellation=self.current_cancellation_signal(),
            )
        except LspRequestCancelled as error:
            return _lsp_failure(
                "LSP diagnostics request cancelled; "
                + _safe_failure_message("diagnostics_wait", error),
                status=ToolOutcomeStatus.CANCELLED,
                error_kind=ToolErrorKind.INTERRUPTED,
            )
        except LspRequestTimedOut as error:
            return _lsp_failure(
                "LSP diagnostics request timed out; "
                + _safe_failure_message("diagnostics_wait", error),
                status=ToolOutcomeStatus.TIMED_OUT,
                error_kind=ToolErrorKind.INTERRUPTED,
            )
        except LspClientError as error:
            return _lsp_failure(_safe_failure_message("diagnostics", error))
        except Exception as error:
            return _lsp_failure(_safe_failure_message("diagnostics", error))

        try:
            result = _diagnostic_tool_outcome(outcome)
        except Exception as error:
            return _lsp_failure(_safe_failure_message("format", error))

        acknowledged = manager.acknowledge_diagnostic_batch(
            outcome.batch_id,
            consumer_id="lsp_diagnostics",
        )
        return result.with_metadata(acknowledged=acknowledged)


def _diagnostic_tool_outcome(outcome: DiagnosticOutcome) -> ToolOutcome:
    metadata: dict[str, Any] = {
        "operation": "diagnostics",
        "diagnostic_status": outcome.status.value,
        "batch_id": outcome.batch_id,
    }
    if outcome.is_published:
        assert outcome.block is not None
        metadata.update(
            {
                "diagnostic_count": len(outcome.block.items),
                "document_version": outcome.document_version,
                "diagnostic_generation": outcome.diagnostic_generation,
            }
        )
        rendered = render_blocks(
            [outcome.block],
            max_diagnostics=len(outcome.block.items),
            include_warnings=True,
        )
        if rendered is None:
            rendered = (
                "LSP diagnostics published clean "
                f"(file={outcome.block.file_path}, "
                f"document_version={outcome.document_version}, "
                f"diagnostic_generation={outcome.diagnostic_generation})"
            )
        return ToolOutcome(
            summary=(
                "LSP diagnostics published: "
                f"{len(outcome.block.items)} item(s)"
            ),
            content=rendered,
            metadata=metadata,
        )

    rendered_failure = render_diagnostic_outcome(outcome) or (
        f"LSP diagnostics ended (status={outcome.status.value})"
    )
    if outcome.status is DiagnosticOutcomeStatus.TIMED_OUT:
        status = ToolOutcomeStatus.TIMED_OUT
        error_kind = ToolErrorKind.INTERRUPTED
    elif outcome.status is DiagnosticOutcomeStatus.CANCELLED:
        status = ToolOutcomeStatus.CANCELLED
        error_kind = ToolErrorKind.INTERRUPTED
    else:
        status = ToolOutcomeStatus.FAILED
        error_kind = ToolErrorKind.EXECUTION
    return ToolOutcome(
        status=status,
        summary=rendered_failure.splitlines()[0][:160],
        content=rendered_failure,
        error_kind=error_kind,
        metadata=metadata,
    )


def _lsp_success(operation: str, content: str) -> ToolOutcome:
    first_line = next(
        (line.strip().rstrip(":") for line in content.splitlines() if line.strip()),
        f"{operation} completed",
    )
    return ToolOutcome(
        summary=first_line[:160],
        content=content,
        metadata={"operation": operation, "effect_class": "read"},
    )


def _lsp_failure(
    message: str,
    *,
    status: ToolOutcomeStatus = ToolOutcomeStatus.FAILED,
    error_kind: ToolErrorKind = ToolErrorKind.EXECUTION,
) -> ToolOutcome:
    return ToolOutcome(
        status=status,
        summary=message.splitlines()[0][:160],
        content=message,
        error_kind=error_kind,
        metadata={"effect_class": "read"},
    )


def _safe_failure_message(
    phase: str,
    error: BaseException,
) -> str:
    """Render the frozen causal snapshot, or a minimal local-only fallback."""
    try:
        facts = getattr(error, "failure_facts", None)
    except Exception:
        facts = None
    if isinstance(facts, LspFailureFacts):
        return render_lsp_failure(
            facts,
            fallback_phase=phase,
            fallback_error_type=type(error).__name__,
        )
    safe_phase = _safe_failure_fact(phase, "unknown")
    safe_error_type = _safe_failure_fact(type(error).__name__, "Error")
    try:
        protocol_code = getattr(error, "code", None)
    except Exception:
        protocol_code = None
    if not (type(protocol_code) is int and -(2**31) <= protocol_code <= 2**31 - 1):
        protocol_code = None
    code_fact = (
        f", protocol_error_code={protocol_code}" if protocol_code is not None else ""
    )
    return (
        "LSP request failed "
        f"(phase={safe_phase}, error_type={safe_error_type}{code_fact})"
    )


def _safe_failure_fact(value: str, fallback: str) -> str:
    safe = "".join(
        character
        for character in value
        if character.isascii() and (character.isalnum() or character in {"_", "-", "."})
    )[:64]
    return safe or fallback


def _validated_status_snapshot(value: object) -> LspStatusSnapshot:
    if type(value) is not LspStatusSnapshot or type(value.enabled) is not bool:
        raise TypeError("LSP status provider returned an invalid projection")
    languages = value.configured_languages
    if (
        not isinstance(languages, tuple)
        or any(not _is_status_fact(language) for language in languages)
        or len(set(languages)) != len(languages)
        or tuple(sorted(languages)) != languages
    ):
        raise TypeError("LSP status provider returned an invalid projection")

    transports = value.transports
    if not isinstance(transports, tuple):
        raise TypeError("LSP status provider returned an invalid projection")
    identities: set[tuple[str, str]] = set()
    for transport in transports:
        if not _valid_status_transport(transport):
            raise TypeError("LSP status provider returned an invalid projection")
        identity = (transport.language, transport.root_hash)
        if identity in identities:
            raise TypeError("LSP status provider returned an invalid projection")
        identities.add(identity)
    if (
        tuple(sorted(transports, key=lambda item: (item.language, item.root_hash)))
        != transports
    ):
        raise TypeError("LSP status provider returned an invalid projection")

    if not _valid_counter_items(value.availability_metrics) or not _valid_counter_items(
        value.diagnostic_batch_metrics
    ):
        raise TypeError("LSP status provider returned an invalid projection")
    return value


def _valid_status_transport(value: object) -> bool:
    if type(value) is not LspStatusTransportSnapshot:
        return False
    return (
        _is_status_fact(value.language)
        and len(value.root_hash) == 12
        and all(character in "0123456789abcdef" for character in value.root_hash)
        and isinstance(value.state, LspTransportState)
        and type(value.generation) is int
        and value.generation >= 0
        and (value.error_phase is None or _is_status_fact(value.error_phase))
        and (value.error_type is None or _is_status_fact(value.error_type))
        and _is_status_code(value.protocol_error_code)
        and _is_status_code(value.return_code)
        and type(value.retry_scheduled) is bool
    )


def _valid_counter_items(value: object) -> bool:
    if not isinstance(value, tuple) or len(value) > 64:
        return False
    keys: list[str] = []
    for item in value:
        if not isinstance(item, tuple) or len(item) != 2:
            return False
        key, count = item
        if not _is_status_fact(key) or type(count) is not int or count < 0:
            return False
        keys.append(key)
    return len(set(keys)) == len(keys) and tuple(sorted(value)) == value


def _is_status_fact(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 64
        and value.isascii()
        and all(
            character.isalnum() or character in {"_", "-", "."} for character in value
        )
    )


def _is_status_code(value: object) -> bool:
    return value is None or (type(value) is int and -(2**31) <= value <= 2**31 - 1)


# ── internal helpers ───────────────────────────────────────────────────────


def _position_params(path: Path, line: int, character: int) -> dict[str, Any]:
    """Build ``textDocument`` + ``position`` params for position-based LSP methods."""
    return {
        "textDocument": {"uri": path.resolve().as_uri()},
        "position": {
            "line": line - 1,  # 1-based → 0-based
            "character": character - 1,
        },
    }
