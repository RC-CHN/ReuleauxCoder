import pytest

from reuleauxcoder.app.rpc.editor_documents import EditorDocuments
from reuleauxcoder.infrastructure.rpc.peer import RpcError


def test_editor_updates_are_monotonic_and_saved_documents_are_released(tmp_path):
    state = EditorDocuments()
    path = str(tmp_path / "file.py")
    assert state.update(2, [path]) == 2
    assert "Unsaved editor changes" in state.guard(path)
    assert state.update(1, []) == 2
    assert state.guard(path)
    assert state.update(3, []) == 3
    assert state.guard(path) is None


@pytest.mark.parametrize("revision,paths", [(True, []), (-1, []), (1, ["relative"]), (1, [None]), (1, "not-a-list")])
def test_invalid_editor_state_does_not_clear_existing_guard(tmp_path, revision, paths):
    state = EditorDocuments()
    path = str(tmp_path / "file.py")
    state.update(0, [path])
    with pytest.raises(RpcError):
        state.update(revision, paths)
    assert state.guard(path)
