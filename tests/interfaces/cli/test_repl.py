from types import SimpleNamespace

import reuleauxcoder.interfaces.cli.repl as repl_module


def test_new_repl_input_clears_previous_stop_before_command_dispatch(
    monkeypatch,
    tmp_path,
) -> None:
    operations = []
    agent = SimpleNamespace(
        current_session_id=None,
        clear_stop_request=lambda: operations.append("clear"),
    )
    config = SimpleNamespace(
        model="test-model",
        base_url="https://example.invalid",
        history_file=str(tmp_path / "history"),
    )

    monkeypatch.setattr(repl_module, "ensure_user_dirs", lambda: None)
    monkeypatch.setattr(repl_module, "show_banner", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        repl_module,
        "pt_prompt",
        lambda *args, **kwargs: "/compact force summarize",
    )

    def handle_command(*args, **kwargs):
        operations.append("dispatch")
        return {
            "action": "exit",
            "action_id": "system.exit",
            "session_id": None,
            "session_exit_time": None,
        }

    monkeypatch.setattr(repl_module, "handle_command", handle_command)

    repl_module.run_repl(
        agent,
        config,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
    )

    assert operations == ["clear", "dispatch"]
