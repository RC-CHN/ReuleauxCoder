from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from reuleauxcoder.domain.images import ImageConfig, image_parts, image_request_stats
from reuleauxcoder.infrastructure.persistence.images import ImageStore
from reuleauxcoder.services.llm.client import LLM
from reuleauxcoder.services.llm.factory import reconfigure_llm_from_settings
from reuleauxcoder.domain.config.models import ModelProfileConfig
from reuleauxcoder.services.llm.providers import (
    ProviderHTTPError,
    _chat_messages,
    _responses_input,
    _anthropic_request,
)
from tests.domain.test_images import picture


def stream():
    return iter(
        [
            SimpleNamespace(
                usage=None,
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content="Seen.", tool_calls=None, reasoning_content=None
                        ),
                    )
                ],
            )
        ]
    )


def switch(llm, capable=False):
    reconfigure_llm_from_settings(
        llm,
        ModelProfileConfig(
            name="test",
            model="vision" if capable else "text",
            api_key="test",
            support_modal=("text", "image") if capable else ("text",),
        ),
    )


@pytest.fixture
def image_llm(tmp_path):
    llm = LLM(model="vision", api_key="test", support_modal=("text", "image"))
    llm.image_store = ImageStore(tmp_path, ImageConfig(originals_cache_max_bytes=1))
    llm._call_with_retry = lambda params: stream()
    image = replace(
        llm.image_store.import_bytes("session", picture(size=(80, 60))), turn_id="turn1"
    )
    yield llm, image
    llm.client.close()


def test_413_recovery_is_finite_and_keeps_new_tool_images(image_llm):
    llm, image = image_llm
    images = [image]
    for color in ("red", "blue"):
        images.append(
            replace(
                llm.image_store.import_bytes(
                    "session", picture(size=(80, 60), color=color)
                ),
                turn_id="turn1",
            )
        )
    messages = [{"role": "user", "content": [item.to_part() for item in images]}]
    original = deepcopy(messages)
    seen = []

    def provider(params):
        count = len(image_parts(params["messages"]))
        seen.append(count)
        if count:
            raise ProviderHTTPError("test", 413)
        return stream()

    llm._call_with_retry = provider
    llm.chat(messages, session_id="session", metadata={"image_turn_id": "turn1"})
    assert seen == [3, 2, 0]
    assert [item.get("status_code") for item in llm.last_image_attempts] == [
        413,
        413,
        None,
    ]
    assert sum(item["image_base64_bytes"] for item in llm.last_image_attempts) > sum(
        4 * ((item.size_bytes + 2) // 3) for item in images
    )
    assert messages == original
    fresh = replace(
        llm.image_store.import_bytes("session", picture(size=(80, 60), color="green")),
        turn_id="turn1",
    )
    messages.append({"role": "user", "content": [fresh.to_part()]})
    llm._call_with_retry = lambda params: stream()
    llm.chat(
        messages,
        session_id="session",
        metadata={"image_turn_id": "turn1", "turn_id": "goal-auto-continuation"},
    )
    assert len(image_parts(llm.last_dispatched_request["messages"])) == 1
    switch(llm)
    llm.chat(messages, session_id="session")
    switch(llm, True)
    llm.chat(messages, session_id="session", metadata={"image_turn_id": "turn1"})
    assert len(image_parts(llm.last_dispatched_request["messages"])) == 4


def test_normal_retries_are_counted_as_image_payloads(image_llm, monkeypatch):
    llm, image = image_llm
    del llm._call_with_retry
    seen = []

    def provider(params):
        seen.append(deepcopy(params))
        if len(seen) == 1:
            raise ProviderHTTPError("test", 503)
        return stream()

    llm._provider_adapter.open_stream = provider
    llm._provider_adapter.is_retryable = lambda error: error.status_code == 503
    monkeypatch.setattr("reuleauxcoder.services.llm.client.time.sleep", lambda _: None)
    llm.chat([{"role": "user", "content": [image.to_part()]}], session_id="session")
    assert len(llm.last_image_attempts) == 2
    assert (
        llm.last_image_attempts[0]["image_base64_bytes"]
        == llm.last_image_attempts[1]["image_base64_bytes"]
        > 0
    )


def test_three_provider_image_mappings_preserve_tool_results(image_llm):
    llm, image = image_llm
    wire = {
        "type": "image_url",
        "image_url": {"url": llm.image_store.data_url("session", image)},
    }
    calls = [
        {
            "id": str(i),
            "type": "function",
            "function": {"name": "view_image", "arguments": "{}"},
        }
        for i in range(2)
    ]
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "look"}, wire]},
        {"role": "assistant", "content": None, "tool_calls": calls},
        {
            "role": "tool",
            "tool_call_id": "0",
            "content": [{"type": "text", "text": "crop"}, wire],
        },
        {"role": "tool", "tool_call_id": "1", "content": "other result"},
    ]
    original = deepcopy(messages)
    chat = _chat_messages(messages)
    assert [item["role"] for item in chat] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "user",
    ]
    assert chat[2]["tool_call_id"] == "0"
    assert len(image_parts(chat)) == 2
    responses = _responses_input(messages, volatile_tail_count=0, cache_mode="implicit")
    output = next(
        item for item in responses if item.get("type") == "function_call_output"
    )
    assert output["call_id"] == "0"
    assert output["output"][1]["type"] == "input_image"
    anthropic = _anthropic_request(
        {"model": "vision", "max_tokens": 100, "messages": messages}
    )
    tool_result = anthropic["messages"][-1]["content"][0]
    assert tool_result["tool_use_id"] == "0"
    assert tool_result["content"][1]["type"] == "image"
    assert messages == original
    assert image_request_stats(messages)["image_count"] == 2
