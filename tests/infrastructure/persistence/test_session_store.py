import threading
from pathlib import Path

from reuleauxcoder.domain.context.manager import MESSAGE_TOKEN_KEY
from reuleauxcoder.domain.session.models import SessionRuntimeState
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
        runtime_state=SessionRuntimeState(
            model="gpt-4o",
            active_mode="coder",
            llm_debug_trace=True,
        ),
        fingerprint="local",
    )

    loaded = store.load(session_id)
    assert loaded is not None
    assert loaded.messages[0]["role"] == messages[0]["role"]
    assert loaded.messages[0]["content"] == messages[0]["content"]
    assert isinstance(loaded.messages[0].get(MESSAGE_TOKEN_KEY), int)
    assert loaded.model == "gpt-4o"
    assert loaded.total_prompt_tokens == 12
    assert loaded.total_completion_tokens == 34
    assert loaded.active_mode == "coder"
    assert loaded.runtime_state.model == "gpt-4o"
    assert loaded.runtime_state.active_mode == "coder"
    assert loaded.runtime_state.llm_debug_trace is True
    assert loaded.fingerprint == "local"


def test_session_store_save_with_exit_appends_exit_marker(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "bye"}],
        model="gpt-4o",
        is_exit=True,
    )

    loaded = store.load(session_id)
    assert loaded is not None
    assert loaded.messages[-1]["role"] == "system"
    assert loaded.messages[-1]["content"].startswith("[SESSION_EXIT]")
    assert isinstance(loaded.messages[-1].get(MESSAGE_TOKEN_KEY), int)
    assert store.get_exit_time(loaded.messages) is not None


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
    assert loaded.messages[-1]["role"] == "system"
    assert "[LLM_ERROR_DIAGNOSTIC]" in loaded.messages[-1]["content"]
    assert isinstance(loaded.messages[-1].get(MESSAGE_TOKEN_KEY), int)


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
    assert isinstance(loaded.messages[0].get(MESSAGE_TOKEN_KEY), int)

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(persisted["messages"][0].get(MESSAGE_TOKEN_KEY), int)


def test_session_store_list_filters_by_fingerprint(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    local_id = store.save(messages=[{"role": "user", "content": "first"}], model="m1", fingerprint="local")
    remote_id = store.save(messages=[{"role": "user", "content": "second"}], model="m2", fingerprint="remote:abc")
    (tmp_path / "broken.json").write_text("{not-json}", encoding="utf-8")

    local_sessions = store.list(limit=10, fingerprint="local")
    remote_sessions = store.list(limit=10, fingerprint="remote:abc")
    all_sessions = store.list(limit=10, fingerprint=None)

    assert [item.id for item in local_sessions] == [local_id]
    assert [item.id for item in remote_sessions] == [remote_id]
    assert {item.id for item in all_sessions} == {local_id, remote_id}
    assert store.get_latest(fingerprint="local") is not None
    assert store.get_latest(fingerprint="remote:abc") is not None


def test_session_store_get_latest_prefers_recently_updated_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    first_id = store.save(messages=[{"role": "user", "content": "first"}], model="m1", fingerprint="local")
    second_id = store.save(messages=[{"role": "user", "content": "second"}], model="m2", fingerprint="local")

    # Update the older session after the newer one was created.
    store.save(messages=[{"role": "user", "content": "first-updated"}], model="m1", session_id=first_id, fingerprint="local")

    latest = store.get_latest(fingerprint="local")
    assert latest is not None
    assert latest.id == first_id
    assert latest.id != second_id


def test_session_store_get_exit_time_returns_none_without_marker() -> None:
    messages = [{"role": "user", "content": "hello"}]
    assert SessionStore.get_exit_time(messages) is None


def test_session_store_load_missing_returns_none(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    assert store.load("missing") is None


def test_session_store_concurrent_save_keeps_sessions_readable(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(messages=[{"role": "user", "content": "seed"}], model="m1", fingerprint="local")

    def update_existing(index: int) -> None:
        store.save(
            messages=[{"role": "user", "content": f"existing-{index}"}],
            model="m1",
            session_id=session_id,
            fingerprint="local",
        )

    def create_new(index: int) -> None:
        store.save(
            messages=[{"role": "user", "content": f"new-{index}"}],
            model="m2",
            fingerprint="remote:abc",
        )

    threads = [threading.Thread(target=update_existing, args=(i,)) for i in range(4)] + [
        threading.Thread(target=create_new, args=(i,)) for i in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    loaded = store.load(session_id)
    assert loaded is not None
    assert loaded.messages[0]["content"].startswith("existing-")

    local_latest = store.get_latest(fingerprint="local")
    remote_latest = store.get_latest(fingerprint="remote:abc")
    assert local_latest is not None
    assert local_latest.id == session_id
    assert remote_latest is not None
    assert remote_latest.fingerprint == "remote:abc"

    listed = store.list(limit=20, fingerprint=None)
    assert len(listed) >= 5
