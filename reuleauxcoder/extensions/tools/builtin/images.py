"""Read workspace images through the shared ImageStore pipeline."""

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)
from reuleauxcoder.domain.workspace import WorkspaceError
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler


class ViewImageTool(Tool):
    name = "view_image"
    effect_class = "read_only_internal"
    parallel_safe = True
    description = (
        "Read an image file from a local or workspace-relative path on the workspace "
        "host. The active local/remote WorkspacePort reads bounded bytes and imports "
        "them into the same session ImageStore used by pasted images. Use a small "
        "region [x,y,width,height] in original orientation-corrected pixels for "
        "unreadable detail. Text-only models retain the path reference but receive an "
        "explicit placeholder instead of image bytes; switch to a vision model before "
        "claiming visual observations. Originals may be evicted; sent variants remain."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "description": "Absolute or workspace-relative image path on the workspace host.",
            },
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
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())
        self._agent = None

    def bind_agent(self, agent) -> None:
        self._agent = agent

    def execute(
        self,
        path: str,
        region: list[int] | None = None,
        full_resolution: bool = False,
    ) -> ToolOutcome:
        return self.run_backend(
            path=path, region=region, full_resolution=full_resolution
        )

    @backend_handler("local")
    @backend_handler("remote_relay")
    def _execute_host(
        self,
        path: str,
        region: list[int] | None = None,
        full_resolution: bool = False,
    ) -> ToolOutcome:
        agent = self._agent
        try:
            if agent is None or agent.image_store is None or not agent.current_session_id:
                raise ValueError("Session image storage is unavailable")
            if not isinstance(path, str) or not path.strip():
                raise ValueError("path must be a non-empty string")
            workspace = self.backend.workspace
            resolved = workspace.resolve(path)
            try:
                reference_path = resolved.relative_to(workspace.root).as_posix()
            except ValueError:
                reference_path = str(resolved)
            max_bytes = getattr(
                getattr(agent.image_store, "config", None),
                "import_max_bytes",
                64 * 1024 * 1024,
            )
            data = workspace.read_bytes(path, max_bytes=max_bytes)
            imported = agent.image_store.import_bytes(
                agent.current_session_id,
                data,
                name=path.replace("\\", "/").rsplit("/", 1)[-1],
                path=reference_path,
            )
            if region is not None or full_resolution:
                image = agent.image_store.read_image(
                    agent.current_session_id,
                    imported.attachment_id,
                    region=tuple(region) if region is not None else None,
                    full_resolution=full_resolution,
                    path=reference_path,
                )
            else:
                image = imported
            withheld = "image" not in getattr(agent.llm, "support_modal", ())
            content = image.caption()
            if withheld:
                content += (
                    "\nThe current model is text-only. The image path is retained, "
                    "but image bytes are withheld from provider requests; switch to a "
                    "vision model before claiming any visual observation."
                )
            return ToolOutcome(
                content=content,
                summary=f"Read image {image.width}x{image.height}",
                images=(image,),
                metadata={
                    "source_path": reference_path,
                    "image_withheld": withheld,
                },
            )
        except (ValueError, OSError, WorkspaceError) as error:
            return ToolOutcome(
                status=ToolOutcomeStatus.FAILED,
                content=str(error),
                error_kind=ToolErrorKind.INVALID_ARGUMENTS,
            )
