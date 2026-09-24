"""Stable document snapshots, independent of LSP scheduling and transport state."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


from reuleauxcoder.extensions.lsp.client import (
    LspDocumentChangedDuringRead,
    LspDocumentCloseError,
    LspDocumentDecodeError,
    LspDocumentReadError,
    LspDocumentStatError,
    LspDocumentTooLarge,
    MAX_LSP_FILE_SIZE_BYTES,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _DocumentStamp:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> _DocumentStamp:
        return cls(
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            ctime_ns=metadata.st_ctime_ns,
        )


@dataclass(frozen=True, slots=True)
class _DocumentSnapshot:
    content: str = field(repr=False)
    stamp: _DocumentStamp


class DocumentReader:
    """Own stable bounded file reads, retry and file-handle cleanup."""

    @classmethod
    def _read_file_content(cls, file_path: Path) -> str:
        """Read one bounded UTF-8 document or raise a typed safe failure."""
        snapshot = cls._load_document_for_sync(
            file_path,
            last_stamp=None,
            force=True,
        )
        assert snapshot is not None
        return snapshot.content

    @classmethod
    def _load_document_for_sync(
        cls,
        file_path: Path,
        *,
        last_stamp: _DocumentStamp | None,
        force: bool,
    ) -> _DocumentSnapshot | None:
        """Load a stable bounded snapshot, retrying one concurrent mutation."""
        for attempt in range(2):
            try:
                handle = file_path.open("rb")
            except OSError as error:
                raise LspDocumentReadError("LSP document read failed") from error

            changed_during_read = False
            snapshot: _DocumentSnapshot | None = None
            try:
                before = cls._document_stamp(handle)
                if before.size > MAX_LSP_FILE_SIZE_BYTES:
                    raise LspDocumentTooLarge("LSP document exceeds the size limit")
                if force or last_stamp is None or before != last_stamp:
                    try:
                        raw = handle.read(MAX_LSP_FILE_SIZE_BYTES + 1)
                    except OSError as error:
                        raise LspDocumentReadError(
                            "LSP document read failed"
                        ) from error
                    if len(raw) > MAX_LSP_FILE_SIZE_BYTES:
                        raise LspDocumentTooLarge("LSP document exceeds the size limit")
                    after = cls._document_stamp(handle)
                    if before != after:
                        changed_during_read = True
                    else:
                        try:
                            content = raw.decode("utf-8")
                        except UnicodeDecodeError as error:
                            raise LspDocumentDecodeError(
                                "LSP document is not valid UTF-8"
                            ) from error
                        snapshot = _DocumentSnapshot(content=content, stamp=after)
            except BaseException as primary_error:
                cls._close_document_handle(
                    handle,
                    primary_error=primary_error,
                )
                raise
            cls._close_document_handle(handle)
            if not changed_during_read:
                return snapshot
            if attempt == 0:
                continue
            raise LspDocumentChangedDuringRead(
                "LSP document changed repeatedly while being read"
            )
        raise AssertionError("document read retry loop exhausted")

    @staticmethod
    def _document_stamp(handle: Any) -> _DocumentStamp:
        try:
            return _DocumentStamp.from_stat(os.fstat(handle.fileno()))
        except (OSError, ValueError) as error:
            raise LspDocumentStatError("LSP document stat failed") from error

    @staticmethod
    def _close_document_handle(
        handle: Any,
        *,
        primary_error: BaseException | None = None,
    ) -> None:
        try:
            handle.close()
        except Exception as close_error:
            if primary_error is None:
                raise LspDocumentCloseError(
                    "LSP document close failed"
                ) from close_error
            try:
                primary_error.secondary_error_operation = "document_close"  # type: ignore[attr-defined]
                primary_error.secondary_error_type = type(close_error).__name__  # type: ignore[attr-defined]
            except Exception as observation_error:
                logger.warning(
                    "LSP document close-failure observation failed: error_type=%s",
                    type(observation_error).__name__,
                )
            logger.warning(
                "LSP document close failed while preserving primary failure: "
                "error_type=%s",
                type(close_error).__name__,
            )
