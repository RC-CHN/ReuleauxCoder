from types import SimpleNamespace

from reuleauxcoder.domain.history import HistoryLedger
from reuleauxcoder.extensions.tools.builtin.history import (
    ArtifactReadTool,
    HistoryReadTool,
    HistorySearchTool,
)
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore


def _saved_history(tmp_path):
    ledger = HistoryLedger()
    ledger.append_message(
        {"role": "user", "content": "find the stale cache bug"}, source="user"
    )
    session_id = SessionStore(tmp_path).save(
        messages=[{"role": "user", "content": "current view"}],
        model="model",
        history_events=list(ledger.events),
    )
    return session_id


def _bind(tool, tmp_path):
    tool._agent_config = SimpleNamespace(session_dir=str(tmp_path))
    return tool


def test_history_search_and_read_use_append_only_jsonl(tmp_path) -> None:
    session_id = _saved_history(tmp_path)

    searched = _bind(HistorySearchTool(), tmp_path).execute(session_id, "stale cache")
    read = _bind(HistoryReadTool(), tmp_path).execute(session_id, start_seq=1)

    assert searched.success is True
    assert "stale cache bug" in searched.content
    assert read.success is True
    assert '"seq": 1' in read.content


def test_artifact_read_is_confined_to_session_artifacts(tmp_path) -> None:
    session_id = _saved_history(tmp_path)
    artifact = tmp_path / session_id / "artifacts" / "tools" / "output.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"full":"output"}', encoding="utf-8")
    tool = _bind(ArtifactReadTool(), tmp_path)
    tool.bind_agent(SimpleNamespace(current_session_id=session_id))

    assert (
        tool.preflight_validate(
            {"artifact_ref": "tools/output.json"}, schema_only=True
        )
        is None
    )
    outcome = tool.execute("tools/output.json")
    escaped = tool.execute("../../outside")

    assert outcome.success is True
    assert '"output"' in outcome.content
    assert outcome.metadata["session_id"] == session_id
    assert escaped.success is False


def test_artifact_read_pages_exact_content_and_reports_next_offset(tmp_path) -> None:
    session_id = _saved_history(tmp_path)
    source = "alpha-中文-beta-" * 9
    artifact = tmp_path / session_id / "artifacts" / "tools" / "output.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(source, encoding="utf-8")
    tool = _bind(ArtifactReadTool(), tmp_path)
    tool.bind_agent(SimpleNamespace(current_session_id=session_id))

    pages: list[str] = []
    offset = 0
    while True:
        outcome = tool.execute("tools/output.txt", offset=offset, limit=11)
        assert outcome.success is True
        pages.append(outcome.content or "")
        next_offset = outcome.metadata["next_offset"]
        if next_offset is None:
            assert "Artifact read complete" in outcome.model_text
            break
        assert f"Next offset: {next_offset}" in outcome.model_text
        offset = int(next_offset)

    assert "".join(pages) == source


def test_artifact_read_keeps_explicit_cross_session_access(tmp_path) -> None:
    session_id = _saved_history(tmp_path)
    artifact = tmp_path / session_id / "artifacts" / "tools" / "output.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("cross-session", encoding="utf-8")
    tool = _bind(ArtifactReadTool(), tmp_path)
    tool.bind_agent(SimpleNamespace(current_session_id="another-session"))

    outcome = tool.execute("tools/output.txt", session_id=session_id)

    assert outcome.success is True
    assert outcome.content == "cross-session"
    assert outcome.metadata["session_id"] == session_id


def test_artifact_read_rejects_unbounded_or_unresolved_reads(tmp_path) -> None:
    tool = _bind(ArtifactReadTool(), tmp_path)

    unresolved = tool.execute("tools/output.txt")
    oversized = tool.execute(
        "tools/output.txt",
        limit=12_001,
        session_id="session_test",
    )

    assert unresolved.success is False
    assert "current session is unavailable" in unresolved.model_text
    assert oversized.success is False
    assert "limit must be an integer from 1 to 12000" in oversized.model_text
