"""Small management adapter usable with or without an initialized Agent."""

from reuleauxcoder.app.configuration import ConfigurationService
from reuleauxcoder.domain.config.management import ConfigOperationError
from reuleauxcoder.infrastructure.rpc.peer import RpcError

OPERATIONS = (
    "describe",
    "inspect",
    "prepare",
    "validate",
    "apply",
    "history",
    "revert",
    "recover",
)


def dispatch(
    service: ConfigurationService, operation: str, parameters: dict, *, actor="user"
) -> dict:
    return service.execute(operation, parameters, actor=actor)


def bind_configuration(peer, service: ConfigurationService, *, writable=True) -> None:
    def handler(operation):
        def invoke(**parameters):
            try:
                if not writable and operation not in ("describe", "inspect", "history"):
                    raise ConfigOperationError(
                        "local_only",
                        "Configuration changes require a workspace-host management connection.",
                    )
                return dispatch(service, operation, parameters)
            except ConfigOperationError as error:
                raise RpcError(-32020, str(error), {"code": error.code}) from error
            except (ValueError, OSError) as error:
                raise RpcError(
                    -32020,
                    "Configuration operation failed without changing the active runtime.",
                    {"code": "operation_failed"},
                ) from error

        return invoke

    for operation in OPERATIONS:
        peer.methods[f"config.{operation}"] = handler(operation)
