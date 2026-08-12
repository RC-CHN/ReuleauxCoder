import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from reuleauxcoder.domain.context.manager import MESSAGE_TOKEN_KEY
from reuleauxcoder.domain.context.checkpoint import CompactionCheckpoint
from reuleauxcoder.domain.context.replay import (
    ReplayEnvelope,
    RequestEnvelope,
    content_hash,
)
from reuleauxcoder.domain.history import HistoryLedger
from reuleauxcoder.domain.session.models import (
    Session,
    SessionRestoreIssue,
    SessionRuntimeState,
)
from reuleauxcoder.infrastructure.persistence.session_store import (
    SessionRestoreError,
    SessionStore,
)


def _request_envelopes(
    count: int,
    *,
    session_id: str = "session-request-retention",
) -> list[RequestEnvelope]:
    replay = ReplayEnvelope.create(
        session_id=session_id,
        cache_epoch=0,
        history_version=0,
        model_profile="model",
        provider_family="openai-compatible",
        request_mode="chat-completions",
        instructions=[],
        tools=[],
        items=[],
    )
    return [
        RequestEnvelope.create(
            replay=replay,
            overlay={"round": index},
            overlay_revision=index + 1,
            overlay_tokens=1,
        )
        for index in range(count)
    ]


def _rehash_replay(payload: dict) -> None:
    stable = {
        key: payload[key]
        for key in (
            "model_profile",
            "provider_family",
            "request_mode",
            "request_settings",
            "instructions",
            "tools",
            "items",
        )
    }
    core = {
        key: payload[key]
        for key in (
            "schema_version",
            "session_id",
            "cache_epoch",
            "history_version",
            "model_profile",
            "provider_family",
            "request_mode",
            "request_settings",
            "instructions",
            "tools",
            "items",
            "item_provenance",
        )
    }
    payload["stable_prefix_hash"] = content_hash(stable)
    payload["canonical_payload_hash"] = content_hash(core)


def test_session_restore_error_rejects_content_bearing_facts() -> None:
    sentinel = "session-secret-must-not-leak"

    error = SessionRestoreError(
        phase=f"restore:{sentinel}",
        error_type=sentinel,
        ref=f"../{sentinel}",
    )

    assert error.phase == "restore"
    assert error.error_type == "Exception"
    assert error.ref == "session_artifact"
    assert sentinel not in str(error)


def test_session_restore_issue_constructor_enforces_safe_facts() -> None:
    with pytest.raises(ValueError):
        SessionRestoreIssue(
            phase="unsafe:phase",
            error_type="ValueError",
            ref="manifest",
        )
    with pytest.raises(ValueError):
        SessionRestoreIssue(
            phase="restore",
            error_type="ValueError",
            ref="manifest",
            count=-1,
        )


def test_invalid_restore_issue_carrier_is_folded_without_crashing_or_leaking(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "saved"}],
        model="model",
        restore_issues=[object()],  # type: ignore[list-item]
    )

    loaded = store.load(session_id)

    assert loaded is not None
    assert any(
        issue.phase == "restore_issues_validate"
        and issue.error_type == "SessionRestoreIssuesValidationError"
        and issue.ref == "restore_issues"
        for issue in loaded.restore_issues
    )


def test_empty_reserved_session_directory_is_not_a_persisted_session(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    events_path = store.get_session_events_path("session_reserved")

    assert events_path.parent.is_dir()
    inventory = store.get_latest_result(fingerprint="local")
    assert inventory.session is None
    assert inventory.issues == ()


def test_session_preview_uses_latest_real_user_request(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[
            {"role": "user", "content": "initial topic"},
            {"role": "assistant", "content": "old answer"},
            {
                "role": "user",
                "content": "[SESSION_RESUME] User returned.\n\nlatest request",
            },
            {"role": "assistant", "content": "latest answer"},
            {"role": "user", "content": "[SESSION_EXIT] User left."},
        ],
        model="m1",
    )

    metadata = next(item for item in store.list() if item.id == session_id)

    assert metadata.preview == "latest request"


def test_session_preview_writer_normalizes_control_characters(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[
            {
                "role": "user",
                "content": "safe\x00preview\nwithout\u202econtrols",
            }
        ],
        model="model",
    )

    inventory = store.get_latest_result(fingerprint="local")
    metadata = inventory.session

    assert metadata is not None and metadata.id == session_id
    assert metadata.preview == "safe preview without controls"
    assert inventory.issues == ()


def test_session_listing_uses_directory_metadata_without_full_restore(
    tmp_path: Path, monkeypatch
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "metadata only"}], model="m1"
    )

    monkeypatch.setattr(
        store,
        "_load_session_directory",
        lambda _directory: (_ for _ in ()).throw(
            AssertionError("listing must not restore full sessions")
        ),
    )

    listed = store.list()

    assert [item.id for item in listed] == [session_id]
    assert listed[0].preview == "metadata only"


def test_session_projection_serves_repeated_inventory_without_manifest_scan(
    tmp_path: Path, monkeypatch
) -> None:
    writer = SessionStore(tmp_path)
    session_id = writer.save(
        messages=[{"role": "user", "content": "indexed"}],
        model="model",
    )
    assert [item.id for item in writer.list()] == [session_id]
    assert writer.session_projection_path.exists()

    reader = SessionStore(tmp_path)
    monkeypatch.setattr(
        reader,
        "_scan_inventory",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("healthy projection must avoid manifest scan")
        ),
    )

    inventory = reader.list_result(fingerprint="local")

    assert [item.id for item in inventory.sessions] == [session_id]
    assert inventory.issues == ()


def test_ready_session_projection_is_updated_after_authoritative_save(
    tmp_path: Path, monkeypatch
) -> None:
    store = SessionStore(tmp_path)
    first_id = store.save(
        messages=[{"role": "user", "content": "first"}],
        model="m1",
    )
    store.list()
    second_id = store.save(
        messages=[{"role": "user", "content": "second"}],
        model="m2",
    )

    reader = SessionStore(tmp_path)
    monkeypatch.setattr(
        reader,
        "_scan_inventory",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("save should update the ready projection")
        ),
    )

    assert {item.id for item in reader.list(fingerprint=None)} == {
        first_id,
        second_id,
    }


def test_corrupt_session_projection_rebuilds_without_touching_authority(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "authoritative"}],
        model="model",
    )
    store.list()
    manifest_path = tmp_path / session_id / "manifest.json"
    replay_path = tmp_path / session_id / "replay.json"
    manifest_before = manifest_path.read_bytes()
    replay_before = replay_path.read_bytes()
    store.session_projection_path.write_bytes(b"not-a-sqlite-database")

    repaired = SessionStore(tmp_path)
    inventory = repaired.list_result(fingerprint="local")

    assert [item.id for item in inventory.sessions] == [session_id]
    assert any(
        issue.phase == "session_projection"
        and issue.ref == "session_index"
        for issue in inventory.issues
    )
    assert manifest_path.read_bytes() == manifest_before
    assert replay_path.read_bytes() == replay_before
    assert repaired.session_projection_path.read_bytes().startswith(b"SQLite format 3")


def test_projection_update_failure_does_not_fail_save_and_is_reported_later(
    tmp_path: Path, monkeypatch
) -> None:
    store = SessionStore(tmp_path)
    store.save(
        messages=[{"role": "user", "content": "seed"}],
        model="model",
    )
    store.list()

    monkeypatch.setattr(
        store._projection,
        "upsert",
        lambda _row: (_ for _ in ()).throw(RuntimeError("index-content-secret")),
    )
    saved_id = store.save(
        messages=[{"role": "user", "content": "still durable"}],
        model="model",
    )

    assert (tmp_path / saved_id / "manifest.json").exists()
    assert (tmp_path / saved_id / "replay.json").exists()
    inventory = SessionStore(tmp_path).list_result(fingerprint="local")
    assert saved_id in {item.id for item in inventory.sessions}
    issue = next(
        issue
        for issue in inventory.issues
        if issue.phase == "session_projection"
    )
    assert issue.error_type == "RuntimeError"
    assert issue.ref == "session_index"
    assert "index-content-secret" not in issue.render()


def test_session_projection_summary_uses_derived_counters(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.save(
        messages=[{"role": "user", "content": "one"}],
        model="m1",
        total_prompt_tokens=10,
        total_completion_tokens=4,
    )
    store.save(
        messages=[{"role": "user", "content": "two"}],
        model="m2",
        total_prompt_tokens=20,
        total_completion_tokens=6,
    )

    summary = store.projection_summary()

    assert summary is not None
    assert summary.session_count == 2
    assert summary.prompt_tokens == 30
    assert summary.completion_tokens == 10
    assert summary.event_count == 4
    assert summary.request_count == 0
    assert summary.checkpoint_count == 0


@pytest.mark.parametrize(
    "unsafe_preview",
    [42, "x" * 121, "unsafe\npreview", "unsafe\u202epreview"],
)
def test_unsafe_preview_is_rebuilt_without_blocking_inventory_or_load(
    tmp_path: Path,
    unsafe_preview,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "authoritative preview"}],
        model="model",
    )
    manifest_path = tmp_path / session_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["preview"] = unsafe_preview
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    inventory = store.list_result(fingerprint="local")
    listed = inventory.sessions

    assert [item.id for item in listed] == [session_id]
    assert listed[0].preview == ""
    assert any(
        issue.phase == "preview_validate"
        and issue.error_type == "SessionPreviewValidationError"
        and issue.ref == "preview"
        for issue in inventory.issues
    )
    latest = store.get_latest(fingerprint="local")
    assert latest is not None and latest.id == session_id

    loaded = store.load(session_id)
    assert loaded is not None
    assert loaded.messages[0]["content"] == "authoritative preview"
    assert loaded.get_preview() == "authoritative preview"
    assert any(issue.phase == "preview_validate" for issue in loaded.restore_issues)

    store.save(
        messages=loaded.messages,
        model=loaded.model,
        session_id=session_id,
        history_events=loaded.history_events,
        replay_envelope=loaded.replay_envelope,
        history_completeness=loaded.history_completeness,
        restore_issues=loaded.restore_issues,
    )
    rebuilt = store.list(fingerprint="local")[0]
    assert rebuilt.preview == "authoritative preview"


