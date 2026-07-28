from io import StringIO

import pytest
from rich.console import Console

from reuleauxcoder.domain.approval import (
    ApprovalSection,
    ApprovalSectionKind,
)
from reuleauxcoder.extensions.remote_exec.protocol import TerminalCapabilities
from reuleauxcoder.interfaces.cli.interaction_presenter import (
    interaction_constraints,
    render_interaction_request,
)
from reuleauxcoder.interfaces.entrypoint.remote_relay import (
    create_remote_console,
    export_remote_console,
)
from reuleauxcoder.interfaces.interactions import (
    InputTextRequest,
    ReviewGrantOption,
    ReviewRequest,
)


@pytest.mark.parametrize("width", [80, 120])
def test_local_and_remote_cli_render_identical_review_frames(width: int) -> None:
    request = ReviewRequest(
        title="Approval required: edit_file",
        summary="Tool 'edit_file' requires approval.",
        sections=(
            ApprovalSection(
                id="diff",
                title="Proposed edit diff",
                kind=ApprovalSectionKind.DIFF,
                content="--- a/demo.py\n+++ b/demo.py\n-old\n+new\n",
            ),
        ),
        grant_options=(
            ReviewGrantOption("exact", "This file", "demo.py"),
        ),
    )
    local = Console(
        file=StringIO(),
        record=True,
        width=width,
        color_system=None,
        force_terminal=False,
    )
    remote = create_remote_console(
        TerminalCapabilities(width=width, color_level="none", unicode=True)
    )

    render_interaction_request(local, request)
    render_interaction_request(remote, request)

    local_frame = local.export_text()
    assert local_frame == export_remote_console(remote)
    assert "[1/Y] Approve" in local_frame
    assert "[S] Allow for session" in local_frame
    assert "[2/N] Reject" in local_frame
    assert set("┏┓┗┛┃━┌┐└┘│─").intersection(local_frame)
    assert interaction_constraints(request) == {
        "value_type": "review_decision",
        "approve_label": "Approve",
        "reject_label": "Reject",
        "actions": ("allow_once", "allow_session", "deny"),
        "supports_feedback": True,
        "grant_options": (
            {
                "id": "exact",
                "label": "This file",
                "description": "demo.py",
                "broad": False,
            },
        ),
    }


def test_secret_text_constraint_is_explicit_on_the_wire() -> None:
    request = InputTextRequest("Secure input", "Enter hidden text", secret=True)

    assert interaction_constraints(request) == {
        "value_type": "string",
        "allow_empty": False,
        "secret": True,
    }


def test_large_write_review_is_head_tail_folded_inside_bounded_frame() -> None:
    diff = "\n".join(
        ["--- a/demo.py", "+++ b/demo.py", "@@ -0,0 +1,80 @@"]
        + [f"+line-{index}" for index in range(80)]
    )
    request = ReviewRequest(
        title="Approval required: write_file",
        summary="Tool 'write_file' requires approval.",
        sections=(
            ApprovalSection(
                id="diff",
                title="Proposed file diff",
                kind=ApprovalSectionKind.DIFF,
                content=diff,
            ),
        ),
    )
    console = Console(
        file=StringIO(),
        record=True,
        width=60,
        color_system=None,
        force_terminal=False,
    )

    render_interaction_request(
        console, request, max_preview_lines=8, max_preview_chars=300
    )
    output = console.export_text()

    assert "+line-0" in output
    assert "+line-79" in output
    assert "output folded" in output
    assert set("┏┓┗┛┃━┌┐└┘│─").intersection(output)
    assert max(map(len, output.splitlines())) <= 60
