import base64
from dataclasses import replace
from threading import Event

import pytest

from reuleauxcoder.domain.images import ChatInput
from reuleauxcoder.infrastructure.rpc.peer import RpcError
from reuleauxcoder.app.rpc.codec import encode
from tests.domain.test_images import picture


def attach(runtime, tmp_path):
    path = tmp_path / "screen.png"
    path.write_bytes(picture(size=(400, 300)))
    return runtime.client.attach_image(str(path))


def test_image_preview_validates_session_import_metadata_and_size(runtime, tmp_path):
    image = attach(runtime, tmp_path)
    state = runtime.client.state
    params = dict(session_id=state.session_id, session_generation=state.session_generation)

    def preview(reference):
        return runtime.client.peer.request("images.preview", {**params, "image": encode(reference)})

    assert preview(image) == runtime.agent.image_store.data_url(state.session_id, image)
    # A committed turn may add its correlation ID without changing the image.
    assert preview(replace(image, turn_id="sent-turn")) == preview(image)
    for invalid, message in [
        (replace(image, width=image.width + 1), "metadata"),
        (replace(image, size_bytes=3 * 1024 * 1024), "size"),
        (replace(image, attachment_id="0" * 64), "not imported"),
        ({"path": str(tmp_path / "screen.png")}, "reference"),
    ]:
        with pytest.raises(RpcError, match=message):
            preview(invalid)
    runtime.agent.reset()
    with pytest.raises(RpcError, match="different session"):
        preview(image)


def test_attachment_admission_preserves_draft_until_capable_model(runtime, tmp_path):
    image = attach(runtime, tmp_path)
    state = runtime.client.state
    value = ChatInput("inspect", (image,), state.session_id, state.session_generation)
    with pytest.raises(RpcError, match="does not support images"):
        runtime.client.submit(value)
    assert runtime.agent.messages == []
    runtime.agent.llm.support_modal = ("text", "image")
    assert runtime.client.submit(value).status == "running"
    runtime.client.wait_idle()
    assert (
        runtime.agent.messages[0]["content"][1]["attachment_id"] == image.attachment_id
    )
    assert runtime.agent.messages[0]["content"][1]["turn_id"]


def test_stale_attachment_and_tampered_reference_are_rejected(runtime, tmp_path):
    runtime.agent.llm.support_modal = ("text", "image")
    image = attach(runtime, tmp_path)
    state = runtime.client.state
    value = ChatInput(
        "",
        (replace(image, width=image.width + 1),),
        state.session_id,
        state.session_generation,
    )
    with pytest.raises(RpcError, match="metadata"):
        runtime.client.submit(value)
    runtime.agent.reset()
    with pytest.raises(RpcError, match="different session"):
        runtime.client.submit(replace(value, images=(image,)))


def test_upload_rejects_stale_session_and_out_of_order_chunks(runtime):
    state = runtime.client.state
    raw = picture(size=(10, 10))
    upload = runtime.client.peer.request(
        "images.begin",
        {
            "session_id": state.session_id,
            "session_generation": state.session_generation,
            "name": "x.png",
            "size_bytes": len(raw),
        },
    )
    params = {
        "upload_id": upload["upload_id"],
        "offset": 1,
        "data": base64.b64encode(raw).decode(),
    }
    with pytest.raises(RpcError, match="offset"):
        runtime.client.peer.request("images.append", params)
    runtime.agent.reset()
    with pytest.raises(RpcError, match="different session"):
        runtime.client.peer.request("images.append", {**params, "offset": 0})
    assert runtime.server.images.pending is None


def test_accepted_queued_images_survive_a_later_text_model_switch(runtime, tmp_path):
    runtime.agent.llm.support_modal = ("text", "image")
    image = attach(runtime, tmp_path)
    started, release = Event(), Event()

    def run():
        started.set()
        assert release.wait(5)
        runtime.agent.llm.support_modal = ("text",)
        return "done"

    runtime.loop.run = run
    runtime.client.submit("first")
    assert started.wait(5)
    try:
        # Exercise the closing-turn window, where steering becomes queued input.
        runtime.agent._accepting_user_steering = False
        state = runtime.client.state
        value = ChatInput(
            "queued", (image,), state.session_id, state.session_generation
        )
        assert runtime.client.submit(value).status == "queued"
    finally:
        release.set()
    runtime.client.wait_idle()
    assert (
        runtime.agent.messages[-1]["content"][1]["attachment_id"] == image.attachment_id
    )