def test_corrupt_manifest_with_unknown_scope_blocks_auto_resume_fail_closed(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    local_id = store.save(
        messages=[{"role": "user", "content": "local"}],
        model="model",
        fingerprint="local",
    )
    foreign_id = store.save(
        messages=[{"role": "user", "content": "foreign"}],
        model="model",
        fingerprint="remote:peer",
    )
    (tmp_path / foreign_id / "manifest.json").write_text(
        '{"broken":',
        encoding="utf-8",
    )

    inventory = store.list_result(fingerprint="local")
    assert [item.id for item in inventory.sessions] == [local_id]
    assert any(issue.phase == "manifest_decode" for issue in inventory.issues)
    with pytest.raises(SessionRestoreError) as raised:
        store.get_latest(fingerprint="local")
    assert raised.value.phase == "manifest_decode"


def test_corrupt_manifest_unknown_scope_blocks_every_fingerprint(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    foreign_id = store.save(
        messages=[{"role": "user", "content": "foreign"}],
        model="model",
        fingerprint="remote:peer",
    )
    (tmp_path / foreign_id / "manifest.json").write_text(
        '{"broken":',
        encoding="utf-8",
    )

    for fingerprint in ("local", "remote:peer"):
        with pytest.raises(SessionRestoreError) as raised:
            store.get_latest(fingerprint=fingerprint)
        assert raised.value.phase == "manifest_decode"


def test_newest_matching_corrupt_session_blocks_older_healthy_resume(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    healthy_id = store.save(
        messages=[{"role": "user", "content": "healthy"}],
        model="model",
        fingerprint="local",
    )
    corrupt_id = store.save(
        messages=[{"role": "user", "content": "newer"}],
        model="model",
        fingerprint="local",
    )
    (tmp_path / corrupt_id / "manifest.json").write_text('{"broken":', encoding="utf-8")

    listed = store.list_result(fingerprint="local")
    assert [item.id for item in listed.sessions] == [healthy_id]
    assert any(issue.phase == "manifest_decode" for issue in listed.issues)
    with pytest.raises(SessionRestoreError) as raised:
        store.get_latest_result(fingerprint="local")
    assert raised.value.phase == "manifest_decode"


def test_older_matching_corruption_degrades_but_does_not_hide_newer_health(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    corrupt_id = store.save(
        messages=[{"role": "user", "content": "older"}],
        model="model",
        fingerprint="local",
    )
    (tmp_path / corrupt_id / "manifest.json").write_text('{"broken":', encoding="utf-8")
    healthy_id = store.save(
        messages=[{"role": "user", "content": "newer healthy"}],
        model="model",
        fingerprint="local",
    )

    latest = store.get_latest_result(fingerprint="local")

    assert latest.session is not None and latest.session.id == healthy_id
    assert any(issue.phase == "manifest_decode" for issue in latest.issues)


def test_newer_unknown_fingerprint_corruption_fails_closed(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.save(
        messages=[{"role": "user", "content": "healthy"}],
        model="model",
        fingerprint="local",
    )
    unknown = tmp_path / "unknown-session"
    unknown.mkdir()
    (unknown / "manifest.json").write_text('{"broken":', encoding="utf-8")

    with pytest.raises(SessionRestoreError) as raised:
        store.get_latest_result(fingerprint="local")

    assert raised.value.phase == "manifest_decode"


def test_corrupt_legacy_inventory_is_reported_and_not_treated_as_absent(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    (tmp_path / "legacy-broken.json").write_text('{"broken":', encoding="utf-8")

    inventory = store.list_result(fingerprint="local")
    assert inventory.sessions == ()
    assert any(issue.ref == "legacy_session" for issue in inventory.issues)
    with pytest.raises(SessionRestoreError) as raised:
        store.get_latest(fingerprint="local")
    assert raised.value.ref == "legacy_session"


def test_inventory_entry_disappearance_is_not_reported_as_clean_absence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "saved"}],
        model="model",
    )
    session_dir = tmp_path / session_id
    original_lstat = Path.lstat

    def vanish_during_scan(path: Path, *args, **kwargs):
        if path == session_dir:
            raise FileNotFoundError
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", vanish_during_scan)

    inventory = store.list_result(fingerprint="local")

    assert inventory.sessions == ()
    assert any(
        issue.phase == "session_discovery"
        and issue.error_type == "FileNotFoundError"
        and issue.ref == "session_directory"
        for issue in inventory.issues
    )
    with pytest.raises(SessionRestoreError):
        store.get_latest_result(fingerprint="local")


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


def test_bounded_custom_session_id_roundtrips_through_safe_path_mapping(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = "remote:peer:session"

    assert (
        store.save(
            messages=[{"role": "user", "content": "saved"}],
            model="model",
            session_id=session_id,
        )
        == session_id
    )
    loaded = store.load(session_id)

    assert loaded is not None and loaded.id == session_id
    assert loaded.messages[0]["content"] == "saved"


def test_session_id_mapping_is_injective_for_remote_and_underscore_ids(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    remote_id = "remote:peer:session"
    underscore_id = "remote_peer_session"

    store.save(
        messages=[{"role": "user", "content": "remote"}],
        model="model",
        session_id=remote_id,
    )
    store.save(
        messages=[{"role": "user", "content": "underscore"}],
        model="model",
        session_id=underscore_id,
    )

    remote_directory = store.get_session_events_path(remote_id).parent
    underscore_directory = store.get_session_events_path(underscore_id).parent
    assert remote_directory != tmp_path / remote_id
    assert ":" not in remote_directory.name
    assert underscore_directory == tmp_path / underscore_id
    assert remote_directory != underscore_directory
    assert {item.id for item in store.list(fingerprint="local")} == {
        remote_id,
        underscore_id,
    }
    assert store.load(remote_id).messages[0]["content"] == "remote"  # type: ignore[union-attr]
    assert store.load(underscore_id).messages[0]["content"] == "underscore"  # type: ignore[union-attr]


def test_windows_reserved_session_id_uses_portable_directory(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)

    store.save(
        messages=[{"role": "user", "content": "portable"}],
        model="model",
        session_id="con",
    )

    directory = store.get_session_events_path("con").parent
    assert directory != tmp_path / "con"
    assert store.load("con").messages[0]["content"] == "portable"  # type: ignore[union-attr]


def test_windows_unsafe_legacy_session_path_is_not_probed(monkeypatch) -> None:
    from reuleauxcoder.infrastructure.persistence import session_paths

    monkeypatch.setattr(session_paths, "_ON_WINDOWS", True)

    candidates = session_paths.session_path_candidates("remote:peer:session")
    assert len(candidates) == 1
    assert ":" not in candidates[0]


@pytest.mark.parametrize(
    "session_id",
    ["../escape", "nested/session", r"nested\session", "a..b", "has space"],
)
def test_unsafe_session_ids_are_rejected_instead_of_sanitized(
    tmp_path: Path, session_id: str
) -> None:
    store = SessionStore(tmp_path)

    with pytest.raises(SessionRestoreError) as raised:
        store.save(
            messages=[{"role": "user", "content": "must not write"}],
            model="model",
            session_id=session_id,
        )

    assert raised.value.phase == "session_identity"
    assert raised.value.error_type == "SessionIdentityValidationError"
    assert list(tmp_path.iterdir()) == []


def test_sessions_root_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "actual-sessions"
    target.mkdir()
    sessions_link = tmp_path / "sessions"
    sessions_link.symlink_to(target, target_is_directory=True)
    store = SessionStore(sessions_link)

    with pytest.raises(SessionRestoreError) as raised:
        store.list_result(fingerprint="local")

    assert raised.value.phase == "session_discovery"
    assert raised.value.error_type == "SymbolicLinkError"
    assert raised.value.ref == "session_directory"


@pytest.mark.parametrize("legacy", [False, True])
def test_session_inventory_rejects_symlink_entries(
    tmp_path: Path, legacy: bool
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    outside = tmp_path / ("outside.json" if legacy else "outside-session")
    if legacy:
        outside.write_text("{}", encoding="utf-8")
        entry = sessions_dir / "session_link.json"
    else:
        outside.mkdir()
        entry = sessions_dir / "session_link"
    entry.symlink_to(outside, target_is_directory=not legacy)
    store = SessionStore(sessions_dir)

    inventory = store.list_result(fingerprint="local")

    assert inventory.sessions == ()
    assert any(
        issue.error_type == "SymbolicLinkError"
        and issue.ref == ("legacy_session" if legacy else "session_directory")
        for issue in inventory.issues
    )
    with pytest.raises(SessionRestoreError) as raised:
        store.get_latest(fingerprint="local")
    assert raised.value.error_type == "SymbolicLinkError"


def test_replay_symlink_is_terminal_but_history_symlink_is_degraded(
    tmp_path: Path,
) -> None:
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir)
    replay_session = store.save(
        messages=[{"role": "user", "content": "replay"}], model="model"
    )
    replay_path = sessions_dir / replay_session / "replay.json"
    outside_replay = tmp_path / "outside-replay.json"
    replay_path.replace(outside_replay)
    replay_path.symlink_to(outside_replay)

    with pytest.raises(SessionRestoreError) as replay_error:
        store.load(replay_session)
    assert replay_error.value.error_type == "SymbolicLinkError"
    assert replay_error.value.ref == "replay"

    history_session = store.save(
        messages=[{"role": "user", "content": "snapshot"}], model="model"
    )
    events_path = sessions_dir / history_session / "events.jsonl"
    outside_events = tmp_path / "outside-events.jsonl"
    events_path.replace(outside_events)
    events_path.symlink_to(outside_events)

    loaded = store.load(history_session)

    assert loaded is not None
    assert loaded.messages[0]["content"] == "snapshot"
    assert loaded.history_completeness == "degraded"
    assert any(
        issue.error_type == "SymbolicLinkError" and issue.ref == "history_ledger"
        for issue in loaded.restore_issues
    )


def test_empty_canonical_history_is_not_reported_as_missing(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(messages=[], model="model")

    loaded = store.load(session_id)

    assert loaded is not None
    assert loaded.messages == []
    assert loaded.restore_issues == ()


def test_session_store_retains_only_latest_request_records(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    requests = _request_envelopes(205)

    session_id = store.save(
        messages=[{"role": "user", "content": "bounded requests"}],
        model="model",
        session_id="session-request-retention",
        request_envelopes=requests,
    )

    requests_dir = tmp_path / session_id / "requests"
    loaded = store.load(session_id)

    assert loaded is not None
    assert len(list(requests_dir.glob("*.json"))) == 200
    assert [item.request_id for item in loaded.request_envelopes] == [
        item.request_id for item in requests[-200:]
    ]


def test_legacy_session_request_directory_is_bounded_on_load(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "legacy requests"}],
        model="model",
        session_id="legacy-request-retention",
    )
    session_dir = tmp_path / session_id
    manifest_path = session_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("request_ids")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    requests_dir = session_dir / "requests"
    for request in _request_envelopes(205, session_id=session_id):
        (requests_dir / f"{request.request_id}.json").write_text(
            json.dumps(request.to_dict()),
            encoding="utf-8",
        )

    loaded = store.load(session_id)

    assert loaded is not None
    assert len(loaded.request_envelopes) == 200


@pytest.mark.parametrize("persisted_last_event_seq", [None, 1])
def test_load_recovers_messages_committed_after_replay_snapshot(
    tmp_path: Path,
    persisted_last_event_seq: int | None,
) -> None:
    progress = []
    store = SessionStore(tmp_path, progress.append)
    ledger = HistoryLedger()
    first = {"role": "user", "content": "persisted snapshot"}
    second = {"role": "assistant", "content": "durable ledger tail"}
    first_event = ledger.append_message(first, source="user_input")
    replay = ReplayEnvelope.create(
        session_id="session-tail",
        cache_epoch=0,
        history_version=0,
        model_profile="model",
        provider_family="openai-compatible",
        request_mode="chat-completions",
        instructions=[],
        tools=[],
        items=[first],
        item_provenance=[
            {
                "source_event_ids": [first_event.event_id],
                "artifact_refs": [],
                "checkpoint_id": None,
            }
        ],
    )
    ledger.append_message(second, source="assistant_response")
    store.save(
        messages=[first],
        model="model",
        session_id="session-tail",
        history_events=list(ledger.events),
        replay_envelope=replay,
    )
    manifest_path = tmp_path / "session-tail" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if persisted_last_event_seq is None:
        manifest.pop("last_event_seq")
    else:
        manifest["last_event_seq"] = persisted_last_event_seq
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    loaded = store.load("session-tail")

    assert loaded is not None
    assert [message["content"] for message in loaded.messages] == [
        "persisted snapshot",
        "durable ledger tail",
    ]
    assert any(
        message.startswith(
            "Recovering 1 message update(s) from the durable history tail"
        )
        for message in progress
    )
    assert any(
        message.startswith("Calculating token counts for 1 recovered message(s)")
        for message in progress
    )
    assert any(
        message.startswith("Token counts ready (1 message(s),") for message in progress
    )


@pytest.mark.parametrize(
    ("persisted_last_event_seq", "expected_error_type"),
    [
        (True, "HistoryLastEventSequenceValidationError"),
        (999, "HistoryLastEventSequenceMismatch"),
    ],
)
def test_invalid_or_ahead_last_event_sequence_blocks_uncertain_tail(
    tmp_path: Path,
    persisted_last_event_seq,
    expected_error_type: str,
) -> None:
    store = SessionStore(tmp_path)
    ledger = HistoryLedger()
    first = {"role": "user", "content": "authoritative replay"}
    tail = {"role": "assistant", "content": "uncertain ledger tail"}
    first_event = ledger.append_message(first, source="user_input")
    replay = ReplayEnvelope.create(
        session_id="session-last-seq",
        cache_epoch=0,
        history_version=0,
        model_profile="model",
        provider_family="openai-compatible",
        request_mode="chat-completions",
        instructions=[],
        tools=[],
        items=[first],
        item_provenance=[
            {
                "source_event_ids": [first_event.event_id],
                "artifact_refs": [],
                "checkpoint_id": None,
            }
        ],
    )
    ledger.append_message(tail, source="assistant_response")
    session_id = store.save(
        messages=[first],
        model="model",
        session_id="session-last-seq",
        history_events=list(ledger.events),
        replay_envelope=replay,
    )
    manifest_path = tmp_path / session_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["last_event_seq"] = persisted_last_event_seq
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    loaded = store.load(session_id)

    assert loaded is not None
    assert [message["content"] for message in loaded.messages] == [
        "authoritative replay"
    ]
    assert loaded.history_completeness == "degraded"
    assert any(
        issue.error_type == expected_error_type and issue.ref == "history_ledger"
        for issue in loaded.restore_issues
    )
    if persisted_last_event_seq == 999:
        assert loaded.history_next_seq_floor == 999


def test_history_issue_keeps_authoritative_replay_and_does_not_apply_tail(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    ledger = HistoryLedger()
    first = {"role": "user", "content": "authoritative replay"}
    tail = {"role": "assistant", "content": "uncertain ledger tail"}
    first_event = ledger.append_message(first, source="user_input")
    replay = ReplayEnvelope.create(
        session_id="session-degraded-tail",
        cache_epoch=0,
        history_version=0,
        model_profile="model",
        provider_family="openai-compatible",
        request_mode="chat-completions",
        instructions=[],
        tools=[],
        items=[first],
        item_provenance=[
            {
                "source_event_ids": [first_event.event_id],
                "artifact_refs": [],
                "checkpoint_id": None,
            }
        ],
    )
    ledger.append_message(tail, source="assistant_response")
    session_id = store.save(
        messages=[first],
        model="model",
        session_id="session-degraded-tail",
        history_events=list(ledger.events),
        replay_envelope=replay,
    )
    with (tmp_path / session_id / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("{}\n")

    loaded = store.load(session_id)

    assert loaded is not None
    assert [message["content"] for message in loaded.messages] == [
        "authoritative replay"
    ]
    assert any(issue.phase == "history_decode" for issue in loaded.restore_issues)
    assert loaded.history_completeness == "degraded"


def test_new_session_layout_separates_full_ledger_from_runtime_view(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    ledger = HistoryLedger()
    raw = {"role": "tool", "tool_call_id": "call", "content": "raw-line\n" * 200}
    ledger.append_message(raw, source="tool_result")
    view = [
        {
            "role": "system",
            "content": "[Context checkpoint summary]\nTool output archived.",
        }
    ]
    replay = ReplayEnvelope.create(
        session_id=None,
        cache_epoch=1,
        history_version=1,
        model_profile="model",
        provider_family="openai-compatible",
        request_mode="chat-completions",
        instructions=[{"role": "system", "content": "stable"}],
        tools=[],
        items=view,
    )

    session_id = store.save(
        messages=view,
        model="model",
        history_events=list(ledger.events),
        replay_envelope=replay,
    )

    directory = tmp_path / session_id
    assert (directory / "manifest.json").exists()
    assert (directory / "events.jsonl").exists()
    assert (directory / "replay.json").exists()
    assert "raw-line" in (directory / "events.jsonl").read_text(encoding="utf-8")
    loaded = store.load(session_id)
    assert loaded is not None
    assert loaded.messages[0]["content"].startswith("[Context checkpoint summary]")
    assert loaded.history_completeness == "complete"
    assert loaded.replay_envelope is not None
    assert loaded.replay_envelope.validate()


def test_compaction_checkpoints_are_immutable_session_artifacts(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    checkpoint = CompactionCheckpoint.create(
        trigger="quality_wall",
        strategy=["summarize"],
        source_history_version=3,
        replacement_history=[
            {"role": "system", "content": "[Context checkpoint summary]\nstate"}
        ],
        tokens_before=60_000,
        tokens_after=40_000,
        preserved_rounds=3,
        cache_epoch=2,
        actual_prompt_tokens=61_000,
        cached_input_tokens=50_000,
    )
    session_id = store.save(
        messages=list(checkpoint.replacement_history),
        model="model",
        checkpoints=[checkpoint],
    )

    checkpoint_path = tmp_path / session_id / "checkpoints" / f"{checkpoint.id}.json"
    assert checkpoint_path.exists()
    loaded = store.load(session_id)
    assert loaded is not None
    assert loaded.checkpoints == [checkpoint]


def test_events_jsonl_only_appends_new_event_ids(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    ledger = HistoryLedger()
    ledger.append_message({"role": "user", "content": "one"}, source="user")
    session_id = store.save(
        messages=[{"role": "user", "content": "one"}],
        model="model",
        history_events=list(ledger.events),
    )
    events_path = tmp_path / session_id / "events.jsonl"
    first_lines = events_path.read_text(encoding="utf-8").splitlines()

    store.save(
        messages=[{"role": "user", "content": "one"}],
        model="model",
        session_id=session_id,
        history_events=list(ledger.events),
    )
    assert events_path.read_text(encoding="utf-8").splitlines() == first_lines

    ledger.append_message({"role": "assistant", "content": "two"}, source="assistant")
    store.save(
        messages=[
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
        ],
        model="model",
        session_id=session_id,
        history_events=list(ledger.events),
    )
    assert len(events_path.read_text(encoding="utf-8").splitlines()) == 4


def test_load_projects_legacy_request_replay_without_rewriting_ledger(
    tmp_path: Path,
) -> None:
    progress = []
    store = SessionStore(tmp_path, progress=progress.append)
    ledger = HistoryLedger()
    request = ledger.append(
        "request_committed",
        {"replay": {"items": ["x" * 300_000]}},
    )
    usage = ledger.append(
        "usage_observed",
        {
            "actual_prompt_tokens": 10,
            "cached_input_tokens": 5,
            "local_request_estimate": 8,
            "local_history_estimate": 4,
            "request_boundary": "turn:1",
            "model_profile": "model",
        },
    )
    session_id = store.save(
        messages=[{"role": "user", "content": "hello"}],
        model="model",
        history_events=list(ledger.events),
    )
    events_path = tmp_path / session_id / "events.jsonl"
    events_path.write_text(
        "\n".join(
            json.dumps(event.to_dict(), ensure_ascii=False)
            for event in (request, usage)
        )
        + "\n",
        encoding="utf-8",
    )
    replay_path = tmp_path / session_id / "replay.json"
    replay_before = ReplayEnvelope.from_dict(
        json.loads(replay_path.read_text(encoding="utf-8"))
    )
    size_before = events_path.stat().st_size

    loaded = store.load(session_id)

    assert loaded is not None
    restored_request = next(
        event for event in loaded.history_events if event.event_id == request.event_id
    )
    assert restored_request.seq == request.seq
    assert restored_request.payload["replay"]["item_count"] == 1
    assert "items" not in restored_request.payload["replay"]
    assert any(event.event_id == usage.event_id for event in loaded.history_events)
    assert events_path.stat().st_size == size_before
    assert loaded.messages[0]["content"] == "hello"
    assert loaded.replay_envelope is not None
    assert loaded.replay_envelope.validate()
    assert loaded.replay_envelope.stable_prefix_hash == replay_before.stable_prefix_hash
    assert loaded.replay_envelope.cache_epoch == replay_before.cache_epoch
    assert any(message.startswith("Reading history ledger (") for message in progress)
    assert not any(
        message.startswith("History ledger compacted:") for message in progress
    )


def test_incremental_exit_appends_only_new_exit_events(tmp_path: Path) -> None:
    ledger = HistoryLedger()
    message = {"role": "user", "content": "one"}
    ledger.append_message(message, source="user")
    session_id = SessionStore(tmp_path).save(
        messages=[message],
        model="model",
        history_events=list(ledger.events),
    )
    events_path = tmp_path / session_id / "events.jsonl"
    before = events_path.read_text(encoding="utf-8").splitlines()

    SessionStore(tmp_path).save(
        messages=[message],
        model="model",
        session_id=session_id,
        is_exit=True,
        history_events=list(ledger.events),
        incremental=True,
        events_already_persisted=True,
    )

    after = events_path.read_text(encoding="utf-8").splitlines()
    assert after[: len(before)] == before
    assert len(after) == len(before) + 2
    loaded = SessionStore(tmp_path).load(session_id)
    assert loaded is not None
    assert loaded.messages[-1]["content"].startswith("[SESSION_EXIT]")


@pytest.mark.parametrize(
    ("damage", "error_type"),
    [
        (lambda payload: payload.__setitem__("session_id", "foreign"), "ValueError"),
        (lambda payload: payload.__setitem__("schema_version", 99), "ValueError"),
        (
            lambda payload: payload["payload"]["message"].__setitem__(
                "content", "\ud800"
            ),
            "UnicodeEncodeError",
        ),
        (lambda payload: payload.__setitem__("timestamp", float("nan")), "ValueError"),
    ],
)
def test_save_strictly_validates_existing_history_before_dedupe(
    tmp_path: Path,
    damage,
    error_type: str,
) -> None:
    session_id = "session-existing-history-validation"
    message = {"role": "user", "content": "authoritative"}
    ledger = HistoryLedger(session_id=session_id)
    ledger.append_message(message, source="user")
    store = SessionStore(tmp_path)
    store.save(
        messages=[message],
        model="model",
        session_id=session_id,
        history_events=list(ledger.events),
    )
    events_path = tmp_path / session_id / "events.jsonl"
    payload = json.loads(events_path.read_text(encoding="utf-8").splitlines()[0])
    damage(payload)
    events_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(SessionRestoreError) as raised:
        store.save(
            messages=[message],
            model="model",
            session_id=session_id,
            history_events=list(ledger.events),
        )

    assert raised.value.phase == "history_write_validate"
    assert raised.value.error_type == error_type
    assert raised.value.ref == "history_ledger"


def test_save_rejects_valid_but_conflicting_existing_history_identity(
    tmp_path: Path,
) -> None:
    session_id = "session-existing-history-conflict"
    message = {"role": "user", "content": "authoritative"}
    ledger = HistoryLedger(session_id=session_id)
    ledger.append_message(message, source="user")
    store = SessionStore(tmp_path)
    store.save(
        messages=[message],
        model="model",
        session_id=session_id,
        history_events=list(ledger.events),
    )
    events_path = tmp_path / session_id / "events.jsonl"
    payload = json.loads(events_path.read_text(encoding="utf-8").splitlines()[0])
    payload["payload"]["message"]["content"] = "different but valid"
    events_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(SessionRestoreError) as raised:
        store.save(
            messages=[message],
            model="model",
            session_id=session_id,
            history_events=list(ledger.events),
        )

    assert raised.value.error_type == "HistoryEventConflictError"


def test_new_save_does_not_duplicate_canonical_session_as_legacy_file(
    tmp_path: Path,
) -> None:
    ledger = HistoryLedger()
    ledger.append_message({"role": "user", "content": "one"}, source="user")
    session_id = SessionStore(tmp_path).save(
        messages=[{"role": "user", "content": "one"}],
        model="model",
        history_events=list(ledger.events),
    )

    assert (tmp_path / session_id / "manifest.json").exists()
    assert (tmp_path / session_id / "replay.json").exists()
    assert not (tmp_path / f"{session_id}.json").exists()


def test_valid_canonical_load_removes_stale_legacy_duplicate(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "canonical"}], model="model"
    )
    duplicate = tmp_path / f"{session_id}.json"
    duplicate.write_text("{}", encoding="utf-8")

    loaded = store.load(session_id)

    assert loaded is not None
    assert loaded.messages[0]["content"] == "canonical"
    assert not duplicate.exists()


def test_tampered_canonical_replay_fails_without_legacy_fallback(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "safe fallback"}], model="model"
    )
    canonical = store.load(session_id)
    assert canonical is not None
    legacy_path = tmp_path / f"{session_id}.json"
    legacy_path.write_text(
        json.dumps(canonical.to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    replay_path = tmp_path / session_id / "replay.json"
    replay_data = json.loads(replay_path.read_text(encoding="utf-8"))
    replay_data["items"][0]["content"] = "tampered"
    replay_path.write_text(json.dumps(replay_data), encoding="utf-8")

    with pytest.raises(SessionRestoreError) as raised:
        store.load(session_id)

    error = raised.value
    assert error.phase == "replay_validate"
    assert error.error_type == "ReplayValidationError"
    assert error.ref == "replay"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "tampered" not in str(error)
    assert legacy_path.exists()


def test_corrupt_canonical_manifest_is_not_missing_or_legacy_fallback(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "canonical"}], model="model"
    )
    canonical = store.load(session_id)
    assert canonical is not None
    legacy_path = tmp_path / f"{session_id}.json"
    legacy_path.write_text(json.dumps(canonical.to_dict()), encoding="utf-8")
    sentinel = "manifest-secret-must-not-leak"
    (tmp_path / session_id / "manifest.json").write_text(
        '{"broken":"' + sentinel,
        encoding="utf-8",
    )

    with pytest.raises(SessionRestoreError) as raised:
        store.load(session_id)

    error = raised.value
    assert error.phase == "manifest_decode"
    assert error.error_type == "JSONDecodeError"
    assert error.ref == "manifest"
    assert error.__context__ is None
    assert sentinel not in str(error)
    assert legacy_path.exists()
    with pytest.raises(SessionRestoreError) as latest_error:
        store.get_latest(fingerprint="local")
    assert latest_error.value.phase == "manifest_decode"


def test_canonical_replay_read_failure_is_safe_terminal(
    tmp_path: Path, monkeypatch
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "canonical"}], model="model"
    )
    replay_path = tmp_path / session_id / "replay.json"
    sentinel = "replay-read-secret-must-not-leak"
    original_read_text = Path.read_text

    def fail_replay_read(path: Path, *args, **kwargs):
        if path == replay_path:
            raise PermissionError(sentinel)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_replay_read)

    with pytest.raises(SessionRestoreError) as raised:
        store.load(session_id)

    error = raised.value
    assert error.phase == "replay_read"
    assert error.error_type == "PermissionError"
    assert error.ref == "replay"
    assert error.__context__ is None
    assert sentinel not in str(error)


def test_escaped_lone_surrogate_in_manifest_is_safe_terminal(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "safe"}], model="model"
    )
    manifest_path = tmp_path / session_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["model"] = "\ud800"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SessionRestoreError) as raised:
        store.load(session_id)

    assert raised.value.phase == "manifest_decode"
    assert raised.value.error_type == "UnicodeEncodeError"
    assert raised.value.ref == "manifest"


