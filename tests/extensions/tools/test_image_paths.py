import base64
from types import SimpleNamespace

import pytest

from reuleauxcoder.domain.images import ImageConfig, image_parts, project_images
from reuleauxcoder.domain.workspace import WorkspaceError
from reuleauxcoder.extensions.remote_exec.backend import RemoteWorkspacePort
from reuleauxcoder.extensions.tools.backend import ExecutionContext, LocalToolBackend
from reuleauxcoder.extensions.tools.builtin.images import ViewImageTool
from reuleauxcoder.infrastructure.persistence.images import ImageStore
from tests.domain.test_images import picture


def _tool(tmp_path, *, modalities=("text", "image"), config=None):
    store = ImageStore(tmp_path / "images", config)
    agent = SimpleNamespace(
        image_store=store,
        current_session_id="session",
        llm=SimpleNamespace(support_modal=modalities),
    )
    tool = ViewImageTool(
        LocalToolBackend(
            ExecutionContext(cwd=str(tmp_path), workspace_root=str(tmp_path))
        )
    )
    tool.bind_agent(agent)
    return store, tool


def test_path_import_reuses_content_variants_and_preserves_each_path(tmp_path):
    store, tool = _tool(tmp_path)
    data = picture(size=(400, 300))
    (tmp_path / "first.png").write_bytes(data)
    (tmp_path / "second.png").write_bytes(data)

    first = tool.execute("first.png")
    second = tool.execute("second.png")
    cropped = tool.execute("second.png", region=[10, 10, 100, 100])

    assert first.success and second.success and cropped.success
    assert first.images[0].attachment_id == second.images[0].attachment_id
    assert first.images[0].variant_id == second.images[0].variant_id
    assert first.images[0].path == "first.png"
    assert second.images[0].path == "second.png"
    assert cropped.images[0].path == "second.png"
    store.validate_reference("session", first.images[0])
    store.validate_reference("session", second.images[0])


def test_path_read_errors_and_text_only_projection(tmp_path):
    store, tool = _tool(
        tmp_path,
        modalities=("text",),
        config=ImageConfig(import_max_bytes=64),
    )
    assert not tool.execute("missing.png").success
    assert not tool.execute(".").success
    (tmp_path / "invalid.png").write_bytes(b"not an image")
    assert not tool.execute("invalid.png").success
    (tmp_path / "large.png").write_bytes(b"x" * 65)
    limited = tool.execute("large.png")
    assert not limited.success
    assert "read limit" in limited.content

    store.config = ImageConfig()
    (tmp_path / "valid.png").write_bytes(picture(size=(80, 60)))
    outcome = tool.execute("valid.png")
    assert outcome.success
    assert outcome.metadata["image_withheld"] is True
    reference = outcome.images[0]
    assert reference.path == "valid.png"
    assert "valid.png" in reference.caption()

    messages = [{"role": "tool", "content": [reference.to_part()]}]
    projected = project_images(messages, supports_images=False)
    assert not image_parts(projected)
    assert "valid.png" in projected[0]["content"][0]["text"]
    assert "does not support image input" in projected[0]["content"][0]["text"]

    projected_vision = project_images(
        messages,
        supports_images=True,
        load_image=lambda image: store.data_url("session", image),
    )
    assert len(image_parts(projected_vision)) == 1


def test_remote_binary_read_uses_capability_and_validates_base64(monkeypatch):
    backend = SimpleNamespace(
        context=SimpleNamespace(workspace_root="/workspace", cwd="/workspace"),
        supports_capability=lambda name: name == "workspace.fs.read_bytes",
    )
    port = RemoteWorkspacePort(backend)
    monkeypatch.setattr(
        port,
        "_request",
        lambda operation, **args: {"data": base64.b64encode(b"image").decode()},
    )
    assert port.read_bytes("image.png", max_bytes=10) == b"image"

    monkeypatch.setattr(port, "_request", lambda operation, **args: {"data": "%%%"})
    with pytest.raises(WorkspaceError, match="invalid remote binary data"):
        port.read_bytes("image.png", max_bytes=10)

    backend.supports_capability = lambda name: False
    with pytest.raises(WorkspaceError, match="upgrade the peer"):
        port.read_bytes("image.png", max_bytes=10)
