import pytest

from reuleauxcoder.app.runtime.approval import build_runtime_approval_provider
from reuleauxcoder.app.runtime.session_state import (
    apply_session_runtime_state,
    bind_session_persistence,
)
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.approval import (
    ApprovalDecision,
    ApprovalGrantCandidate,
    ApprovalRequest,
)
from reuleauxcoder.domain.approval_subjects import approval_scope_key
from reuleauxcoder.domain.config.models import ApprovalRuleConfig, Config
from reuleauxcoder.extensions.tools.backend import ExecutionContext, LocalToolBackend
from reuleauxcoder.extensions.tools.builtin.shell import ShellTool
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore


class _LLM:
    model = "model"
    debug_trace = False


def test_committed_messages_and_plan_are_durable_before_exit(tmp_path) -> None:
    config = Config(api_key="key", session_dir=str(tmp_path))
    agent = Agent(llm=_LLM(), tools=[], config=config)
    store = SessionStore(tmp_path)
    session_id = store.generate_session_id()
    bind_session_persistence(config, agent, store, session_id, fingerprint="local")

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
    bind_session_persistence(config, agent, store, session_id, fingerprint="local")
    agent._append_message({"role": "user", "content": "keep me"}, source="user")

    agent.unbind_session_persistence()
    agent.reset()

    loaded = store.load(session_id)
    assert loaded is not None
    assert loaded.messages[0]["content"] == "keep me"


def test_first_message_is_discoverable_and_later_bursts_coalesce(tmp_path) -> None:
    config = Config(api_key="key", session_dir=str(tmp_path))
    agent = Agent(llm=_LLM(), tools=[], config=config)
    store = SessionStore(tmp_path)
    session_id = store.generate_session_id()
    save_calls = []
    original_save = store.save

    def counted_save(*args, **kwargs):
        save_calls.append(True)
        return original_save(*args, **kwargs)

    store.save = counted_save  # type: ignore[method-assign]
    bind_session_persistence(config, agent, store, session_id, fingerprint="local")

    for index in range(4):
        agent._append_message(
            {"role": "user", "content": f"message-{index}"},
            source="user",
        )
        if index == 0:
            assert store.load(session_id).messages[0]["content"] == "message-0"
    agent.unbind_session_persistence()

    assert len(save_calls) == 2
    loaded = store.load(session_id)
    assert loaded is not None
    assert [message["content"] for message in loaded.messages] == [
        "message-0",
        "message-1",
        "message-2",
        "message-3",
    ]
    performance_names = {
        sample.name for sample in agent.performance_monitor.snapshot()
    }
    assert "history_ledger_write" in performance_names
    assert "session_snapshot" in performance_names


@pytest.mark.parametrize(("command_size", "approved"), [(2_000, True), (64_000, False)])
def test_shell_session_grants_preserve_exact_signatures_and_recover_after_failure(
    tmp_path, command_size, approved,
) -> None:
    config = Config(api_key="key", session_dir=str(tmp_path))
    shell = ShellTool(LocalToolBackend(ExecutionContext(cwd=str(tmp_path))))
    agent = Agent(llm=_LLM(), tools=[shell], config=config)
    store = SessionStore(tmp_path)
    session_id = store.generate_session_id()
    bind_session_persistence(config, agent, store, session_id, fingerprint="local")
    arguments = {"command": "node -e '\n" + "console.log(1);\n" * (command_size // 16) + "'"}
    subjects = shell.approval_subjects(arguments)
    assert len(subjects[0]) > 1_024
    scope_key = approval_scope_key(shell, session_id=session_id)
    rule = ApprovalRuleConfig(
        tool_name="shell", tool_source="builtin", pattern=subjects[0],
        scope_key=scope_key, action="allow",
    )
    grant = ApprovalGrantCandidate(
        id="exact", label="This command signature", description=arguments["command"],
        proposed_rules=(rule,), scope_key=scope_key,
    )
    presented = []

    def handler(pending) -> None:
        presented.append(pending.request)
        pending.resolve(
            ApprovalDecision.allow_session(grant)
            if pending.request.request_id == "long-command"
            else ApprovalDecision.allow_once("reviewed separately")
        )

    provider = build_runtime_approval_provider(agent, handler)
    try:
        decision = provider.request_approval(ApprovalRequest(
            request_id="long-command", tool_name="shell", tool_source="builtin",
            tool_args=arguments, subjects=subjects, scope_key=scope_key,
            grant_candidates=(grant,),
        ))
        assert decision.approved is approved
        if not approved:
            assert "session_write_validate" in decision.reason
        expected_rules = [rule] if approved else []
        assert agent.session_approval_rules == expected_rules
        loaded = store.load(session_id)
        assert loaded is not None
        assert loaded.runtime_state.approval_rules == [r.to_dict() for r in expected_rules]

        if approved:
            restored = Agent(llm=_LLM(), tools=[], config=config)
            apply_session_runtime_state(loaded, config, restored)
            restored_provider = build_runtime_approval_provider(restored, handler)
            repeated = restored_provider.request_approval(ApprovalRequest(
                tool_name="shell", tool_source="builtin", subjects=subjects,
                scope_key=scope_key,
            ))
            assert repeated.approved
            assert len(presented) == 1
            other_environment = restored_provider.request_approval(ApprovalRequest(
                tool_name="shell", tool_source="builtin", subjects=subjects,
                scope_key="other-environment",
            ))
            assert other_environment.reason == "reviewed separately"

        # A later command still reaches human approval and saves successfully,
        # including after an oversized grant was rejected by the runtime budget.
        following = provider.request_approval(ApprovalRequest(
            tool_name="shell", tool_source="builtin", scope_key=scope_key,
            subjects=shell.approval_subjects({"command": "echo next"}),
        ))
        assert following.reason == "reviewed separately"
        agent.persist_runtime_snapshot()
        assert store.load(session_id).runtime_state.approval_rules == [
            r.to_dict() for r in expected_rules
        ]
    finally:
        agent.unbind_session_persistence()
