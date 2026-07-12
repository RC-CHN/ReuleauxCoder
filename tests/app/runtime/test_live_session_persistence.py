from reuleauxcoder.app.runtime.session_state import bind_session_persistence
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore


class _LLM:
    model = "model"
    debug_trace = False


def test_committed_messages_and_plan_are_durable_before_exit(tmp_path) -> None:
    config = Config(api_key="key", session_dir=str(tmp_path))
    agent = Agent(llm=_LLM(), tools=[], config=config)
    store = SessionStore(tmp_path)
    session_id = store.generate_session_id()
    bind_session_persistence(
        config, agent, store, session_id, fingerprint="local"
    )

    agent._append_message(
        {"role": "user", "content": "durable before exit"}, source="user"
    )
    agent.plan_controller.update(
        [{"step": "Verify", "active_form": "Verifying", "status": "in_progress"}],
        explanation=None,
        tool_call_id="plan",
        session_generation=0,
    )

    loaded = store.load(session_id)
    assert loaded is not None
    assert loaded.messages[0]["content"] == "durable before exit"
    assert loaded.runtime_state.plan_state["revision"] == 1
    assert any(event.kind == "plan_updated" for event in loaded.history_events)


def test_unbound_reset_does_not_replace_saved_session_view(tmp_path) -> None:
    config = Config(api_key="key", session_dir=str(tmp_path))
    agent = Agent(llm=_LLM(), tools=[], config=config)
    store = SessionStore(tmp_path)
    session_id = store.generate_session_id()
    bind_session_persistence(
        config, agent, store, session_id, fingerprint="local"
    )
    agent._append_message({"role": "user", "content": "keep me"}, source="user")

    agent.unbind_session_persistence()
    agent.reset()

    loaded = store.load(session_id)
    assert loaded is not None
    assert loaded.messages[0]["content"] == "keep me"
