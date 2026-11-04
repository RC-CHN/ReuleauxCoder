from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.config.models import Config, ContextConfig
from reuleauxcoder.domain.context.replay import canonicalize, content_hash
from reuleauxcoder.domain.images import ChatInput, ImageConfig, image_parts
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.domain.runtime.serialization import (
    tool_outcome_from_dict,
    tool_outcome_to_dict,
)
from reuleauxcoder.extensions.tools.backend import ExecutionContext, LocalToolBackend
from reuleauxcoder.extensions.tools.builtin.images import ViewImageTool
from reuleauxcoder.infrastructure.persistence.images import ImageStore
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.services.llm.client import LLM
from tests.domain.test_images import picture
from tests.services.test_llm_images import stream, switch


def make_agent(tmp_path, retention="history"):
    config = Config(
        api_key="test",
        support_modal=("text", "image"),
        session_auto_save=False,
        context=ContextConfig(image_retention=retention, summarize_keep_recent_turns=1),
    )
    llm = LLM(model="vision", api_key="test", support_modal=("text", "image"))
    llm._call_with_retry = lambda params: stream()
    agent = Agent(llm, tools=[], config=config)
    agent.current_session_id = "session"
    agent.image_store = llm.image_store = ImageStore(
        tmp_path, ImageConfig(originals_cache_max_bytes=1)
    )
    image = agent.image_store.import_bytes("session", picture(size=(80, 60)))
    return agent, image


def test_restore_after_text_model_keeps_canonical_image_and_provenance(tmp_path):
    agent, image = make_agent(tmp_path)
    try:
        agent.chat(ChatInput("inspect", (image,)))
        original = agent.messages[0].copy()
        vision_tokens = agent.context.get_context_tokens([original])
        sent = image_parts(agent.llm.last_dispatched_request["messages"])
        image_event = next(
            event
            for event in agent.history_ledger.events
            if event.kind == "message_committed"
        )
        assert agent.replay_envelope.item_provenance[0]["source_event_ids"] == [
            image_event.event_id
        ]
        assert agent.request_envelopes[-1].canonical_request_hash == content_hash(
            agent.llm.last_dispatched_request
        )
        assert any(
            event.kind == "image_payload_observed"
            for event in agent.history_ledger.events
        )
        switch(agent.llm)
        assert agent.context.get_context_tokens([original]) < vision_tokens // 2
        agent.chat("continue in text")
        assert not image_parts(agent.llm.last_dispatched_request["messages"])
        assert agent.messages[0] == original
        store = SessionStore(tmp_path)
        store.save(
            agent.messages,
            agent.llm.model,
            session_id="session",
            history_events=agent.history_ledger.events,
            replay_envelope=agent.replay_envelope,
            request_envelopes=agent.request_envelopes,
        )
        loaded = store.load("session")
        restored = Agent(agent.llm, tools=[], config=agent.runtime_config)
        restored.current_session_id = "session"
        restored.restore_history_runtime(loaded)
        assert canonicalize(restored.messages[0]) == canonicalize(original)
        assert restored.image_turn_id == agent.image_turn_id
        switch(restored.llm, True)
        restored.chat("look again")
        assert image_parts(restored.llm.last_dispatched_request["messages"]) == sent
    finally:
        agent.llm.client.close()


def test_user_turn_owner_survives_steering_goal_and_model_switch(tmp_path):
    agent, image = make_agent(tmp_path, "user_turn")
    try:
        agent.chat(ChatInput("inspect", (image,)))
        owner = agent.image_turn_id
        agent._current_turn_id = "steering"
        agent._accepting_user_steering = True
        assert agent.submit_user_steering(ChatInput("more detail", (image,)))
        assert agent._drain_user_steering() == 1
        assert all(part["turn_id"] == owner for part in image_parts(agent.messages))
        switch(agent.llm)
        agent.chat("", goal_continuation=True)
        assert agent.image_turn_id == owner
        assert not image_parts(agent.llm.last_dispatched_request["messages"])
        switch(agent.llm, True)
        agent.chat("", goal_continuation=True)
        assert len(image_parts(agent.llm.last_dispatched_request["messages"])) == 2
        agent.chat("a new user turn")
        assert agent.image_turn_id != owner
        assert not image_parts(agent.llm.last_dispatched_request["messages"])
        assert len(image_parts(agent.messages)) == 2
        for capable in (False, True):
            switch(agent.llm, capable)
            agent.chat("", goal_continuation=True)
            assert not image_parts(agent.llm.last_dispatched_request["messages"])
    finally:
        agent.llm.client.close()


def test_text_model_compaction_does_not_resurrect_removed_images(tmp_path):
    agent, image = make_agent(tmp_path)
    try:
        agent.chat(ChatInput("inspect", (image,)))
        switch(agent.llm)
        for index in range(7):
            agent.chat(f"follow up {index}")
        assert agent.force_compress_context("collapse", None)
        assert not image_parts(agent.messages)
        assert image.attachment_id in str(
            [event.payload for event in agent.history_ledger.events]
        )
        switch(agent.llm, True)
        agent.chat("continue after compaction")
        assert not image_parts(agent.llm.last_dispatched_request["messages"])
    finally:
        agent.llm.client.close()


def test_initial_text_only_path_image_restores_after_switching_to_vision(tmp_path):
    agent, _ = make_agent(tmp_path)
    try:
        data = picture(size=(80, 60))
        (tmp_path / "initial.png").write_bytes(data)
        image = agent.image_store.import_bytes(
            "session", data, name="initial.png", path="initial.png"
        )
        switch(agent.llm)
        agent.chat(ChatInput("inspect", (image,)))
        request_text = str(agent.llm.last_dispatched_request["messages"])
        assert not image_parts(agent.llm.last_dispatched_request["messages"])
        assert "initial.png" in request_text
        assert "does not support image input" in request_text
        assert len(image_parts(agent.messages)) == 1

        switch(agent.llm, True)
        agent.chat("look now")
        assert len(image_parts(agent.llm.last_dispatched_request["messages"])) == 1
        assert len(image_parts(agent.messages)) == 1
    finally:
        agent.llm.client.close()


def test_view_image_tool_pipeline_keeps_typed_reference(tmp_path):
    agent, _ = make_agent(tmp_path)
    try:
        # This test needs an original to create a crop.
        agent.image_store = agent.llm.image_store = ImageStore(tmp_path)
        (tmp_path / "workspace.png").write_bytes(picture(size=(400, 300)))
        tool = ViewImageTool(
            LocalToolBackend(
                ExecutionContext(cwd=str(tmp_path), workspace_root=str(tmp_path))
            )
        )
        tool.bind_agent(agent)
        agent.tools.append(tool)
        agent.image_turn_id = "turn"
        result = agent._executor.execute(
            ToolCall(
                id="view",
                name="view_image",
                arguments={
                    "path": "workspace.png",
                    "region": [10, 10, 100, 100],
                },
            )
        )
        assert isinstance(result, list)
        agent._append_message(
            {"role": "tool", "tool_call_id": "view", "content": result},
            source="tool_result",
        )
        assert image_parts(agent.messages)[0]["turn_id"] == "turn"
        assert image_parts(agent.messages)[0]["path"] == "workspace.png"
        outcome = tool.execute("workspace.png", region=[10, 10, 100, 100])
        restored = tool_outcome_from_dict(tool_outcome_to_dict(outcome))
        assert restored.images == outcome.images
        switch(agent.llm)
        withheld = tool.execute("workspace.png")
        assert withheld.success
        assert withheld.metadata["image_withheld"] is True
    finally:
        agent.llm.client.close()
