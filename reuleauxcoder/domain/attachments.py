"""References to ordinary files uploaded into the runtime workspace."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AttachmentReference:
    attachment_id: str
    name: str
    mime_type: str
    size_bytes: int
    path: str
