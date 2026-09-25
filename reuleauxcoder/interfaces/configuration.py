"""Read-only configuration CLI and independent stdio inspection process."""

import argparse
import json
import os
from pathlib import Path
import sys

from reuleauxcoder.app.configuration import ConfigurationService, OPERATIONS
from reuleauxcoder.app.rpc.configuration import bind_configuration, dispatch
from reuleauxcoder.domain.config.management import ConfigOperationError
from reuleauxcoder.infrastructure.persistence.config_files import MAX_CONFIG_BYTES


def run_stdio(service):
    from reuleauxcoder.infrastructure.rpc.peer import RpcPeer, RpcError
    from reuleauxcoder.infrastructure.rpc.transport import StreamTransport

    writer = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    peer = RpcPeer(StreamTransport(sys.stdin.buffer, writer))
    bind_configuration(peer, service)

    def initialize(version=1):
        if version != 1:
            raise RpcError(-32602, "Unsupported configuration protocol version")
        return {"mode": "configuration", **service.describe()}

    peer.methods["initialize"] = initialize
    peer.start()
    try:
        peer.closed.wait()
    finally:
        peer.close()
        peer.wait_closed(timeout=10)


def recovery_stdio(argv):
    """Preserve the launch command's paths without initializing its runtime."""
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("-c", "--config")
    parser.add_argument("--cwd")
    args, _ = parser.parse_known_args(argv)
    return main(["rpc", *(["--config", args.config] if args.config else []),
                 *(["--workspace", args.cwd] if args.cwd else [])])


def main(argv=None):
    for stream in (sys.stdout, sys.stdin):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="rcoder config", description="Describe, inspect and check configuration without starting an Agent.")
    parser.add_argument("operation", choices=(*OPERATIONS, "rpc"))
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, help="Explicit configuration layer")
    parser.add_argument("--section", help="Section to describe")
    parser.add_argument("--check", action="append", choices=("static", "startup", "model"))
    parser.add_argument("--profile", action="append", help="Model profile to test; repeat up to eight times")
    parser.add_argument("--content", help="YAML buffer to check instead of the selected file; path or - for stdin")
    parser.add_argument("--scope", choices=("user", "workspace", "explicit"), default="workspace")
    parser.add_argument("--revision", help="Expected disk revision from inspect")
    args = parser.parse_args(argv)
    if args.operation != "check" and (args.check or args.profile or args.content or args.revision):
        parser.error("--check, --profile, --content and --revision require check")
    if args.section and args.operation != "describe":
        parser.error("--section requires describe")
    workspace = args.workspace.absolute()
    explicit = (workspace / args.config).absolute() if args.config and not args.config.is_absolute() else args.config
    service = ConfigurationService.for_workspace(workspace, explicit=explicit)
    if args.operation == "rpc":
        return run_stdio(service)
    try:
        parameters = {}
        if args.operation == "describe" and args.section:
            parameters["section"] = args.section
        elif args.operation == "check":
            parameters.update(checks=args.check, profiles=args.profile, base_revision=args.revision)
            if args.content:
                if args.content == "-":
                    content = sys.stdin.read(MAX_CONFIG_BYTES + 1)
                else:
                    with Path(args.content).open(encoding="utf-8-sig") as stream:
                        content = stream.read(MAX_CONFIG_BYTES + 1)
                parameters["documents"] = [{"scope": args.scope, "content": content}]
        result = dispatch(service, args.operation, parameters)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return int(result.get("valid") is False or any(item["status"] != "passed" for item in result.get("checks", [])))
    except ConfigOperationError as error:
        print(json.dumps({"error": {"code": error.code, "message": str(error)}}))
        return 2
    except (ValueError, OSError):
        print(json.dumps({"error": {"code": "invalid_input", "message": "Cannot read or check the requested configuration."}}))
        return 2
