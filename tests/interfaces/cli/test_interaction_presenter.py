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
from reuleauxcoder.interfaces.interactions import ReviewRequest


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

    assert local.export_text() == export_remote_console(remote)
    assert interaction_constraints(request) == {
        "value_type": "boolean",
        "approve_label": "Approve",
        "reject_label": "Reject",
    }
