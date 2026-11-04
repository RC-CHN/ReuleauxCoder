"""Read-only inspection of the model's current compacted context view."""

from __future__ import annotations

from collections import Counter
import json

from reuleauxcoder.domain.agent.tool_outcome import ToolOutcome
from reuleauxcoder.domain.images import image_parts
from reuleauxcoder.domain.llm.context_messages import SYNTHETIC_CONTEXT_METADATA_KEY
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool


class ContextInspectTool(Tool):
    name = "context_inspect"
    effect_class = "read_only_internal"
    parallel_safe = True
    description = (
        "Inspect the model's current context projection without changing it. "
        "Reports compacted-view roles, token estimates, replay/checkpoint "
        "watermarks, preserved rounds, and images withheld by current model "
        "capabilities. Original messages remain available through history tools."
    )
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())
        self._agent = None

    def bind_agent(self, agent) -> None:
        self._agent = agent

    def execute(self) -> ToolOutcome:  # type: ignore[override]
        if self._agent is None:
            raise RuntimeError("context_inspect is not bound to an agent")
        agent = self._agent
        messages = list(agent.state.messages)
        context = agent.context
        checkpoints = context.checkpoints
        latest = checkpoints[-1].to_dict() if checkpoints else None
        if latest is not None:
            latest.pop("replacement_history", None)

        roles = Counter(str(message.get("role") or "unknown") for message in messages)
        synthetic = Counter(
            str(marker.get("tag"))
            for message in messages
            if isinstance((marker := message.get(SYNTHETIC_CONTEXT_METADATA_KEY)), dict)
            and marker.get("tag")
        )
        images = len(image_parts(messages))
        modalities = tuple(getattr(agent.llm, "support_modal", ()) or ("text",))
        supports_images = "image" in modalities
        latest_usage = context.latest_usage
        payload = {
            "session_id": getattr(agent, "current_session_id", None),
            "model": str(getattr(agent.llm, "model", "unknown")),
            "support_modal": list(modalities),
            "current_view": {
                "message_count": len(messages),
                "roles": dict(sorted(roles.items())),
                "synthetic_messages": dict(sorted(synthetic.items())),
                "estimated_tokens": context.get_context_tokens(messages),
                "request_input_limit": context.request_input_limit,
            },
            "replay": {
                "history_version": context.history_version,
                "cache_epoch": context.cache_epoch,
                "latest_actual_prompt_tokens": (
                    getattr(latest_usage, "actual_prompt_tokens", None)
                    if latest_usage is not None
                    else None
                ),
            },
            "compaction": {
                "checkpoint_count": len(checkpoints),
                "latest_checkpoint": latest,
                "human_transcript": (
                    "Original events and the human transcript are unchanged; "
                    "model requests use the current compacted view."
                ),
            },
            "images": {
                "canonical_parts": images,
                "supports_image_input": supports_images,
                "withheld_from_requests": bool(images and not supports_images),
                "reason": (
                    None
                    if not images or supports_images
                    else "current model does not support image input"
                ),
            },
        }
        content = json.dumps(payload, ensure_ascii=False)
        return ToolOutcome(
            summary="Inspected the current model context view",
            content=content,
            metadata={
                "checkpoint_count": len(checkpoints),
                "images_withheld": payload["images"]["withheld_from_requests"],
            },
        )
