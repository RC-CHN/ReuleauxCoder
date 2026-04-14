from pathlib import Path

from reuleauxcoder.infrastructure.persistence.session_store import SessionStore


def test_session_store_save_and_load_roundtrip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    messages = [{"role": "user", "content": "hello world"}]

    session_id = store.save(
        messages=messages,
        model="gpt-4o",
        total_prompt_tokens=12,
        total_completion_tokens=34,
        active_mode="coder",
    )

    loaded = store.load(session_id)
    assert loaded is not None
    loaded_messages, model, prompt_tokens, completion_tokens, active_mode = loaded
    assert loaded_messages == messages
    assert model == "gpt-4o"
    assert prompt_tokens == 12
    assert completion_tokens == 34
    assert active_mode == "coder"


def test_session_store_save_with_exit_appends_exit_marker(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "bye"}],
        model="gpt-4o",
        is_exit=True,
    )

    loaded = store.load(session_id)
    assert loaded is not None
    loaded_messages = loaded[0]
    assert loaded_messages[-1]["role"] == "system"
    assert loaded_messages[-1]["content"].startswith("[SESSION_EXIT]")
    assert store.get_exit_time(loaded_messages) is not None


def test_session_store_list_ignores_invalid_json_and_returns_latest_first(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    first_id = store.save(messages=[{"role": "user", "content": "first"}], model="m1")
    second_id = store.save(messages=[{"role": "user", "content": "second"}], model="m2")
    (tmp_path / "broken.json").write_text("{not-json}", encoding="utf-8")

    sessions = store.list(limit=10)
    ids = [item.id for item in sessions]

    assert first_id in ids
    assert second_id in ids
    assert len(sessions) == 2
    assert store.get_latest() is not None


def test_session_store_get_exit_time_returns_none_without_marker() -> None:
    messages = [{"role": "user", "content": "hello"}]
    assert SessionStore.get_exit_time(messages) is None


def test_session_store_load_missing_returns_none(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    assert store.load("missing") is None
