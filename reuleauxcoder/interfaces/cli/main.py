"""Lightweight executable dispatch; stdio mode never imports the CLI renderer."""


def main():
    import sys
    if "--config-management-stdio" in sys.argv[1:]:
        from reuleauxcoder.interfaces.configuration import recovery_stdio
        return recovery_stdio(sys.argv[1:])
    if sys.argv[1:2] == ["config"]:
        from reuleauxcoder.interfaces.configuration import main as configure
        return configure(sys.argv[2:])
    from reuleauxcoder.interfaces.cli.args import parse_args

    args = parse_args()
    if args.rpc_stdio:
        from pathlib import Path
        from reuleauxcoder.interfaces.entrypoint.dependencies import AppOptions
        from reuleauxcoder.interfaces.entrypoint.rpc import run_stdio

        return run_stdio(
            AppOptions(
                config_path=Path(args.config) if args.config else None,
                model=args.model,
                resume_session_id=args.resume,
                auto_resume_latest=True,
                server_mode=args.server,
            )
        )
    from reuleauxcoder.interfaces.entrypoint.cli import main as run

    return run()
