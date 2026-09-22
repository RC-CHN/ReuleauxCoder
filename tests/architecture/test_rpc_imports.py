"""Check transitive imports before test fixtures load the execution runtime."""

import subprocess
import sys


def test_python_client_does_not_load_runtime_or_frontends():
    subprocess.run(
        [sys.executable, "-c", """
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
            'reuleauxcoder.interfaces',
            'openai', 'rich', 'prompt_toolkit',
        )
        if fullname.startswith(forbidden):
            raise AssertionError(fullname)

sys.meta_path.insert(0, Boundary())
from reuleauxcoder.app.rpc.client import RuntimeClient
from reuleauxcoder.app.rpc.codec import encode, decode
from reuleauxcoder.app.rpc.models import RuntimeSnapshot
assert decode(encode(RuntimeSnapshot())) == RuntimeSnapshot()
"""],
        check=True,
    )
