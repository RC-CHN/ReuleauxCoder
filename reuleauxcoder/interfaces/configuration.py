"""Recovery-capable configuration CLI and stdio management process."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from reuleauxcoder.app.configuration import ConfigurationService
from reuleauxcoder.app.rpc.configuration import bind_configuration, dispatch
from reuleauxcoder.domain.config.management import ConfigOperationError


def run_stdio(service: ConfigurationService):
    from reuleauxcoder.infrastructure.rpc.peer import RpcPeer, RpcError
    from reuleauxcoder.infrastructure.rpc.transport import StreamTransport

    writer = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    peer = RpcPeer(StreamTransport(sys.stdin.buffer, writer))
    bind_configuration(peer, service)

    def initialize(version=1):
        if version != 1:
            raise RpcError(-32602, "Unsupported configuration API version")
        return {"mode": "configuration", **service.describe()}

    peer.methods["initialize"] = initialize
    peer.start()
    try:
        peer.closed.wait()
    finally:
        peer.close()
        peer.wait_closed(timeout=10)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        prog="rcoder config",
        description="Inspect, validate and repair configuration without starting an Agent.",
    )
    parser.add_argument(
        "operation",
        choices=(
            "describe",
            "inspect",
            "prepare",
            "validate",
            "check",
            "apply",
            "history",
            "revert",
            "recover",
            "rpc",
        ),
    )
    parser.add_argument("change_id", nargs="?")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, help="Explicit configuration layer")
    parser.add_argument(
        "--scope", choices=("user", "workspace", "explicit"), default="workspace"
    )
    parser.add_argument(
        "--section", help="Return the schema of one configuration section"
    )
    documents = parser.add_mutually_exclusive_group()
    documents.add_argument(
        "--changes", help="JSON array of field changes; file path or - for stdin"
    )
    documents.add_argument(
        "--document", help="Complete replacement JSON object; file path or - for stdin"
    )
    parser.add_argument(
        "--revision", help="Revision from config inspect for conflict detection"
    )
    parser.add_argument(
        "--check", action="append", choices=("static", "startup", "model")
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--side", choices=("before", "after"), default="before")
    parser.add_argument(
        "--allow-unverified-model",
        action="store_true",
        help="Explicitly save a model without a successful connection test; static/startup checks still required",
    )
    args = parser.parse_args(argv)
    service = ConfigurationService.for_workspace(args.workspace, explicit=args.config)
    if args.operation == "rpc":
        return run_stdio(service)
    try:
        operation = "validate" if args.operation == "check" else args.operation
        parameters = {}
        if operation == "describe" and args.section:
            parameters["section"] = args.section
        if operation in ("apply", "revert", "recover"):
            if not args.change_id:
                parser.error("this operation requires a change_id")
            parameters["change_id"] = args.change_id
        if operation == "prepare":
            path = args.changes or args.document
            if not path:
                parser.error("prepare requires --changes or --document")
            if path == "-":
                text = sys.stdin.read(1024 * 1024 + 1)
            else:
                with Path(path).open(encoding="utf-8") as stream:
                    text = stream.read(1024 * 1024 + 1)
            if len(text.encode("utf-8")) > 1024 * 1024:
                raise ConfigOperationError(
                    "too_large", "Configuration input exceeds 1 MiB."
                )
            parameters.update(scope=args.scope, base_revision=args.revision)
            parameters["changes" if args.changes else "document"] = json.loads(text)
        elif operation == "validate":
            parameters.update(
                change_id=args.change_id, checks=args.check or ["static", "startup"]
            )
        elif operation == "apply":
            parameters["allow_unverified_model"] = args.allow_unverified_model
        elif operation == "history":
            parameters["limit"] = args.limit
        elif operation == "recover":
            if not args.revision:
                parser.error("recover requires --revision from a fresh inspect")
            parameters.update(base_revision=args.revision, side=args.side)
        result = dispatch(service, operation, parameters)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        if result.get("valid") is False or any(
            check["status"] != "passed" for check in result.get("checks", [])
        ):
            return 1
        return 0
    except ConfigOperationError as error:
        print(json.dumps({"error": {"code": error.code, "message": str(error)}}))
        return 2
    except (ValueError, OSError):
        print(
            json.dumps(
                {
                    "error": {
                        "code": "invalid_input",
                        "message": "Cannot read the requested configuration input or complete the operation.",
                    }
                }
            )
        )
        return 2
