from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_agent_depends_on_domain_llm_protocol_not_concrete_service_client() -> None:
    path = ROOT / "reuleauxcoder" / "domain" / "agent" / "agent.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert "reuleauxcoder.domain.llm.protocols" in imports
    assert "reuleauxcoder.services.llm.client" not in imports
