"""CLI argument parsing."""

import argparse

from reuleauxcoder import __version__


def parse_args():
    parser = argparse.ArgumentParser(
        prog="rcoder",
        description="ReuleauxCoder terminal-native coding agent.",
    )
    parser.add_argument("-c", "--config", help="Path to config.yaml")
    parser.add_argument("-m", "--model", help="Override model from config.yaml")
    parser.add_argument("-p", "--prompt", help="One-shot prompt (non-interactive mode)")
    parser.add_argument("-r", "--resume", metavar="ID", help="Resume a saved session")
    parser.add_argument(
        "--server",
        action="store_true",
        help="Run as a dedicated remote relay host",
    )
    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser.parse_args()