def test_lone_surrogate_history_and_optional_artifact_restore_degraded(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    checkpoint = CompactionCheckpoint.create(
        trigger="quality_wall",
        strategy=["summarize"],
        source_history_version=1,
        replacement_history=[{"role": "user", "content": "safe"}],
        tokens_before=10,
        tokens_after=5,
        preserved_rounds=1,
    )
    session_id = store.save(
        messages=[{"role": "user", "content": "authoritative snapshot"}],
        model="model",
        checkpoints=[checkpoint],
    )
    invalid_history = {
        "schema_version": 2,
        "seq": 500,
        "event_id": "he_surrogate",
        "kind": "audit",
        "timestamp": 1.0,
        "session_generation": 0,
        "payload": {"value": "\ud800"},
        "session_id": session_id,
        "artifact_refs": [],
        "supersedes_event_ids": [],
    }
    with (tmp_path / session_id / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(invalid_history) + "\n")
    checkpoint_path = tmp_path / session_id / "checkpoints" / f"{checkpoint.id}.json"
    checkpoint_payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint_payload["trigger"] = "\ud800"
    checkpoint_path.write_text(json.dumps(checkpoint_payload), encoding="utf-8")

    loaded = store.load(session_id)

    assert loaded is not None
    assert loaded.messages[0]["content"] == "authoritative snapshot"
    assert loaded.checkpoints == []
    assert loaded.history_completeness == "degraded"
    assert {
        (issue.phase, issue.error_type, issue.ref) for issue in loaded.restore_issues
    } >= {
        ("history_decode", "UnicodeEncodeError", "history_ledger"),
        ("checkpoint_decode", "UnicodeEncodeError", "checkpoint"),
    }


def test_lone_surrogate_is_rejected_before_session_or_artifact_write(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)

    with pytest.raises(SessionRestoreError) as message_error:
        store.save(
            messages=[{"role": "user", "content": "\ud800"}],
            model="model",
        )
    assert message_error.value.phase == "session_write_validate"
    assert message_error.value.error_type == "UnicodeEncodeError"

    request = replace(_request_envelopes(1)[0], replay_envelope_hash="\ud800")
    with pytest.raises(SessionRestoreError) as request_error:
        store.save(
            messages=[{"role": "user", "content": "safe"}],
            model="model",
            request_envelopes=[request],
        )
    assert request_error.value.phase == "request_record_write_validate"
    assert request_error.value.error_type == "UnicodeEncodeError"

    checkpoint = replace(
        CompactionCheckpoint.create(
            trigger="quality_wall",
            strategy=["summarize"],
            source_history_version=1,
            replacement_history=[{"role": "user", "content": "safe"}],
            tokens_before=10,
            tokens_after=5,
            preserved_rounds=1,
        ),
        trigger="\ud800",
    )
    with pytest.raises(SessionRestoreError) as checkpoint_error:
        store.save(
            messages=[{"role": "user", "content": "safe"}],
            model="model",
            checkpoints=[checkpoint],
        )
    assert checkpoint_error.value.phase == "checkpoint_write_validate"
    assert checkpoint_error.value.error_type == "UnicodeEncodeError"


def test_missing_canonical_replay_is_corruption_not_missing_session(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "canonical"}], model="model"
    )
    (tmp_path / session_id / "replay.json").unlink()

    with pytest.raises(SessionRestoreError) as raised:
        store.load(session_id)

    assert raised.value.phase == "replay_read"
    assert raised.value.error_type == "FileNotFoundError"
    assert raised.value.ref == "replay"


