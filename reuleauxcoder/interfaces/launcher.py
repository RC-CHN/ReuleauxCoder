"""Choose a terminal frontend without importing or starting the agent."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from reuleauxcoder.interfaces.cli.args import create_parser


TUI_ASSETS = Path(__file__).resolve().parents[1] / "_tui"


def _node_version(executable: str) -> int:
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Cannot run Node at {executable}: {error}") from error
    match = re.fullmatch(r"v(\d+)\.\d+\.\d+(?:-[\w.-]+)?", result.stdout.strip())
    if result.returncode or match is None:
        raise RuntimeError(
            f"Cannot detect Node version at {executable}: {result.stderr.strip() or result.stdout.strip() or 'no output'}"
        )
    return int(match[1])


def _cli():
    from reuleauxcoder.interfaces.cli.main import main

    return main()


def _is_terminal():
    return sys.stdin.isatty() and sys.stdout.isatty()


def _launch(*, explicit_tui: bool):
    if sys.argv[1:2] == ["config"]:
        from reuleauxcoder.interfaces.configuration import main
        return main(sys.argv[2:])
    parser = create_parser(prog="rcoder-tui" if explicit_tui else "rcoder")
    parser.epilog = (
        "rcoder selects TUI when compatible Node is available; otherwise CLI. "
        "Use rcoder-cli or rcoder-tui to choose explicitly. "
        "TUI requires an interactive terminal."
    )
    parser.add_argument("--cwd", help="TUI working directory")
    parser.add_argument("--python", help="Override the TUI backend Python executable")
    parser.add_argument(
        "--backend", help="Custom TUI stdio backend; pass its arguments after --"
    )
    parser.add_argument("--theme", help="TUI theme preset or JSON path")
    parser.add_argument(
        "--no-mouse",
        action="store_true",
        help="Disable TUI mouse reporting for native terminal selection",
    )
    parser.add_argument(
        "--no-alt-screen",
        action="store_true",
        help="Render TUI in the main terminal buffer",
    )
    argv = sys.argv[1:]
    args, _ = parser.parse_known_args(argv)
    cli_mode = args.prompt is not None or args.server or args.rpc_stdio
    terminal = _is_terminal()
    if not explicit_tui and (cli_mode or not terminal):
        return _cli()
    if cli_mode:
        parser.error("--prompt, --server and --rpc-stdio require rcoder-cli")
    if not terminal:
        parser.error(
            "TUI requires terminal stdin and stdout; use rcoder-cli for redirected input/output"
        )

    node = shutil.which("node")
    reason = None
    if node is None:
        reason = "Node was not found on PATH"
    else:
        try:
            major = _node_version(node)
        except RuntimeError as error:
            reason = str(error)
    if reason:
        return _unavailable(reason, explicit_tui=explicit_tui)

    bundle = TUI_ASSETS / "cli.mjs"
    manifest = TUI_ASSETS / "manifest.json"
    if not bundle.is_file() or not manifest.is_file():
        print(
            "rcoder: TUI files are missing. Reinstall the published wheel, or in a source checkout run "
            "`npm ci --prefix reuleauxcoder-tui` and `npm run bundle --prefix reuleauxcoder-tui`. "
            "Use rcoder-cli to run the CLI.",
            file=sys.stderr,
        )
        return 1
    minimum = json.loads(manifest.read_text(encoding="utf-8"))["node_major"]
    if major < minimum:
        return _unavailable(
            f"Node {major} at {node} is too old; TUI requires Node >= {minimum}",
            explicit_tui=explicit_tui,
        )
    if not args.backend and not args.python:
        argv = ["--python", sys.executable, *argv]
    try:
        os.execv(node, [node, str(bundle), *argv])
    except OSError as error:
        print(f"rcoder: Cannot start TUI with {node}: {error}", file=sys.stderr)
        return 1


def _unavailable(reason: str, *, explicit_tui: bool):
    if explicit_tui:
        print(
            f"rcoder-tui: {reason}. Install compatible Node or use rcoder-cli.",
            file=sys.stderr,
        )
        return 1
    from rich.console import Console
    from rich.text import Text

    from reuleauxcoder.interfaces.cli.theme import DEFAULT_CLI_THEME
    from reuleauxcoder.presentation.semantics import DisplayTone

    theme = DEFAULT_CLI_THEME
    message = Text.assemble(
        (
            f"rcoder: TUI unavailable: {reason}; falling back to CLI.\n",
            theme.style(DisplayTone.WARNING),
        ),
        (
            "Install or update Node.js on PATH to enable TUI",
            theme.style(DisplayTone.ACCENT),
        ),
        ", then run ",
        ("rcoder-tui", theme.style(DisplayTone.ACCENT)),
        "; no npm install is needed.\n",
        (
            "Use rcoder-cli to select CLI explicitly and skip this notice.",
            theme.style(DisplayTone.MUTED),
        ),
    )
    Console(stderr=True).print(message, soft_wrap=True)
    return _cli()


def main():
    return _launch(explicit_tui=False)


def tui_main():
    return _launch(explicit_tui=True)
