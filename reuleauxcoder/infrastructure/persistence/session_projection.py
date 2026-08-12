"""Rebuildable SQLite projection for session inventory queries."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Iterable

from reuleauxcoder.domain.session.models import SessionMetadata
from reuleauxcoder.infrastructure.persistence.session_paths import (
    session_path_candidates,
)


INDEX_DIRECTORY_NAME = ".inventory"
INDEX_DATABASE_NAME = "sessions.sqlite3"
INDEX_DIRTY_NAME = "dirty"
INDEX_FAILURE_NAME = "failure"
_SCHEMA_VERSION = 1
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*")


class SessionProjectionError(RuntimeError):
    """Content-free failure raised by the optional query projection."""

    def __init__(self, error_type: str) -> None:
        self.error_type = _safe_error_type_name(error_type)
        super().__init__(
            "Session query projection failed "
            f"(error_type={self.error_type}, ref=session_index)"
        )


@dataclass(frozen=True, slots=True)
class SessionProjectionRow:
    metadata: SessionMetadata
    rank_mtime_ns: int
    rank_saved_at: float
    source_kind: str
    source_mtime_ns: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    event_count: int = 0
    request_count: int = 0
    checkpoint_count: int = 0


@dataclass(frozen=True, slots=True)
class SessionProjectionSummary:
    session_count: int
    prompt_tokens: int
    completion_tokens: int
    event_count: int
    request_count: int
    checkpoint_count: int


class SessionInventoryProjection:
    """Keep derived metadata queryable without scanning every manifest."""

    def __init__(self, sessions_dir: Path) -> None:
        self._sessions_dir = sessions_dir
        self._index_dir = sessions_dir / INDEX_DIRECTORY_NAME
        self._database = self._index_dir / INDEX_DATABASE_NAME
        self._dirty = self._index_dir / INDEX_DIRTY_NAME
        self._failure = self._index_dir / INDEX_FAILURE_NAME

    @property
    def database_path(self) -> Path:
        return self._database

    def query(
        self,
        *,
        fingerprint: str | None,
        limit: int,
    ) -> tuple[SessionProjectionRow, ...] | None:
        if not self._database.exists() or self._dirty.exists():
            return None
        self._validate_index_paths(create=False)
        try:
            with closing(self._connect()) as connection, connection:
                meta = connection.execute(
                    "SELECT schema_version, root_mtime_ns, ready FROM metadata "
                    "WHERE singleton = 1"
                ).fetchone()
                if meta is None or int(meta[0]) != _SCHEMA_VERSION:
                    raise SessionProjectionError("ProjectionSchemaMismatch")
                if not int(meta[2]):
                    return None
                if int(meta[1]) != self._root_mtime_ns():
                    return None
                parameters: list[object] = []
                where = ""
                if fingerprint is not None:
                    where = "WHERE fingerprint = ?"
                    parameters.append(fingerprint)
                parameters.append(max(0, int(limit)))
                selected = connection.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM sessions {where} "
                    "ORDER BY rank_mtime_ns DESC, rank_saved_at DESC, id DESC "
                    "LIMIT ?",
                    parameters,
                ).fetchall()
                newest = connection.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM sessions "
                    "ORDER BY rank_mtime_ns DESC, rank_saved_at DESC, id DESC "
                    "LIMIT 1"
                ).fetchone()
        except (OSError, sqlite3.Error, TypeError, ValueError) as error:
            raise SessionProjectionError(type(error).__name__) from None
        validation_rows = list(selected)
        if newest is not None and all(row[0] != newest[0] for row in validation_rows):
            validation_rows.append(newest)
        if not all(self._source_is_current(row) for row in validation_rows):
            return None
        return tuple(_row_from_sql(row) for row in selected)

    def replace(self, rows: Iterable[SessionProjectionRow]) -> None:
        self._validate_index_paths(create=True)
        materialized = tuple(rows)
        try:
            with closing(self._connect()) as connection, connection:
                self._ensure_schema(connection)
                connection.execute("DELETE FROM sessions")
                connection.executemany(_UPSERT_SQL, map(_row_to_sql, materialized))
                self._write_metadata(connection, ready=1)
            self._clear_dirty()
            self._clear_failure()
        except (OSError, sqlite3.Error, TypeError, ValueError) as error:
            raise SessionProjectionError(type(error).__name__) from None

    def mark_dirty(self) -> bool:
        """Mark an existing ready projection stale before authoritative writes."""
        if not self._database.exists():
            return False
        self._validate_index_paths(create=False)
        try:
            with closing(self._connect()) as connection, connection:
                meta = connection.execute(
                    "SELECT schema_version, ready FROM metadata WHERE singleton = 1"
                ).fetchone()
            if meta is None or int(meta[0]) != _SCHEMA_VERSION or not int(meta[1]):
                return False
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self._dirty, flags, 0o600)
            os.close(descriptor)
        except (OSError, sqlite3.Error, TypeError, ValueError) as error:
            raise SessionProjectionError(type(error).__name__) from None
        return True

    def upsert(self, row: SessionProjectionRow) -> None:
        self._validate_index_paths(create=False)
        try:
            with closing(self._connect()) as connection, connection:
                self._ensure_schema(connection)
                connection.execute(_UPSERT_SQL, _row_to_sql(row))
                self._write_metadata(connection, ready=1)
            self._clear_dirty()
        except (OSError, sqlite3.Error, TypeError, ValueError) as error:
            raise SessionProjectionError(type(error).__name__) from None

    def summary(self) -> SessionProjectionSummary | None:
        rows = self.query(fingerprint=None, limit=1)
        if rows is None:
            return None
        try:
            with closing(self._connect()) as connection, connection:
                values = connection.execute(
                    "SELECT COUNT(*), COALESCE(SUM(prompt_tokens), 0), "
                    "COALESCE(SUM(completion_tokens), 0), "
                    "COALESCE(SUM(event_count), 0), "
                    "COALESCE(SUM(request_count), 0), "
                    "COALESCE(SUM(checkpoint_count), 0) FROM sessions"
                ).fetchone()
        except (sqlite3.Error, TypeError, ValueError) as error:
            raise SessionProjectionError(type(error).__name__) from None
        assert values is not None
        return SessionProjectionSummary(*(int(value) for value in values))

    def reset(self) -> None:
        """Remove only derived index files; session artifacts are untouched."""
        if not self._index_dir.exists():
            return
        self._validate_index_paths(create=False, allow_invalid_database=True)
        try:
            for path in (
                self._database,
                self._database.with_name(f"{self._database.name}-journal"),
                self._database.with_name(f"{self._database.name}-wal"),
                self._database.with_name(f"{self._database.name}-shm"),
                self._dirty,
            ):
                status = _lstat_optional(path)
                if status is not None:
                    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
                        raise SessionProjectionError("ProjectionPathError")
                    path.unlink()
        except OSError as error:
            raise SessionProjectionError(type(error).__name__) from None

    def retain_failure(self, error_type: str) -> None:
        """Best-effort durable safe fact for a later inventory consumer."""
        safe_type = _safe_error_type_name(error_type)
        self._validate_index_paths(create=True, allow_invalid_database=True)
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self._failure, flags, 0o600)
            try:
                os.write(descriptor, safe_type.encode("ascii"))
            finally:
                os.close(descriptor)
        except OSError as error:
            raise SessionProjectionError(type(error).__name__) from None

    def consume_failure(self) -> str | None:
        status = _lstat_optional(self._failure)
        if status is None:
            return None
        self._validate_index_paths(create=False, allow_invalid_database=True)
        try:
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
                raise SessionProjectionError("ProjectionPathError")
            error_type = _safe_error_type_name(
                self._failure.read_text(encoding="ascii")
            )
            self._failure.unlink()
            return error_type
        except (OSError, UnicodeError) as error:
            raise SessionProjectionError(type(error).__name__) from None

    def _source_is_current(self, sql_row: tuple) -> bool:
        session_id = sql_row[0]
        source_kind = sql_row[7]
        expected_mtime_ns = sql_row[8]
        if not _is_safe_id(session_id) or source_kind not in {"manifest", "legacy"}:
            raise SessionProjectionError("ProjectionRowValidationError")
        names = session_path_candidates(session_id)
        sources = (
            tuple(self._sessions_dir / name / "manifest.json" for name in names)
            if source_kind == "manifest"
            else tuple(self._sessions_dir / f"{name}.json" for name in names)
        )
        for source in sources:
            try:
                status = source.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise SessionProjectionError(type(error).__name__) from None
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
                return False
            return status.st_mtime_ns == int(expected_mtime_ns)
        return False

    def _validate_index_paths(
        self,
        *,
        create: bool,
        allow_invalid_database: bool = False,
    ) -> None:
        try:
            root_status = self._sessions_dir.lstat()
            if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(root_status.st_mode):
                raise SessionProjectionError("ProjectionPathError")
            index_status = _lstat_optional(self._index_dir)
            if index_status is None and create:
                self._index_dir.mkdir(mode=0o700)
                index_status = self._index_dir.lstat()
            if index_status is None:
                raise SessionProjectionError("ProjectionPathError")
            if stat.S_ISLNK(index_status.st_mode) or not stat.S_ISDIR(
                index_status.st_mode
            ):
                raise SessionProjectionError("ProjectionPathError")
            database_status = _lstat_optional(self._database)
            if database_status is not None and (
                stat.S_ISLNK(database_status.st_mode)
                or (
                    not allow_invalid_database
                    and not stat.S_ISREG(database_status.st_mode)
                )
            ):
                raise SessionProjectionError("ProjectionPathError")
            for auxiliary in (self._dirty, self._failure):
                auxiliary_status = _lstat_optional(auxiliary)
                if auxiliary_status is not None and (
                    stat.S_ISLNK(auxiliary_status.st_mode)
                    or not stat.S_ISREG(auxiliary_status.st_mode)
                ):
                    raise SessionProjectionError("ProjectionPathError")
        except SessionProjectionError:
            raise
        except OSError as error:
            raise SessionProjectionError(type(error).__name__) from None

    def _root_mtime_ns(self) -> int:
        return self._sessions_dir.stat().st_mtime_ns

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=1.0)
        connection.execute("PRAGMA busy_timeout = 1000")
        return connection

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS metadata ("
            "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
            "schema_version INTEGER NOT NULL, root_mtime_ns INTEGER NOT NULL, "
            "ready INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "id TEXT PRIMARY KEY, model TEXT NOT NULL, saved_at TEXT NOT NULL, "
            "preview TEXT NOT NULL, fingerprint TEXT NOT NULL, "
            "rank_mtime_ns INTEGER NOT NULL, rank_saved_at REAL NOT NULL, "
            "source_kind TEXT NOT NULL, source_mtime_ns INTEGER NOT NULL, "
            "prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL, "
            "event_count INTEGER NOT NULL, request_count INTEGER NOT NULL, "
            "checkpoint_count INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS sessions_fingerprint_rank "
            "ON sessions(fingerprint, rank_mtime_ns DESC, rank_saved_at DESC, id DESC)"
        )

    def _write_metadata(self, connection: sqlite3.Connection, *, ready: int) -> None:
        connection.execute(
            "INSERT INTO metadata(singleton, schema_version, root_mtime_ns, ready) "
            "VALUES(1, ?, ?, ?) ON CONFLICT(singleton) DO UPDATE SET "
            "schema_version=excluded.schema_version, "
            "root_mtime_ns=excluded.root_mtime_ns, ready=excluded.ready",
            (_SCHEMA_VERSION, self._root_mtime_ns(), ready),
        )

    def _clear_dirty(self) -> None:
        status = _lstat_optional(self._dirty)
        if status is None:
            return
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise SessionProjectionError("ProjectionPathError")
        self._dirty.unlink()

    def _clear_failure(self) -> None:
        status = _lstat_optional(self._failure)
        if status is None:
            return
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise SessionProjectionError("ProjectionPathError")
        self._failure.unlink()


_SELECT_COLUMNS = (
    "id, model, saved_at, preview, fingerprint, rank_mtime_ns, rank_saved_at, "
    "source_kind, source_mtime_ns, prompt_tokens, completion_tokens, "
    "event_count, request_count, checkpoint_count"
)
_UPSERT_SQL = (
    "INSERT INTO sessions("
    + _SELECT_COLUMNS
    + ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(id) DO UPDATE SET "
    "model=excluded.model, saved_at=excluded.saved_at, preview=excluded.preview, "
    "fingerprint=excluded.fingerprint, rank_mtime_ns=excluded.rank_mtime_ns, "
    "rank_saved_at=excluded.rank_saved_at, source_kind=excluded.source_kind, "
    "source_mtime_ns=excluded.source_mtime_ns, "
    "prompt_tokens=excluded.prompt_tokens, "
    "completion_tokens=excluded.completion_tokens, "
    "event_count=excluded.event_count, request_count=excluded.request_count, "
    "checkpoint_count=excluded.checkpoint_count"
)


def _row_to_sql(row: SessionProjectionRow) -> tuple[object, ...]:
    metadata = row.metadata
    return (
        metadata.id,
        metadata.model,
        metadata.saved_at,
        metadata.preview,
        metadata.fingerprint,
        row.rank_mtime_ns,
        row.rank_saved_at,
        row.source_kind,
        row.source_mtime_ns,
        row.prompt_tokens,
        row.completion_tokens,
        row.event_count,
        row.request_count,
        row.checkpoint_count,
    )


def _row_from_sql(row: tuple) -> SessionProjectionRow:
    return SessionProjectionRow(
        metadata=SessionMetadata(
            id=row[0],
            model=row[1],
            saved_at=row[2],
            preview=row[3],
            fingerprint=row[4],
        ),
        rank_mtime_ns=int(row[5]),
        rank_saved_at=float(row[6]),
        source_kind=row[7],
        source_mtime_ns=int(row[8]),
        prompt_tokens=int(row[9]),
        completion_tokens=int(row[10]),
        event_count=int(row[11]),
        request_count=int(row[12]),
        checkpoint_count=int(row[13]),
    )


def _lstat_optional(path: Path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _is_safe_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 128
        and _SAFE_ID.fullmatch(value) is not None
        and ".." not in value
    )


def _safe_error_type_name(value: object) -> str:
    if (
        isinstance(value, str)
        and value
        and len(value) <= 64
        and value.isascii()
        and value.replace("_", "").isalnum()
    ):
        return value
    return "Exception"
