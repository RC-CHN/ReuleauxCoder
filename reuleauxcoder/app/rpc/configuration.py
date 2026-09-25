"""Read-only configuration adapter, usable without an Agent."""

from reuleauxcoder.app.configuration import ConfigurationService, OPERATIONS
from reuleauxcoder.domain.config.management import ConfigOperationError
from reuleauxcoder.infrastructure.rpc.peer import RpcError


def dispatch(service: ConfigurationService, operation: str, parameters: dict) -> dict:
    return service.execute(operation, parameters)


def bind_configuration(peer, service: ConfigurationService, *, allow_checks=True) -> None:
    def handler(operation):
        def invoke(**parameters):
            try:
                if operation == "check" and not allow_checks:
                    raise ConfigOperationError("local_only", "Configuration checks require a workspace-host connection.")
                return dispatch(service, operation, parameters)
            except ConfigOperationError as error:
                raise RpcError(-32020, str(error), {"code": error.code}) from error
            except (ValueError, OSError):
                raise RpcError(-32020, "Configuration could not be inspected or checked.", {"code": "operation_failed"}) from None
        return invoke

    for operation in OPERATIONS:
        peer.methods[f"config.{operation}"] = handler(operation)
