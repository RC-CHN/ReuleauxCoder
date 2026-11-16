"""Text queue controls shared by the linear CLI and remote chat relay."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reuleauxcoder.app.rpc.client import RuntimeClient


def queue_command(runtime: RuntimeClient, argument: str) -> str:
    words = argument.split()
    if not words:
        runtime.refresh()
        return "\n".join(
            f"{item.submission_id or item.steering_id}  {item.text}"
            for item in runtime.state.queued_inputs
        ) or "No queued messages."
    if len(words) == 2 and words[0] == "cancel":
        result = runtime.cancel_input(words[1])
        return ("Queued message cancelled." if result["outcome"] == "cancelled"
                else "This message is no longer queued.")
    return "Usage: /queue [cancel <message ID>]"
