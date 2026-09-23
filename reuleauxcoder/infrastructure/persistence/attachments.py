"""Publish complete attachments without overwriting workspace files."""

from __future__ import annotations

import mimetypes
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import BinaryIO

from reuleauxcoder.domain.attachments import AttachmentReference
from reuleauxcoder.infrastructure.persistence.session_paths import (
    is_safe_session_id,
    session_path_component,
)


def validate_attachment_name(name: str) -> None:
    reserved = {"con", "prn", "aux", "nul"} | {
        f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
    }
    if (
        not isinstance(name, str)
        or not name
        or len(name.encode("utf-8")) > 255
        or name.endswith((" ", "."))
        or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in name)
        or name.split(".", 1)[0].lower() in reserved
    ):
        raise ValueError(
            "Invalid attachment filename; provide a portable filename without directories"
        )


def store_attachment(
    workspace: Path, session_id: str, name: str, source: BinaryIO
) -> AttachmentReference:
    validate_attachment_name(name)
    if not is_safe_session_id(session_id):
        raise ValueError("Invalid attachment session")
    workspace = workspace.resolve()
    directory = workspace
    for component in (".rcoder", "attachments", session_path_component(session_id)):
        directory = directory / component
        if directory.is_symlink() or not directory.resolve().is_relative_to(workspace):
            raise ValueError(
                "Attachment directory must stay inside the workspace without symlinks"
            )
        directory.mkdir(exist_ok=True)
    attachment_id = uuid.uuid4().hex
    destination = directory / attachment_id
    # The final path appears only after the complete file has been copied.
    with tempfile.TemporaryDirectory(prefix=".upload-", dir=directory) as staging:
        with (Path(staging) / name).open("xb") as target:
            shutil.copyfileobj(source, target, length=256 * 1024)
            size = target.tell()
        Path(staging).rename(destination)
    return AttachmentReference(
        attachment_id=attachment_id,
        name=name,
        mime_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
        size_bytes=size,
        path=(destination / name).relative_to(workspace).as_posix(),
    )
