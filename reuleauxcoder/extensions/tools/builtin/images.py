"""Read cached session images at higher fidelity without another file upload."""

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler


class ViewImageTool(Tool):
    name = "view_image"
    effect_class = "read_only_internal"
    parallel_safe = True
    description = (
        "Read a previously attached image from this session at higher fidelity. "
        "Use its attachment_id and preferably a small region [x,y,width,height] "
        "in original orientation-corrected pixels. Ordinary images are compact "
        "previews; use this tool for unreadable text instead of guessing. "
        "Originals may have been evicted; existing sent versions remain in history."
    )
    parameters = {
        "type": "object",
        "properties": {
            "attachment_id": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "region": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0},
                "minItems": 4,
                "maxItems": 4,
            },
            "full_resolution": {
                "type": "boolean",
                "description": "Preserve pixel dimensions; fails if over the detail byte budget",
            },
        },
        "required": ["attachment_id"],
        "additionalProperties": False,
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())
        self._agent = None

    def bind_agent(self, agent) -> None:
        self._agent = agent

    def execute(
        self,
        attachment_id: str,
        region: list[int] | None = None,
        full_resolution: bool = False,
    ) -> ToolOutcome:
        return self.run_backend(
            attachment_id=attachment_id, region=region, full_resolution=full_resolution
        )

    @backend_handler("local")
    @backend_handler("remote_relay")
    def _execute_host(
        self,
        attachment_id: str,
        region: list[int] | None = None,
        full_resolution: bool = False,
    ) -> ToolOutcome:
        agent = self._agent
        try:
            if agent is None or "image" not in getattr(agent.llm, "support_modal", ()):
                raise ValueError(
                    "Current model does not support image input; switch to an image-capable model"
                )
            if agent.image_store is None or not agent.current_session_id:
                raise ValueError("Session image storage is unavailable")
            image = agent.image_store.read_image(
                agent.current_session_id,
                attachment_id,
                region=tuple(region) if region is not None else None,
                full_resolution=full_resolution,
            )
            return ToolOutcome(
                content=image.caption(),
                summary=f"Read image {image.width}x{image.height}",
                images=(image,),
            )
        except (ValueError, OSError) as error:
            return ToolOutcome(
                status=ToolOutcomeStatus.FAILED,
                content=str(error),
                error_kind=ToolErrorKind.INVALID_ARGUMENTS,
            )
