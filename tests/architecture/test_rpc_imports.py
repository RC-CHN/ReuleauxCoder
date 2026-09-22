"""Check transitive imports before test fixtures load the execution runtime."""

import subprocess
import sys
import pytest


@pytest.mark.parametrize("frontend", [False, True])
def test_python_client_does_not_load_runtime_or_frontends(frontend):
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys

class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        forbidden = (
            'reuleauxcoder.domain.agent.agent',
            'reuleauxcoder.domain.context.manager',
            'reuleauxcoder.services.llm',
            'reuleauxcoder.extensions.tools.registry',
            'reuleauxcoder.extensions.mcp.manager',
            'reuleauxcoder.extensions.skills.service',
            'reuleauxcoder.interfaces.entrypoint',
            'openai',
        )
        if fullname.startswith(forbidden):
            raise AssertionError(fullname)

sys.meta_path.insert(0, Boundary())
from reuleauxcoder.app.rpc.client import RuntimeClient
from reuleauxcoder.app.rpc.codec import encode, decode
from reuleauxcoder.app.rpc.models import RuntimeSnapshot
assert decode(encode(RuntimeSnapshot())) == RuntimeSnapshot()
"""
            + (
                """
from reuleauxcoder.interfaces.cli.application import run_cli
from reuleauxcoder.interfaces.relay import RelayUI
"""
                if frontend
                else """
assert not any(name.startswith(('rich', 'prompt_toolkit')) for name in sys.modules)
"""
            ),
        ],
        check=True,
    )


def test_stdio_bootstrap_does_not_load_a_terminal_frontend():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys

class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.startswith(('rich', 'prompt_toolkit', 'reuleauxcoder.interfaces.cli.render')):
            raise AssertionError(fullname)

sys.meta_path.insert(0, Boundary())
from reuleauxcoder.interfaces.cli.main import main
from reuleauxcoder.interfaces.entrypoint.rpc import run_stdio
from reuleauxcoder.interfaces.entrypoint.runner import AppRunner
""",
        ],
        check=True,
    )