def test_inventory_does_not_read_foreign_replay_artifacts(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    local_id = store.save(
        messages=[{"role": "user", "content": "local"}],
        model="model",
        fingerprint="local",
    )
    foreign_id = store.save(
        messages=[{"role": "user", "content": "foreign"}],
        model="model",
        fingerprint="remote:peer",
    )
    (tmp_path / foreign_id / "replay.json").unlink()

    inventory = store.get_latest_result(fingerprint="local")
    latest = inventory.session

    assert latest is not None and latest.id == local_id
    assert inventory.issues == ()


def test_replay_from_another_session_is_rejected_as_canonical_mismatch(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    first_id = store.save(
        messages=[{"role": "user", "content": "first"}],
        model="model",
    )
    second_id = store.save(
        messages=[{"role": "user", "content": "second"}],
        model="model",
    )
    second_replay = (tmp_path / second_id / "replay.json").read_text(encoding="utf-8")
    (tmp_path / first_id / "replay.json").write_text(
        second_replay,
        encoding="utf-8",
    )

    with pytest.raises(SessionRestoreError) as raised:
        store.load(first_id)

    assert raised.value.phase == "replay_validate"
    assert raised.value.error_type == "ValueError"
    assert raised.value.ref == "replay"


def test_v3_replay_without_session_id_is_rejected(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "saved"}], model="model"
    )
    replay_path = tmp_path / session_id / "replay.json"
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    replay["session_id"] = None
    replay_path.write_text(json.dumps(replay), encoding="utf-8")

    with pytest.raises(SessionRestoreError) as raised:
        store.load(session_id)

    assert raised.value.error_type == "ValueError"


