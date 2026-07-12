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

    searched = _bind(HistorySearchTool(), tmp_path).execute(
        session_id, "stale cache"
    )
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

    outcome = tool.execute(session_id, "tools/output.json")
    escaped = tool.execute(session_id, "../../outside")

    assert outcome.success is True
    assert '"output"' in outcome.content
    assert escaped.success is False
