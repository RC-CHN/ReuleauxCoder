import base64

import pytest

from reuleauxcoder.app.rpc.attachments import (
    ATTACHMENT_CHUNK_BYTES,
    ATTACHMENT_MAX_BYTES,
)
from reuleauxcoder.app.rpc.codec import decode
from reuleauxcoder.infrastructure.rpc.peer import RpcError


@pytest.fixture
def uploads(runtime, tmp_path):
    runtime.agent.runtime_working_directory = str(tmp_path)
    return runtime


def begin(runtime, name="notes.txt", size=0):
    state = runtime.client.state
    return runtime.client.peer.request(
        "attachments.begin",
        {
            "session_id": state.session_id,
            "session_generation": state.session_generation,
            "name": name,
            "size_bytes": size,
        },
    )["upload_id"]


def append(runtime, upload_id, data, offset=0):
    return runtime.client.peer.request(
        "attachments.append",
        {
            "upload_id": upload_id,
            "offset": offset,
            "data": base64.b64encode(data).decode(),
        },
    )


def complete(runtime, upload_id):
    return decode(
        runtime.client.peer.request("attachments.complete", {"upload_id": upload_id})
    )


def test_streamed_attachment_preserves_bytes_and_same_name_files(uploads, tmp_path):
    data = bytes(range(256)) * 2200
    upload_id = begin(uploads, "资料.bin", len(data))
    assert not (tmp_path / ".rcoder" / "attachments").exists()
    for offset in range(0, len(data), ATTACHMENT_CHUNK_BYTES):
        chunk = data[offset : offset + ATTACHMENT_CHUNK_BYTES]
        assert append(uploads, upload_id, chunk, offset) == offset + len(chunk)
    first = complete(uploads, upload_id)
    second = complete(uploads, begin(uploads, "资料.bin"))
    assert first.name == second.name == "资料.bin"
    assert first.attachment_id != second.attachment_id
    assert (
        first.path == f".rcoder/attachments/test-session/{first.attachment_id}/资料.bin"
    )
    assert first.mime_type == "application/octet-stream"
    assert first.size_bytes == len(data)
    assert second.size_bytes == 0
    assert (tmp_path / first.path).read_bytes() == data
    assert (tmp_path / second.path).read_bytes() == b""
    assert uploads.agent.messages == []  # Upload is not a chat submission.
    assert uploads.server.attachments.pending is None


@pytest.mark.parametrize("size", [-1, True, 1.5, "10", ATTACHMENT_MAX_BYTES + 1])
def test_reject_declared_sizes_before_allocating(uploads, tmp_path, size):
    with pytest.raises(RpcError, match="size"):
        begin(uploads, size=size)
    assert uploads.server.attachments.pending is None
    assert not (tmp_path / ".rcoder" / "attachments").exists()


@pytest.mark.parametrize(
    "name",
    [
        "",
        "../escape",
        "/tmp/escape",
        "a\\b",
        "..",
        "NUL.txt",
        "a:",
        "a\x00b",
        "尾" * 86,
    ],
)
def test_reject_unsafe_names(uploads, name):
    with pytest.raises(RpcError, match="filename"):
        begin(uploads, name)


def test_reject_bad_chunks_and_incomplete_upload(uploads):
    upload_id = begin(uploads, size=2)
    with pytest.raises(RpcError, match="incomplete"):
        complete(uploads, upload_id)
    with pytest.raises(RpcError, match="offset"):
        append(uploads, upload_id, b"x", 1)
    with pytest.raises(RpcError, match="exceeds"):
        append(uploads, upload_id, b"xxx")
    with pytest.raises(RpcError, match="size"):
        append(uploads, upload_id, b"x" * (ATTACHMENT_CHUNK_BYTES + 3))
    # A decoded chunk can exceed the limit even when Base64 length fits.
    with pytest.raises(RpcError, match="exceeds"):
        append(uploads, upload_id, b"x" * (ATTACHMENT_CHUNK_BYTES + 1))
    with pytest.raises(RpcError, match="encoding"):
        uploads.client.peer.request(
            "attachments.append", {"upload_id": upload_id, "offset": 0, "data": "!!!!"}
        )
    assert uploads.server.attachments.pending.received == 0
    append(uploads, upload_id, b"o")
    with pytest.raises(RpcError, match="exceeds"):
        append(uploads, upload_id, b"kk", 1)
    append(uploads, upload_id, b"k", 1)
    assert complete(uploads, upload_id).size_bytes == 2


def test_limit_is_inclusive_and_cancel_closes_temporary_file(uploads, tmp_path):
    upload_id = begin(uploads, size=ATTACHMENT_MAX_BYTES)
    file = uploads.server.attachments.pending.file
    append(uploads, upload_id, b"partial")
    uploads.client.peer.request("attachments.cancel", {"upload_id": upload_id})
    assert file.closed
    assert uploads.server.attachments.pending is None
    assert not (tmp_path / ".rcoder" / "attachments").exists()


@pytest.mark.parametrize("change", ["session", "workspace"])
def test_context_change_rejects_and_discards_upload(uploads, tmp_path, change):
    upload_id = begin(uploads, size=1)
    file = uploads.server.attachments.pending.file
    if change == "session":
        uploads.agent.reset()
    else:
        uploads.agent.runtime_working_directory = str(tmp_path / "other")
    with pytest.raises(RpcError, match="session|Workspace"):
        append(uploads, upload_id, b"x")
    assert file.closed
    assert uploads.server.attachments.pending is None


def test_replacement_and_shutdown_close_pending_upload(uploads):
    first = begin(uploads)
    file = uploads.server.attachments.pending.file
    second = begin(uploads)
    assert file.closed
    with pytest.raises(RpcError, match="Unknown"):
        complete(uploads, first)
    uploads.client.peer.request("attachments.cancel", {"upload_id": first})
    assert uploads.server.attachments.pending.upload_id == second
    file = uploads.server.attachments.pending.file
    uploads.client.shutdown()
    assert file.closed


def test_reject_symlinked_storage(uploads, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (tmp_path / ".rcoder").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    with pytest.raises(RpcError, match="symlinks"):
        complete(uploads, begin(uploads))
    assert list(outside.iterdir()) == []
    assert uploads.server.attachments.pending is None


def test_storage_copies_in_bounded_reads(tmp_path):
    from reuleauxcoder.infrastructure.persistence.attachments import store_attachment

    class Source:
        calls = 0

        def read(self, size):
            assert 0 < size <= ATTACHMENT_CHUNK_BYTES
            self.calls += 1
            return b"x" * size if self.calls < 4 else b""

    source = Source()
    result = store_attachment(tmp_path, "test-session", "archive.zip", source)
    assert result.size_bytes == 3 * ATTACHMENT_CHUNK_BYTES
    assert (tmp_path / result.path).stat().st_size == result.size_bytes
    assert not list(tmp_path.rglob(".upload-*"))


def test_failed_publish_leaves_no_partial_attachment(uploads, tmp_path, monkeypatch):
    from reuleauxcoder.infrastructure.persistence import attachments

    def fail_copy(source, target, length):
        target.write(b"partial")
        raise OSError("Disk full")

    monkeypatch.setattr(attachments.shutil, "copyfileobj", fail_copy)
    upload_id = begin(uploads, size=2)
    append(uploads, upload_id, b"ok")
    file = uploads.server.attachments.pending.file
    with pytest.raises(RpcError, match="Disk full"):
        complete(uploads, upload_id)
    assert file.closed
    assert uploads.server.attachments.pending is None
    assert list((tmp_path / ".rcoder/attachments/test-session").iterdir()) == []
