from pathlib import Path

from reuleauxcoder.domain.context.manager import MESSAGE_TOKEN_KEY
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
    assert loaded_messages[0]["role"] == messages[0]["role"]
    assert loaded_messages[0]["content"] == messages[0]["content"]
    assert isinstance(loaded_messages[0].get(MESSAGE_TOKEN_KEY), int)
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
    assert isinstance(loaded_messages[-1].get(MESSAGE_TOKEN_KEY), int)
    assert store.get_exit_time(loaded_messages) is not None


def test_session_store_append_system_message_updates_existing_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(messages=[{"role": "user", "content": "hello"}], model="gpt-4o")

    store.append_system_message(
        session_id,
        "gpt-4o",
        "[LLM_ERROR_DIAGNOSTIC] path=/tmp/demo.json error=BadRequestError: boom",
        active_mode="coder",
    )

    loaded = store.load(session_id)
    assert loaded is not None
    loaded_messages = loaded[0]
    assert loaded_messages[-1]["role"] == "system"
    assert "[LLM_ERROR_DIAGNOSTIC]" in loaded_messages[-1]["content"]
    assert isinstance(loaded_messages[-1].get(MESSAGE_TOKEN_KEY), int)


def test_session_store_load_backfills_missing_message_token_counts(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(messages=[{"role": "user", "content": "hello"}], model="gpt-4o")
    path = tmp_path / f"{session_id}.json"

    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    data["messages"][0].pop(MESSAGE_TOKEN_KEY, None)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    loaded = store.load(session_id)
    assert loaded is not None
    loaded_messages = loaded[0]
    assert isinstance(loaded_messages[0].get(MESSAGE_TOKEN_KEY), int)

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(persisted["messages"][0].get(MESSAGE_TOKEN_KEY), int)


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
