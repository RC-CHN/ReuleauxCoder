"""Bounded frontend-to-runtime image transfer, bound to a session generation."""

from __future__ import annotations

import base64
import binascii
import tempfile
import uuid

from reuleauxcoder.app.rpc.codec import encode
from reuleauxcoder.infrastructure.rpc.peer import RpcError


IMAGE_CHUNK_BYTES = 256 * 1024


class ImageUploads:
    def __init__(self, server):
        self.server = server
        self.pending = None

    def close(self):
        if self.pending is not None:
            self.pending["file"].close()
            self.pending = None

    def _check_session(self, session_id, session_generation):
        server = self.server
        if not server._initialized or server._closing:
            raise RpcError(-32002, "Session is unavailable")
        if (session_id, session_generation) != (
            server.commands.session_id,
            server.agent.session_generation,
        ):
            raise RpcError(
                -32602, "Image belongs to a different session; attach it again"
            )
        if not session_id or type(session_generation) is not int:
            raise RpcError(-32602, "Invalid image session")

    def begin(self, session_id, session_generation, name, size_bytes):
        with self.server._lock:
            self._check_session(session_id, session_generation)
            limit = self.server.agent.image_store.config.import_max_bytes
            if (
                not isinstance(name, str)
                or len(name) > 256
                or type(size_bytes) is not int
                or not 0 < size_bytes <= limit
            ):
                raise RpcError(
                    -32602, f"Invalid image name or size (maximum {limit} bytes)"
                )
            self.close()
            self.pending = {
                "id": uuid.uuid4().hex,
                "session_id": session_id,
                "session_generation": session_generation,
                "name": name,
                "size": size_bytes,
                "received": 0,
                "file": tempfile.TemporaryFile(),
            }
            return {"upload_id": self.pending["id"], "chunk_bytes": IMAGE_CHUNK_BYTES}

    def _upload(self, upload_id):
        pending = self.pending
        if pending is None or pending["id"] != upload_id:
            raise RpcError(-32602, "Unknown image upload")
        try:
            self._check_session(pending["session_id"], pending["session_generation"])
        except RpcError:
            self.close()
            raise
        return pending

    def append(self, upload_id, offset, data):
        with self.server._lock:
            upload = self._upload(upload_id)
            if (
                type(offset) is not int
                or offset != upload["received"]
                or not isinstance(data, str)
                or len(data) > 4 * ((IMAGE_CHUNK_BYTES + 2) // 3)
            ):
                raise RpcError(-32602, "Invalid image chunk offset or size")
            try:
                chunk = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error) as error:
                raise RpcError(-32602, "Invalid image chunk encoding") from error
            if (
                not chunk
                or len(chunk) > IMAGE_CHUNK_BYTES
                or offset + len(chunk) > upload["size"]
            ):
                raise RpcError(-32602, "Image chunk exceeds declared size")
            upload["file"].write(chunk)
            upload["received"] += len(chunk)
            return upload["received"]

    def complete(self, upload_id):
        with self.server._lock:
            upload = self._upload(upload_id)
            if upload["received"] != upload["size"]:
                raise RpcError(-32602, "Image upload is incomplete")
            try:
                upload["file"].seek(0)
                image = self.server.agent.image_store.import_bytes(
                    upload["session_id"],
                    upload["file"].read(),
                    name=upload["name"],
                )
                return encode(image)
            except (ValueError, OSError) as error:
                raise RpcError(-32602, str(error)) from error
            finally:
                self.close()

    def cancel(self, upload_id):
        with self.server._lock:
            if self.pending is not None and self.pending["id"] == upload_id:
                self.close()