@pytest.mark.parametrize(
    "damage",
    [
        lambda replay: replay.__setitem__("cache_epoch", "0"),
        lambda replay: replay["items"][0].__setitem__("content", 42),
        lambda replay: replay.__setitem__(
            "tools", [{"type": "function", "function": {"name": 42}}]
        ),
        lambda replay: replay.__setitem__(
            "item_provenance", [{"source_event_ids": "not-a-list"}]
        ),
        lambda replay: replay.__setitem__("schema_version", 4),
    ],
)
def test_self_consistent_replay_with_invalid_raw_semantics_is_terminal(
    tmp_path: Path, damage
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "saved"}], model="model"
    )
    replay_path = tmp_path / session_id / "replay.json"
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    damage(replay)
    _rehash_replay(replay)
    replay_path.write_text(json.dumps(replay), encoding="utf-8")

    with pytest.raises(SessionRestoreError) as raised:
        store.load(session_id)

    assert raised.value.phase == "replay_validate"
    assert raised.value.ref == "replay"


def test_replay_accepts_legitimate_multimodal_content(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "inspect"},
                    {"type": "image_url", "image_url": {"url": "data:image/x"}},
                ],
            }
        ],
        model="model",
    )

    loaded = store.load(session_id)

    assert loaded is not None
    assert isinstance(loaded.messages[0]["content"], list)


def test_manifest_runtime_state_is_strictly_validated(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "saved"}], model="model"
    )
    manifest_path = tmp_path / session_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime_state"]["approval_rules"] = "deny everything"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SessionRestoreError) as runtime_error:
        store.load(session_id)
    assert runtime_error.value.error_type == "SessionRuntimeStateValidationError"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", "x" * 1_025),
        ("skills_disabled", [f"skill_{index}" for index in range(257)]),
        (
            "approval_rules",
            [{"action": "allow", "pattern": "x" * 1_025}],
        ),
        ("plan_state", {"items": [], "explanation": "x" * 1_025}),
        (
            "progress_state",
            {"phase": "ready", "summary": "x" * 1_025},
        ),
    ],
)
def test_manifest_runtime_state_is_bounded_before_consumers_run(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "saved"}], model="model"
    )
    manifest_path = tmp_path / session_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime_state"][field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SessionRestoreError) as raised:
        store.load(session_id)

    assert raised.value.error_type == "SessionRuntimeStateValidationError"


def test_unknown_history_completeness_cannot_claim_behavior_is_clean(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "saved"}], model="model"
    )
    manifest_path = tmp_path / session_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["history_completeness"] = "clean_enough"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SessionRestoreError) as raised:
        store.load(session_id)

    assert raised.value.error_type == "SessionManifestValidationError"


def test_inventory_reuses_full_plan_validation_before_selecting_latest(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "saved"}], model="model"
    )
    manifest_path = tmp_path / session_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime_state"]["plan_state"] = {
        "items": [
            {
                "step": f"step {index}",
                "active_form": f"doing {index}",
                "status": "pending",
            }
            for index in range(21)
        ]
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    inventory = store.list_result(fingerprint="local")
    assert inventory.sessions == ()
    assert inventory.issues[0].error_type == "SessionRuntimeStateValidationError"
    with pytest.raises(SessionRestoreError) as raised:
        store.get_latest_result(fingerprint="local")
    assert raised.value.error_type == "SessionRuntimeStateValidationError"


@pytest.mark.parametrize(
    "approval_rule",
    [
        {"tool_name": "execute_command"},
        {"tool_name": "execute_command", "action": 1},
        {"tool_name": "execute_command", "action": "future_action"},
    ],
)
def test_persisted_approval_rule_requires_explicit_known_action(
    tmp_path: Path,
    approval_rule: dict,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "saved"}], model="model"
    )
    manifest_path = tmp_path / session_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime_state"]["approval_rules"] = [approval_rule]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SessionRestoreError) as raised:
        store.load(session_id)
    assert raised.value.phase == "manifest_validate"
    assert raised.value.error_type == "SessionRuntimeStateValidationError"


@pytest.mark.parametrize(
    ("persisted_counts", "expected_error_type"),
    [
        (1, "MessageTokenCountsValidationError"),
        ([-1], "MessageTokenCountsValidationError"),
        ([1, 2], "MessageTokenCountMismatch"),
    ],
)
def test_invalid_token_projections_restore_degraded_and_are_recomputed(
    tmp_path: Path,
    persisted_counts,
    expected_error_type: str,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "authoritative replay"}],
        model="model",
        total_prompt_tokens=10,
        total_completion_tokens=5,
    )
    manifest_path = tmp_path / session_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["total_prompt_tokens"] = "broken"
    manifest["total_completion_tokens"] = -1
    manifest["message_token_counts"] = persisted_counts
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    loaded = store.load(session_id)

    assert loaded is not None
    assert loaded.messages[0]["content"] == "authoritative replay"
    recomputed = loaded.messages[0][MESSAGE_TOKEN_KEY]
    assert isinstance(recomputed, int) and not isinstance(recomputed, bool)
    assert recomputed >= 0
    assert loaded.total_prompt_tokens == 0
    assert loaded.total_completion_tokens == 0
    facts = {(issue.error_type, issue.ref) for issue in loaded.restore_issues}
    assert (expected_error_type, "message_token_counts") in facts
    assert (
        "TokenUsageCounterValidationError",
        "total_prompt_tokens",
    ) in facts
    assert (
        "TokenUsageCounterValidationError",
        "total_completion_tokens",
    ) in facts

    store.save(
        messages=loaded.messages,
        model=loaded.model,
        session_id=session_id,
        total_prompt_tokens=loaded.total_prompt_tokens,
        total_completion_tokens=loaded.total_completion_tokens,
        history_events=loaded.history_events,
        replay_envelope=loaded.replay_envelope,
        history_completeness=loaded.history_completeness,
        restore_issues=loaded.restore_issues,
    )
    reloaded = store.load(session_id)
    assert reloaded is not None
    assert expected_error_type in {
        issue.error_type for issue in reloaded.restore_issues
    }


def test_manifest_identity_is_strictly_validated(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "saved"}], model="model"
    )
    manifest_path = tmp_path / session_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["id"] = "session_other"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SessionRestoreError) as identity_error:
        store.load(session_id)
    assert identity_error.value.error_type == "SessionIdentityMismatchError"


@pytest.mark.parametrize(
    ("raw_restore_issues", "preserves_valid_fact"),
    [
        ("broken-observability-carrier", False),
        (
            [
                {"phase": "../unsafe"},
                {
                    "phase": "request_record_decode",
                    "error_type": "JSONDecodeError",
                    "ref": "request_record",
                },
            ],
            True,
        ),
    ],
)
def test_invalid_restore_issue_metadata_is_degraded_not_terminal(
    tmp_path: Path,
    raw_restore_issues,
    preserves_valid_fact: bool,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "authoritative replay"}],
        model="model",
    )
    manifest_path = tmp_path / session_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["restore_issues"] = raw_restore_issues
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    loaded = store.load(session_id)

    assert loaded is not None
    assert loaded.messages[0]["content"] == "authoritative replay"
    validation_issue = next(
        issue
        for issue in loaded.restore_issues
        if issue.phase == "restore_issues_validate"
    )
    assert validation_issue.error_type == "SessionRestoreIssuesValidationError"
    assert validation_issue.ref == "restore_issues"
    assert (
        any(issue.phase == "request_record_decode" for issue in loaded.restore_issues)
        is preserves_valid_fact
    )
    assert "broken-observability-carrier" not in "\n".join(
        issue.render() for issue in loaded.restore_issues
    )

    store.save(
        messages=loaded.messages,
        model=loaded.model,
        session_id=session_id,
        history_events=loaded.history_events,
        replay_envelope=loaded.replay_envelope,
        history_completeness=loaded.history_completeness,
        restore_issues=loaded.restore_issues,
    )
    reloaded = store.load(session_id)
    assert reloaded is not None
    assert any(
        issue.phase == "restore_issues_validate" for issue in reloaded.restore_issues
    )


@pytest.mark.parametrize(
    ("schema_version", "expected_error_type"),
    [
        (True, "SessionStorageSchemaValidationError"),
        (0, "SessionStorageSchemaValidationError"),
        (3, "UnsupportedSessionStorageSchemaError"),
    ],
)
def test_present_manifest_storage_schema_version_is_bounded(
    tmp_path: Path,
    schema_version,
    expected_error_type: str,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "saved"}], model="model"
    )
    manifest_path = tmp_path / session_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["storage_schema_version"] = schema_version
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SessionRestoreError) as raised:
        store.load(session_id)
    assert raised.value.phase == "manifest_validate"
    assert raised.value.error_type == expected_error_type
    assert raised.value.ref == "manifest"


def test_missing_manifest_schema_is_legacy_compatible_but_unknown_policy_is_not(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "saved"}], model="model"
    )
    manifest_path = tmp_path / session_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("storage_schema_version")
    manifest.pop("event_payload_policy")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    loaded = store.load(session_id)
    assert loaded is not None
    assert loaded.messages[0]["content"] == "saved"

    manifest["event_payload_policy"] = "future_delta_encoding"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SessionRestoreError) as raised:
        store.load(session_id)
    assert raised.value.error_type == "EventPayloadPolicyValidationError"


def test_invalid_saved_at_is_an_inventory_failure_not_an_epoch_rank(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "saved"}], model="model"
    )
    manifest_path = tmp_path / session_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["saved_at"] = "not-a-date"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    inventory = store.list_result(fingerprint="local")
    assert inventory.sessions == ()
    issue = inventory.issues[0]
    assert issue.phase == "manifest_validate"
    assert issue.error_type == "ValueError"
    with pytest.raises(SessionRestoreError) as raised:
        store.get_latest(fingerprint="local")
    assert raised.value.phase == "manifest_validate"


def test_invalid_manifest_fingerprint_is_unknown_and_blocks_all_scopes(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "remote"}],
        model="model",
        fingerprint="remote:peer",
    )
    manifest_path = tmp_path / session_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["fingerprint"] = 42
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    for fingerprint in ("local", "remote:peer"):
        with pytest.raises(SessionRestoreError) as raised:
            store.get_latest(fingerprint=fingerprint)
        assert raised.value.error_type == "SessionFingerprintValidationError"


