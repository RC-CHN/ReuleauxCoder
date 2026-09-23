"""Bounded, session-bound byte uploads for ordinary workspace attachments."""

from __future__ import annotations

import base64
import binascii
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from reuleauxcoder.app.rpc.codec import encode
from reuleauxcoder.infrastructure.persistence.attachments import (
    store_attachment,
    validate_attachment_name,
)
from reuleauxcoder.infrastructure.rpc.peer import RpcError


ATTACHMENT_CHUNK_BYTES = 256 * 1024
ATTACHMENT_MAX_BYTES = 64 * 1024 * 1024


@dataclass
class PendingAttachment:
    upload_id: str
    session_id: str
    session_generation: int
    workspace: Path
    name: str
    size: int
    file: BinaryIO
    received: int = 0


class AttachmentUploads:
    def __init__(self, server):
        self.server = server
        self.pending: PendingAttachment | None = None

    def close(self):
        if self.pending is not None:
            self.pending.file.close()
            self.pending = None

    def _workspace(self):
        return Path(self.server.agent.runtime_working_directory or Path.cwd()).resolve()

    def _check_session(self, session_id, session_generation):
        server = self.server
        if not server._initialized or server._closing:
            raise RpcError(-32002, "Session is unavailable")
        if (
            not session_id
            or type(session_generation) is not int
            or (session_id, session_generation)
            != (server.commands.session_id, server.agent.session_generation)
        ):
            raise RpcError(
                -32602, "Attachment belongs to a different session; attach it again"
            )

    def begin(self, session_id, session_generation, name, size_bytes):
        with self.server._lock:
            self._check_session(session_id, session_generation)
            try:
                validate_attachment_name(name)
            except ValueError as error:
                raise RpcError(-32602, str(error)) from error
            if (
                type(size_bytes) is not int
                or not 0 <= size_bytes <= ATTACHMENT_MAX_BYTES
            ):
                raise RpcError(
                    -32602,
                    f"Invalid attachment size (maximum {ATTACHMENT_MAX_BYTES} bytes)",
                )
            self.close()
            self.pending = PendingAttachment(
                uuid.uuid4().hex,
                session_id,
                session_generation,
                self._workspace(),
                name,
                size_bytes,
                tempfile.TemporaryFile(),
            )
            return {
                "upload_id": self.pending.upload_id,
                "chunk_bytes": ATTACHMENT_CHUNK_BYTES,
            }

    def _upload(self, upload_id):
        upload = self.pending
        if upload is None or upload.upload_id != upload_id:
            raise RpcError(-32602, "Unknown attachment upload")
        try:
            self._check_session(upload.session_id, upload.session_generation)
            if upload.workspace != self._workspace():
                raise RpcError(-32602, "Workspace changed during attachment upload")
        except RpcError:
            self.close()
            raise
        return upload

    def append(self, upload_id, offset, data):
        with self.server._lock:
            upload = self._upload(upload_id)
            if (
                type(offset) is not int
                or offset != upload.received
                or not isinstance(data, str)
                or len(data) > 4 * ((ATTACHMENT_CHUNK_BYTES + 2) // 3)
            ):
                raise RpcError(-32602, "Invalid attachment chunk offset or size")
            try:
                chunk = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error) as error:
                raise RpcError(-32602, "Invalid attachment chunk encoding") from error
            if (
                not chunk
                or len(chunk) > ATTACHMENT_CHUNK_BYTES
                or offset + len(chunk) > upload.size
            ):
                raise RpcError(-32602, "Attachment chunk exceeds declared size")
            upload.file.write(chunk)
            upload.received += len(chunk)
            return upload.received

    def complete(self, upload_id):
        with self.server._lock:
            upload = self._upload(upload_id)
            if upload.received != upload.size:
                raise RpcError(-32602, "Attachment upload is incomplete")
            try:
                upload.file.seek(0)
                return encode(
                    store_attachment(
                        upload.workspace, upload.session_id, upload.name, upload.file
                    )
                )
            except (ValueError, OSError) as error:
                raise RpcError(-32602, str(error)) from error
            finally:
                self.close()

    def cancel(self, upload_id):
        with self.server._lock:
            if self.pending is not None and self.pending.upload_id == upload_id:
                self.close()