def test_untrusted_inventory_sidecar_cannot_scope_a_corrupt_manifest(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "remote"}],
        model="model",
        fingerprint="remote:peer",
    )
    session_dir = tmp_path / session_id
    manifest_path = session_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["fingerprint"] = 42
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (session_dir / "inventory.json").write_text(
        json.dumps(
            {
                "id": session_id,
                "fingerprint": "remote:peer",
                "untrusted_extra": True,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SessionRestoreError) as local_error:
        store.get_latest(fingerprint="local")
    with pytest.raises(SessionRestoreError) as remote_error:
        store.get_latest(fingerprint="remote:peer")

    assert local_error.value.error_type == "SessionFingerprintValidationError"
    assert remote_error.value.error_type == "SessionFingerprintValidationError"


@pytest.mark.parametrize(
    "unsafe_fingerprint",
    ["remote:peer\nunsafe", "x" * 257],
)
def test_unsafe_manifest_fingerprint_fails_without_echoing_content(
    tmp_path: Path,
    unsafe_fingerprint: str,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "remote"}],
        model="model",
        fingerprint="remote:peer",
    )
    manifest_path = tmp_path / session_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["fingerprint"] = unsafe_fingerprint
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SessionRestoreError) as load_error:
        store.load(session_id)
    assert load_error.value.error_type == "SessionFingerprintValidationError"
    assert unsafe_fingerprint not in str(load_error.value)

    for fingerprint in ("local", "remote:peer"):
        with pytest.raises(SessionRestoreError) as latest_error:
            store.get_latest(fingerprint=fingerprint)
        assert latest_error.value.error_type == "SessionFingerprintValidationError"
        assert unsafe_fingerprint not in str(latest_error.value)


def test_inventory_rank_failure_is_reported_instead_of_using_epoch(
    tmp_path: Path, monkeypatch
) -> None:
    store = SessionStore(tmp_path)
    healthy_id = store.save(
        messages=[{"role": "user", "content": "healthy older session"}],
        model="model",
    )
    session_id = store.save(
        messages=[{"role": "user", "content": "rank unknown"}], model="model"
    )
    manifest_path = tmp_path / session_id / "manifest.json"
    original_metadata_rank = store._metadata_rank

    def fail_manifest_rank(metadata, source_path: Path, *, ref: str):
        if source_path == manifest_path:
            raise SessionRestoreError(
                phase="inventory_rank",
                error_type="PermissionError",
                ref=ref,
            )
        return original_metadata_rank(metadata, source_path, ref=ref)

    monkeypatch.setattr(store, "_metadata_rank", fail_manifest_rank)

    inventory = store.list_result(fingerprint="local")
    assert [item.id for item in inventory.sessions] == [healthy_id]
    assert any(
        issue.phase == "inventory_rank" and issue.error_type == "PermissionError"
        for issue in inventory.issues
    )
    with pytest.raises(SessionRestoreError) as raised:
        store.get_latest(fingerprint="local")
    assert raised.value.phase == "inventory_rank"
    assert "must-not-leak" not in str(raised.value)


def test_corrupt_history_restores_degraded_with_safe_facts(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "replay survives"}], model="model"
    )
    sentinel = "history-secret-must-not-leak"
    events_path = tmp_path / session_id / "events.jsonl"
    with events_path.open("a", encoding="utf-8") as stream:
        stream.write('{"broken":"' + sentinel + "\n")

    loaded = store.load(session_id)

    assert loaded is not None
    assert loaded.messages[0]["content"] == "replay survives"
    assert loaded.history_completeness == "degraded"
    decode_issue = next(
        issue for issue in loaded.restore_issues if issue.phase == "history_decode"
    )
    assert decode_issue.error_type == "JSONDecodeError"
    assert decode_issue.ref == "history_ledger"
    assert sentinel not in decode_issue.render()


def test_semantically_corrupt_history_disables_behavior_tail_and_reserves_seq(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "snapshot"}], model="model"
    )
    events_path = tmp_path / session_id / "events.jsonl"
    malformed = {
        "schema_version": 2,
        "seq": 500,
        "event_id": "he_malformed",
        "kind": "message_committed",
        "timestamp": 1.0,
        "session_generation": 0,
        "payload": {
            "source": "user_input",
            "message": {"role": "user", "content": 42},
        },
        "session_id": session_id,
        "artifact_refs": [],
        "supersedes_event_ids": [],
    }
    valid_tail = {
        **malformed,
        "seq": 501,
        "event_id": "he_valid_tail",
        "payload": {
            "source": "user_input",
            "message": {"role": "user", "content": "must not project"},
        },
    }
    with events_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(malformed) + "\n")
        stream.write(json.dumps(valid_tail) + "\n")

    loaded = store.load(session_id)

    assert loaded is not None
    assert [message["content"] for message in loaded.messages] == ["snapshot"]
    assert any(event.event_id == "he_valid_tail" for event in loaded.history_events)
    assert loaded.history_behavior_projection_safe is False
    assert loaded.history_next_seq_floor == 501
    ledger = HistoryLedger(
        loaded.history_events,
        next_seq_floor=loaded.history_next_seq_floor,
        session_id=session_id,
    )
    ledger.bind_jsonl(events_path)
    appended = ledger.append("normal_runtime_event", {"visible": True})
    assert appended.seq == 502

    restored_again = store.load(session_id)
    assert restored_again is not None
    assert any(event.seq == 502 for event in restored_again.history_events)
    assert restored_again.history_next_seq_floor == 502


@pytest.mark.parametrize(
    ("kind", "event_payload"),
    [
        (
            "subagent_job_changed",
            {
                "job_id": "sj_bad",
                "mode": "execute",
                "task": "unsafe projection",
                "status": "completed",
                "created_at": 1.0,
                "progress": {"not": "a list"},
            },
        ),
        (
            "subagent_communication_queued",
            {
                "item_id": "msg_bad",
                "direction": "child_to_parent",
                "generation": 0,
                "seq": "not-an-integer",
                "content": "must not project",
                "sender_agent_id": "child",
                "recipient_agent_id": "root",
                "created_at": 1.0,
                "kind": "milestone",
            },
        ),
    ],
)
def test_malformed_subagent_behavior_degrades_the_whole_history_projection(
    tmp_path: Path, kind: str, event_payload: dict
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "authoritative snapshot"}],
        model="model",
    )
    malformed = {
        "schema_version": 2,
        "seq": 500,
        "event_id": f"he_bad_{kind}",
        "kind": kind,
        "timestamp": 1.0,
        "session_generation": 0,
        "payload": event_payload,
        "session_id": session_id,
        "artifact_refs": [],
        "supersedes_event_ids": [],
    }
    with (tmp_path / session_id / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(malformed) + "\n")

    loaded = store.load(session_id)

    assert loaded is not None
    assert loaded.messages[0]["content"] == "authoritative snapshot"
    assert loaded.history_completeness == "degraded"
    assert loaded.history_behavior_projection_safe is False
    assert loaded.history_next_seq_floor == 500
    assert any(
        issue.phase == "history_decode" and issue.ref == "history_ledger"
        for issue in loaded.restore_issues
    )


@pytest.mark.parametrize(
    ("kind", "event_payload"),
    [
        (
            "usage_observed",
            {
                "actual_prompt_tokens": "not-an-integer",
                "cached_input_tokens": None,
                "local_request_estimate": 1,
                "local_history_estimate": 1,
                "request_boundary": "attempt",
                "model_profile": "model",
            },
        ),
        (
            "steering_admitted",
            {
                "steering_id": "steer_bad",
                "turn_id": "turn_bad",
                "generation": "not-an-integer",
                "content": "must not project",
            },
        ),
        ("approval_requested", {"request_id": 42, "tool_name": "shell"}),
        (
            "plan_updated",
            {
                "owner_agent_id": "root",
                "session_generation": 0,
                "revision": 1,
                "items": "not-a-list",
                "explanation": None,
                "tool_call_id": "call_plan",
            },
        ),
    ],
)
def test_malformed_consumed_history_kind_disables_behavior_projection(
    tmp_path: Path,
    kind: str,
    event_payload: dict,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "snapshot"}], model="model"
    )
    event = {
        "schema_version": 2,
        "seq": 500,
        "event_id": f"he_bad_{kind}",
        "kind": kind,
        "timestamp": 1.0,
        "session_generation": 0,
        "payload": event_payload,
        "session_id": session_id,
        "artifact_refs": [],
        "supersedes_event_ids": [],
    }
    with (tmp_path / session_id / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event) + "\n")

    loaded = store.load(session_id)

    assert loaded is not None
    assert loaded.history_completeness == "degraded"
    assert loaded.history_behavior_projection_safe is False
    assert loaded.history_next_seq_floor == 500
    assert any(issue.phase == "history_decode" for issue in loaded.restore_issues)


def test_history_rejects_future_schema_and_cross_session_identity(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "snapshot"}], model="model"
    )
    events_path = tmp_path / session_id / "events.jsonl"
    future = {
        "schema_version": 3,
        "seq": 100,
        "event_id": "he_future",
        "kind": "audit",
        "timestamp": 1.0,
        "session_generation": 0,
        "payload": {},
        "session_id": session_id,
        "artifact_refs": [],
        "supersedes_event_ids": [],
    }
    foreign = {
        **future,
        "schema_version": 2,
        "seq": 101,
        "event_id": "he_foreign",
        "session_id": "other-session",
    }
    with events_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(future) + "\n")
        stream.write(json.dumps(foreign) + "\n")

    loaded = store.load(session_id)

    assert loaded is not None
    assert loaded.history_completeness == "degraded"
    assert loaded.history_next_seq_floor == 101
    assert not any(event.seq in {100, 101} for event in loaded.history_events)


def test_history_physical_line_count_reserves_sequence_without_raw_seq(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "snapshot"}], model="model"
    )
    events_path = tmp_path / session_id / "events.jsonl"
    with events_path.open("a", encoding="utf-8") as stream:
        stream.write("{}\n" * 5)

    loaded = store.load(session_id)

    assert loaded is not None
    assert loaded.history_next_seq_floor == 7
    ledger = HistoryLedger(
        loaded.history_events,
        next_seq_floor=loaded.history_next_seq_floor,
    )
    assert ledger.append("normal_runtime_event", {}).seq == 8


def test_history_read_failure_restores_degraded_without_empty_claim(
    tmp_path: Path, monkeypatch
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "replay survives"}], model="model"
    )
    events_path = tmp_path / session_id / "events.jsonl"
    sentinel = "history-read-secret-must-not-leak"
    original_stat = Path.stat

    def fail_history_stat(path: Path, *args, **kwargs):
        if path == events_path:
            raise PermissionError(sentinel)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fail_history_stat)

    loaded = store.load(session_id)

    assert loaded is not None
    assert loaded.messages[0]["content"] == "replay survives"
    assert loaded.history_completeness == "degraded"
    read_issue = next(
        issue for issue in loaded.restore_issues if issue.phase == "history_read"
    )
    assert read_issue.error_type == "PermissionError"
    assert read_issue.ref == "history_ledger"
    assert sentinel not in read_issue.render()


def test_optional_record_corruption_restores_with_agent_visible_facts(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    request_record = _request_envelopes(1)[0]
    checkpoint = CompactionCheckpoint.create(
        trigger="quality_wall",
        strategy=["summarize"],
        source_history_version=1,
        replacement_history=[{"role": "user", "content": "safe replay"}],
        tokens_before=100,
        tokens_after=50,
        preserved_rounds=1,
    )
    session_id = store.save(
        messages=[{"role": "user", "content": "safe replay"}],
        model="model",
        request_envelopes=[request_record],
        checkpoints=[checkpoint],
    )
    sentinel = "optional-record-content-must-not-leak"
    (
        tmp_path / session_id / "requests" / f"{request_record.request_id}.json"
    ).write_text(
        '{"broken":"' + sentinel,
        encoding="utf-8",
    )
    (tmp_path / session_id / "checkpoints" / f"{checkpoint.id}.json").write_text(
        '{"broken":"' + sentinel,
        encoding="utf-8",
    )

    loaded = store.load(session_id)

    assert loaded is not None
    assert loaded.messages[0]["content"] == "safe replay"
    assert loaded.request_envelopes == []
    assert loaded.checkpoints == []
    assert loaded.history_completeness == "legacy_compacted_or_unknown"
    facts = {issue.phase: issue for issue in loaded.restore_issues}
    assert facts["request_record_decode"].error_type == "JSONDecodeError"
    assert facts["request_record_decode"].ref == "request_record"
    assert facts["checkpoint_decode"].error_type == "JSONDecodeError"
    assert facts["checkpoint_decode"].ref == "checkpoint"
    assert sentinel not in "\n".join(issue.render() for issue in loaded.restore_issues)

    store.save(
        messages=loaded.messages,
        model=loaded.model,
        session_id=session_id,
        history_events=loaded.history_events,
        replay_envelope=loaded.replay_envelope,
        history_completeness=loaded.history_completeness,
        restore_issues=loaded.restore_issues,
    )
    reloaded = store.load(session_id)
    assert reloaded is not None
    persisted_phases = {issue.phase for issue in reloaded.restore_issues}
    assert {"request_record_decode", "checkpoint_decode"} <= persisted_phases


def test_optional_artifact_symlinks_are_dropped_with_visible_issues(
    tmp_path: Path,
) -> None:
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir)
    request = _request_envelopes(1)[0]
    checkpoint = CompactionCheckpoint.create(
        trigger="quality_wall",
        strategy=["summarize"],
        source_history_version=1,
        replacement_history=[{"role": "user", "content": "safe replay"}],
        tokens_before=100,
        tokens_after=50,
        preserved_rounds=1,
    )
    session_id = store.save(
        messages=[{"role": "user", "content": "safe replay"}],
        model="model",
        request_envelopes=[request],
        checkpoints=[checkpoint],
    )
    request_path = sessions_dir / session_id / "requests" / f"{request.request_id}.json"
    checkpoint_path = (
        sessions_dir / session_id / "checkpoints" / f"{checkpoint.id}.json"
    )
    outside_request = tmp_path / "outside-request.json"
    outside_checkpoint = tmp_path / "outside-checkpoint.json"
    request_path.replace(outside_request)
    checkpoint_path.replace(outside_checkpoint)
    request_path.symlink_to(outside_request)
    checkpoint_path.symlink_to(outside_checkpoint)

    loaded = store.load(session_id)

    assert loaded is not None
    assert loaded.request_envelopes == []
    assert loaded.checkpoints == []
    assert {(issue.error_type, issue.ref) for issue in loaded.restore_issues} >= {
        ("SymbolicLinkError", "request_record"),
        ("SymbolicLinkError", "checkpoint"),
    }


def test_optional_record_ids_and_semantics_are_validated(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    request_record = _request_envelopes(1)[0]
    checkpoint = CompactionCheckpoint.create(
        trigger="quality_wall",
        strategy=["summarize"],
        source_history_version=1,
        replacement_history=[{"role": "user", "content": "safe replay"}],
        tokens_before=100,
        tokens_after=50,
        preserved_rounds=1,
    )
    session_id = store.save(
        messages=[{"role": "user", "content": "safe replay"}],
        model="model",
        request_envelopes=[request_record],
        checkpoints=[checkpoint],
    )
    request_path = (
        tmp_path / session_id / "requests" / f"{request_record.request_id}.json"
    )
    request_payload = json.loads(request_path.read_text(encoding="utf-8"))
    request_payload["request_id"] = "rq_wrong"
    request_path.write_text(json.dumps(request_payload), encoding="utf-8")
    checkpoint_path = tmp_path / session_id / "checkpoints" / f"{checkpoint.id}.json"
    checkpoint_payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint_payload["created_at"] = "not-a-number"
    checkpoint_path.write_text(json.dumps(checkpoint_payload), encoding="utf-8")

    loaded = store.load(session_id)

    assert loaded is not None
    assert loaded.request_envelopes == []
    assert loaded.checkpoints == []
    facts = {issue.phase: issue for issue in loaded.restore_issues}
    assert facts["request_record_validate"].error_type == "ValueError"
    assert facts["checkpoint_validate"].error_type == "ValueError"
    assert loaded.history_completeness == "legacy_compacted_or_unknown"


@pytest.mark.parametrize("unsafe_id", ["../escape", "nested/value", "x" * 129])
def test_save_rejects_unsafe_request_and_checkpoint_ids(
    tmp_path: Path, unsafe_id: str
) -> None:
    store = SessionStore(tmp_path)
    request = replace(_request_envelopes(1)[0], request_id=unsafe_id)
    checkpoint = replace(
        CompactionCheckpoint.create(
            trigger="quality_wall",
            strategy=["summarize"],
            source_history_version=1,
            replacement_history=[{"role": "user", "content": "safe"}],
            tokens_before=10,
            tokens_after=5,
            preserved_rounds=1,
        ),
        id=unsafe_id,
    )

    with pytest.raises(SessionRestoreError) as request_error:
        store.save(
            messages=[{"role": "user", "content": "safe"}],
            model="model",
            request_envelopes=[request],
        )
    assert request_error.value.error_type == "ArtifactIdentityValidationError"
    assert request_error.value.ref == "request_record"

    with pytest.raises(SessionRestoreError) as checkpoint_error:
        store.save(
            messages=[{"role": "user", "content": "safe"}],
            model="model",
            checkpoints=[checkpoint],
        )
    assert checkpoint_error.value.error_type == "ArtifactIdentityValidationError"
    assert checkpoint_error.value.ref == "checkpoint"


def test_legacy_unsafe_optional_ids_are_dropped_with_visible_issues(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = "session_legacy_unsafe_optional_ids"
    request = replace(
        _request_envelopes(1, session_id=session_id)[0], request_id="../rq"
    )
    checkpoint = replace(
        CompactionCheckpoint.create(
            trigger="quality_wall",
            strategy=["summarize"],
            source_history_version=1,
            replacement_history=[{"role": "user", "content": "safe"}],
            tokens_before=10,
            tokens_after=5,
            preserved_rounds=1,
        ),
        id="nested/checkpoint",
    )
    payload = Session(
        id=session_id,
        model="model",
        saved_at="2026-01-01T00:00:00",
        messages=[{"role": "user", "content": "safe"}],
        request_envelopes=[request],
        checkpoints=[checkpoint],
    ).to_dict()
    (tmp_path / f"{session_id}.json").write_text(json.dumps(payload), encoding="utf-8")

    loaded = store.load(session_id)

    assert loaded is not None
    assert loaded.request_envelopes == []
    assert loaded.checkpoints == []
    assert {(issue.phase, issue.ref) for issue in loaded.restore_issues} >= {
        ("legacy_request_validate", "request_record"),
        ("legacy_checkpoint_validate", "checkpoint"),
    }


def test_repeated_optional_failures_are_aggregated_with_occurrence_count(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    request_records = _request_envelopes(10)
    session_id = store.save(
        messages=[{"role": "user", "content": "safe replay"}],
        model="model",
        request_envelopes=request_records,
    )
    for request_record in request_records:
        request_path = (
            tmp_path / session_id / "requests" / f"{request_record.request_id}.json"
        )
        request_path.write_text('{"broken":', encoding="utf-8")

    loaded = store.load(session_id)

    assert loaded is not None
    issue = next(
        item for item in loaded.restore_issues if item.phase == "request_record_decode"
    )
    assert issue.error_type == "JSONDecodeError"
    assert issue.ref == "request_record"
    assert issue.count == 10
    assert len(loaded.restore_issues) == 1


def test_restore_issue_overflow_is_explicit_and_persisted(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    issues = [
        SessionRestoreIssue(
            phase=f"phase_{index}",
            error_type=f"Failure_{index}",
            ref=f"artifact_{index}",
        )
        for index in range(9)
    ]

    session_id = store.save(
        messages=[{"role": "user", "content": "saved"}],
        model="model",
        restore_issues=issues,
    )
    manifest = json.loads(
        (tmp_path / session_id / "manifest.json").read_text(encoding="utf-8")
    )
    persisted = manifest["restore_issues"]

    assert len(persisted) == 8
    assert persisted[-1]["error_type"] == "AdditionalIssuesOmitted"
    assert persisted[-1]["count"] == 2
    loaded = store.load(session_id)
    assert loaded is not None
    assert loaded.restore_issues[-1].error_type == "AdditionalIssuesOmitted"
    assert loaded.restore_issues[-1].count == 2


def test_session_store_save_with_exit_appends_exit_marker(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "bye"}],
        model="gpt-4o",
        is_exit=True,
    )

    loaded = store.load(session_id)
    assert loaded is not None
    assert loaded.messages[-1]["role"] == "user"
    assert loaded.messages[-1]["content"].startswith("[SESSION_EXIT]")
    assert isinstance(loaded.messages[-1].get(MESSAGE_TOKEN_KEY), int)
    assert store.get_exit_time(loaded.messages) is not None


def test_session_store_places_synthetic_tool_result_before_exit_marker(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_pending",
                        "type": "function",
                        "function": {"name": "edit_file", "arguments": "{}"},
                    }
                ],
            }
        ],
        model="gpt-4o",
        is_exit=True,
    )

    loaded = store.load(session_id)

    assert loaded is not None
    assert [message["role"] for message in loaded.messages] == [
        "assistant",
        "tool",
        "user",
    ]
    assert loaded.messages[1]["tool_call_id"] == "call_pending"
    assert loaded.messages[2]["content"].startswith("[SESSION_EXIT]")
    assert any(
        issue.phase == "message_reconcile"
        and issue.error_type == "SynthesizedToolResult"
        and issue.ref == "message_history"
        for issue in loaded.restore_issues
    )


def test_session_store_reports_filled_and_discarded_tool_results(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_one",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_one", "content": ""},
            {"role": "tool", "tool_call_id": "call_one", "content": "duplicate"},
            {"role": "tool", "tool_call_id": "call_orphan", "content": "orphan"},
        ],
        model="model",
    )

    loaded = store.load(session_id)

    assert loaded is not None
    facts = {issue.error_type: issue for issue in loaded.restore_issues}
    assert facts["FilledToolResultContent"].count == 0
    assert facts["DiscardedToolResult"].count == 2


def test_load_reports_tool_result_synthesized_from_legacy_messages(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_persisted",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }
        ],
    }
    session_id = "session_legacy_missing_tool_result"
    legacy = Session(
        id=session_id,
        model="model",
        saved_at="2026-01-01T00:00:00",
        messages=[assistant],
    )
    (tmp_path / f"{session_id}.json").write_text(
        json.dumps(legacy.to_dict()), encoding="utf-8"
    )

    loaded = store.load(session_id)

    assert loaded is not None
    assert [message["role"] for message in loaded.messages] == ["assistant", "tool"]
    assert loaded.messages[1]["tool_call_id"] == "call_persisted"
    assert any(
        issue.phase == "message_reconcile"
        and issue.error_type == "SynthesizedToolResult"
        for issue in loaded.restore_issues
    )


def test_session_store_append_system_message_updates_existing_session(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "hello"}], model="gpt-4o"
    )

    store.append_system_message(
        session_id,
        "gpt-4o",
        "[LLM_ERROR_DIAGNOSTIC] path=/tmp/demo.json error=BadRequestError: boom",
        active_mode="coder",
    )

    loaded = store.load(session_id)
    assert loaded is not None
    assert loaded.messages[-1]["role"] == "user"
    assert loaded.messages[-1]["content"].startswith("<session_diagnostic")
    assert "[LLM_ERROR_DIAGNOSTIC]" in loaded.messages[-1]["content"]
    assert isinstance(loaded.messages[-1].get(MESSAGE_TOKEN_KEY), int)


def test_session_store_load_backfills_missing_message_token_counts(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = "legacy_missing_tokens"
    path = tmp_path / f"{session_id}.json"
    legacy = Session(
        id=session_id,
        model="gpt-4o",
        saved_at="2026-01-01T00:00:00",
        messages=[{"role": "user", "content": "hello"}],
    )
    path.write_text(json.dumps(legacy.to_dict(), ensure_ascii=False), encoding="utf-8")

    loaded = store.load(session_id)
    assert loaded is not None
    assert isinstance(loaded.messages[0].get(MESSAGE_TOKEN_KEY), int)
    assert not path.exists()
    assert (tmp_path / session_id / "replay.json").exists()


def test_session_store_load_repairs_legacy_out_of_order_tool_results(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = "legacy_tool_order"
    path = tmp_path / f"{session_id}.json"
    assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_legacy",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }
        ],
    }
    legacy = Session(
        id=session_id,
        model="gpt-4o",
        saved_at="2026-01-01T00:00:00",
        messages=[
            assistant,
            {"role": "user", "content": "[SESSION_EXIT] old"},
            {
                "role": "tool",
                "tool_call_id": "call_legacy",
                "content": "recovered later",
            },
        ],
    )
    path.write_text(json.dumps(legacy.to_dict(), ensure_ascii=False), encoding="utf-8")

    loaded = store.load(session_id)

    assert loaded is not None
    assert [message["role"] for message in loaded.messages] == [
        "assistant",
        "tool",
        "user",
    ]
    persisted = store.load(session_id)
    assert persisted is not None
    assert [message["role"] for message in persisted.messages] == [
        "assistant",
        "tool",
        "user",
    ]
    assert any(
        issue.error_type == "ReorderedToolResult" for issue in persisted.restore_issues
    )


def test_legacy_session_identity_mismatch_is_terminal_and_inventory_visible(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    requested_id = "session_legacy_requested"
    legacy = Session(
        id="session_legacy_other",
        model="model",
        saved_at="2026-01-01T00:00:00",
        messages=[{"role": "user", "content": "saved"}],
    )
    (tmp_path / f"{requested_id}.json").write_text(
        json.dumps(legacy.to_dict()), encoding="utf-8"
    )

    with pytest.raises(SessionRestoreError) as raised:
        store.load(requested_id)
    assert raised.value.error_type == "SessionIdentityMismatchError"
    inventory = store.list_result(fingerprint="local")
    assert inventory.sessions == ()
    assert inventory.issues[0].error_type == "SessionIdentityMismatchError"
    with pytest.raises(SessionRestoreError) as latest_error:
        store.get_latest(fingerprint="local")
    assert latest_error.value.error_type == "SessionIdentityMismatchError"


def test_legacy_history_event_from_another_session_is_terminal(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = "session_legacy_history_owner"
    foreign_ledger = HistoryLedger(session_id="session_foreign")
    foreign_ledger.append_message(
        {"role": "user", "content": "foreign behavioral history"},
        source="user_input",
    )
    payload = Session(
        id=session_id,
        model="model",
        saved_at="2026-01-01T00:00:00",
        messages=[{"role": "user", "content": "authoritative snapshot"}],
        history_events=list(foreign_ledger.events),
    ).to_dict()
    payload["history_events"] = [
        event.to_dict() for event in foreign_ledger.events
    ]
    (tmp_path / f"{session_id}.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionRestoreError) as raised:
        store.load(session_id)

    assert raised.value.phase == "legacy_session_validate"
    assert raised.value.error_type == "LegacySessionPayloadValidationError"
    assert raised.value.ref == "legacy_session"


def test_invalid_present_legacy_runtime_and_history_do_not_default_cleanly(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = "session_legacy_invalid"
    payload = Session(
        id=session_id,
        model="model",
        saved_at="2026-01-01T00:00:00",
        messages=[{"role": "user", "content": "saved"}],
    ).to_dict()
    payload["runtime_state"] = {"skills_disabled": "all"}
    legacy_path = tmp_path / f"{session_id}.json"
    legacy_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionRestoreError) as runtime_error:
        store.load(session_id)
    assert runtime_error.value.error_type == "SessionRuntimeStateValidationError"

    payload["runtime_state"] = {}
    payload["history_events"] = [{}]
    legacy_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SessionRestoreError) as history_error:
        store.load(session_id)
    assert history_error.value.error_type == "LegacySessionPayloadValidationError"


def test_invalid_legacy_replay_request_and_checkpoint_restore_degraded(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = "session_legacy_optional_damage"
    payload = Session(
        id=session_id,
        model="model",
        saved_at="2026-01-01T00:00:00",
        messages=[{"role": "user", "content": "authoritative snapshot"}],
    ).to_dict()
    payload["replay_envelope"] = {"schema_version": "3"}
    payload["request_envelopes"] = "damaged"
    payload["checkpoints"] = [{"id": "cc_damaged"}]
    (tmp_path / f"{session_id}.json").write_text(json.dumps(payload), encoding="utf-8")

    inventory = store.list_result(fingerprint="local")
    assert [item.id for item in inventory.sessions] == [session_id]
    assert {issue.ref for issue in inventory.issues} >= {
        "replay",
        "request_record",
        "checkpoint",
    }

    loaded = store.load(session_id)

    assert loaded is not None
    assert loaded.messages[0]["content"] == "authoritative snapshot"
    assert loaded.replay_envelope is not None and loaded.replay_envelope.validate()
    assert loaded.request_envelopes == []
    assert loaded.checkpoints == []
    assert {issue.ref for issue in loaded.restore_issues} >= {
        "replay",
        "request_record",
        "checkpoint",
    }


def test_invalid_legacy_token_projection_is_degraded_and_recomputed(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = "session_legacy_bad_tokens"
    payload = Session(
        id=session_id,
        model="model",
        saved_at="2026-01-01T00:00:00",
        messages=[
            {
                "role": "user",
                "content": "authoritative legacy message",
                MESSAGE_TOKEN_KEY: -99,
            }
        ],
    ).to_dict()
    payload["total_prompt_tokens"] = "broken"
    payload["total_completion_tokens"] = -1
    payload["restore_issues"] = {"broken": "legacy-observability-carrier"}
    legacy_path = tmp_path / f"{session_id}.json"
    legacy_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = store.load(session_id)

    assert loaded is not None
    assert loaded.messages[0]["content"] == "authoritative legacy message"
    recomputed = loaded.messages[0][MESSAGE_TOKEN_KEY]
    assert isinstance(recomputed, int) and recomputed >= 0
    assert loaded.total_prompt_tokens == 0
    assert loaded.total_completion_tokens == 0
    facts = {(issue.error_type, issue.ref) for issue in loaded.restore_issues}
    assert (
        "MessageTokenMetadataValidationError",
        "message_token_metadata",
    ) in facts
    assert (
        "TokenUsageCounterValidationError",
        "total_prompt_tokens",
    ) in facts
    assert (
        "TokenUsageCounterValidationError",
        "total_completion_tokens",
    ) in facts
    assert any(
        issue.phase == "restore_issues_validate"
        and issue.error_type == "SessionRestoreIssuesValidationError"
        for issue in loaded.restore_issues
    )
    assert not legacy_path.exists()
    assert (tmp_path / session_id / "manifest.json").exists()


def test_session_store_list_filters_by_fingerprint(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    local_id = store.save(
        messages=[{"role": "user", "content": "first"}], model="m1", fingerprint="local"
    )
    remote_id = store.save(
        messages=[{"role": "user", "content": "second"}],
        model="m2",
        fingerprint="remote:abc",
    )
    (tmp_path / "broken.json").write_text("{not-json}", encoding="utf-8")

    local_sessions = store.list(limit=10, fingerprint="local")
    remote_sessions = store.list(limit=10, fingerprint="remote:abc")
    all_sessions = store.list(limit=10, fingerprint=None)

    assert [item.id for item in local_sessions] == [local_id]
    assert [item.id for item in remote_sessions] == [remote_id]
    assert {item.id for item in all_sessions} == {local_id, remote_id}
    with pytest.raises(SessionRestoreError):
        store.get_latest(fingerprint="local")
    # A corrupt legacy payload has no independently validated fingerprint hint,
    # so it must block every filtered latest-session selection.
    with pytest.raises(SessionRestoreError):
        store.get_latest(fingerprint="remote:abc")


def test_session_store_get_latest_prefers_recently_updated_session(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    first_id = store.save(
        messages=[{"role": "user", "content": "first"}], model="m1", fingerprint="local"
    )
    second_id = store.save(
        messages=[{"role": "user", "content": "second"}],
        model="m2",
        fingerprint="local",
    )

    # Update the older session after the newer one was created.
    store.save(
        messages=[{"role": "user", "content": "first-updated"}],
        model="m1",
        session_id=first_id,
        fingerprint="local",
    )

    latest = store.get_latest(fingerprint="local")
    assert latest is not None
    assert latest.id == first_id
    assert latest.id != second_id


def test_session_store_get_exit_time_returns_none_without_marker() -> None:
    messages = [{"role": "user", "content": "hello"}]
    assert SessionStore.get_exit_time(messages) is None


def test_session_store_get_exit_time_skips_non_string_content() -> None:
    messages = [
        {"role": "user", "content": None},
        {"role": "system", "content": [{"type": "text", "text": "hello"}]},
    ]
    assert SessionStore.get_exit_time(messages) is None


def test_session_store_load_missing_returns_none(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    assert store.load("missing") is None


def test_session_store_concurrent_save_keeps_sessions_readable(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "seed"}], model="m1", fingerprint="local"
    )

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

    threads = [
        threading.Thread(target=update_existing, args=(i,)) for i in range(4)
    ] + [threading.Thread(target=create_new, args=(i,)) for i in range(4)]
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
